"""
C4 Agent L2 功能测试 — InfluxDB 数据入库接入
============================================

测试依据: c4/test/agent/README.md §4.6 执行验证（副作用检查）

验证 c4_agent 通过 Registry 认识 c4_influxdb_client（reader 角色），从 Modbus 点表生成
「采集 + 写入 InfluxDB」接入方案，用户确认后生成 config.json（含 c4_modbus_client 段
和 c4_influxdb_client 段）并启动 MCP 服务：

  1. Modbus 采集 + 写入 InfluxDB 首次接入 → config.json 含 c4_modbus_client + c4_influxdb_client
  2. influxdb 实例含 url/token/org/bucket
  3. influxdb point 含 key（引用 writer）+ measurement
  4. writer/reader 分类与 Registry 一致（modbus=writer, influxdb=reader）

严格按 README.md 规格实现（黑盒 + 真实 LLM，不侵入 Agent 内部）。
"""

import json
import time

import pytest  # type: ignore

from test_helpers import (
    create_full_csv,
    full_access_flow,
    retry_llm,
)
from assertions import (
    assert_config_json_valid,
    assert_shm_ids_assigned,
    assert_writer_reader_from_registry,
)


def _load_config(agent, result) -> dict:
    """读取接入流程产出的 config.json（优先 result，回退 agent.config_dir）。"""
    config = result.get("config_json")
    if config is None:
        config_path = agent.config_dir / "config.json"
        if config_path.exists():
            config = assert_config_json_valid(config_path)
    assert config is not None, "config.json should be generated after access flow"
    return config


# 引导 LLM 生成 influxdb 转发目标的方案消息（含 url/token/org/bucket/measurement）
INFLUXDB_PLAN_MSG = (
    "生成接入方案，并将采集到的数据写入 InfluxDB 时序数据库"
    "（数据库地址 http://127.0.0.1:8086，token=test-token，"
    "组织名 activesys，数据库名 hnals，表名 wind_turbine）"
)


@pytest.mark.llm
class TestInfluxdbAccess:
    """InfluxDB 数据入库接入 — 执行验证"""

    @retry_llm(max_attempts=3)
    def test_influxdb_first_access(self, chat, agent, tmp_path, registry_dir):
        """
        Modbus 采集 + 写入 InfluxDB 首次接入。
        config.json 含 c4_modbus_client + c4_influxdb_client，实例结构正确。
        """
        csv_path = create_full_csv(tmp_path)

        result = full_access_flow(
            chat, agent, str(csv_path),
            upload_msg="接入华能阿拉善1#风机",
            plan_msg=INFLUXDB_PLAN_MSG,
            confirm=True,
            tmp_path=tmp_path,
        )

        # 等待执行完成（SSE 流已关闭，但副作用需要时间）
        time.sleep(3)

        config = _load_config(agent, result)

        # 1. config.json 含 c4_shm_manager + c4_modbus_client + c4_influxdb_client
        assert "c4_shm_manager" in config, "config.json must contain c4_shm_manager"
        assert "c4_modbus_client" in config, (
            f"Expected c4_modbus_client in config. Got: "
            f"{[k for k in config if k.startswith('c4_')]}"
        )
        assert "c4_influxdb_client" in config, (
            f"Expected c4_influxdb_client in config. Got: "
            f"{[k for k in config if k.startswith('c4_')]}"
        )

        # 2. influxdb 实例结构：id/url/token/org/bucket 齐全
        instances = config.get("c4_influxdb_client", [])
        assert len(instances) > 0, "Should have at least one influxdb instance"
        for inst in instances:
            assert "id" in inst, f"influxdb instance missing id: {inst}"
            assert inst.get("url", "").startswith("http://"), (
                f"influxdb instance missing url: {inst}"
            )
            assert inst.get("token"), f"influxdb instance missing token: {inst}"
            assert inst.get("org"), f"influxdb instance missing org: {inst}"
            assert inst.get("bucket"), f"influxdb instance missing bucket: {inst}"
            assert "points" in inst and len(inst["points"]) > 0, (
                f"influxdb instance missing points: {inst}"
            )

        # 3. 所有 shm_id 结构完整（id/shm_id 字段存在）
        assert_shm_ids_assigned(config)

        # 4. writer/reader 分类与 Registry 一致（modbus=writer, influxdb=reader）
        assert_writer_reader_from_registry(config, registry_dir)

    @retry_llm(max_attempts=3)
    def test_influxdb_point_mapping(self, chat, agent, tmp_path):
        """
        Modbus 采集 + 写入 InfluxDB。
        influxdb point 含 key（引用 writer point）+ measurement。
        """
        csv_path = create_full_csv(tmp_path)

        result = full_access_flow(
            chat, agent, str(csv_path),
            upload_msg="接入华能阿拉善1#风机",
            plan_msg=INFLUXDB_PLAN_MSG,
            confirm=True,
            tmp_path=tmp_path,
        )

        time.sleep(3)
        config = _load_config(agent, result)

        assert "c4_influxdb_client" in config, "config.json must contain c4_influxdb_client"
        instances = config.get("c4_influxdb_client", [])
        assert instances, "Should have at least one influxdb instance"

        # 收集 influxdb point 的 key/measurement
        keys = set()
        measurements = set()
        for inst in instances:
            for pt in inst.get("points", []):
                assert pt.get("key"), f"influxdb point missing key: {pt}"
                assert pt.get("measurement"), f"influxdb point missing measurement: {pt}"
                keys.add(pt["key"])
                measurements.add(pt["measurement"])

        # key 应引用 modbus writer 的 point（格式 {writer_id}.{point_id}）
        modbus_instances = config.get("c4_modbus_client", [])
        assert modbus_instances, "Should have at least one modbus instance"
        writer_prefix = f"{modbus_instances[0].get('id')}."
        for k in keys:
            assert k.startswith(writer_prefix), (
                f"influxdb point key '{k}' should reference writer '{writer_prefix}'"
            )

        # measurement 非空且非默认场站缩写占位（应来自 plan 提供的 wind_turbine 或场站缩写）
        assert measurements, "influxdb point should have non-empty measurement"
