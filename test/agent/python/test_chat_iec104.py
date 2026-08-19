"""
C4 Agent L2 功能测试 — IEC104 数据采集接入
============================================

测试依据: c4/test/agent/README.md §4.6 执行验证（副作用检查）

验证 c4_agent 通过 Registry 认识 c4_iec104_client，从 IEC104 点表 CSV 生成接入方案，
用户确认后生成 config.json（含 c4_iec104_client 段）并启动 MCP 服务：

  1. IEC104 首次接入 → config.json 含 c4_iec104_client 段，实例含 id/ip/port/points
  2. 所有 point 的 shm_id 已分配（非 0）
  3. writer/reader 分类与 Registry 一致（c4_iec104_client 为 writer）

严格按 README.md 规格实现（黑盒 + 真实 LLM，不侵入 Agent 内部）。
"""

import json
import time

import pytest  # type: ignore

from test_helpers import (
    full_access_flow,
    retry_llm,
)
from assertions import (
    assert_config_json_valid,
    assert_shm_ids_assigned,
    assert_writer_reader_from_registry,
)


# IEC104 点表：遥信（addr=1）+ 遥测（addr=16385）+ 遥脉（addr=25601），protocol=iec104，port=2404
# 注：point 名须为字母开头（executor 校验 point.id 仅允许 [a-zA-Z][a-zA-Z0-9_.]*）
IEC104_POINTS = [
    "device_name,device_ip,protocol,port,point_name,addr",
    "华能阿拉善1#主变,192.168.110.99,iec104,2404,alarm1,1",
    "华能阿拉善1#主变,192.168.110.99,iec104,2404,uab,16385",
    "华能阿拉善1#主变,192.168.110.99,iec104,2404,energy_total,25601",
]


def _write_iec104_csv(tmp_path) -> str:
    """写 IEC104 点表 CSV，返回文件路径。"""
    csv_path = tmp_path / "iec104_points.csv"
    csv_path.write_text("\n".join(IEC104_POINTS), encoding="utf-8")
    return str(csv_path)


def _load_config(agent, result) -> dict:
    """读取接入流程产出的 config.json（优先 result，回退 agent.config_dir）。"""
    config = result.get("config_json")
    if config is None:
        config_path = agent.config_dir / "config.json"
        if config_path.exists():
            config = assert_config_json_valid(config_path)
    assert config is not None, "config.json should be generated after access flow"
    return config


@pytest.mark.llm
class TestIec104Access:
    """IEC104 数据采集接入 — 执行验证"""

    @retry_llm(max_attempts=3)
    def test_iec104_first_access(self, chat, agent, tmp_path, registry_dir):
        """IEC104 首次接入 → config.json 含 c4_iec104_client，实例结构正确，shm_id 已分配。"""
        csv_path = _write_iec104_csv(tmp_path)

        result = full_access_flow(
            chat, agent, csv_path,
            upload_msg="接入华能阿拉善1#主变",
            plan_msg="生成接入方案（仅采集，不需要转发）",
            confirm=True,
            tmp_path=tmp_path,
        )

        # 等待执行完成（SSE 流已关闭，但副作用需要时间）
        time.sleep(3)

        config = _load_config(agent, result)

        # 1. config.json 含 c4_shm_manager + c4_iec104_client
        assert "c4_shm_manager" in config, "config.json must contain c4_shm_manager"
        assert "c4_iec104_client" in config, (
            f"Expected c4_iec104_client in config. Got: "
            f"{[k for k in config if k.startswith('c4_')]}"
        )

        # 2. 实例结构：id/ip/port/points 齐全，points 含 id/addr
        instances = config.get("c4_iec104_client", [])
        assert len(instances) > 0, "Should have at least one iec104 instance"
        for inst in instances:
            assert "id" in inst, f"IEC104 instance missing id: {inst}"
            assert "ip" in inst, f"IEC104 instance missing ip: {inst}"
            assert "port" in inst, f"IEC104 instance missing port: {inst}"
            assert "points" in inst and len(inst["points"]) > 0, (
                f"IEC104 instance missing points: {inst}"
            )
            for pt in inst["points"]:
                assert "id" in pt, f"IEC104 point missing id: {pt}"
                assert "addr" in pt, f"IEC104 point missing addr: {pt}"

        # 3. 所有 shm_id 已分配（非 0）
        assert_shm_ids_assigned(config)

        # 4. writer/reader 分类与 Registry 一致（c4_iec104_client 为 writer）
        assert_writer_reader_from_registry(config, registry_dir)

    @retry_llm(max_attempts=3)
    def test_iec104_point_addr_preserved(self, chat, agent, tmp_path):
        """IEC104 接入 → point 的 addr（IOA）与点表一致（遥信/遥测/遥脉三类区间）。"""
        csv_path = _write_iec104_csv(tmp_path)

        result = full_access_flow(
            chat, agent, csv_path,
            upload_msg="接入华能阿拉善1#主变",
            plan_msg="生成接入方案（仅采集）",
            confirm=True,
            tmp_path=tmp_path,
        )

        time.sleep(3)
        config = _load_config(agent, result)

        assert "c4_iec104_client" in config, "config.json must contain c4_iec104_client"
        instances = config.get("c4_iec104_client", [])
        assert instances, "Should have at least one iec104 instance"

        # 收集所有 point 的 addr，验证点表的 IOA 全部保留
        addrs = {pt["addr"] for inst in instances for pt in inst.get("points", [])}
        for expected_addr in (1, 16385, 25601):
            assert expected_addr in addrs, (
                f"Expected IOA {expected_addr} in config points, got: {sorted(addrs)}"
            )
