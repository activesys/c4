"""
C4 Agent L1 确定性功能测试 — Agent 启动恢复（四级瀑布）

被测对象: Agent 启动/恢复四级瀑布（agent.md §3.2.3；c4_architecture.md §3.1.2:
L0 config 健康 → L1 连接 → L2 收敛 → L3 监控接续）。
旧「无条件 Stop-Start」规则已随架构变更废止——无在途事务标记时收敛仅 start
（ALREADY_RUNNING 无动作、零中断），完整 Stop-Start（stop → adjust_shm → start）
仅在恢复已回滚事务的配置时执行。

被测接口: Agent 进程启动行为 + 文件系统副作用 + 端口监听 / shm write_seq 推进
（MCP 进程为常驻系统服务，进程存在与否不作为实例运行依据——生命周期双层模型）。

设计依据: c4/test/agent/README.md §3.2
"""

import json
import subprocess
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Optional

import pytest  # type: ignore

from assertions import assert_config_json_valid
from conftest import (
    AgentHandle,
    McpStackHandle,
    SocketMcpClient,
    _find_free_port,
    agent_command,
    clear_transaction_files,
    corrupt_config_json,
    _cleanup_instance_shm,
    port_is_listening,
    wait_port,
    write_agent_json,
    write_config_json,
    write_config_prev,
    write_pending_marker,
)

# ──────────────────────────────────────────────
#  测试用 config.json 模板（端口动态分配，支持端口监听探测）
# ──────────────────────────────────────────────


def _make_config(writer_port: int, fwd_port: int, extra_point: bool = False) -> dict:
    """asfp2_server(writer, 监听 writer_port) + asfp2_client(reader, 转发 → fwd_port)。"""
    points = [{"id": "point_1000", "addr": 1000, "shm_id": 0}]
    if extra_point:
        points.append({"id": "point_1002", "addr": 1002, "shm_id": 0})
    return {
        "c4_shm_manager": {
            "writer": ["c4_asfp2_server"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_asfp2_server": [
            {
                "id": "test_asfp2_srv_1",
                "name": "ASFP2接收服务1",
                "ip": "127.0.0.1",
                "port": writer_port,
                "points": points,
            }
        ],
        "c4_asfp2_client": [
            {
                "id": "test_asfp2_cli_1",
                "name": "ASFP2转发1",
                "ip": "127.0.0.1",
                "port": fwd_port,
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
                        "key": "test_asfp2_srv_1.point_1000",
                        "addr": 3001,
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

    process = subprocess.Popen(
        agent_command(agent_binary, config_dir),
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


def _instance_shm_exists(instance_id: str = "c4_test") -> bool:
    """实例 shm 段存在性（POSIX shm = /dev/shm/c4_<instance_id>）。"""
    return Path(f"/dev/shm/{instance_id}").exists()


def _normalized_config(path: Path) -> Optional[str]:
    """读取 config 并做 shm_id 无关的规范化快照（收敛的 shm 回填不参与比较）。"""
    try:
        cfg = assert_config_json_valid(path)

        def _strip(obj: object) -> object:
            if isinstance(obj, dict):
                return {k: _strip(v) for k, v in obj.items() if k != "shm_id"}
            if isinstance(obj, list):
                return [_strip(x) for x in obj]
            return obj

        return json.dumps(_strip(cfg), indent=2, sort_keys=True)
    except Exception:
        return None


def _wait_last_error(handle: AgentHandle, timeout: float = 30.0) -> dict:
    """轮询 GET /api/state 直至瀑布收敛完成（lastError 字段出现）。

    HTTP 服务先于瀑布监听（C4_RS_00241），就绪 ≠ 收敛完成——L0 报告在瀑布末尾写入，
    单次读取会与收敛竞争。返回最终 state（断言仍由用例自己做）。
    """
    deadline = time.time() + timeout
    state: dict = {}
    while time.time() < deadline:
        try:
            state = handle.get_state()
            payload = state.get("state", state)
            if payload.get("lastError"):
                return state
        except Exception:
            pass
        time.sleep(0.5)
    return state


def _wait_config_point_ids(
    config_path: Path, want_ids: list[str], timeout: float = 30.0
) -> dict:
    """轮询直至 config.json 的 c4_asfp2_server[0] 点表 id 列表等于 want_ids。

    HTTP 服务先于瀑布监听（C4_RS_00241），就绪 ≠ L0 恢复完成——单次读取会与
    L0 恢复竞争（config.json 尚为损坏/新版）。以「点表恢复为目标态」作为
    L0 恢复完成的可观测信号；返回最终解析结果（断言仍由用例自己做）。
    """
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        try:
            cfg = assert_config_json_valid(config_path)
            last = cfg
            pts = cfg.get("c4_asfp2_server") or [{}]
            if isinstance(pts, list) and pts:
                ids = [p.get("id") for p in (pts[0].get("points") or [])]
                if ids == want_ids:
                    return cfg
        except Exception:
            pass
        time.sleep(0.5)
    return last


def _stop_all_instances(stack: McpStackHandle) -> None:
    """停止全部数据路径实例并清理实例 shm 段（测试隔离——常驻 MCP 进程不重启，
    下一测试从零实例 + 无 shm 段状态开始）。"""
    for svc in ("c4_modbus_client", "c4_iec104_client", "c4_asfp2_client",
                "c4_asfp2_server", "c4_influxdb_client"):
        sock = Path(stack.sock_dir) / f"{svc}.sock"
        if not sock.exists():
            continue
        try:
            client = SocketMcpClient(svc, stack.sock_dir, timeout=5.0)
            client.call_tool_text("stop", {})
            client.close()
        except Exception:
            pass
    _cleanup_instance_shm("c4_test")


# ══════════════════════════════════════════════
#  §3.2.1  首次启动
# ══════════════════════════════════════════════


class TestFirstStart:
    """首次启动场景 — 无 config.json（期望状态零实例合法）。"""

    def test_no_config(
        self,
        agent: AgentHandle,
        mcp_stack: McpStackHandle,
    ) -> None:
        """
        用例 3.2.1.1: config.json 不存在 → Agent 就绪，不创建 config.json，
        数据服务零实例（期望状态零实例合法）；瀑布对账 c4_shm_manager：
        段不存在则 create_shm（幂等 create-or-attach）。
        """
        # Agent 已由 fixture 启动并确认就绪（GET /api/services = 200）

        # 断言: 不创建 config.json
        config_path = agent.config_dir / "config.json"
        assert not config_path.exists(), (
            f"config.json should NOT be created on first start, "
            f"but found at {config_path}"
        )

        # 断言: 无数据路径实例（测试栈内无配置即无端口可探；
        # MCP 进程为常驻系统服务，进程存在与否不作为断言面）
        # 事务文件不应存在
        assert not (agent.config_dir / "pending_change.json").exists()
        assert not (agent.config_dir / "config.json.prev.1").exists()

    def test_shm_manager_reconciliation(
        self,
        agent: AgentHandle,
        mcp_stack: McpStackHandle,
    ) -> None:
        """
        用例 3.2.1.2: config.json 不存在 → c4_shm_manager socket 可连，
        瀑布对账 shm：create_shm（幂等 create-or-attach，无配置 → 默认 100k 点），
        shm 段存在即视为正确。
        """
        # 断言: shm_manager socket 可连（Agent 已连接）
        client = SocketMcpClient("c4_shm_manager", mcp_stack.sock_dir)
        try:
            text, is_err = client.call_tool_text("query_status", {})
            assert not is_err, f"query_status failed: {text[:200]}"
        finally:
            client.close()

        # 断言: shm 段存在（create_shm 已执行；HTTP 就绪 ≠ 瀑布完成，轮询等待）
        deadline = time.time() + 30.0
        while time.time() < deadline and not _instance_shm_exists():
            time.sleep(0.5)
        assert _instance_shm_exists(), (
            "Expected instance shared memory segment after create_shm reconciliation"
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
        mcp_stack: McpStackHandle,
    ) -> None:
        """
        用例 3.2.2.1: config.json 含 1 个 c4_asfp2_server + 1 个 c4_asfp2_client，
        实例未运行。Agent 就绪；L2 收敛对全部服务 start（无 pending_change.json
        标记 → 不执行 Stop-Start）；两服务实例按配置拉起（start 返回 success）；
        MCP 进程常驻不退出。
        """
        writer_port, fwd_port = _find_free_port(), _find_free_port()
        config_dir = tmp_path / "etc_c4"
        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=_make_config(writer_port, fwd_port),
        )

        try:
            # 断言: Agent 就绪（startup helper 已确保）
            services = handle.get_services()
            assert isinstance(services, list)

            # 断言: 实例按配置拉起（端口监听探测——实例运行依据）
            wait_port(writer_port, True)
            # reader 无监听端口语义，以 config 的 shm 分配佐证 start 完成
            config = assert_config_json_valid(config_dir / "config.json")
            sid = config["c4_asfp2_server"][0]["points"][0]["shm_id"]
            assert sid != 0, "shm_id should be allocated (create_shm with config)"

            # 断言: 无事务文件（无标记 → 收敛仅 start，不执行 Stop-Start）
            assert not (config_dir / "pending_change.json").exists()
            assert not (config_dir / "config.json.prev.1").exists()
        finally:
            _teardown_agent(handle)
            _stop_all_instances(mcp_stack)

    def test_restart_services_already_running(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
        mcp_stack: McpStackHandle,
    ) -> None:
        """
        用例 3.2.2.2: 先启动 Agent，再重启 Agent。
        重启后对已在运行的服务 start 返回 ALREADY_RUNNING（一等成功路径结果，
        无动作）；数据路径零中断（故障矩阵：Agent 崩溃/重启不影响 MCP 实例）。
        """
        writer_port, fwd_port = _find_free_port(), _find_free_port()
        config_dir = tmp_path / "etc_c4"
        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=_make_config(writer_port, fwd_port),
        )

        try:
            # 第一次启动确认: 实例运行
            wait_port(writer_port, True)

            # 持续观测端口：Agent 重启全程不得出现监听中断（零中断断言）
            interrupt: list[str] = []
            watching = threading.Event()
            watching.set()

            def _watch() -> None:
                while watching.is_set():
                    if not port_is_listening(writer_port):
                        interrupt.append(f"port {writer_port} dropped")
                        return
                    time.sleep(0.2)

            watcher = threading.Thread(target=_watch, daemon=True)
            watcher.start()

            # 重启 Agent（MCP 常驻，实例不受影响）
            handle.kill()
            time.sleep(1)
            handle.restart()

            # 重启后断言: 实例仍在运行（ALREADY_RUNNING 无动作——端口未中断）
            wait_port(writer_port, True)
            time.sleep(0.5)
            watching.clear()
            watcher.join(timeout=2)
            assert not interrupt, (
                f"数据路径应零中断（ALREADY_RUNNING 无动作），观测到: {interrupt}"
            )

            # config 存在且有效，shm 分配保持
            config = assert_config_json_valid(config_dir / "config.json")
            sid = config["c4_asfp2_server"][0]["points"][0]["shm_id"]
            assert sid != 0

            # 无事务文件残留（重启不产生 .prev.1 / 标记）
            assert not (config_dir / "pending_change.json").exists()
            assert not (config_dir / "config.json.prev.1").exists()
        finally:
            _teardown_agent(handle)
            _stop_all_instances(mcp_stack)


# ══════════════════════════════════════════════
#  §3.2.3  配置损坏恢复（L0）
# ══════════════════════════════════════════════


class TestCorruptConfigRecovery:
    """配置损坏恢复场景（L0 config 健康）。"""

    def test_prev1_valid(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
        mcp_stack: McpStackHandle,
    ) -> None:
        """
        用例 3.2.3.1: config.json 损坏，config.json.prev.1 有效。
        L0：校验 .prev.1（parse + schema）通过 → 恢复为 config.json（获得权威地位）
        → 正常启动并按恢复后的配置收敛；向用户显式报告恢复情况（不得静默）。
        """
        writer_port, fwd_port = _find_free_port(), _find_free_port()
        config_dir = tmp_path / "etc_c4"
        config_dir.mkdir(parents=True, exist_ok=True)

        # .prev.1 = 有效配置（writer 单点 + 单转发）；config.json = 同结构但双点，
        # 随后被损坏——恢复后应与 .prev.1 内容一致（单点版本）
        prev_content = _make_config(writer_port, fwd_port)
        write_config_prev(config_dir, prev_content)

        newer_content = _make_config(writer_port, fwd_port, extra_point=True)
        write_config_json(config_dir, newer_content)
        corrupt_config_json(config_dir)

        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=None,  # 不覆盖，保持已损坏的 config
        )

        try:
            # 断言: Agent 就绪
            services = handle.get_services()
            assert isinstance(services, list)

            # 断言: config.json 被恢复为 .prev.1 内容（获得权威地位）
            # （轮询至 L0 恢复完成——HTTP 就绪 ≠ 收敛完成，与 §3.2.4 等待模式一致）
            config = _wait_config_point_ids(
                config_dir / "config.json", ["point_1000"]
            )
            pts = config["c4_asfp2_server"][0]["points"]
            assert [p["id"] for p in pts] == ["point_1000"], (
                f"config.json 应恢复为 .prev.1 内容（单点版本）: {[p['id'] for p in pts]}"
            )

            # 断言: 按恢复后的配置收敛（writer 实例拉起）
            wait_port(writer_port, True)

            # 断言: 向用户显式报告恢复情况（lastError 非空，不得静默）
            state = _wait_last_error(handle)
            state_payload = state.get("state", state)
            assert state_payload.get("lastError"), (
                f"L0 恢复必须显式报告，不得静默。State: {state_payload}"
            )
        finally:
            _teardown_agent(handle)
            _stop_all_instances(mcp_stack)

    def test_no_prev1(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
        mcp_stack: McpStackHandle,
    ) -> None:
        """
        用例 3.2.3.2: config.json 损坏，无 .prev.1。
        L0：.prev 缺失 → 不得覆盖、保留当前 config.json、删除 pending_change.json
        （防重入）、报告异常等待人工介入。
        """
        config_dir = tmp_path / "etc_c4"
        config_dir.mkdir(parents=True, exist_ok=True)

        write_config_json(config_dir, _make_config(1, 2))
        corrupt_config_json(config_dir)
        corrupted_raw = (config_dir / "config.json").read_text()

        # 确保没有 .prev.1
        prev_path = config_dir / "config.json.prev.1"
        if prev_path.exists():
            prev_path.unlink()

        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=None,
        )

        try:
            # 断言: Agent 就绪（不崩溃）
            services = handle.get_services()
            assert isinstance(services, list)

            # 断言: 保留当前 config.json（不得覆盖——损坏原文保留）
            assert (config_dir / "config.json").read_text() == corrupted_raw, (
                "config.json must be kept as-is when .prev.1 is missing (不得覆盖)"
            )

            # 断言: 报告异常等待人工介入（lastError 非空）
            state = _wait_last_error(handle)
            state_payload = state.get("state", state)
            assert state_payload.get("lastError"), (
                f"L0 异常必须报告并等待人工介入。State: {state_payload}"
            )
        finally:
            _teardown_agent(handle)
            _stop_all_instances(mcp_stack)

    def test_both_corrupt(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
        mcp_stack: McpStackHandle,
    ) -> None:
        """
        用例 3.2.3.3: config.json 和 config.json.prev.1 都损坏。
        L0：.prev 不可用 → 同 3.2.3.2：保留 config.json、报告异常等待人工介入。
        """
        config_dir = tmp_path / "etc_c4"
        config_dir.mkdir(parents=True, exist_ok=True)

        write_config_json(config_dir, _make_config(1, 2))
        corrupt_config_json(config_dir)
        corrupted_raw = (config_dir / "config.json").read_text()

        write_config_prev(config_dir, _make_config(3, 4))
        bak_path = config_dir / "config.json.prev.1"
        bak_raw = bak_path.read_text()
        idx = bak_raw.rfind("}")
        bak_path.write_text(bak_raw[:idx])  # 损坏 .prev.1

        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=None,
        )

        try:
            # 断言: Agent 就绪（不崩溃）
            services = handle.get_services()
            assert isinstance(services, list)

            # 断言: 保留当前 config.json
            assert (config_dir / "config.json").read_text() == corrupted_raw, (
                "config.json must be kept as-is when .prev.1 is unusable"
            )

            # 断言: 报告异常等待人工介入
            state = _wait_last_error(handle)
            state_payload = state.get("state", state)
            assert state_payload.get("lastError"), (
                f"L0 异常必须报告。State: {state_payload}"
            )
        finally:
            _teardown_agent(handle)
            _stop_all_instances(mcp_stack)

    def test_pending_marker_rollback(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
        mcp_stack: McpStackHandle,
    ) -> None:
        """
        用例 3.2.3.4: pending_change.json 存在（崩溃于变更事务中）+ .prev.1 有效。
        L0：发现标记 → 恢复 .prev.1 → 以恢复后的配置执行完整 Stop-Start
        （含 adjust_shm）→ 向用户报告「上次接入变更未完成，已回滚，接入不成功」
        → 继续瀑布（变更作废不续做，C4_RS_00066）。
        """
        writer_port, fwd_port = _find_free_port(), _find_free_port()
        config_dir = tmp_path / "etc_c4"
        config_dir.mkdir(parents=True, exist_ok=True)

        # 旧版（回滚目标）：仅 writer 单点
        old_config = _make_config(writer_port, fwd_port)
        write_config_prev(config_dir, old_config)

        # 新版（半截变更）：writer 追加第二点
        new_config = _make_config(writer_port, fwd_port, extra_point=True)
        write_config_json(config_dir, new_config)

        # 事务标记（涉及两个服务）
        write_pending_marker(
            config_dir, services=["c4_asfp2_server", "c4_asfp2_client"]
        )

        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=None,
        )

        try:
            # 断言: Agent 就绪
            services = handle.get_services()
            assert isinstance(services, list)

            # 断言: config.json = .prev.1（旧版生效，半截变更作废）
            # （轮询至 L0 恢复完成——HTTP 就绪 ≠ 收敛完成，与 §3.2.4 等待模式一致）
            config = _wait_config_point_ids(
                config_dir / "config.json", ["point_1000"]
            )
            pts = config["c4_asfp2_server"][0]["points"]
            assert [p["id"] for p in pts] == ["point_1000"], (
                f"config.json 应恢复为旧版（仅 point_1000），实际: {[p['id'] for p in pts]}"
            )

            # 断言: 事务标记已删除（不续做，防重入）
            assert not (config_dir / "pending_change.json").exists(), (
                "pending_change.json should be deleted after rollback"
            )

            # 断言: 报告「接入不成功」（显式告知，不得静默）
            state = _wait_last_error(handle)
            state_payload = state.get("state", state)
            last_error = state_payload.get("lastError") or ""
            assert "接入不成功" in last_error, (
                f"应报告「接入不成功」。State: {state_payload}"
            )

            # 断言: 以恢复后的配置执行完整 Stop-Start（实例拉起 + shm 分配一致）
            # （收敛后重读 config——adjust_shm 的 shm_id 回填晚于 L0 恢复）
            wait_port(writer_port, True)
            config_final = assert_config_json_valid(config_dir / "config.json")
            pts_final = config_final["c4_asfp2_server"][0]["points"]
            assert [p["id"] for p in pts_final] == ["point_1000"], (
                f"收敛后 config 应仍为恢复后的旧版: {[p['id'] for p in pts_final]}"
            )
            sid = pts_final[0]["shm_id"]
            assert sid != 0, "恢复后的配置应经 adjust_shm 重新分配 shm_id"
            client = SocketMcpClient("c4_shm_manager", mcp_stack.sock_dir)
            try:
                rp = client.read_points([sid])
                assert not rp["errors"], (
                    f"shm 分配应与恢复后的 config 一致: {rp}"
                )
            finally:
                client.close()
        finally:
            _teardown_agent(handle)
            _stop_all_instances(mcp_stack)


# ══════════════════════════════════════════════
#  §3.2.4  崩溃恢复
# ══════════════════════════════════════════════


class TestCrashRecovery:
    """崩溃恢复场景 — 验证 config.json ↔ shm ↔ 实例状态三者一致。

    事务边界原则：崩溃是否落在变更事务内由 pending_change.json 标记显式界定——
    无标记 → 收敛仅 start（ALREADY_RUNNING 无动作）；有标记 → 变更作废：
    恢复 .prev.1 → 完整 Stop-Start（含 adjust_shm）→ 报告「接入不成功」。
    """

    def test_crash_no_pending_marker(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
        mcp_stack: McpStackHandle,
    ) -> None:
        """
        用例 3.2.4.1: 正常运行中崩溃（无在途事务）→ kill → restart。
        config.json 内容不变；实例保持运行（Agent 崩溃期间数据路径不中断——
        MCP 常驻）；重启后 start 返回 ALREADY_RUNNING 无动作；shm 分配与 config 一致。
        """
        writer_port, fwd_port = _find_free_port(), _find_free_port()
        config_dir = tmp_path / "etc_c4"
        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=_make_config(writer_port, fwd_port),
        )

        try:
            wait_port(writer_port, True)
            config_before = assert_config_json_valid(config_dir / "config.json")
            before_raw = json.dumps(config_before, indent=2, sort_keys=True)
            sid_before = config_before["c4_asfp2_server"][0]["points"][0]["shm_id"]

            # kill Agent（模拟崩溃）——实例保持运行
            handle.kill()
            time.sleep(1)
            assert port_is_listening(writer_port), (
                "Agent 崩溃期间数据路径不得中断（MCP 常驻，C4_RS_00030/00031）"
            )

            # restart Agent
            handle.restart()

            # 断言: config.json 内容不变
            config_after = assert_config_json_valid(config_dir / "config.json")
            after_raw = json.dumps(config_after, indent=2, sort_keys=True)
            assert before_raw == after_raw, "config.json changed after crash recovery"

            # 断言: 实例保持运行（ALREADY_RUNNING 无动作）
            wait_port(writer_port, True)

            # 断言: shm 分配与 config 一致（read_points 无 SHM 错误）
            sid_after = config_after["c4_asfp2_server"][0]["points"][0]["shm_id"]
            assert sid_after == sid_before != 0
            client = SocketMcpClient("c4_shm_manager", mcp_stack.sock_dir)
            try:
                rp = client.read_points([sid_after])
                assert not rp["errors"], f"shm 分配应与 config 一致: {rp}"
            finally:
                client.close()
        finally:
            _teardown_agent(handle)
            _stop_all_instances(mcp_stack)

    def test_crash_after_rename_before_marker_delete(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
        mcp_stack: McpStackHandle,
    ) -> None:
        """
        用例 3.2.4.2: rename 后、删除标记前崩溃。
        构造变更中途态：pending_change.json 存在 + config.json = 新版本 +
        .prev.1 = 旧版本 → kill → restart。
        重启后 config.json = .prev.1（旧版生效，半截变更作废）；报告「接入不成功」；
        以恢复后的配置执行完整 Stop-Start（含 adjust_shm）；shm 分配与恢复后的
        config 一致。
        """
        writer_port, fwd_port = _find_free_port(), _find_free_port()
        config_dir = tmp_path / "etc_c4"
        # 新版（2 点）先正常启动，使实例按新版运行
        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=_make_config(writer_port, fwd_port, extra_point=True),
        )

        try:
            wait_port(writer_port, True)

            # kill 后构造变更中途态
            handle.kill()
            time.sleep(0.5)
            new_config = assert_config_json_valid(config_dir / "config.json")
            old_config = deepcopy(new_config)
            old_config["c4_asfp2_server"][0]["points"] = [
                p for p in old_config["c4_asfp2_server"][0]["points"]
                if p["id"] == "point_1000"
            ]
            write_config_prev(config_dir, old_config)
            write_pending_marker(
                config_dir, services=["c4_asfp2_server", "c4_asfp2_client"]
            )

            # restart Agent
            handle.restart()

            # 断言: config.json = .prev.1（旧版生效）
            config_after = assert_config_json_valid(config_dir / "config.json")
            pts = config_after["c4_asfp2_server"][0]["points"]
            assert [p["id"] for p in pts] == ["point_1000"], (
                f"半截变更应作废回滚，实际 points: {[p['id'] for p in pts]}"
            )

            # 断言: 标记已删除；报告「接入不成功」
            assert not (config_dir / "pending_change.json").exists()
            state = _wait_last_error(handle)
            state_payload = state.get("state", state)
            assert "接入不成功" in (state_payload.get("lastError") or ""), (
                f"应报告「接入不成功」。State: {state_payload}"
            )

            # 断言: 完整 Stop-Start 后实例与 shm 一致（恢复后的单点分配有效）
            wait_port(writer_port, True)
            sid = pts[0]["shm_id"]
            assert sid != 0
            client = SocketMcpClient("c4_shm_manager", mcp_stack.sock_dir)
            try:
                rp = client.read_points([sid])
                assert not rp["errors"], f"shm 分配应与恢复后 config 一致: {rp}"
            finally:
                client.close()
        finally:
            _teardown_agent(handle)
            _stop_all_instances(mcp_stack)

    def test_crash_after_marker_before_rename(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
        mcp_stack: McpStackHandle,
    ) -> None:
        """
        用例 3.2.4.3: 写标记后、rename 前崩溃。
        构造变更中途态：pending_change.json 存在 + config.json 仍为旧版 +
        .prev.1 = 旧版 → kill → restart。
        重启后恢复 .prev.1（与当前一致）→ 完整 Stop-Start（含 adjust_shm）→
        报告「接入不成功」→ 收敛完成。
        """
        writer_port, fwd_port = _find_free_port(), _find_free_port()
        config_dir = tmp_path / "etc_c4"
        old_config = _make_config(writer_port, fwd_port)
        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=old_config,
        )

        try:
            wait_port(writer_port, True)
            # 首次收敛快照（shm_id 无关——收敛回填不参与内容比较）
            before_norm = _normalized_config(config_dir / "config.json")
            assert before_norm is not None

            handle.kill()
            time.sleep(0.5)

            # config.json 仍为旧版 + .prev.1 = 旧版 + 标记
            write_config_prev(config_dir, old_config)
            write_pending_marker(
                config_dir, services=["c4_asfp2_server", "c4_asfp2_client"]
            )

            handle.restart()

            # 断言: config.json = .prev.1（与变更前旧版一致；轮询至瀑布收敛——
            # HTTP 就绪 ≠ 收敛完成，adjust_shm 的 shm 回填完成后内容才稳定）
            deadline = time.time() + 30
            after_norm: Optional[str] = None
            while time.time() < deadline:
                after_norm = _normalized_config(config_dir / "config.json")
                if after_norm == before_norm:
                    break
                time.sleep(0.5)
            assert after_norm == before_norm, (
                "config.json 应恢复为 .prev.1（旧版一致）"
            )

            # 断言: 标记删除 + 报告「接入不成功」+ 收敛完成（实例拉起）
            assert not (config_dir / "pending_change.json").exists()
            state = _wait_last_error(handle)
            state_payload = state.get("state", state)
            assert "接入不成功" in (state_payload.get("lastError") or "")
            wait_port(writer_port, True)
            sid = assert_config_json_valid(
                config_dir / "config.json"
            )["c4_asfp2_server"][0]["points"][0]["shm_id"]
            assert sid != 0, "完整 Stop-Start 后 shm 分配应与恢复后的 config 一致"
        finally:
            _teardown_agent(handle)
            _stop_all_instances(mcp_stack)

    def test_crash_mid_convergence_no_marker(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
        mcp_stack: McpStackHandle,
    ) -> None:
        """
        用例 3.2.4.4: 无标记、收敛中途崩溃。
        同 3.2.4.1——无在途事务标记即保证 config 与运行状态一致，start 幂等
        （ALREADY_RUNNING 无动作）。
        """
        writer_port, fwd_port = _find_free_port(), _find_free_port()
        config_dir = tmp_path / "etc_c4"
        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=_make_config(writer_port, fwd_port),
        )

        try:
            wait_port(writer_port, True)
            before_raw = json.dumps(
                assert_config_json_valid(config_dir / "config.json"),
                indent=2, sort_keys=True,
            )

            # 收敛后短暂等待即 kill（无 pending_change.json）
            handle.kill()
            time.sleep(0.3)
            handle.restart()

            # 断言: config 内容不变；实例最终恢复
            after_raw = json.dumps(
                assert_config_json_valid(config_dir / "config.json"),
                indent=2, sort_keys=True,
            )
            assert before_raw == after_raw
            wait_port(writer_port, True)
        finally:
            _teardown_agent(handle)
            _stop_all_instances(mcp_stack)


# ══════════════════════════════════════════════
#  §3.2 补充 — 降级 / 重连收敛（L1 优雅降级，C4_RS_00242）
# ══════════════════════════════════════════════


class TestDegradedAndReconnect:
    """服务 socket 暂不可连 → 降级不阻塞其余服务 → 服务恢复后重连收敛。"""

    def test_degraded_then_reconnect(
        self,
        tmp_path: Path,
        agent_binary: str,
        shm_manager_binary: str,
        registry_dir: Path,
        mcp_stack: McpStackHandle,
    ) -> None:
        """
        场景: c4_asfp2_client socket 不可连（服务重启中）→ Agent 就绪不受阻塞，
        其余服务照常收敛；服务重新上线后 Agent 重连并按 config 收敛（start）。
        """
        writer_port, fwd_port = _find_free_port(), _find_free_port()
        config_dir = tmp_path / "etc_c4"
        handle = _start_agent_with_config(
            config_dir, registry_dir, shm_manager_binary, agent_binary,
            config_content=_make_config(writer_port, fwd_port),
        )

        try:
            # 停掉 reader 服务（模拟服务重启中——socket 消失）
            mcp_stack.stop_service("c4_asfp2_client")
            time.sleep(0.5)

            # kill + restart Agent（reader 缺席）
            handle.kill()
            time.sleep(0.5)
            handle.restart()

            # 断言: Agent 就绪不受阻塞；writer 照常收敛（互相不阻塞）
            services = handle.get_services()
            assert isinstance(services, list)
            wait_port(writer_port, True)

            # 断言: 降级标记可见（存活状态＝连接状态推导，/api/services alive 字段）
            def _alive_of(svc: str) -> Optional[bool]:
                for entry in handle.get_services():
                    if entry.get("service_type") == svc:
                        return entry.get("alive")
                return None

            assert _alive_of("c4_asfp2_client") is False, (
                "c4_asfp2_client 不可连时应标记为未存活（降级）"
            )

            # 服务重新上线 → Agent 退避重连 → 按 config 收敛
            mcp_stack.start_service("c4_asfp2_client")
            assert mcp_stack.wait_socket("c4_asfp2_client", timeout=10)

            deadline = time.time() + 30
            alive = False
            while time.time() < deadline:
                if _alive_of("c4_asfp2_client") is True:
                    alive = True
                    break
                time.sleep(0.5)
            assert alive, "服务重新上线后 Agent 应重连成功（alive=true）"
        finally:
            _teardown_agent(handle)
            _stop_all_instances(mcp_stack)
            # 恢复栈完整性（后续测试依赖全部服务在线）
            mcp_stack.start_service("c4_asfp2_client")
            mcp_stack.wait_socket("c4_asfp2_client", timeout=10)
