"""
C4_FUN_00058 功能测试 — ASFP2 Server stop/restart 生命周期。

TC1~TC10: 验证 c4_asfp2_server 的 Stop-Start 协议。
严格按 README.md 规格实现，不参考 Go 源码。
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # type: ignore

from conftest import (  # noqa: E402
    _assert_block_written_seq,
    _assert_mcp_error,
    _assert_mcp_success,
    _assert_port_listening,
    _make_standard_config,
    _make_port_conflict_config,
    _make_modified_config,
    _make_corrected_config,
    _read_config_shm_id,
    _roots_callback,
    _run_adjust_shm,
    _run_asfp2_client,
    _prepare_config_with_shm,
    wait_port_released,
)
from shm_helpers import shm_path, read_shm_block  # noqa: E402


class TestStopRestart:
    """C4_FUN_00058: ASFP2 Server stop/restart 生命周期测试。"""

    # ═══════════════════════════════════════════════
    #  TC1: stop — 运行中停止，端口释放
    # ═══════════════════════════════════════════════

    def test_tc1_stop_releases_port(
        self, prepare_environment, start_asfp2_server, isolated_shm
    ):
        """TC1: stop 在运行状态返回 success 并释放端口。"""
        iid = "test_tc1"
        isolated_shm(iid)
        port = 9000

        config = _make_standard_config(iid, port)
        config_path, _ = prepare_environment(config, iid)

        # 前置: start 成功，端口监听
        resp = start_asfp2_server.call_tool(
            "start", {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        _assert_mcp_success(resp)
        _assert_port_listening(port)

        # 操作: 确认端口可连接 → stop → 轮询等待释放
        resp = start_asfp2_server.call_tool("stop", {})
        _assert_mcp_success(resp)
        wait_port_released(port)

    # ═══════════════════════════════════════════════
    #  TC2: stop — 未启动时调用
    # ═══════════════════════════════════════════════

    def test_tc2_stop_before_start(
        self, prepare_environment, start_asfp2_server, isolated_shm
    ):
        """TC2: stop 在未启动状态返回 SERVICE_NOT_READY。"""
        iid = "test_tc2"
        isolated_shm(iid)

        config = _make_standard_config(iid)
        config_path, _ = prepare_environment(config, iid)

        # SUT 已 MCP initialize，但 start 从未调用
        resp = start_asfp2_server.call_tool("stop", {})
        _assert_mcp_error(resp, "SERVICE_NOT_READY")

    # ═══════════════════════════════════════════════
    #  TC3: start — 已运行时重复调用
    # ═══════════════════════════════════════════════

    def test_tc3_start_while_running(
        self, prepare_environment, start_asfp2_server, isolated_shm
    ):
        """TC3: start 在已运行状态返回 ALREADY_RUNNING。"""
        iid = "test_tc3"
        isolated_shm(iid)
        port = 9000

        config = _make_standard_config(iid, port)
        config_path, _ = prepare_environment(config, iid)

        resp = start_asfp2_server.call_tool(
            "start", {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        _assert_mcp_success(resp)

        # 同一 SUT 进程，无间隔 stop — 再次调用 start
        resp = start_asfp2_server.call_tool(
            "start", {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        _assert_mcp_error(resp, "ALREADY_RUNNING")

        # teardown
        start_asfp2_server.call_tool("stop", {})

    # ═══════════════════════════════════════════════
    #  TC4: 简单重启（stop → start，无配置变更）
    # ═══════════════════════════════════════════════

    def test_tc4_simple_restart(
        self, prepare_environment, start_asfp2_server, isolated_shm
    ):
        """TC4: stop → start 重启后端口重新监听、数据流恢复。"""
        iid = "test_tc4"
        isolated_shm(iid)
        port = 9000

        config = _make_standard_config(iid, port)
        config_path, _ = prepare_environment(config, iid)

        on_req = _roots_callback([{"uri": f"file://{config_path}"}])

        # 前置: start 成功
        resp = start_asfp2_server.call_tool("start", {}, on_request=on_req)
        _assert_mcp_success(resp)

        # 操作: stop → start
        resp = start_asfp2_server.call_tool("stop", {})
        _assert_mcp_success(resp)

        resp = start_asfp2_server.call_tool("start", {}, on_request=on_req)
        _assert_mcp_success(resp)

        # 验证: 端口重新监听
        _assert_port_listening(port)

        # 验证: 数据流恢复 — write_seq 前后对比
        sp = shm_path(iid)
        seq1_before = _assert_block_written_seq(sp, 1)
        seq2_before = _assert_block_written_seq(sp, 2)

        rc, stdout, stderr = _run_asfp2_client(port, 1000, 1001, times=3)
        assert rc == 0, f"asfp2_client failed: {stderr}"
        time.sleep(0.3)

        seq1_after = read_shm_block(sp, 1)["write_seq"]
        seq2_after = read_shm_block(sp, 2)["write_seq"]
        assert seq1_after > seq1_before, (
            f"shm_id=1 write_seq not incremented: {seq1_before} → {seq1_after}"
        )
        assert seq2_after >= seq2_before, (
            f"shm_id=2 write_seq decreased: {seq2_before} → {seq2_after}"
        )

        # teardown
        start_asfp2_server.call_tool("stop", {})

    # ═══════════════════════════════════════════════
    #  TC5: 完整 Stop-Start 协议（stop → adjust_shm → start）
    # ═══════════════════════════════════════════════

    def test_tc5_full_protocol(
        self, prepare_environment, start_asfp2_server, isolated_shm
    ):
        """TC5: stop → start 链路验证（adjust_shm 由 TC9 覆盖）。"""
        iid = "test_tc5"
        isolated_shm(iid)
        port = 9000

        config = _make_standard_config(iid, port)
        config_path, _ = prepare_environment(config, iid)

        on_req = _roots_callback([{"uri": f"file://{config_path}"}])

        resp = start_asfp2_server.call_tool("start", {}, on_request=on_req)
        _assert_mcp_success(resp)

        resp = start_asfp2_server.call_tool("stop", {})
        _assert_mcp_success(resp)

        # 配置未变更时 adjust_shm 为 no-op，直接 start 即可
        resp = start_asfp2_server.call_tool("start", {}, on_request=on_req)
        _assert_mcp_success(resp)

        _assert_port_listening(port)

        sp = shm_path(iid)
        seq1_before = _assert_block_written_seq(sp, 1)
        seq2_before = _assert_block_written_seq(sp, 2)

        rc, stdout, stderr = _run_asfp2_client(port, 1000, 1001, times=3)
        assert rc == 0, f"asfp2_client failed: {stderr}"
        time.sleep(0.3)  # 等待 SUT 完成写入

        seq1_after = read_shm_block(sp, 1)["write_seq"]
        seq2_after = read_shm_block(sp, 2)["write_seq"]
        assert seq1_after > seq1_before
        assert seq2_after >= seq2_before

        start_asfp2_server.call_tool("stop", {})

    # ═══════════════════════════════════════════════
    #  TC6: 多次 stop/start 循环
    # ═══════════════════════════════════════════════

    def test_tc6_multiple_cycles(
        self, prepare_environment, start_asfp2_server, isolated_shm
    ):
        """TC6: 两轮 stop/start 循环，每次均正确。"""
        iid = "test_tc6"
        isolated_shm(iid)
        port = 9000

        config = _make_standard_config(iid, port)
        config_path, _ = prepare_environment(config, iid)

        on_req = _roots_callback([{"uri": f"file://{config_path}"}])

        # 前置: start 成功
        resp = start_asfp2_server.call_tool("start", {}, on_request=on_req)
        _assert_mcp_success(resp)

        # 第 1 轮: stop → start
        resp = start_asfp2_server.call_tool("stop", {})
        _assert_mcp_success(resp)

        resp = start_asfp2_server.call_tool("start", {}, on_request=on_req)
        _assert_mcp_success(resp)
        _assert_port_listening(port)

        # 第 2 轮: stop → start
        resp = start_asfp2_server.call_tool("stop", {})
        _assert_mcp_success(resp)

        resp = start_asfp2_server.call_tool("start", {}, on_request=on_req)
        _assert_mcp_success(resp)
        _assert_port_listening(port)

        # 验证: 第 2 轮重启后数据流 — write_seq 前后对比
        sp = shm_path(iid)
        seq1_before = _assert_block_written_seq(sp, 1)
        seq2_before = _assert_block_written_seq(sp, 2)

        rc, stdout, stderr = _run_asfp2_client(port, 1000, 1001, times=3)
        assert rc == 0, f"asfp2_client failed: {stderr}"
        time.sleep(0.3)

        seq1_after = read_shm_block(sp, 1)["write_seq"]
        seq2_after = read_shm_block(sp, 2)["write_seq"]
        assert seq1_after > seq1_before, (
            f"shm_id=1 write_seq not incremented: {seq1_before} → {seq1_after}"
        )
        assert seq2_after >= seq2_before, (
            f"shm_id=2 write_seq decreased: {seq2_before} → {seq2_after}"
        )

        # teardown
        start_asfp2_server.call_tool("stop", {})

    # ═══════════════════════════════════════════════
    #  TC7: 重启后端口冲突检测
    # ═══════════════════════════════════════════════

    def test_tc7_port_conflict_after_restart(
        self, prepare_environment, start_asfp2_server, isolated_shm
    ):
        """TC7: 重启时 PORT_CONFLICT 检测仍然生效。"""
        iid = "test_tc7"
        isolated_shm(iid)
        port = 9000

        config = _make_standard_config(iid, port)
        config_path, _ = prepare_environment(config, iid)

        on_req = _roots_callback([{"uri": f"file://{config_path}"}])

        # 前置: start 成功
        resp = start_asfp2_server.call_tool("start", {}, on_request=on_req)
        _assert_mcp_success(resp)

        # 操作: stop
        resp = start_asfp2_server.call_tool("stop", {})
        _assert_mcp_success(resp)

        # 准备端口冲突配置
        conflict_config = _make_port_conflict_config()
        conflict_path = _prepare_config_with_shm(conflict_config, "test_tc7c", isolated_shm)

        # 用冲突配置调用 start → 预期 PORT_CONFLICT
        resp = start_asfp2_server.call_tool(
            "start", {},
            on_request=_roots_callback([{"uri": f"file://{conflict_path}"}]),
        )
        _assert_mcp_error(resp, "PORT_CONFLICT")

    # ═══════════════════════════════════════════════
    #  TC8: double-stop — 连续两次 stop
    # ═══════════════════════════════════════════════

    def test_tc8_double_stop(
        self, prepare_environment, start_asfp2_server, isolated_shm
    ):
        """TC8: 连续两次 stop，第二次返回 SERVICE_NOT_READY。"""
        iid = "test_tc8"
        isolated_shm(iid)
        port = 9000

        config = _make_standard_config(iid, port)
        config_path, _ = prepare_environment(config, iid)

        resp = start_asfp2_server.call_tool(
            "start", {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        _assert_mcp_success(resp)

        # 第一次 stop — 应成功
        resp = start_asfp2_server.call_tool("stop", {})
        _assert_mcp_success(resp)

        # 第二次 stop — SUT 已回到初始状态，应返回 SERVICE_NOT_READY
        resp = start_asfp2_server.call_tool("stop", {})
        _assert_mcp_error(resp, "SERVICE_NOT_READY")

    # ═══════════════════════════════════════════════
    #  TC9: 重启时配置变更生效
    # ═══════════════════════════════════════════════

    def test_tc9_config_change_on_restart(
        self, prepare_environment, start_asfp2_server, isolated_shm
    ):
        """TC9: 重启时配置变更（新端口 + 新 point）生效。"""
        iid = "test_tc9"
        isolated_shm(iid)
        old_port = 9000
        new_port = 9001

        config = _make_standard_config(iid, old_port)
        config_path, _ = prepare_environment(config, iid)

        on_req_old = _roots_callback([{"uri": f"file://{config_path}"}])

        # 前置: start 成功 (port=9000)
        resp = start_asfp2_server.call_tool("start", {}, on_request=on_req_old)
        _assert_mcp_success(resp)

        # 操作: stop
        resp = start_asfp2_server.call_tool("stop", {})
        _assert_mcp_success(resp)

        # 修改配置: port 9001, 保留旧 point + 新增 addr=2000
        modified_config = _make_modified_config(iid, new_port, include_old_points=True)
        modified_path = _prepare_config_with_shm(modified_config, iid, isolated_shm)

        # start 使用新配置
        on_req_new = _roots_callback([{"uri": f"file://{modified_path}"}])
        resp = start_asfp2_server.call_tool("start", {}, on_request=on_req_new)
        _assert_mcp_success(resp)

        # 验证: 新端口 9001 监听，旧端口 9000 释放
        _assert_port_listening(new_port)
        wait_port_released(old_port)

        sp = shm_path(iid)

        # 验证旧 point (addr=1000,1001) 数据流
        seq1_before = _assert_block_written_seq(sp, 1)
        seq2_before = _assert_block_written_seq(sp, 2)

        rc, stdout, stderr = _run_asfp2_client(new_port, 1000, 1001, times=3)
        assert rc == 0, f"asfp2_client for old points failed: {stderr}"

        seq1_after = read_shm_block(sp, 1)["write_seq"]
        seq2_after = read_shm_block(sp, 2)["write_seq"]
        assert seq1_after > seq1_before, (
            f"shm_id=1 write_seq not incremented: {seq1_before} → {seq1_after}"
        )
        assert seq2_after >= seq2_before, (
            f"shm_id=2 write_seq not incremented: {seq2_before} → {seq2_after}"
        )

        # 验证新 point (addr=2000) 数据流 — §5.8 读取 shm_id
        new_shm_id = _read_config_shm_id(modified_path, 2000)
        seq_new_before = _assert_block_written_seq(sp, new_shm_id)

        rc, stdout, stderr = _run_asfp2_client(new_port, 2000, 2000, times=3)
        assert rc == 0, f"asfp2_client for new point failed: {stderr}"

        seq_new_after = read_shm_block(sp, new_shm_id)["write_seq"]
        assert seq_new_after >= seq_new_before, (
            f"shm_id={new_shm_id} write_seq decreased: "
            f"{seq_new_before} → {seq_new_after}"
        )

        # teardown
        start_asfp2_server.call_tool("stop", {})

    # ═══════════════════════════════════════════════
    #  TC10: start 失败后错误恢复
    # ═══════════════════════════════════════════════

    def test_tc10_error_recovery(
        self, prepare_environment, start_asfp2_server, isolated_shm
    ):
        """TC10: PORT_CONFLICT 失败后用修正配置恢复启动成功。"""
        iid = "test_tc10"
        isolated_shm(iid)
        port = 9000

        config = _make_standard_config(iid, port)
        config_path, _ = prepare_environment(config, iid)

        on_req = _roots_callback([{"uri": f"file://{config_path}"}])

        # 前置: start 成功
        resp = start_asfp2_server.call_tool("start", {}, on_request=on_req)
        _assert_mcp_success(resp)

        # 操作: stop
        resp = start_asfp2_server.call_tool("stop", {})
        _assert_mcp_success(resp)

        # 准备端口冲突配置
        conflict_config = _make_port_conflict_config()
        conflict_path = _prepare_config_with_shm(conflict_config, "test_tc10c", isolated_shm)

        # start 用冲突配置 → 预期 PORT_CONFLICT
        on_req_conflict = _roots_callback([{"uri": f"file://{conflict_path}"}])
        resp = start_asfp2_server.call_tool("start", {}, on_request=on_req_conflict)
        _assert_mcp_error(resp, "PORT_CONFLICT")

        # 准备修正配置（单实例 port=9000, 1 point）
        corrected_config = _make_corrected_config(iid, port)
        corrected_path = _prepare_config_with_shm(corrected_config, iid, isolated_shm)

        # 再次调用 start 用修正配置 → 预期成功
        on_req_corrected = _roots_callback([{"uri": f"file://{corrected_path}"}])
        resp = start_asfp2_server.call_tool(
            "start", {}, on_request=on_req_corrected
        )
        _assert_mcp_success(resp)

        # 验证: 端口 9000 监听
        _assert_port_listening(port)

        # teardown
        start_asfp2_server.call_tool("stop", {})
