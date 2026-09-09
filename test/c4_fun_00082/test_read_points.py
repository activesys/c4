"""C4_FUN_00082 §2 — read_points 工具契约（TC1~TC12 + TC11a~c）。"""

import json
import struct
import threading
import time

from conftest import write_point


def _call_read(mcp, shm_ids):
    resp = mcp.call_tool("read_points", {"shm_ids": shm_ids})
    result = resp["result"]
    if result.get("isError", False):
        return {"isError": True, "text": result["content"][0]["text"]}
    return {"isError": False, **json.loads(result["content"][0]["text"])}


def _seed(direct_stack, shm_id, data_type, value, ts=None):
    write_point(direct_stack.shm_path, shm_id, data_type,
                value, ts if ts is not None else int(time.time() * 1000))


def test_tc1_single_ok(direct_stack):
    """TC1 单点正常读取：value/timestamp_ms/seq 均为预写值。"""
    sid = direct_stack.shm_ids["wt1.windspeed"]
    ts = int(time.time() * 1000)
    _seed(direct_stack, sid, 6, 7256, ts)
    r = _call_read(direct_stack.client, [sid])
    assert not r["isError"]
    entry = next(e for e in r["reads"] if e["shm_id"] == sid)
    assert entry["status"] == "ok"
    assert entry["value"] == 7256
    assert entry["timestamp_ms"] == ts


def test_tc2_no_data(direct_stack):
    """TC2 no_data：块从未写入（state=0）。"""
    sid = direct_stack.shm_ids["wt1.oiltemp"]  # oiltemp 从未播种
    r = _call_read(direct_stack.client, [sid])
    entry = next(e for e in r["reads"] if e["shm_id"] == sid)
    assert entry["status"] == "no_data"
    assert "value" not in entry


def test_tc3_batch_mixed(direct_stack):
    """TC3 批量混合：ok 与 no_data 互不影响。"""
    ok_id = direct_stack.shm_ids["wt1.windspeed"]
    empty_id = direct_stack.shm_ids["wt1.oiltemp"]
    _seed(direct_stack, ok_id, 6, 111, int(time.time() * 1000))
    r = _call_read(direct_stack.client, [ok_id, empty_id])
    assert len(r["reads"]) == 2
    statuses = {e["shm_id"]: e["status"] for e in r["reads"]}
    assert statuses[ok_id] == "ok"
    assert statuses[empty_id] == "no_data"


def test_tc4_boolean_decode(direct_stack):
    """TC4 BOOLEAN/BIT 解码：value=1；value_raw == "1"（十进制串）。"""
    sid = direct_stack.shm_ids["wt1.power"]
    _seed(direct_stack, sid, 0, 1, int(time.time() * 1000))
    r = _call_read(direct_stack.client, [sid])
    entry = r["reads"][0]
    assert entry["value"] == 1
    assert entry["value_raw"] == "1"


def test_tc5_int16_sign_extend(direct_stack):
    """TC5 有符号整型符号扩展：INT16 = -5。"""
    sid = direct_stack.shm_ids["wt1.power"]
    _seed(direct_stack, sid, 3, -5 & (2**16 - 1), int(time.time() * 1000))
    entry = _call_read(direct_stack.client, [sid])["reads"][0]
    assert entry["value"] == -5


def test_tc6_uint16_zero_extend(direct_stack):
    """TC6 无符号零扩展：UINT16 = 0xFFFB → 65531（零扩展，不为负）。"""
    sid = direct_stack.shm_ids["wt1.power"]
    _seed(direct_stack, sid, 4, 0xFFFB, int(time.time() * 1000))
    entry = _call_read(direct_stack.client, [sid])["reads"][0]
    assert entry["value"] == 65531


def test_tc7_float32_decode(direct_stack):
    """TC7 FLOAT32 解码：1.5 的 IEEE 位型（本机序低 4 字节）。"""
    import struct as _s
    sid = direct_stack.shm_ids["wt1.windspeed"]
    bits = struct.unpack("<I", struct.pack("<f", 1.5))[0]
    _seed(direct_stack, sid, 10, bits, int(time.time() * 1000))
    entry = _call_read(direct_stack.client, [sid])["reads"][0]
    assert entry["value"] == 1.5


def test_tc8_float16_special(direct_stack):
    """TC8 FLOAT16 特例：低 4 字节 = float32 位型 0x3FC00000 → 1.5。

    回归锚点：func_test_case.md 用例 15（server 写入路径）；本用例为
    read_points 解码路径。
    """
    sid = direct_stack.shm_ids["wt1.power"]
    _seed(direct_stack, sid, 9, 0x3FC00000, int(time.time() * 1000))
    entry = _call_read(direct_stack.client, [sid])["reads"][0]
    assert entry["value"] == 1.5


def test_tc9_float64_decode(direct_stack):
    """TC9 FLOAT64 解码：3.14。"""
    import struct as _s
    sid = direct_stack.shm_ids["wt1.windspeed"]
    bits = int.from_bytes(_s.pack("<d", 3.14), "little")
    _seed(direct_stack, sid, 11, bits, int(time.time() * 1000))
    entry = _call_read(direct_stack.client, [sid])["reads"][0]
    assert abs(entry["value"] - 3.14) < 1e-9


def test_tc10_value_raw_uint64(direct_stack):
    """TC10 value_raw 权威位型：UINT64 = 2^63 的十进制串。"""
    sid = direct_stack.shm_ids["wt1.power"]
    _seed(direct_stack, sid, 8, 2**63, int(time.time() * 1000))
    entry = _call_read(direct_stack.client, [sid])["reads"][0]
    assert entry["value_raw"] == str(2**63)


def test_tc11_out_of_range(direct_stack):
    """TC11 越界：shm_id ≥ max_points → SHM_ID_OUT_OF_RANGE。"""
    r = _call_read(direct_stack.client, [10_000_000])
    assert r["isError"] is True
    assert "SHM_ID_OUT_OF_RANGE" in r["text"]


def test_tc11a_empty_list(direct_stack):
    """TC11a 空列表：通过 schema 校验 → 业务层校验拒绝（isError=true）。"""
    r = _call_read(direct_stack.client, [])
    assert r["isError"] is True
    assert "SHM_ID_OUT_OF_RANGE" in r["text"]


def test_tc11b_count_over_limit(direct_stack):
    """TC11b 数量超限：1001 个（schema 无 maxItems）→ 业务层拒绝。"""
    sid = direct_stack.shm_ids["wt1.windspeed"]
    r = _call_read(direct_stack.client, [sid] * 1001)
    assert r["isError"] is True
    assert "SHM_ID_OUT_OF_RANGE" in r["text"]


def test_tc11c_header_block(direct_stack):
    """TC11c shm_id=0（schema minimum 1）：请求被拒绝——

    JSON-RPC -32602 schema 校验错误（shm_manager.md §3 约定）或业务层
    isError=true，二者皆视为通过。
    """
    r = _call_read(direct_stack.client, [0])
    if "error" in r and not r.get("isError"):
        assert r["error"]["code"] == -32602
        return
    assert r.get("isError") is True


def test_tc12_seqlock_no_torn_reads(direct_stack):
    """TC12 seqlock 稳定性：并发写 100 次读取，永不返回撕裂数据。"""
    sid = direct_stack.shm_ids["wt1.windspeed"]
    written = [11111, 22222, 33333, 44444]
    stop = {"flag": False}

    def writer():
        i = 0
        while not stop["flag"]:
            _seed(direct_stack, sid, 6, written[i % len(written)], int(time.time() * 1000))
            i += 1
            time.sleep(0.001)

    import threading
    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(100):
            r = _call_read(direct_stack.client, [sid])
            entry = r["reads"][0]
            if entry["status"] == "ok":
                assert entry["value"] in written
            time.sleep(0.005)
    finally:
        stop["flag"] = True
        t.join()
