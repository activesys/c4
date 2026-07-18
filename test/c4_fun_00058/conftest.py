"""
C4_FUN_00058 测试公共 fixture — ASFP2 Server stop/restart 生命周期测试。

复用 c4_fun_00057 的 prepare_environment / start_asfp2_server / isolated_shm
fixture，与 c4_fun_00042 的模式一致。
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import importlib.util

# ──────────────────────────────────────────────
#  复用 c4_fun_00057 的 fixture
# ──────────────────────────────────────────────

_src_path = os.path.join(os.path.dirname(__file__), "../c4_fun_00057/conftest.py")
_spec = importlib.util.spec_from_file_location("c4_fun_00057_conftest", _src_path)
_c57 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_c57)

prepare_environment = _c57.prepare_environment
start_asfp2_server = _c57.start_asfp2_server
isolated_shm = _c57.isolated_shm
_roots_callback = _c57._roots_callback
shm_mgr_client = _c57.shm_mgr_client

# ──────────────────────────────────────────────
#  本地 shm_helpers
# ──────────────────────────────────────────────

from shm_helpers import shm_path, read_shm_block  # noqa: E402

# ──────────────────────────────────────────────
#  常量
# ──────────────────────────────────────────────

ASFP2_CLIENT = "/usr/local/bin/asfp2_client"
SHM_MAGIC = 0xC4DA7A00


# ──────────────────────────────────────────────
#  Helper: 端口释放检测（轮询重试）
# ──────────────────────────────────────────────

def wait_port_released(port: int, timeout: float = 3.0, interval: float = 0.1):
    """轮询检测端口是否已释放（100ms 间隔，最长 timeout 秒）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=interval)
            s.close()
        except (ConnectionRefusedError, OSError):
            return  # 端口已释放
        time.sleep(interval)
    raise RuntimeError(f"Port {port} not released within {timeout}s")


# ──────────────────────────────────────────────
#  Helper: assert MCP error
# ──────────────────────────────────────────────

def _assert_mcp_error(resp: dict, expected_prefix: str):
    """验证 MCP 响应为业务错误且错误消息以前缀开始。"""
    assert resp["result"]["isError"] is True, (
        f"Expected isError=true, got: {resp}"
    )
    text = resp["result"]["content"][0]["text"]
    assert text.startswith(expected_prefix), (
        f"Expected prefix '{expected_prefix}', got '{text}'"
    )


# ──────────────────────────────────────────────
#  Helper: assert MCP success
# ──────────────────────────────────────────────

def _assert_mcp_success(resp: dict):
    """验证 MCP 响应为成功（isError: false, text: 'success'）。"""
    assert resp["result"].get("isError", False) is False, (
        f"Expected isError=false, got: {resp}"
    )
    assert resp["result"]["content"][0]["text"] == "success", (
        f"Expected 'success', got: {resp}"
    )


# ──────────────────────────────────────────────
#  Helper: assert port is listening
# ──────────────────────────────────────────────

def _assert_port_listening(port: int, timeout: float = 1.0):
    """验证本地端口已监听。"""
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        s.close()
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        raise AssertionError(f"Port {port} is not listening: {e}")


# ──────────────────────────────────────────────
#  Helper: assert block was written (write_seq check)
# ──────────────────────────────────────────────

def _assert_block_written_seq(shm_path_str: str, shm_id: int):
    """
    验证 Data Block 的 magic 有效，返回当前 write_seq。
    不检查 state——重启后 block 可能尚未被写入（state=0）。
    """
    block = read_shm_block(shm_path_str, shm_id)
    assert block["magic"] == SHM_MAGIC, (
        f"shm_id={shm_id}: magic=0x{block['magic']:08X}, expected 0x{SHM_MAGIC:08X}"
    )
    return block["write_seq"]


# ──────────────────────────────────────────────
#  Helper: run asfp2_client subprocess
# ──────────────────────────────────────────────

def _run_asfp2_client(
    port: int,
    begin_addr: int,
    end_addr: int,
    times: int = 3,
    data_type: int = 4,  # UINT16
    timeout: int = 10,
):
    """运行 asfp2_client 发送 ASFP2 数据包。返回 (returncode, stdout, stderr)。"""
    cmd = [
        ASFP2_CLIENT, "-s", "127.0.0.1", "-p", str(port),
        "-t", str(times),
        "-b", str(begin_addr), "-e", str(end_addr),
        "-B", "100", "-E", "200",
        "--type", str(data_type),
        "--i0", "10", "--i1", "10",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


# ──────────────────────────────────────────────
#  Helper: run adjust_shm via c4_shm_manager
# ──────────────────────────────────────────────

def _run_adjust_shm(config_path: str, instance_id: str | None = None):
    """
    启动 c4_shm_manager 子进程，MCP initialize，调用 adjust_shm，关闭进程。
    如果指定 instance_id，先调用 create_shm（若 shm 已存在则忽略错误）。
    """
    # 复用 c4_fun_00057 的 McpClient 和二进制发现
    McpClient = _c57.McpClient
    _find_shm_manager_binary = _c57._find_shm_manager_binary

    binary = _find_shm_manager_binary()
    client = McpClient(binary)

    if instance_id is not None:
        client.call_tool(
            "create_shm",
            {"instance_id": instance_id},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )

    resp = client.call_tool(
        "adjust_shm",
        {},
        on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
    )
    client.close()

    if resp["result"].get("isError", False):
        raise RuntimeError(
            f"adjust_shm failed: {resp['result']['content'][0]['text']}"
        )
    return resp


def _prepare_config_with_shm(config_dict: dict, instance_id: str, register_cleanup=None) -> str:
    """
    准备配置文件和共享内存（独立于 prepare_environment fixture）。
    用于 TC 中需要二次准备配置的场景：
    1. 写入 config JSON 文件
    2. 启动独立 c4_shm_manager → create_shm → adjust_shm → 关闭
    返回 config_path。
    
    若提供 register_cleanup(isolated_shm callback)，注册 instance_id 用于 teardown 清理。
    """
    if register_cleanup is not None:
        register_cleanup(instance_id)

    fd, config_path = tempfile.mkstemp(suffix=".json", prefix="c4_config_")
    with os.fdopen(fd, "w") as f:
        json.dump(config_dict, f)

    _run_adjust_shm(config_path, instance_id=instance_id)
    return config_path


# ──────────────────────────────────────────────
#  Config factories (§3)
# ──────────────────────────────────────────────

def _make_standard_config(instance_id: str = "test_stop_restart", port: int = 9000):
    """§3.1 标准配置：单实例 2 point (addr=1000, 1001)。"""
    return {
        "c4_shm_manager": {
            "writer": ["c4_asfp2_server"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_asfp2_server": [
            {
                "name": "停止重启测试服务",
                "id": instance_id,
                "port": port,
                "t1": 0,
                "t2": 0,
                "forward_kack": 255,
                "inverse_keep": 0,
                "points": [
                    {"id": "pt_a", "addr": 1000, "shm_id": 0},
                    {"id": "pt_b", "addr": 1001, "shm_id": 0},
                ],
            }
        ],
        "c4_asfp2_client": [],
    }


def _make_port_conflict_config():
    """§3.2 端口冲突配置：两个实例均为 port=9000。"""
    return {
        "c4_shm_manager": {
            "writer": ["c4_asfp2_server"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_asfp2_server": [
            {
                "name": "实例1",
                "id": "conflict_r1",
                "port": 9000,
                "t1": 0,
                "t2": 0,
                "forward_kack": 255,
                "inverse_keep": 0,
                "points": [{"id": "p1", "addr": 1000, "shm_id": 0}],
            },
            {
                "name": "实例2",
                "id": "conflict_r2",
                "port": 9000,
                "t1": 0,
                "t2": 0,
                "forward_kack": 255,
                "inverse_keep": 0,
                "points": [{"id": "p2", "addr": 2000, "shm_id": 0}],
            },
        ],
        "c4_asfp2_client": [],
    }


def _make_modified_config(
    instance_id: str = "test_stop_restart",
    port: int = 9001,
    include_old_points: bool = True,
):
    """
    TC9 修改后配置：port 改为指定值，保留旧 point (addr=1000,1001)，
    新增 point (addr=2000)。"""
    points = []
    if include_old_points:
        points.extend([
            {"id": "pt_a", "addr": 1000, "shm_id": 0},
            {"id": "pt_b", "addr": 1001, "shm_id": 0},
        ])
    points.append({"id": "pt_c", "addr": 2000, "shm_id": 0})

    return {
        "c4_shm_manager": {
            "writer": ["c4_asfp2_server"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_asfp2_server": [
            {
                "name": "停止重启测试服务(改)",
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


def _make_corrected_config(instance_id: str = "test_stop_restart", port: int = 9000):
    """TC10 修正配置：单实例 1 point (addr=1000)。"""
    return {
        "c4_shm_manager": {
            "writer": ["c4_asfp2_server"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_asfp2_server": [
            {
                "name": "修正配置服务",
                "id": instance_id,
                "port": port,
                "t1": 0,
                "t2": 0,
                "forward_kack": 255,
                "inverse_keep": 0,
                "points": [{"id": "p1", "addr": 1000, "shm_id": 0}],
            }
        ],
        "c4_asfp2_client": [],
    }


def _read_config_shm_id(config_path: str, addr: int) -> int:
    """从磁盘上的配置 JSON 读取指定 addr 的 shm_id（§5.8）。"""
    with open(config_path, "r") as f:
        config = json.load(f)
    for pt in config["c4_asfp2_server"][0]["points"]:
        if pt["addr"] == addr:
            return pt["shm_id"]
    raise ValueError(f"addr={addr} not found in config {config_path}")
