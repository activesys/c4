"""
C4 Agent L1 确定性功能测试 — InfluxDB MCP 连接与生命周期
=========================================================

验证 c4_agent 通过 Registry 认识 c4_influxdb_client（reader 角色），
在启动恢复（无条件 Stop-Start）中通过 MCP 协议连接它并调用 start/stop：

  1. Registry 加载 → GET /api/services 返回含 c4_influxdb_client
  2. config.json 含 c4_influxdb_client 实例 → Agent 启动恢复连接并 start → 进程运行
  3. writer/reader 分类正确（c4_influxdb_client 为 reader）

黑盒测试，不侵入 Agent 内部。
"""

import json
import subprocess
import time
from pathlib import Path
from typing import Optional

import pytest  # type: ignore

from assertions import (
    assert_config_json_valid,
    assert_process_running,
    assert_writer_reader_from_registry,
)
from conftest import (
    AgentHandle,
    _find_free_port,
    write_agent_json,
)


# ──────────────────────────────────────────────
#  测试用 config.json 模板 — asfp2_server(writer) + influxdb(reader)
# ──────────────────────────────────────────────

_INFLUXDB_CONFIG: dict = {
    "c4_shm_manager": {
        "writer": ["c4_asfp2_server"],
        "reader": ["c4_influxdb_client"],
    },
    "c4_asfp2_server": [
        {
            "id": "test_asfp2_srv_1",
            "name": "ASFP2接收服务1",
            "ip": "0.0.0.0",
            "port": 0,
            "points": [
                {
                    "id": "point_1000",
                    "addr": 1000,
                    "shm_id": 0,
                },
            ],
        }
    ],
    "c4_influxdb_client": [
        {
            "id": "test_influx_1",
            "name": "InfluxDB入库1",
            "url": "http://127.0.0.1:8086",
            "token": "test-token",
            "org": "activesys",
            "bucket": "testdb",
            "points": [
                {
                    "key": "test_asfp2_srv_1.point_1000",
                    "measurement": "wind_turbine",
                    "field": "windspeed",
                    "type": "float",
                    "shm_id": 0,
                },
            ],
        }
    ],
}


# ──────────────────────────────────────────────
#  辅助函数
# ──────────────────────────────────────────────


def _start_agent_with_config(
    config_dir: Path,
    registry_dir: Path,
    shm_manager_binary: str,
    agent_binary: str,
    config_content: Optional[dict] = None,
) -> AgentHandle:
    """制备 config_dir，写入 agent.json + 可选的 config.json，启动 Agent。"""
    port = _find_free_port()
    write_agent_json(config_dir, registry_dir, shm_manager_binary, port)

    config_path = config_dir / "config.json"
    if config_content is not None:
        config_path.write_text(
            json.dumps(config_content, indent=2, ensure_ascii=False)
        )

    cmd = [agent_binary, "--config-dir", str(config_dir)]
    if agent_binary.endswith(".js"):
        cmd = ["node", *cmd]
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    base_url = f"http://127.0.0.1:{port}"

    from urllib.request import urlopen

    deadline = time.time() + 60.0
    last_error = None
    ready = False
    while time.time() < deadline:
        try:
            with urlopen(f"{base_url}/api/services", timeout=0.5) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception as e:
            last_error = e
        time.sleep(0.5)

    if not ready:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        pytest.fail(
            f"Agent did not become ready within 60s. "
            f"Base URL: {base_url}, Last error: {last_error}"
        )

    return AgentHandle(process, base_url, port, config_dir, set())


def _teardown_agent(handle: AgentHandle) -> None:
    """关闭 AgentHandle 并终止进程。"""
    if handle.process and handle.process.poll() is None:
        handle.process.terminate()
        try:
            handle.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            handle.process.kill()
            try:
                handle.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


# ══════════════════════════════════════════════
#  1. Registry 加载 — influxdb 出现在服务列表
# ══════════════════════════════════════════════


class TestInfluxdbRegistry:
    """Registry 加载 — c4_influxdb_client 应出现在服务列表中。"""

    def test_influxdb_in_services(self, agent: AgentHandle) -> None:
        """GET /api/services 返回的服务列表含 c4_influxdb_client。"""
        services = agent.get_services()
        assert isinstance(services, list), f"Expected list, got {type(services).__name__}"

        service_types = [s.get("service_type") for s in services if isinstance(s, dict)]
        assert "c4_influxdb_client" in service_types, (
            f"c4_influxdb_client should be in registry services, "
            f"got: {service_types}"
        )


# ══════════════════════════════════════════════
#  2. 启动恢复 — 连接 influxdb 并执行 Stop-Start
# ══════════════════════════════════════════════


class TestInfluxdbStartupRecovery:
    """启动恢复场景 — config.json 含 influxdb 实例。"""

    def test_influxdb_startup_connects_and_starts(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
    ) -> None:
        """
        config.json 含 asfp2_server(writer) + influxdb(reader)。
        Agent 启动恢复时连接 influxdb 并执行 stop → adjust_shm → start，
        验证 influxdb 进程运行 + config 结构完整 + reader 分类正确。
        """
        config_dir = tmp_path / "etc_c4"
        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=_INFLUXDB_CONFIG,
        )

        try:
            # 断言: Agent 就绪
            services = handle.get_services()
            assert isinstance(services, list)

            # 断言: config.json 存在且有效，含 c4_influxdb_client 段
            config = assert_config_json_valid(config_dir / "config.json")
            assert "c4_influxdb_client" in config, (
                f"config.json should contain c4_influxdb_client, got keys: "
                f"{[k for k in config if k.startswith('c4_')]}"
            )

            influx_instances = config.get("c4_influxdb_client", [])
            assert len(influx_instances) == 1, (
                f"Expected 1 influxdb instance, got {len(influx_instances)}"
            )
            inst = influx_instances[0]
            assert inst.get("id") == "test_influx_1"
            assert inst.get("url", "").startswith("http://"), (
                f"influxdb instance should have url, got: {inst}"
            )

            # 断言: writer/reader 分类正确（influxdb 为 reader）
            assert_writer_reader_from_registry(config, registry_dir)

            # 断言: influxdb 进程运行（Agent 已通过 MCP 连接并 start）
            assert_process_running("c4_influxdb_client")

            # 断言: writer 进程运行（asfp2_server 也应被启动）
            assert_process_running("c4_asfp2_server")

        finally:
            _teardown_agent(handle)

    def test_influxdb_point_key_preserved(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
    ) -> None:
        """
        influxdb 实例的 point key/measurement/field/type 应完整保留在 config.json 中。
        """
        config_dir = tmp_path / "etc_c4"
        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=_INFLUXDB_CONFIG,
        )

        try:
            config = assert_config_json_valid(config_dir / "config.json")
            instances = config.get("c4_influxdb_client", [])
            assert instances, "Should have at least one influxdb instance"

            points = instances[0].get("points", [])
            assert len(points) == 1, f"Expected 1 influxdb point, got {len(points)}"

            pt = points[0]
            assert pt.get("key") == "test_asfp2_srv_1.point_1000", (
                f"point key should be preserved, got: {pt.get('key')}"
            )
            assert pt.get("measurement") == "wind_turbine", (
                f"measurement should be preserved, got: {pt.get('measurement')}"
            )
            assert pt.get("field") == "windspeed", (
                f"field should be preserved, got: {pt.get('field')}"
            )
            assert pt.get("type") == "float", (
                f"type should be preserved, got: {pt.get('type')}"
            )

        finally:
            _teardown_agent(handle)
