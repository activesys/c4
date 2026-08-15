import os
import sys
import subprocess
import importlib.util
import json
import tempfile

import pytest

# Reuse c4_fun_00057 fixtures
_src_path = os.path.join(os.path.dirname(__file__), "../c4_fun_00057/conftest.py")
_spec = importlib.util.spec_from_file_location("c4_fun_00057_conftest", _src_path)
_c57 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_c57)

start_asfp2_server = _c57.start_asfp2_server
shm_mgr_client = _c57.shm_mgr_client
mcp = _c57.mcp


# ──────────────────────────────────────────────
#  Fixture: prepare_environment（config_path 参数方式）
#  create_shm / adjust_shm 通过 config_path 参数读取配置，
#  对应设计文档 c4_shm_manager.md。
# ──────────────────────────────────────────────

@pytest.fixture
def prepare_environment(shm_mgr_client):
    """
    Function 级 fixture — 准备配置文件 + 共享内存。
    返回工厂函数 (config_dict, instance_id) → (config_path, instance_id)。
    内部完成 create_shm(instance_id, config_path) + adjust_shm(instance_id, config_path)，并在返回前关闭 shm_manager。
    """
    temp_files: list[str] = []

    def _prepare(config_dict: dict, instance_id: str):
        # 步骤 1: 写入配置文件
        fd, config_path = tempfile.mkstemp(suffix=".json", prefix="c4_config_")
        temp_files.append(config_path)
        with os.fdopen(fd, "w") as f:
            json.dump(config_dict, f)

        # 步骤 2: create_shm — 以 config_path 参数指定配置（config-based sizing + 分配 shm_id）
        resp = shm_mgr_client.call_tool(
            "create_shm",
            {"instance_id": instance_id, "config_path": config_path},
        )
        if resp["result"].get("isError", False):
            raise RuntimeError(
                f"create_shm failed: {resp['result']['content'][0]['text']}"
            )

        # 步骤 3: adjust_shm — 重新读取配置、分配 shm_id、必要时扩容
        resp = shm_mgr_client.call_tool(
            "adjust_shm", {"instance_id": instance_id, "config_path": config_path},
        )
        if resp["result"].get("isError", False):
            raise RuntimeError(
                f"adjust_shm failed: {resp['result']['content'][0]['text']}"
            )

        # 步骤 4: 关闭 shm_manager（shm_manager 在 shm_mgr_client teardown 中也会关闭）
        shm_mgr_client.close()

        return config_path, instance_id

    yield _prepare

    # Teardown: 清理临时配置文件
    for path in temp_files:
        try:
            os.unlink(path)
        except OSError:
            pass

# ──────────────────────────────────────────────
#  Fixture: isolated_shm（config_path 参数方式）
#  start 通过 instance_id 直接打开 /dev/shm/{instance_id} 共享内存，
#  因此每个用例启动前清空 /dev/shm 下残留的 c4_* 对象，保证本用例的
#  create_shm 是唯一存在的共享内存，避免残留对象干扰。
# ──────────────────────────────────────────────


def _wipe_c4_shm():
    """删除 /dev/shm 下所有 c4_* 共享内存对象。"""
    import glob

    for p in glob.glob("/dev/shm/c4_*"):
        try:
            os.unlink(p)
        except OSError:
            pass


@pytest.fixture
def isolated_shm():
    """Function 级隔离 fixture。setup 清空全部 c4_* 共享内存并登记本实例，teardown 释放。"""
    registered: list[str] = []

    def register(instance_id: str) -> None:
        registered.append(instance_id)
        _wipe_c4_shm()
        try:
            shm_unlink(f"/{instance_id}")
        except OSError:
            pass

    yield register

    for iid in registered:
        try:
            shm_unlink(f"/{iid}")
        except OSError:
            pass


# Reuse shm_helpers from c4_fun_00057
_shm_path = os.path.join(os.path.dirname(__file__), "../c4_fun_00057/shm_helpers.py")
_shm_spec = importlib.util.spec_from_file_location("shm_helpers", _shm_path)
_shm = importlib.util.module_from_spec(_shm_spec)
_shm_spec.loader.exec_module(_shm)

read_shm_block = _shm.read_shm_block
shm_path = _shm.shm_path
read_shm_header = _shm.read_shm_header
shm_unlink = _shm.shm_unlink

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
