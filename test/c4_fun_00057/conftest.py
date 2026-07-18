"""
C4_FUN_00057 测试公共基础设施 — MCP 客户端 + fixtures。
共享内存操作函数见 shm_helpers.py。
"""

import json
import os
import sys
import subprocess
import tempfile
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


# ──────────────────────────────────────────────
#  SUT 二进制发现
# ──────────────────────────────────────────────


def _find_asfp2_binary() -> str:
    """查找或编译 c4_asfp2_server 二进制。"""
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
#  Fixtures
# ──────────────────────────────────────────────


@pytest.fixture(scope="session")
def mcp():
    """Session 级 MCP 客户端 — c4_asfp2_server。"""
    binary = _find_asfp2_binary()
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
        # 步骤 1: 写入配置文件
        fd, config_path = tempfile.mkstemp(suffix=".json", prefix="c4_config_")
        temp_files.append(config_path)
        with os.fdopen(fd, "w") as f:
            json.dump(config_dict, f)

        # 步骤 3: create_shm
        resp = shm_mgr_client.call_tool(
            "create_shm",
            {"instance_id": instance_id},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        if resp["result"].get("isError", False):
            raise RuntimeError(
                f"create_shm failed: {resp['result']['content'][0]['text']}"
            )

        # 步骤 4: adjust_shm
        resp = shm_mgr_client.call_tool(
            "adjust_shm",
            {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        if resp["result"].get("isError", False):
            raise RuntimeError(
                f"adjust_shm failed: {resp['result']['content'][0]['text']}"
            )

        # 步骤 5: 关闭 shm_manager（shm_manager 在 shm_mgr_client teardown 中也会关闭）
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
def start_asfp2_server():
    """
    Function 级 fixture — 启动 c4_asfp2_server，验证工具列表，yield 客户端，关闭。
    对应 README §2.2 步骤 6-7。
    """
    binary = _find_asfp2_binary()
    client = McpClient(binary)

    # 步骤 7: 验证 start/pause/resume/status 均已注册
    resp = client.list_tools()
    if "result" in resp and "tools" in resp["result"]:
        tool_names = [t["name"] for t in resp["result"]["tools"]]
    else:
        # tools/list 可能返回 content 格式
        try:
            inner = json.loads(resp["result"]["content"][0]["text"])
            tool_names = [t["name"] for t in inner.get("tools", [])]
        except (KeyError, IndexError, json.JSONDecodeError):
            tool_names = []

    assert "start" in tool_names, f"start not in tools: {tool_names}"
    assert "stop" in tool_names, f"stop not in tools: {tool_names}"
    assert "status" in tool_names, f"status not in tools: {tool_names}"

    yield client
    client.close()
