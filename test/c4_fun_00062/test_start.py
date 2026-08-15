"""
C4_FUN_00062 测试用例 — c4_modbus_client start 工具。

验证 c4_modbus_client 在收到 Agent 的 start 工具调用后：
1. 通过 config_path 参数获取配置文件路径并读取 c4_modbus_client 配置段
2. 校验配置有效性（shm_id 合法性、fun/addr/type/swap 合法性、point 区间不重叠）
3. 以 O_RDWR 附加已有共享内存并校验 magic
4. 构建 (uid, fun, addr) → shm_id 映射索引
5. 为每个配置实例启动 goroutine，net.Dial 主动连接 Modbus/TCP 设备（modbusd）
6. 全部实例连接成功才返回 "success"；任一失败则 tear down 并返回 CONNECT_FAILED
7. 各错误码正确返回

严格按 README.md 规格实现，不参考 Go 源码。
"""

import json
import mmap
import os
import struct
import sys
import tempfile
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
    _write_config_file,
)
from shm_helpers import shm_path, read_shm_block  # noqa: E402


# ──────────────────────────────────────────────
#  辅助：搭建设备端 modbusd + redis，返回 port
# ──────────────────────────────────────────────


def _setup_device(start_modbusd, write_redis, port, points, values):
    """启动 modbusd 并写入各 point 的 Redis 值。points: list[(key, modbusaddr, funcode, type)]。"""
    mb_points = [
        _make_modbusd_point(key, modbusaddr, funcode, type_)
        for (key, modbusaddr, funcode, type_) in points
    ]
    mb_cfg = _make_modbusd_config(port, mb_points)
    start_modbusd(_write_config_file(mb_cfg))
    for key, value in values:
        write_redis(key, value)


class TestModbusClientStart:

    # ── TC1: 基本启动 — 单实例单点连接成功 ──────────

    def test_tc1_basic_startup(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC1: 单实例单点，start 返回 success（连接已建立）。"""
        instance_id = "c4_fun62tc1"
        isolated_shm(instance_id)
        port = _free_port()

        _setup_device(
            start_modbusd, write_redis, port,
            [("MB_PT_001", 1000, 2, 4)],
            [("MB_PT_001", 4660)],
        )

        c4_insts = [
            _make_c4_instance(
                "启动测试设备", "test_device", port,
                [_make_c4_point("pt_a", 1, 1000, 3, 4)],
            )
        ]
        config_path, _ = prepare_environment(_make_c4_config(c4_insts), instance_id)

        sut = start_modbus_client()
        resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
        _assert_mcp_success(resp)

    # ── TC2: 多实例启动 — 3 个实例各自连接 ──────────

    def test_tc2_multi_instance(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC2: 3 个实例均指向同一 modbusd，各自 1 个 point，addr 不同，start 成功。"""
        instance_id = "c4_fun62tc2"
        isolated_shm(instance_id)
        port = _free_port()

        _setup_device(
            start_modbusd, write_redis, port,
            [
                ("MB_PT_001", 1000, 2, 4),
                ("MB_PT_002", 1002, 2, 4),
                ("MB_PT_003", 1004, 2, 4),
            ],
            [("MB_PT_001", 1), ("MB_PT_002", 2), ("MB_PT_003", 3)],
        )

        c4_insts = [
            _make_c4_instance("设备1", "dev1", port, [_make_c4_point("pt_a", 1, 1000, 3, 4)]),
            _make_c4_instance("设备2", "dev2", port, [_make_c4_point("pt_a", 1, 1002, 3, 4)]),
            _make_c4_instance("设备3", "dev3", port, [_make_c4_point("pt_a", 1, 1004, 3, 4)]),
        ]
        config_path, _ = prepare_environment(_make_c4_config(c4_insts), instance_id)

        sut = start_modbus_client()
        resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
        _assert_mcp_success(resp)

    # ── TC3: 空实例列表 — 0 个实例 ──────────

    def test_tc3_empty_instances(self, prepare_environment, start_modbus_client, isolated_shm):
        """TC3: c4_modbus_client: []，start 成功（无实例，但仍需 shm_open + magic 校验）。"""
        instance_id = "c4_fun62tc3"
        isolated_shm(instance_id)

        config_path, _ = prepare_environment(_make_c4_config([]), instance_id)

        sut = start_modbus_client()
        resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
        _assert_mcp_success(resp)

    # ── TC4: 重复调用 start → ALREADY_RUNNING ──────────

    def test_tc4_double_start(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC4: 首次 start 成功，再次 start 返回 ALREADY_RUNNING。"""
        instance_id = "c4_fun62tc4"
        isolated_shm(instance_id)
        port = _free_port()

        _setup_device(
            start_modbusd, write_redis, port,
            [("MB_PT_001", 1000, 2, 4)],
            [("MB_PT_001", 4660)],
        )

        c4_insts = [
            _make_c4_instance(
                "启动测试设备", "test_device", port,
                [_make_c4_point("pt_a", 1, 1000, 3, 4)],
            )
        ]
        config_path, _ = prepare_environment(_make_c4_config(c4_insts), instance_id)

        sut = start_modbus_client()
        resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
        _assert_mcp_success(resp)

        resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
        _assert_mcp_error(resp, "ALREADY_RUNNING")

    # ── TC5: start 未调用前调用 stop → 幂等 success ──────────

    def test_tc5_stop_idempotent(self, start_modbus_client):
        """TC5: start 前调用 stop 幂等返回 success。"""
        sut = start_modbus_client()
        resp = sut.call_tool("stop", {})
        _assert_mcp_success(resp)

    # ── TC6: config_path 缺失 → CONFIG_PATH_MISSING ──────────

    def test_tc6_config_path_missing(self, start_modbus_client):
        """TC6: start 提供 instance_id 但不提供 config_path → CONFIG_PATH_MISSING。"""
        sut = start_modbus_client()
        resp = sut.call_tool("start", {"instance_id": "c4_fun62tc6"})
        _assert_mcp_error(resp, "CONFIG_PATH_MISSING")

    # ── TC7: 配置文件格式错误 → CONFIG_PARSE_ERROR ──────────

    @pytest.mark.parametrize(
        "bad_config_content",
        [
            "{invalid json\n",
            '{"c4_shm_manager": {"writer": [], "reader": []}}',
        ],
    )
    def test_tc7_config_parse_error(
        self, shm_mgr_client, start_modbus_client, isolated_shm, bad_config_content,
    ):
        """TC7: 格式错误的配置文件 → CONFIG_PARSE_ERROR。

        子场景:
        (a) JSON 语法错误
        (b) 合法 JSON 但缺少 c4_modbus_client 段
        """
        instance_id = f"c4_fun62tc7{abs(hash(bad_config_content)) % 100000}"
        isolated_shm(instance_id)

        # 先创建共享内存（无配置文件 → 默认 10 万点）
        resp = shm_mgr_client.call_tool("create_shm", {"instance_id": instance_id})
        assert resp["result"].get("isError", False) is False, (
            f"create_shm failed for TC7: {resp}"
        )

        fd, bad_config_path = tempfile.mkstemp(
            suffix=".json", prefix="c4_config_bad_"
        )
        with os.fdopen(fd, "w") as f:
            f.write(bad_config_content)

        try:
            sut = start_modbus_client()
            resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": bad_config_path})
            _assert_mcp_error(resp, "CONFIG_PARSE_ERROR")
        finally:
            os.unlink(bad_config_path)

    # ── TC8: 共享内存不存在 → SHM_OPEN_FAILED ──────────

    def test_tc8_shm_open_failed(
        self, start_modbusd, write_redis, start_modbus_client, isolated_shm,
    ):
        """TC8: 不创建共享内存，手工 shm_id=1 → SHM_OPEN_FAILED。"""
        instance_id = "c4_fun62tc8"
        isolated_shm(instance_id)
        port = _free_port()

        _setup_device(
            start_modbusd, write_redis, port,
            [("MB_PT_001", 1000, 2, 4)],
            [("MB_PT_001", 4660)],
        )

        # 不创建 shm，手工将 shm_id 置为 1（模拟 adjust_shm 已回填）
        c4_insts = [
            _make_c4_instance(
                "启动测试设备", "test_device", port,
                [_make_c4_point("pt_a", 1, 1000, 3, 4, shm_id=1)],
            )
        ]
        config_path = _write_config_file(_make_c4_config(c4_insts))

        try:
            sut = start_modbus_client()
            resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
            _assert_mcp_error(resp, "SHM_OPEN_FAILED")
        finally:
            os.unlink(config_path)

    # ── TC9: 共享内存 magic 损坏 → SHM_CORRUPTED ──────────

    def test_tc9_shm_corrupted(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC9: 修改 Header magic 为 0xDEADBEEF → SHM_CORRUPTED。"""
        instance_id = "c4_fun62tc9"
        isolated_shm(instance_id)
        port = _free_port()

        _setup_device(
            start_modbusd, write_redis, port,
            [("MB_PT_001", 1000, 2, 4)],
            [("MB_PT_001", 4660)],
        )

        c4_insts = [
            _make_c4_instance(
                "启动测试设备", "test_device", port,
                [_make_c4_point("pt_a", 1, 1000, 3, 4)],
            )
        ]
        config_path, _ = prepare_environment(_make_c4_config(c4_insts), instance_id)

        # 损坏共享内存 Header magic 字段
        path = shm_path(instance_id)
        fd = os.open(path, os.O_RDWR)
        try:
            buf = mmap.mmap(fd, 4, mmap.MAP_SHARED, mmap.PROT_WRITE)
            buf[0:4] = struct.pack(">I", 0xDEADBEEF)
            buf.close()
        finally:
            os.close(fd)

        sut = start_modbus_client()
        resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
        _assert_mcp_error(resp, "SHM_CORRUPTED")

    # ── TC10: shm_id 未分配（=0）→ SHM_ID_NOT_ASSIGNED ──────────

    def test_tc10_shm_id_not_assigned(
        self, start_modbusd, write_redis, shm_mgr_client,
        start_modbus_client, isolated_shm,
    ):
        """TC10: shm 存在但配置 shm_id=0 → SHM_ID_NOT_ASSIGNED。

        说明：README 规格为「create_shm 但跳过 adjust_shm → shm_id 仍为 0」。
        实测 c4_shm_manager.create_shm(instance_id, config_path) 会回填 shm_id（0→1），故改用
        create_shm（带 instance_id 不带 config_path → 默认 10 万点、不回填）+ 单独写 shm_id=0 的配置，
        以构造「shm 存在 + shm_id=0」的前提。
        """
        instance_id = "c4_fun62tc10"
        isolated_shm(instance_id)
        port = _free_port()

        _setup_device(
            start_modbusd, write_redis, port,
            [("MB_PT_001", 1000, 2, 4)],
            [("MB_PT_001", 4660)],
        )

        # 创建共享内存（不传 config_path → 默认 10 万点，不回填 shm_id）
        resp = shm_mgr_client.call_tool("create_shm", {"instance_id": instance_id})
        assert resp["result"].get("isError", False) is False, (
            f"create_shm failed for TC10: {resp}"
        )

        # 配置中 shm_id 保持 0（未分配）
        c4_insts = [
            _make_c4_instance(
                "启动测试设备", "test_device", port,
                [_make_c4_point("pt_a", 1, 1000, 3, 4, shm_id=0)],
            )
        ]
        config_path = _write_config_file(_make_c4_config(c4_insts))

        try:
            sut = start_modbus_client()
            resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
            _assert_mcp_error(resp, "SHM_ID_NOT_ASSIGNED")
        finally:
            os.unlink(config_path)

    # ── TC11: point 字段非法 → INVALID_POINT ──────────

    @pytest.mark.parametrize(
        "points,desc",
        [
            # (a) fun 非法
            ([_make_c4_point("pt_a", 1, 1000, 6, 4)], "fun=6"),
            # (b) type 非法：INT8 不适用于寄存器
            ([_make_c4_point("pt_a", 1, 1000, 3, 1)], "type=1"),
            # (c) swap 非法：swap=3 不整除 count=4
            ([_make_c4_point("pt_a", 1, 1000, 3, 10, swap=3)], "swap=3"),
            # (d) 重复 (uid,fun,addr)
            (
                [
                    _make_c4_point("pt_a", 1, 1000, 3, 4),
                    _make_c4_point("pt_b", 1, 1000, 3, 4),
                ],
                "duplicate (uid,fun,addr)",
            ),
            # (e) point 区间重叠：type=10(span=2) addr=1000 与 addr=1001
            (
                [
                    _make_c4_point("pt_a", 1, 1000, 3, 10, swap=2),
                    _make_c4_point("pt_b", 1, 1001, 3, 10, swap=2),
                ],
                "overlap",
            ),
        ],
    )
    def test_tc11_invalid_point(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm, points, desc,
    ):
        """TC11: point 字段非法 → INVALID_POINT。"""
        instance_id = f"c4_fun62tc11{abs(hash(desc)) % 100000}"
        isolated_shm(instance_id)
        port = _free_port()

        _setup_device(
            start_modbusd, write_redis, port,
            [("MB_PT_001", 1000, 2, 4)],
            [("MB_PT_001", 4660)],
        )

        c4_insts = [
            _make_c4_instance("启动测试设备", "test_device", port, points)
        ]
        config_path, _ = prepare_environment(_make_c4_config(c4_insts), instance_id)

        sut = start_modbus_client()
        resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
        _assert_mcp_error(resp, "INVALID_POINT")

    # ── TC12: 设备不可达 → CONNECT_FAILED（tear down）──────────

    def test_tc12_connect_failed(
        self, start_modbusd, write_redis, prepare_environment,
        start_modbus_client, isolated_shm,
    ):
        """TC12: 实例 1 可达、实例 2 不可达 → CONNECT_FAILED，实例 1 被 tear down。"""
        instance_id = "c4_fun62tc12"
        isolated_shm(instance_id)
        port = _free_port()
        unreachable_port = _free_port()  # 无监听

        _setup_device(
            start_modbusd, write_redis, port,
            [("MB_PT_001", 1000, 2, 4)],
            [("MB_PT_001", 4660)],
        )

        c4_insts = [
            _make_c4_instance(
                "实例1", "dev1", port,
                [_make_c4_point("pt_a", 1, 1000, 3, 4)],
            ),
            _make_c4_instance(
                "实例2", "dev2", unreachable_port,
                [_make_c4_point("pt_b", 1, 1000, 3, 4)],
            ),
        ]
        config_path, _ = prepare_environment(_make_c4_config(c4_insts), instance_id)

        sut = start_modbus_client()
        resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
        _assert_mcp_error(resp, "CONNECT_FAILED")

        # tear-down 验证：实例 1（shm_id=1）的 goroutine 已被 tear down，write_seq 不再递增
        sp = shm_path(instance_id)
        seq_before = read_shm_block(sp, 1)["write_seq"]
        time.sleep(0.3)  # 3 个轮询周期（timer=100ms）
        seq_after = read_shm_block(sp, 1)["write_seq"]
        assert seq_after == seq_before, (
            f"instance1 write_seq advanced after tear-down: {seq_before} → {seq_after}"
        )

        # 随后 stop 幂等返回 success
        resp = sut.call_tool("stop", {})
        _assert_mcp_success(resp)
