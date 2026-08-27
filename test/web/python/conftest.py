"""
C4 Web 契约测试公共基础设施 — conftest.py

pytest 的 conftest 按 test 文件所在目录向上发现、不会跨测试根复用，因此
`c4/test/web/python/` 自带本 conftest，**移植**（而非 import）
`c4/test/agent/python/conftest.py` 的启动 fixture 等价逻辑：

  写 agent.json → 启动 c4_shm_manager（由 Agent 代为托管）→ 启动 agent →
  轮询 GET /api/services 就绪 → teardown。

设计依据: c4/test/web/README.md §2.4、§6
参考实现: c4/test/agent/python/conftest.py

与 agent 测试方案的关键差异:
  - 契约测试**不使用** ChatHelper.confirm(interrupt_id)（interrupt 模型已过时，
    与 web.md v0.1.2「后端从不产出 interrupt」结论冲突）；
    确认/取消一律经 POST /api/chat 发送关键词。
  - AgentHandle.chat() 支持 conversation_id 参数，用于 §6.5 回显断言。
  - SSEEventStream 增加 get_header() 以读取 X-Conversation-Id 响应头。
"""

import atexit
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Generator, Optional
from urllib.request import Request, urlopen

import pytest  # type: ignore

# ──────────────────────────────────────────────
#  项目根路径
# ──────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_DIR = _PROJECT_ROOT / "config"


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
            # 非 event/data 前缀的行（如 :ok 注释）忽略

        # 流结束时如有未完成的 data，也计入
        if current_data:
            event_type = current_event or "message"
            self.events.append(SSEEvent(event_type, "\n".join(current_data)))

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

    def get_header(self, name: str) -> Optional[str]:
        """读取 HTTP 响应头（如 X-Conversation-Id）。"""
        if self._response is None:
            return None
        try:
            return self._response.headers.get(name)
        except Exception:
            return None


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
    """查找 c4_agent 可执行文件（TypeScript → node 入口 dist/index.js）。"""
    path = os.environ.get("C4_AGENT_PATH")
    if path and os.path.isfile(path):
        return path

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


def _find_binary_for_service(service_type: str) -> Optional[str]:
    """
    查找给定 MCP 服务类型的实际二进制路径。

    契约测试不会启动 MCP 服务（不做 confirm/execute），因此这里仅做静态查找、
    **不触发 go build**，避免拖慢测试或污染源码树。找不到时返回 None，
    registry 条目保留原始 binary_path（仅影响服务启动，不影响契约断言）。
    """
    candidates = [
        f"/usr/local/bin/{service_type}",
        str(_PROJECT_ROOT / "mcp" / service_type / service_type),
        str(_PROJECT_ROOT / "mcp" / service_type / "build" / service_type),
        str(_PROJECT_ROOT / "build" / "mcp" / service_type / service_type),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return os.path.abspath(p)
    return None


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

    def chat(
        self,
        message: str,
        history: Optional[list] = None,
        conversation_id: Optional[str] = None,
    ) -> SSEEventStream:
        """
        POST /api/chat → SSE 流。

        conversation_id 会写入请求体 conversationId 字段，用于 §6.5 回显断言。
        用法:
            with agent.chat("你好") as stream:
                text = stream.text_content()
        """
        body: dict = {"message": message}
        if history:
            body["history"] = history
        if conversation_id:
            body["conversationId"] = conversation_id
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
    返回 c4_agent 可执行文件路径（node 入口 dist/index.js）。
    从 C4_AGENT_PATH 或自动查找。
    """
    return _find_agent_binary()


@pytest.fixture(scope="session")
def shm_manager_binary() -> str:
    """
    返回 c4_shm_manager 二进制路径。
    从 C4_SHM_MANAGER_PATH 或自动查找。
    """
    return _find_binary("C4_SHM_MANAGER_PATH", "c4_shm_manager")


@pytest.fixture(scope="session")
def registry_dir(tmp_path_factory) -> Path:
    """
    Session 级 fixture — 制备 mcp-registry/ 目录。
    复制 config/mcp-registry/*.json 到临时路径，并修正 binary_path 为实际产物路径。
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
      1. 制备 tmp_path 作为配置目录（agent.json + mcp-registry/）
      2. 启动 c4_agent --config-dir <tmp_path>（Agent 托管 c4_shm_manager）
      3. 轮询 GET /api/services 直到返回 200（Agent 就绪）
      4. yield AgentHandle(base_url, process, config_dir)
      5. teardown: SIGTERM → 等待 10s → SIGKILL → ipcrm 清理 shm → atexit 兜底
    """
    config_dir = tmp_path / "c4_config"
    port = _find_free_port()

    # 1. 制备 agent.json
    write_agent_json(config_dir, registry_dir, shm_manager_binary, port)

    # 2. 默认不创建 config.json（模拟首次启动）
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

    # Step 3: 清理 shm（ipcrm）
    _cleanup_shm_ids(shm_ids)

    # Step 4: 注册到 session atexit 兜底
    _SESSION_SHM_IDS.update(shm_ids)


# ──────────────────────────────────────────────
#  Pytest Markers
# ──────────────────────────────────────────────

# llm 测试标记 — 需要 LLM API key（DEEPSEEK_API_KEY）
# 用法: @pytest.mark.llm
# 无 DEEPSEEK_API_KEY 时自动 skip。


def pytest_configure(config):
    """注册 pytest 标记。"""
    config.addinivalue_line(
        "markers",
        "llm: 需要 LLM 推理的契约用例（DEEPSEEK_API_KEY 必需）",
    )


def pytest_collection_modifyitems(config, items):
    """
    llm 测试自动跳过：若 DEEPSEEK_API_KEY 未设置，标记 llm 的测试项自动 skip。
    """
    has_api_key = bool(os.environ.get("DEEPSEEK_API_KEY"))
    if has_api_key:
        return

    skip_llm = pytest.mark.skip(reason="DEEPSEEK_API_KEY not set — skipping LLM test")
    for item in items:
        if "llm" in item.keywords:
            item.add_marker(skip_llm)
