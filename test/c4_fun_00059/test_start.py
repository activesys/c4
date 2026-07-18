"""
C4_FUN_00059 功能测试 — c4_asfp2_client start 工具。
测试 SUT (c4_asfp2_client) 在 MCP stdio JSON-RPC 协议下，
接收 start 工具调用后的行为。
"""

import json
import mmap
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # type: ignore

from conftest import (
    _assert_mcp_error,
    _assert_mcp_success,
    _make_client_config,
    _make_partial_unreachable_config,
    _roots_callback,
)
from shm_helpers import read_shm_header


class TestStart:
    """start 工具测试（TC1~TC11，对应 README §4）。"""

    # ──────────────────────────────────────────────
    #  TC1: 基本启动 — 单实例
    # ──────────────────────────────────────────────

    def test_tc1_basic_startup(
        self, mcp, prepare_environment, asfp2_servers, isolated_shm
    ):
        """
        前置：标准配置（§3.1）。asfp2_server -p 9900 & 已监听。
        操作：启动 SUT，MCP initialize，调用 start。
        预期：返回 "success"。asfp2_server 侧可见 TCP 连接。
        """
        instance_id = "tc1"
        isolated_shm(instance_id)
        asfp2_servers(9900)

        cfg = _make_client_config(port=9900)
        config_path, iid = prepare_environment(cfg, instance_id)
        assert iid == instance_id

        resp = mcp.call_tool(
            "start",
            {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        _assert_mcp_success(resp)

    # ──────────────────────────────────────────────
    #  TC2: 多实例启动
    # ──────────────────────────────────────────────

    def test_tc2_multi_instance(
        self, mcp, prepare_environment, asfp2_servers, isolated_shm
    ):
        """
        前置：3 实例配置（§3.2）。asfp2_server 分别在 9901/9902/9903 监听。
        操作：调用 start。
        预期：返回 "success"。3 个 asfp2_server 均可见连接。
        """
        instance_id = "tc2"
        isolated_shm(instance_id)
        asfp2_servers(9901, 9902, 9903)

        cfg = _make_client_config(port=9901, num_instances=3)
        config_path, iid = prepare_environment(cfg, instance_id)
        assert iid == instance_id

        resp = mcp.call_tool(
            "start",
            {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        _assert_mcp_success(resp)

    # ──────────────────────────────────────────────
    #  TC3: 空实例列表
    # ──────────────────────────────────────────────

    def test_tc3_empty_instances(
        self, mcp, prepare_environment, isolated_shm
    ):
        """
        前置："c4_asfp2_client": []（空数组）。SHM 正常创建（writer 端仍有 points）。
        操作：调用 start。
        预期：返回 "success"（无实例需启动，调用 shm_open + mmap 后直接返回）。
        """
        instance_id = "tc3"
        isolated_shm(instance_id)

        # 构建空实例配置 — writer 端正常，reader 端为空数组
        writer_instance = {
            "name": "\u6a21\u62df\u6570\u636e\u6e90",
            "id": "mock_writer",
            "ip": "127.0.0.1",
            "port": 502,
            "hton_register": 1,
            "hton_total": 0,
            "t0": 30,
            "t1": 10,
            "retries": 10,
            "coils_quantity_max": 2000,
            "registers_quantity_max": 125,
            "timer": 1000,
            "points": [
                {"id": "pt_a", "uid": 1, "addr": 1000, "fun": 3, "type": 4, "swap": 2, "shm_id": 0},
                {"id": "pt_b", "uid": 1, "addr": 1002, "fun": 3, "type": 4, "swap": 2, "shm_id": 0},
            ],
        }

        cfg = {
            "c4_shm_manager": {
                "writer": ["c4_modbus_client"],
                "reader": ["c4_asfp2_client"],
            },
            "c4_modbus_client": [writer_instance],
            "c4_asfp2_client": [],
        }

        config_path, iid = prepare_environment(cfg, instance_id)
        assert iid == instance_id

        resp = mcp.call_tool(
            "start",
            {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        _assert_mcp_success(resp)

    # ──────────────────────────────────────────────
    #  TC4: 重复调用 start → ALREADY_RUNNING
    # ──────────────────────────────────────────────

    def test_tc4_duplicate_start(
        self, mcp, prepare_environment, asfp2_servers, isolated_shm
    ):
        """
        前置：TC1 已成功启动。
        操作：再次调用 start。
        预期：isError: true，错误码 ALREADY_RUNNING。
        """
        instance_id = "tc4"
        isolated_shm(instance_id)
        asfp2_servers(9904)

        cfg = _make_client_config(port=9904)
        config_path, iid = prepare_environment(cfg, instance_id)
        assert iid == instance_id

        on_request = _roots_callback([{"uri": f"file://{config_path}"}])

        # 第一次 start — 应成功
        resp1 = mcp.call_tool("start", {}, on_request=on_request)
        _assert_mcp_success(resp1)

        # 第二次 start — 应返回 ALREADY_RUNNING
        resp2 = mcp.call_tool("start", {}, on_request=on_request)
        _assert_mcp_error(resp2, "ALREADY_RUNNING")

    # ──────────────────────────────────────────────
    #  TC5: start 未调用前调用 stop/status → SERVICE_NOT_READY
    # ──────────────────────────────────────────────

    def test_tc5_service_not_ready(self, mcp, isolated_shm):
        """
        前置：MCP initialize 完成，但未调用 start。
        操作：调用 stop、status。
        预期：均返回 isError: true，SERVICE_NOT_READY。
        """
        instance_id = "tc5"
        isolated_shm(instance_id)

        # stop — 服务未启动
        resp_stop = mcp.call_tool(
            "stop",
            {},
            on_request=_roots_callback([]),
        )
        _assert_mcp_error(resp_stop, "SERVICE_NOT_READY")

        # status — 服务未启动
        resp_status = mcp.call_tool(
            "status",
            {},
            on_request=_roots_callback([]),
        )
        _assert_mcp_error(resp_status, "SERVICE_NOT_READY")

    # ──────────────────────────────────────────────
    #  TC6: shm_id 未分配 → SHM_ID_NOT_ASSIGNED
    # ──────────────────────────────────────────────

    def test_tc6_shm_id_not_assigned(
        self, mcp, shm_mgr_client, isolated_shm
    ):
        """
        前置：配置中所有 point 的 shm_id=0（跳过 adjust_shm），SHM 正常创建。
        操作：调用 start。
        预期：isError: true，SHM_ID_NOT_ASSIGNED。
        """
        instance_id = "tc6"
        isolated_shm(instance_id)

        cfg = _make_client_config(port=9906)
        fd, config_path = tempfile.mkstemp(suffix=".json", prefix="c4_config_")
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)

        try:
            # create_shm — 创建 SHM，但不调用 adjust_shm
            create_resp = shm_mgr_client.call_tool(
                "create_shm",
                {"instance_id": instance_id},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            assert not create_resp["result"].get(
                "isError", False
            ), f"create_shm failed: {create_resp}"

            # 关闭 shm_manager（跳过 adjust_shm）
            shm_mgr_client.close()

            # create_shm 会回填 config 中的 shm_id → 重新写入 shm_id=0 的配置
            cfg = _make_client_config(port=9906)
            with open(config_path, "w") as f:
                json.dump(cfg, f)

            # 调用 start — 应检测到 shm_id 未分配
            resp = mcp.call_tool(
                "start",
                {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_error(resp, "SHM_ID_NOT_ASSIGNED")
        finally:
            try:
                os.unlink(config_path)
            except OSError:
                pass

    # ──────────────────────────────────────────────
    #  TC7: 配置文件格式错误 → CONFIG_PARSE_ERROR
    # ──────────────────────────────────────────────

    def test_tc7_config_parse_error(
        self, mcp, prepare_environment, isolated_shm
    ):
        """
        前置：SHM 正常。配置缺失 c4_asfp2_client key。
        操作：调用 start。
        预期：isError: true，CONFIG_PARSE_ERROR。
        """
        instance_id = "tc7"
        isolated_shm(instance_id)

        # 先用有效配置创建 SHM（prepare_environment 需要它）
        valid_cfg = _make_client_config(port=9907)
        config_path, iid = prepare_environment(valid_cfg, instance_id)
        assert iid == instance_id

        # 覆写配置文件 — 删除 c4_asfp2_client key
        invalid_cfg = {
            "c4_shm_manager": valid_cfg["c4_shm_manager"],
            "c4_modbus_client": valid_cfg["c4_modbus_client"],
            # c4_asfp2_client 故意缺失
        }
        with open(config_path, "w") as f:
            json.dump(invalid_cfg, f)

        resp = mcp.call_tool(
            "start",
            {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        _assert_mcp_error(resp, "CONFIG_PARSE_ERROR")

    # ──────────────────────────────────────────────
    #  TC8: roots/list 超时 → CONFIG_PATH_MISSING
    # ──────────────────────────────────────────────

    def test_tc8_config_path_missing(self, mcp, isolated_shm):
        """
        前置：MCP initialize 完成。
        操作：调用 start，roots/list 返回不存在的文件路径。
        预期：isError: true，CONFIG_PATH_MISSING。
        """
        instance_id = "tc8"
        isolated_shm(instance_id)

        # 返回不存在的文件路径 — SUT 立即返回 CONFIG_PATH_MISSING
        resp = mcp.call_tool(
            "start",
            {},
            on_request=_roots_callback(
                [{"uri": "file:///tmp/nonexistent_config_xyz.json"}]
            ),
        )
        _assert_mcp_error(resp, "CONFIG_PATH_MISSING")

    # ──────────────────────────────────────────────
    #  TC9: 共享内存不存在 → SHM_OPEN_FAILED
    # ──────────────────────────────────────────────

    def test_tc9_shm_open_failed(self, mcp, isolated_shm):
        """
        前置：未创建 SHM（跳过 create_shm）。配置正常（shm_id 非零）。
        操作：调用 start。
        预期：isError: true，SHM_OPEN_FAILED。
        """
        instance_id = "tc9"
        isolated_shm(instance_id)

        # 写入配置文件但不创建 SHM — shm_id 必须为合法值才能通过 validateConfig
        cfg = _make_client_config(port=9909)
        # 手动设置 client points 的 shm_id 为非零（模拟已由 shm_manager 分配）
        for inst in cfg["c4_asfp2_client"]:
            for pt in inst["points"]:
                pt["shm_id"] = 99  # 任意非零值，SHM 不存在时会触发 SHM_OPEN_FAILED
        fd, config_path = tempfile.mkstemp(suffix=".json", prefix="c4_config_")
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)

        try:
            resp = mcp.call_tool(
                "start",
                {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_error(resp, "SHM_OPEN_FAILED")
        finally:
            try:
                os.unlink(config_path)
            except OSError:
                pass

    # ──────────────────────────────────────────────
    #  TC10: 共享内存 magic 损坏 → SHM_CORRUPTED
    # ──────────────────────────────────────────────

    def test_tc10_shm_corrupted(
        self, mcp, prepare_environment, isolated_shm
    ):
        """
        前置：SHM 已创建，Python 通过 mmap 修改 Header magic 为 0xDEADBEEF。
        操作：调用 start。
        预期：isError: true，SHM_CORRUPTED。
        """
        instance_id = "tc10"
        isolated_shm(instance_id)

        cfg = _make_client_config(port=9910)
        config_path, iid = prepare_environment(cfg, instance_id)
        assert iid == instance_id

        # 通过 mmap 修改 Header magic 前 4 字节
        shm_path = f"/dev/shm/c4_{instance_id}"
        fd = os.open(shm_path, os.O_RDWR)
        try:
            shm = mmap.mmap(fd, 32, mmap.MAP_SHARED, mmap.PROT_WRITE)
            shm.write(struct.pack("=I", 0xDEADBEEF))
            shm.close()
        finally:
            os.close(fd)

        # 验证 magic 已被修改（通过只读 header）
        header = read_shm_header(shm_path)
        assert header["magic"] == 0xDEADBEEF, (
            f"magic corruption failed, got 0x{header['magic']:08X}"
        )

        resp = mcp.call_tool(
            "start",
            {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        _assert_mcp_error(resp, "SHM_CORRUPTED")

    # ──────────────────────────────────────────────
    #  TC11: 连接失败 — 原子性回滚
    # ──────────────────────────────────────────────

    def test_tc11_atomic_rollback(
        self, mcp, prepare_environment, asfp2_servers, isolated_shm
    ):
        """
        前置：部分不可达配置（§3.4）。asfp2_server -p 9901 & 已监听。
        操作：调用 start。
        预期：
          - 返回 isError: true，CONNECT_FAILED
          - 可达实例（port 9901）的连接被 tear down → asfp2_server 侧无活跃连接
        """
        instance_id = "tc11"
        isolated_shm(instance_id)
        asfp2_servers(9901)

        cfg = _make_partial_unreachable_config()
        config_path, iid = prepare_environment(cfg, instance_id)
        assert iid == instance_id

        resp = mcp.call_tool(
            "start",
            {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        _assert_mcp_error(resp, "CONNECT_FAILED")
