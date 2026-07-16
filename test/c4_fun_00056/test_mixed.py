"""
C4_FUN_00056 测试用例 — adjust_shm 混合操作（同时回收与再分配）

验证 c4_shm_manager adjust_shm 工具在配置文件既删除旧点又新增点时的混合行为：
  TC1:  等量置换（删除3点+新增3点，无扩容）
  TC2:  净增（删除3点+新增5点，无扩容）
  TC3:  净减（删除6点+新增4点，无扩容）
  TC4:  混合扩容（删除3点+新增15点，触发扩容）
  TC5:  单点置换（删除1点+新增1点）
  TC6:  全量回收（writer/reader 均为空）
  TC7:  全量回收后重新分配
  TC8:  扩容后混合（先扩容，再混合操作）
  TC9:  已有点完全不受影响（交叉验证）
  TC10: Block 完整性验证
  TC11: 配置回填验证
  TC12: SHM_NOT_CREATED 错误
  TC13: CONFIG_PATH_MISSING 错误
  TC14: CONFIG_MISSING_SECTION 错误（一方为空）
  TC15: DUPLICATE_KEY 错误
  TC16: UNKNOWN_READER_KEY 错误

所有验证均通过直接读取共享内存完成，不使用 query_status 工具。
Writer 激活通过 mmap 直接写入 block state 字节模拟。

配置格式（Go 二进制期望的格式）：
  c4_shm_manager.writer — 字符串数组，每项为顶层 key
  c4_shm_manager.reader — 字符串数组，每项为顶层 key
  每个 writer/reader section 是数组，每项含 id 和 points
  Reader key 格式: {device_id}.{point_id}
"""

import json
import mmap
import os
import struct
import tempfile
import uuid

import pytest

from shm_helpers import read_shm_block, read_shm_header, shm_path

MAGIC = 0xC4DA7A00


# ══════════════════════════════════════════════════
#  模拟 Writer 激活（直接写入共享内存）
# ══════════════════════════════════════════════════


def set_block_state(shm_full_path, shm_id, state):
    """设置 block[shm_id] 的 state 字段（偏移 = shm_id * 32 + 4）。"""
    offset = shm_id * 32 + 4
    fd = None
    s = None
    try:
        fd = os.open(shm_full_path, os.O_RDWR)
        s = mmap.mmap(fd, offset + 1, mmap.MAP_SHARED,
                      prot=mmap.PROT_READ | mmap.PROT_WRITE)
        s.seek(offset)
        s.write(bytes([state]))
    finally:
        if s is not None:
            s.close()
        if fd is not None:
            os.close(fd)


def set_header_point_count(shm_full_path, count):
    """设置 Header 的 point_count 字段（偏移 = 8，大端 uint32）。"""
    fd = None
    s = None
    try:
        fd = os.open(shm_full_path, os.O_RDWR)
        s = mmap.mmap(fd, 16, mmap.MAP_SHARED,
                      prot=mmap.PROT_READ | mmap.PROT_WRITE)
        s[8:12] = struct.pack(">I", count)
    finally:
        if s is not None:
            s.close()
        if fd is not None:
            os.close(fd)


def activate_blocks_from_config(shm_full_path, config_path):
    """读取配置中所有已分配 shm_id，设置对应 block state=1（模拟 Writer 激活）。

    在 adjust_shm 后调用，将包括复用回收 block 的所有已分配 block 设为活跃。
    """
    with open(config_path) as f:
        cfg = json.load(f)
    assigned = set()
    sm = cfg.get("c4_shm_manager", {})
    for w_type in sm.get("writer", []):
        if w_type not in cfg:
            continue
        for inst in cfg[w_type]:
            for pt in inst.get("points", []):
                sid = pt.get("shm_id", 0)
                if sid > 0:
                    assigned.add(int(sid))
    for sid in assigned:
        set_block_state(shm_full_path, sid, 1)


# ══════════════════════════════════════════════════
#  配置工厂 & 修改辅助
# ══════════════════════════════════════════════════


def build_initial_config():
    """构造初始配置 dict（4 个 writer + 1 个 reader，10 点，shm_id=0）。

    writer1 (device w1):  2 点 → 期望 shm_id 1..2
    writer2 (device w2):  2 点 → 期望 shm_id 3..4
    writer3 (device w3):  3 点 → 期望 shm_id 5..7
    writer4 (device w4):  3 点 → 期望 shm_id 8..10
    """
    return {
        "c4_shm_manager": {
            "writer": ["writer1", "writer2", "writer3", "writer4"],
            "reader": ["reader1"],
        },
        "writer1": [{
            "id": "w1",
            "points": [
                {"id": "p1", "shm_id": 0},
                {"id": "p2", "shm_id": 0},
            ],
        }],
        "writer2": [{
            "id": "w2",
            "points": [
                {"id": "p1", "shm_id": 0},
                {"id": "p2", "shm_id": 0},
            ],
        }],
        "writer3": [{
            "id": "w3",
            "points": [
                {"id": "p1", "shm_id": 0},
                {"id": "p2", "shm_id": 0},
                {"id": "p3", "shm_id": 0},
            ],
        }],
        "writer4": [{
            "id": "w4",
            "points": [
                {"id": "p1", "shm_id": 0},
                {"id": "p2", "shm_id": 0},
                {"id": "p3", "shm_id": 0},
            ],
        }],
        "reader1": [{
            "points": [
                {"key": "w1.p1", "shm_id": 0},
                {"key": "w1.p2", "shm_id": 0},
            ],
        }],
    }


def _read_config(path):
    """读取 JSON 配置文件，返回 dict。"""
    with open(path, "r") as f:
        return json.load(f)


def _write_config(path, cfg):
    """将配置 dict 写入 JSON 配置文件。"""
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)


def add_writer_section(cfg, writer_name, device_id, points):
    """向配置 dict 添加新的 writer top-level section。

    更新 c4_shm_manager.writer 列表并创建新的 writer section。
    """
    if writer_name not in cfg["c4_shm_manager"]["writer"]:
        cfg["c4_shm_manager"]["writer"].append(writer_name)
    cfg[writer_name] = [{
        "id": device_id,
        "points": [dict(p) for p in points],
    }]


def remove_writer_section(cfg, writer_name):
    """从配置 dict 中删除 writer section 并从 writer 列表移除。"""
    cfg["c4_shm_manager"]["writer"] = [
        w for w in cfg["c4_shm_manager"]["writer"] if w != writer_name
    ]
    cfg.pop(writer_name, None)


def get_writer_points_cfg(cfg, writer_name):
    """获取指定 writer 第一个 device 的 points 列表。"""
    return cfg[writer_name][0]["points"]


def get_reader_points_cfg(cfg, reader_name):
    """获取指定 reader 第一个 device 的 points 列表。"""
    return cfg[reader_name][0]["points"]


def collect_all_writer_shm_ids(cfg):
    """收集配置中所有 writer 的 shm_id 列表。"""
    ids = []
    for w_name in cfg["c4_shm_manager"]["writer"]:
        for pt in cfg[w_name][0]["points"]:
            ids.append(pt["shm_id"])
    return ids


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
    assert "error" not in resp, f"Expected success, got error: {resp.get('error')}"
    assert resp["result"].get("isError", False) is False, (
        f"Expected isError=False, got: {resp}"
    )
    text = resp["result"]["content"][0]["text"]
    assert text == "success", f"Expected 'success', got: '{text}'"


def _assert_mcp_error(resp, expected_prefix):
    """断言 MCP 响应为错误，且错误文本以 expected_prefix 开头。"""
    assert resp["result"]["isError"] is True, f"Expected isError=True, got: {resp}"
    text = resp["result"]["content"][0]["text"]
    assert text.startswith(expected_prefix), (
        f"Expected prefix '{expected_prefix}', got: '{text}'"
    )


# ══════════════════════════════════════════════════
#  通用测试流程
# ══════════════════════════════════════════════════


def _create_and_activate(mcp, isolated_shm, instance_id):
    """通用前置：create_shm + 激活所有 10 个 block state=1。

    Returns:
        (config_path, temp_dir): 配置文件路径和临时目录。
    """
    isolated_shm(instance_id)
    temp_dir = tempfile.mkdtemp(prefix="c4_test_")
    config_path = os.path.join(temp_dir, "config.json")

    cfg = build_initial_config()
    _write_config(config_path, cfg)

    on_request = _roots_callback([{"uri": f"file://{config_path}"}])
    resp = mcp.call_tool("create_shm", {"instance_id": instance_id},
                         on_request=on_request)
    _assert_mcp_success(resp)

    # 读回 backfill 后的配置，获取实际 shm_ids
    backfilled = _read_config(config_path)
    all_ids = collect_all_writer_shm_ids(backfilled)
    assert len(all_ids) == 10, (
        f"Expected 10 total shm_ids after create_shm, got {len(all_ids)}: {all_ids}"
    )
    assert sorted(all_ids) == list(range(1, 11)), (
        f"Expected shm_ids 1..10, got {sorted(all_ids)}"
    )

    # 激活所有 block state=1
    full_path = shm_path(instance_id)
    for sid in all_ids:
        set_block_state(full_path, sid, 1)
    set_header_point_count(full_path, len(all_ids))

    return config_path, temp_dir


# ══════════════════════════════════════════════════
#  测试用例
# ══════════════════════════════════════════════════


# ── TC1: 等量置换 ──

@pytest.mark.timeout(30)
def test_tc1_equal_swap(mcp, isolated_shm):
    """TC1: 删除 writer3（3 点）+ 新增 writer5（3 点）→ 等量置换。

    预期：point_count=10 不变，writer5 复用 writer3 的 block[5..7]。
    """
    iid = f"tc1_{uuid.uuid4().hex[:8]}"
    config_path, temp_dir = _create_and_activate(mcp, isolated_shm, iid)

    try:
        # 修改配置：删除 writer3，新增 writer5（3 点，shm_id=0）
        cfg = _read_config(config_path)
        remove_writer_section(cfg, "writer3")
        add_writer_section(cfg, "writer5", "w5", [
            {"id": "p1", "shm_id": 0},
            {"id": "p2", "shm_id": 0},
            {"id": "p3", "shm_id": 0},
        ])
        _write_config(config_path, cfg)

        # adjust_shm
        on_request = _roots_callback([{"uri": f"file://{config_path}"}])
        resp = mcp.call_tool("adjust_shm", {}, on_request=on_request)
        _assert_mcp_success(resp)

        activate_blocks_from_config(shm_path(iid), config_path)

        full_path = shm_path(iid)
        h = read_shm_header(full_path)
        assert h["point_count"] == 10, f"point_count: {h['point_count']}"
        assert h["max_points"] == 20, f"max_points: {h['max_points']}"
        assert h["remap_version"] == 0, f"remap_version: {h['remap_version']}"

        # writer1: block[1..2] state=1
        for sid in (1, 2):
            assert read_shm_block(full_path, sid)["state"] == 1, (
                f"block[{sid}].state must be 1 (writer1)"
            )

        # writer2: block[3..4] state=1
        for sid in (3, 4):
            assert read_shm_block(full_path, sid)["state"] == 1, (
                f"block[{sid}].state must be 1 (writer2)"
            )

        # writer5 复用 writer3 位置: block[5..7] state=1
        for sid in (5, 6, 7):
            assert read_shm_block(full_path, sid)["state"] == 1, (
                f"block[{sid}].state must be 1 (writer5 reused)"
            )

        # writer4: block[8..10] state=1
        for sid in (8, 9, 10):
            assert read_shm_block(full_path, sid)["state"] == 1, (
                f"block[{sid}].state must be 1 (writer4)"
            )

        # 空闲: block[11..20] state=0
        for sid in range(11, 21):
            assert read_shm_block(full_path, sid)["state"] == 0, (
                f"block[{sid}].state must be 0 (free)"
            )

        # 配置回填：writer5 的 shm_id 为 5,6,7
        backfilled = _read_config(config_path)
        w5_ids = [p["shm_id"] for p in get_writer_points_cfg(backfilled, "writer5")]
        assert w5_ids == [5, 6, 7], f"writer5 shm_ids: {w5_ids}"

        # writer3 已从配置移除
        assert "writer3" not in cfg["c4_shm_manager"]["writer"]
        assert "writer3" not in backfilled.get("c4_shm_manager", {}).get("writer", [])

    finally:
        os.unlink(config_path)
        os.rmdir(temp_dir)


# ── TC2: 净增 ──

@pytest.mark.timeout(30)
def test_tc2_net_increase(mcp, isolated_shm):
    """TC2: 删除 writer3（3 点）+ 新增 writer5（5 点）→ 净增 2 点。

    预期：required_points=12 ≤ max=20，writer5 的 5 点从 [5..7]+[11..12] 分配。
    """
    iid = f"tc2_{uuid.uuid4().hex[:8]}"
    config_path, temp_dir = _create_and_activate(mcp, isolated_shm, iid)

    try:
        cfg = _read_config(config_path)
        remove_writer_section(cfg, "writer3")
        add_writer_section(cfg, "writer5", "w5", [
            {"id": "p1", "shm_id": 0},
            {"id": "p2", "shm_id": 0},
            {"id": "p3", "shm_id": 0},
            {"id": "p4", "shm_id": 0},
            {"id": "p5", "shm_id": 0},
        ])
        _write_config(config_path, cfg)

        on_request = _roots_callback([{"uri": f"file://{config_path}"}])
        resp = mcp.call_tool("adjust_shm", {}, on_request=on_request)
        _assert_mcp_success(resp)

        activate_blocks_from_config(shm_path(iid), config_path)

        full_path = shm_path(iid)
        h = read_shm_header(full_path)
        assert h["point_count"] == 12, f"point_count: {h['point_count']}"
        assert h["max_points"] == 20, f"max_points: {h['max_points']}"
        assert h["remap_version"] == 0, f"remap_version: {h['remap_version']}"

        # block[5..7]: state=1（复用回收空间）
        for sid in (5, 6, 7):
            assert read_shm_block(full_path, sid)["state"] == 1, (
                f"block[{sid}].state must be 1 (reused)"
            )

        # block[11..12]: state=1（新分配）
        for sid in (11, 12):
            assert read_shm_block(full_path, sid)["state"] == 1, (
                f"block[{sid}].state must be 1 (new)"
            )

        # block[8..10]: state=1（writer4 不变）
        for sid in (8, 9, 10):
            assert read_shm_block(full_path, sid)["state"] == 1, (
                f"block[{sid}].state must be 1 (writer4)"
            )

        # block[13..20]: state=0（空闲）
        for sid in range(13, 21):
            assert read_shm_block(full_path, sid)["state"] == 0, (
                f"block[{sid}].state must be 0 (free)"
            )

        # 配置回填
        backfilled = _read_config(config_path)
        w5_ids = [p["shm_id"] for p in get_writer_points_cfg(backfilled, "writer5")]
        assert len(w5_ids) == 5, f"writer5 should have 5 points, got {w5_ids}"
        assert all(sid > 0 for sid in w5_ids), f"writer5 shm_ids: {w5_ids}"

    finally:
        os.unlink(config_path)
        os.rmdir(temp_dir)


# ── TC3: 净减 ──

@pytest.mark.timeout(30)
def test_tc3_net_decrease(mcp, isolated_shm):
    """TC3: 删除 writer3+writer4（6 点）+ 新增 writer5（4 点）→ 净减 2 点。

    预期：required_points=8 ≤ max=20，writer5 的 4 点从 [5..8] 分配。
    """
    iid = f"tc3_{uuid.uuid4().hex[:8]}"
    config_path, temp_dir = _create_and_activate(mcp, isolated_shm, iid)

    try:
        cfg = _read_config(config_path)
        remove_writer_section(cfg, "writer3")
        remove_writer_section(cfg, "writer4")
        add_writer_section(cfg, "writer5", "w5", [
            {"id": "p1", "shm_id": 0},
            {"id": "p2", "shm_id": 0},
            {"id": "p3", "shm_id": 0},
            {"id": "p4", "shm_id": 0},
        ])
        _write_config(config_path, cfg)

        on_request = _roots_callback([{"uri": f"file://{config_path}"}])
        resp = mcp.call_tool("adjust_shm", {}, on_request=on_request)
        _assert_mcp_success(resp)

        activate_blocks_from_config(shm_path(iid), config_path)

        full_path = shm_path(iid)
        h = read_shm_header(full_path)
        assert h["point_count"] == 8, f"point_count: {h['point_count']}"
        assert h["max_points"] == 20, f"max_points: {h['max_points']}"
        assert h["remap_version"] == 0, f"remap_version: {h['remap_version']}"

        # block[5..8]: state=1（writer5 复用回收空间）
        for sid in (5, 6, 7, 8):
            assert read_shm_block(full_path, sid)["state"] == 1, (
                f"block[{sid}].state must be 1 (writer5)"
            )

        # block[9..10]: state=0（多余回收，保持空闲）
        for sid in (9, 10):
            assert read_shm_block(full_path, sid)["state"] == 0, (
                f"block[{sid}].state must be 0 (leftover reclaim)"
            )

        # block[1..4]: state=1（writer1,2 不变）
        for sid in (1, 2, 3, 4):
            assert read_shm_block(full_path, sid)["state"] == 1, (
                f"block[{sid}].state must be 1 (writer1/2)"
            )

        # 配置回填
        backfilled = _read_config(config_path)
        w5_ids = [p["shm_id"] for p in get_writer_points_cfg(backfilled, "writer5")]
        assert len(w5_ids) == 4, f"writer5 should have 4 points, got {w5_ids}"
        assert all(sid > 0 for sid in w5_ids), f"writer5 shm_ids: {w5_ids}"

    finally:
        os.unlink(config_path)
        os.rmdir(temp_dir)


# ── TC4: 混合扩容 ──

@pytest.mark.timeout(30)
def test_tc4_mixed_expand(mcp, isolated_shm):
    """TC4: 删除 writer3（3 点）+ 新增 writer5（15 点）→ 触发扩容。

    预期：required_points=22 > max=20，扩容至 44。remap_version++。
    """
    iid = f"tc4_{uuid.uuid4().hex[:8]}"
    config_path, temp_dir = _create_and_activate(mcp, isolated_shm, iid)

    try:
        cfg = _read_config(config_path)
        remove_writer_section(cfg, "writer3")
        add_writer_section(cfg, "writer5", "w5", [
            {"id": f"p{i}", "shm_id": 0} for i in range(1, 16)
        ])
        _write_config(config_path, cfg)

        on_request = _roots_callback([{"uri": f"file://{config_path}"}])
        resp = mcp.call_tool("adjust_shm", {}, on_request=on_request)
        _assert_mcp_success(resp)

        activate_blocks_from_config(shm_path(iid), config_path)

        full_path = shm_path(iid)
        h = read_shm_header(full_path)
        assert h["point_count"] == 22, f"point_count: {h['point_count']}"
        assert h["max_points"] == 44, f"max_points: {h['max_points']}"
        assert h["remap_version"] > 0, f"remap_version: {h['remap_version']}"

        # block[5..7]: state=1（复用回收）
        for sid in (5, 6, 7):
            assert read_shm_block(full_path, sid)["state"] == 1, (
                f"block[{sid}].state must be 1 (reused)"
            )

        # block[11..22]: state=1（部分从旧空闲 + 扩容空间）
        for sid in range(11, 23):
            assert read_shm_block(full_path, sid)["state"] == 1, (
                f"block[{sid}].state must be 1 (writer5)"
            )

        # block[1..4][8..10]: state=1（writer1,2,4 不变）
        for sid in (1, 2, 3, 4, 8, 9, 10):
            assert read_shm_block(full_path, sid)["state"] == 1, (
                f"block[{sid}].state must be 1 (unchanged)"
            )

        # 扩容区域 block[21..44]: magic=0xC4DA7A00
        for sid in range(21, 45):
            b = read_shm_block(full_path, sid)
            assert b["magic"] == MAGIC, (
                f"block[{sid}].magic: {hex(b['magic'])}"
            )

        # 配置回填
        backfilled = _read_config(config_path)
        w5_ids = [p["shm_id"] for p in get_writer_points_cfg(backfilled, "writer5")]
        assert len(w5_ids) == 15, f"writer5 should have 15 points, got {w5_ids}"
        assert all(sid > 0 for sid in w5_ids), f"writer5 shm_ids: {w5_ids}"

    finally:
        os.unlink(config_path)
        os.rmdir(temp_dir)


# ── TC5: 单点置换 ──

@pytest.mark.timeout(30)
def test_tc5_single_point_swap(mcp, isolated_shm):
    """TC5: 删除 writer1 的第 2 个点（block[2]）+ 新增 writer5（1 点）。

    预期：block[2] 被回收后复用给 writer5。
    """
    iid = f"tc5_{uuid.uuid4().hex[:8]}"
    config_path, temp_dir = _create_and_activate(mcp, isolated_shm, iid)

    try:
        cfg = _read_config(config_path)
        # 删除 writer1 中 shm_id=2 的点（即 p2）
        pts = get_writer_points_cfg(cfg, "writer1")
        pts = [p for p in pts if p["shm_id"] != 2]
        cfg["writer1"][0]["points"] = pts
        # 同步删除 reader 中对 w1.p2 的引用
        for rd in cfg.get("reader1", []):
            rpts = rd.get("points", [])
            rd["points"] = [p for p in rpts if p.get("key") != "w1.p2"]
        # 新增 writer5（1 点）
        add_writer_section(cfg, "writer5", "w5", [
            {"id": "p1", "shm_id": 0},
        ])
        _write_config(config_path, cfg)

        on_request = _roots_callback([{"uri": f"file://{config_path}"}])
        resp = mcp.call_tool("adjust_shm", {}, on_request=on_request)
        _assert_mcp_success(resp)

        activate_blocks_from_config(shm_path(iid), config_path)

        full_path = shm_path(iid)
        h = read_shm_header(full_path)
        assert h["point_count"] == 10, f"point_count: {h['point_count']}"
        assert h["max_points"] == 20, f"max_points: {h['max_points']}"
        assert h["remap_version"] == 0, f"remap_version: {h['remap_version']}"

        # block[1]: state=1（writer1 保留的点）
        assert read_shm_block(full_path, 1)["state"] == 1, (
            "block[1].state must be 1 (writer1 retained)"
        )

        # block[2]: state=1（writer5 新点复用）
        assert read_shm_block(full_path, 2)["state"] == 1, (
            "block[2].state must be 1 (writer5 reused)"
        )

        # block[3..10]: state=1（writer2,3,4 完全不变）
        for sid in range(3, 11):
            assert read_shm_block(full_path, sid)["state"] == 1, (
                f"block[{sid}].state must be 1 (unchanged)"
            )

        # block[11..20]: state=0（空闲）
        for sid in range(11, 21):
            assert read_shm_block(full_path, sid)["state"] == 0, (
                f"block[{sid}].state must be 0 (free)"
            )

        # 配置回填
        backfilled = _read_config(config_path)
        w5_ids = [p["shm_id"] for p in get_writer_points_cfg(backfilled, "writer5")]
        assert w5_ids == [2], f"writer5 shm_ids: {w5_ids}"

    finally:
        os.unlink(config_path)
        os.rmdir(temp_dir)


# ── TC6: 全量回收 ──

@pytest.mark.timeout(30)
def test_tc6_full_reclaim(mcp, isolated_shm):
    """TC6: writer=[] 且 reader=[]，所有 block 回收。

    预期：adjust_shm 返回 success（不返回 CONFIG_MISSING_SECTION）。
    """
    iid = f"tc6_{uuid.uuid4().hex[:8]}"
    config_path, temp_dir = _create_and_activate(mcp, isolated_shm, iid)

    try:
        cfg = _read_config(config_path)
        cfg["c4_shm_manager"]["writer"] = []
        cfg["c4_shm_manager"]["reader"] = []
        for w in ["writer1", "writer2", "writer3", "writer4", "reader1"]:
            cfg.pop(w, None)
        _write_config(config_path, cfg)

        on_request = _roots_callback([{"uri": f"file://{config_path}"}])
        resp = mcp.call_tool("adjust_shm", {}, on_request=on_request)
        _assert_mcp_success(resp)

        activate_blocks_from_config(shm_path(iid), config_path)

        full_path = shm_path(iid)
        h = read_shm_header(full_path)
        assert h["point_count"] == 0, f"point_count: {h['point_count']}"
        assert h["max_points"] == 20, f"max_points: {h['max_points']}"

        # 所有 block[1..10] state=0, magic 保持
        for sid in range(1, 11):
            b = read_shm_block(full_path, sid)
            assert b["state"] == 0, f"block[{sid}].state must be 0 (reclaimed)"
            assert b["magic"] == MAGIC, f"block[{sid}].magic: {hex(b['magic'])}"

        # block[11..20]: state=0（原空闲不变）
        for sid in range(11, 21):
            assert read_shm_block(full_path, sid)["state"] == 0, (
                f"block[{sid}].state must be 0 (free)"
            )

        # shm 文件仍存在
        assert os.path.exists(full_path), "shm file should still exist"

    finally:
        os.unlink(config_path)
        os.rmdir(temp_dir)


# ── TC7: 全量回收后重新分配 ──

@pytest.mark.timeout(30)
def test_tc7_reclaim_then_reallocate(mcp, isolated_shm):
    """TC7: TC6 后重新添加 writer5（5 点）。

    预期：writer5 的 5 点从 [1..5] 顺序分配。
    """
    iid = f"tc7_{uuid.uuid4().hex[:8]}"
    config_path, temp_dir = _create_and_activate(mcp, isolated_shm, iid)

    try:
        # 第一步：TC6 全量回收
        cfg = _read_config(config_path)
        cfg["c4_shm_manager"]["writer"] = []
        cfg["c4_shm_manager"]["reader"] = []
        for w in ["writer1", "writer2", "writer3", "writer4", "reader1"]:
            cfg.pop(w, None)
        _write_config(config_path, cfg)

        on_request = _roots_callback([{"uri": f"file://{config_path}"}])
        resp = mcp.call_tool("adjust_shm", {}, on_request=on_request)
        _assert_mcp_success(resp)

        activate_blocks_from_config(shm_path(iid), config_path)

        full_path = shm_path(iid)
        h = read_shm_header(full_path)
        assert h["point_count"] == 0, "after full reclaim: point_count should be 0"

        # 第二步：添加 writer5（5 点）并添加 reader 使双方非空
        cfg = _read_config(config_path)
        add_writer_section(cfg, "writer5", "w5", [
            {"id": f"p{i}", "shm_id": 0} for i in range(1, 6)
        ])
        # 添加 reader 使双方均非空
        cfg["c4_shm_manager"]["reader"] = ["reader1"]
        cfg["reader1"] = [{
            "points": [{"key": "w5.p1", "shm_id": 0}],
        }]
        _write_config(config_path, cfg)

        resp = mcp.call_tool("adjust_shm", {}, on_request=on_request)
        _assert_mcp_success(resp)

        activate_blocks_from_config(full_path, config_path)

        h = read_shm_header(full_path)
        assert h["point_count"] == 5, f"point_count: {h['point_count']}"
        assert h["max_points"] == 20, f"max_points: {h['max_points']}"

        # block[1..5]: state=1（全新分配）
        for sid in range(1, 6):
            assert read_shm_block(full_path, sid)["state"] == 1, (
                f"block[{sid}].state must be 1 (new)"
            )

        # block[6..20]: state=0
        for sid in range(6, 21):
            assert read_shm_block(full_path, sid)["state"] == 0, (
                f"block[{sid}].state must be 0 (free)"
            )

        # 配置回填：writer5 的 shm_ids = [1,2,3,4,5]
        backfilled = _read_config(config_path)
        w5_ids = [p["shm_id"] for p in get_writer_points_cfg(backfilled, "writer5")]
        assert w5_ids == [1, 2, 3, 4, 5], f"writer5 shm_ids: {w5_ids}"

    finally:
        os.unlink(config_path)
        os.rmdir(temp_dir)


# ── TC8: 扩容后混合 ──

@pytest.mark.timeout(30)
def test_tc8_post_expand_mixed(mcp, isolated_shm):
    """TC8: 第一次 adjust_shm 纯新增扩容，第二次混合操作。

    第一次：新增 writer5（15 点）→ 扩容至 max=50，point_count=25
    第二次：删除 writer3（3 点）+ 新增 writer6（8 点）
    """
    iid = f"tc8_{uuid.uuid4().hex[:8]}"
    config_path, temp_dir = _create_and_activate(mcp, isolated_shm, iid)

    try:
        on_request = _roots_callback([{"uri": f"file://{config_path}"}])

        # 第一次：纯新增 writer5（15 点）→ 触发扩容
        cfg = _read_config(config_path)
        add_writer_section(cfg, "writer5", "w5", [
            {"id": f"p{i}", "shm_id": 0} for i in range(1, 16)
        ])
        _write_config(config_path, cfg)

        resp = mcp.call_tool("adjust_shm", {}, on_request=on_request)
        _assert_mcp_success(resp)

        activate_blocks_from_config(shm_path(iid), config_path)

        full_path = shm_path(iid)
        h = read_shm_header(full_path)
        assert h["point_count"] == 25, (
            f"after 1st adjust: point_count={h['point_count']}"
        )
        assert h["max_points"] == 50, (
            f"after 1st adjust: max_points={h['max_points']}"
        )

        # 激活 writer5 新增的 block（shm_ids 从 backfill 获取）
        backfilled_1 = _read_config(config_path)
        w5_ids = [p["shm_id"] for p in get_writer_points_cfg(backfilled_1, "writer5")]
        for sid in w5_ids:
            set_block_state(full_path, sid, 1)
        set_header_point_count(full_path, 25)

        # 第二次：删除 writer3（3 点）+ 新增 writer6（8 点）
        cfg = _read_config(config_path)
        remove_writer_section(cfg, "writer3")
        add_writer_section(cfg, "writer6", "w6", [
            {"id": f"p{i}", "shm_id": 0} for i in range(1, 9)
        ])
        _write_config(config_path, cfg)

        resp = mcp.call_tool("adjust_shm", {}, on_request=on_request)
        _assert_mcp_success(resp)

        activate_blocks_from_config(full_path, config_path)

        h = read_shm_header(full_path)
        assert h["point_count"] == 30, (
            f"after 2nd adjust: point_count={h['point_count']}"
        )
        assert h["max_points"] == 50, (
            f"after 2nd adjust: max_points={h['max_points']} (no second expand)"
        )

        # block[5..7]: state=1（writer6 复用原 writer3 位置）
        for sid in (5, 6, 7):
            assert read_shm_block(full_path, sid)["state"] == 1, (
                f"block[{sid}].state must be 1 (writer6 reused)"
            )

        # writer1,2,4,5 的 block 不受影响
        backfilled_2 = _read_config(config_path)
        for w_name in ["writer1", "writer2", "writer4", "writer5"]:
            w_ids = [p["shm_id"] for p in get_writer_points_cfg(backfilled_2, w_name)]
            for sid in w_ids:
                assert read_shm_block(full_path, sid)["state"] == 1, (
                    f"block[{sid}].state must be 1 ({w_name})"
                )

        # 配置回填
        w6_ids = [p["shm_id"] for p in get_writer_points_cfg(backfilled_2, "writer6")]
        assert len(w6_ids) == 8, f"writer6 should have 8 points, got {w6_ids}"
        assert all(sid > 0 for sid in w6_ids), f"writer6 shm_ids: {w6_ids}"

    finally:
        os.unlink(config_path)
        os.rmdir(temp_dir)


# ── TC9: 已有点完全不受影响（交叉验证）──

@pytest.mark.timeout(30)
def test_tc9_retained_points_unchanged(mcp, isolated_shm):
    """TC9: 删除 writer2（2 点）+ 新增 writer5（2 点）。

    验证每个被保留的 writer 逐点不变。
    """
    iid = f"tc9_{uuid.uuid4().hex[:8]}"
    config_path, temp_dir = _create_and_activate(mcp, isolated_shm, iid)

    try:
        cfg = _read_config(config_path)
        # 记录未变更 writer 的 shm_ids
        w1_ids_before = [p["shm_id"] for p in get_writer_points_cfg(cfg, "writer1")]
        w3_ids_before = [p["shm_id"] for p in get_writer_points_cfg(cfg, "writer3")]
        w4_ids_before = [p["shm_id"] for p in get_writer_points_cfg(cfg, "writer4")]

        remove_writer_section(cfg, "writer2")
        add_writer_section(cfg, "writer5", "w5", [
            {"id": "p1", "shm_id": 0},
            {"id": "p2", "shm_id": 0},
        ])
        _write_config(config_path, cfg)

        on_request = _roots_callback([{"uri": f"file://{config_path}"}])
        resp = mcp.call_tool("adjust_shm", {}, on_request=on_request)
        _assert_mcp_success(resp)

        activate_blocks_from_config(shm_path(iid), config_path)

        full_path = shm_path(iid)
        backfilled = _read_config(config_path)

        # writer1: shm_ids 不变
        w1_ids_after = [p["shm_id"] for p in get_writer_points_cfg(backfilled, "writer1")]
        assert w1_ids_after == w1_ids_before, (
            f"writer1 shm_ids changed: {w1_ids_before} → {w1_ids_after}"
        )
        for sid in w1_ids_after:
            assert read_shm_block(full_path, sid)["state"] == 1, (
                f"writer1 block[{sid}].state must be 1"
            )

        # writer3: shm_ids 不变
        w3_ids_after = [p["shm_id"] for p in get_writer_points_cfg(backfilled, "writer3")]
        assert w3_ids_after == w3_ids_before, (
            f"writer3 shm_ids changed: {w3_ids_before} → {w3_ids_after}"
        )
        for sid in w3_ids_after:
            assert read_shm_block(full_path, sid)["state"] == 1, (
                f"writer3 block[{sid}].state must be 1"
            )

        # writer4: shm_ids 不变
        w4_ids_after = [p["shm_id"] for p in get_writer_points_cfg(backfilled, "writer4")]
        assert w4_ids_after == w4_ids_before, (
            f"writer4 shm_ids changed: {w4_ids_before} → {w4_ids_after}"
        )
        for sid in w4_ids_after:
            assert read_shm_block(full_path, sid)["state"] == 1, (
                f"writer4 block[{sid}].state must be 1"
            )

        # writer2 的 block[3..4] 被回收后复用给 writer5 → state=1
        for sid in (3, 4):
            assert read_shm_block(full_path, sid)["state"] == 1, (
                f"block[{sid}].state must be 1 (writer5 reused)"
            )

        # writer5 的新点从 [3..4]（刚回收）分配
        w5_ids = [p["shm_id"] for p in get_writer_points_cfg(backfilled, "writer5")]
        assert sorted(w5_ids) == [3, 4], f"writer5 shm_ids: {w5_ids}"
        for sid in w5_ids:
            assert read_shm_block(full_path, sid)["state"] == 1, (
                f"writer5 block[{sid}].state must be 1"
            )

    finally:
        os.unlink(config_path)
        os.rmdir(temp_dir)


# ── TC10: Block 完整性验证 ──

@pytest.mark.timeout(30)
def test_tc10_block_integrity(mcp, isolated_shm):
    """TC10: 删除 writer3（3 点）+ 新增 writer5（3 点）。

    对所有 block[1..20] 逐块验证 magic、state。
    """
    iid = f"tc10_{uuid.uuid4().hex[:8]}"
    config_path, temp_dir = _create_and_activate(mcp, isolated_shm, iid)

    try:
        cfg = _read_config(config_path)
        w1_ids = set(p["shm_id"] for p in get_writer_points_cfg(cfg, "writer1"))
        w2_ids = set(p["shm_id"] for p in get_writer_points_cfg(cfg, "writer2"))
        w4_ids = set(p["shm_id"] for p in get_writer_points_cfg(cfg, "writer4"))
        all_retained_ids = w1_ids | w2_ids | w4_ids

        remove_writer_section(cfg, "writer3")
        add_writer_section(cfg, "writer5", "w5", [
            {"id": "p1", "shm_id": 0},
            {"id": "p2", "shm_id": 0},
            {"id": "p3", "shm_id": 0},
        ])
        _write_config(config_path, cfg)

        on_request = _roots_callback([{"uri": f"file://{config_path}"}])
        resp = mcp.call_tool("adjust_shm", {}, on_request=on_request)
        _assert_mcp_success(resp)

        activate_blocks_from_config(shm_path(iid), config_path)

        full_path = shm_path(iid)
        backfilled = _read_config(config_path)
        w5_ids = set(p["shm_id"] for p in get_writer_points_cfg(backfilled, "writer5"))

        # 验证所有 block[1..20]
        for sid in range(1, 21):
            b = read_shm_block(full_path, sid)
            assert b["magic"] == MAGIC, f"block[{sid}].magic: {hex(b['magic'])}"

            if sid in all_retained_ids:
                assert b["state"] == 1, f"block[{sid}].state must be 1 (retained)"
            elif sid in w5_ids:
                assert b["state"] == 1, f"block[{sid}].state must be 1 (writer5)"
            else:
                assert b["state"] == 0, f"block[{sid}].state must be 0 (free)"

    finally:
        os.unlink(config_path)
        os.rmdir(temp_dir)


# ── TC11: 配置回填验证 ──

@pytest.mark.timeout(30)
def test_tc11_config_writeback(mcp, isolated_shm):
    """TC11: 删除 writer3（3 点）+ 新增 writer5（3 点）。

    验证配置回填：writer5 shm_ids 回填、已有 writer shm_ids 不变、
    writer3 已移除、无 shm_id=0 残留。
    """
    iid = f"tc11_{uuid.uuid4().hex[:8]}"
    config_path, temp_dir = _create_and_activate(mcp, isolated_shm, iid)

    try:
        cfg = _read_config(config_path)
        w1_ids_before = [p["shm_id"] for p in get_writer_points_cfg(cfg, "writer1")]
        w2_ids_before = [p["shm_id"] for p in get_writer_points_cfg(cfg, "writer2")]
        w4_ids_before = [p["shm_id"] for p in get_writer_points_cfg(cfg, "writer4")]

        remove_writer_section(cfg, "writer3")
        add_writer_section(cfg, "writer5", "w5", [
            {"id": "p1", "shm_id": 0},
            {"id": "p2", "shm_id": 0},
            {"id": "p3", "shm_id": 0},
        ])
        _write_config(config_path, cfg)

        on_request = _roots_callback([{"uri": f"file://{config_path}"}])
        resp = mcp.call_tool("adjust_shm", {}, on_request=on_request)
        _assert_mcp_success(resp)

        backfilled = _read_config(config_path)

        # writer5 所有 point 的 shm_id 已从 0 回填为 ≥1 的实际值
        w5_ids = [p["shm_id"] for p in get_writer_points_cfg(backfilled, "writer5")]
        assert len(w5_ids) == 3, "writer5 should have 3 points"
        assert all(sid > 0 for sid in w5_ids), f"writer5 shm_ids: {w5_ids}"
        assert sorted(w5_ids) == [5, 6, 7], f"writer5 shm_ids: {w5_ids}"

        # writer1,2,4 的 point 的 shm_id 保持原值不变
        w1_ids_after = [p["shm_id"] for p in get_writer_points_cfg(backfilled, "writer1")]
        assert w1_ids_after == w1_ids_before, (
            f"writer1 shm_ids changed: {w1_ids_before} → {w1_ids_after}"
        )
        w2_ids_after = [p["shm_id"] for p in get_writer_points_cfg(backfilled, "writer2")]
        assert w2_ids_after == w2_ids_before, (
            f"writer2 shm_ids changed: {w2_ids_before} → {w2_ids_after}"
        )
        w4_ids_after = [p["shm_id"] for p in get_writer_points_cfg(backfilled, "writer4")]
        assert w4_ids_after == w4_ids_before, (
            f"writer4 shm_ids changed: {w4_ids_before} → {w4_ids_after}"
        )

        # writer3 已从配置中移除
        assert "writer3" not in backfilled["c4_shm_manager"]["writer"]
        assert "writer3" not in backfilled

        # 无 shm_id=0 残留
        all_ids = collect_all_writer_shm_ids(backfilled)
        assert 0 not in all_ids, f"Found shm_id=0 in config: {all_ids}"

    finally:
        os.unlink(config_path)
        os.rmdir(temp_dir)


# ── TC12: SHM_NOT_CREATED ──

@pytest.mark.timeout(30)
def test_tc12_shm_not_created(mcp, isolated_shm):
    """TC12: 不调用 create_shm，直接 adjust_shm → SHM_NOT_CREATED。"""
    iid = f"tc12_{uuid.uuid4().hex[:8]}"
    isolated_shm(iid)
    temp_dir = tempfile.mkdtemp(prefix="c4_test_")
    config_path = os.path.join(temp_dir, "config.json")

    try:
        cfg = build_initial_config()
        _write_config(config_path, cfg)

        on_request = _roots_callback([{"uri": f"file://{config_path}"}])
        resp = mcp.call_tool("adjust_shm", {}, on_request=on_request)
        _assert_mcp_error(resp, "SHM_NOT_CREATED")

    finally:
        os.unlink(config_path)
        os.rmdir(temp_dir)


# ── TC13: CONFIG_PATH_MISSING ──

@pytest.mark.timeout(30)
def test_tc13_config_path_missing(mcp, isolated_shm):
    """TC13: create_shm 后，adjust_shm 时 roots/list 返回错误 → CONFIG_PATH_MISSING。"""
    iid = f"tc13_{uuid.uuid4().hex[:8]}"
    config_path, temp_dir = _create_and_activate(mcp, isolated_shm, iid)

    try:
        resp = mcp.call_tool("adjust_shm", {}, on_request=_roots_error_callback())
        _assert_mcp_error(resp, "CONFIG_PATH_MISSING")

    finally:
        os.unlink(config_path)
        os.rmdir(temp_dir)


# ── TC14: CONFIG_MISSING_SECTION ──

@pytest.mark.timeout(30)
def test_tc14_config_missing_section(mcp, isolated_shm):
    """TC14: writer=[] 但 reader 非空 → CONFIG_MISSING_SECTION。"""
    iid = f"tc14_{uuid.uuid4().hex[:8]}"
    config_path, temp_dir = _create_and_activate(mcp, isolated_shm, iid)

    try:
        cfg = _read_config(config_path)
        cfg["c4_shm_manager"]["writer"] = []
        for w in ["writer1", "writer2", "writer3", "writer4"]:
            cfg.pop(w, None)
        # reader 保持非空（reader1 仍在）
        _write_config(config_path, cfg)

        on_request = _roots_callback([{"uri": f"file://{config_path}"}])
        resp = mcp.call_tool("adjust_shm", {}, on_request=on_request)
        _assert_mcp_error(resp, "CONFIG_MISSING_SECTION")

    finally:
        os.unlink(config_path)
        os.rmdir(temp_dir)


# ── TC15: DUPLICATE_KEY ──

@pytest.mark.timeout(30)
def test_tc15_duplicate_key(mcp, isolated_shm):
    """TC15: 新增 writer5 时 key 与已有 writer 重复 → DUPLICATE_KEY。

    key 格式: {device_id}.{point_id}
    writer1 有 w1.p1 → writer5 也创建 w1.p1 → 冲突。
    """
    iid = f"tc15_{uuid.uuid4().hex[:8]}"
    config_path, temp_dir = _create_and_activate(mcp, isolated_shm, iid)

    try:
        cfg = _read_config(config_path)
        # writer5 使用与 writer1 相同的 device_id "w1" 和 point_id "p1"
        # → key "w1.p1" 冲突
        add_writer_section(cfg, "writer5", "w1", [
            {"id": "p1", "shm_id": 0},
        ])
        _write_config(config_path, cfg)

        on_request = _roots_callback([{"uri": f"file://{config_path}"}])
        resp = mcp.call_tool("adjust_shm", {}, on_request=on_request)
        _assert_mcp_error(resp, "DUPLICATE_KEY")

    finally:
        os.unlink(config_path)
        os.rmdir(temp_dir)


# ── TC16: UNKNOWN_READER_KEY ──

@pytest.mark.timeout(30)
def test_tc16_unknown_reader_key(mcp, isolated_shm):
    """TC16: Reader 的 key 引用已被删除的 Writer point → UNKNOWN_READER_KEY。"""
    iid = f"tc16_{uuid.uuid4().hex[:8]}"
    config_path, temp_dir = _create_and_activate(mcp, isolated_shm, iid)

    try:
        cfg = _read_config(config_path)
        # 删除 writer1 → reader1 仍引用 w1.p1、w1.p2 → key 不再存在
        remove_writer_section(cfg, "writer1")
        _write_config(config_path, cfg)

        on_request = _roots_callback([{"uri": f"file://{config_path}"}])
        resp = mcp.call_tool("adjust_shm", {}, on_request=on_request)
        _assert_mcp_error(resp, "UNKNOWN_READER_KEY")

    finally:
        os.unlink(config_path)
        os.rmdir(temp_dir)
