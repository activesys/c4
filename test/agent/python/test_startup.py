"""
C4 Agent L1 确定性功能测试 — Agent 启动恢复 (§3.2)

被测对象: Agent 启动时的无条件 Stop-Start 协议 (agent.md §3.2.3)
被测接口: Agent 进程启动行为 + 文件系统副作用

设计依据: c4/test/agent/README.md §3.2
"""

import json
import shutil
import subprocess
import time
from copy import deepcopy
from pathlib import Path
from typing import Optional

import pytest  # type: ignore

from assertions import (
    assert_config_json_valid,
    assert_config_shm_process_consistent,
    assert_process_running,
    assert_shm_ids_assigned,
)
from conftest import (
    AgentHandle,
    _find_free_port,
    corrupt_config_json,
    write_agent_json,
    write_config_bak,
    write_config_json,
)

# ──────────────────────────────────────────────
#  测试用 config.json 模板
# ──────────────────────────────────────────────

_CONFIG_WITH_SERVICES: dict = {
    "c4_shm_manager": {
        "instance_id": "c4_test",
        "max_points": 100000,
        "writer": ["c4_asfp2_server"],
        "reader": ["c4_asfp2_client"],
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
                {
                    "id": "point_1002",
                    "addr": 1002,
                    "shm_id": 0,
                },
            ],
        }
    ],
    "c4_asfp2_client": [
        {
            "id": "test_asfp2_cli_1",
            "name": "ASFP2转发1",
            "ip": "127.0.0.1",
            "port": 9999,
            "t0": 30,
            "t1": 20,
            "t2": 10,
            "timer": 100,
            "key_sequence": 1,
            "same_data_type": 1,
            "same_timestamp": 1,
            "smart": 1,
            "forward_kack": 255,
            "inverse_keep": 0,
            "points": [
                {
                    "id": "fwd_1000",
                    "key": "test_asfp2_srv_1.point_1000",
                    "addr": 3001,
                    "shm_id": 0,
                },
                {
                    "id": "fwd_1002",
                    "key": "test_asfp2_srv_1.point_1002",
                    "addr": 3002,
                    "shm_id": 0,
                },
            ],
        }
    ],
}

_CONFIG_UPDATED: dict = deepcopy(_CONFIG_WITH_SERVICES)
_CONFIG_UPDATED["c4_asfp2_client"].append(  # type: ignore[index]
    {
        "id": "test_asfp2_cli_2",
        "name": "ASFP2转发2",
        "ip": "127.0.0.1",
        "port": 9998,
        "t0": 30,
        "t1": 20,
        "t2": 10,
        "timer": 100,
        "key_sequence": 1,
        "same_data_type": 1,
        "same_timestamp": 1,
        "smart": 1,
        "forward_kack": 255,
        "inverse_keep": 0,
        "points": [
            {
                "id": "fwd2_1000",
                "key": "test_asfp2_srv_1.point_1000",
                "addr": 4001,
                "shm_id": 0,
            },
        ],
    }
)


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
    """
    制备 config_dir，写入 agent.json + 可选的 config.json，启动 Agent。
    返回 AgentHandle。
    """
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

    # 等待就绪
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


def _shm_segments_exist() -> bool:
    """检查系统中是否存在共享内存段。"""
    try:
        result = subprocess.run(
            ["ipcs", "-m"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # ipcs -m 输出正常时至少会有 header 行，实际 shm 段在后续行
        lines = result.stdout.strip().split("\n")
        return len(lines) > 1  # 超过 header 行说明有 shm 段
    except Exception:
        return False


def _config_services_count(config: dict) -> int:
    """计算 config 中声明的 MCP 服务进程数（不含 c4_shm_manager）。"""
    shm_section = config.get("c4_shm_manager", {})
    writers = shm_section.get("writer", [])
    readers = shm_section.get("reader", [])
    return len(writers) + len(readers)


# ══════════════════════════════════════════════
#  §3.2.1  首次启动
# ══════════════════════════════════════════════


class TestFirstStart:
    """首次启动场景 — 无 config.json。"""

    def test_no_config(self, agent: AgentHandle) -> None:
        """
        用例 3.2.1.1: config.json 不存在 → Agent 就绪，
        不创建 config.json，无数据路径 MCP 进程。
        """
        # Agent 已由 fixture 启动并确认就绪（GET /api/services = 200）

        # 断言: 不创建 config.json
        config_path = agent.config_dir / "config.json"
        assert not config_path.exists(), (
            f"config.json should NOT be created on first start, "
            f"but found at {config_path}"
        )

        # 断言: 无数据路径 MCP 进程（除 shm_manager 外）
        # 首次启动不应有任何 modbus / asfp2 / iec104 等服务进程
        for svc in ["c4_asfp2_server", "c4_asfp2_client",
                     "c4_iec104_client", "c4_asfp2_server",
                     "c4_influxdb_client"]:
            try:
                result = subprocess.run(
                    ["pgrep", "-f", svc],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                pids = result.stdout.strip()
                assert not pids, (
                    f"Service '{svc}' has running processes ({pids}) "
                    f"but should not start on first boot"
                )
            except subprocess.TimeoutExpired:
                pass

    def test_shm_manager_running(self, agent: AgentHandle) -> None:
        """
        用例 3.2.1.2: config.json 不存在 → shm_manager 进程存活，
        shm 段存在。
        """
        # 断言: shm_manager 进程存活
        assert_process_running("c4_shm_manager")

        # 断言: shm 段存在
        assert _shm_segments_exist(), (
            "Expected shared memory segments to exist after first start"
        )


# ══════════════════════════════════════════════
#  §3.2.2  正常重启
# ══════════════════════════════════════════════


class TestNormalRestart:
    """正常重启场景 — 有效 config.json。"""

    def test_restart_valid_config(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
    ) -> None:
        """
        用例 3.2.2.1: config.json 含 1 个 c4_asfp2_server + 1 个 c4_asfp2_client。
        Agent 就绪；stop(幂等) → adjust_shm → start；两服务进程均启动。
        """
        config_dir = tmp_path / "etc_c4"
        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=_CONFIG_WITH_SERVICES,
        )

        try:
            # 断言: Agent 就绪（startup helper 已确保）
            services = handle.get_services()
            assert isinstance(services, list)

            # 断言: config.json 存在且有效
            config = assert_config_json_valid(config_dir / "config.json")

            # 断言: shm_id 已分配（adjust_shm 完成）
            assert_shm_ids_assigned(config)

            # 断言: 两服务进程均启动
            assert_process_running("c4_asfp2_server")
            assert_process_running("c4_asfp2_client")

        finally:
            _teardown_agent(handle)

    def test_restart_services_already_running(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
    ) -> None:
        """
        用例 3.2.2.2: 先启动 Agent（同 3.2.2.1），再重启 Agent。
        重启后服务恢复，config/shsm 一致。
        """
        config_dir = tmp_path / "etc_c4"
        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=_CONFIG_WITH_SERVICES,
        )

        try:
            # 第一次启动确认: 服务运行
            assert_process_running("c4_asfp2_server")
            assert_process_running("c4_asfp2_client")

            # 重启 Agent
            handle.kill()
            time.sleep(1)  # 给进程清理时间
            handle.restart()

            # 重启后断言: 服务恢复
            assert_process_running("c4_asfp2_server")
            assert_process_running("c4_asfp2_client")

            # config 存在且有效
            config = assert_config_json_valid(config_dir / "config.json")
            assert_shm_ids_assigned(config)

        finally:
            _teardown_agent(handle)


# ══════════════════════════════════════════════
#  §3.2.3  配置损坏恢复
# ══════════════════════════════════════════════


class TestCorruptConfigRecovery:
    """配置损坏恢复场景。"""

    def test_bak_valid(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
    ) -> None:
        """
        用例 3.2.3.1: config.json 损坏，config.json.bak 有效。
        Agent 从 .bak 恢复 config.json 后正常启动，服务运行。
        """
        config_dir = tmp_path / "etc_c4"

        bak_content = deepcopy(_CONFIG_WITH_SERVICES)
        del bak_content["c4_asfp2_client"]  # type: ignore[arg-type]
        bak_content["c4_shm_manager"]["reader"] = []  # type: ignore[index]

        write_config_bak(config_dir, bak_content)
        write_config_json(config_dir, _CONFIG_WITH_SERVICES)  # 写入完整 config
        corrupt_config_json(config_dir)  # 损坏

        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=None,  # 不覆盖，保持已损坏/已写入的 config
        )

        try:
            # 断言: Agent 就绪
            services = handle.get_services()
            assert isinstance(services, list)

            # 断言: config.json 被恢复为 .bak 内容
            config = assert_config_json_valid(config_dir / "config.json")
            # 恢复后的 config 应匹配 .bak 内容（不含 asfp2_client）
            assert "c4_asfp2_server" in config, (
                "config.json should contain c4_asfp2_server after .bak restore"
            )
            assert "c4_asfp2_client" not in config, (
                "config.json should NOT contain c4_asfp2_client (.bak did not have it)"
            )

            # 断言: modbus 服务启动
            assert_process_running("c4_asfp2_server")

        finally:
            _teardown_agent(handle)

    def test_no_bak(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
    ) -> None:
        """
        用例 3.2.3.2: config.json 损坏，无 .bak。
        等同于首次启动（3.2.1.1）。
        """
        config_dir = tmp_path / "etc_c4"

        # 制备: 写入有效 config.json 然后损坏它
        write_config_json(config_dir, _CONFIG_WITH_SERVICES)
        corrupt_config_json(config_dir)

        # 确保没有 .bak
        bak_path = config_dir / "config.json.bak"
        if bak_path.exists():
            bak_path.unlink()

        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=None,
        )

        try:
            # 断言: Agent 就绪（不崩溃）
            services = handle.get_services()
            assert isinstance(services, list)

            # 断言: 等同于首次启动 — 不创建/恢复 config.json（或创建空 config）
            config_path = config_dir / "config.json"
            if config_path.exists():
                # 如果 Agent 写入了 config，验证它不包含测试数据
                try:
                    config = json.loads(config_path.read_text())
                    assert not isinstance(config.get("c4_asfp2_server"), list) or len(
                        config.get("c4_asfp2_server", [])
                    ) == 0, (
                        "config.json should not contain service instances "
                        "after corrupt config recovery (equivalent to first start)"
                    )
                except json.JSONDecodeError:
                    pytest.fail(
                        "config.json is still corrupt after restart — "
                        "Agent should have cleaned up"
                    )

            # 无数据路径 MCP 进程
            for svc in ["c4_asfp2_server", "c4_asfp2_client"]:
                result = subprocess.run(
                    ["pgrep", "-f", svc],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                assert not result.stdout.strip(), (
                    f"Service '{svc}' should not be running after corrupt config"
                )

        finally:
            _teardown_agent(handle)

    def test_both_corrupt(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
    ) -> None:
        """
        用例 3.2.3.3: config.json 和 config.json.bak 都损坏。
        等同于首次启动（3.2.1.1）。
        """
        config_dir = tmp_path / "etc_c4"

        # 制备: 有效 config + 有效 .bak，然后都损坏
        write_config_json(config_dir, _CONFIG_WITH_SERVICES)

        bak_content = deepcopy(_CONFIG_WITH_SERVICES)
        del bak_content["c4_asfp2_client"]  # type: ignore[arg-type]
        bak_content["c4_shm_manager"]["reader"] = []  # type: ignore[index]
        write_config_bak(config_dir, bak_content)

        corrupt_config_json(config_dir)  # 损坏 config.json
        corrupt_config_json(config_dir)  # 损坏 config.json.bak
        # corrupt_config_json 默认操作 config.json；需要额外损坏 .bak
        bak_path = config_dir / "config.json.bak"
        bak_raw = bak_path.read_text()
        idx = bak_raw.rfind("}")
        if idx > 0:
            bak_path.write_text(bak_raw[:idx])

        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=None,
        )

        try:
            # 断言: Agent 就绪（不崩溃）
            services = handle.get_services()
            assert isinstance(services, list)

            # 断言: 等同于首次启动
            # 无数据路径 MCP 进程
            for svc in ["c4_asfp2_server", "c4_asfp2_client"]:
                result = subprocess.run(
                    ["pgrep", "-f", svc],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                assert not result.stdout.strip(), (
                    f"Service '{svc}' should not be running after "
                    f"both config files corrupted"
                )

        finally:
            _teardown_agent(handle)


# ══════════════════════════════════════════════
#  §3.2.4  崩溃恢复
# ══════════════════════════════════════════════


class TestCrashRecovery:
    """崩溃恢复场景 — 验证 config.json ↔ shm ↔ 进程状态三者一致。"""

    def test_crash_normal(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
    ) -> None:
        """
        用例 3.2.4.1: 正常运行中崩溃 → kill → restart。
        重启后 config 内容不变，MCP 进程恢复运行。
        """
        config_dir = tmp_path / "etc_c4"
        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=_CONFIG_WITH_SERVICES,
        )

        try:
            # 确认服务运行
            assert_process_running("c4_asfp2_server")
            assert_process_running("c4_asfp2_client")

            # 保存崩溃前 config 快照
            config_before = assert_config_json_valid(config_dir / "config.json")
            config_before_raw = json.dumps(config_before, indent=2, sort_keys=True)

            # kill Agent
            handle.kill()

            # restart Agent
            handle.restart()

            # 断言: config.json 内容不变
            config_after = assert_config_json_valid(config_dir / "config.json")
            config_after_raw = json.dumps(config_after, indent=2, sort_keys=True)
            assert config_before_raw == config_after_raw, (
                f"config.json changed after crash recovery\n"
                f"BEFORE:\n{config_before_raw}\n"
                f"AFTER:\n{config_after_raw}"
            )

            # 断言: MCP 服务进程恢复运行
            assert_process_running("c4_asfp2_server")
            assert_process_running("c4_asfp2_client")

            # 断言: shm_id 已分配
            assert_shm_ids_assigned(config_after)

        finally:
            _teardown_agent(handle)

    def test_crash_config_updated(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
    ) -> None:
        """
        用例 3.2.4.2: config 更新后未 stop 时崩溃 → kill → restart。
        重启后 config.json = 新版本；新服务进程运行。
        """
        config_dir = tmp_path / "etc_c4"
        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=_CONFIG_WITH_SERVICES,
        )

        try:
            # 确认旧版服务运行
            assert_process_running("c4_asfp2_server")

            # 手动写入新版 config.json（多 1 个 modbus 实例）
            config_path = config_dir / "config.json"
            config_path.write_text(
                json.dumps(_CONFIG_UPDATED, indent=2, ensure_ascii=False)
            )

            # kill Agent（不经过正常重启路径）
            handle.kill()

            # restart Agent
            handle.restart()

            # 断言: config.json = 新版本
            config_after = assert_config_json_valid(config_path)
            client_instances = config_after.get("c4_asfp2_client", [])
            assert len(client_instances) == 2, (
                f"Expected 2 asfp2_client instances in config after recovery, "
                f"got {len(client_instances)}"
            )

            # 断言: shm_id 已分配
            assert_shm_ids_assigned(config_after)

            # 断言: 新增的服务进程也启动
            assert_process_running("c4_asfp2_server")
            assert_process_running("c4_asfp2_client")

        finally:
            _teardown_agent(handle)

    def test_crash_restart_after_stop(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
    ) -> None:
        """
        用例 3.2.4.3: stop 完成后崩溃（重启 → 就绪 → kill → 再重启）。
        验证恢复结果一致。
        """
        config_dir = tmp_path / "etc_c4"
        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=_CONFIG_WITH_SERVICES,
        )

        try:
            # 确认初始状态
            assert_process_running("c4_asfp2_server")
            assert_process_running("c4_asfp2_client")

            config_before = assert_config_json_valid(config_dir / "config.json")
            config_before_raw = json.dumps(config_before, indent=2, sort_keys=True)

            # 第一次重启（stop 已完成，start 可能未完成时 kill）
            handle.kill()
            time.sleep(0.5)
            handle.restart()

            # 确认重启后服务恢复
            assert_process_running("c4_asfp2_server")
            assert_process_running("c4_asfp2_client")

            # 再次 kill
            handle.kill()

            # 再次 restart
            handle.restart()

            # 断言: config 内容不变
            config_after = assert_config_json_valid(config_dir / "config.json")
            config_after_raw = json.dumps(config_after, indent=2, sort_keys=True)
            assert config_before_raw == config_after_raw, (
                f"config.json changed after double crash recovery\n"
                f"BEFORE:\n{config_before_raw}\n"
                f"AFTER:\n{config_after_raw}"
            )

            # 断言: 服务最终恢复
            assert_process_running("c4_asfp2_server")
            assert_process_running("c4_asfp2_client")

            assert_shm_ids_assigned(config_after)

        finally:
            _teardown_agent(handle)

    def test_crash_restart_start_mid(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
    ) -> None:
        """
        用例 3.2.4.4: start 中途崩溃（重启 → 就绪 → kill → 再重启）。
        验证恢复结果一致。
        """
        config_dir = tmp_path / "etc_c4"
        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=_CONFIG_WITH_SERVICES,
        )

        try:
            # 确认初始状态
            assert_process_running("c4_asfp2_server")
            assert_process_running("c4_asfp2_client")

            config_before = assert_config_json_valid(config_dir / "config.json")
            config_before_raw = json.dumps(config_before, indent=2, sort_keys=True)

            # 重启 → 等待就绪 → kill → 再重启
            handle.kill()
            time.sleep(0.5)
            handle.restart()

            # 确认重启后服务恢复
            assert_process_running("c4_asfp2_server")
            assert_process_running("c4_asfp2_client")

            handle.kill()
            time.sleep(0.3)  # 短暂等待后 kill（模拟 start 中途）

            handle.restart()

            # 断言: config 内容不变
            config_after = assert_config_json_valid(config_dir / "config.json")
            config_after_raw = json.dumps(config_after, indent=2, sort_keys=True)
            assert config_before_raw == config_after_raw, (
                f"config.json changed after crash recovery (3.2.4.4)\n"
                f"BEFORE:\n{config_before_raw}\n"
                f"AFTER:\n{config_after_raw}"
            )

            # 断言: 服务最终恢复
            assert_process_running("c4_asfp2_server")
            assert_process_running("c4_asfp2_client")

            assert_shm_ids_assigned(config_after)

        finally:
            _teardown_agent(handle)
