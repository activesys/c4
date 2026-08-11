"""
C4 Agent 功能测试公共基础设施 — conftest.py

提供:
  - McpClient: MCP stdio JSON-RPC 客户端
  - AgentHandle: Agent 进程 + HTTP API 封装
  - SSEEventStream: HTTP SSE 流客户端
  - ChatHelper: 对话辅助（send / send_with_file / confirm）
  - Fixtures: agent_binary, shm_manager_binary, registry_dir, agent, chat
  - Helpers: write_agent_json, write_config_json, corrupt_config_json, write_config_bak
  - pytest markers: llm (L2 tests)

设计依据: c4/test/agent/README.md §2.2-2.3
参考实现: c4/test/c4_fun_00057/conftest.py (McpClient pattern)
"""

import atexit
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from typing import Any, Callable, Generator, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest  # type: ignore

# ──────────────────────────────────────────────
#  项目根路径
# ──────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_DIR = _PROJECT_ROOT / "config"


# ──────────────────────────────────────────────
#  MCP Stdio Client (adapt from c4_fun_00057)
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
            raise EOFError("MCP process exited unexpectedly")
        return json.loads(line)

    def _initialize(self) -> None:
        """MCP 握手: initialize → 读响应 → initialized 通知。"""
        self._next_id += 1
        self._send({
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"roots": {"listChanged": True}},
                "clientInfo": {"name": "c4_test", "version": "1.0.0"},
            },
        })
        resp = self._recv()
        if "error" in resp:
            raise RuntimeError(f"MCP initialize failed: {resp['error']}")
        self._stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        self._stdin.flush()

    def list_tools(self) -> dict:
        """发送 tools/list 请求并返回响应。"""
        self._next_id += 1
        req_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": req_id, "method": "tools/list", "params": {}})
        while True:
            msg = self._recv()
            if "id" in msg and msg["id"] == req_id:
                return msg

    def call_tool(
        self, tool_name: str, arguments: dict, on_request: Optional[Callable] = None
    ) -> dict:
        """调用 MCP 工具。on_request 签名 (method, params, request_id) → dict | None。"""
        self._next_id += 1
        req_id = self._next_id
        self._send({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        })
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
        """关闭 MCP 客户端并终止进程。"""
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
#  SSE Event Stream
# ──────────────────────────────────────────────


class SSEEvent:
    """单个 SSE 事件。event 字段为 null 时等效于 'message'。"""

    def __init__(self, event_type: str, data: str):
        self.type = event_type  # event 字段值
        self.data = data


class SSEEventStream:
    """
    HTTP 流式 SSE 客户端。

    用法:
        with SSEEventStream(url, data=payload, method="POST") as stream:
            for event in stream.events:
                ...
            text = stream.text_content()
    """

    def __init__(
        self,
        url: str = "",
        data: Optional[Any] = None,
        headers: Optional[dict] = None,
        method: str = "POST",
        timeout: float = 120.0,
    ):
        self._url = url
        self._data: Any = data
        self._headers = headers or {}
        self._method = method
        self._timeout = timeout
        self._response: Any = None
        self.events: list[SSEEvent] = []
        self._collected: bool = False

    def __enter__(self):
        # 如果 _response 已由 upload() 提前设置（绕过 __init__），直接返回
        if self._response is not None:
            return self
        body: Optional[bytes] = None
        if self._data is not None:
            body = self._data.encode("utf-8") if isinstance(self._data, str) else self._data
        req = Request(self._url, data=body, headers=self._headers, method=self._method)
        self._response = urlopen(req, timeout=self._timeout)
        return self

    def __exit__(self, *args):
        if self._response:
            try:
                self._response.close()
            except Exception:
                pass

    def __iter__(self):
        self._collect_events()
        return iter(self.events)

    def _collect_events(self) -> None:
        """从 HTTP 响应流中解析 SSE 事件。"""
        if self._collected:
            return
        self._collected = True

        if self._response is None:
            raise RuntimeError("SSEEventStream._collect_events() called before __enter__")
        current_data: list[str] = []
        current_event: Optional[str] = None

        for line_bytes in self._response:
            line = line_bytes.decode("utf-8").rstrip("\r\n")

            if line.startswith("event:"):
                current_event = line[6:].strip()
            elif line.startswith("data:"):
                current_data.append(line[5:].strip())
            elif line == "":
                # 空行表示一个完整事件
                if current_data:
                    event_type = current_event or "message"
                    self.events.append(SSEEvent(event_type, "\n".join(current_data)))
                    current_data = []
                    current_event = None
            # 非 event/data 前缀的行忽略

        # 流结束时如有未完成的 data，也计入
        if current_data:
            event_type = current_event or "message"
            self.events.append(SSEEvent(event_type, "\n".join(current_data)))

    def wait_for_event(self, event_type: str, timeout: float = 30.0) -> Optional[SSEEvent]:
        """
        阻塞等待特定类型的事件。timeout 秒后返回 None。
        注意：调用此方法后会消费整个流。
        """
        self._collect_events()
        for evt in self.events:
            if evt.type == event_type:
                return evt
        return None

    def text_content(self) -> str:
        """
        拼接所有 assistant 消息文本。

        服务端 SSE data 载荷为 JSON（{type:"text", content:"..."} 等），
        此处提取 type=="text" 的 content 字段；tool_call/tool_result
        为内部事件不进入对话文本。非 JSON data 原样保留。
        """
        self._collect_events()
        parts: list[str] = []
        for evt in self.events:
            if evt.type not in ("assistant", "message"):
                continue
            try:
                payload: Any = json.loads(evt.data)
            except (json.JSONDecodeError, TypeError):
                parts.append(evt.data)
                continue
            if isinstance(payload, dict) and payload.get("type") == "text":
                content = payload.get("content")
                if isinstance(content, str):
                    parts.append(content)
        return "\n".join(parts)


# ──────────────────────────────────────────────
#  Binary Discovery Helpers
# ──────────────────────────────────────────────


def _find_binary(
    env_var: str,
    name: str,
    extra_candidates: Optional[list[str]] = None,
) -> str:
    """
    通用二进制查找:
    1. 环境变量 env_var
    2. extra_candidates
    3. /usr/local/bin/<name>
    4. <PROJECT_ROOT>/mcp/<dir>/<name>
    5. <PROJECT_ROOT>/mcp/<dir>/build/<name>
    6. <PROJECT_ROOT>/build/mcp/<dir>/<name>
    7. go build 自动编译
    """
    path = os.environ.get(env_var)
    if path and os.path.isfile(path):
        return path

    candidates = list(extra_candidates or [])
    candidates.append(f"/usr/local/bin/{name}")
    candidates.append(str(_PROJECT_ROOT / "mcp" / name / name))
    candidates.append(str(_PROJECT_ROOT / "mcp" / name / "build" / name))
    candidates.append(str(_PROJECT_ROOT / "build" / "mcp" / name / name))

    for p in candidates:
        if os.path.isfile(p):
            return os.path.abspath(p)

    # go build 自动编译
    src_dir = _PROJECT_ROOT / "mcp" / name
    if src_dir.is_dir():
        result = subprocess.run(
            ["go", "build", "-o", name, "."],
            cwd=str(src_dir),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return str(src_dir / name)
        else:
            pytest.skip(f"Failed to build {name}: {result.stderr.strip()}")

    pytest.skip(
        f"{name} binary not found. Set {env_var} env var or build from c4/mcp/{name}"
    )
    return ""  # unreachable


def _find_agent_binary() -> str:
    """查找 c4_agent 可执行文件（TypeScript → node 或编译产物）。"""
    path = os.environ.get("C4_AGENT_PATH")
    if path and os.path.isfile(path):
        return path

    # c4_agent 可能是二进制或 node 入口
    candidates = [
        "/usr/local/bin/c4_agent",
        str(_PROJECT_ROOT / "agent" / "dist" / "index.js"),
        str(_PROJECT_ROOT / "agent" / "build" / "index.js"),
        str(_PROJECT_ROOT / "build" / "agent" / "c4_agent"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return os.path.abspath(p)

    pytest.skip(
        "c4_agent binary not found. "
        "Set C4_AGENT_PATH env var or build from c4/agent/"
    )
    return ""  # unreachable


# ──────────────────────────────────────────────
#  Port Utilities
# ──────────────────────────────────────────────


def _find_free_port() -> int:
    """找到一个可用的 TCP 端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ──────────────────────────────────────────────
#  Config Helpers
# ──────────────────────────────────────────────


def write_agent_json(
    config_dir: Path,
    registry_dir: Path,
    shm_manager_binary: str,
    port: int,
) -> None:
    """
    写入最小 agent.json。
    LLM 配置中 temperature=0 确保确定性输出；
    server 监听指定端口；registry 路径指向 mcp-registry/。
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    agent_config = {
        "model": {
            "provider": "deepseek",
            "name": "deepseek-chat",
            "temperature": 0,
            "max_tokens": 4096,
            "api_key_env": "DEEPSEEK_API_KEY",
        },
        "server": {
            "host": "127.0.0.1",
            "port": port,
            "cors_origin": "*",
        },
        "mcp_registry": {
            "path": str(registry_dir),
        },
        "shm_manager": {
            "binary": shm_manager_binary,
            "instance_id": "c4_test",
            "config_path": str(config_dir / "config.json"),
        },
        "state": {
            "backend": "filesystem",
            "path": str(config_dir / "state"),
        },
        "logging": {
            "level": "info",
            "dir": str(config_dir / "logs"),
        },
    }
    agent_path = config_dir / "agent.json"
    agent_path.write_text(json.dumps(agent_config, indent=2, ensure_ascii=False))


def write_config_json(config_dir: Path, content: Optional[dict]) -> None:
    """
    写入 config.json。content=None 表示不创建（模拟首次启动）。
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.json"
    if content is None:
        # 确保文件不存在（模拟首次启动）
        if config_path.exists():
            config_path.unlink()
        return
    config_path.write_text(json.dumps(content, indent=2, ensure_ascii=False))


def corrupt_config_json(config_dir: Path) -> None:
    """将 config.json 截断为损坏的 JSON。"""
    config_path = config_dir / "config.json"
    raw = config_path.read_text()
    # 截断到倒数第二个 } — 产生语法错误
    idx = raw.rfind("}")
    if idx > 0:
        config_path.write_text(raw[:idx])


def write_config_bak(config_dir: Path, content: dict) -> None:
    """写入 config.json.bak（用于损坏恢复测试）。"""
    config_dir.mkdir(parents=True, exist_ok=True)
    bak_path = config_dir / "config.json.bak"
    bak_path.write_text(json.dumps(content, indent=2, ensure_ascii=False))


# ──────────────────────────────────────────────
#  AgentHandle
# ──────────────────────────────────────────────


class AgentHandle:
    """封装 Agent 进程 + HTTP 访问。"""

    def __init__(
        self,
        process: subprocess.Popen,
        base_url: str,
        port: int,
        config_dir: Path,
        shm_ids: set[int],
    ):
        self.process = process
        self.base_url = base_url
        self.port = port
        self.config_dir = config_dir
        self._shm_ids: set[int] = shm_ids  # 注册的 shmid 用于 teardown 清理

    def _http_get(self, path: str) -> dict:
        """GET 请求 → 解析 JSON 响应。"""
        url = f"{self.base_url}{path}"
        with urlopen(url, timeout=10.0) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)

    def get_services(self) -> Any:
        """GET /api/services → Registry L1 服务的列表。"""
        data = self._http_get("/api/services")
        if isinstance(data, dict) and "services" in data:
            data = data["services"]
        return data

    def get_state(self) -> dict:
        """GET /api/state → {phase, hasAccessPlan, lastError}。"""
        return self._http_get("/api/state")

    def chat(self, message: str, history: list | None = None) -> SSEEventStream:
        """
        POST /api/chat → SSE 流。

        返回 SSEEventStream 上下文管理器。
        用法:
            with agent.chat("你好") as stream:
                text = stream.text_content()
        """
        body: dict = {"message": message}
        if history:
            body["history"] = history
        payload = json.dumps(body)
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        return SSEEventStream(
            f"{self.base_url}/api/chat",
            data=payload,
            headers=headers,
            method="POST",
            timeout=120.0,
        )

    def upload(self, file_path: str, message: str) -> SSEEventStream:
        """
        POST /api/upload (multipart) + chat 消息 → SSE 流。

        用法:
            with agent.upload("/path/to/points.xlsx", "接入此设备") as stream:
                text = stream.text_content()
        """
        import email.parser
        from io import BytesIO

        boundary = "----C4TestBoundary"
        body_lines: list[str] = []
        body_lines.append(f"--{boundary}")
        body_lines.append(
            f'Content-Disposition: form-data; name="file"; filename="{os.path.basename(file_path)}"'
        )
        body_lines.append("Content-Type: application/octet-stream")
        body_lines.append("")
        with open(file_path, "rb") as f:
            file_content = f.read()
        body_lines_b = (
            "\r\n".join(body_lines).encode("utf-8")
            + b"\r\n"
            + file_content
            + b"\r\n"
        )
        body_lines_b += f"--{boundary}".encode("utf-8")
        body_lines_b += b"\r\n"
        body_lines_b += b'Content-Disposition: form-data; name="message"\r\n\r\n'
        body_lines_b += message.encode("utf-8") + b"\r\n"
        body_lines_b += f"--{boundary}--\r\n".encode("utf-8")

        req = Request(
            f"{self.base_url}/api/upload",
            data=body_lines_b,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        # SSEEventStream 直接使用已准备好的 response
        stream = SSEEventStream.__new__(SSEEventStream)
        stream._url = f"{self.base_url}/api/upload"
        stream._response = urlopen(req, timeout=120.0)
        stream._collected = False
        stream.events = []
        return stream

    def kill(self) -> None:
        """SIGKILL Agent 进程（模拟崩溃）。"""
        if self.process and self.process.poll() is None:
            self.process.kill()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def restart(self) -> None:
        """
        重新启动 Agent 进程并等待就绪。
        使用相同的 config_dir 和端口。
        """
        agent_binary = os.environ.get("C4_AGENT_PATH", "")
        if not agent_binary or not os.path.isfile(agent_binary):
            # 尝试从 agent.json 推断
            agent_binary = _find_agent_binary()

        cmd = [agent_binary, "--config-dir", str(self.config_dir)]
        if agent_binary.endswith(".js"):
            cmd = ["node", *cmd]
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._wait_ready()

    def _wait_ready(self, timeout: float = 60.0, interval: float = 0.5) -> None:
        """轮询 GET /api/services 直到返回 200。"""
        deadline = time.time() + timeout
        last_error = None
        while time.time() < deadline:
            try:
                with urlopen(
                    f"{self.base_url}/api/services", timeout=interval
                ) as resp:
                    if resp.status == 200:
                        return
            except Exception as e:
                last_error = e
            time.sleep(interval)
        raise TimeoutError(
            f"Agent did not become ready within {timeout}s. Last error: {last_error}"
        )


# ──────────────────────────────────────────────
#  ChatHelper
# ──────────────────────────────────────────────


class ChatHelper:
    """
    对话辅助类，封装 Agent 的 chat / upload / confirm 操作。

    通过 chat() fixture 获取。
    自动跟踪多步对话的上下文（历史消息），确保 agent 能跨步保持状态。

    用法:
        def test_hello(chat):
            stream = chat.send("你好")
            assert "你好" in stream.text_content()
    """

    def __init__(self, agent: AgentHandle):
        self._agent = agent
        self._history: list[dict] = []

    def send(self, message: str) -> SSEEventStream:
        """POST /api/chat（含历史上下文），返回 SSEEventStream。"""
        history = list(self._history)
        self._history.append({"role": "user", "content": message})
        return self._agent.chat(message, history=history)

    def send_with_file(self, message: str, file_path: str) -> SSEEventStream:
        """POST /api/upload（含历史上下文），返回 SSEEventStream。"""
        self._history.append({"role": "user", "content": message})
        return self._agent.upload(file_path, message)

    def record_response(self, text: str) -> None:
        """记录 agent 的回复文本到历史上下文。"""
        if text:
            self._history.append({"role": "assistant", "content": text})

    def confirm(self, interrupt_id: str) -> SSEEventStream:
        """
        发送确认消息以通过 interrupt 检查点。
        实现方式：POST /api/chat 并附带 interrupt_id 上下文。
        """
        return self._agent.chat(f"[confirm interrupt_id={interrupt_id}]")


# ──────────────────────────────────────────────
#  Shm Cleanup Helpers
# ──────────────────────────────────────────────

# 全局注册表：session 级 shmid 集合，atexit 兜底清理
_SESSION_SHM_IDS: set[int] = set()


def _cleanup_shm_ids(shm_ids: set[int]) -> None:
    """清理共享内存段：ipcrm -M <shmid>。"""
    for shmid in shm_ids:
        try:
            subprocess.run(
                ["ipcrm", "-M", str(shmid)],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass


def _session_atexit_cleanup() -> None:
    """session 级 atexit 兜底：清理所有注册的共享内存。"""
    _cleanup_shm_ids(_SESSION_SHM_IDS)
    _SESSION_SHM_IDS.clear()


atexit.register(_session_atexit_cleanup)


def _collect_shm_ids_from_config(config: dict) -> set[int]:
    """从 config.json 中收集所有 services[].points[].shm_id。"""
    ids: set[int] = set()
    for key, value in config.items():
        if key == "c4_shm_manager":
            continue
        if isinstance(value, list):
            for instance in value:
                if isinstance(instance, dict):
                    for pt in instance.get("points", []):
                        sid = pt.get("shm_id", 0)
                        if sid > 0:
                            ids.add(sid)
    return ids


# ──────────────────────────────────────────────
#  Fixtures
# ──────────────────────────────────────────────


@pytest.fixture(scope="session")
def agent_binary() -> str:
    """
    返回 c4_agent 可执行文件路径。
    从 C4_AGENT_PATH 或自动查找/编译。
    """
    return _find_agent_binary()


@pytest.fixture(scope="session")
def shm_manager_binary() -> str:
    """
    返回 c4_shm_manager 二进制路径。
    从 C4_SHM_MANAGER_PATH 或自动查找/编译。
    """
    return _find_binary("C4_SHM_MANAGER_PATH", "c4_shm_manager")


def _find_binary_for_service(service_type: str) -> str | None:
    """Find the actual binary path for a given MCP service type."""
    import pytest
    try:
        binary = _find_binary(f"C4_{service_type.removeprefix('c4_').upper()}_PATH", service_type)
        return binary
    except pytest.skip.Exception:
        return None  # binary not found, but registry entry still valid for schema lookup


@pytest.fixture(scope="session")
def registry_dir(tmp_path_factory) -> Path:
    """
    Session 级 fixture — 制备 mcp-registry/ 目录。
    复制 config/mcp-registry/*.json 到临时路径，并修正 binary_path 为实际编译产物路径。
    """
    tmp = tmp_path_factory.mktemp("mcp_registry")
    src_registry = _CONFIG_DIR / "mcp-registry"
    if src_registry.is_dir():
        for src_file in src_registry.glob("*.json"):
            data = json.loads(src_file.read_text())
            svc = data.get("service_type", "")
            binary = _find_binary_for_service(svc)
            if binary:
                data["binary_path"] = binary
            tmp_file = tmp / src_file.name
            tmp_file.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return tmp


@pytest.fixture(scope="function")
def agent(
    agent_binary: str,
    shm_manager_binary: str,
    registry_dir: Path,
    tmp_path: Path,
) -> Generator[AgentHandle, None, None]:
    """
    Function 级隔离的 Agent 实例。

    生命周期:
      1. 制备 tmp_path 作为配置目录（agent.json + config.json + mcp-registry/）
      2. 启动 c4_shm_manager
      3. 启动 c4_agent --config-dir <tmp_path>
      4. 轮询 GET /api/services 直到返回 200（Agent 就绪）
      5. yield AgentHandle(base_url, process, config_dir)
      6. teardown: SIGTERM → 等待 10s → SIGKILL → ipcrm 清理 shm → session atexit 兜底
    """
    config_dir = tmp_path / "etc_c4"
    port = _find_free_port()

    # 1. 制备 agent.json
    write_agent_json(config_dir, registry_dir, shm_manager_binary, port)

    # 2. 默认不创建 config.json（模拟首次启动），各测试自行调用 write_config_json
    config_path = config_dir / "config.json"
    if config_path.exists():
        config_path.unlink()

    # 3. 启动 c4_agent
    cmd = [agent_binary, "--config-dir", str(config_dir)]
    if agent_binary.endswith(".js"):
        cmd = ["node", *cmd]
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    base_url = f"http://127.0.0.1:{port}"

    # 4. 等待就绪
    deadline = time.time() + 60.0
    last_error = None
    ready = False
    while time.time() < deadline:
        try:
            with urlopen(f"{base_url}/api/services", timeout=0.5) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception as e:
            last_error = e
        time.sleep(0.5)

    if not ready:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        pytest.fail(
            f"Agent did not become ready within 60s. "
            f"Last error: {last_error}. "
            f"Check agent logs at {config_dir / 'logs'}"
        )

    shm_ids: set[int] = set()
    handle = AgentHandle(process, base_url, port, config_dir, shm_ids)

    yield handle

    # ── Teardown ──
    # 收集当前 config.json 中的 shm_id
    try:
        if (config_dir / "config.json").exists():
            config_data = json.loads((config_dir / "config.json").read_text())
            shm_ids = _collect_shm_ids_from_config(config_data)
    except Exception:
        pass

    # Step 1: SIGTERM → 等待 10s
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # Step 2: SIGKILL
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    # Step 3: 清理 shm（ipcrm）
    _cleanup_shm_ids(shm_ids)

    # Step 4: 注册到 session atexit 兜底
    _SESSION_SHM_IDS.update(shm_ids)


@pytest.fixture(scope="function")
def chat(agent: AgentHandle) -> ChatHelper:
    """
    Function 级 ChatHelper，封装 agent 的对话 API。

    用法:
        def test_greeting(chat):
            with chat.send("你好") as stream:
                assert "你好" in stream.text_content()
    """
    return ChatHelper(agent)


# ──────────────────────────────────────────────
#  Pytest Markers
# ──────────────────────────────────────────────

# L2 测试标记 — 需要 LLM API key
# 用法: @pytest.mark.llm
# 运行: pytest -m llm        (仅 L2)
#       pytest -m "not llm"   (仅 L1)
#       pytest                 (全跑，L2 无 API key 时自动 skip)


def pytest_configure(config):
    """注册 pytest 标记。"""
    config.addinivalue_line(
        "markers",
        "llm: L2 tests that require LLM inference (DEEPSEEK_API_KEY needed)",
    )


def pytest_collection_modifyitems(config, items):
    """
    L2 测试自动跳过：若 DEEPSEEK_API_KEY 未设置，
    标记 llm 的测试项自动 skip。
    """
    has_api_key = bool(os.environ.get("DEEPSEEK_API_KEY"))
    if has_api_key:
        return

    skip_llm = pytest.mark.skip(reason="DEEPSEEK_API_KEY not set — skipping L2 test")
    for item in items:
        if "llm" in item.keywords:
            item.add_marker(skip_llm)


# ──────────────────────────────────────────────
#  L2 测试批处理建议
# ──────────────────────────────────────────────
# L2 测试执行时间较长（每个用例 10–60s LLM 响应），建议按以下策略优化：
#
# 1. 同一对话流的测试合并为一个测试函数内的多步骤验证
#    （如完整接入流），减少 Agent 重复启动
#
# 2. L2 测试按 batch 分组（参见 pytest.mark.llm 注册时的 batch 参数），
#    批次间添加 cooling_off 间隔（如 10s）避免 API 限速
#
# 3. 错误恢复测试（§4.8）使用预构造的 JSON 文件绕过 LLM，
#    直接测试执行模块
