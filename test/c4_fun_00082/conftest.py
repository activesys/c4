"""C4_FUN_00082 测试栈 — read_points 直连栈 + Agent REST 栈。

硬约束（README §1）：不使用 systemd c4-agent、不碰生产配置/shm；
conftest 自建完整测试栈（tmp config-dir + 独立 instance + 非冲突端口），
测试以普通用户运行；teardown 杀整组进程 + shm_unlink。
"""

import importlib.util
import json
import mmap
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

_TEST_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _TEST_DIR.parents[1]

# 跨目录复用 00053 的 shm_helpers（本机序读写）
sys.path.insert(0, str(_PROJECT_ROOT / "test" / "c4_fun_00053"))
from shm_helpers import shm_unlink  # noqa: E402

INSTANCE_DIRECT = "c4_ft82s"  # §2 read_points 直连栈
INSTANCE_AGENT = "c4_ft82"    # §3 Agent REST 栈


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _shm_manager_binary() -> str:
    path = os.environ.get("C4_SHM_MANAGER_PATH")
    if path and os.path.isfile(path):
        return path
    candidate = _PROJECT_ROOT / "mcp" / "c4_shm_manager" / "c4_shm_manager"
    if candidate.is_file():
        return str(candidate)
    subprocess.run(["go", "build", "-o", "c4_shm_manager", "."],
                   cwd=str(candidate.parent), check=True, capture_output=True)
    return str(candidate)


def _agent_entry() -> list[str]:
    path = os.environ.get("C4_AGENT_PATH")
    if not path or not os.path.isfile(path):
        path = str(_PROJECT_ROOT / "agent" / "dist" / "index.js")
    if path.endswith(".js"):
        node = os.environ.get("NODE_BIN", "node")
        return [node, path]
    return [path]


# ── fixture config（README §4，schema 依据 c4_architecture.md §3.2.1）──

FIXTURE_CONFIG = {
    "c4_shm_manager": {"writer": ["c4_asfp2_server"], "reader": ["c4_asfp2_client"]},
    "c4_asfp2_server": [
        {"id": "wt1", "port": 19001,
         "points": [{"id": "windspeed", "addr": 3000},
                    {"id": "power", "addr": 3001},
                    {"id": "oiltemp", "addr": 3002}]},
    ],
    "c4_asfp2_client": [
        {"id": "center", "ip": "127.0.0.1", "port": 19900,
         "points": [{"key": "wt1.windspeed", "addr": 5000}]},
    ],
}


def write_point(shm_path: str, shm_id: int, data_type: int, value: int, ts: int) -> None:
    """seqlock 直写一个点：seq(奇) → 字段 → seq(偶)。本机序（README §4 勘误注记）。"""
    fd = os.open(shm_path, os.O_RDWR)
    m = mmap.mmap(fd, 0, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
    off = shm_id * 32
    seq = struct.unpack_from("=Q", m, off + 8)[0]
    struct.pack_into("=Q", m, off + 8, seq + 1)
    m[off + 4] = 1
    m[off + 7] = data_type
    struct.pack_into("=Q", m, off + 16, ts)
    struct.pack_into("=Q", m, off + 24, value & (2**64 - 1))
    struct.pack_into("=Q", m, off + 8, seq + 2)
    m.flush()
    m.close()
    os.close(fd)


class McpClient:
    """stdio JSON-RPC 客户端（握手流程同 c4_fun_00053/conftest.py）。"""

    def __init__(self, binary_path: str):
        self.process = subprocess.Popen(
            [binary_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        self._stdin = self.process.stdin
        self._stdout = self.process.stdout
        self._next_id = 0
        self._send({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05",
                               "capabilities": {},
                               "clientInfo": {"name": "c4_ft82", "version": "1.0.0"}}})
        resp = self._recv()
        if "error" in resp:
            raise RuntimeError(f"initialize failed: {resp['error']}")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg: dict) -> None:
        self._stdin.write(json.dumps(msg) + "\n")
        self._stdin.flush()

    def _recv(self) -> dict:
        line = self._stdout.readline()
        if not line:
            raise EOFError("SUT exited")
        return json.loads(line)

    def call_tool(self, name: str, arguments: dict) -> dict:
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}})
        while True:
            msg = self._recv()
            if msg.get("id") == rid:
                return msg

    def close(self) -> None:
        try:
            self.process.terminate()
            self.process.wait(timeout=5)
        except Exception:
            self.process.kill()


def http_json(base: str, method: str, path: str, body: dict | None = None, timeout: float = 10):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def poll_until(fn, deadline_s: float, interval_s: float = 0.25):
    """轮询+截止时间（README §5：禁固定 sleep 后单次断言）。"""
    end = time.time() + deadline_s
    last = None
    while time.time() < end:
        last = fn()
        if last:
            return last
        time.sleep(interval_s)
    return last


# ── Agent 栈 fixture ──────────────────────────────────────

class AgentStack:
    def __init__(self, base_url: str, shm_path: str, config_json: Path, proc: subprocess.Popen):
        self.base_url = base_url
        self.shm_path = shm_path
        self.config_json = config_json
        self.proc = proc

    def shm_ids(self) -> dict:
        """key → shm_id（create_shm 回填后的 tmp config.json）。"""
        config = json.loads(self.config_json.read_text())
        out = {}
        for inst in config["c4_asfp2_server"]:
            for pt in inst["points"]:
                out[f"{inst['id']}.{pt['id']}"] = int(pt["shm_id"])
        return out

    def restart(self) -> None:
        """测试进程内重启（非 systemctl，README 00084 TC18）。"""
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self._spawn()

    def _spawn(self) -> None:
        env = os.environ.copy()
        env.setdefault("DEEPSEEK_API_KEY", "test-dummy-key")
        self.proc = subprocess.Popen(
            _agent_entry() + ["--config-dir", str(self.tmp_dir)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
        self._wait_ready()

    def _wait_ready(self, deadline_s: float = 60.0) -> None:
        end = time.time() + deadline_s
        while time.time() < end:
            try:
                with urllib.request.urlopen(self.base_url + "/api/state", timeout=2) as r:
                    if r.status == 200:
                        return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError("Agent 未在截止时间内就绪")


@pytest.fixture(scope="session")
def agent_stack():
    """§3 Agent REST 栈：instance c4_ft82，agent 自启的 c4_shm_manager。"""
    tmp = Path(tempfile.mkdtemp(prefix="c4_ft82_"))
    (tmp / "registry").mkdir()
    port = _free_port()
    shm_bin = _shm_manager_binary()
    agent_json = {
        "instance_id": INSTANCE_AGENT,
        "model": {"provider": "deepseek", "name": "deepseek-chat", "temperature": 0,
                  "max_tokens": 4096, "api_key_env": "DEEPSEEK_API_KEY"},
        "server": {"host": "127.0.0.1", "port": port, "cors_origin": "*"},
        "mcp_registry": {"path": str(tmp / "registry")},
        "shm_manager": {"binary": shm_bin, "config_path": str(tmp / "config.json")},
        "state": {"backend": "memory", "path": str(tmp / "state")},
        "logging": {"level": "info", "dir": str(tmp / "logs")},
    }
    (tmp / "agent.json").write_text(json.dumps(agent_json, indent=2))
    (tmp / "config.json").write_text(json.dumps(FIXTURE_CONFIG, indent=2))

    env = os.environ.copy()
    env.setdefault("DEEPSEEK_API_KEY", "test-dummy-key")
    proc = subprocess.Popen(
        _agent_entry() + ["--config-dir", str(tmp)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )
    stack = AgentStack(base_url=f"http://127.0.0.1:{port}",
                       shm_path=f"/dev/shm/{INSTANCE_AGENT}",
                       config_json=tmp / "config.json", proc=proc)
    stack.tmp_dir = tmp
    try:
        stack._wait_ready()
        # 等启动恢复（create_shm 回填 shm_id）完成
        poll_until(
            lambda: json.loads(
                urllib.request.urlopen(stack.base_url + "/api/state", timeout=2).read()
            ) is not None,
            deadline_s=15,
        )
        time.sleep(1)
        yield stack
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shm_unlink(f"/{INSTANCE_AGENT}")


@pytest.fixture(scope="session")
def direct_stack(tmp_path_factory):
    """§2 read_points 直连栈：独立 instance c4_ft82s。"""
    tmp = Path(tempfile.mkdtemp(prefix="c4_ft82s_"))
    (tmp / "config.json").write_text(json.dumps(FIXTURE_CONFIG, indent=2))
    binary = _shm_manager_binary()
    client = McpClient(binary)
    resp = client.call_tool("create_shm", {
        "instance_id": INSTANCE_DIRECT, "config_path": str(tmp / "config.json"),
    })
    text = resp["result"]["content"][0]["text"]
    assert not resp["result"].get("isError", False), f"create_shm failed: {text}"

    # 读取回填后的 shm_id 映射
    backfilled = json.loads((tmp / "config.json").read_text())
    shm_ids = {}
    for inst in backfilled["c4_asfp2_server"]:
        for pt in inst["points"]:
            shm_ids[f"{inst['id']}.{pt['id']}"] = int(pt["shm_id"])

    stack = type("DirectStack", (), {})()
    stack.client = client
    stack.shm_path = f"/dev/shm/{INSTANCE_DIRECT}"
    stack.shm_ids = shm_ids
    stack.tmp = tmp
    try:
        yield stack
    finally:
        client.close()
        shm_unlink(f"/{INSTANCE_DIRECT}")
