"""
C4_FUN_00013 测试用例 — c4_iec104_client 事件驱动数据采集通路。

验证 c4_iec104_client 的事件驱动数据采集通路（真实设备端 iec104d + Redis + redis_tool）：
1. 主站建立 TCP 连接 + STARTDT 激活 + 按 gi_timer 周期总召唤（GI）+ 按 it_timer 周期累计量召唤（IT）
2. 三类数据类型正确解析：遥信单点（M_SP_NA_1 → BOOLEAN）、遥测短浮点（M_ME_NC_1 → FLOAT32）、
   遥脉累计量（M_IT_NA_1 → INT32）
3. 带时标类型（with_cp56time2a=1）正确解析
4. 按 (instance, ioa) → shm_id 映射写入共享内存（write_seq 递增、value 正确、block.type 正确）
5. 数据变化后主站更新共享内存
6. 多实例各自独立采集

严格按 README.md 规格实现，不参考 Go 源码。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # type: ignore

from conftest import (  # noqa: E402
    _assert_mcp_success,
    _free_port,
    _make_c4_config,
    _make_c4_instance,
    _make_c4_point,
    _make_iec104d_config,
    _make_iec104d_point,
    _write_config_file,
    wait_write_seq_advanced,
)
from shm_helpers import read_shm_block, read_shm_value, shm_path  # noqa: E402


# ──────────────────────────────────────────────
#  辅助函数
# ──────────────────────────────────────────────


def _start_acquisition(
    start_iec104d, write_redis, prepare_environment, start_iec104_client,
    isolated_shm, instance_id, iec_points, iec_vals, build_instances,
    with_cp56time2a=0,
):
    """搭建完整数据通路并 start，返回 (sut, shm_path_str)。

    iec_points: [(key, 104addr)] — iec104d 设备端点。
    iec_vals:   [(key, value)] — 写入 Redis 的键值。
    build_instances(port) → [c4 instance dict]。
    """
    isolated_shm(instance_id)
    port = _free_port()

    iec_cfg = _make_iec104d_config(
        port,
        [_make_iec104d_point(k, a) for (k, a) in iec_points],
        with_cp56time2a=with_cp56time2a,
    )
    start_iec104d(_write_config_file(iec_cfg))
    for key, val in iec_vals:
        write_redis(key, val)

    instances = build_instances(port)
    config_path, _ = prepare_environment(_make_c4_config(instances), instance_id)

    sut = start_iec104_client()
    resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
    _assert_mcp_success(resp)
    return sut, shm_path(instance_id)


def _acquire_value(sp, shm_id, data_type):
    """等待 write_seq 递增后读取 value，返回 (block, value)。"""
    seq_before = read_shm_block(sp, shm_id)["write_seq"]
    wait_write_seq_advanced(sp, shm_id, seq_before)
    block = read_shm_block(sp, shm_id)
    value = read_shm_value(sp, shm_id, data_type)
    return block, value


def _wait_shm_value(sp, shm_id, expected, data_type, timeout=3.0, interval=0.05):
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


def _assert_float(value, expected):
    """FLOAT32 近似比较。"""
    assert abs(value - expected) < 1e-6, (
        f"float mismatch: {value} != {expected}"
    )


class TestIec104Acquisition:

    # ── TC1: 遥信单点（YX，M_SP_NA_1 → BOOLEAN）──────────

    def test_tc1_yx_boolean(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm,
    ):
        """TC1: addr=1 遥信，写 DI=1 → block.type=0，value=1。"""
        instance_id = "c4_fun13tc1"

        def build(port):
            return [
                _make_c4_instance(
                    "采集测试主变", "acq_transformer", port,
                    [_make_c4_point("di_1", 1)],
                )
            ]

        _, sp = _start_acquisition(
            start_iec104d, write_redis, prepare_environment, start_iec104_client,
            isolated_shm, instance_id,
            [("TF_TEST_DI001", 1)],
            [("TF_TEST_DI001", 1)],
            build,
        )

        block, value = _acquire_value(sp, 1, 0)
        assert block["type"] == 0, f"expected BOOLEAN(0), got {block['type']}"
        assert value == 1

    # ── TC2: 遥测短浮点（YC，M_ME_NC_1 → FLOAT32）──────────

    def test_tc2_yc_float32(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm,
    ):
        """TC2: addr=16385 遥测，写 AI=1.5 → block.type=10，value=1.5。"""
        instance_id = "c4_fun13tc2"

        def build(port):
            return [
                _make_c4_instance(
                    "采集测试主变", "acq_transformer", port,
                    [_make_c4_point("ai_1", 16385)],
                )
            ]

        _, sp = _start_acquisition(
            start_iec104d, write_redis, prepare_environment, start_iec104_client,
            isolated_shm, instance_id,
            [("TF_TEST_AI001", 16385)],
            [("TF_TEST_AI001", 1.5)],
            build,
        )

        block, value = _acquire_value(sp, 1, 10)
        assert block["type"] == 10, f"expected FLOAT32(10), got {block['type']}"
        _assert_float(value, 1.5)

    # ── TC3: 遥脉累计量（YM，M_IT_NA_1 → INT32）──────────

    def test_tc3_ym_int32(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm,
    ):
        """TC3: addr=25601 遥脉，写 AI101=1000 → block.type=5，value=1000。"""
        instance_id = "c4_fun13tc3"

        def build(port):
            return [
                _make_c4_instance(
                    "采集测试主变", "acq_transformer", port,
                    [_make_c4_point("ai_101", 25601)],
                    it_timer=100, gi_timer=0,
                )
            ]

        _, sp = _start_acquisition(
            start_iec104d, write_redis, prepare_environment, start_iec104_client,
            isolated_shm, instance_id,
            [("TF_TEST_AI101", 25601)],
            [("TF_TEST_AI101", 1000)],
            build,
        )

        block, value = _acquire_value(sp, 1, 5)
        assert block["type"] == 5, f"expected INT32(5), got {block['type']}"
        assert value == 1000

    # ── TC4: 带时标类型（with_cp56time2a=1）──────────

    def test_tc4_with_timestamp(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm,
    ):
        """TC4: with_cp56time2a=1，遥信 + 遥测，值字段与无时标类型一致。"""
        instance_id = "c4_fun13tc4"

        def build(port):
            return [
                _make_c4_instance(
                    "采集测试主变", "acq_transformer", port,
                    [
                        _make_c4_point("di_1", 1),
                        _make_c4_point("ai_1", 16385),
                    ],
                )
            ]

        _, sp = _start_acquisition(
            start_iec104d, write_redis, prepare_environment, start_iec104_client,
            isolated_shm, instance_id,
            [("TF_TEST_DI001", 1), ("TF_TEST_AI001", 16385)],
            [("TF_TEST_DI001", 1), ("TF_TEST_AI001", 2.5)],
            build, with_cp56time2a=1,
        )

        block_di, v_di = _acquire_value(sp, 1, 0)
        block_ai, v_ai = _acquire_value(sp, 2, 10)
        assert block_di["type"] == 0, f"expected BOOLEAN(0), got {block_di['type']}"
        assert v_di == 1
        assert block_ai["type"] == 10, f"expected FLOAT32(10), got {block_ai['type']}"
        _assert_float(v_ai, 2.5)

    # ── TC5: 多类型混合（遥信 + 遥测 + 遥脉同测）──────────

    def test_tc5_multi_type(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm,
    ):
        """TC5: 3 point 混合，type 0/10/5，value 0/3.75/2000。"""
        instance_id = "c4_fun13tc5"

        def build(port):
            return [
                _make_c4_instance(
                    "采集测试主变", "acq_transformer", port,
                    [
                        _make_c4_point("di_1", 1),
                        _make_c4_point("ai_1", 16385),
                        _make_c4_point("ai_101", 25601),
                    ],
                    it_timer=100,
                )
            ]

        _, sp = _start_acquisition(
            start_iec104d, write_redis, prepare_environment, start_iec104_client,
            isolated_shm, instance_id,
            [
                ("TF_TEST_DI001", 1),
                ("TF_TEST_AI001", 16385),
                ("TF_TEST_AI101", 25601),
            ],
            [
                ("TF_TEST_DI001", 0),
                ("TF_TEST_AI001", 3.75),
                ("TF_TEST_AI101", 2000),
            ],
            build,
        )

        block_di, v_di = _acquire_value(sp, 1, 0)
        block_ai, v_ai = _acquire_value(sp, 2, 10)
        block_ym, v_ym = _acquire_value(sp, 3, 5)

        assert block_di["type"] == 0, f"expected BOOLEAN(0), got {block_di['type']}"
        assert v_di == 0
        assert block_ai["type"] == 10, f"expected FLOAT32(10), got {block_ai['type']}"
        _assert_float(v_ai, 3.75)
        assert block_ym["type"] == 5, f"expected INT32(5), got {block_ym['type']}"
        assert v_ym == 2000

    # ── TC6: 周期采集（总召 GI + 累计量召唤 IT）──────────

    def test_tc6_periodic(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm,
    ):
        """TC6: 3 point 周期采集，write_seq 持续递增。"""
        instance_id = "c4_fun13tc6"

        def build(port):
            return [
                _make_c4_instance(
                    "采集测试主变", "acq_transformer", port,
                    [
                        _make_c4_point("di_1", 1),
                        _make_c4_point("ai_1", 16385),
                        _make_c4_point("ai_101", 25601),
                    ],
                    it_timer=100,
                )
            ]

        _, sp = _start_acquisition(
            start_iec104d, write_redis, prepare_environment, start_iec104_client,
            isolated_shm, instance_id,
            [
                ("TF_TEST_DI001", 1),
                ("TF_TEST_AI001", 16385),
                ("TF_TEST_AI101", 25601),
            ],
            [
                ("TF_TEST_DI001", 0),
                ("TF_TEST_AI001", 3.75),
                ("TF_TEST_AI101", 2000),
            ],
            build,
        )

        for sid in (1, 2, 3):
            seq0 = read_shm_block(sp, sid)["write_seq"]
            wait_write_seq_advanced(sp, sid, seq0)
            seq1 = read_shm_block(sp, sid)["write_seq"]
            wait_write_seq_advanced(sp, sid, seq1)

    # ── TC7: 数据变化（值更新）──────────

    def test_tc7_data_change(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm,
    ):
        """TC7: addr=16385，写 1.5 → 采到后改写 2.5 → 采到 2.5。"""
        instance_id = "c4_fun13tc7"

        def build(port):
            return [
                _make_c4_instance(
                    "采集测试主变", "acq_transformer", port,
                    [_make_c4_point("ai_1", 16385)],
                )
            ]

        _, sp = _start_acquisition(
            start_iec104d, write_redis, prepare_environment, start_iec104_client,
            isolated_shm, instance_id,
            [("TF_TEST_AI001", 16385)],
            [("TF_TEST_AI001", 1.5)],
            build,
        )

        _, v1 = _acquire_value(sp, 1, 10)
        _assert_float(v1, 1.5)

        write_redis("TF_TEST_AI001", 2.5)
        v2 = _wait_shm_value(sp, 1, 2.5, 10, timeout=5.0)
        _assert_float(v2, 2.5)

    # ── TC8: 多实例独立采集 ──────────

    def test_tc8_multi_instance(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm,
    ):
        """TC8: 1 个 iec104d 两点，2 个 c4 实例各 1 point，各自独立采集。"""
        instance_id = "c4_fun13tc8"

        def build(port):
            return [
                _make_c4_instance(
                    "设备1", "dev1", port, [_make_c4_point("pt_a", 16385)]
                ),
                _make_c4_instance(
                    "设备2", "dev2", port, [_make_c4_point("pt_a", 16386)]
                ),
            ]

        _, sp = _start_acquisition(
            start_iec104d, write_redis, prepare_environment, start_iec104_client,
            isolated_shm, instance_id,
            [("TF_TEST_AI001", 16385), ("TF_TEST_AI002", 16386)],
            [("TF_TEST_AI001", 1.5), ("TF_TEST_AI002", 2.5)],
            build,
        )

        block1, v1 = _acquire_value(sp, 1, 10)
        block2, v2 = _acquire_value(sp, 2, 10)

        assert block1["type"] == 10, f"expected FLOAT32(10), got {block1['type']}"
        assert block2["type"] == 10, f"expected FLOAT32(10), got {block2['type']}"
        _assert_float(v1, 1.5)
        _assert_float(v2, 2.5)
