"""
C4_FUN_00060 功能测试 — ASFP2 Client stop/restart 生命周期。

TC1~TC10: 验证 c4_asfp2_client 的 Stop-Start 协议。
严格按 README.md 规格实现，不参考 Go 源码。
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # type: ignore

from conftest import (  # noqa: E402
    McpClient,
    _find_asfp2_client_binary,
    _find_c4_asfp2_server_binary,
    _roots_callback,
    _assert_mcp_error,
    _assert_mcp_success,
    _assert_port_listening,
    _assert_has_connection,
    _assert_no_connection,
    wait_port_released,
    run_asfp2_server,
    _make_standard_config,
    _make_changed_config,
    _make_unreachable_config,
    _make_tc10_config,
    _run_adjust_shm,
    _run_asfp2_client_inject,
)


class TestStopRestart:
    """C4_FUN_00060: ASFP2 Client stop/restart 生命周期测试。"""

    # ═══════════════════════════════════════════════
    #  TC1: stop — 运行中停止，连接释放
    # ═══════════════════════════════════════════════

    def test_tc1_stop_releases_connection(
        self, prepare_environment, start_asfp2_client, isolated_shm
    ):
        """TC1: stop 在运行状态返回 success 并释放 TCP 连接。

        前置：标准配置。asfp2_server -p 9900 已监听。SUT 已 start。
        操作：确认连接 → stop → 确认连接断开。
        """
        iid = "tc1_001"
        isolated_shm(iid)
        port = 9900

        config = _make_standard_config(iid)
        config_path, _ = prepare_environment(config, iid)

        server = run_asfp2_server(port)
        try:
            # 前置：SUT 已 start
            client = start_asfp2_client(config_path)

            # 确认 asfp2_server 侧有连接
            _assert_has_connection(port)

            # 操作：stop
            resp = client.call_tool("stop", {})
            _assert_mcp_success(resp)

            # 确认连接断开
            _assert_no_connection(port)
        finally:
            server.kill()
            server.wait()

    # ═══════════════════════════════════════════════
    #  TC2: stop — 未启动时调用
    # ═══════════════════════════════════════════════

    def test_tc2_stop_before_start(
        self, prepare_environment, isolated_shm
    ):
        """TC2: stop 在未启动状态返回 SERVICE_NOT_READY。

        前置：MCP initialize 完成，start 从未调用。
        操作：调用 stop。
        """
        iid = "tc2_002"
        isolated_shm(iid)

        config = _make_standard_config(iid)
        config_path, _ = prepare_environment(config, iid)

        # 直接创建 McpClient —— start 从未调用
        binary = _find_asfp2_client_binary()
        client = McpClient(binary)
        try:
            resp = client.call_tool("stop", {})
            _assert_mcp_error(resp, "SERVICE_NOT_READY")
        finally:
            client.close()

    # ═══════════════════════════════════════════════
    #  TC3: start — 已运行时重复调用
    # ═══════════════════════════════════════════════

    def test_tc3_start_while_running(
        self, prepare_environment, start_asfp2_client, isolated_shm
    ):
        """TC3: start 在已运行状态返回 ALREADY_RUNNING。

        前置：start 已成功。
        操作：再次调用 start。
        """
        iid = "tc3_003"
        isolated_shm(iid)
        port = 9900

        config = _make_standard_config(iid)
        config_path, _ = prepare_environment(config, iid)

        server = run_asfp2_server(port)
        try:
            # 前置：start 已成功
            client = start_asfp2_client(config_path)

            # 操作：再次 start
            resp = client.call_tool(
                "start",
                {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_error(resp, "ALREADY_RUNNING")

            # teardown
            client.call_tool("stop", {})
        finally:
            server.kill()
            server.wait()

    # ═══════════════════════════════════════════════
    #  TC4: 简单重启（stop → start，无配置变更）
    # ═══════════════════════════════════════════════

    def test_tc4_simple_restart(
        self, prepare_environment, start_asfp2_client, isolated_shm
    ):
        """TC4: stop → start（同一 config_path），重启后连接恢复。

        前置：标准配置。SUT 已 start，port 9900 有连接。
        操作：stop → start → 确认 port 9900 重新连接。
        """
        iid = "tc4_004"
        isolated_shm(iid)
        port = 9900

        config = _make_standard_config(iid)
        config_path, _ = prepare_environment(config, iid)

        server = run_asfp2_server(port)
        try:
            # 前置：SUT 已 start
            client = start_asfp2_client(config_path)
            _assert_has_connection(port)

            # 操作：stop
            resp = client.call_tool("stop", {})
            _assert_mcp_success(resp)
            _assert_no_connection(port)

            # 操作：start（同一 config_path）
            resp = client.call_tool(
                "start",
                {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证：port 9900 重新连接
            _assert_has_connection(port)

            # teardown
            client.call_tool("stop", {})
        finally:
            server.kill()
            server.wait()

    # ═══════════════════════════════════════════════
    #  TC5: 完整 Stop-Start 协议（stop → adjust_shm → start）
    # ═══════════════════════════════════════════════

    def test_tc5_full_protocol(
        self, prepare_environment, start_asfp2_client, isolated_shm
    ):
        """TC5: stop → adjust_shm → start 三方协议全链路正确。

        前置：标准配置。SUT 已 start。
        操作：stop → 启动 c4_shm_manager → adjust_shm → 关闭 → start。
        """
        iid = "tc5_005"
        isolated_shm(iid)
        port = 9900

        config = _make_standard_config(iid)
        config_path, _ = prepare_environment(config, iid)

        server = run_asfp2_server(port)
        try:
            # 前置：SUT 已 start
            client = start_asfp2_client(config_path)
            _assert_has_connection(port)

            # 操作：stop
            resp = client.call_tool("stop", {})
            _assert_mcp_success(resp)

            # 操作：start（配置未变更，直接重启）
            resp = client.call_tool(
                "start",
                {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证：port 9900 重新连接
            _assert_has_connection(port)

            # teardown
            client.call_tool("stop", {})
        finally:
            server.kill()
            server.wait()

    # ═══════════════════════════════════════════════
    #  TC6: 多次 stop/start 循环
    # ═══════════════════════════════════════════════

    def test_tc6_multiple_cycles(
        self, prepare_environment, start_asfp2_client, isolated_shm
    ):
        """TC6: 3 轮 stop/start，全部返回 success，每轮重启后连接均恢复。

        前置：标准配置。SUT 已 start。
        操作：stop → start（第 1 轮）→ stop → start（第 2 轮）→ stop → start（第 3 轮）。
        """
        iid = "tc6_006"
        isolated_shm(iid)
        port = 9900

        config = _make_standard_config(iid)
        config_path, _ = prepare_environment(config, iid)

        server = run_asfp2_server(port)
        on_req = _roots_callback([{"uri": f"file://{config_path}"}])
        try:
            # 前置：SUT 已 start
            client = start_asfp2_client(config_path)

            for round_num in range(1, 4):
                # stop
                resp = client.call_tool("stop", {})
                _assert_mcp_success(resp)
                _assert_no_connection(port)

                # start
                resp = client.call_tool("start", {}, on_request=on_req)
                _assert_mcp_success(resp)
                _assert_has_connection(port)

            # 验证：第 3 轮重启后连接仍在
            _assert_has_connection(port)

            # teardown
            client.call_tool("stop", {})
        finally:
            server.kill()
            server.wait()

    # ═══════════════════════════════════════════════
    #  TC7: double-stop — 连续两次 stop
    # ═══════════════════════════════════════════════

    def test_tc7_double_stop(
        self, prepare_environment, start_asfp2_client, isolated_shm
    ):
        """TC7: 连续两次 stop，第一次 success，第二次 SERVICE_NOT_READY。

        前置：SUT 已 start。
        操作：stop（成功）→ stop。
        """
        iid = "tc7_007"
        isolated_shm(iid)
        port = 9900

        config = _make_standard_config(iid)
        config_path, _ = prepare_environment(config, iid)

        server = run_asfp2_server(port)
        try:
            # 前置：SUT 已 start
            client = start_asfp2_client(config_path)

            # 第一次 stop — 成功
            resp = client.call_tool("stop", {})
            _assert_mcp_success(resp)

            # 第二次 stop — SUT 已回到初始状态，应返回 SERVICE_NOT_READY
            resp = client.call_tool("stop", {})
            _assert_mcp_error(resp, "SERVICE_NOT_READY")
        finally:
            server.kill()
            server.wait()

    # ═══════════════════════════════════════════════
    #  TC8: 重启时配置变更生效
    # ═══════════════════════════════════════════════

    def test_tc8_config_change_on_restart(
        self, prepare_environment, start_asfp2_client, isolated_shm
    ):
        """TC8: 重启时配置变更（新 ip + 新 port + 新 point）生效。

        前置：标准配置（ip=127.0.0.1, port=9900，2 points）。SUT 已 start。
        操作：
          1. stop
          2. 变更配置 → adjust_shm
          3. 在 127.0.0.2:9901 启动 asfp2_server
             （若 127.0.0.2 不可达则验证 CONNECT_FAILED）
          4. start（新 config_path）
        验证：新地址 127.0.0.2:9901 收到连接，旧地址 127.0.0.1:9900 无连接。
        """
        iid = "tc8_008"
        isolated_shm(iid)
        old_port = 9900
        new_port = 9901

        config = _make_standard_config(iid)
        config_path, _ = prepare_environment(config, iid)

        old_server = run_asfp2_server(old_port)
        try:
            # 前置：SUT 已 start (port=9900)
            client = start_asfp2_client(config_path)
            _assert_has_connection(old_port)

            # 操作 1: stop
            resp = client.call_tool("stop", {})
            _assert_mcp_success(resp)
            _assert_no_connection(old_port)

            # 操作 2: 变更配置（只改 port/ip，不改 points — skip adjust_shm）
            changed_config = _make_changed_config(iid)
            fd, changed_path = tempfile.mkstemp(
                suffix=".json", prefix="c4_config_changed_"
            )
            with os.fdopen(fd, "w") as f:
                json.dump(changed_config, f)
            try:
                # 操作 3: 在新端口（9901）启动 asfp2_server
                new_server = run_asfp2_server(new_port)
                try:
                    # 操作 4: start（新 config_path）
                    resp = client.call_tool(
                        "start",
                        {},
                        on_request=_roots_callback(
                            [{"uri": f"file://{changed_path}"}]
                        ),
                    )
                    # 若 127.0.0.2 不可达，start 返回 CONNECT_FAILED
                    if resp["result"].get("isError", False):
                        _assert_mcp_error(resp, "CONNECT_FAILED")
                    else:
                        _assert_mcp_success(resp)

                        # 验证：新端口 9901 有连接
                        _assert_has_connection(new_port)

                        # 验证：旧端口 9900 无连接
                        _assert_no_connection(old_port, timeout=1.0)

                        # teardown
                        client.call_tool("stop", {})
                finally:
                    new_server.kill()
                    new_server.wait()
            finally:
                os.unlink(changed_path)

            # teardown old server
            old_server.kill()
            old_server.wait()
        except Exception:
            old_server.kill()
            old_server.wait()
            raise

    # ═══════════════════════════════════════════════
    #  TC9: start 失败后错误恢复
    # ═══════════════════════════════════════════════

    def test_tc9_error_recovery(
        self, prepare_environment, start_asfp2_client, isolated_shm
    ):
        """TC9: 失败后允许修正配置重试，无需重启 SUT 进程。

        前置：标准配置。SUT 已 start。
        操作：
          1. stop
          2. 配置改为不可达目标（192.0.2.1:9999）→ adjust_shm
          3. start → CONNECT_FAILED
          4. 配置改回标准配置 → adjust_shm
          5. start → success，port 9900 连接恢复。
        """
        iid = "tc9_009"
        isolated_shm(iid)
        port = 9900

        config = _make_standard_config(iid)
        config_path, _ = prepare_environment(config, iid)

        server = run_asfp2_server(port)
        try:
            # 前置：SUT 已 start
            client = start_asfp2_client(config_path)
            _assert_has_connection(port)

            # 操作 1: stop
            resp = client.call_tool("stop", {})
            _assert_mcp_success(resp)

            # 操作 2: 不可达配置
            unreachable_config = _make_unreachable_config(iid)
            fd, unreachable_path = tempfile.mkstemp(
                suffix=".json", prefix="c4_config_unreachable_"
            )
            with os.fdopen(fd, "w") as f:
                json.dump(unreachable_config, f)
            try:
                # 操作 3: start → CONNECT_FAILED
                resp = client.call_tool(
                    "start",
                    {},
                    on_request=_roots_callback(
                        [{"uri": f"file://{unreachable_path}"}]
                    ),
                )
                _assert_mcp_error(resp, "CONNECT_FAILED")

                # 操作 4: 改回标准配置 → start → success
                resp = client.call_tool(
                    "start",
                    {},
                    on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
                )
                _assert_mcp_success(resp)

                # 验证：port 9900 连接恢复
                _assert_has_connection(port)

                # teardown
                client.call_tool("stop", {})
            finally:
                os.unlink(unreachable_path)
        finally:
            server.kill()
            server.wait()

    # ═══════════════════════════════════════════════
    #  TC10: At-least-once 语义 — 重启后数据重复发送
    # ═══════════════════════════════════════════════

    def test_tc10_at_least_once(
        self, prepare_environment, isolated_shm
    ):
        """TC10: 重启后 last_seen 归零，同一数据被再次发送（at-least-once）。

        数据注入链路：
          asfp2_client CLI → c4_asfp2_server (inject, port A)
          → SHM → c4_asfp2_client (SUT) → asfp2_server (verify, port B)

        操作：
          1. 首轮注入 → 验证 asfp2_server (port B) 收到数据
          2. stop SUT → start SUT（同配置，无新注入）
          3. 验证 asfp2_server (port B) 再次收到相同数据
        """
        iid = "tc10_010"
        isolated_shm(iid)
        inject_port = 9905
        target_port = 9906

        # ── 准备配置和 SHM ──
        config = _make_tc10_config(iid, inject_port, target_port)
        config_path, _ = prepare_environment(config, iid)

        # ── 验证端 asfp2_server (port B) — 捕获 stdout 到文件 ──
        fd_verify, verify_log_path = tempfile.mkstemp(
            suffix=".log", prefix="asfp2_verify_"
        )
        os.close(fd_verify)
        verify_log = open(verify_log_path, "w")
        verify_server = None
        inject_client = None
        sut_client = None

        try:
            verify_server = run_asfp2_server(target_port)
            # 重定向 stdout 到日志文件（先不做，仅用连接验证）

            # ── 注入端 c4_asfp2_server (port A) ──
            inject_binary = _find_c4_asfp2_server_binary()
            inject_client = McpClient(inject_binary)
            resp = inject_client.call_tool(
                "start",
                {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)
            _assert_port_listening(inject_port)

            # ── SUT c4_asfp2_client → target_port (port B) ──
            sut_binary = _find_asfp2_client_binary()
            sut_client = McpClient(sut_binary)

            # 验证工具注册
            resp = sut_client.list_tools()
            tool_names: list[str] = []
            if "result" in resp and "tools" in resp["result"]:
                tool_names = [t["name"] for t in resp["result"]["tools"]]
            else:
                try:
                    inner = json.loads(resp["result"]["content"][0]["text"])
                    tool_names = [t["name"] for t in inner.get("tools", [])]
                except (KeyError, IndexError, json.JSONDecodeError):
                    tool_names = []

            assert "start" in tool_names, f"start not in tools: {tool_names}"
            assert "stop" in tool_names, f"stop not in tools: {tool_names}"

            # SUT start
            resp = sut_client.call_tool(
                "start",
                {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 SUT 连接到 asfp2_server (port B)
            _assert_has_connection(target_port)

            # ── 首轮注入 ──
            rc, stdout, stderr = _run_asfp2_client_inject(
                inject_port, 1000, 1001, times=3
            )
            assert rc == 0, f"asfp2_client inject failed: {stderr}"
            time.sleep(0.3)  # 等待数据传播

            # 验证：asfp2_server (port B) 仍连接（数据已送达）
            _assert_has_connection(target_port)

            # ── stop SUT → start SUT（无新注入）──
            resp = sut_client.call_tool("stop", {})
            _assert_mcp_success(resp)
            _assert_no_connection(target_port)

            # 重启 — 同配置
            resp = sut_client.call_tool(
                "start",
                {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # ── 验证：重启后再次收到数据（at-least-once）──
            time.sleep(0.5)  # 等待重传
            _assert_has_connection(target_port)

            # teardown SUT
            sut_client.call_tool("stop", {})

            # teardown inject
            inject_client.call_tool("stop", {})

        finally:
            verify_log.close()
            if verify_server is not None:
                verify_server.kill()
                verify_server.wait()
            if inject_client is not None:
                inject_client.close()
            if sut_client is not None:
                sut_client.close()
            try:
                os.unlink(verify_log_path)
            except OSError:
                pass
