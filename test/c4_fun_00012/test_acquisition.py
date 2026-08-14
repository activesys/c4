"""
C4_FUN_00012 测试用例 — c4_modbus_client 轮询采集数据通路。

验证 c4_modbus_client 的轮询采集数据通路（真实设备端 modbusd + Redis + redis_tool）：
1. 4 个读功能码（0x01~0x04）正确构造请求并解析响应
2. 各数据类型（BOOLEAN/BIT/INT16/UINT16/INT32/UINT32/FLOAT32）正确解码
3. 字节序规则（hton_register + swap）正确处理四种设备字节序（ABCD/BADC/CDAB/DCBA）
4. 解析后的值按 Seqlock 协议写入共享内存
5. 连接断开（modbusd 停止）后重连恢复
6. timer 周期轮询、每次写入覆盖前值

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
    _make_modbusd_config,
    _make_modbusd_point,
    _stop_process,
    _write_config_file,
    wait_write_seq_advanced,
)
from shm_helpers import read_shm_block, read_shm_value, shm_path  # noqa: E402


# ──────────────────────────────────────────────
#  辅助函数
# ──────────────────────────────────────────────


def _start_acquisition(
    start_modbusd, write_redis, prepare_environment, start_modbus_client,
    isolated_shm, instance_id, mb_points, mb_vals, build_instances,
    mb_hton=1, mb_swap=0,
):
    """搭建完整数据通路并 start，返回 (sut, shm_path_str)。

    mb_points: [(key, modbusaddr, funcode, type)] — modbusd 设备端点。
    mb_vals:   [(key, value)] — 写入 Redis 的键值。
    build_instances(port) → [c4 instance dict]。
    """
    isolated_shm(instance_id)
    port = _free_port()

    mb_cfg = _make_modbusd_config(
        port,
        [_make_modbusd_point(k, a, f, t) for (k, a, f, t) in mb_points],
        hton_register=mb_hton, swap=mb_swap,
    )
    start_modbusd(_write_config_file(mb_cfg))
    for key, val in mb_vals:
        write_redis(key, val)

    instances = build_instances(port)
    config_path, _ = prepare_environment(_make_c4_config(instances), instance_id)

    sut = start_modbus_client()
    resp = sut.call_tool("start", {"config_path": config_path})
    _assert_mcp_success(resp)
    return sut, shm_path(instance_id)


def _wait_value(sp, shm_id, data_type):
    """等待 write_seq 递增后读取 value。"""
    seq_before = read_shm_block(sp, shm_id)["write_seq"]
    wait_write_seq_advanced(sp, shm_id, seq_before)
    return read_shm_value(sp, shm_id, data_type)


def _assert_float(value, expected, rel=1e-5):
    """FLOAT32 近似比较。"""
    assert abs(value - expected) <= rel * abs(expected), (
        f"float mismatch: {value} != {expected}"
    )


class TestModbusAcquisition:

    # ── TC1: fun=3 读保持寄存器 — 单寄存器 UINT16 ──────────

    def test_tc1_uint16(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC1: fun=3, UINT16，value=0x1234。"""
        instance_id = "fun12_tc1"

        def build(port):
            return [
                _make_c4_instance(
                    "采集测试设备", "acq_device", port,
                    [_make_c4_point("pt_a", 1, 1000, 3, 4)],
                    hton_register=1,
                )
            ]

        _, sp = _start_acquisition(
            start_modbusd, write_redis, prepare_environment, start_modbus_client,
            isolated_shm, instance_id,
            [("MB_PT_001", 1000, 2, 4)],
            [("MB_PT_001", 4660)],
            build, mb_hton=1,
        )
        assert _wait_value(sp, 1, 4) == 0x1234

    # ── TC2: FLOAT32 — ABCD（标准大端）──────────

    def test_tc2_float32_abcd(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC2: ABCD 字节序 → c4 hton=1, swap=2。"""
        instance_id = "fun12_tc2"

        def build(port):
            return [
                _make_c4_instance(
                    "采集测试设备", "acq_device", port,
                    [_make_c4_point("pt_a", 1, 1000, 3, 10, swap=2)],
                    hton_register=1,
                )
            ]

        _, sp = _start_acquisition(
            start_modbusd, write_redis, prepare_environment, start_modbus_client,
            isolated_shm, instance_id,
            [("MB_AI_001", 1000, 2, 10)],
            [("MB_AI_001", 1.5)],
            build, mb_hton=1, mb_swap=1,
        )
        _assert_float(_wait_value(sp, 1, 10), 1.5)

    # ── TC3: FLOAT32 — BADC（寄存器内字节交换）──────────

    def test_tc3_float32_badc(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC3: BADC 字节序 → c4 hton=0, swap=2。"""
        instance_id = "fun12_tc3"

        def build(port):
            return [
                _make_c4_instance(
                    "采集测试设备", "acq_device", port,
                    [_make_c4_point("pt_a", 1, 1000, 3, 10, swap=2)],
                    hton_register=0,
                )
            ]

        _, sp = _start_acquisition(
            start_modbusd, write_redis, prepare_environment, start_modbus_client,
            isolated_shm, instance_id,
            [("MB_AI_001", 1000, 2, 10)],
            [("MB_AI_001", 1.5)],
            build, mb_hton=0, mb_swap=1,
        )
        _assert_float(_wait_value(sp, 1, 10), 1.5)

    # ── TC4: FLOAT32 — CDAB（低字在前）──────────

    def test_tc4_float32_cdab(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC4: CDAB 字节序 → c4 hton=1, swap=0。"""
        instance_id = "fun12_tc4"

        def build(port):
            return [
                _make_c4_instance(
                    "采集测试设备", "acq_device", port,
                    [_make_c4_point("pt_a", 1, 1000, 3, 10, swap=0)],
                    hton_register=1,
                )
            ]

        _, sp = _start_acquisition(
            start_modbusd, write_redis, prepare_environment, start_modbus_client,
            isolated_shm, instance_id,
            [("MB_AI_001", 1000, 2, 10)],
            [("MB_AI_001", 1.5)],
            build, mb_hton=1, mb_swap=0,
        )
        _assert_float(_wait_value(sp, 1, 10), 1.5)

    # ── TC5: FLOAT32 — DCBA（完全反转）──────────

    def test_tc5_float32_dcba(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC5: DCBA 字节序 → c4 hton=0, swap=0。"""
        instance_id = "fun12_tc5"

        def build(port):
            return [
                _make_c4_instance(
                    "采集测试设备", "acq_device", port,
                    [_make_c4_point("pt_a", 1, 1000, 3, 10, swap=0)],
                    hton_register=0,
                )
            ]

        _, sp = _start_acquisition(
            start_modbusd, write_redis, prepare_environment, start_modbus_client,
            isolated_shm, instance_id,
            [("MB_AI_001", 1000, 2, 10)],
            [("MB_AI_001", 1.5)],
            build, mb_hton=0, mb_swap=0,
        )
        _assert_float(_wait_value(sp, 1, 10), 1.5)

    # ── TC6: 多类型覆盖（INT32 / UINT32 / FLOAT32）──────────

    def test_tc6_multi_type(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC6: 3 个 point 覆盖 INT32/UINT32/FLOAT32（ABCD 字节序）。"""
        instance_id = "fun12_tc6"

        def build(port):
            return [
                _make_c4_instance(
                    "采集测试设备", "acq_device", port,
                    [
                        _make_c4_point("pt_i32", 1, 1000, 3, 5, swap=2),
                        _make_c4_point("pt_u32", 1, 1002, 3, 6, swap=2),
                        _make_c4_point("pt_f32", 1, 1004, 3, 10, swap=2),
                    ],
                    hton_register=1,
                )
            ]

        _, sp = _start_acquisition(
            start_modbusd, write_redis, prepare_environment, start_modbus_client,
            isolated_shm, instance_id,
            [
                ("MB_AI_1000", 1000, 2, 5),
                ("MB_AI_1002", 1002, 2, 6),
                ("MB_AI_1004", 1004, 2, 10),
            ],
            [
                ("MB_AI_1000", 100000),
                ("MB_AI_1002", 305419896),
                ("MB_AI_1004", 2.5),
            ],
            build, mb_hton=1, mb_swap=1,
        )
        assert _wait_value(sp, 1, 5) == 100000
        assert _wait_value(sp, 2, 6) == 305419896
        _assert_float(_wait_value(sp, 3, 10), 2.5)

    # ── TC7: fun=1 读线圈 — BIT ──────────

    def test_tc7_coil_bit(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC7: fun=1, BIT，value=1。"""
        instance_id = "fun12_tc7"

        def build(port):
            return [
                _make_c4_instance(
                    "采集测试设备", "acq_device", port,
                    [_make_c4_point("pt_a", 1, 0, 1, 15)],
                )
            ]

        _, sp = _start_acquisition(
            start_modbusd, write_redis, prepare_environment, start_modbus_client,
            isolated_shm, instance_id,
            [("MB_COIL_000", 0, 0, 15)],
            [("MB_COIL_000", 1)],
            build,
        )
        assert _wait_value(sp, 1, 15) == 1

    # ── TC8: fun=2 读离散输入 — BOOLEAN ──────────

    def test_tc8_di_boolean(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC8: fun=2, BOOLEAN，value=0。"""
        instance_id = "fun12_tc8"

        def build(port):
            return [
                _make_c4_instance(
                    "采集测试设备", "acq_device", port,
                    [_make_c4_point("pt_a", 1, 0, 2, 0)],
                )
            ]

        _, sp = _start_acquisition(
            start_modbusd, write_redis, prepare_environment, start_modbus_client,
            isolated_shm, instance_id,
            [("MB_DI_000", 0, 1, 0)],
            [("MB_DI_000", 0)],
            build,
        )
        assert _wait_value(sp, 1, 0) == 0

    # ── TC9: fun=4 读输入寄存器 — UINT16 ──────────

    def test_tc9_input_register_uint16(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC9: fun=4, UINT16，value=0xBEEF。"""
        instance_id = "fun12_tc9"

        def build(port):
            return [
                _make_c4_instance(
                    "采集测试设备", "acq_device", port,
                    [_make_c4_point("pt_a", 1, 500, 4, 4)],
                    hton_register=1,
                )
            ]

        _, sp = _start_acquisition(
            start_modbusd, write_redis, prepare_environment, start_modbus_client,
            isolated_shm, instance_id,
            [("MB_IR_500", 500, 3, 4)],
            [("MB_IR_500", 48879)],
            build, mb_hton=1,
        )
        assert _wait_value(sp, 1, 4) == 0xBEEF

    # ── TC10: 相邻 point 采集（连续区间，批处理合并）──────────

    def test_tc10_adjacent_points(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC10: 相邻 FLOAT32 point（addr 1000/1002）触发批处理合并，数据仍正确。"""
        instance_id = "fun12_tc10"

        def build(port):
            return [
                _make_c4_instance(
                    "采集测试设备", "acq_device", port,
                    [
                        _make_c4_point("pt_a", 1, 1000, 3, 10, swap=2),
                        _make_c4_point("pt_b", 1, 1002, 3, 10, swap=2),
                    ],
                    hton_register=1,
                )
            ]

        _, sp = _start_acquisition(
            start_modbusd, write_redis, prepare_environment, start_modbus_client,
            isolated_shm, instance_id,
            [
                ("MB_AI_1000", 1000, 2, 10),
                ("MB_AI_1002", 1002, 2, 10),
            ],
            [("MB_AI_1000", 1.5), ("MB_AI_1002", 2.5)],
            build, mb_hton=1, mb_swap=1,
        )
        _assert_float(_wait_value(sp, 1, 10), 1.5)
        _assert_float(_wait_value(sp, 2, 10), 2.5)

    # ── TC11: 不相邻 point 采集（拆分路径）──────────

    def test_tc11_nonadjacent_points(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC11: 不相邻 UINT16 point（addr 1000/2000）拆分批次，数据仍正确。"""
        instance_id = "fun12_tc11"

        def build(port):
            return [
                _make_c4_instance(
                    "采集测试设备", "acq_device", port,
                    [
                        _make_c4_point("pt_a", 1, 1000, 3, 4),
                        _make_c4_point("pt_b", 1, 2000, 3, 4),
                    ],
                    hton_register=1,
                )
            ]

        _, sp = _start_acquisition(
            start_modbusd, write_redis, prepare_environment, start_modbus_client,
            isolated_shm, instance_id,
            [
                ("MB_PT_1000", 1000, 2, 4),
                ("MB_PT_2000", 2000, 2, 4),
            ],
            [("MB_PT_1000", 100), ("MB_PT_2000", 200)],
            build, mb_hton=1,
        )
        assert _wait_value(sp, 1, 4) == 100
        assert _wait_value(sp, 2, 4) == 200

    # ── TC12: modbusd 停止后重启 — 连接断开重连恢复 ──────────

    def test_tc12_reconnect(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC12: modbusd 停止后 write_seq 停止递增，重启后恢复。"""
        instance_id = "fun12_tc12"
        isolated_shm(instance_id)
        port = _free_port()

        mb_cfg = _make_modbusd_config(
            port, [_make_modbusd_point("MB_PT_001", 1000, 2, 4)], hton_register=1
        )
        mb_path = _write_config_file(mb_cfg)
        proc, _ = start_modbusd(mb_path)
        write_redis("MB_PT_001", 4660)

        c4_insts = [
            _make_c4_instance(
                "采集测试设备", "acq_device", port,
                [_make_c4_point("pt_a", 1, 1000, 3, 4)],
                t1=2, retries=2, hton_register=1,
            )
        ]
        config_path, _ = prepare_environment(_make_c4_config(c4_insts), instance_id)

        sut = start_modbus_client()
        resp = sut.call_tool("start", {"config_path": config_path})
        _assert_mcp_success(resp)

        sp = shm_path(instance_id)

        # 1. 确认 write_seq 递增（轮询进行中）
        seq0 = read_shm_block(sp, 1)["write_seq"]
        wait_write_seq_advanced(sp, 1, seq0)

        # 2. 停止 modbusd → 确认 write_seq 停止递增（连接断开）
        _stop_process(proc)
        time.sleep(0.5)  # 等待客户端检测到连接断开
        seq1 = read_shm_block(sp, 1)["write_seq"]
        time.sleep(0.5)  # 再等一段时间，确认不再递增
        seq1b = read_shm_block(sp, 1)["write_seq"]
        assert seq1b == seq1, (
            f"write_seq still advancing after modbusd stop: {seq1} → {seq1b}"
        )

        # 3. 重启 modbusd（同配置、同端口）
        start_modbusd(mb_path)

        # 4. 确认 write_seq 恢复递增，value 正确（modbusd DAM 需 ≥1 个 modbus.timer 周期刷新）
        wait_write_seq_advanced(sp, 1, seq1b, timeout=15.0)
        deadline = time.monotonic() + 5.0
        value = None
        while time.monotonic() < deadline:
            value = read_shm_value(sp, 1, 4)
            if value == 0x1234:
                break
            time.sleep(0.05)
        assert value == 0x1234, f"value did not recover to 0x1234: {value}"

    # ── TC13: timer 周期轮询 — 多次写入覆盖 ──────────

    def test_tc13_timer_periodic(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC13: timer 周期轮询，改写 Redis 值后 shm value 覆盖为新值。"""
        instance_id = "fun12_tc13"

        def build(port):
            return [
                _make_c4_instance(
                    "采集测试设备", "acq_device", port,
                    [_make_c4_point("pt_a", 1, 1000, 3, 4)],
                    hton_register=1, timer=100,
                )
            ]

        _, sp = _start_acquisition(
            start_modbusd, write_redis, prepare_environment, start_modbus_client,
            isolated_shm, instance_id,
            [("MB_PT_001", 1000, 2, 4)],
            [("MB_PT_001", 100)],
            build, mb_hton=1,
        )

        # seq_1 → seq_2
        seq1 = read_shm_block(sp, 1)["write_seq"]
        wait_write_seq_advanced(sp, 1, seq1)
        seq2 = read_shm_block(sp, 1)["write_seq"]

        # 改写 Redis 值，等待 shm value 覆盖为新值
        write_redis("MB_PT_001", 200)
        deadline = time.monotonic() + 5.0
        value = None
        while time.monotonic() < deadline:
            value = read_shm_value(sp, 1, 4)
            if value == 200:
                break
            time.sleep(0.05)
        seq3 = read_shm_block(sp, 1)["write_seq"]

        assert seq1 < seq2 < seq3, f"write_seq not strictly increasing: {seq1} {seq2} {seq3}"
        assert value == 200, f"value not overwritten: {value}"
