"""
C4_FUN_00060 测试公共基础设施 — MCP 客户端 + fixtures。
共享内存操作函数见 shm_helpers.py。

复用 c4_fun_00057 的 McpClient / prepare_environment / isolated_shm / shm_mgr_client
模式，扩展 c4_asfp2_client 特定的 config 工厂和连接验证辅助函数。
"""

import json
import os
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
#  Binary Discovery
# ──────────────────────────────────────────────


def _find_asfp2_client_binary() -> str:
    """查找或编译 c4_asfp2_client 二进制（SUT）。"""
    path = os.environ.get("C4_ASFP2_CLIENT_PATH")
    if path and os.path.isfile(path):
        return path

    test_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(test_dir, "../../mcp/c4_asfp2_client/c4_asfp2_client"),
        os.path.join(test_dir, "../../mcp/c4_asfp2_client/build/c4_asfp2_client"),
        os.path.join(test_dir, "../../build/mcp/c4_asfp2_client/c4_asfp2_client"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return os.path.abspath(p)

    src_dir = os.path.abspath(os.path.join(test_dir, "../../mcp/c4_asfp2_client"))
    if os.path.isdir(src_dir):
        result = subprocess.run(
            ["go", "build", "-o", "c4_asfp2_client", "."],
            cwd=src_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return os.path.join(src_dir, "c4_asfp2_client")
        else:
            pytest.skip(
                f"Failed to build c4_asfp2_client: {result.stderr.strip()}"
            )

    pytest.skip(
        "c4_asfp2_client binary not found. "
        "Set C4_ASFP2_CLIENT_PATH env var or build from c4/mcp/c4_asfp2_client"
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


def _find_c4_asfp2_server_binary() -> str:
    """查找或编译 c4_asfp2_server 二进制（TC10 注入链路用）。"""
    path = os.environ.get("C4_ASFP2_SERVER_PATH")
    if path and os.path.isfile(path):
        return path

    test_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(test_dir, "../../mcp/c4_asfp2_server/c4_asfp2_server"),
        os.path.join(test_dir, "../../mcp/c4_asfp2_server/build/c4_asfp2_server"),
        os.path.join(test_dir, "../../build/mcp/c4_asfp2_server/c4_asfp2_server"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return os.path.abspath(p)

    src_dir = os.path.abspath(os.path.join(test_dir, "../../mcp/c4_asfp2_server"))
    if os.path.isdir(src_dir):
        result = subprocess.run(
            ["go", "build", "-o", "c4_asfp2_server", "."],
            cwd=src_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return os.path.join(src_dir, "c4_asfp2_server")
        else:
            pytest.skip(
                f"Failed to build c4_asfp2_server: {result.stderr.strip()}"
            )

    pytest.skip(
        "c4_asfp2_server binary not found. "
        "Set C4_ASFP2_SERVER_PATH env var or build from c4/mcp/c4_asfp2_server"
    )
    return ""  # unreachable — pytest.skip() always raises


# ──────────────────────────────────────────────
#  roots/list 回调工厂
# ──────────────────────────────────────────────


def _roots_callback(roots_list):
    """创建 on_request 回调：对 roots/list 返回 roots_list。"""

    def cb(method, params, request_id):
        if method == "roots/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"roots": roots_list},
            }
        return None

    return cb


# ──────────────────────────────────────────────
#  MCP Error / Success Assert Helpers
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


def _assert_mcp_success(resp: dict):
    """验证 MCP 响应为成功（isError: false, text: 'success'）。"""
    assert resp["result"].get("isError", False) is False, (
        f"Expected isError=false, got: {resp}"
    )
    assert resp["result"]["content"][0]["text"] == "success", (
        f"Expected 'success', got: {resp}"
    )


# ──────────────────────────────────────────────
#  Connection Verification Helpers
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


def _assert_port_listening(port: int, timeout: float = 1.0):
    """验证本地端口已监听。"""
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        s.close()
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        raise AssertionError(f"Port {port} is not listening: {e}")


def _has_established_connection(port: int) -> bool:
    """检查端口上是否存在 ESTABLISHED 状态的 TCP 连接。"""
    result = subprocess.run(
        ["ss", "-tnp", "sport", "=", f":{port}"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return "ESTAB" in result.stdout


def _assert_has_connection(port: int, timeout: float = 2.0):
    """轮询等待端口上出现 ESTABLISHED 连接。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _has_established_connection(port):
            return
        time.sleep(0.2)
    raise AssertionError(
        f"No ESTABLISHED connection on port {port} within {timeout}s"
    )


def _assert_no_connection(port: int, timeout: float = 3.0):
    """轮询等待端口上所有 ESTABLISHED 连接均已释放。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _has_established_connection(port):
            return
        time.sleep(0.2)
    raise AssertionError(
        f"Port {port} still has ESTABLISHED connection after {timeout}s"
    )


# ──────────────────────────────────────────────
#  asfp2_server Subprocess Helper
# ──────────────────────────────────────────────

ASFP2_SERVER = "/usr/local/bin/asfp2_server"


def run_asfp2_server(port: int):
    """启动 acquisition asfp2_server 在指定端口监听，返回 Popen 对象。

    轮询等待端口可连接后返回。调用方在 teardown 中负责 terminate/wait。
    """
    proc = subprocess.Popen(
        [ASFP2_SERVER, "-p", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # 等待 server 开始监听
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.3)
            s.close()
            return proc
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    proc.kill()
    proc.wait()
    raise RuntimeError(f"asfp2_server failed to start on port {port}")


# ──────────────────────────────────────────────
#  Fixtures
# ──────────────────────────────────────────────


@pytest.fixture(scope="session")
def mcp():
    """Session 级 MCP 客户端 — c4_asfp2_client。"""
    binary = _find_asfp2_client_binary()
    client = McpClient(binary)
    yield client
    client.close()


@pytest.fixture
def isolated_shm():
    """Function 级隔离 fixture。setup 预防性清理，teardown 释放。"""
    registered: list[str] = []

    def register(instance_id: str) -> None:
        registered.append(instance_id)
        try:
            shm_unlink(f"/c4_{instance_id}")
        except OSError:
            pass

    yield register

    for iid in registered:
        try:
            shm_unlink(f"/c4_{iid}")
        except OSError:
            pass


@pytest.fixture
def shm_mgr_client():
    """Function 级 fixture — 启动 c4_shm_manager，MCP initialize，yield 客户端，关闭。"""
    binary = _find_shm_manager_binary()
    client = McpClient(binary)
    yield client
    client.close()


@pytest.fixture
def prepare_environment(shm_mgr_client):
    """
    Function 级 fixture — 准备配置文件 + 共享内存。
    返回工厂函数 (config_dict, instance_id) → (config_path, instance_id)。
    内部完成 create_shm + adjust_shm，并在返回前关闭 shm_manager。
    """
    temp_files: list[str] = []

    def _prepare(config_dict: dict, instance_id: str):
        # 写入配置文件
        fd, config_path = tempfile.mkstemp(suffix=".json", prefix="c4_config_")
        temp_files.append(config_path)
        with os.fdopen(fd, "w") as f:
            json.dump(config_dict, f)

        # create_shm
        resp = shm_mgr_client.call_tool(
            "create_shm",
            {"instance_id": instance_id},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        if resp["result"].get("isError", False):
            raise RuntimeError(
                f"create_shm failed: {resp['result']['content'][0]['text']}"
            )

        # adjust_shm
        resp = shm_mgr_client.call_tool(
            "adjust_shm",
            {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        if resp["result"].get("isError", False):
            raise RuntimeError(
                f"adjust_shm failed: {resp['result']['content'][0]['text']}"
            )

        # 关闭 shm_manager
        shm_mgr_client.close()

        return config_path, instance_id

    yield _prepare

    # Teardown: 清理临时配置文件
    for path in temp_files:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.fixture
def start_asfp2_client():
    """
    Function 级 fixture — 返回工厂函数 (config_path) → McpClient。

    内部完成：启动 c4_asfp2_client → MCP initialize → list_tools 验证 →
    调用 start 工具（带 roots/list 回调）。teardown 关闭所有创建的 client。
    """
    binary = _find_asfp2_client_binary()
    clients: list[McpClient] = []

    def _start(config_path: str) -> McpClient:
        client = McpClient(binary)

        # 验证工具注册
        resp = client.list_tools()
        tool_names: list[str] = []
        if "result" in resp and "tools" in resp["result"]:
            tool_names = [t["name"] for t in resp["result"]["tools"]]
        else:
            try:
                inner = json.loads(resp["result"]["content"][0]["text"])
                tool_names = [t["name"] for t in inner.get("tools", [])]
            except (KeyError, IndexError, json.JSONDecodeError):
                tool_names = []

        assert "start" in tool_names, f"start not in tools: {tool_names}"
        assert "stop" in tool_names, f"stop not in tools: {tool_names}"

        # 调用 start — 配置路径通过 roots/list 回调传递
        resp = client.call_tool(
            "start",
            {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        _assert_mcp_success(resp)

        clients.append(client)
        return client

    yield _start

    # Teardown: 关闭所有创建的 client
    for client in clients:
        client.close()


# ──────────────────────────────────────────────
#  Config Factories
# ──────────────────────────────────────────────


def _make_standard_config(instance_id: str = "test_stop_restart"):
    """§3.1 标准配置：mock_writer + 单客户端 port=9900，2 points。

    c4_modbus_client 为 mock 写者，c4_asfp2_client 为被测读端。
    """
    return {
        "c4_shm_manager": {
            "writer": ["c4_modbus_client"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_modbus_client": [
            {
                "name": "模拟数据源",
                "id": "mock_writer",
                "ip": "127.0.0.1",
                "port": 502,
                "hton_register": 1,
                "hton_total": 0,
                "t0": 30,
                "t1": 10,
                "retries": 10,
                "coils_quantity_max": 2000,
                "registers_quantity_max": 125,
                "timer": 1000,
                "points": [
                    {"id": "pt_a", "uid": 1, "addr": 1000, "fun": 3, "type": 4, "swap": 2, "shm_id": 0},
                    {"id": "pt_b", "uid": 1, "addr": 1002, "fun": 3, "type": 4, "swap": 2, "shm_id": 0},
                ],
            }
        ],
        "c4_asfp2_client": [
            {
                "name": "停止重启测试客户端",
                "ip": "127.0.0.1",
                "port": 9900,
                "t0": 30,
                "t1": 0,
                "t2": 0,
                "smart": 1,
                "forward_kack": 255,
                "inverse_keep": 0,
                "timer": 100,
                "points": [
                    {"key": "mock_writer.pt_a", "addr": 1000, "shm_id": 0},
                    {"key": "mock_writer.pt_b", "addr": 1001, "shm_id": 0},
                ],
            }
        ],
    }


def _make_changed_config(instance_id: str = "test_changed"):
    """§3.2 变更后配置：ip=127.0.0.2，port=9901，新增 point（addr=2000）。"""
    return {
        "c4_shm_manager": {
            "writer": ["c4_modbus_client"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_modbus_client": [
            {
                "name": "模拟数据源",
                "id": "mock_writer",
                "ip": "127.0.0.1",
                "port": 502,
                "hton_register": 1,
                "hton_total": 0,
                "t0": 30,
                "t1": 10,
                "retries": 10,
                "coils_quantity_max": 2000,
                "registers_quantity_max": 125,
                "timer": 1000,
                "points": [
                    {"id": "pt_a", "uid": 1, "addr": 1000, "fun": 3, "type": 4, "swap": 2, "shm_id": 1},
                    {"id": "pt_b", "uid": 1, "addr": 1002, "fun": 3, "type": 4, "swap": 2, "shm_id": 2},
                ],
            }
        ],
        "c4_asfp2_client": [
            {
                "name": "变更配置测试客户端",
                "ip": "127.0.0.2",
                "port": 9901,
                "t0": 30,
                "t1": 0,
                "t2": 0,
                "smart": 1,
                "forward_kack": 255,
                "inverse_keep": 0,
                "timer": 100,
                "points": [
                    {"key": "mock_writer.pt_a", "addr": 1000, "shm_id": 1},
                    {"key": "mock_writer.pt_b", "addr": 1001, "shm_id": 2},
                ],
            }
        ],
    }


def _make_unreachable_config(instance_id: str = "test_unreachable"):
    """不可达目标配置：ip=192.0.2.1（TEST-NET-1），port=9999。"""
    return {
        "c4_shm_manager": {
            "writer": ["c4_modbus_client"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_modbus_client": [
            {
                "name": "模拟数据源",
                "id": "mock_writer",
                "ip": "127.0.0.1",
                "port": 502,
                "hton_register": 1,
                "hton_total": 0,
                "t0": 30,
                "t1": 10,
                "retries": 10,
                "coils_quantity_max": 2000,
                "registers_quantity_max": 125,
                "timer": 1000,
                "points": [
                    {"id": "pt_a", "uid": 1, "addr": 1000, "fun": 3, "type": 4, "swap": 2, "shm_id": 1},
                    {"id": "pt_b", "uid": 1, "addr": 1002, "fun": 3, "type": 4, "swap": 2, "shm_id": 2},
                ],
            }
        ],
        "c4_asfp2_client": [
            {
                "name": "不可达测试客户端",
                "ip": "192.0.2.1",
                "port": 9999,
                "t0": 30,
                "t1": 0,
                "t2": 0,
                "smart": 1,
                "forward_kack": 255,
                "inverse_keep": 0,
                "timer": 100,
                "points": [
                    {"key": "mock_writer.pt_a", "addr": 1000, "shm_id": 1},
                    {"key": "mock_writer.pt_b", "addr": 1001, "shm_id": 2},
                ],
            }
        ],
    }


def _make_tc10_config(instance_id: str, inject_port: int, target_port: int):
    """TC10 完整数据注入链路配置。

    c4_asfp2_server (inject) 监听 inject_port，接收 asfp2_client CLI 注入的数据，
    写入 SHM。c4_asfp2_client (SUT) 从 SHM 读取，发送到 target_port 的 asfp2_server。
    两者共享同一 SHM 实例，通过 key 映射关联。
    """
    return {
        "c4_shm_manager": {
            "writer": ["c4_asfp2_server"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_asfp2_server": [
            {
                "name": "TC10注入端",
                "id": f"{instance_id}_inject",
                "port": inject_port,
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
        "c4_asfp2_client": [
            {
                "name": "TC10 SUT",
                "ip": "127.0.0.1",
                "port": target_port,
                "t0": 30,
                "t1": 0,
                "t2": 0,
                "smart": 1,
                "forward_kack": 255,
                "inverse_keep": 0,
                "timer": 100,
                "points": [
                    {"key": f"{instance_id}_inject.pt_a", "addr": 1000, "shm_id": 0},
                    {"key": f"{instance_id}_inject.pt_b", "addr": 1001, "shm_id": 0},
                ],
            }
        ],
    }


# ──────────────────────────────────────────────
#  adjust_shm Helper（独立 shm_manager 子进程）
# ──────────────────────────────────────────────


def _run_adjust_shm(config_path: str, instance_id: Optional[str] = None):
    """
    启动独立 c4_shm_manager 子进程，MCP initialize，
    必要时 create_shm，然后 adjust_shm，关闭进程。

    用于 TC 中 stop 后调整 SHM 布局再 start 的场景。
    """
    binary = _find_shm_manager_binary()
    client = McpClient(binary)

    if instance_id is not None:
        resp = client.call_tool(
            "create_shm",
            {"instance_id": instance_id},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        text = resp["result"]["content"][0]["text"]
        if resp["result"].get("isError", False) and "ALREADY_EXISTS" not in text:
            raise RuntimeError(f"create_shm failed: {text}")

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


# ──────────────────────────────────────────────
#  asfp2_client CLI Helper（TC10 数据注入）
# ──────────────────────────────────────────────

ASFP2_CLIENT = "/usr/local/bin/asfp2_client"


def _run_asfp2_client_inject(
    port: int,
    begin_addr: int,
    end_addr: int,
    times: int = 3,
    timeout: int = 10,
):
    """运行 asfp2_client CLI 发送 ASFP2 数据包到指定端口。

    返回 (returncode, stdout, stderr)。
    """
    cmd = [
        ASFP2_CLIENT, "-s", "127.0.0.1", "-p", str(port),
        "-t", str(times),
        "-b", str(begin_addr), "-e", str(end_addr),
        "-B", "100", "-E", "200",
        "--type", "4",
        "--i0", "10", "--i1", "10",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout, result.stderr
