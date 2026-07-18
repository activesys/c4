import os
import sys
import subprocess
import importlib.util

# Reuse c4_fun_00057 fixtures
_src_path = os.path.join(os.path.dirname(__file__), "../c4_fun_00057/conftest.py")
_spec = importlib.util.spec_from_file_location("c4_fun_00057_conftest", _src_path)
_c57 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_c57)

prepare_environment = _c57.prepare_environment
start_asfp2_server = _c57.start_asfp2_server
isolated_shm = _c57.isolated_shm
_roots_callback = _c57._roots_callback
shm_mgr_client = _c57.shm_mgr_client
mcp = _c57.mcp

# Reuse shm_helpers from c4_fun_00057
_shm_path = os.path.join(os.path.dirname(__file__), "../c4_fun_00057/shm_helpers.py")
_shm_spec = importlib.util.spec_from_file_location("shm_helpers", _shm_path)
_shm = importlib.util.module_from_spec(_shm_spec)
_shm_spec.loader.exec_module(_shm)

read_shm_block = _shm.read_shm_block
shm_path = _shm.shm_path
read_shm_header = _shm.read_shm_header

# asfp2_client binary path constant
ASFP2_CLIENT = "/usr/local/bin/asfp2_client"


# ──────────────────────────────────────────────
#  Helper: assert MCP error
# ──────────────────────────────────────────────

def _assert_mcp_error(resp, expected_prefix):
    assert resp["result"]["isError"] is True
    text = resp["result"]["content"][0]["text"]
    assert text.startswith(expected_prefix), f"Expected '{expected_prefix}', got '{text}'"


# ──────────────────────────────────────────────
#  Helper: run asfp2_client subprocess
# ──────────────────────────────────────────────

def _run_asfp2_client(
    server_ip="127.0.0.1",
    port=9000,
    times=1,
    packet_size=0,
    key_begin=1000,
    key_end=1002,
    data_begin=100,
    data_end=200,
    data_type=4,
    ts_start=None,
    no_attr=False,
    protocol=None,
    extra_args=None,
    timeout=10,
):
    """Run asfp2_client as subprocess. Returns (returncode, stdout, stderr)."""
    cmd = [
        ASFP2_CLIENT, "-s", server_ip, "-p", str(port),
        "-t", str(times), "-z", str(packet_size),
        "-b", str(key_begin), "-e", str(key_end),
        "-B", str(data_begin), "-E", str(data_end),
        "--type", str(data_type),
        "--i0", "10", "--i1", "10",
    ]
    if ts_start is not None:
        cmd.extend(["--ts-start", str(ts_start)])
    if no_attr:
        cmd.extend(["--nks", "--nsdt", "--nstp"])
    if protocol is not None:
        cmd.extend(["-P", str(protocol)])
    if extra_args:
        cmd.extend(extra_args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


# ──────────────────────────────────────────────
#  Helper: verify block state
# ──────────────────────────────────────────────

def _assert_block_written(shm_path_str, shm_id, expected_type):
    """Verify that a Data Block was written with correct state and type."""
    block = read_shm_block(shm_path_str, shm_id)
    assert block["state"] == 1, f"shm_id={shm_id}: state={block['state']}, expected 1"
    assert block["type"] == expected_type, f"shm_id={shm_id}: type={block['type']}, expected {expected_type}"
    assert block["timestamp"] > 0, f"shm_id={shm_id}: timestamp=0, expected >0"
    return block


def _assert_block_not_written(shm_path_str, shm_id):
    """Verify that a Data Block was NOT written (state=0)."""
    block = read_shm_block(shm_path_str, shm_id)
    assert block["state"] == 0, f"shm_id={shm_id}: state={block['state']}, expected 0 (not written)"
