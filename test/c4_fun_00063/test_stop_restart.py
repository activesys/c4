"""
C4_FUN_00063 功能测试 — c4_modbus_client Stop-Start 协议。

验证 c4_modbus_client 的 Stop-Start 协议：
1. stop 在运行状态返回 success 并停止轮询（释放 TCP 连接）
2. stop 在未启动状态幂等返回 success
3. start 在已运行状态返回 ALREADY_RUNNING
4. 简单重启（stop → start）后连接恢复、数据流恢复
5. 完整 Stop-Start 协议（stop → adjust_shm → start）
6. 多次 stop/start 循环正确
7. 连续两次 stop（double-stop）幂等
8. 重启时配置变更（新端口 / 新 point）生效
9. start 失败（CONNECT_FAILED）后修正配置恢复成功

严格按 README.md 规格实现，不参考 Go 源码。
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # type: ignore

from conftest import (  # noqa: E402
    _assert_mcp_error,
    _assert_mcp_success,
    _free_port,
    _make_c4_config,
    _make_c4_instance,
    _make_c4_point,
    _make_modbusd_config,
    _make_modbusd_point,
    _run_adjust_shm,
    _write_config_file,
    wait_write_seq_advanced,
)
from shm_helpers import read_shm_block, shm_path  # noqa: E402


# ──────────────────────────────────────────────
#  辅助函数
# ──────────────────────────────────────────────

STANDARD_MB_POINTS = [
    ("MB_PT_001", 1000, 2, 4),
    ("MB_PT_002", 1002, 2, 4),
]


def _standard_c4_instances(port, ip="127.0.0.1", t0=5):
    """§3.1 标准 c4 实例：单实例 2 points（UINT16）。"""
    return [
        _make_c4_instance(
            "停止重启测试设备", "sr_device", port,
            [
                _make_c4_point("pt_a", 1, 1000, 3, 4),
                _make_c4_point("pt_b", 1, 1002, 3, 4),
            ],
            t0=t0, ip=ip,
        )
    ]


def _setup_standard(
    start_modbusd, write_redis, prepare_environment, start_modbus_client,
    isolated_shm, instance_id,
):
    """搭建标准配置（§3.1）数据通路，返回 (sut, config_path, sp, port)。"""
    isolated_shm(instance_id)
    port = _free_port()

    mb_cfg = _make_modbusd_config(
        port, [_make_modbusd_point(*p) for p in STANDARD_MB_POINTS]
    )
    start_modbusd(_write_config_file(mb_cfg))
    write_redis("MB_PT_001", 100)
    write_redis("MB_PT_002", 200)

    config_path, _ = prepare_environment(
        _make_c4_config(_standard_c4_instances(port)), instance_id
    )
    sut = start_modbus_client()
    return sut, config_path, shm_path(instance_id), port


class TestStopRestart:

    # ── TC1: stop — 运行中停止，轮询停止 ──────────

    def test_tc1_stop_running(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC1: stop 在运行状态返回 success，write_seq 停止递增。"""
        instance_id = "c4_fun63tc1"
        sut, config_path, sp, _ = _setup_standard(
            start_modbusd, write_redis, prepare_environment, start_modbus_client,
            isolated_shm, instance_id,
        )

        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))

        seq0 = read_shm_block(sp, 1)["write_seq"]
        wait_write_seq_advanced(sp, 1, seq0)

        _assert_mcp_success(sut.call_tool("stop", {}))

        seq1 = read_shm_block(sp, 1)["write_seq"]
        time.sleep(0.3)  # 3 个轮询周期（timer=100ms）
        assert read_shm_block(sp, 1)["write_seq"] == seq1, (
            "write_seq still advancing after stop"
        )

    # ── TC2: stop — 未启动时调用 ──────────

    def test_tc2_stop_before_start(self, start_modbus_client):
        """TC2: stop 在未启动状态幂等返回 success。"""
        sut = start_modbus_client()
        _assert_mcp_success(sut.call_tool("stop", {}))

    # ── TC3: start — 已运行时重复调用 ──────────

    def test_tc3_start_while_running(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC3: start 在已运行状态返回 ALREADY_RUNNING。"""
        instance_id = "c4_fun63tc3"
        sut, config_path, sp, _ = _setup_standard(
            start_modbusd, write_redis, prepare_environment, start_modbus_client,
            isolated_shm, instance_id,
        )

        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))
        _assert_mcp_error(
            sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}), "ALREADY_RUNNING"
        )

    # ── TC4: 简单重启（stop → start，无配置变更）──────────

    def test_tc4_simple_restart(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC4: stop → start 后数据流恢复。"""
        instance_id = "c4_fun63tc4"
        sut, config_path, sp, _ = _setup_standard(
            start_modbusd, write_redis, prepare_environment, start_modbus_client,
            isolated_shm, instance_id,
        )

        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))
        _assert_mcp_success(sut.call_tool("stop", {}))
        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))

        seq_before = read_shm_block(sp, 1)["write_seq"]
        wait_write_seq_advanced(sp, 1, seq_before)

    # ── TC5: 完整 Stop-Start 协议（stop → adjust_shm → start）──────────

    def test_tc5_full_protocol(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC5: stop → adjust_shm → start 三步全链路正确。"""
        instance_id = "c4_fun63tc5"
        sut, config_path, sp, _ = _setup_standard(
            start_modbusd, write_redis, prepare_environment, start_modbus_client,
            isolated_shm, instance_id,
        )

        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))
        _assert_mcp_success(sut.call_tool("stop", {}))

        _run_adjust_shm(config_path, instance_id)

        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))

        seq_before = read_shm_block(sp, 1)["write_seq"]
        wait_write_seq_advanced(sp, 1, seq_before)

    # ── TC6: 多次 stop/start 循环 ──────────

    def test_tc6_multiple_cycles(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC6: 三轮 stop/start 循环，每次重启后轮询均恢复。"""
        instance_id = "c4_fun63tc6"
        sut, config_path, sp, _ = _setup_standard(
            start_modbusd, write_redis, prepare_environment, start_modbus_client,
            isolated_shm, instance_id,
        )

        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))

        for _ in range(3):
            _assert_mcp_success(sut.call_tool("stop", {}))
            _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))
            seq_before = read_shm_block(sp, 1)["write_seq"]
            wait_write_seq_advanced(sp, 1, seq_before)

    # ── TC7: double-stop — 连续两次 stop ──────────

    def test_tc7_double_stop(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC7: 连续两次 stop 均返回 success（幂等）。"""
        instance_id = "c4_fun63tc7"
        sut, config_path, sp, _ = _setup_standard(
            start_modbusd, write_redis, prepare_environment, start_modbus_client,
            isolated_shm, instance_id,
        )

        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))
        _assert_mcp_success(sut.call_tool("stop", {}))
        _assert_mcp_success(sut.call_tool("stop", {}))

    # ── TC8: 重启时配置变更生效（新端口 + 新 point）──────────

    def test_tc8_config_change_on_restart(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC8: 重启时新端口 + 新 point 均生效。"""
        instance_id = "c4_fun63tc8"
        sut, config_path, sp, port = _setup_standard(
            start_modbusd, write_redis, prepare_environment, start_modbus_client,
            isolated_shm, instance_id,
        )

        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))
        _assert_mcp_success(sut.call_tool("stop", {}))

        port2 = _free_port()
        mb_cfg2 = _make_modbusd_config(
            port2,
            [
                _make_modbusd_point("MB_PT_001", 1000, 2, 4),
                _make_modbusd_point("MB_PT_002", 1002, 2, 4),
                _make_modbusd_point("MB_PT_003", 2000, 2, 4),
            ],
        )
        start_modbusd(_write_config_file(mb_cfg2))
        write_redis("MB_PT_003", 300)

        c4_insts2 = [
            _make_c4_instance(
                "停止重启测试设备", "sr_device", port2,
                [
                    _make_c4_point("pt_a", 1, 1000, 3, 4),
                    _make_c4_point("pt_b", 1, 1002, 3, 4),
                    _make_c4_point("pt_c", 1, 2000, 3, 4),
                ],
            )
        ]
        new_config_path = _write_config_file(_make_c4_config(c4_insts2))

        _run_adjust_shm(new_config_path, instance_id)

        with open(new_config_path, "r") as f:
            new_cfg = json.load(f)
        new_shm_id = next(
            pt["shm_id"]
            for pt in new_cfg["c4_modbus_client"][0]["points"]
            if pt["addr"] == 2000
        )

        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": new_config_path}))

        for sid in (1, 2, new_shm_id):
            seq_before = read_shm_block(sp, sid)["write_seq"]
            wait_write_seq_advanced(sp, sid, seq_before)

    # ── TC9: start 失败后错误恢复 ──────────

    def test_tc9_error_recovery(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC9: CONNECT_FAILED 后修正配置重试成功。"""
        instance_id = "c4_fun63tc9"
        sut, config_path, sp, port = _setup_standard(
            start_modbusd, write_redis, prepare_environment, start_modbus_client,
            isolated_shm, instance_id,
        )

        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))
        _assert_mcp_success(sut.call_tool("stop", {}))

        unreachable_path = _write_config_file(
            _make_c4_config(_standard_c4_instances(502, ip="192.0.2.1", t0=2))
        )
        _run_adjust_shm(unreachable_path, instance_id)
        _assert_mcp_error(
            sut.call_tool("start", {"instance_id": instance_id, "config_path": unreachable_path}),
            "CONNECT_FAILED",
        )

        recover_path = _write_config_file(
            _make_c4_config(_standard_c4_instances(port))
        )
        _run_adjust_shm(recover_path, instance_id)
        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": recover_path}))

        seq_before = read_shm_block(sp, 1)["write_seq"]
        wait_write_seq_advanced(sp, 1, seq_before)
