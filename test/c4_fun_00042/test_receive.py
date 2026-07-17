"""
C4_FUN_00042 功能测试 — ASFP2 Server 数据接收验证。

测试 c4_asfp2_server 从 ASFP2 客户端接收数据并写入共享内存。
"""

import os
import sys
import struct
import time
import json
import subprocess

import pytest  # type: ignore
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from conftest import (  # noqa: E402
    _run_asfp2_client,
    _assert_block_written,
    _assert_block_not_written,
    _roots_callback,
    ASFP2_CLIENT,
)
from shm_helpers import shm_path, read_shm_block  # noqa: E402


# ──────────────────────────────────────────────
#  Value extraction helpers
# ──────────────────────────────────────────────

def _value_u16(value_int):
    """Extract UINT16 from 8-byte big-endian value."""
    return struct.unpack(">H", struct.pack(">Q", value_int)[6:8])[0]


def _value_f32(value_int):
    """Extract FLOAT32 from 8-byte value (native-endian for v2.0.0/v2.1.0)."""
    return struct.unpack("f", struct.pack(">Q", value_int)[4:8])[0]


def _value_u64(value_int):
    """Extract UINT64 from 8-byte value."""
    return struct.unpack(">Q", struct.pack(">Q", value_int))[0]


# ──────────────────────────────────────────────
#  Config templates
# ──────────────────────────────────────────────

def _make_config(port, points):
    """Build a standard config dict with given port and points list."""
    return {
        "c4_shm_manager": {"writer": ["c4_asfp2_server"], "reader": ["c4_asfp2_client"]},
        "c4_asfp2_server": [{
            "name": "test",
            "id": "test_receiver",
            "port": port,
            "t1": 0,
            "t2": 0,
            "forward_kack": 255,
            "inverse_keep": 0,
            "points": points,
        }],
        "c4_asfp2_client": [],
    }


def _standard_config(port):
    """Standard 2-key config: addr 1000→shm_id=1, 1001→shm_id=2."""
    return _make_config(port, [
        {"id": "p1", "addr": 1000, "shm_id": 0},
        {"id": "p2", "addr": 1001, "shm_id": 0},
    ])


def _multi_key_config(port, nkeys=5):
    """N-key config: addr 1000..(1000+nkeys-1)."""
    return _make_config(port, [
        {"id": f"p{i}", "addr": 1000 + i, "shm_id": 0}
        for i in range(nkeys)
    ])


def _six_key_config(port):
    """6-key config for TC10: three ranges (1000-1001, 2000-2001, 3000-3001)."""
    return _make_config(port, [
        {"id": "p1", "addr": 1000, "shm_id": 0},
        {"id": "p2", "addr": 1001, "shm_id": 0},
        {"id": "p3", "addr": 2000, "shm_id": 0},
        {"id": "p4", "addr": 2001, "shm_id": 0},
        {"id": "p5", "addr": 3000, "shm_id": 0},
        {"id": "p6", "addr": 3001, "shm_id": 0},
    ])


# ──────────────────────────────────────────────
#  Test class
# ──────────────────────────────────────────────

class TestReceive:
    """TC1~TC12: ASFP2 Server 数据接收测试。"""

    # ── TC1: 基本数据接收 — UINT16 ──

    def test_tc1_basic(self, prepare_environment, start_asfp2_server, isolated_shm):
        iid = "test_tc1"
        isolated_shm(iid)

        config = _standard_config(9000)
        config_path, iid = prepare_environment(config, iid)

        resp = start_asfp2_server.call_tool(
            "start", {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        assert resp["result"].get("isError", False) is False
        assert resp["result"]["content"][0]["text"] == "success"

        rc, _, _ = _run_asfp2_client(port=9000, data_type=4, ts_start=1000000)
        assert rc == 0
        time.sleep(0.5)

        shm = shm_path(iid)
        b1 = _assert_block_written(shm, 1, 4)
        b2 = _assert_block_written(shm, 2, 4)
        assert 100 <= _value_u16(b1["value"]) <= 200
        assert 100 <= _value_u16(b2["value"]) <= 200

        start_asfp2_server.call_tool("pause", {})

    # ── TC2: 无属性优化 ──

    def test_tc2_no_attr(self, prepare_environment, start_asfp2_server, isolated_shm):
        iid = "test_tc2"
        isolated_shm(iid)

        config = _standard_config(9100)
        config_path, iid = prepare_environment(config, iid)

        resp = start_asfp2_server.call_tool(
            "start", {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        assert resp["result"].get("isError", False) is False
        assert resp["result"]["content"][0]["text"] == "success"

        rc, _, _ = _run_asfp2_client(port=9100, data_type=4, ts_start=1000000, no_attr=True)
        assert rc == 0
        time.sleep(0.5)

        shm = shm_path(iid)
        _assert_block_written(shm, 1, 4)
        _assert_block_written(shm, 2, 4)

        start_asfp2_server.call_tool("pause", {})

    # ── TC3: key_range — 多 key 连续发送 ──

    def test_tc3_multi_key(self, prepare_environment, start_asfp2_server, isolated_shm):
        iid = "test_tc3"
        isolated_shm(iid)

        config = _multi_key_config(9200, nkeys=5)
        config_path, iid = prepare_environment(config, iid)

        resp = start_asfp2_server.call_tool(
            "start", {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        assert resp["result"].get("isError", False) is False
        assert resp["result"]["content"][0]["text"] == "success"

        # TC3 does not set -z (uses default max).  Use -z 0 (asfp2_client treats 0 as max).
        rc, _, _ = _run_asfp2_client(
            port=9200, data_type=4, packet_size=0,
            key_begin=1000, key_end=1004,
            data_begin=100, data_end=500,
        )
        assert rc == 0
        time.sleep(0.5)

        shm = shm_path(iid)
        for shm_id in range(1, 6):
            _assert_block_written(shm, shm_id, 4)

        start_asfp2_server.call_tool("pause", {})

    # ── TC4: 变长类型过滤 — STRING 被丢弃 ──

    def test_tc4_string_filter(self, prepare_environment, start_asfp2_server, isolated_shm):
        iid = "test_tc4"
        isolated_shm(iid)

        config = _standard_config(9300)
        config_path, iid = prepare_environment(config, iid)

        resp = start_asfp2_server.call_tool(
            "start", {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        assert resp["result"].get("isError", False) is False
        assert resp["result"]["content"][0]["text"] == "success"

        rc, _, _ = _run_asfp2_client(port=9300, data_type=12)
        assert rc == 0
        time.sleep(0.5)

        shm = shm_path(iid)
        _assert_block_not_written(shm, 1)
        _assert_block_not_written(shm, 2)

        start_asfp2_server.call_tool("pause", {})

    # ── TC5: BOOLEAN 类型 ──

    def test_tc5_boolean(self, prepare_environment, start_asfp2_server, isolated_shm):
        iid = "test_tc5"
        isolated_shm(iid)

        config = _multi_key_config(9400, nkeys=5)
        config_path, iid = prepare_environment(config, iid)

        resp = start_asfp2_server.call_tool(
            "start", {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        assert resp["result"].get("isError", False) is False
        assert resp["result"]["content"][0]["text"] == "success"

        rc, _, _ = _run_asfp2_client(
            port=9400, data_type=0, packet_size=5,
            key_begin=1000, key_end=1004,
        )
        assert rc == 0
        time.sleep(0.5)

        shm = shm_path(iid)
        for shm_id in range(1, 6):
            _assert_block_written(shm, shm_id, 0)

        start_asfp2_server.call_tool("pause", {})

    # ── TC6: BIT 类型 ──

    def test_tc6_bit(self, prepare_environment, start_asfp2_server, isolated_shm):
        iid = "test_tc6"
        isolated_shm(iid)

        config = _multi_key_config(9500, nkeys=5)
        config_path, iid = prepare_environment(config, iid)

        resp = start_asfp2_server.call_tool(
            "start", {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        assert resp["result"].get("isError", False) is False
        assert resp["result"]["content"][0]["text"] == "success"

        rc, _, _ = _run_asfp2_client(
            port=9500, data_type=15, packet_size=5,
            key_begin=1000, key_end=1004,
        )
        assert rc == 0
        time.sleep(0.5)

        shm = shm_path(iid)
        for shm_id in range(1, 6):
            _assert_block_written(shm, shm_id, 15)

        start_asfp2_server.call_tool("pause", {})

    # ── TC7: FLOAT32 类型 ──

    def test_tc7_float32(self, prepare_environment, start_asfp2_server, isolated_shm):
        iid = "test_tc7"
        isolated_shm(iid)

        config = _standard_config(9600)
        config_path, iid = prepare_environment(config, iid)

        resp = start_asfp2_server.call_tool(
            "start", {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        assert resp["result"].get("isError", False) is False
        assert resp["result"]["content"][0]["text"] == "success"

        rc, _, _ = _run_asfp2_client(port=9600, data_type=10)
        assert rc == 0
        time.sleep(0.5)

        shm = shm_path(iid)
        b1 = _assert_block_written(shm, 1, 10)
        _assert_block_written(shm, 2, 10)
        assert 100.0 <= _value_f32(b1["value"]) <= 200.0

        start_asfp2_server.call_tool("pause", {})

    # ── TC8: v2.1.0 扩展格式 ──

    def test_tc8_v210(self, prepare_environment, start_asfp2_server, isolated_shm):
        iid = "test_tc8"
        isolated_shm(iid)

        config = _standard_config(9700)
        config_path, iid = prepare_environment(config, iid)

        resp = start_asfp2_server.call_tool(
            "start", {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        assert resp["result"].get("isError", False) is False
        assert resp["result"]["content"][0]["text"] == "success"

        rc, _, _ = _run_asfp2_client(port=9700, data_type=4, protocol=7)
        assert rc == 0
        time.sleep(0.5)

        shm = shm_path(iid)
        _assert_block_written(shm, 1, 4)
        _assert_block_written(shm, 2, 4)

        start_asfp2_server.call_tool("pause", {})

    # ── TC9: LARGE_DATA_BLOCK 类型 — 被过滤 ──

    def test_tc9_large_data_block(self, tmp_path, prepare_environment, start_asfp2_server, isolated_shm):
        iid = "test_tc9"
        isolated_shm(iid)

        # Create a test blob file
        blob_file = tmp_path / "test_blob.bin"
        blob_file.write_bytes(b"\x01\x02\x03\x04\x05\x06\x07\x08")

        config = _standard_config(9800)
        config_path, iid = prepare_environment(config, iid)

        resp = start_asfp2_server.call_tool(
            "start", {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        assert resp["result"].get("isError", False) is False
        assert resp["result"]["content"][0]["text"] == "success"

        # Use -F to send LARGE_DATA_BLOCK. Omit -B/-E/-z/--type since -F mode replaces them.
        cmd = [
            ASFP2_CLIENT, "-s", "127.0.0.1", "-p", "9800",
            "-t", "1", "-z", "1",
            "-b", "1000", "-e", "1001",
            "-F", str(blob_file),
            "--i0", "10", "--i1", "10",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0
        time.sleep(0.5)

        shm = shm_path(iid)
        _assert_block_not_written(shm, 1)
        _assert_block_not_written(shm, 2)

        start_asfp2_server.call_tool("pause", {})

    # ── TC10: 多连接并发 ──

    def test_tc10_concurrent(self, prepare_environment, start_asfp2_server, isolated_shm):
        iid = "test_tc10"
        isolated_shm(iid)

        config = _six_key_config(9900)
        config_path, iid = prepare_environment(config, iid)

        resp = start_asfp2_server.call_tool(
            "start", {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        assert resp["result"].get("isError", False) is False
        assert resp["result"]["content"][0]["text"] == "success"

        # Three concurrent connections sending different key ranges
        def run_client(port, key_begin, key_end, data_begin, data_end):
            return _run_asfp2_client(
                port=port,
                key_begin=key_begin, key_end=key_end,
                data_begin=data_begin, data_end=data_end,
                data_type=4,
            )

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(run_client, 9900, 1000, 1001, 100, 200),
                executor.submit(run_client, 9900, 2000, 2001, 300, 400),
                executor.submit(run_client, 9900, 3000, 3001, 500, 600),
            ]
            for f in futures:
                rc, _, _ = f.result()
                assert rc == 0

        time.sleep(0.5)

        shm = shm_path(iid)
        # Verify all 6 blocks written
        for shm_id in range(1, 7):
            _assert_block_written(shm, shm_id, 4)

        # Data isolation: check value ranges per connection
        b1 = read_shm_block(shm, 1)
        b2 = read_shm_block(shm, 2)
        assert 100 <= _value_u16(b1["value"]) <= 200
        assert 100 <= _value_u16(b2["value"]) <= 200

        b3 = read_shm_block(shm, 3)
        b4 = read_shm_block(shm, 4)
        assert 300 <= _value_u16(b3["value"]) <= 400
        assert 300 <= _value_u16(b4["value"]) <= 400

        b5 = read_shm_block(shm, 5)
        b6 = read_shm_block(shm, 6)
        assert 500 <= _value_u16(b5["value"]) <= 600
        assert 500 <= _value_u16(b6["value"]) <= 600

        start_asfp2_server.call_tool("pause", {})

    # ── TC11: BLOB/BITSTRING 变长类型过滤 ──

    @pytest.mark.parametrize("dtype", [13, 14])
    def test_tc11_blob_bitstring(self, dtype, prepare_environment, start_asfp2_server, isolated_shm):
        iid = f"test_tc11_{dtype}"
        isolated_shm(iid)

        config = _standard_config(10100)
        config_path, iid = prepare_environment(config, iid)

        resp = start_asfp2_server.call_tool(
            "start", {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        assert resp["result"].get("isError", False) is False
        assert resp["result"]["content"][0]["text"] == "success"

        rc, _, _ = _run_asfp2_client(port=10100, data_type=dtype)
        assert rc == 0
        time.sleep(0.5)

        shm = shm_path(iid)
        _assert_block_not_written(shm, 1)
        _assert_block_not_written(shm, 2)

        start_asfp2_server.call_tool("pause", {})

    # ── TC12: UINT64 — 8 字节整型边界 ──

    def test_tc12_uint64(self, prepare_environment, start_asfp2_server, isolated_shm):
        iid = "test_tc12"
        isolated_shm(iid)

        config = _standard_config(10200)
        config_path, iid = prepare_environment(config, iid)

        resp = start_asfp2_server.call_tool(
            "start", {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        assert resp["result"].get("isError", False) is False
        assert resp["result"]["content"][0]["text"] == "success"

        rc, _, _ = _run_asfp2_client(port=10200, data_type=8)
        assert rc == 0
        time.sleep(0.5)

        shm = shm_path(iid)
        b1 = _assert_block_written(shm, 1, 8)
        _assert_block_written(shm, 2, 8)
        assert 100 <= _value_u64(b1["value"]) <= 200

        start_asfp2_server.call_tool("pause", {})
