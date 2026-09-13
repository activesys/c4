"""
C4 Agent 功能测试公共基础设施 — conftest.py

提供:
  - SocketMcpClient: Unix-socket MCP JSON-RPC 客户端（连接常驻 MCP 服务）
  - McpStackHandle: 常驻 MCP 测试栈（shm_manager + 5 个数据服务，C4_SOCK_DIR 指向临时目录）
  - AgentHandle: Agent 进程 + HTTP API 封装
  - SSEEventStream: HTTP SSE 流客户端
  - ChatHelper: 对话辅助（send / send_with_file / confirm）
  - Fixtures: agent_binary, shm_manager_binary, registry_dir, mcp_stack, agent, chat, abbr_registry
  - Helpers: write_agent_json, write_config_json, corrupt_config_json, write_config_prev,
    write_pending_marker, clear_transaction_files, write_abbr_registry
  - pytest markers: llm (L2 tests)

测试栈契约（c4_architecture.md §3.1.1 独立服务模型）:
  - MCP 服务进程由测试栈自启（常驻、零实例、仅监听 socket——C4_SOCK_DIR 指向临时目录）；
  - Agent 是 MCP 客户端，经 socket 连接、从不拉起 MCP 进程；
  - config.json 事务文件：pending_change.json + config.json.prev.1~.3（无 .bak）。

设计依据: c4/test/agent/README.md §2.2-2.3
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
import tempfile
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

# 全部 MCP 服务（含 c4_shm_manager）——常驻测试栈的进程集合
ALL_MCP_SERVICES = [
    "c4_shm_manager",
    "c4_modbus_client",
    "c4_iec104_client",
    "c4_asfp2_client",
    "c4_asfp2_server",
    "c4_influxdb_client",
]

# 会话级 socket 目录（pytest_configure 设置，Agent 子进程经环境变量继承）
_SOCK_DIR: str = ""


# ──────────────────────────────────────────────
#  Unix-Socket MCP Client
# ──────────────────────────────────────────────


class SocketMcpClient:
    """通过 Unix-socket MCP JSON-RPC 与常驻 MCP 服务通信（一行一条 JSON）。"""

    def __init__(self, service_type: str, sock_dir: str = "", timeout: float = 10.0):
        sock_dir = sock_dir or _SOCK_DIR
        assert sock_dir, "C4_SOCK_DIR 未设置（pytest_configure 未运行？）"
        self.sock_path = os.path.join(sock_dir, f"{service_type}.sock")
        self.timeout = timeout
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect(self.sock_path)
        self._buf = b""
        self._next_id = 0
        self._initialize()

    def _send(self, msg: dict) -> None:
        self._sock.sendall((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))

    def _recv(self) -> dict:
        while True:
            idx = self._buf.find(b"\n")
            if idx >= 0:
                line = self._buf[:idx]
                self._buf = self._buf[idx + 1:]
                return json.loads(line.decode("utf-8"))
            chunk = self._sock.recv(65536)
            if not chunk:
                raise EOFError("MCP socket closed")
            self._buf += chunk

    def _initialize(self) -> None:
        self._next_id += 1
        self._send({
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "c4_test", "version": "1.0.0"},
            },
        })
        resp = self._recv()
        if "error" in resp:
            raise RuntimeError(f"MCP initialize failed: {resp['error']}")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """调用 MCP 工具，返回完整 JSON-RPC 响应。"""
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

    def call_tool_text(self, tool_name: str, arguments: dict) -> tuple[str, bool]:
        """调用工具，返回 (text, is_error)。"""
        resp = self.call_tool(tool_name, arguments)
        result = resp.get("result", {})
        parts = [
            c.get("text", "")
            for c in (result.get("content") or [])
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        return ("\n".join(parts), bool(result.get("isError")))

    def read_points(self, shm_ids: list[int]) -> dict:
        """read_points 封装（返回 {reads: [...], errors: [...]}）。"""
        text, is_err = self.call_tool_text("read_points", {"shm_ids": shm_ids})
        assert not is_err, f"read_points failed: {text[:200]}"
        return json.loads(text)

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


# ──────────────────────────────────────────────
#  常驻 MCP 测试栈
# ──────────────────────────────────────────────


class McpStackHandle:
    """常驻 MCP 服务测试栈：进程由测试自启，Agent 仅连接。"""

    def __init__(self, sock_dir: str, log_dir: Path):
        self.sock_dir = sock_dir
        self.log_dir = log_dir
        self.processes: dict[str, subprocess.Popen] = {}

    def start_service(self, service_type: str) -> None:
        """启动单个常驻 MCP 服务（stdin=/dev/null → resident 模式，仅监听 socket）。"""
        if service_type in self.processes and self.processes[service_type].poll() is None:
            return
        binary = _find_binary(f"C4_{service_type.removeprefix('c4_').upper()}_PATH", service_type)
        log = open(self.log_dir / f"{service_type}.log", "w")
        env = dict(os.environ)
        env["C4_SOCK_DIR"] = self.sock_dir
        self.processes[service_type] = subprocess.Popen(
            [binary],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
        )

    def stop_service(self, service_type: str) -> None:
        """停止单个服务（模拟服务下线——降级/重连测试用）。"""
        proc = self.processes.pop(service_type, None)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        sock = Path(self.sock_dir) / f"{service_type}.sock"
        if sock.exists():
            sock.unlink()

    def start_all(self) -> None:
        for svc in ALL_MCP_SERVICES:
            self.start_service(svc)

    def stop_all(self) -> None:
        for svc in list(self.processes):
            self.stop_service(svc)

    def wait_socket(self, service_type: str, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        sock = Path(self.sock_dir) / f"{service_type}.sock"
        while time.time() < deadline:
            if sock.exists():
                return True
            time.sleep(0.1)
        return False


def _cleanup_instance_shm(instance_id: str) -> None:
    """清理实例共享内存段（shm_unlink：/dev/shm/c4_<instance_id>）。"""
    path = f"/dev/shm/{instance_id}"
    try:
        os.unlink(path)
    except OSError:
        pass


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
        # SSE 流按 token 分片（每片一个 text 事件）——空串拼接还原原文，
        # 与 run_cases.chat() 一致；换行拼接会把 "hnals_wt1" 切成 "hn als _w t 1"
        return "".join(parts)


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


def agent_command(agent_binary: str, config_dir: Path) -> list[str]:
    """构造 Agent 启动命令（.js 入口经 node 启动）。"""
    cmd = [agent_binary, "--config-dir", str(config_dir)]
    if agent_binary.endswith(".js"):
        cmd = ["node", *cmd]
    return cmd


# ──────────────────────────────────────────────
#  Port Utilities
# ──────────────────────────────────────────────


def _find_free_port() -> int:
    """找到一个可用的 TCP 端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_is_listening(port: int, timeout: float = 1.0) -> bool:
    """TCP 端口监听探测——实例运行状态的断言依据（生命周期双层模型：
    MCP 进程常驻，进程存在 ≠ 实例运行；端口监听 / write_seq 推进才是）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def wait_port(port: int, want: bool, timeout: float = 15.0) -> None:
    """轮询端口监听状态直至满足或超时（poll-until-deadline，禁固定 sleep 单次断言）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_is_listening(port) == want:
            return
        time.sleep(0.3)
    raise AssertionError(
        f"端口 {port} {'监听' if want else '释放'}超时（{timeout}s）"
    )


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
        "instance_id": "c4_test",
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
        # 场站绑定（agent.md §3.2.1.3a：site 存于 agent.json 权威配置；
        # site 缺失流程由 write_abbr_registry("site_missing") 单独覆盖）
        "site": {"name": "华能阿拉善", "abbr": "hnals"},
        "mcp_registry": {
            "path": str(registry_dir),
        },
        "shm_manager": {
            "binary": shm_manager_binary,
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


def write_config_prev(config_dir: Path, content: dict) -> None:
    """
    写入 config.json.prev.1（回滚源，滚动保留 .prev.1~.3）。
    用于 L0 恢复/事务回滚测试（c4_architecture.md §3.1.2）。
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    prev_path = config_dir / "config.json.prev.1"
    prev_path.write_text(json.dumps(content, indent=2, ensure_ascii=False))


def write_pending_marker(
    config_dir: Path,
    services: Optional[list[str]] = None,
    description: str = "测试构造：崩溃于变更事务中",
) -> Path:
    """
    写入 pending_change.json 事务标记（模拟崩溃于变更事务中）。
    返回标记路径。
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    marker_path = config_dir / "pending_change.json"
    marker_path.write_text(json.dumps({
        "description": description,
        "services": services or [],
        "rollback_source": str(config_dir / "config.json.prev.1"),
        "created_at": "2026-01-01T00:00:00Z",
    }, indent=2, ensure_ascii=False))
    return marker_path


def clear_transaction_files(config_dir: Path) -> None:
    """清除事务文件组：pending_change.json + config.json.prev.1~.3（测试隔离用）。"""
    config_dir.mkdir(parents=True, exist_ok=True)
    names = ["pending_change.json"] + [f"config.json.prev.{i}" for i in (1, 2, 3)]
    for name in names:
        p = config_dir / name
        if p.exists():
            p.unlink()


# abbr 记忆库内容（agent.md §3.2.1.3a）
_ABBR_SITE = {"name": "华能阿拉善", "abbr": "hnals"}
_ABBR_ENTRIES = [
    {
        "id": "hnals_wt1",
        "name": "1#风机",
        "abbr": "wt1",
        "service_type": "c4_modbus_client",
        "role": "writer",
        "description": "采集 1#风机的数据",
    }
]


def write_abbr_registry(config_dir: Path, mode: str = "normal") -> Path:
    """
    在 config_dir 下写 abbr_registry.json（entries）+ 管理 agent.json 的 site（agent.md §3.2.1.3a）。

    site 存于 agent.json（权威配置），entries 存于 abbr_registry.json。

    mode ∈ {normal, entries_missing, site_missing, corrupted}:
      - normal:         agent.json 有 site + entries 正常
      - entries_missing: agent.json 有 site + 空 entries
      - site_missing:   agent.json 无 site + entries 正常
      - corrupted:      agent.json 有 site + 截断的非法 JSON

    返回 abbr_registry.json 路径。
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    registry_path = config_dir / "abbr_registry.json"

    # 管理 agent.json 的 site 字段
    agent_path = config_dir / "agent.json"
    agent_cfg: dict = {}
    if agent_path.exists():
        agent_cfg = json.loads(agent_path.read_text(encoding="utf-8"))
    if mode == "site_missing":
        agent_cfg.pop("site", None)
    else:
        agent_cfg["site"] = _ABBR_SITE
    agent_path.write_text(
        json.dumps(agent_cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    if mode == "normal":
        content: dict = {"entries": _ABBR_ENTRIES}
    elif mode == "entries_missing":
        content = {"entries": []}
    elif mode == "site_missing":
        content = {"entries": _ABBR_ENTRIES}
    elif mode == "corrupted":
        raw = json.dumps({"entries": _ABBR_ENTRIES}, indent=2, ensure_ascii=False)
        # 截断为非法 JSON 文本
        registry_path.write_text(raw[: len(raw) // 2])
        return registry_path
    else:
        raise ValueError(f"unknown abbr_registry mode: {mode!r}")

    registry_path.write_text(json.dumps(content, indent=2, ensure_ascii=False))
    return registry_path


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

        self.process = subprocess.Popen(
            agent_command(agent_binary, self.config_dir),
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

    def reset_conversation(self) -> None:
        """清空客户端历史——开启全新会话（与 run_cases 每用例新建 Conv 等价）。

        多阶段用例在长历史下继续追问时，LLM 会因上下文污染陷入确认死循环
        （实测：两轮完整接入历史 + 修改请求 → 确认后反复询问，永不产出
        output_plan_steps）；独立会话是 E2E runner 验证过的交互模型。
        """
        self._history = []

    def confirm(self, interrupt_id: str) -> SSEEventStream:
        """
        发送确认消息以通过 interrupt 检查点。
        实现方式：POST /api/chat 并附带 interrupt_id 上下文。
        """
        return self._agent.chat(f"[confirm interrupt_id={interrupt_id}]")


# ──────────────────────────────────────────────
#  Session-level cleanup
# ──────────────────────────────────────────────

_SESSION_SHM_IDS: set[int] = set()


def _cleanup_shm_ids(shm_ids: set[int]) -> None:
    """清理共享内存段：ipcrm -M <shmid>（兼容 SysV 残留）。"""
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
#  Pytest hooks & Fixtures
# ──────────────────────────────────────────────


def pytest_configure(config):
    """注册 pytest 标记 + 会话级 socket 目录（独立服务模型：Agent 经 env 继承）。"""
    config.addinivalue_line(
        "markers",
        "llm: L2 tests that require LLM inference (DEEPSEEK_API_KEY needed)",
    )
    global _SOCK_DIR
    if not _SOCK_DIR:
        _SOCK_DIR = tempfile.mkdtemp(prefix="c4_agent_test_socks_")
    os.environ["C4_SOCK_DIR"] = _SOCK_DIR


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


@pytest.fixture(scope="session")
def mcp_stack(tmp_path_factory) -> Generator[McpStackHandle, None, None]:
    """
    Session 级常驻 MCP 测试栈。

    启动全部 6 个 MCP 服务（resident 模式：仅监听 <sock-dir>/<service>.sock、
    零数据路径实例），Agent 经 C4_SOCK_DIR 指向同一目录连接。
    teardown: 逐个 SIGTERM → SIGKILL → 清理 socket 与 /dev/shm 残段。
    """
    log_dir = Path(tempfile.mkdtemp(prefix="c4_agent_test_mcp_logs_"))
    stack = McpStackHandle(_SOCK_DIR, log_dir)
    stack.start_all()
    for svc in ALL_MCP_SERVICES:
        assert stack.wait_socket(svc, timeout=10), f"{svc} socket 未就绪"

    yield stack

    stack.stop_all()
    for leftover in Path(_SOCK_DIR).glob("*.sock"):
        leftover.unlink(missing_ok=True)
    for seg in Path("/dev/shm").glob("c4_test*"):
        seg.unlink(missing_ok=True)
    shutil.rmtree(log_dir, ignore_errors=True)


def _find_binary_for_service(service_type: str) -> str | None:
    """Find the actual binary path for a given MCP service type."""
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
    mcp_stack: McpStackHandle,
    tmp_path: Path,
) -> Generator[AgentHandle, None, None]:
    """
    Function 级隔离的 Agent 实例。

    生命周期:
      1. 制备 tmp_path 作为配置目录（agent.json + config.json + mcp-registry/）
      2. MCP 常驻栈已由 mcp_stack fixture 就绪（Agent 仅经 socket 连接、从不拉起）
      3. 启动 c4_agent --config-dir <tmp_path>（C4_SOCK_DIR 经环境继承）
      4. 轮询 GET /api/services 直到返回 200（Agent 就绪）
      5. yield AgentHandle(base_url, process, config_dir)
      6. teardown: SIGTERM → 等待 10s → SIGKILL → 清理实例 shm 段 → session atexit 兜底
    """
    config_dir = tmp_path / "c4_config"
    port = _find_free_port()

    # 1. 制备 agent.json
    write_agent_json(config_dir, registry_dir, shm_manager_binary, port)

    # 2. 默认不创建 config.json（模拟首次启动），各测试自行调用 write_config_json
    clear_transaction_files(config_dir)
    config_path = config_dir / "config.json"
    if config_path.exists():
        config_path.unlink()

    # 3. 启动 c4_agent（C4_SOCK_DIR 已在 pytest_configure 写入 os.environ，子进程继承）
    process = subprocess.Popen(
        agent_command(agent_binary, config_dir),
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
    # 注意：使用 handle.process 而非局部 process — 测试内 kill()+restart()
    # 会替换 handle.process，teardown 必须清理最新进程，避免泄漏重启后的 Agent。
    if handle.process.poll() is None:
        handle.process.terminate()
        try:
            handle.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # Step 2: SIGKILL
            handle.process.kill()
            try:
                handle.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    # Step 3: 停掉本测试拉起的数据路径实例（常驻 MCP 进程不重启，仅停实例——
    # 函数级隔离，下一测试从零实例状态开始）+ 清理实例 shm 段
    for svc in ALL_MCP_SERVICES:
        if svc == "c4_shm_manager":
            continue
        try:
            client = SocketMcpClient(svc, _SOCK_DIR or "", timeout=5.0)
            client.call_tool_text("stop", {})
            client.close()
        except Exception:
            pass
    _cleanup_instance_shm("c4_test")
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


@pytest.fixture(scope="function")
def abbr_registry(agent: AgentHandle) -> Callable[..., Path]:
    """
    Function 级 abbr 记忆库制备器，绑定 agent.config_dir。

    用法:
        abbr_registry()               # normal（agent.json 有 site + entries）
        abbr_registry("corrupted")    # 截断的非法 JSON
    """
    def _write(mode: str = "normal") -> Path:
        return write_abbr_registry(agent.config_dir, mode)

    return _write
