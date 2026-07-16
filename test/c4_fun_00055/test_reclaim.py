"""
C4_FUN_00055 测试用例 — adjust_shm 块回收功能

验证 c4_shm_manager adjust_shm 工具在 required_points < point_count 时的块回收行为：
  A: 纯删除回收 — TC1, TC2, TC3
  B: 无删除（幂等性）— TC4, TC5
  C: 状态一致性 — TC6, TC7, TC8, TC9, TC10, TC11
  D: 错误路径 — TC12, TC13, TC14

所有验证均通过直接读取共享内存完成，不使用 query_status 工具。
Writer 激活通过 mmap 直接写入 block state 字节模拟。

注：当前 Go 实现中 adjust_shm 的回收算法尚未实现，本测试严格按 README
     规格断言预期行为——测试预期全部失败，证明源码尚未实现回收功能。
"""

import json
import mmap
import os
import struct
import tempfile

from shm_helpers import read_shm_block, read_shm_header, shm_path

MAGIC = 0xC4DA7A00


# ══════════════════════════════════════════════════
#  模拟 Writer 激活（直接写入共享内存）— README §1.3
# ══════════════════════════════════════════════════


def set_block_state(shm_path_arg, shm_id, state):
    """设置 block[shm_id] 的 state 字段（偏移 = shm_id * 32 + 4）。"""
    offset = shm_id * 32 + 4
    fd = None
    _shm = None
    try:
        fd = os.open(shm_path_arg, os.O_RDWR)
        _shm = mmap.mmap(fd, offset + 1, mmap.MAP_SHARED,
                         prot=mmap.PROT_READ | mmap.PROT_WRITE)
        _shm.seek(offset)
        _shm.write(bytes([state]))
    finally:
        if _shm is not None:
            _shm.close()
        if fd is not None:
            os.close(fd)


def set_header_point_count(shm_path_arg, count):
    """设置 Header 的 point_count 字段（偏移 = 8，大端 uint32）。"""
    fd = None
    _shm = None
    try:
        fd = os.open(shm_path_arg, os.O_RDWR)
        _shm = mmap.mmap(fd, 16, mmap.MAP_SHARED,
                         prot=mmap.PROT_READ | mmap.PROT_WRITE)
        _shm[8:12] = struct.pack(">I", count)
    finally:
        if _shm is not None:
            _shm.close()
        if fd is not None:
            os.close(fd)


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


def _make_initial_config(writer_points):
    """构造初始配置 dict — README §3.1 精确版。"""
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
            "points": [
                {"key": f"device1.p{i}", "addr": 100 + i, "shm_id": 0}
                for i in range(1, writer_points + 1)
            ],
        }],
    }
    return cfg


def _make_multi_writer_config():
    """构造含 modbus(4点) + iec104(3点) 的初始配置 dict（TC2 用）。

    max_points = 7*2 = 14（按 README TC2 规格）。
    Reader 为空，避免删除 iec104 时产生 UNKNOWN_READER_KEY。
    """
    modbus_points = [
        {"id": f"mp{i}", "uid": 1, "addr": 40000 + i, "shm_id": 0}
        for i in range(1, 5)
    ]
    iec104_points = [
        {"id": f"ip{i}", "addr": 16384 + i, "shm_id": 0}
        for i in range(1, 4)
    ]
    return {
        "c4_shm_manager": {
            "writer": ["c4_modbus_client", "c4_iec104_client"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_modbus_client": [{"id": "device1", "points": modbus_points}],
        "c4_iec104_client": [{
            "id": "rtu1",
            "common_address": 1,
            "points": iec104_points,
        }],
        "c4_asfp2_client": [{"points": []}],
    }


def remove_points_from_config(config_path, service_id, point_ids):
    """从配置中删除指定 point_id 的采集点，同时清理对应 reader key。

    Reader key 模式：device1.{point_id}（匹配 README §3.1 配置工厂）。
    """
    with open(config_path, "r") as f:
        cfg = json.load(f)
    for writer in cfg.get(service_id, []):
        if isinstance(writer, dict) and "points" in writer:
            writer["points"] = [
                p for p in writer["points"] if p["id"] not in point_ids
            ]
    # 同步清理 reader key
    for reader_section in cfg.get("c4_asfp2_client", []):
        if isinstance(reader_section, dict) and "points" in reader_section:
            reader_section["points"] = [
                rp for rp in reader_section["points"]
                if not _reader_key_matches_deleted_point(rp, point_ids)
            ]
    with open(config_path, "w") as f:
        json.dump(cfg, f)


def _reader_key_matches_deleted_point(reader_point, deleted_point_ids):
    """检查 reader key 是否对应被删除的 writer point。

    key 格式 device1.{pid}，提取 pid 与 deleted_point_ids 比较。
    """
    key = reader_point.get("key", "")
    if key.startswith("device1."):
        pid = key[len("device1."):]
        return pid in deleted_point_ids
    return False


def add_points_to_config(config_path, service_id, new_points):
    """向已有 Writer section 的 points 数组追加新采集点（shm_id=0）。"""
    with open(config_path, "r") as f:
        cfg = json.load(f)
    for writer in cfg.get(service_id, []):
        if isinstance(writer, dict) and "points" in writer:
            writer["points"].extend(new_points)
    with open(config_path, "w") as f:
        json.dump(cfg, f)


def remove_writer_section(config_path, section_name):
    """删除整个 Writer section 并从 c4_shm_manager.writer 列表中移除。

    同时清理对应的 reader keys（key 前缀匹配此 section 的 device id）。
    """
    with open(config_path, "r") as f:
        cfg = json.load(f)
    # 获取此 writer 的 device id 用于清理 reader keys
    device_id = None
    if section_name in cfg:
        for writer in cfg[section_name]:
            if isinstance(writer, dict) and "id" in writer:
                device_id = writer["id"]
                break
    # 从 writer 列表移除
    if "c4_shm_manager" in cfg:
        writers = cfg["c4_shm_manager"].get("writer", [])
        cfg["c4_shm_manager"]["writer"] = [
            w for w in writers if w != section_name
        ]
    # 删除 writer section
    if section_name in cfg:
        del cfg[section_name]
    # 清理对应的 reader keys
    if device_id is not None:
        for reader_section in cfg.get("c4_asfp2_client", []):
            if isinstance(reader_section, dict) and "points" in reader_section:
                reader_section["points"] = [
                    rp for rp in reader_section["points"]
                    if not rp.get("key", "").startswith(f"{device_id}.")
                ]
    with open(config_path, "w") as f:
        json.dump(cfg, f)


# ══════════════════════════════════════════════════
#  roots/list 回调
# ══════════════════════════════════════════════════


def _roots_callback(roots_list):
    """返回处理 roots/list 请求的回调函数。"""
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


def _get_shm_id_map(config_path, service_id):
    """从配置文件读取指定 service 的 {point_id: shm_id} 映射。"""
    cfg = _read_config(config_path)
    pts = cfg[service_id][0]["points"]
    return {p["id"]: p["shm_id"] for p in pts}


def _get_shm_ids_from_config(config_path, service_id):
    """从配置文件读取指定 service 所有 point 的 shm_id 列表。"""
    return list(_get_shm_id_map(config_path, service_id).values())


# ══════════════════════════════════════════════════
#  分支 A：纯删除回收
# ══════════════════════════════════════════════════


class TestPureReclaim:
    """TC1–TC3：删除采集点触发孤儿 block 回收。"""

    def test_tc1_remove_some_points(self, mcp, isolated_shm):
        """TC1: 删除部分采集点 — 5 点中删除 3 点，孤儿 block 回收到 state=0。

        README §4.1 TC1 — 全部 4 个验证步骤。
        """
        iid = "test_tc1"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=5)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            # 模拟 Writer 激活：block[1..5] state=1
            for sid in range(1, 6):
                set_block_state(path, sid, 1)
            set_header_point_count(path, 5)

            # 操作：删除 p3、p4、p5
            remove_points_from_config(
                config_path, "c4_modbus_client", ["p3", "p4", "p5"],
            )

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 1：保留的 block state=1
            assert read_shm_block(path, 1)["state"] == 1, "block[1].state must be 1"
            assert read_shm_block(path, 2)["state"] == 1, "block[2].state must be 1"

            # 验证 2：孤儿回收 — block[3..5] state=0
            assert read_shm_block(path, 3)["state"] == 0, (
                "block[3].state must be 0 (orphan reclaimed)"
            )
            assert read_shm_block(path, 4)["state"] == 0, (
                "block[4].state must be 0 (orphan reclaimed)"
            )
            assert read_shm_block(path, 5)["state"] == 0, (
                "block[5].state must be 0 (orphan reclaimed)"
            )

            # 验证 3：Header — point_count=2, max_points=10, remap_version=0
            h = read_shm_header(path)
            assert h["point_count"] == 2, f"point_count: {h['point_count']}"
            assert h["max_points"] == 10, f"max_points: {h['max_points']}"
            assert h["remap_version"] == 0, f"remap_version: {h['remap_version']}"

            # 验证 4：配置回填 — p1→1, p2→2；p3,p4,p5 已移除
            shm_map = _get_shm_id_map(config_path, "c4_modbus_client")
            assert shm_map.get("p1") == 1, f"p1 shm_id: {shm_map.get('p1')}"
            assert shm_map.get("p2") == 2, f"p2 shm_id: {shm_map.get('p2')}"
            assert "p3" not in shm_map, "p3 should be removed from config"
            assert "p4" not in shm_map, "p4 should be removed from config"
            assert "p5" not in shm_map, "p5 should be removed from config"

        finally:
            os.unlink(config_path)

    def test_tc2_remove_entire_writer(self, mcp, isolated_shm):
        """TC2: 删除整个 Writer — modbus 4 点 + iec104 3 点，删除 iec104。

        README §4.1 TC2 — max=14，all 7 blocks state=1 → iec104 orphan reclaimed。
        """
        iid = "test_tc2"
        isolated_shm(iid)

        config = _make_multi_writer_config()
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)

            # 验证 create_shm 后的分配
            modbus_ids_before = _get_shm_ids_from_config(
                config_path, "c4_modbus_client",
            )
            iec104_ids = _get_shm_ids_from_config(
                config_path, "c4_iec104_client",
            )
            assert sorted(modbus_ids_before) == [1, 2, 3, 4], (
                f"modbus shm_ids: {sorted(modbus_ids_before)}"
            )
            assert sorted(iec104_ids) == [5, 6, 7], (
                f"iec104 shm_ids: {sorted(iec104_ids)}"
            )

            # 模拟 Writer 激活：所有 7 个 block state=1
            for sid in range(1, 8):
                set_block_state(path, sid, 1)
            set_header_point_count(path, 7)

            # 操作：删除 c4_iec104_client section
            remove_writer_section(config_path, "c4_iec104_client")

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 1：modbus block[1..4] state=1（保留）
            for sid in range(1, 5):
                assert read_shm_block(path, sid)["state"] == 1, (
                    f"block[{sid}].state must be 1 (retained modbus)"
                )

            # 验证 2：iec104 block[5..7] state=0（回收）
            for sid in range(5, 8):
                assert read_shm_block(path, sid)["state"] == 0, (
                    f"block[{sid}].state must be 0 (orphan iec104 reclaimed)"
                )

            # 验证 3：Header — point_count=4, remap_version=0
            h = read_shm_header(path)
            assert h["point_count"] == 4, f"point_count: {h['point_count']}"
            assert h["remap_version"] == 0, f"remap_version: {h['remap_version']}"

            # 验证 4：配置回填 — modbus shm_ids 1..4 不变
            modbus_ids_after = _get_shm_ids_from_config(
                config_path, "c4_modbus_client",
            )
            assert modbus_ids_after == modbus_ids_before, (
                f"modbus shm_ids changed: {modbus_ids_before} → {modbus_ids_after}"
            )

            # iec104 section 已从 config 中完全移除
            cfg = _read_config(config_path)
            assert "c4_iec104_client" not in cfg
            assert "c4_iec104_client" not in cfg["c4_shm_manager"]["writer"]

        finally:
            os.unlink(config_path)

    def test_tc3_reclaim_and_reuse(self, mcp, isolated_shm):
        """TC3: 回收 + 新增点复用 — 删除 3 点再加 2 新点。

        README §4.1 TC3 — 回收 block[3..5]，新点 p6→3, p7→4 复用空闲 block。
        """
        iid = "test_tc3"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=5)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            for sid in range(1, 6):
                set_block_state(path, sid, 1)
            set_header_point_count(path, 5)

            # 操作：删除 p3,p4,p5 + 添加 p6,p7
            remove_points_from_config(
                config_path, "c4_modbus_client", ["p3", "p4", "p5"],
            )
            add_points_to_config(
                config_path, "c4_modbus_client",
                [
                    {"id": "p6", "uid": 1, "addr": 40006, "shm_id": 0},
                    {"id": "p7", "uid": 1, "addr": 40007, "shm_id": 0},
                ],
            )

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 1：block[3..5] state=0（回收的 3 个）
            for sid in range(3, 6):
                assert read_shm_block(path, sid)["state"] == 0, (
                    f"block[{sid}].state must be 0 (orphan reclaimed)"
                )

            # 验证 2：新点 p6→3, p7→4（从回收的 block 中复用）
            shm_map = _get_shm_id_map(config_path, "c4_modbus_client")
            assert shm_map.get("p6") == 3, f"p6 shm_id: {shm_map.get('p6')} (expected 3)"
            assert shm_map.get("p7") == 4, f"p7 shm_id: {shm_map.get('p7')} (expected 4)"

            # 验证 3：保留点 block[1] state=1, block[2] state=1
            assert read_shm_block(path, 1)["state"] == 1, "block[1].state must be 1"
            assert read_shm_block(path, 2)["state"] == 1, "block[2].state must be 1"

            # 验证 4：Header — point_count=4, max_points=10, remap_version=0
            h = read_shm_header(path)
            assert h["point_count"] == 4, f"point_count: {h['point_count']}"
            assert h["max_points"] == 10, f"max_points: {h['max_points']}"
            assert h["remap_version"] == 0, f"remap_version: {h['remap_version']}"

            # 验证 5：配置回填 — p6→3, p7→4（已在验证 2 中覆盖）
            assert shm_map.get("p1") == 1
            assert shm_map.get("p2") == 2

        finally:
            os.unlink(config_path)


# ══════════════════════════════════════════════════
#  分支 B：无删除（幂等性）
# ══════════════════════════════════════════════════


class TestNoDelete:
    """TC4–TC5：无删除场景 — 幂等性和边界情况。"""

    def test_tc4_idempotent_no_change(self, mcp, isolated_shm):
        """TC4: 无删除 → 无回收 — 不修改 config 调用 adjust_shm。

        README §4.2 TC4。
        """
        iid = "test_tc4"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=3)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            for sid in range(1, 4):
                set_block_state(path, sid, 1)
            set_header_point_count(path, 3)

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 1：block[1..3] state=1（全部保持）
            for sid in range(1, 4):
                assert read_shm_block(path, sid)["state"] == 1, (
                    f"block[{sid}].state must be 1"
                )

            # 验证 2：Header — point_count=3（不变），remap_version=0
            h = read_shm_header(path)
            assert h["point_count"] == 3, f"point_count: {h['point_count']}"
            assert h["remap_version"] == 0, f"remap_version: {h['remap_version']}"

        finally:
            os.unlink(config_path)

    def test_tc5_clear_all_writer_points(self, mcp, isolated_shm):
        """TC5: 清空 modbus points 数组 → 全量回收。

        required_points=0，但 writer/reader 列表非空，走正常回收路径：
        所有 state=1 block 均为孤儿 → 全部回收，point_count→0，返回 success。
        """
        iid = "test_tc5"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=3)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            for sid in range(1, 4):
                set_block_state(path, sid, 1)
            set_header_point_count(path, 3)

            # 清空所有 writer points 和 reader keys
            cfg = _read_config(config_path)
            cfg["c4_modbus_client"][0]["points"] = []
            cfg["c4_asfp2_client"][0]["points"] = []
            with open(config_path, "w") as f:
                json.dump(cfg, f)

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 全量回收验证
            h = read_shm_header(path)
            assert h["point_count"] == 0, (
                f"after clearing all points: point_count should be 0, got {h['point_count']}"
            )
            for sid in range(1, 4):
                assert read_shm_block(path, sid)["state"] == 0, (
                    f"block[{sid}] should be reclaimed (state=0)"
                )

        finally:
            os.unlink(config_path)


# ══════════════════════════════════════════════════
#  分支 C：状态一致性
# ══════════════════════════════════════════════════


class TestStateConsistency:
    """TC6–TC11：回收后 Header、Block、配置回填等状态一致性验证。"""

    def test_tc6_header_consistency_after_remove(self, mcp, isolated_shm):
        """TC6: 回收后 Header 状态一致性 — 完整字段验证。

        README §4.3 TC6 — 通过 read_shm_header 直接读取，不使用 query_status。
        """
        iid = "test_tc6"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=6)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            for sid in range(1, 7):
                set_block_state(path, sid, 1)
            set_header_point_count(path, 6)

            remove_points_from_config(
                config_path, "c4_modbus_client", ["p4", "p5", "p6"],
            )

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证：read_shm_header 返回完整字段
            h = read_shm_header(path)
            assert h["magic"] == MAGIC, f"magic: {hex(h['magic'])} (expected {hex(MAGIC)})"
            assert h["version"] == 1, f"version: {h['version']} (expected 1)"
            assert h["point_count"] == 3, f"point_count: {h['point_count']} (expected 3)"
            assert h["max_points"] == 12, f"max_points: {h['max_points']} (expected 12)"
            assert h["remap_version"] == 0, f"remap_version: {h['remap_version']} (expected 0)"

        finally:
            os.unlink(config_path)

    def test_tc7_config_writeback_shm_id_unchanged(self, mcp, isolated_shm):
        """TC7: 回收后配置回填 — 保留点的 shm_id 不变。

        README §4.3 TC7 — p5 保持 shm_id=5（不重新编号），reader shm_ids 匹配。
        """
        iid = "test_tc7"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=5)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)

            # 捕获 create_shm 回填后的 p1 字段（用于非 shm_id 字段不变性验证）
            before_cfg = _read_config(config_path)
            p1_before = None
            for wp in before_cfg["c4_modbus_client"][0]["points"]:
                if wp["id"] == "p1":
                    p1_before = dict(wp)
                    break
            assert p1_before is not None, "p1 must exist after create_shm"

            for sid in range(1, 6):
                set_block_state(path, sid, 1)
            set_header_point_count(path, 5)

            # 操作：删除 p3,p4（2 点），保留 p1,p2,p5
            remove_points_from_config(
                config_path, "c4_modbus_client", ["p3", "p4"],
            )

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 1：保留点的 shm_id 不变 — p5 保持 5（不重新编号为 3）
            shm_map = _get_shm_id_map(config_path, "c4_modbus_client")
            assert shm_map.get("p1") == 1, f"p1 shm_id: {shm_map.get('p1')}"
            assert shm_map.get("p2") == 2, f"p2 shm_id: {shm_map.get('p2')}"
            assert shm_map.get("p5") == 5, (
                f"p5 shm_id: {shm_map.get('p5')} (must stay 5, not renumbered to 3)"
            )

            # 验证 2：Reader shm_ids 匹配保留点的 Writer shm_ids
            after = _read_config(config_path)
            reader_pts = after["c4_asfp2_client"][0]["points"]
            writer_pts = after["c4_modbus_client"][0]["points"]
            writer_by_id = {wp["id"]: wp["shm_id"] for wp in writer_pts}
            for rp in reader_pts:
                key = rp["key"]
                # key 格式: device1.pX → point id 为 pX
                pid = key[len("device1."):]
                assert pid in writer_by_id, f"reader key '{key}' has no writer point"
                assert rp["shm_id"] == writer_by_id[pid], (
                    f"reader '{key}' shm_id={rp['shm_id']} != writer {writer_by_id[pid]}"
                )

            # 验证 3：非 shm_id 字段未被修改
            after = _read_config(config_path)
            for wp in after["c4_modbus_client"][0]["points"]:
                if wp["id"] == "p1":
                    assert wp["uid"] == p1_before["uid"], (
                        f"p1 uid changed: {p1_before['uid']} → {wp['uid']}"
                    )
                    assert wp["addr"] == p1_before["addr"], (
                        f"p1 addr changed: {p1_before['addr']} → {wp['addr']}"
                    )

            # p3,p4 已不在 config 中
            assert "p3" not in shm_map, "p3 should be removed from config"
            assert "p4" not in shm_map, "p4 should be removed from config"

        finally:
            os.unlink(config_path)

    def test_tc8_block_integrity_after_remove(self, mcp, isolated_shm):
        """TC8: 回收后 block 完整性 — 回收的 block state=0，magic 不变。

        README §4.3 TC8 — 回收的 block[4..5] state=0（不是 "state 不变"），
        保留的 block[1..3] magic/state=1/reserved/type 不变。
        """
        iid = "test_tc8"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=5)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            for sid in range(1, 6):
                set_block_state(path, sid, 1)
            set_header_point_count(path, 5)

            # 捕获回收前的 block[1..3] 状态
            blocks_before = {}
            for sid in range(1, 4):
                blocks_before[sid] = read_shm_block(path, sid)

            remove_points_from_config(
                config_path, "c4_modbus_client", ["p4", "p5"],
            )

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 1：回收的 block[4..5] — state=0, magic=0xC4DA7A00 不变
            for sid in (4, 5):
                b = read_shm_block(path, sid)
                assert b["state"] == 0, (
                    f"block[{sid}].state must be 0 (reclaimed), got {b['state']}"
                )
                assert b["magic"] == MAGIC, (
                    f"block[{sid}].magic must be {hex(MAGIC)}, got {hex(b['magic'])}"
                )

            # 验证 2：保留的 block[1..3] — magic、state=1、reserved、type 均不变
            for sid in range(1, 4):
                after = read_shm_block(path, sid)
                assert after["state"] == 1, (
                    f"block[{sid}].state must be 1 (retained), got {after['state']}"
                )
                assert after["magic"] == MAGIC, (
                    f"block[{sid}].magic changed: {hex(after['magic'])}"
                )
                assert after["reserved"] == blocks_before[sid]["reserved"], (
                    f"block[{sid}].reserved changed: "
                    f"{blocks_before[sid]['reserved']} → {after['reserved']}"
                )
                assert after["type"] == blocks_before[sid]["type"], (
                    f"block[{sid}].type changed: "
                    f"{blocks_before[sid]['type']} → {after['type']}"
                )

            # 验证 3：空闲 block[6..10] — magic=0xC4DA7A00, state=0（不受回收影响）
            for sid in range(6, 11):
                b = read_shm_block(path, sid)
                assert b["magic"] == MAGIC, (
                    f"block[{sid}].magic must be {hex(MAGIC)}, got {hex(b['magic'])}"
                )
                assert b["state"] == 0, (
                    f"block[{sid}].state must be 0 (free), got {b['state']}"
                )

        finally:
            os.unlink(config_path)

    def test_tc9_partial_writer_activation(self, mcp, isolated_shm):
        """TC9: 部分 Writer 激活 — 仅部分 block state=1，回收仅扫描活跃块。

        README §4.3 TC9 — 删除 p1（shm_id=1），
        block[1] 从 state=1 → 0（孤儿回收），
        block[3]/[5] 保持 state=1（保留），
        block[2]/[4] state=0 不变（从未激活不受影响）。
        """
        iid = "test_tc9"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=5)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)

            # 仅设置 block[1], block[3], block[5] 为 state=1
            # 不调用 set_header_point_count — point_count 保持 create_shm 后的值 5
            set_block_state(path, 1, 1)
            set_block_state(path, 3, 1)
            set_block_state(path, 5, 1)

            # 验证前置：point_count 仍为 5
            h_before = read_shm_header(path)
            assert h_before["point_count"] == 5, (
                f"point_count before should be 5, got {h_before['point_count']}"
            )

            # 操作：删除 p1 (shm_id=1)，total=4
            # required_points(4) < point_count(5) → 触发回收
            remove_points_from_config(
                config_path, "c4_modbus_client", ["p1"],
            )

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 1：block[1] state=0（p1 被删除，孤儿回收）
            assert read_shm_block(path, 1)["state"] == 0, (
                "block[1].state must be 0 (p1 deleted, orphan reclaimed)"
            )

            # 验证 2：保留的活跃 block[3], block[5] state=1
            assert read_shm_block(path, 3)["state"] == 1, (
                "block[3].state must be 1 (retained active point)"
            )
            assert read_shm_block(path, 5)["state"] == 1, (
                "block[5].state must be 1 (retained active point)"
            )

            # 验证 3：block[2], block[4] state=0（从未激活的点不受影响）
            assert read_shm_block(path, 2)["state"] == 0, (
                "block[2].state must be 0 (never activated, unaffected)"
            )
            assert read_shm_block(path, 4)["state"] == 0, (
                "block[4].state must be 0 (never activated, unaffected)"
            )

            # 验证 4：Header — point_count=4, remap_version=0
            h = read_shm_header(path)
            assert h["point_count"] == 4, f"point_count: {h['point_count']} (expected 4)"
            assert h["remap_version"] == 0, f"remap_version: {h['remap_version']}"

        finally:
            os.unlink(config_path)

    def test_tc10_single_point_removal(self, mcp, isolated_shm):
        """TC10: 单点删除边界 — 仅删除 1 点，block[6] state=0。

        README §4.3 TC10 — 最小删除单位，验证单个点删除触发正确回收。
        """
        iid = "test_tc10"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=6)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            for sid in range(1, 7):
                set_block_state(path, sid, 1)
            set_header_point_count(path, 6)

            # 操作：删除 p6（1 点）
            remove_points_from_config(
                config_path, "c4_modbus_client", ["p6"],
            )

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 1：block[6] state=0（p6 被回收）
            assert read_shm_block(path, 6)["state"] == 0, (
                "block[6].state must be 0 (p6 reclaimed)"
            )

            # 验证 2：block[1..5] state=1（保留）
            for sid in range(1, 6):
                assert read_shm_block(path, sid)["state"] == 1, (
                    f"block[{sid}].state must be 1 (retained)"
                )

            # 验证 3：Header — point_count=5, remap_version=0
            h = read_shm_header(path)
            assert h["point_count"] == 5, f"point_count: {h['point_count']} (expected 5)"
            assert h["remap_version"] == 0, f"remap_version: {h['remap_version']}"

        finally:
            os.unlink(config_path)

    def test_tc11_sequential_removal(self, mcp, isolated_shm):
        """TC11: 序列回收 — 两次 adjust_shm，递进删除。

        README §4.3 TC11 — 第一次删除 p4,p5，第二次删除 p2,p3。
        验证第二次在已有回收结果的 shm 上正确识别新增孤儿。
        """
        iid = "test_tc11"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=5)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            path = shm_path(iid)
            for sid in range(1, 6):
                set_block_state(path, sid, 1)
            set_header_point_count(path, 5)

            # ── 操作 1：删除 p4,p5 ──
            remove_points_from_config(
                config_path, "c4_modbus_client", ["p4", "p5"],
            )

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # 验证 1a：block[4..5] state=0（第一次回收）
            assert read_shm_block(path, 4)["state"] == 0, (
                "block[4].state must be 0 after 1st reclaim"
            )
            assert read_shm_block(path, 5)["state"] == 0, (
                "block[5].state must be 0 after 1st reclaim"
            )
            # 验证 1a：block[1..3] state=1（保留）
            for sid in range(1, 4):
                assert read_shm_block(path, sid)["state"] == 1, (
                    f"block[{sid}].state must be 1 after 1st reclaim"
                )
            # 验证 1a：point_count=3
            h1 = read_shm_header(path)
            assert h1["point_count"] == 3, (
                f"point_count after 1st: {h1['point_count']} (expected 3)"
            )

            # ── 操作 2：再删除 p2,p3 ──
            remove_points_from_config(
                config_path, "c4_modbus_client", ["p2", "p3"],
            )

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            # 验证 2a：返回 success
            _assert_mcp_success(resp)

            # 验证 2b：block[2..3] state=0（第二次回收）
            assert read_shm_block(path, 2)["state"] == 0, (
                "block[2].state must be 0 after 2nd reclaim"
            )
            assert read_shm_block(path, 3)["state"] == 0, (
                "block[3].state must be 0 after 2nd reclaim"
            )

            # 验证 2b：block[1] state=1（保留）
            assert read_shm_block(path, 1)["state"] == 1, (
                "block[1].state must be 1 (only retained point)"
            )

            # 验证 2b：block[4..5] state=0（第一次回收结果不变）
            assert read_shm_block(path, 4)["state"] == 0, (
                "block[4].state must be 0 (preserved from 1st reclaim)"
            )
            assert read_shm_block(path, 5)["state"] == 0, (
                "block[5].state must be 0 (preserved from 1st reclaim)"
            )

            # 验证 2c：Header — point_count=1, remap_version=0
            h2 = read_shm_header(path)
            assert h2["point_count"] == 1, (
                f"point_count after 2nd: {h2['point_count']} (expected 1)"
            )
            assert h2["remap_version"] == 0, f"remap_version: {h2['remap_version']}"

        finally:
            os.unlink(config_path)


# ══════════════════════════════════════════════════
#  分支 D：错误路径
# ══════════════════════════════════════════════════


class TestErrorPaths:
    """TC12–TC14：SHM 未创建、roots/list 失败、writer 为空。"""

    def test_tc12_shm_not_created(self, mcp, isolated_shm):
        """TC12: SHM 未创建 → SHM_NOT_CREATED。

        README §4.4 TC12 — 不调用 create_shm，直接 adjust_shm。
        """
        iid = "test_tc12"
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

    def test_tc13_roots_list_failure(self, mcp, isolated_shm):
        """TC13: roots/list 失败 → CONFIG_PATH_MISSING。

        README §4.4 TC13 — adjust_shm 时 Python 对 roots/list 返回 MCP 错误。
        """
        iid = "test_tc13"
        isolated_shm(iid)

        config = _make_initial_config(writer_points=2)
        config_path = _make_config_file(config)

        try:
            resp = mcp.call_tool(
                "create_shm", {"instance_id": iid},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_success(resp)

            # adjust_shm 时 roots/list 返回错误
            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_error_callback(),
            )
            _assert_mcp_error(resp, "CONFIG_PATH_MISSING")

        finally:
            os.unlink(config_path)

    def test_tc14_empty_writer_list(self, mcp, isolated_shm):
        """TC14: c4_shm_manager.writer=[] → CONFIG_MISSING_SECTION。

        README §4.4 TC14 — writer 列表为空。
        """
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

            # 操作：c4_shm_manager.writer = []
            cfg = _read_config(config_path)
            cfg["c4_shm_manager"]["writer"] = []
            with open(config_path, "w") as f:
                json.dump(cfg, f)

            resp = mcp.call_tool(
                "adjust_shm", {},
                on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
            )
            _assert_mcp_error(resp, "CONFIG_MISSING_SECTION")

        finally:
            os.unlink(config_path)
