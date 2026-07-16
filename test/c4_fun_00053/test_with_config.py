"""
C4_FUN_00053 分支 2 测试用例 — 配置文件存在

验证 c4_shm_manager 在配置文件存在时正确解析配置、计算共享内存大小、
分配 shm_id、回填 Reader 引用、写回配置文件。
"""

import json
import os
import tempfile

from shm_helpers import get_shm_size, read_shm_block, read_shm_header, shm_path

MAGIC = 0xC4DA7A00

# ──────────────────────────────────────────────
#  配置工厂
# ──────────────────────────────────────────────

MODBUS_POINT_TEMP = {"id": "temp", "uid": 1, "addr": 1000, "fun": 3, "type": 10, "swap": 2, "shm_id": 0}
MODBUS_POINT_PRESS = {"id": "press", "uid": 1, "addr": 1002, "fun": 3, "type": 10, "swap": 2, "shm_id": 0}
IEC104_POINT = {"id": "voltage", "addr": 16385, "shm_id": 0}
IEC104_POINT_A = {"id": "voltage_a", "addr": 16385, "shm_id": 0}
IEC104_POINT_B = {"id": "voltage_b", "addr": 16386, "shm_id": 0}
IEC104_POINT_C = {"id": "voltage_c", "addr": 16387, "shm_id": 0}
ASFP2_READER = {"key": "device1.temp", "addr": 100, "shm_id": 0}
ASFP2_READER_FULL = {
    "key": "device1.temp", "addr": 100, "shm_id": 0,
    "valid": 1, "coeff": 0.38, "base": 0,
}


def _make_config_file(config_dict):
    """将配置 dict 写入临时 JSON 文件，返回路径。调用方负责清理。"""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="c4_config_", text=True)
    with os.fdopen(fd, "w") as f:
        json.dump(config_dict, f)
    return path


def _make_config(writer_sections=None, reader_sections=None,
                 writer_types=None, reader_types=None):
    """构造最小有效配置。"""
    cfg = {
        "c4_shm_manager": {
            "writer": writer_types or [],
            "reader": reader_types or [],
        }
    }
    if writer_sections:
        for key, val in writer_sections.items():
            cfg[key] = val
    if reader_sections:
        for key, val in reader_sections.items():
            cfg[key] = val
    return cfg


# ──────────────────────────────────────────────
#  roots/list 回调
# ──────────────────────────────────────────────


def _roots_callback(roots_list):
    def cb(method, params, request_id):
        if method == "roots/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"roots": roots_list},
            }
        return None
    return cb


# ──────────────────────────────────────────────
#  辅助断言
# ──────────────────────────────────────────────


def _assert_mcp_error(resp, expected_prefix):
    assert resp["result"]["isError"] is True, f"Expected isError, got: {resp}"
    text = resp["result"]["content"][0]["text"]
    assert text.startswith(expected_prefix), (
        f"Expected prefix '{expected_prefix}', got: '{text}'"
    )


def _assert_header(path, max_points, point_count=0):
    h = read_shm_header(path)
    assert h["magic"] == MAGIC, f"magic: {hex(h['magic'])}"
    assert h["version"] == 1, f"version: {h['version']}"
    assert h["point_count"] == point_count, f"point_count: {h['point_count']}"
    assert h["max_points"] == max_points, f"max_points: {h['max_points']}"
    assert h["remap_version"] == 0
    assert h["global_write_seq"] == 0
    assert h["reserved"] == 0
    expected_size = (max_points + 1) * 32
    actual_size = get_shm_size(path)
    assert actual_size == expected_size, f"shm size: {actual_size} vs {expected_size}"


def _assert_block(path, shm_id):
    b = read_shm_block(path, shm_id)
    assert b["magic"] == MAGIC, f"Block {shm_id} magic: {hex(b['magic'])}"
    assert b["state"] == 0, f"Block {shm_id} state: {b['state']}"
    assert b["reserved"] == 0
    assert b["type"] == 0
    assert b["write_seq"] == 0
    assert b["timestamp"] == 0
    assert b["value"] == 0


def _read_config(path):
    with open(path, "r") as f:
        return json.load(f)


# ──────────────────────────────────────────────
#  测试用例
# ──────────────────────────────────────────────


class TestWithConfigShmCreation:

    # ── TC9: 单 Writer 2x 分配 ─────────────────

    def test_tc9_single_writer_2x(self, mcp, isolated_shm):
        iid = "test_tc9"
        isolated_shm(iid)

        config = _make_config(
            writer_types=["c4_modbus_client"],
            reader_types=["c4_asfp2_client"],
            writer_sections={
                "c4_modbus_client": [{
                    "id": "device1",
                    "points": [MODBUS_POINT_TEMP, MODBUS_POINT_PRESS],
                }]
            },
            reader_sections={
                "c4_asfp2_client": [{
                    "points": [
                        {"key": "device1.temp", "addr": 100, "shm_id": 0},
                        {"key": "device1.press", "addr": 101, "shm_id": 0},
                    ]
                }]
            },
        )
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            assert resp["result"].get("isError", False) is False
            assert resp["result"]["content"][0]["text"] == "success"

            path = shm_path(iid)
            _assert_header(path, max_points=4, point_count=2)
            _assert_block(path, 1)
            _assert_block(path, 2)
            _assert_block(path, 4)
        finally:
            os.unlink(config_path)

    # ── TC10: 多 Writer 聚合 ───────────────────

    def test_tc10_multi_writer(self, mcp, isolated_shm):
        iid = "test_tc10"
        isolated_shm(iid)

        config = _make_config(
            writer_types=["c4_modbus_client", "c4_iec104_client"],
            reader_types=["c4_asfp2_client"],
            writer_sections={
                "c4_modbus_client": [{
                    "id": "device1",
                    "points": [MODBUS_POINT_TEMP, MODBUS_POINT_PRESS],
                }],
                "c4_iec104_client": [{
                    "id": "rtu1",
                    "common_address": 1,
                    "points": [IEC104_POINT_A, IEC104_POINT_B, IEC104_POINT_C],
                }],
            },
            reader_sections={
                "c4_asfp2_client": [{"points": []}],
            },
        )
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            assert resp["result"].get("isError", False) is False
            assert resp["result"]["content"][0]["text"] == "success"

            path = shm_path(iid)
            # writer_points = 2 + 3 = 5, max_points = 10
            _assert_header(path, max_points=10, point_count=5)
            _assert_block(path, 1)
            _assert_block(path, 5)
        finally:
            os.unlink(config_path)

    # ── TC11: Reader 回填 ─────────────────────

    def test_tc11_reader_backfill(self, mcp, isolated_shm):
        iid = "test_tc11"
        isolated_shm(iid)

        config = _make_config(
            writer_types=["c4_modbus_client"],
            reader_types=["c4_asfp2_client"],
            writer_sections={
                "c4_modbus_client": [{
                    "id": "device1",
                    "points": [MODBUS_POINT_TEMP, MODBUS_POINT_PRESS],
                }]
            },
            reader_sections={
                "c4_asfp2_client": [{
                    "points": [
                        {"key": "device1.temp", "addr": 100, "shm_id": 0},
                        {"key": "device1.press", "addr": 101, "shm_id": 0},
                    ]
                }]
            },
        )
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            assert resp["result"].get("isError", False) is False

            updated = _read_config(config_path)
            writer_pts = updated["c4_modbus_client"][0]["points"]
            reader_pts = updated["c4_asfp2_client"][0]["points"]

            assert writer_pts[0]["shm_id"] == 1
            assert writer_pts[1]["shm_id"] == 2
            assert reader_pts[0]["shm_id"] == 1
            assert reader_pts[1]["shm_id"] == 2
        finally:
            os.unlink(config_path)

    # ── TC12: writer 为空 ──────────────────────

    def test_tc12_empty_writer(self, mcp, isolated_shm):
        iid = "test_tc12"
        isolated_shm(iid)

        config = _make_config(
            writer_types=[],
            reader_types=["c4_asfp2_client"],
            reader_sections={
                "c4_asfp2_client": [{"points": [ASFP2_READER]}],
            },
        )
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_error(resp, "CONFIG_MISSING_SECTION")
        finally:
            os.unlink(config_path)

    # ── TC13: reader 为空 ──────────────────────

    def test_tc13_empty_reader(self, mcp, isolated_shm):
        iid = "test_tc13"
        isolated_shm(iid)

        config = _make_config(
            writer_types=["c4_modbus_client"],
            reader_types=[],
            writer_sections={
                "c4_modbus_client": [{
                    "id": "device1",
                    "points": [MODBUS_POINT_TEMP],
                }],
            },
        )
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_error(resp, "CONFIG_MISSING_SECTION")
        finally:
            os.unlink(config_path)

    # ── TC14: 重复 key ─────────────────────────

    def test_tc14_duplicate_key(self, mcp, isolated_shm):
        iid = "test_tc14"
        isolated_shm(iid)

        # same service id "device1" × same point id "temp" → key collision
        config = _make_config(
            writer_types=["c4_modbus_client"],
            reader_types=["c4_asfp2_client"],
            writer_sections={
                "c4_modbus_client": [
                    {"id": "device1", "points": [MODBUS_POINT_TEMP]},
                    {"id": "device1", "points": [MODBUS_POINT_TEMP]},
                ]
            },
            reader_sections={
                "c4_asfp2_client": [{"points": [ASFP2_READER]}],
            },
        )
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_error(resp, "DUPLICATE_KEY")
        finally:
            os.unlink(config_path)

    # ── TC15: Reader key 不存在 ────────────────

    def test_tc15_unknown_reader_key(self, mcp, isolated_shm):
        iid = "test_tc15"
        isolated_shm(iid)

        config = _make_config(
            writer_types=["c4_modbus_client"],
            reader_types=["c4_asfp2_client"],
            writer_sections={
                "c4_modbus_client": [{
                    "id": "device1",
                    "points": [MODBUS_POINT_TEMP],
                }]
            },
            reader_sections={
                "c4_asfp2_client": [{
                    "points": [
                        {"key": "device1.unknown", "addr": 999, "shm_id": 0},
                    ]
                }]
            },
        )
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_error(resp, "UNKNOWN_READER_KEY")
        finally:
            os.unlink(config_path)

    # ── TC16: 配置文件回填 ─────────────────────

    def test_tc16_config_writeback(self, mcp, isolated_shm):
        iid = "test_tc16"
        isolated_shm(iid)

        config = _make_config(
            writer_types=["c4_modbus_client"],
            reader_types=["c4_asfp2_client"],
            writer_sections={
                "c4_modbus_client": [{
                    "id": "device1",
                    "points": [
                        {"id": "temp", "uid": 1, "addr": 1000, "shm_id": 0},
                        {"id": "press", "uid": 1, "addr": 1002, "shm_id": 0},
                    ],
                }]
            },
            reader_sections={
                "c4_asfp2_client": [{
                    "points": [
                        {"key": "device1.temp", "addr": 100, "shm_id": 0},
                        {"key": "device1.press", "addr": 101, "shm_id": 0},
                    ]
                }]
            },
        )
        config_path = _make_config_file(config)

        try:
            mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )

            updated = _read_config(config_path)

            # Writer shm_ids filled
            w = updated["c4_modbus_client"][0]["points"]
            assert w[0]["shm_id"] == 1
            assert w[1]["shm_id"] == 2
            # Non-shm_id fields preserved
            assert w[0]["id"] == "temp"
            assert w[0]["uid"] == 1
            assert w[0]["addr"] == 1000

            # Reader shm_ids filled
            r = updated["c4_asfp2_client"][0]["points"]
            assert r[0]["shm_id"] == 1
            assert r[1]["shm_id"] == 2
            assert r[0]["key"] == "device1.temp"
            assert r[0]["addr"] == 100
        finally:
            os.unlink(config_path)

    # ── TC17: query_status 交叉验证 ─────────────

    def test_tc17_query_status(self, mcp, isolated_shm):
        iid = "test_tc17"
        isolated_shm(iid)

        config = _make_config(
            writer_types=["c4_modbus_client"],
            reader_types=["c4_asfp2_client"],
            writer_sections={
                "c4_modbus_client": [{
                    "id": "device1",
                    "points": [MODBUS_POINT_TEMP, MODBUS_POINT_PRESS],
                }],
            },
            reader_sections={
                "c4_asfp2_client": [{
                    "points": [
                        {"key": "device1.temp", "addr": 100, "shm_id": 0},
                        {"key": "device1.press", "addr": 101, "shm_id": 0},
                    ]
                }]
            },
        )
        config_path = _make_config_file(config)

        try:
            mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )

            resp = mcp.call_tool("query_status", {})
            assert resp["result"].get("isError", False) is False

            inner = json.loads(resp["result"]["content"][0]["text"])
            assert inner["magic"] == "valid"
            assert inner["point_count"] == 2
            assert inner["max_points"] == 4
            assert inner["free_blocks"] == 2
        finally:
            os.unlink(config_path)

    # ── TC18: config JSON 格式错误 ──────────────

    def test_tc18_malformed_json(self, mcp, isolated_shm):
        iid = "test_tc18"
        isolated_shm(iid)

        # Write invalid JSON to config file
        config_path = _make_config_file({})  # dummy, overwrite
        with open(config_path, "w") as f:
            f.write("{invalid json")

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_error(resp, "CONFIG")  # CONFIG_PARSE_ERROR or CONFIG_MISSING_SECTION
        finally:
            os.unlink(config_path)

    # ── TC19: 缺少 c4_shm_manager 段 ──────────

    def test_tc19_missing_shm_manager_section(self, mcp, isolated_shm):
        iid = "test_tc19"
        isolated_shm(iid)

        config = {
            "c4_modbus_client": [{
                "id": "device1",
                "points": [MODBUS_POINT_TEMP],
            }],
        }
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_error(resp, "CONFIG_MISSING_SECTION")
        finally:
            os.unlink(config_path)

    # ── TC20: 分支 2 重复创建 ──────────────────

    def test_tc20_duplicate_create_with_config(self, mcp, isolated_shm):
        iid = "test_tc20"
        isolated_shm(iid)

        config = _make_config(
            writer_types=["c4_modbus_client"],
            reader_types=["c4_asfp2_client"],
            writer_sections={
                "c4_modbus_client": [{
                    "id": "device1", "points": [MODBUS_POINT_TEMP],
                }],
            },
            reader_sections={
                "c4_asfp2_client": [{"points": [ASFP2_READER]}],
            },
        )
        config_path = _make_config_file(config)

        try:
            resp1 = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            assert resp1["result"].get("isError", False) is False

            resp2 = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_error(resp2, "SHM_ALREADY_EXISTS")
        finally:
            os.unlink(config_path)

    # ── TC21: writer 类型无 config section ──────

    def test_tc21_unknown_writer_type(self, mcp, isolated_shm):
        iid = "test_tc21"
        isolated_shm(iid)

        # writer lists "c4_unknown_service" which has no section in config
        config = _make_config(
            writer_types=["c4_unknown_service"],
            reader_types=["c4_asfp2_client"],
            reader_sections={
                "c4_asfp2_client": [{"points": [ASFP2_READER]}],
            },
        )
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_error(resp, "CONFIG_MISSING_SECTION")
        finally:
            os.unlink(config_path)

    # ── TC22: Reader backfill 保留额外字段 ──────

    def test_tc22_reader_field_preservation(self, mcp, isolated_shm):
        iid = "test_tc22"
        isolated_shm(iid)

        config = _make_config(
            writer_types=["c4_modbus_client"],
            reader_types=["c4_asfp2_client"],
            writer_sections={
                "c4_modbus_client": [{
                    "id": "device1", "points": [MODBUS_POINT_TEMP],
                }],
            },
            reader_sections={
                "c4_asfp2_client": [{"points": [ASFP2_READER_FULL]}],
            },
        )
        config_path = _make_config_file(config)

        try:
            mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )

            updated = _read_config(config_path)
            r = updated["c4_asfp2_client"][0]["points"][0]
            assert r["shm_id"] == 1
            assert r["valid"] == 1
            assert r["coeff"] == 0.38
            assert r["base"] == 0
        finally:
            os.unlink(config_path)

    # ── TC23: 分支 2 Data Block 边界验证 ────────

    def test_tc23_block_boundary(self, mcp, isolated_shm):
        iid = "test_tc23"
        isolated_shm(iid)

        config = _make_config(
            writer_types=["c4_modbus_client"],
            reader_types=["c4_asfp2_client"],
            writer_sections={
                "c4_modbus_client": [{
                    "id": "device1",
                    "points": [MODBUS_POINT_TEMP, MODBUS_POINT_PRESS],
                }],
            },
            reader_sections={
                "c4_asfp2_client": [{
                    "points": [
                        {"key": "device1.temp", "addr": 100, "shm_id": 0},
                        {"key": "device1.press", "addr": 101, "shm_id": 0},
                    ]
                }],
            },
        )
        config_path = _make_config_file(config)

        try:
            mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )

            path = shm_path(iid)
            # bounds: shm_id=1 (first), 2 (last allocated), 4 (last overall)
            for sid in (1, 2, 4):
                _assert_block(path, sid)
        finally:
            os.unlink(config_path)
