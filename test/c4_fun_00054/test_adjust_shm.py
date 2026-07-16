"""
C4_FUN_00054 测试用例 — adjust_shm MCP 工具

验证 c4_shm_manager adjust_shm 工具的全部分支：
  A: 不扩容（空闲块足够）— TC1, TC2, TC3
  B: 扩容（超容量）— TC4, TC5, TC6
  C: 配置解析错误 — TC7, TC8, TC9, TC10, TC11, TC12, TC22
  D: 基础设施错误 — TC13, TC14
  E: 配置回填验证 — TC15, TC16, TC17
  F: 状态一致性 — TC18, TC19, TC20a/b, TC21, TC23, TC24

所有验证均通过直接读取共享内存完成，不使用 query_status 工具。
"""

import json
import os
import tempfile

from shm_helpers import get_shm_size, read_shm_block, read_shm_header, shm_path

MAGIC = 0xC4DA7A00
BLOCK_SIZE = 32


# ══════════════════════════════════════════════════
#  配置工厂 & 修改辅助
# ══════════════════════════════════════════════════


def _make_config_file(config_dict):
    """将配置 dict 写入临时 JSON 文件，返回路径。调用方负责清理。"""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="c4_config_", text=True)
    with os.fdopen(fd, "w") as f:
        json.dump(config_dict, f)
    return path


def _read_config(path):
    """读取 JSON 配置文件，返回 dict。"""
    with open(path, "r") as f:
        return json.load(f)


def _make_initial_config(writer_points, reader_points=None):
    """构造 create_shm 可用的初始配置。

    Args:
        writer_points: modbus writer 采集点数
        reader_points: 若为 int，生成等量 reader key；若为 None，reader 为空
    """
    cfg = {
        "c4_shm_manager": {
            "writer": ["c4_modbus_client"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_modbus_client": [{
            "id": "device1",
            "points": [
                {"id": f"p{i}", "uid": 1, "addr": 40000 + i, "shm_id": 0}
                for i in range(1, writer_points + 1)
            ],
        }],
        "c4_asfp2_client": [{
            "points": [] if reader_points is None else [
                {"key": f"device1.p{i}", "addr": 100 + i, "shm_id": 0}
                for i in range(1, reader_points + 1)
            ],
        }],
    }
    return cfg


def add_points_to_config(config_path, service_id, new_points):
    """向已有 Writer section 的 points 数组追加新采集点（shm_id=0）。"""
    with open(config_path, "r") as f:
        cfg = json.load(f)
    for writer in cfg.get(service_id, []):
        if isinstance(writer, dict) and "points" in writer:
            writer["points"].extend(new_points)
    with open(config_path, "w") as f:
        json.dump(cfg, f)


def add_writer_to_config(config_path, writer_name, writer_points):
    """添加新的 Writer section 并更新 c4_shm_manager.writer 列表。

    Args:
        config_path:   配置文件路径（原地修改）
        writer_name:   e.g. "c4_iec104_client"
        writer_points: list of point dicts（新 writer 的采集点）
    """
    with open(config_path, "r") as f:
        cfg = json.load(f)
    # 更新 writer 类型列表
    if "c4_shm_manager" in cfg:
        writers = cfg["c4_shm_manager"].get("writer", [])
        if writer_name not in writers:
            cfg["c4_shm_manager"]["writer"] = writers + [writer_name]
    # 添加 writer section
    cfg[writer_name] = [{
        "id": "rtu1",
        "common_address": 1,
        "points": writer_points,
    }]
    with open(config_path, "w") as f:
        json.dump(cfg, f)


# ══════════════════════════════════════════════════
#  roots/list 回调
# ══════════════════════════════════════════════════


def _roots_callback(roots_list):
    """返回处理 roots/list 请求的回调函数。

    Args:
        roots_list: list of {"uri": "file://..."} dicts，空列表表示无配置
    """
    def cb(method, params, request_id):
        if method == "roots/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"roots": roots_list},
            }
        return None
    return cb


def _roots_error_callback():
    """返回一个对 roots/list 返回 MCP 错误的回调函数。"""
    def cb(method, params, request_id):
        if method == "roots/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": "Simulated roots failure"},
            }
        return None
    return cb


# ══════════════════════════════════════════════════
#  辅助断言
# ══════════════════════════════════════════════════


def _assert_mcp_success(resp):
    """断言 MCP 工具调用返回 success。"""
    assert resp["result"].get("isError", False) is False, (
        f"Expected success, got: {resp}"
    )
    assert resp["result"]["content"][0]["text"] == "success", (
        f"Expected 'success', got: '{resp['result']['content'][0]['text']}'"
    )


def _assert_mcp_error(resp, expected_prefix):
    """断言 MCP 响应为错误，且错误文本以 expected_prefix 开头。"""
    assert resp["result"]["isError"] is True, f"Expected isError, got: {resp}"
    text = resp["result"]["content"][0]["text"]
    assert text.startswith(expected_prefix), (
        f"Expected prefix '{expected_prefix}', got: '{text}'"
    )


def _assert_header(path, max_points, point_count, remap_version=0):
    """断言共享内存 Header 各字段。"""
    h = read_shm_header(path)
    assert h["magic"] == MAGIC, f"magic: {hex(h['magic'])}"
    assert h["version"] == 1, f"version: {h['version']}"
    assert h["point_count"] == point_count, (
        f"point_count: {h['point_count']}, expected {point_count}"
    )
    assert h["max_points"] == max_points, (
        f"max_points: {h['max_points']}, expected {max_points}"
    )
    assert h["remap_version"] == remap_version, (
        f"remap_version: {h['remap_version']}, expected {remap_version}"
    )
    assert h["global_write_seq"] == 0, (
        f"global_write_seq: {h['global_write_seq']}"
    )


def _assert_block(path, shm_id):
    """断言 Data Block 处于初始状态（magic=0xC4DA7A00, 其余字段=0）。"""
    b = read_shm_block(path, shm_id)
    assert b["magic"] == MAGIC, f"Block[{shm_id}] magic: {hex(b['magic'])}"
    assert b["state"] == 0, f"Block[{shm_id}] state: {b['state']}"
    assert b["reserved"] == 0, f"Block[{shm_id}] reserved: {b['reserved']}"
    assert b["type"] == 0, f"Block[{shm_id}] type: {b['type']}"
    assert b["write_seq"] == 0, f"Block[{shm_id}] write_seq: {b['write_seq']}"
    assert b["timestamp"] == 0, f"Block[{shm_id}] timestamp: {b['timestamp']}"
    assert b["value"] == 0, f"Block[{shm_id}] value: {b['value']}"


def _assert_block_new(path, shm_id):
    """断言新扩容 Block magic=0xC4DA7A00 且 state=0（比 _assert_block 更宽松）。"""
    b = read_shm_block(path, shm_id)
    assert b["magic"] == MAGIC, f"Block[{shm_id}] magic: {hex(b['magic'])}"
    assert b["state"] == 0, f"Block[{shm_id}] state: {b['state']}"


def _get_shm_ids_from_config(config_path, service_id):
    """从配置文件读取指定 service 所有 point 的 shm_id 列表。"""
    cfg = _read_config(config_path)
    pts = cfg[service_id][0]["points"]
    return [p["shm_id"] for p in pts]


# ══════════════════════════════════════════════════
#  分支 A：不扩容（空闲块足够）
# ══════════════════════════════════════════════════


class TestNoExpand:
    """TC1–TC3：required_points ≤ max_points，在空闲块中分配，无 ftruncate。"""

    def test_tc1_no_expand_add_one(self, mcp, isolated_shm):
        """TC1: 不扩容 — 新增 1 个采集点（3≤4），空闲块分配。"""
        iid = "test_tc1"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2)
        config_path = _make_config_file(config)

        try:
            # 前置：create_shm
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            _assert_header(path, max_points=4, point_count=2, remap_version=0)
            size_before = get_shm_size(path)
            assert size_before == (4 + 1) * BLOCK_SIZE                 # 160

            # 捕获已有 block 内容
            b1_before = read_shm_block(path, 1)
            b2_before = read_shm_block(path, 2)

            # 修改配置：追加 1 个新 point (total=3 ≤ 4)
            add_points_to_config(
                config_path, "c4_modbus_client",
                [{"id": "p3", "uid": 1, "addr": 40003, "shm_id": 0}],
            )

            # 操作：adjust_shm
            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 1: max_points/remap_version 不变
            _assert_header(path, max_points=4, point_count=3, remap_version=0)

            # 验证 2+3: 已有 p1→1, p2→2；新 p3→3
            shm_ids = _get_shm_ids_from_config(config_path, "c4_modbus_client")
            assert shm_ids == [1, 2, 3], f"shm_ids: {shm_ids}"

            # 验证 4: 配置回填 — 所有 writer shm_ids > 0
            assert all(sid > 0 for sid in shm_ids)

            # 验证 5: Header 直接读取
            h = read_shm_header(path)
            assert h["max_points"] == 4
            assert h["point_count"] == 3
            assert h["remap_version"] == 0

            # 验证 6: Block[3] 初始化正确；已有 Block 不变
            _assert_block(path, 3)
            b1_after = read_shm_block(path, 1)
            b2_after = read_shm_block(path, 2)
            for key in ("magic", "state"):
                assert b1_after[key] == b1_before[key], f"Block[1] {key} changed"
                assert b2_after[key] == b2_before[key], f"Block[2] {key} changed"

            # 验证 7: shm 文件大小不变
            assert get_shm_size(path) == size_before

        finally:
            os.unlink(config_path)

    def test_tc2_idempotent_no_change(self, mcp, isolated_shm):
        """TC2: 不扩容 — 无新增点（幂等性）。"""
        iid = "test_tc2"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=3)      # max_points=6
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            _assert_header(path, max_points=6, point_count=3, remap_version=0)
            size_before = get_shm_size(path)
            assert size_before == (6 + 1) * BLOCK_SIZE                 # 224

            # 不修改配置，直接调用 adjust_shm
            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 1: max_points/remap_version 不变
            _assert_header(path, max_points=6, point_count=3, remap_version=0)

            # 验证 2: 所有 shm_id 与 create_shm 后一致
            shm_ids = _get_shm_ids_from_config(config_path, "c4_modbus_client")
            assert shm_ids == [1, 2, 3], f"shm_ids: {shm_ids}"

            # 验证 3: Header 直接读取
            h = read_shm_header(path)
            assert h["max_points"] == 6
            assert h["point_count"] == 3
            assert h["remap_version"] == 0

            # 验证 4: shm 文件大小不变
            assert get_shm_size(path) == size_before

        finally:
            os.unlink(config_path)

    def test_tc3_boundary_equal_max(self, mcp, isolated_shm):
        """TC3: 不扩容 — 边界 required_points == max_points。"""
        iid = "test_tc3"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2)      # max_points=4
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)
            path = shm_path(iid)
            size_before = get_shm_size(path)
            assert size_before == (4 + 1) * BLOCK_SIZE                 # 160

            # 追加 2 点 → total=4 == max_points
            add_points_to_config(
                config_path, "c4_modbus_client",
                [
                    {"id": "p3", "uid": 1, "addr": 40003, "shm_id": 0},
                    {"id": "p4", "uid": 1, "addr": 40004, "shm_id": 0},
                ],
            )

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 1: max_points=4 不变，未触发扩容
            _assert_header(path, max_points=4, point_count=4, remap_version=0)

            # 验证 2: 所有 4 点均有唯一 shm_id
            shm_ids = _get_shm_ids_from_config(config_path, "c4_modbus_client")
            assert sorted(shm_ids) == [1, 2, 3, 4], f"shm_ids: {shm_ids}"
            assert len(set(shm_ids)) == 4, "duplicate shm_ids found"

            # 验证 3: Header 直接读取
            h = read_shm_header(path)
            assert h["max_points"] == 4
            assert h["point_count"] == 4
            assert h["remap_version"] == 0

            # 验证 4: shm 文件大小不变
            assert get_shm_size(path) == size_before

        finally:
            os.unlink(config_path)


# ══════════════════════════════════════════════════
#  分支 B：扩容（超容量）
# ══════════════════════════════════════════════════


class TestExpand:
    """TC4–TC6：required_points > max_points → ftruncate + remap_version++。"""

    def test_tc4_expand_single_writer(self, mcp, isolated_shm):
        """TC4: 扩容 — 单 Writer 新点超容量 (5＞4 → max=10)。"""
        iid = "test_tc4"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)
            path = shm_path(iid)

            # 追加 3 点 → total=5 > max=4
            add_points_to_config(
                config_path, "c4_modbus_client",
                [
                    {"id": "p3", "uid": 1, "addr": 40003, "shm_id": 0},
                    {"id": "p4", "uid": 1, "addr": 40004, "shm_id": 0},
                    {"id": "p5", "uid": 1, "addr": 40005, "shm_id": 0},
                ],
            )

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 1: max_points = 5×2 = 10
            # 验证 2: remap_version 递增 0→1
            _assert_header(path, max_points=10, point_count=5, remap_version=1)

            # 验证 3+4: 已有 p1→1, p2→2；新 p3→3, p4→4, p5→5
            shm_ids = _get_shm_ids_from_config(config_path, "c4_modbus_client")
            assert shm_ids == [1, 2, 3, 4, 5], f"shm_ids: {shm_ids}"

            # 验证 5: shm 文件大小 = (10+1)×32 = 352
            expected_size = (10 + 1) * BLOCK_SIZE
            assert get_shm_size(path) == expected_size, (
                f"size: {get_shm_size(path)} vs {expected_size}"
            )

            # 验证 6: 扩容新增区域 Block[6..10] 初始化正确
            for sid in range(6, 11):
                _assert_block(path, sid)

            # 验证 7: Header 直接读取
            h = read_shm_header(path)
            assert h["point_count"] == 5
            assert h["max_points"] == 10
            assert h["remap_version"] == 1

        finally:
            os.unlink(config_path)

    def test_tc5_expand_multi_writer(self, mcp, isolated_shm):
        """TC5: 扩容 — 多 Writer 聚合超容量 (6＞4 → max=12)。"""
        iid = "test_tc5"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)

            # 新增 iec104 Writer（4 点）→ total=6 > max=4
            iec104_points = [
                {"id": f"iec_p{i}", "addr": 16384 + i, "shm_id": 0}
                for i in range(1, 5)
            ]
            add_writer_to_config(config_path, "c4_iec104_client", iec104_points)

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 1: max_points = 6×2 = 12
            _assert_header(path, max_points=12, point_count=6, remap_version=1)

            # 验证 2: modbus p1→1,p2→2；iec104 p3→3..p6→6
            modbus_ids = _get_shm_ids_from_config(config_path, "c4_modbus_client")
            assert modbus_ids == [1, 2], f"modbus shm_ids: {modbus_ids}"

            iec104_ids = _get_shm_ids_from_config(config_path, "c4_iec104_client")
            assert iec104_ids == [3, 4, 5, 6], f"iec104 shm_ids: {iec104_ids}"

            # 验证 3+4: remap_version > 0, point_count=6
            h = read_shm_header(path)
            assert h["remap_version"] > 0
            assert h["point_count"] == 6
            assert h["max_points"] == 12

        finally:
            os.unlink(config_path)

    def test_tc6_expand_large_growth(self, mcp, isolated_shm):
        """TC6: 扩容 — 大幅增长 (10＞4 → max=20)。"""
        iid = "test_tc6"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)

            # 追加 8 点 → total=10 > max=4
            add_points_to_config(
                config_path, "c4_modbus_client",
                [
                    {"id": f"p{i}", "uid": 1, "addr": 40000 + i, "shm_id": 0}
                    for i in range(3, 11)
                ],
            )

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 1: max_points = 10×2 = 20
            _assert_header(path, max_points=20, point_count=10, remap_version=1)

            # 验证 2: 10 点均有连续 shm_id 1..10
            shm_ids = _get_shm_ids_from_config(config_path, "c4_modbus_client")
            assert shm_ids == list(range(1, 11)), f"shm_ids: {shm_ids}"

            # 验证 3: shm 文件大小 = (20+1)×32 = 672
            expected_size = (20 + 1) * BLOCK_SIZE
            assert get_shm_size(path) == expected_size, (
                f"size: {get_shm_size(path)} vs {expected_size}"
            )

            # 验证 4: Block[1]（首块）、Block[10]（末个已分配）、Block[20]（末块）
            for sid in (1, 10, 20):
                _assert_block(path, sid)

        finally:
            os.unlink(config_path)


# ══════════════════════════════════════════════════
#  分支 C：配置解析错误
# ══════════════════════════════════════════════════


class TestConfigErrors:
    """TC7–TC12, TC22：配置缺失/解析失败/格式错误。"""

    def test_tc7_missing_shm_manager_section(self, mcp, isolated_shm):
        """TC7: adjust_shm 缺少 c4_shm_manager 段。"""
        iid = "test_tc7"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2)
        config_path = _make_config_file(config)

        try:
            # 前置：create_shm
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            h_before = read_shm_header(path)

            # 删除顶层 c4_shm_manager key
            cfg = _read_config(config_path)
            del cfg["c4_shm_manager"]
            with open(config_path, "w") as f:
                json.dump(cfg, f)

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_error(resp, "CONFIG_MISSING_SECTION")

            # 验证：shm 状态不变
            h_after = read_shm_header(path)
            assert h_after["max_points"] == h_before["max_points"]
            assert h_after["remap_version"] == h_before["remap_version"]
            assert h_after["point_count"] == h_before["point_count"]

        finally:
            os.unlink(config_path)

    def test_tc8_empty_writer(self, mcp, isolated_shm):
        """TC8: adjust_shm writer 为空。"""
        iid = "test_tc8"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            h_before = read_shm_header(path)

            # c4_shm_manager.writer = []
            cfg = _read_config(config_path)
            cfg["c4_shm_manager"]["writer"] = []
            with open(config_path, "w") as f:
                json.dump(cfg, f)

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_error(resp, "CONFIG_MISSING_SECTION")

            # shm 不变
            h_after = read_shm_header(path)
            assert h_after["max_points"] == h_before["max_points"]
            assert h_after["point_count"] == h_before["point_count"]

        finally:
            os.unlink(config_path)

    def test_tc9_empty_reader(self, mcp, isolated_shm):
        """TC9: adjust_shm reader 为空。"""
        iid = "test_tc9"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2, reader_points=1)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            h_before = read_shm_header(path)

            # c4_shm_manager.reader = []
            cfg = _read_config(config_path)
            cfg["c4_shm_manager"]["reader"] = []
            with open(config_path, "w") as f:
                json.dump(cfg, f)

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_error(resp, "CONFIG_MISSING_SECTION")

            # shm 不变
            h_after = read_shm_header(path)
            assert h_after["max_points"] == h_before["max_points"]
            assert h_after["point_count"] == h_before["point_count"]

        finally:
            os.unlink(config_path)

    def test_tc10_malformed_json(self, mcp, isolated_shm):
        """TC10: config JSON 格式错误。"""
        iid = "test_tc10"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            h_before = read_shm_header(path)

            # 覆盖为非法 JSON
            with open(config_path, "w") as f:
                f.write("{broken json")

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_error(resp, "CONFIG")

            # shm 状态不变
            h_after = read_shm_header(path)
            assert h_after["max_points"] == h_before["max_points"]
            assert h_after["point_count"] == h_before["point_count"]

        finally:
            os.unlink(config_path)

    def test_tc11_duplicate_key(self, mcp, isolated_shm):
        """TC11: 重复 key → DUPLICATE_KEY。"""
        iid = "test_tc11"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            h_before = read_shm_header(path)

            # 追加与 p1 相同 id 的点 → device1.p1 重复
            add_points_to_config(
                config_path, "c4_modbus_client",
                [{"id": "p1", "uid": 2, "addr": 50000, "shm_id": 0}],
            )

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_error(resp, "DUPLICATE_KEY")

            # shm 状态不变
            h_after = read_shm_header(path)
            assert h_after["max_points"] == h_before["max_points"]
            assert h_after["point_count"] == h_before["point_count"]

        finally:
            os.unlink(config_path)

    def test_tc12_unknown_reader_key(self, mcp, isolated_shm):
        """TC12: Reader key 不存在 → UNKNOWN_READER_KEY。"""
        iid = "test_tc12"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2, reader_points=1)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            h_before = read_shm_header(path)

            # 向 reader 追加不存在的 key
            cfg = _read_config(config_path)
            cfg["c4_asfp2_client"][0]["points"].append(
                {"key": "device1.ghost", "addr": 999, "shm_id": 0}
            )
            with open(config_path, "w") as f:
                json.dump(cfg, f)

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_error(resp, "UNKNOWN_READER_KEY")

            # shm 状态不变
            h_after = read_shm_header(path)
            assert h_after["max_points"] == h_before["max_points"]
            assert h_after["point_count"] == h_before["point_count"]

        finally:
            os.unlink(config_path)

    def test_tc22_empty_config_object(self, mcp, isolated_shm):
        """TC22: 空配置文件 {} → CONFIG_MISSING_SECTION。"""
        iid = "test_tc22"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            h_before = read_shm_header(path)

            # 替换为 {}
            with open(config_path, "w") as f:
                f.write("{}")

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_error(resp, "CONFIG_MISSING_SECTION")

            # shm 状态不变
            h_after = read_shm_header(path)
            assert h_after["max_points"] == h_before["max_points"]
            assert h_after["point_count"] == h_before["point_count"]

        finally:
            os.unlink(config_path)


# ══════════════════════════════════════════════════
#  分支 D：基础设施错误
# ══════════════════════════════════════════════════


class TestInfraErrors:
    """TC13–TC14：SHM 未创建、roots/list 失败。"""

    def test_tc13_shm_not_created(self, mcp, isolated_shm):
        """TC13: SHM 未创建 → SHM_NOT_CREATED。"""
        iid = "test_tc13"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2)
        config_path = _make_config_file(config)

        try:
            # 不调用 create_shm，直接 adjust_shm
            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_error(resp, "SHM_NOT_CREATED")

        finally:
            os.unlink(config_path)

    def test_tc14_roots_list_failure(self, mcp, isolated_shm):
        """TC14: roots/list 失败 → CONFIG_PATH_MISSING。"""
        iid = "test_tc14"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            h_before = read_shm_header(path)

            # adjust_shm 时 roots/list 返回错误
            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_error_callback(),
            )
            _assert_mcp_error(resp, "CONFIG_PATH_MISSING")

            # shm 状态不变
            h_after = read_shm_header(path)
            assert h_after["max_points"] == h_before["max_points"]
            assert h_after["point_count"] == h_before["point_count"]

        finally:
            os.unlink(config_path)


# ══════════════════════════════════════════════════
#  分支 E：配置回填验证
# ══════════════════════════════════════════════════


class TestConfigWriteback:
    """TC15–TC17：adjust_shm 后的配置文件回填正确性。"""

    def test_tc15_writer_shm_ids_all_filled(self, mcp, isolated_shm):
        """TC15: 配置回填 — Writer shm_ids 全部填充且无重复。"""
        iid = "test_tc15"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2)      # max=4
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 追加 2 点
            add_points_to_config(
                config_path, "c4_modbus_client",
                [
                    {"id": "p3", "uid": 1, "addr": 40003, "shm_id": 0},
                    {"id": "p4", "uid": 1, "addr": 40004, "shm_id": 0},
                ],
            )

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 1: 所有 shm_id > 0
            shm_ids = _get_shm_ids_from_config(config_path, "c4_modbus_client")
            assert all(sid > 0 for sid in shm_ids), f"shm_ids: {shm_ids}"

            # 验证 2: 无重复
            assert len(shm_ids) == len(set(shm_ids)), (
                f"duplicate shm_ids: {shm_ids}"
            )

            # 验证 3: 没有 shm_id=0 残留
            assert 0 not in shm_ids, f"found shm_id=0: {shm_ids}"

        finally:
            os.unlink(config_path)

    def test_tc16_reader_shm_ids_match_writer(self, mcp, isolated_shm):
        """TC16: 配置回填 — Reader shm_ids 匹配 Writer 同 key 的 shm_id。"""
        iid = "test_tc16"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2, reader_points=1)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 追加 2 点 + reader 对应 key
            add_points_to_config(
                config_path, "c4_modbus_client",
                [
                    {"id": "p3", "uid": 1, "addr": 40003, "shm_id": 0},
                    {"id": "p4", "uid": 1, "addr": 40004, "shm_id": 0},
                ],
            )
            cfg = _read_config(config_path)
            cfg["c4_asfp2_client"][0]["points"].extend([
                {"key": "device1.p3", "addr": 103, "shm_id": 0},
                {"key": "device1.p4", "addr": 104, "shm_id": 0},
            ])
            with open(config_path, "w") as f:
                json.dump(cfg, f)

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证：Reader point shm_id = 对应 Writer point（同 key）的 shm_id
            updated = _read_config(config_path)
            writer_pts = updated["c4_modbus_client"][0]["points"]
            reader_pts = updated["c4_asfp2_client"][0]["points"]

            writer_map = {f"device1.{p['id']}": p["shm_id"] for p in writer_pts}
            for rp in reader_pts:
                key = rp["key"]
                assert key in writer_map, f"reader key '{key}' not in writer"
                assert rp["shm_id"] == writer_map[key], (
                    f"reader '{key}' shm_id={rp['shm_id']} != writer {writer_map[key]}"
                )

        finally:
            os.unlink(config_path)

    def test_tc17_non_shm_id_fields_preserved(self, mcp, isolated_shm):
        """TC17: 配置回填 — 非 shm_id 字段保留不变。"""
        iid = "test_tc17"
        isolated_shm(iid)

        # 构造含额外字段的配置
        config = {
            "c4_shm_manager": {
                "writer": ["c4_modbus_client"],
                "reader": ["c4_asfp2_client"],
            },
            "c4_modbus_client": [{
                "id": "device1",
                "points": [
                    {"id": "p1", "uid": 1, "addr": 40001, "fun": 3, "type": 10,
                     "swap": 2, "shm_id": 0},
                    {"id": "p2", "uid": 1, "addr": 40002, "fun": 3, "type": 10,
                     "swap": 2, "shm_id": 0},
                ],
            }],
            "c4_asfp2_client": [{
                "points": [
                    {"key": "device1.p1", "addr": 100, "shm_id": 0,
                     "valid": 1, "coeff": 0.38, "base": 0},
                    {"key": "device1.p2", "addr": 101, "shm_id": 0,
                     "valid": 1, "coeff": 0.42, "base": 0},
                ],
            }],
        }
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 记录 create_shm 回填后的字段（用于 baseline）
            after_create = _read_config(config_path)

            # 追加 1 点
            add_points_to_config(
                config_path, "c4_modbus_client",
                [{"id": "p3", "uid": 1, "addr": 40003, "fun": 3, "type": 10,
                  "swap": 2, "shm_id": 0}],
            )

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            updated = _read_config(config_path)
            writer_pts = updated["c4_modbus_client"][0]["points"]
            reader_pts = updated["c4_asfp2_client"][0]["points"]

            # 验证 1: Writer 已有 point 的非 shm_id 字段不变
            for pt_idx in range(2):  # first 2 existing points
                for pf in ("uid", "addr", "fun", "type", "swap"):
                    wpt = writer_pts[pt_idx]
                    ref = after_create["c4_modbus_client"][0]["points"][pt_idx]
                    assert wpt[pf] == ref[pf], (
                        f"writer p{pt_idx+1} field '{pf}' changed: "
                        f"{ref[pf]} → {wpt[pf]}"
                    )

            # 验证 2: Reader point 的非 shm_id 字段不变
            for pt_idx in range(2):
                for pf in ("valid", "coeff", "base", "addr"):
                    rpt = reader_pts[pt_idx]
                    ref = after_create["c4_asfp2_client"][0]["points"][pt_idx]
                    assert rpt[pf] == ref[pf], (
                        f"reader p{pt_idx+1} field '{pf}' changed: "
                        f"{ref[pf]} → {rpt[pf]}"
                    )

            # 验证 3: 仅有 shm_id 字段发生了变化
            for i in range(2):
                wpt = writer_pts[i]
                ref = after_create["c4_modbus_client"][0]["points"][i]
                # shm_id was 1,2 after create_shm; should still be 1,2 after adjust_shm
                assert wpt["shm_id"] == ref["shm_id"], (
                    f"writer p{i+1} shm_id changed: "
                    f"{ref['shm_id']} → {wpt['shm_id']}"
                )

            # 新追加 point 的 shm_id > 0
            assert writer_pts[2]["shm_id"] > 0

        finally:
            os.unlink(config_path)


# ══════════════════════════════════════════════════
#  分支 F：状态一致性
# ══════════════════════════════════════════════════


class TestStateConsistency:
    """TC18–TC21, TC23–TC24：Header、Block、remap_version、链式扩容、默认 shm 过渡。"""

    def test_tc18_header_consistency_no_expand(self, mcp, isolated_shm):
        """TC18: 不扩容后 Header 状态一致性。"""
        iid = "test_tc18"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 追加 1 点
            add_points_to_config(
                config_path, "c4_modbus_client",
                [{"id": "p3", "uid": 1, "addr": 40003, "shm_id": 0}],
            )

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            _assert_header(path, max_points=4, point_count=3, remap_version=0)

        finally:
            os.unlink(config_path)

    def test_tc19_header_consistency_expand(self, mcp, isolated_shm):
        """TC19: 扩容后 Header 状态一致性。"""
        iid = "test_tc19"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 追加 3 点（触发扩容）
            add_points_to_config(
                config_path, "c4_modbus_client",
                [
                    {"id": "p3", "uid": 1, "addr": 40003, "shm_id": 0},
                    {"id": "p4", "uid": 1, "addr": 40004, "shm_id": 0},
                    {"id": "p5", "uid": 1, "addr": 40005, "shm_id": 0},
                ],
            )

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            _assert_header(path, max_points=10, point_count=5, remap_version=1)

        finally:
            os.unlink(config_path)

    # ── TC20a/TC20b: remap_version 行为 ─────
    # TC20a: 不扩容 → remap_version 不变。
    #   已在以下测试中验证：TC1（remap_version=0 不变）、TC2（幂等不变）、
    #   TC3（边界不变）、TC18（不扩容后 Header 一致性）。
    #
    # TC20b: 扩容 → remap_version 递增 1。
    #   已在以下测试中验证：TC4（0→1）、TC5（0→>0）、TC6（0→1）、
    #   TC19（0→1）、TC23（两次扩容 0→1→2）。

    def test_tc21_block_integrity_after_expand(self, mcp, isolated_shm):
        """TC21: Block 内容完整性（扩容后）— 已有 block 不变，新增 block 初始化。"""
        iid = "test_tc21"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            # 捕获扩容前的 Block 状态
            b1_before = read_shm_block(path, 1)
            b2_before = read_shm_block(path, 2)

            # 追加 3 点（触发扩容 max=4→10）
            add_points_to_config(
                config_path, "c4_modbus_client",
                [
                    {"id": "p3", "uid": 1, "addr": 40003, "shm_id": 0},
                    {"id": "p4", "uid": 1, "addr": 40004, "shm_id": 0},
                    {"id": "p5", "uid": 1, "addr": 40005, "shm_id": 0},
                ],
            )

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 1: 扩容不影响已有 Block 内容
            for sid, before in ((1, b1_before), (2, b2_before)):
                after = read_shm_block(path, sid)
                for field in ("magic", "state", "reserved", "type"):
                    assert after[field] == before[field], (
                        f"Block[{sid}] {field} changed: "
                        f"{before[field]} → {after[field]}"
                    )

            # 验证 2: 扩容后新增区域 magic=0xC4DA7A00, state=0
            for sid in range(5, 11):          # 旧 max=4 → 新 blocks 5..10
                _assert_block_new(path, sid)

            # 验证 3: 新增 block 的其余字段均为 0
            for sid in range(5, 11):
                b = read_shm_block(path, sid)
                assert b["type"] == 0, f"Block[{sid}] type: {b['type']}"
                assert b["write_seq"] == 0, f"Block[{sid}] write_seq: {b['write_seq']}"
                assert b["timestamp"] == 0, f"Block[{sid}] timestamp: {b['timestamp']}"
                assert b["value"] == 0, f"Block[{sid}] value: {b['value']}"

        finally:
            os.unlink(config_path)

    def test_tc23_chained_expansion(self, mcp, isolated_shm):
        """TC23: 链式扩容 — 两次 adjust_shm 均触发扩容。"""
        iid = "test_tc23"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2)
        config_path = _make_config_file(config)

        try:
            # 前置：create_shm
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            _assert_header(path, max_points=4, point_count=2, remap_version=0)

            # ── 操作 1: 追加 3 点 (total=5>4) → adjust_shm ──
            add_points_to_config(
                config_path, "c4_modbus_client",
                [
                    {"id": "p3", "uid": 1, "addr": 40003, "shm_id": 0},
                    {"id": "p4", "uid": 1, "addr": 40004, "shm_id": 0},
                    {"id": "p5", "uid": 1, "addr": 40005, "shm_id": 0},
                ],
            )

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 1a
            _assert_header(path, max_points=10, point_count=5, remap_version=1)
            shm_ids_1 = _get_shm_ids_from_config(config_path, "c4_modbus_client")
            assert shm_ids_1 == [1, 2, 3, 4, 5], f"round1 shm_ids: {shm_ids_1}"

            # ── 操作 2: 再追加 6 点 (total=11>10) → adjust_shm ──
            add_points_to_config(
                config_path, "c4_modbus_client",
                [
                    {"id": f"p{i}", "uid": 1, "addr": 40000 + i, "shm_id": 0}
                    for i in range(6, 12)
                ],
            )

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 2a+2b: max=22, remap_version=2, point_count=11
            _assert_header(path, max_points=22, point_count=11, remap_version=2)

            # 验证 2c: 第一次的 5 个点保持 shm_id=1..5
            shm_ids_2 = _get_shm_ids_from_config(config_path, "c4_modbus_client")
            assert shm_ids_2[:5] == [1, 2, 3, 4, 5], (
                f"first 5 shm_ids changed: {shm_ids_2[:5]}"
            )

            # 验证 2d: 新增 6 点分配到 shm_id=6..11
            assert shm_ids_2 == list(range(1, 12)), f"round2 shm_ids: {shm_ids_2}"

            # 验证 2e: shm 文件大小 = (22+1)×32 = 736
            expected_size = (22 + 1) * BLOCK_SIZE
            assert get_shm_size(path) == expected_size, (
                f"size: {get_shm_size(path)} vs {expected_size}"
            )

            # 验证 2f: 第二次扩容新增区域 Block[11..22] 初始化正确
            for sid in range(11, 23):
                _assert_block_new(path, sid)

        finally:
            os.unlink(config_path)

    def test_tc24_default_shm_transition(self, mcp, isolated_shm):
        """TC24: 默认 shm 上调用 adjust_shm（无配置创建 → 引入配置）。"""
        iid = "test_tc24"
        isolated_shm(iid)

        # 前置：create_shm 无配置文件 → max=100000, point_count=0
        resp = mcp.call_tool(
            "create_shm", {"instance_id": iid},
            on_request=_roots_callback([]),
        )
        _assert_mcp_success(resp)

        path = shm_path(iid)
        # 验证默认 shm 状态
        h = read_shm_header(path)
        assert h["max_points"] == 100000, f"max_points: {h['max_points']}"
        assert h["point_count"] == 0, f"point_count: {h['point_count']}"
        assert h["remap_version"] == 0
        size_before = get_shm_size(path)
        assert size_before == (100000 + 1) * BLOCK_SIZE       # 3,200,032

        # 创建配置文件（5 点）
        config = _make_initial_config(writer_points=5)
        config_path = _make_config_file(config)

        try:
            # adjust_shm，roots/list 指向新配置文件
            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 2: max_points=100000 不变
            # 验证 4: point_count=5（从 0 更新）
            # 验证 2: remap_version=0 不变
            _assert_header(path, max_points=100000, point_count=5, remap_version=0)

            # 验证 3: 5 点分配到 shm_id=1..5
            shm_ids = _get_shm_ids_from_config(config_path, "c4_modbus_client")
            assert shm_ids == [1, 2, 3, 4, 5], f"shm_ids: {shm_ids}"

            # 验证 5: shm 文件大小不变
            assert get_shm_size(path) == size_before

            # 验证 6: 配置文件所有 writer shm_id > 0，无 0 残留
            assert all(sid > 0 for sid in shm_ids)
            assert 0 not in shm_ids

        finally:
            os.unlink(config_path)
