"""
C4_FUN_00062 测试公共基础设施 — MCP 客户端 + redis/modbusd/授权 fixtures。

对应 c4_fun_00062 README §2.7 的 fixture 契约：
  license_env           (session)  确保 $ACQUISITION/license/ 有有效授权
  start_modbusd         (function) 启动 modbusd 子进程，返回 (process, port)
  write_redis           (function) 用 redis_tool 写一个 Redis key
  prepare_environment   (function) 生成配置 → create_shm → adjust_shm → 关闭
  start_modbus_client   (function) 启动 c4_modbus_client（MCP initialize）

共享内存操作见 shm_helpers.py。
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # type: ignore
from shm_helpers import shm_unlink  # noqa: E402


# ──────────────────────────────────────────────
#  MCP Stdio Client
# ──────────────────────────────────────────────


class McpClient:
    """通过 MCP stdio JSON-RPC 与 SUT 进程通信。"""

    def __init__(self, binary_path: str):
        self.process = subprocess.Popen(
            [binary_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self._stdin = self.process.stdin
        self._stdout = self.process.stdout
        self._next_id = 0
        self._closed = False
        self._initialize()

    def _send(self, msg: dict) -> None:
        line = json.dumps(msg, ensure_ascii=False)
        self._stdin.write(line + "\n")
        self._stdin.flush()

    def _recv(self) -> dict:
        line = self._stdout.readline()
        if not line:
            raise EOFError("SUT process exited unexpectedly")
        return json.loads(line)

    def _initialize(self) -> None:
        """MCP 握手: initialize → 读响应 → initialized 通知。"""
        self._next_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"roots": {"listChanged": True}},
                    "clientInfo": {"name": "c4_test", "version": "1.0.0"},
                },
            }
        )
        resp = self._recv()
        if "error" in resp:
            raise RuntimeError(f"Initialize failed: {resp['error']}")
        self._stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
            + "\n"
        )
        self._stdin.flush()

    def list_tools(self) -> dict:
        """发送 tools/list 请求并返回响应。"""
        self._next_id += 1
        req_id = self._next_id
        self._send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/list",
                "params": {},
            }
        )
        while True:
            msg = self._recv()
            if "id" in msg and msg["id"] == req_id:
                return msg
            if "method" in msg:
                # 不应在 tools/list 期间收到请求，但防御性处理
                pass

    def call_tool(
        self,
        tool_name: str,
        arguments: dict,
        on_request: Optional[Callable] = None,
    ) -> dict:
        """
        调用 MCP 工具。on_request 签名 (method, params, request_id) → dict | None。
        """
        self._next_id += 1
        req_id = self._next_id

        self._send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
        )

        while True:
            msg = self._recv()
            if "id" in msg and msg["id"] == req_id:
                return msg
            if "method" in msg and on_request is not None:
                method = msg["method"]
                params = msg.get("params", {})
                response = on_request(method, params, msg["id"])
                if response is not None:
                    self._send(response)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._stdin.close()
        except Exception:
            pass
        try:
            self._stdout.close()
        except Exception:
            pass
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self._closed = True


# ──────────────────────────────────────────────
#  SUT / 工具二进制发现
# ──────────────────────────────────────────────


def _find_modbus_client_binary() -> str:
    """查找或编译 c4_modbus_client 二进制。"""
    path = os.environ.get("C4_MODBUS_CLIENT_PATH")
    if path and os.path.isfile(path):
        return path

    test_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(test_dir, "../../mcp/c4_modbus_client/c4_modbus_client"),
        os.path.join(test_dir, "../../mcp/c4_modbus_client/build/c4_modbus_client"),
        os.path.join(test_dir, "../../build/mcp/c4_modbus_client/c4_modbus_client"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return os.path.abspath(p)

    src_dir = os.path.abspath(os.path.join(test_dir, "../../mcp/c4_modbus_client"))
    if os.path.isdir(src_dir):
        result = subprocess.run(
            ["go", "build", "-o", "c4_modbus_client", "."],
            cwd=src_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return os.path.join(src_dir, "c4_modbus_client")
        else:
            pytest.skip(
                f"Failed to build c4_modbus_client: {result.stderr.strip()}"
            )

    pytest.skip(
        "c4_modbus_client binary not found. "
        "Set C4_MODBUS_CLIENT_PATH env var or build from c4/mcp/c4_modbus_client"
    )
    return ""  # unreachable — pytest.skip() always raises


def _find_shm_manager_binary() -> str:
    """查找或编译 c4_shm_manager 二进制。"""
    path = os.environ.get("C4_SHM_MANAGER_PATH")
    if path and os.path.isfile(path):
        return path

    test_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(test_dir, "../../mcp/c4_shm_manager/c4_shm_manager"),
        os.path.join(test_dir, "../../mcp/c4_shm_manager/build/c4_shm_manager"),
        os.path.join(test_dir, "../../build/mcp/c4_shm_manager/c4_shm_manager"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return os.path.abspath(p)

    src_dir = os.path.abspath(os.path.join(test_dir, "../../mcp/c4_shm_manager"))
    if os.path.isdir(src_dir):
        result = subprocess.run(
            ["go", "build", "-o", "c4_shm_manager", "."],
            cwd=src_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return os.path.join(src_dir, "c4_shm_manager")
        else:
            pytest.skip(
                f"Failed to build c4_shm_manager: {result.stderr.strip()}"
            )

    pytest.skip(
        "c4_shm_manager binary not found. "
        "Set C4_SHM_MANAGER_PATH env var or build from c4/mcp/c4_shm_manager"
    )
    return ""  # unreachable — pytest.skip() always raises


# ──────────────────────────────────────────────
#  通用 helper
# ──────────────────────────────────────────────


def _free_port() -> int:
    """分配一个当前空闲的 TCP 端口。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_port_listening(port: int, timeout: float = 5.0, interval: float = 0.05):
    """轮询等待本地端口开始监听。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=interval)
            s.close()
            return
        except (ConnectionRefusedError, OSError):
            time.sleep(interval)
    raise RuntimeError(f"Port {port} did not become listening within {timeout}s")


def _wait_port_released(port: int, timeout: float = 5.0, interval: float = 0.05):
    """轮询等待本地端口被释放。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=interval)
            s.close()
            time.sleep(interval)
        except (ConnectionRefusedError, OSError):
            return
    raise RuntimeError(f"Port {port} not released within {timeout}s")


def _stop_process(proc: subprocess.Popen, sig: int = signal.SIGINT):
    """优雅终止子进程，超时后强杀。"""
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(sig)
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _write_config_file(config_dict: dict) -> str:
    """将配置 dict 写入临时 JSON 文件，返回路径。调用方负责清理。"""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="c4_config_")
    with os.fdopen(fd, "w") as f:
        json.dump(config_dict, f)
    return path


def _assert_mcp_success(resp: dict):
    """验证 MCP 响应为成功（isError: false, text: 'success'）。"""
    assert resp["result"].get("isError", False) is False, (
        f"Expected isError=false, got: {resp}"
    )
    assert resp["result"]["content"][0]["text"] == "success", (
        f"Expected 'success', got: {resp}"
    )


def _assert_mcp_error(resp: dict, expected_prefix: str):
    """验证 MCP 响应为业务错误且错误消息以前缀开始。"""
    assert resp["result"]["isError"] is True, (
        f"Expected isError=true, got: {resp}"
    )
    text = resp["result"]["content"][0]["text"]
    assert text.startswith(expected_prefix), (
        f"Expected prefix '{expected_prefix}', got '{text}'"
    )


def wait_write_seq_advanced(
    shm_path_str: str, shm_id: int, seq_before: int,
    timeout: float = 3.0, interval: float = 0.05,
) -> None:
    """轮询重试等待指定 block 的 write_seq 递增（正向断言，c4_fun_00012 README §5.1）。"""
    from shm_helpers import read_write_seq  # noqa: E402

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_write_seq(shm_path_str, shm_id) > seq_before:
            return
        time.sleep(interval)
    raise RuntimeError(f"write_seq did not advance within {timeout}s")


def _run_adjust_shm(config_path: str, instance_id: str) -> None:
    """启动独立 c4_shm_manager → adjust_shm → 关闭（供 Stop-Start 协议二次调整）。"""
    binary = _find_shm_manager_binary()
    client = McpClient(binary)
    try:
        resp = client.call_tool("adjust_shm", {"instance_id": instance_id, "config_path": config_path})
        if resp["result"].get("isError", False):
            raise RuntimeError(
                f"adjust_shm failed: {resp['result']['content'][0]['text']}"
            )
    finally:
        client.close()


# ──────────────────────────────────────────────
#  配置工厂（modbusd 设备端 + c4_modbus_client SUT 端）
# ──────────────────────────────────────────────


def _make_modbusd_config(port, points, hton_register=1, hton_total=0, swap=0):
    """构造 modbusd 设备端配置（c4_fun_00062 README §3.1 模板）。"""
    pwd = tempfile.mkdtemp(prefix="c4_modbusd_")
    return {
        "engine": {"pwd": pwd, "stop_check": 100},
        "log": {"dir": "log", "file": "log.log", "level": 1, "debug_time": 300, "size": 128},
        "modbus": {
            "ip": "127.0.0.1", "port": port,
            "hton_register": hton_register, "hton_total": hton_total,
            "swap": swap, "timer": 100,
        },
        "redis": {
            "ip": "127.0.0.1", "port": 6379, "dbid": 0, "auth": "",
            "with_timestamp": 1, "precision": 6,
        },
        "points": points,
    }


def _make_modbusd_point(key, modbusaddr, funcode, type_):
    """构造 modbusd point 条目。"""
    return {"key": key, "modbusaddr": modbusaddr, "funcode": funcode, "type": type_}


def _make_c4_config(instances):
    """构造 c4 配置文件（c4_modbus_client 段由 instances 提供）。"""
    return {
        "c4_shm_manager": {
            "writer": ["c4_modbus_client"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_modbus_client": instances,
        "c4_asfp2_client": [],
    }


def _make_c4_instance(
    name, iid, port, points,
    t0=5, t1=5, retries=3,
    coils_quantity_max=2000, registers_quantity_max=125,
    hton_register=1, hton_total=0, timer=100,
    ip="127.0.0.1",
):
    """构造 c4_modbus_client 实例（c4_fun_00062 README §3.2 模板）。"""
    return {
        "name": name, "id": iid,
        "ip": ip, "port": port,
        "t0": t0, "t1": t1, "retries": retries,
        "coils_quantity_max": coils_quantity_max,
        "registers_quantity_max": registers_quantity_max,
        "hton_register": hton_register, "hton_total": hton_total, "timer": timer,
        "points": points,
    }


def _make_c4_point(pid, uid, addr, fun, type_, swap=0, shm_id=0):
    """构造 c4_modbus_client point 条目。"""
    return {
        "id": pid, "uid": uid, "addr": addr, "fun": fun,
        "type": type_, "swap": swap, "shm_id": shm_id,
    }


# ──────────────────────────────────────────────
#  Fixtures
# ──────────────────────────────────────────────


@pytest.fixture(scope="session")
def license_env():
    """确保 $ACQUISITION/license/ 有**有效**授权，返回 ACQUISITION 环境变量路径。"""
    acquisition = os.environ.get("ACQUISITION", "/var/acquisition")
    license_dir = os.path.join(acquisition, "license")

    # 检测现有授权是否有效（license_check 输出含 "Authorization to this machine"）
    if os.path.isdir(license_dir):
        dat = os.path.join(license_dir, "license.dat")
        privates = [f for f in os.listdir(license_dir) if f.endswith(".private")]
        if privates and os.path.isfile(dat):
            for p in privates:
                result = subprocess.run(
                    ["license_check", "-c", os.path.join(license_dir, p), "-l", dat],
                    capture_output=True, text=True,
                )
                if "Authorization to this machine" in (result.stdout + result.stderr):
                    return acquisition

    # 缺失或无效时生成（需 root 权限，见 README §5.1）
    os.makedirs(license_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="c4_license_") as tmp:
        result = subprocess.run(
            ["license_tool"], cwd=tmp, capture_output=True, text=True
        )
        if result.returncode != 0:
            pytest.skip(
                "license_tool unavailable and no valid license found: "
                f"{result.stderr.strip()}"
            )
        publics = [f for f in os.listdir(tmp) if f.endswith(".public")]
        privates = [f for f in os.listdir(tmp) if f.endswith(".private")]
        if not publics or not privates:
            pytest.skip("license_tool did not generate key files")

        result = subprocess.run(
            ["license_gen", "-c", os.path.join(tmp, publics[0]), "-n", "test_user"],
            cwd=tmp, capture_output=True, text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"license_gen failed: {result.stderr.strip()}")

        try:
            for f in publics + privates:
                shutil.copy(os.path.join(tmp, f), license_dir)
            shutil.copy(os.path.join(tmp, "license.dat"), license_dir)
        except PermissionError as exc:
            pytest.skip(
                f"cannot write license to {license_dir} (need root): {exc}"
            )

    return acquisition


@pytest.fixture(scope="session")
def redis_server():
    """确保 redis-server 在 127.0.0.1:6379 可达（已运行则复用，否则启动子进程）。"""
    try:
        s = socket.create_connection(("127.0.0.1", 6379), timeout=1.0)
        s.close()
        yield  # 已在运行，直接复用
        return
    except (ConnectionRefusedError, OSError):
        pass

    redis_bin = shutil.which("redis-server")
    if redis_bin is None:
        pytest.skip("redis-server not found and 127.0.0.1:6379 not reachable")

    proc = subprocess.Popen(
        [redis_bin, "--bind", "127.0.0.1", "--port", "6379",
         "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _wait_port_listening(6379, timeout=5.0)
    except RuntimeError:
        proc.kill()
        proc.wait()
        raise
    yield
    _stop_process(proc)


@pytest.fixture
def start_modbusd(license_env, redis_server):
    """启动一个 modbusd 子进程（传 modbusd.json 路径 + ACQUISITION env），
    返回 (process, port)，teardown 时关闭。"""
    procs = []
    pwds = []

    def _start(modbusd_json_path):
        with open(modbusd_json_path, "r") as f:
            cfg = json.load(f)
        port = cfg["modbus"]["port"]
        pwd = cfg["engine"]["pwd"]
        os.makedirs(pwd, exist_ok=True)
        log_dir = cfg.get("log", {}).get("dir", "log")
        os.makedirs(os.path.join(pwd, log_dir), exist_ok=True)
        pwds.append(pwd)

        env = os.environ.copy()
        env["ACQUISITION"] = license_env
        proc = subprocess.Popen(
            ["modbusd", "-c", modbusd_json_path],
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
def write_redis(redis_server):
    """调用 redis_tool 写一个 Redis key 值（不带 -n，写带时间戳结构体）。"""
    keys = []

    def _write(key, value):
        cmd = [
            "redis_tool", "-s", "127.0.0.1", "-P", key,
            "-w", "-V", str(value), "-t", "1",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise RuntimeError(f"redis_tool write failed: {result.stderr.strip()}")
        keys.append(key)

    yield _write

    # teardown：清理写入的 Redis key
    for key in keys:
        subprocess.run(
            ["redis-cli", "-h", "127.0.0.1", "-p", "6379", "DEL", key],
            capture_output=True,
        )


@pytest.fixture
def shm_mgr_client():
    """Function 级 fixture — 启动 c4_shm_manager，MCP initialize，yield 客户端，关闭。"""
    binary = _find_shm_manager_binary()
    client = McpClient(binary)
    yield client
    client.close()


@pytest.fixture
def isolated_shm():
    """Function 级隔离 fixture。setup 预防性清理，teardown 释放。"""
    registered: list = []

    def register(instance_id: str) -> None:
        registered.append(instance_id)
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


@pytest.fixture
def prepare_environment(shm_mgr_client):
    """
    Function 级 fixture — 准备配置文件 + 共享内存。
    返回工厂函数 (config_dict, instance_id) → (config_path, instance_id)。
    内部完成 create_shm + adjust_shm，并在返回前关闭 shm_manager。
    """
    temp_files: list = []

    def _prepare(config_dict: dict, instance_id: str):
        fd, config_path = tempfile.mkstemp(suffix=".json", prefix="c4_config_")
        temp_files.append(config_path)
        with os.fdopen(fd, "w") as f:
            json.dump(config_dict, f)

        resp = shm_mgr_client.call_tool(
            "create_shm",
            {"instance_id": instance_id, "config_path": config_path},
        )
        if resp["result"].get("isError", False):
            raise RuntimeError(
                f"create_shm failed: {resp['result']['content'][0]['text']}"
            )

        resp = shm_mgr_client.call_tool(
            "adjust_shm", {"instance_id": instance_id, "config_path": config_path},
        )
        if resp["result"].get("isError", False):
            raise RuntimeError(
                f"adjust_shm failed: {resp['result']['content'][0]['text']}"
            )

        shm_mgr_client.close()

        return config_path, instance_id

    yield _prepare

    for path in temp_files:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.fixture
def start_modbus_client():
    """Function 级 fixture — 启动 c4_modbus_client（MCP initialize），
    返回工厂函数：每次调用启动一个新的 SUT 进程并返回 MCP 客户端句柄。"""
    clients: list = []

    def _start():
        binary = _find_modbus_client_binary()
        client = McpClient(binary)
        clients.append(client)
        return client

    yield _start

    for client in clients:
        client.close()
