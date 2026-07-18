"""
C4_FUN_00057 测试用例 — c4_asfp2_server start 工具

验证 c4_asfp2_server 在收到 Agent 的 start 工具调用后：
1. 通过 roots/list 获取配置文件路径
2. 读取并校验配置
3. 附加已有共享内存
4. 为每个配置实例启动 goroutine 监听对应端口
5. 返回正确的结果
"""

import json
import mmap
import os
import socket
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # type: ignore
from conftest import _roots_callback
from shm_helpers import shm_path, shm_unlink


# ──────────────────────────────────────────────
#  辅助函数
# ──────────────────────────────────────────────


def _port_is_listening(port, timeout=1):
    """检查本地端口是否已被监听。连接成功返回 True。"""
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        s.close()
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def _assert_mcp_error(resp, expected_prefix):
    """验证 MCP 响应为业务错误且错误消息以前缀开始。"""
    assert resp["result"]["isError"] is True, (
        f"Expected isError=true, got: {resp}"
    )
    text = resp["result"]["content"][0]["text"]
    assert text.startswith(expected_prefix), (
        f"Expected prefix '{expected_prefix}', got '{text}'"
    )


def _make_config_file(config_dict):
    """将配置 dict 写入临时 JSON 文件，返回路径。调用方负责清理。"""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="c4_config_", text=True)
    with os.fdopen(fd, "w") as f:
        json.dump(config_dict, f)
    return path


# ──────────────────────────────────────────────
#  配置工厂
# ──────────────────────────────────────────────


def _make_single_instance_config(instance_id, port, num_points=2):
    """构造单实例配置（§3.1 模板）。"""
    points = []
    for i in range(num_points):
        points.append({"id": f"point_{i}", "addr": 1000 + i, "shm_id": 0})

    return {
        "c4_shm_manager": {
            "writer": ["c4_asfp2_server"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_asfp2_server": [
            {
                "name": f"test_{instance_id}",
                "id": instance_id,
                "port": port,
                "t1": 0,
                "t2": 0,
                "forward_kack": 255,
                "inverse_keep": 0,
                "points": points,
            }
        ],
        "c4_asfp2_client": [],
    }


def _make_multi_instance_config(instance_infos):
    """构造多实例配置。

    instance_infos: list of (instance_id, port) tuples。每个实例 1 个 point。
    """
    instances = []
    for idx, (iid, port) in enumerate(instance_infos):
        instances.append(
            {
                "name": f"test_{iid}",
                "id": iid,
                "port": port,
                "t1": 0,
                "t2": 0,
                "forward_kack": 255,
                "inverse_keep": 0,
                "points": [
                    {"id": f"point_{idx}", "addr": 2000 + idx, "shm_id": 0}
                ],
            }
        )

    return {
        "c4_shm_manager": {
            "writer": ["c4_asfp2_server"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_asfp2_server": instances,
        "c4_asfp2_client": [],
    }


def _make_empty_instance_config():
    """构造空实例列表配置（TC3）。"""
    return {
        "c4_shm_manager": {
            "writer": ["c4_asfp2_server"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_asfp2_server": [],
        "c4_asfp2_client": [],
    }


# ──────────────────────────────────────────────
#  测试用例
# ──────────────────────────────────────────────


class TestAsfp2ServerStart:

    # ── TC1: 基本启动 — 单实例 2 point ──────────

    def test_tc1_basic_startup(
        self, prepare_environment, start_asfp2_server, isolated_shm
    ):
        """TC1: 配置 1 个 Server 实例、port=9000、2 个 point — 启动成功，端口监听。"""
        iid = "test_tc1"
        isolated_shm(iid)

        config = _make_single_instance_config(iid, port=9000, num_points=2)
        config_path, _ = prepare_environment(config, iid)

        resp = start_asfp2_server.call_tool(
            "start",
            {},
            on_request=_roots_callback(
                [{"uri": f"file://{config_path}"}]
            ),
        )
        assert resp["result"].get("isError", False) is False, (
            f"start failed: {resp}"
        )
        assert resp["result"]["content"][0]["text"] == "success", (
            f"Expected 'success', got: {resp}"
        )
        assert _port_is_listening(9000), "Port 9000 is not listening"

    # ── TC2: 多实例启动 — 3 个不同端口 ──────────

    def test_tc2_multi_instance(
        self, prepare_environment, start_asfp2_server, isolated_shm
    ):
        """TC2: 配置 3 个 Server 实例，端口 9100/9101/9102 — 全部启动成功，端口监听。"""
        iid = "test_tc2"
        isolated_shm(iid)

        instance_infos = [
            ("r1", 9100),
            ("r2", 9101),
            ("r3", 9102),
        ]
        config = _make_multi_instance_config(instance_infos)
        config_path, _ = prepare_environment(config, iid)

        resp = start_asfp2_server.call_tool(
            "start",
            {},
            on_request=_roots_callback(
                [{"uri": f"file://{config_path}"}]
            ),
        )
        assert resp["result"].get("isError", False) is False, (
            f"start failed: {resp}"
        )
        assert resp["result"]["content"][0]["text"] == "success"

        for _, port in instance_infos:
            assert _port_is_listening(port), (
                f"Port {port} is not listening"
            )

    # ── TC3: 空实例列表 — 0 个 Server ───────────

    def test_tc3_empty_instances(
        self, prepare_environment, start_asfp2_server, isolated_shm
    ):
        """TC3: 空实例列表 — start 成功，无端口监听。"""
        iid = "test_tc3"
        isolated_shm(iid)

        config = _make_empty_instance_config()
        config_path, _ = prepare_environment(config, iid)

        resp = start_asfp2_server.call_tool(
            "start",
            {},
            on_request=_roots_callback(
                [{"uri": f"file://{config_path}"}]
            ),
        )
        assert resp["result"].get("isError", False) is False, (
            f"start failed: {resp}"
        )
        assert resp["result"]["content"][0]["text"] == "success"

        # 不应有任何端口被监听
        for port in (9000, 9001, 9002, 9100, 9101, 9102, 9200):
            assert not _port_is_listening(port), (
                f"Port {port} should not be listening for empty instances"
            )

    # ── TC4: 重复调用 start →     ALREADY_RUNNING ─────

    def test_tc4_double_start(
        self, prepare_environment, start_asfp2_server, isolated_shm
    ):
        """TC4: SELF-CONTAINED — 首次 start 成功，再次 start 返回     ALREADY_RUNNING。"""
        iid = "test_tc4"
        isolated_shm(iid)

        config = _make_single_instance_config(iid, port=9080, num_points=1)
        config_path, _ = prepare_environment(config, iid)

        # 首次 start
        resp1 = start_asfp2_server.call_tool(
            "start",
            {},
            on_request=_roots_callback(
                [{"uri": f"file://{config_path}"}]
            ),
        )
        assert resp1["result"].get("isError", False) is False
        assert resp1["result"]["content"][0]["text"] == "success"

        # 再次 start — 应返回     ALREADY_RUNNING
        resp2 = start_asfp2_server.call_tool(
            "start",
            {},
            on_request=_roots_callback(
                [{"uri": f"file://{config_path}"}]
            ),
        )
        _assert_mcp_error(resp2, "ALREADY_RUNNING")

    # ── TC5: start 未调用前调用 stop/status → SERVICE_NOT_READY

    def test_tc5_service_not_ready(self, start_asfp2_server):
        """TC5: start 前调用 stop/status 均返回 SERVICE_NOT_READY。"""
        # stop
        resp = start_asfp2_server.call_tool("stop", {})
        _assert_mcp_error(resp, "SERVICE_NOT_READY")

        # status
        resp = start_asfp2_server.call_tool("status", {})
        _assert_mcp_error(resp, "SERVICE_NOT_READY")

    # ── TC6: 端口重复 → PORT_CONFLICT ─────────────

    def test_tc6_port_conflict(
        self, prepare_environment, start_asfp2_server, isolated_shm
    ):
        """TC6: 2 个实例同一端口 9200 — PORT_CONFLICT，端口未被监听。"""
        iid = "test_tc6"
        isolated_shm(iid)

        # 两个实例使用同一端口
        config = _make_multi_instance_config([
            ("r1", 9200),
            ("r2", 9200),
        ])
        config_path, _ = prepare_environment(config, iid)

        resp = start_asfp2_server.call_tool(
            "start",
            {},
            on_request=_roots_callback(
                [{"uri": f"file://{config_path}"}]
            ),
        )
        _assert_mcp_error(resp, "PORT_CONFLICT")

        # 端口不应被监听（SUT 应在检测到冲突后不启动任何 goroutine）
        assert not _port_is_listening(9200), (
            "Port 9200 should not be listening after PORT_CONFLICT"
        )

    # ── TC7: 共享内存不存在 → SHM_OPEN_FAILED ────

    def test_tc7_shm_open_failed(
        self, start_asfp2_server, isolated_shm
    ):
        """TC7: 未创建共享内存 — start 返回 SHM_OPEN_FAILED。"""
        iid = "test_tc7"
        isolated_shm(iid)

        # 创建正常配置文件但不创建 shm
        config = _make_single_instance_config(iid, port=9070, num_points=1)
        config_path = _make_config_file(config)

        try:
            resp = start_asfp2_server.call_tool(
                "start",
                {},
                on_request=_roots_callback(
                    [{"uri": f"file://{config_path}"}]
                ),
            )
            _assert_mcp_error(resp, "SHM_OPEN_FAILED")
        finally:
            os.unlink(config_path)

    # ── TC8: 共享内存 magic 损坏 → SHM_CORRUPTED ──

    def test_tc8_shm_corrupted(
        self, prepare_environment, start_asfp2_server, isolated_shm
    ):
        """TC8: 修改 Header magic 为 0xDEADBEEF — start 返回 SHM_CORRUPTED。"""
        iid = "test_tc8"
        isolated_shm(iid)

        config = _make_single_instance_config(iid, port=9060, num_points=1)
        config_path, _ = prepare_environment(config, iid)

        # 损坏共享内存 Header magic 字段
        path = shm_path(iid)
        fd = os.open(path, os.O_RDWR)
        try:
            buf = mmap.mmap(fd, 4, mmap.MAP_SHARED, mmap.PROT_WRITE)
            buf[0:4] = struct.pack(">I", 0xDEADBEEF)
            buf.close()
        finally:
            os.close(fd)

        resp = start_asfp2_server.call_tool(
            "start",
            {},
            on_request=_roots_callback(
                [{"uri": f"file://{config_path}"}]
            ),
        )
        _assert_mcp_error(resp, "SHM_CORRUPTED")

    # ── TC9: roots/list 超时 → CONFIG_PATH_MISSING ─

    def test_tc9_config_path_missing(self, start_asfp2_server):
        """TC9: 不响应 roots/list — SUT 超时后返回 CONFIG_PATH_MISSING。"""
        # on_request 返回 None，即不响应 roots/list 请求
        resp = start_asfp2_server.call_tool(
            "start",
            {},
            on_request=lambda method, params, request_id: None,
        )
        _assert_mcp_error(resp, "CONFIG_PATH_MISSING")

    # ── TC10: 配置文件格式错误 → CONFIG_PARSE_ERROR ─

    @pytest.mark.parametrize(
        "bad_config_content",
        [
            "{invalid json\n",
            '{"c4_shm_manager": {"writer": [], "reader": []}}',
        ],
    )
    def test_tc10_config_parse_error(
        self, shm_mgr_client, start_asfp2_server, isolated_shm,
        bad_config_content,
    ):
        """TC10: 格式错误的配置文件 — start 返回 CONFIG_PARSE_ERROR。

        子场景:
        (a) JSON 语法错误
        (b) 合法 JSON 但缺少 c4_asfp2_server key
        """
        iid = f"test_tc10_{abs(hash(bad_config_content)) % 100000}"
        isolated_shm(iid)

        # 先创建 shm（使用有效配置，确保共享内存存在）
        valid_config = {
            "c4_shm_manager": {
                "writer": [],
                "reader": [],
            },
        }
        fd, valid_config_path = tempfile.mkstemp(
            suffix=".json", prefix="c4_config_valid_", text=True
        )
        with os.fdopen(fd, "w") as f:
            json.dump(valid_config, f)

        try:
            resp = shm_mgr_client.call_tool(
                "create_shm",
                {"instance_id": iid},
                on_request=_roots_callback(
                    [{"uri": f"file://{valid_config_path}"}]
                ),
            )
            assert resp["result"].get("isError", False) is False, (
                f"create_shm failed for TC10: {resp}"
            )
            resp = shm_mgr_client.call_tool(
                "adjust_shm",
                {},
                on_request=_roots_callback(
                    [{"uri": f"file://{valid_config_path}"}]
                ),
            )
            assert resp["result"].get("isError", False) is False, (
                f"adjust_shm failed for TC10: {resp}"
            )
        finally:
            os.unlink(valid_config_path)

        # 创建格式错误的配置文件
        fd, bad_config_path = tempfile.mkstemp(
            suffix=".json", prefix="c4_config_bad_", text=True
        )
        with os.fdopen(fd, "w") as f:
            f.write(bad_config_content)

        try:
            resp = start_asfp2_server.call_tool(
                "start",
                {},
                on_request=_roots_callback(
                    [{"uri": f"file://{bad_config_path}"}]
                ),
            )
            _assert_mcp_error(resp, "CONFIG_PARSE_ERROR")
        finally:
            os.unlink(bad_config_path)
