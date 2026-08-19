"""
C4_FUN_00065 测试用例 — c4_iec104_client start 工具。

验证 c4_iec104_client 在收到 Agent 的 start 工具调用后：
1. 通过 config_path 参数获取配置文件路径并读取 c4_iec104_client 配置段
2. 校验配置有效性（shm_id 合法性、addr 合法性、addr 实例内唯一、t2<t1、
   modules=32768、ioa_size ∈ {1,2,3}）
3. 以 O_RDWR 附加已有共享内存并校验 magic
4. 构建 (instance, ioa) → shm_id 映射索引
5. 为每个配置实例启动 goroutine，异步发起连接和 STARTDT 激活
6. start 不等待连接/握手——所有实例均已启动即返回 "success"
7. 各错误码正确返回

严格按 README.md 规格实现，不参考 Go 源码。
"""

import mmap
import os
import struct
import sys
import tempfile

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
    _write_config_file,
)
from shm_helpers import shm_path  # noqa: E402


# ──────────────────────────────────────────────
#  辅助：搭建设备端 iec104d + redis，返回 port
# ──────────────────────────────────────────────


def _setup_device(start_iec104d, write_redis, port, points, values,
                  with_cp56time2a=0):
    """启动 iec104d 并写入各 point 的 Redis 值。points: list[(key, 104addr)]。"""
    iec_points = [_make_iec104d_point(key, addr) for (key, addr) in points]
    iec_cfg = _make_iec104d_config(port, iec_points, with_cp56time2a=with_cp56time2a)
    start_iec104d(_write_config_file(iec_cfg))
    for key, value in values:
        write_redis(key, value)


class TestIec104ClientStart:

    # ── TC1: 基本启动 — 单实例单点 ──────────

    def test_tc1_basic_startup(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm,
    ):
        """TC1: 单实例单点，start 返回 success（不等待连接）。"""
        instance_id = "c4_fun65tc1"
        isolated_shm(instance_id)
        port = _free_port()

        _setup_device(
            start_iec104d, write_redis, port,
            [("TF_TEST_AI001", 16385)],
            [("TF_TEST_AI001", 1.5)],
        )

        c4_insts = [
            _make_c4_instance(
                "启动测试主变", "test_transformer", port,
                [_make_c4_point("pt_a", 16385)],
            )
        ]
        config_path, _ = prepare_environment(_make_c4_config(c4_insts), instance_id)

        sut = start_iec104_client()
        resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
        _assert_mcp_success(resp)

    # ── TC2: 多实例启动 — 3 个实例 ──────────

    def test_tc2_multi_instance(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm,
    ):
        """TC2: 1 个 iec104d 3 point，3 个 c4 实例各 1 point，start 成功。"""
        instance_id = "c4_fun65tc2"
        isolated_shm(instance_id)
        port = _free_port()

        _setup_device(
            start_iec104d, write_redis, port,
            [
                ("TF_TEST_AI001", 16385),
                ("TF_TEST_AI002", 16386),
                ("TF_TEST_AI003", 16387),
            ],
            [
                ("TF_TEST_AI001", 1.0),
                ("TF_TEST_AI002", 2.0),
                ("TF_TEST_AI003", 3.0),
            ],
        )

        c4_insts = [
            _make_c4_instance("设备1", "dev1", port, [_make_c4_point("pt_a", 16385)]),
            _make_c4_instance("设备2", "dev2", port, [_make_c4_point("pt_a", 16386)]),
            _make_c4_instance("设备3", "dev3", port, [_make_c4_point("pt_a", 16387)]),
        ]
        config_path, _ = prepare_environment(_make_c4_config(c4_insts), instance_id)

        sut = start_iec104_client()
        resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
        _assert_mcp_success(resp)

    # ── TC3: 空实例列表 — 0 个实例 ──────────

    def test_tc3_empty_instances(self, prepare_environment, start_iec104_client, isolated_shm):
        """TC3: c4_iec104_client: []，start 成功（无实例，但仍需 shm_open + magic 校验）。"""
        instance_id = "c4_fun65tc3"
        isolated_shm(instance_id)

        config_path, _ = prepare_environment(_make_c4_config([]), instance_id)

        sut = start_iec104_client()
        resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
        _assert_mcp_success(resp)

    # ── TC4: 重复调用 start → ALREADY_RUNNING ──────────

    def test_tc4_double_start(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm,
    ):
        """TC4: 首次 start 成功，再次 start 返回 ALREADY_RUNNING。"""
        instance_id = "c4_fun65tc4"
        isolated_shm(instance_id)
        port = _free_port()

        _setup_device(
            start_iec104d, write_redis, port,
            [("TF_TEST_AI001", 16385)],
            [("TF_TEST_AI001", 1.5)],
        )

        c4_insts = [
            _make_c4_instance(
                "启动测试主变", "test_transformer", port,
                [_make_c4_point("pt_a", 16385)],
            )
        ]
        config_path, _ = prepare_environment(_make_c4_config(c4_insts), instance_id)

        sut = start_iec104_client()
        resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
        _assert_mcp_success(resp)

        resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
        _assert_mcp_error(resp, "ALREADY_RUNNING")

    # ── TC5: start 未调用前调用 stop → 幂等 success ──────────

    def test_tc5_stop_idempotent(self, start_iec104_client):
        """TC5: start 前调用 stop 幂等返回 success。"""
        sut = start_iec104_client()
        resp = sut.call_tool("stop", {})
        _assert_mcp_success(resp)

    # ── TC6: config_path 缺失 → CONFIG_PATH_MISSING ──────────

    def test_tc6_config_path_missing(self, start_iec104_client):
        """TC6: start 提供 instance_id 但不提供 config_path → CONFIG_PATH_MISSING。"""
        sut = start_iec104_client()
        resp = sut.call_tool("start", {"instance_id": "c4_fun65tc6"})
        _assert_mcp_error(resp, "CONFIG_PATH_MISSING")

    # ── TC7: 配置文件格式错误 → CONFIG_PARSE_ERROR ──────────

    @pytest.mark.parametrize(
        "bad_config_content",
        [
            "{invalid json\n",
            '{"c4_shm_manager": {"writer": ["c4_iec104_client"], "reader": ["c4_asfp2_client"]}}',
        ],
    )
    def test_tc7_config_parse_error(
        self, shm_mgr_client, start_iec104_client, isolated_shm, bad_config_content,
    ):
        """TC7: 格式错误的配置文件 → CONFIG_PARSE_ERROR。

        子场景:
        (a) JSON 语法错误
        (b) 合法 JSON 但缺少 c4_iec104_client 段
        """
        instance_id = f"c4_fun65tc7{abs(hash(bad_config_content)) % 100000}"
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
            sut = start_iec104_client()
            resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": bad_config_path})
            _assert_mcp_error(resp, "CONFIG_PARSE_ERROR")
        finally:
            os.unlink(bad_config_path)

    # ── TC8: 共享内存不存在 → SHM_OPEN_FAILED ──────────

    def test_tc8_shm_open_failed(
        self, start_iec104d, write_redis, start_iec104_client, isolated_shm,
    ):
        """TC8: 不创建共享内存，手工 shm_id=1 → SHM_OPEN_FAILED。"""
        instance_id = "c4_fun65tc8"
        isolated_shm(instance_id)
        port = _free_port()

        _setup_device(
            start_iec104d, write_redis, port,
            [("TF_TEST_AI001", 16385)],
            [("TF_TEST_AI001", 1.5)],
        )

        # 不创建 shm，手工将 shm_id 置为 1（模拟 adjust_shm 已回填）
        c4_insts = [
            _make_c4_instance(
                "启动测试主变", "test_transformer", port,
                [_make_c4_point("pt_a", 16385, shm_id=1)],
            )
        ]
        config_path = _write_config_file(_make_c4_config(c4_insts))

        try:
            sut = start_iec104_client()
            resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
            _assert_mcp_error(resp, "SHM_OPEN_FAILED")
        finally:
            os.unlink(config_path)

    # ── TC9: 共享内存 magic 损坏 → SHM_CORRUPTED ──────────

    def test_tc9_shm_corrupted(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm,
    ):
        """TC9: 修改 Header magic 为 0xDEADBEEF → SHM_CORRUPTED。"""
        instance_id = "c4_fun65tc9"
        isolated_shm(instance_id)
        port = _free_port()

        _setup_device(
            start_iec104d, write_redis, port,
            [("TF_TEST_AI001", 16385)],
            [("TF_TEST_AI001", 1.5)],
        )

        c4_insts = [
            _make_c4_instance(
                "启动测试主变", "test_transformer", port,
                [_make_c4_point("pt_a", 16385)],
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

        sut = start_iec104_client()
        resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
        _assert_mcp_error(resp, "SHM_CORRUPTED")

    # ── TC10: shm_id 未分配（=0）→ SHM_ID_NOT_ASSIGNED ──────────

    def test_tc10_shm_id_not_assigned(
        self, start_iec104d, write_redis, shm_mgr_client,
        start_iec104_client, isolated_shm,
    ):
        """TC10: shm 存在但配置 shm_id=0 → SHM_ID_NOT_ASSIGNED。

        说明：README 规格为「create_shm 但跳过 adjust_shm → shm_id 仍为 0」。
        c4_shm_manager.create_shm(instance_id, config_path) 会回填 shm_id（0→1），故改用
        create_shm（带 instance_id 不带 config_path → 默认 10 万点、不回填）+ 单独写 shm_id=0 的配置，
        以构造「shm 存在 + shm_id=0」的前提。
        """
        instance_id = "c4_fun65tc10"
        isolated_shm(instance_id)
        port = _free_port()

        _setup_device(
            start_iec104d, write_redis, port,
            [("TF_TEST_AI001", 16385)],
            [("TF_TEST_AI001", 1.5)],
        )

        # 创建共享内存（不传 config_path → 默认 10 万点，不回填 shm_id）
        resp = shm_mgr_client.call_tool("create_shm", {"instance_id": instance_id})
        assert resp["result"].get("isError", False) is False, (
            f"create_shm failed for TC10: {resp}"
        )

        # 配置中 shm_id 保持 0（未分配）
        c4_insts = [
            _make_c4_instance(
                "启动测试主变", "test_transformer", port,
                [_make_c4_point("pt_a", 16385, shm_id=0)],
            )
        ]
        config_path = _write_config_file(_make_c4_config(c4_insts))

        try:
            sut = start_iec104_client()
            resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
            _assert_mcp_error(resp, "SHM_ID_NOT_ASSIGNED")
        finally:
            os.unlink(config_path)

    # ── TC11: 实例级字段非法 → INVALID_CONFIG ──────────

    @pytest.mark.parametrize(
        "instance_kwargs,desc",
        [
            ({"t1": 5, "t2": 5}, "t2 >= t1"),
            ({"ioa_size": 4}, "ioa_size=4"),
            ({"modules": 1000}, "modules=1000"),
        ],
    )
    def test_tc11_invalid_config(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm, instance_kwargs, desc,
    ):
        """TC11: 实例级字段非法 → INVALID_CONFIG。

        子场景:
        (a) t2 >= t1（违反 t2 < t1）
        (b) ioa_size 超出 1/2/3
        (c) modules 非 32768
        """
        instance_id = f"c4_fun65tc11{abs(hash(desc)) % 100000}"
        isolated_shm(instance_id)
        port = _free_port()

        _setup_device(
            start_iec104d, write_redis, port,
            [("TF_TEST_AI001", 16385)],
            [("TF_TEST_AI001", 1.5)],
        )

        c4_insts = [
            _make_c4_instance(
                "启动测试主变", "test_transformer", port,
                [_make_c4_point("pt_a", 16385)],
                **instance_kwargs,
            )
        ]
        config_path, _ = prepare_environment(_make_c4_config(c4_insts), instance_id)

        sut = start_iec104_client()
        resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
        _assert_mcp_error(resp, "INVALID_CONFIG")

    # ── TC12: point 字段非法 → INVALID_POINT ──────────

    @pytest.mark.parametrize(
        "points,desc",
        [
            # (a) addr 越界：ioa_size=3 时 addr=16777216 超出 0~16777215
            ([_make_c4_point("pt_a", 16777216)], "addr out of range"),
            # (b) addr 实例内重复
            (
                [_make_c4_point("pt_a", 16385), _make_c4_point("pt_b", 16385)],
                "duplicate addr",
            ),
        ],
    )
    def test_tc12_invalid_point(
        self, start_iec104d, write_redis, prepare_environment,
        start_iec104_client, isolated_shm, points, desc,
    ):
        """TC12: point 字段非法 → INVALID_POINT。"""
        instance_id = f"c4_fun65tc12{abs(hash(desc)) % 100000}"
        isolated_shm(instance_id)
        port = _free_port()

        _setup_device(
            start_iec104d, write_redis, port,
            [("TF_TEST_AI001", 16385)],
            [("TF_TEST_AI001", 1.5)],
        )

        c4_insts = [
            _make_c4_instance("启动测试主变", "test_transformer", port, points)
        ]
        config_path, _ = prepare_environment(_make_c4_config(c4_insts), instance_id)

        sut = start_iec104_client()
        resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
        _assert_mcp_error(resp, "INVALID_POINT")

    # ── TC13: 设备不可达 — start 仍返回 success（不等待连接）──────────

    def test_tc13_unreachable_device(
        self, prepare_environment, start_iec104_client, isolated_shm,
    ):
        """TC13: 配置指向无 iec104d 监听的端口，start 仍返回 success（不等待连接）。"""
        instance_id = "c4_fun65tc13"
        isolated_shm(instance_id)
        unreachable_port = _free_port()  # 无 iec104d 监听

        c4_insts = [
            _make_c4_instance(
                "启动测试主变", "test_transformer", unreachable_port,
                [_make_c4_point("pt_a", 16385)],
            )
        ]
        config_path, _ = prepare_environment(_make_c4_config(c4_insts), instance_id)

        sut = start_iec104_client()
        resp = sut.call_tool("start", {"instance_id": instance_id, "config_path": config_path})
        _assert_mcp_success(resp)
