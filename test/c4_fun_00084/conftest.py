"""C4_FUN_00084 测试栈 — 复用 00082 基建，instance c4_ft84。

独立服务模型（c4_architecture.md §3.1.1）：测试栈经 C4_SOCK_DIR 自启常驻
c4_shm_manager（仅监听 socket、零实例），Agent 是 MCP 客户端、经 socket 连接、
从不拉起 MCP 进程。降级场景（TC15/TC17）kill 的是本栈自启的常驻 shm_manager
（按 C4_SOCK_DIR 定位，非 Agent 子进程）。
"""

import importlib.util
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import TextIO

import pytest

_TEST_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _TEST_DIR.parents[1]
INSTANCE = "c4_ft84"

_spec = importlib.util.spec_from_file_location(
    "ft82_conftest", _TEST_DIR.parent / "c4_fun_00082" / "conftest.py")
assert _spec is not None and _spec.loader is not None
_ft82 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ft82)

write_point = _ft82.write_point
poll_until = _ft82.poll_until
FIXTURE_CONFIG = _ft82.FIXTURE_CONFIG
shm_unlink = _ft82.shm_unlink


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _find_shm_binary() -> str:
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


class AgentStack:
    sock_dir: str
    proc: subprocess.Popen
    shm_proc: subprocess.Popen
    shm_log: TextIO

    def __init__(self, base_url: str, shm_path: str, tmp: Path):
        self.base_url = base_url
        self.shm_path = shm_path
        self.tmp = tmp
        self.proc = None  # type: ignore[assignment]  # _spawn() 填充
        self.shm_proc = None  # type: ignore[assignment]
        self.shm_log = None  # type: ignore[assignment]

    def shm_ids(self) -> dict:
        """key → shm_id（create_shm 回填后的 tmp config.json）。"""
        config = json.loads((self.tmp / "config.json").read_text())
        out = {}
        for inst in config["c4_asfp2_server"]:
            for pt in inst["points"]:
                out[f"{inst['id']}.{pt['id']}"] = int(pt["shm_id"])
        return out

    def restart(self) -> None:
        """重启整栈（Agent + 常驻 c4_shm_manager）。

        shm_manager 被杀后无自愈（恢复途径＝整机重启/进程重启，README 00084 TC17），
        故 restart 同时拉起两者；shm 段持久（不 unlink），write_seq 不归零（TC18）。
        """
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        if self.shm_proc is not None:
            self.shm_proc.terminate()
            try:
                self.shm_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.shm_proc.kill()
        self._spawn()

    def _spawn(self) -> None:
        # 常驻 c4_shm_manager 先行（Agent 连接前置；unlink-before-bind 自清残留 socket）
        env = os.environ.copy()
        env["C4_SOCK_DIR"] = str(self.tmp / "socks")
        env.setdefault("ZHIPU_API_KEY", "test-dummy-key")
        self.shm_log = open(self.tmp / "shm_manager.log", "w")
        self.shm_proc = subprocess.Popen(
            [_find_shm_binary()], stdin=subprocess.DEVNULL,
            stdout=self.shm_log, stderr=subprocess.STDOUT, env=env,
        )
        poll_until(lambda: (self.tmp / "socks" / "c4_shm_manager.sock").exists(),
                   deadline_s=10, interval_s=0.1)
        self.proc = subprocess.Popen(
            _agent_entry() + ["--config-dir", str(self.tmp)],
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

    def _teardown(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        if self.shm_proc is not None:
            self.shm_proc.terminate()
            try:
                self.shm_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.shm_proc.kill()
        if self.shm_log is not None:
            self.shm_log.close()


@pytest.fixture(scope="session")
def agent_stack():
    """Agent REST 栈：instance c4_ft84（独立服务模型：测试栈自启常驻
    c4_shm_manager，Agent 经 socket 连接、从不拉起 MCP 进程）。"""
    tmp = Path(tempfile.mkdtemp(prefix="c4_ft84_"))
    (tmp / "registry").mkdir()
    (tmp / "socks").mkdir()
    port = _free_port()
    agent_json = {
        "instance_id": INSTANCE,
        "model": {"provider": "zhipu", "name": "glm-5.3-flash",
                  "base_url": "https://open.bigmodel.cn/api/paas/v4", "temperature": 0,
                  "max_tokens": 4096, "api_key_env": "ZHIPU_API_KEY"},
        "server": {"host": "127.0.0.1", "port": port, "cors_origin": "*"},
        "mcp_registry": {"path": str(tmp / "registry")},
        "shm_manager": {"binary": _find_shm_binary(), "config_path": str(tmp / "config.json")},
        "state": {"backend": "memory", "path": str(tmp / "state")},
        "logging": {"level": "info", "dir": str(tmp / "logs")},
    }
    (tmp / "agent.json").write_text(json.dumps(agent_json, indent=2))
    (tmp / "config.json").write_text(json.dumps(FIXTURE_CONFIG, indent=2))

    stack = AgentStack(base_url=f"http://127.0.0.1:{port}",
                       shm_path=f"/dev/shm/{INSTANCE}", tmp=tmp)
    stack.sock_dir = str(tmp / "socks")
    try:
        stack._spawn()
        # 等启动恢复（瀑布 L2 create_shm 回填 shm_id）完成
        def shm_ids_backfilled():
            try:
                return all(v > 0 for v in stack.shm_ids().values())
            except Exception:
                return False
        assert poll_until(shm_ids_backfilled, deadline_s=30, interval_s=0.5), \
            "瀑布 create_shm 未回填 shm_id"
        time.sleep(1)
        yield stack
    finally:
        stack._teardown()
        shm_unlink(f"/{INSTANCE}")
