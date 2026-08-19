"""
C4_FUN_00066 功能测试 — c4_iec104_client Stop-Start 协议。

验证 c4_iec104_client 的 Stop-Start 协议：
1. stop 在运行状态返回 success 并销毁全部实例（write_seq 停止递增）
2. stop 在未启动状态幂等返回 success
3. start 在已运行状态返回 ALREADY_RUNNING
4. 简单重启（stop → start）后数据流恢复
5. 完整 Stop-Start 协议（stop → adjust_shm → start）
6. 多次 stop/start 循环正确
7. 重启时配置变更（新 point）生效
8. 重启后数据流恢复（含值变化）

严格按 README.md 规格实现，不参考 Go 源码。
"""

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
    _make_iec104d_config,
    _make_iec104d_point,
    _run_adjust_shm,
    _write_config_file,
    wait_write_seq_advanced,
)
from shm_helpers import read_shm_block, read_shm_value, shm_path  # noqa: E402


# ──────────────────────────────────────────────
#  辅助函数
# ──────────────────────────────────────────────

STANDARD_IEC_POINTS = [
    ("TF_TEST_AI001", 16385),
]


def _standard_c4_instances(port, ip="127.0.0.1", t0=5):
    """§3.1 标准 c4 实例：单实例 1 point（遥测 FLOAT32）。"""
    return [
        _make_c4_instance(
            "停止重启测试主变", "sr_transformer", port,
            [_make_c4_point("pt_a", 16385)],
            t0=t0, ip=ip,
        )
    ]


def _setup_standard(
    start_iec104d, write_redis, prepare_environment, start_iec104_client,
    isolated_shm, instance_id,
):
    """搭建标准配置（§3.1）数据通路，返回 (sut, config_path, sp, port)。"""
    isolated_shm(instance_id)
    port = _free_port()

    iec_cfg = _make_iec104d_config(
        port, [_make_iec104d_point(*p) for p in STANDARD_IEC_POINTS]
    )
    start_iec104d(_write_config_file(iec_cfg))
    write_redis("TF_TEST_AI001", 1.5)

    config_path, _ = prepare_environment(
        _make_c4_config(_standard_c4_instances(port)), instance_id
    )
    sut = start_iec104_client()
    return sut, config_path, shm_path(instance_id), port


def _wait_value(sp, shm_id, data_type, expected, timeout=3.0, interval=0.05):
    """轮询等待 shm block 的 value 达到期望值（浮点用近似比较）。"""
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = read_shm_value(sp, shm_id, data_type)
        if data_type == 10:  # FLOAT32 近似比较
            if abs(value - expected) <= 1e-6:
                return value
        elif value == expected:
            return value
        time.sleep(interval)
    raise RuntimeError(
        f"shm value did not reach {expected} within {timeout}s: got {value}"
    )


class TestStopRestart:

    # ── TC1: stop — 运行中停止，write_seq 停止递增 ──────────

    def test_tc1_stop_running(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm,
    ):
        """TC1: stop 在运行状态返回 success，write_seq 停止递增。"""
        instance_id = "c4_fun66tc1"
        sut, config_path, sp, _ = _setup_standard(
            start_iec104d, write_redis, prepare_environment, start_iec104_client,
            isolated_shm, instance_id,
        )

        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))

        seq0 = read_shm_block(sp, 1)["write_seq"]
        wait_write_seq_advanced(sp, 1, seq0)

        _assert_mcp_success(sut.call_tool("stop", {}))

        seq1 = read_shm_block(sp, 1)["write_seq"]
        time.sleep(0.3)  # 3 个采集周期（gi_timer=100ms）
        assert read_shm_block(sp, 1)["write_seq"] == seq1, (
            "write_seq still advancing after stop"
        )

    # ── TC2: stop — 未启动时调用（幂等）──────────

    def test_tc2_stop_before_start(self, start_iec104_client):
        """TC2: stop 在未启动状态幂等返回 success。"""
        sut = start_iec104_client()
        _assert_mcp_success(sut.call_tool("stop", {}))

    # ── TC3: start — 已运行时重复调用 ──────────

    def test_tc3_start_while_running(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm,
    ):
        """TC3: start 在已运行状态返回 ALREADY_RUNNING。"""
        instance_id = "c4_fun66tc3"
        sut, config_path, sp, _ = _setup_standard(
            start_iec104d, write_redis, prepare_environment, start_iec104_client,
            isolated_shm, instance_id,
        )

        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))
        _assert_mcp_error(
            sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}),
            "ALREADY_RUNNING",
        )

    # ── TC4: 简单重启（stop → start，无配置变更）──────────

    def test_tc4_simple_restart(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm,
    ):
        """TC4: stop → start 后数据流恢复。"""
        instance_id = "c4_fun66tc4"
        sut, config_path, sp, _ = _setup_standard(
            start_iec104d, write_redis, prepare_environment, start_iec104_client,
            isolated_shm, instance_id,
        )

        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))
        _assert_mcp_success(sut.call_tool("stop", {}))
        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))

        seq_before = read_shm_block(sp, 1)["write_seq"]
        wait_write_seq_advanced(sp, 1, seq_before)

        # 读 value 比对期望值（1.5）
        value = _wait_value(sp, 1, 10, 1.5)
        assert abs(value - 1.5) < 1e-6

    # ── TC5: 完整 Stop-Start 协议（stop → adjust_shm → start）──────────

    def test_tc5_full_protocol(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm,
    ):
        """TC5: stop → adjust_shm → start 三步全链路正确。"""
        instance_id = "c4_fun66tc5"
        sut, config_path, sp, _ = _setup_standard(
            start_iec104d, write_redis, prepare_environment, start_iec104_client,
            isolated_shm, instance_id,
        )

        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))
        _assert_mcp_success(sut.call_tool("stop", {}))

        _run_adjust_shm(config_path, instance_id)

        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))

        seq_before = read_shm_block(sp, 1)["write_seq"]
        wait_write_seq_advanced(sp, 1, seq_before)

        value = _wait_value(sp, 1, 10, 1.5)
        assert abs(value - 1.5) < 1e-6

    # ── TC6: 多次 stop/start 循环 ──────────

    def test_tc6_multiple_cycles(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm,
    ):
        """TC6: 两轮 stop/start 循环，每次重启后数据流均恢复。"""
        instance_id = "c4_fun66tc6"
        sut, config_path, sp, _ = _setup_standard(
            start_iec104d, write_redis, prepare_environment, start_iec104_client,
            isolated_shm, instance_id,
        )

        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))

        for _ in range(2):
            _assert_mcp_success(sut.call_tool("stop", {}))
            _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))
            seq_before = read_shm_block(sp, 1)["write_seq"]
            wait_write_seq_advanced(sp, 1, seq_before)

    # ── TC7: 重启时配置变更生效（新增 point）──────────

    def test_tc7_config_change_on_restart(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm,
    ):
        """TC7: 重启时新增 point（16386）生效，新旧 point 均写入。"""
        instance_id = "c4_fun66tc7"
        isolated_shm(instance_id)
        port = _free_port()

        # iec104d 预配两点（TF_TEST_AI001→16385、TF_TEST_AI002→16386）
        iec_cfg = _make_iec104d_config(
            port,
            [
                _make_iec104d_point("TF_TEST_AI001", 16385),
                _make_iec104d_point("TF_TEST_AI002", 16386),
            ],
        )
        start_iec104d(_write_config_file(iec_cfg))
        write_redis("TF_TEST_AI001", 1.5)
        write_redis("TF_TEST_AI002", 2.5)

        # c4 初始 1 个 point（addr=16385）
        c4_insts = [
            _make_c4_instance(
                "停止重启测试主变", "sr_transformer", port,
                [_make_c4_point("pt_a", 16385)],
            )
        ]
        config_path, _ = prepare_environment(_make_c4_config(c4_insts), instance_id)
        sut = start_iec104_client()

        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))
        _assert_mcp_success(sut.call_tool("stop", {}))

        # 修改 c4 配置：新增 point 16386（shm_id=0），写新配置文件
        c4_insts2 = [
            _make_c4_instance(
                "停止重启测试主变", "sr_transformer", port,
                [
                    _make_c4_point("pt_a", 16385),
                    _make_c4_point("pt_b", 16386),
                ],
            )
        ]
        new_config_path = _write_config_file(_make_c4_config(c4_insts2))

        # adjust_shm 为新 point 分配 shm_id
        _run_adjust_shm(new_config_path, instance_id)

        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": new_config_path}))

        # 旧 point（shm_id=1）与新 point（shm_id=2）数据均正常写入
        sp = shm_path(instance_id)
        for sid in (1, 2):
            seq_before = read_shm_block(sp, sid)["write_seq"]
            wait_write_seq_advanced(sp, sid, seq_before)

    # ── TC8: 重启后数据流恢复（含值变化）──────────

    def test_tc8_restart_value_change(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm,
    ):
        """TC8: 重启后通路恢复，且能持续采集更新后的数据（1.5 → 2.5）。"""
        instance_id = "c4_fun66tc8"
        sut, config_path, sp, _ = _setup_standard(
            start_iec104d, write_redis, prepare_environment, start_iec104_client,
            isolated_shm, instance_id,
        )

        # 1. 首次 start，采集到 value = 1.5
        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))
        value = _wait_value(sp, 1, 10, 1.5)
        assert abs(value - 1.5) < 1e-6

        # 2. 简单重启（stop → start）
        _assert_mcp_success(sut.call_tool("stop", {}))
        _assert_mcp_success(sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path}))

        seq_before = read_shm_block(sp, 1)["write_seq"]
        wait_write_seq_advanced(sp, 1, seq_before)

        # 3. 改写 Redis 值，等待新一轮采集
        write_redis("TF_TEST_AI001", 2.5)
        value = _wait_value(sp, 1, 10, 2.5, timeout=5.0)
        assert abs(value - 2.5) < 1e-6
