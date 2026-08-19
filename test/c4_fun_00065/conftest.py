"""
C4_FUN_00065 测试公共基础设施 — MCP 客户端 + redis/iec104d/授权 fixtures。

复用 c4_fun_00062 的 redis + 授权 fixture（importlib.util，同 c4_fun_00012 复用 c4_fun_00062），
新增 iec104d 从站 fixture 与 c4_iec104_client SUT fixture。

对应 c4_fun_00065 README §2.7 的 fixture 契约：
  license_env           (session)  确保 $ACQUISITION/license/ 有有效授权
  redis_server          (session)  确保 redis-server 在 127.0.0.1:6379 可达
  write_redis           (function) 用 redis_tool 写一个 Redis key
  start_iec104d         (function) 启动 iec104d 从站子进程，返回 (process, port)
  prepare_environment   (function) 生成配置 → create_shm → adjust_shm → 关闭
  start_iec104_client   (function) 启动 c4_iec104_client（MCP initialize）

共享内存操作见 shm_helpers.py。
"""

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # type: ignore


# ──────────────────────────────────────────────
#  复用 c4_fun_00062 的 fixture 与 helper
# ──────────────────────────────────────────────

_src_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "../c4_fun_00062/conftest.py"
)
_spec = importlib.util.spec_from_file_location("c4_fun_00062_conftest", _src_path)
assert _spec is not None and _spec.loader is not None
_c62 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_c62)

# fixtures
license_env = _c62.license_env
redis_server = _c62.redis_server
write_redis = _c62.write_redis
shm_mgr_client = _c62.shm_mgr_client
isolated_shm = _c62.isolated_shm
prepare_environment = _c62.prepare_environment

# helpers
McpClient = _c62.McpClient
_free_port = _c62._free_port
_wait_port_listening = _c62._wait_port_listening
_wait_port_released = _c62._wait_port_released
_stop_process = _c62._stop_process
_write_config_file = _c62._write_config_file
_assert_mcp_success = _c62._assert_mcp_success
_assert_mcp_error = _c62._assert_mcp_error
wait_write_seq_advanced = _c62.wait_write_seq_advanced
_run_adjust_shm = _c62._run_adjust_shm


# ──────────────────────────────────────────────
#  SUT 二进制发现
# ──────────────────────────────────────────────


def _find_iec104_client_binary() -> str:
    """查找或编译 c4_iec104_client 二进制。"""
    path = os.environ.get("C4_IEC104_CLIENT_PATH")
    if path and os.path.isfile(path):
        return path

    test_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(test_dir, "../../mcp/c4_iec104_client/c4_iec104_client"),
        os.path.join(test_dir, "../../mcp/c4_iec104_client/build/c4_iec104_client"),
        os.path.join(test_dir, "../../build/mcp/c4_iec104_client/c4_iec104_client"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return os.path.abspath(p)

    src_dir = os.path.abspath(os.path.join(test_dir, "../../mcp/c4_iec104_client"))
    if os.path.isdir(src_dir):
        result = subprocess.run(
            ["go", "build", "-o", "c4_iec104_client", "."],
            cwd=src_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return os.path.join(src_dir, "c4_iec104_client")
        else:
            pytest.skip(
                f"Failed to build c4_iec104_client: {result.stderr.strip()}"
            )

    pytest.skip(
        "c4_iec104_client binary not found. "
        "Set C4_IEC104_CLIENT_PATH env var or build from c4/mcp/c4_iec104_client"
    )
    return ""  # unreachable — pytest.skip() always raises


# ──────────────────────────────────────────────
#  配置工厂（iec104d 设备端 + c4_iec104_client SUT 端）
# ──────────────────────────────────────────────


def _make_iec104d_config(port, points, with_cp56time2a=0):
    """构造 iec104d 设备端配置（c4_fun_00065 README §3.1 模板）。"""
    pwd = tempfile.mkdtemp(prefix="c4_iec104d_")
    return {
        "engine": {"pwd": pwd, "stop_check": 100},
        "log": {"dir": "log", "file": "log.log", "level": 1, "debug_time": 300, "size": 128},
        "iec104": {
            "ip": "127.0.0.1", "port": port,
            "k": 12, "w": 8,
            "t0": 30, "t1": 15, "t2": 10, "t3": 20,
            "modules": 32768,
            "common_address": 1,
            "with_cp56time2a": with_cp56time2a,
            "acquisition_of_events_timer": 100,
            "cyclic_data_transmission_timer": 0,
            "timer": 100,
        },
        "redis": {
            "ip": "127.0.0.1", "port": 6379, "dbid": 0, "auth": "",
            "with_timestamp": 1, "precision": 6,
        },
        "points": points,
    }


def _make_iec104d_point(key, addr):
    """构造 iec104d point 条目。"""
    return {"key": key, "104addr": addr}


def _make_c4_config(instances):
    """构造 c4 配置文件（c4_iec104_client 段由 instances 提供）。"""
    return {
        "c4_shm_manager": {
            "writer": ["c4_iec104_client"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_iec104_client": instances,
        "c4_asfp2_client": [],
    }


def _make_c4_instance(
    name, iid, port, points,
    k=12, w=8, t0=5, t1=5, t2=3, t3=5,
    modules=32768, common_address=1, ioa_size=3,
    discard_cp56time2a=0, ignore_qds=0, it_timer=0, gi_timer=100,
    ip="127.0.0.1",
):
    """构造 c4_iec104_client 实例（c4_fun_00065 README §3.2 模板）。"""
    return {
        "name": name, "id": iid,
        "ip": ip, "port": port,
        "k": k, "w": w,
        "t0": t0, "t1": t1, "t2": t2, "t3": t3,
        "modules": modules,
        "common_address": common_address,
        "ioa_size": ioa_size,
        "discard_cp56time2a": discard_cp56time2a,
        "ignore_qds": ignore_qds,
        "it_timer": it_timer,
        "gi_timer": gi_timer,
        "points": points,
    }


def _make_c4_point(pid, addr, shm_id=0):
    """构造 c4_iec104_client point 条目。"""
    return {"id": pid, "addr": addr, "shm_id": shm_id}


# ──────────────────────────────────────────────
#  Fixtures
# ──────────────────────────────────────────────


@pytest.fixture
def start_iec104d(license_env, redis_server):
    """启动一个 iec104d 从站子进程（传 iec104d.json 路径 + ACQUISITION env），
    返回 (process, port)，teardown 时关闭并清理工作目录。"""
    procs = []
    pwds = []

    def _start(iec104d_json_path):
        with open(iec104d_json_path, "r") as f:
            cfg = json.load(f)
        port = cfg["iec104"]["port"]
        pwd = cfg["engine"]["pwd"]
        os.makedirs(pwd, exist_ok=True)
        log_dir = cfg.get("log", {}).get("dir", "log")
        os.makedirs(os.path.join(pwd, log_dir), exist_ok=True)
        pwds.append(pwd)

        env = os.environ.copy()
        env["ACQUISITION"] = license_env
        proc = subprocess.Popen(
            ["iec104d", "-c", iec104d_json_path],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(proc)
        _wait_port_listening(port, timeout=5.0)
        return proc, port

    yield _start

    for proc in procs:
        _stop_process(proc)
    for pwd in pwds:
        shutil.rmtree(pwd, ignore_errors=True)


@pytest.fixture
def start_iec104_client():
    """Function 级 fixture — 启动 c4_iec104_client（MCP initialize），
    返回工厂函数：每次调用启动一个新的 SUT 进程并返回 MCP 客户端句柄。"""
    clients = []

    def _start():
        binary = _find_iec104_client_binary()
        client = McpClient(binary)
        clients.append(client)
        return client

    yield _start

    for client in clients:
        client.close()
