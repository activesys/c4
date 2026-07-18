"""
C4_FUN_00059 测试公共基础设施 — MCP 客户端 + fixtures。
SUT: c4_asfp2_client (MCP stdio JSON-RPC)。
共享内存操作函数见 shm_helpers.py。
"""

import json
import os
import sys
import subprocess
import tempfile
import time
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # type: ignore
from shm_helpers import shm_unlink


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
#  二进制发现
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


def _find_asfp2_server_binary() -> str:
    """查找或编译 c4_asfp2_server 二进制（连接验证辅助进程）。"""
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
#  配置构建助手
# ──────────────────────────────────────────────


def _make_client_config(port=9900, num_instances=1):
    """
    标准配置（§3.1 / §3.2）。
    port: 首个客户端实例的监听端口（多实例时递增）。
    num_instances: 客户端实例数量。
    """
    writer_instance = {
        "name": "\u6a21\u62df\u6570\u636e\u6e90",
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

    clients = []
    for i in range(num_instances):
        clients.append(
            {
                "name": f"\u6d4b\u8bd5\u53d1\u9001\u7aef{i + 1}",
                "ip": "127.0.0.1",
                "port": port + i,
                "t0": 30,
                "t1": 0,
                "t2": 0,
                "smart": 1,
                "forward_kack": 255,
                "inverse_keep": 0,
                "timer": 100,
                "points": [
                    {"key": "mock_writer.pt_a", "addr": 1000 + i, "shm_id": 0},
                    {"key": "mock_writer.pt_b", "addr": 1001 + i, "shm_id": 0},
                ],
            }
        )

    return {
        "c4_shm_manager": {
            "writer": ["c4_modbus_client"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_modbus_client": [writer_instance],
        "c4_asfp2_client": clients,
    }


def _make_unreachable_config():
    """
    不可达配置（§3.3）。
    单个客户端实例，目标 IP 为 192.0.2.1 (TEST-NET-1)，t0=5 以加速超时。
    """
    writer_instance = {
        "name": "\u6a21\u62df\u6570\u636e\u6e90",
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

    return {
        "c4_shm_manager": {
            "writer": ["c4_modbus_client"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_modbus_client": [writer_instance],
        "c4_asfp2_client": [
            {
                "name": "\u4e0d\u53ef\u8fbe\u6d4b\u8bd5",
                "ip": "192.0.2.1",
                "port": 9999,
                "t0": 5,
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


def _make_partial_unreachable_config():
    """
    部分不可达配置（§3.4）。
    实例 1 可达 (127.0.0.1:9901)，实例 2 不可达 (192.0.2.1:9999)。
    """
    writer_instance = {
        "name": "\u6a21\u62df\u6570\u636e\u6e90",
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

    return {
        "c4_shm_manager": {
            "writer": ["c4_modbus_client"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_modbus_client": [writer_instance],
        "c4_asfp2_client": [
            {
                "name": "\u53ef\u8fbe\u5b9e\u4f8b",
                "ip": "127.0.0.1",
                "port": 9901,
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
            },
            {
                "name": "\u4e0d\u53ef\u8fbe\u5b9e\u4f8b",
                "ip": "192.0.2.1",
                "port": 9999,
                "t0": 5,
                "t1": 0,
                "t2": 0,
                "smart": 1,
                "forward_kack": 255,
                "inverse_keep": 0,
                "timer": 100,
                "points": [
                    {"key": "mock_writer.pt_a", "addr": 1002, "shm_id": 0},
                    {"key": "mock_writer.pt_b", "addr": 1003, "shm_id": 0},
                ],
            },
        ],
    }


# ──────────────────────────────────────────────
#  断言助手
# ──────────────────────────────────────────────


def _assert_mcp_success(resp):
    """断言 MCP 工具调用成功（isError 不为 True）。"""
    result = resp.get("result", {})
    is_error = result.get("isError", False)
    assert not is_error, f"Expected success, got error: {resp}"


def _assert_mcp_error(resp, expected_code):
    """断言 MCP 工具调用失败，且错误文本包含 expected_code。"""
    result = resp.get("result", {})
    assert result.get("isError", False), f"Expected error, got success: {resp}"
    content = result.get("content", [])
    assert len(content) > 0, f"Expected error content, got: {resp}"
    text = content[0].get("text", "")
    assert expected_code in text, (
        f"Expected error code '{expected_code}' not found in: {text}"
    )


# ──────────────────────────────────────────────
#  Fixtures
# ──────────────────────────────────────────────


@pytest.fixture
def mcp():
    """Function 级 MCP 客户端 — c4_asfp2_client (SUT)。"""
    binary = _find_asfp2_client_binary()
    client = McpClient(binary)
    yield client
    client.close()


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
def prepare_environment(shm_mgr_client):
    """
    Function 级 fixture — 准备配置文件 + 共享内存。
    返回工厂函数 (config_dict, instance_id) → (config_path, instance_id)。
    内部完成 create_shm + adjust_shm，并在返回前关闭 shm_manager。
    """
    temp_files: list[str] = []

    def _prepare(config_dict: dict, instance_id: str):
        # 步骤 1: 写入配置文件
        fd, config_path = tempfile.mkstemp(suffix=".json", prefix="c4_config_")
        temp_files.append(config_path)
        with os.fdopen(fd, "w") as f:
            json.dump(config_dict, f)

        # 步骤 2: create_shm
        resp = shm_mgr_client.call_tool(
            "create_shm",
            {"instance_id": instance_id},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        if resp["result"].get("isError", False):
            raise RuntimeError(
                f"create_shm failed: {resp['result']['content'][0]['text']}"
            )

        # 步骤 3: adjust_shm — 分配 shm_id 给所有 point
        resp = shm_mgr_client.call_tool(
            "adjust_shm",
            {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        if resp["result"].get("isError", False):
            raise RuntimeError(
                f"adjust_shm failed: {resp['result']['content'][0]['text']}"
            )

        # 步骤 4: 关闭 shm_manager
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
def asfp2_servers():
    """
    Function 级 fixture — 启动 acquisition asfp2_server 作为连接目标。
    返回工厂函数 (*ports) → list[subprocess.Popen]。
    Teardown 终止所有启动的 server 进程。
    """
    processes: list[subprocess.Popen] = []

    def _start(*ports):
        binary = "/usr/local/bin/asfp2_server"
        for port in ports:
            proc = subprocess.Popen(
                [binary, "-p", str(port), "--t1", "0", "--t2", "0"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            processes.append(proc)
            time.sleep(0.3)
        return processes

    yield _start

    for proc in processes:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
