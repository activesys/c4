"""C4_FUN_00084 测试栈 — 复用 00082 基建，instance c4_ft84。"""

import importlib.util
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

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
    def __init__(self, base_url: str, shm_path: str, proc: subprocess.Popen, tmp: Path):
        self.base_url = base_url
        self.shm_path = shm_path
        self.proc = proc
        self.tmp = tmp

    def shm_ids(self) -> dict:
        """key → shm_id（create_shm 回填后的 tmp config.json）。"""
        config = json.loads((self.tmp / "config.json").read_text())
        out = {}
        for inst in config["c4_asfp2_server"]:
            for pt in inst["points"]:
                out[f"{inst['id']}.{pt['id']}"] = int(pt["shm_id"])
        return out

    def restart(self) -> None:
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
            _agent_entry() + ["--config-dir", str(self.tmp)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
        self._wait_ready()

    def _wait_ready(self, deadline_s: float = 60.0) -> None:
        import urllib.request
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
    tmp = Path(tempfile.mkdtemp(prefix="c4_ft84_"))
    (tmp / "registry").mkdir()
    port = _free_port()
    agent_json = {
        "instance_id": INSTANCE,
        "model": {"provider": "deepseek", "name": "deepseek-chat", "temperature": 0,
                  "max_tokens": 4096, "api_key_env": "DEEPSEEK_API_KEY"},
        "server": {"host": "127.0.0.1", "port": port, "cors_origin": "*"},
        "mcp_registry": {"path": str(tmp / "registry")},
        "shm_manager": {"binary": _find_shm_binary(), "config_path": str(tmp / "config.json")},
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
                       shm_path=f"/dev/shm/{INSTANCE}", proc=proc, tmp=tmp)
    try:
        stack._wait_ready()
        time.sleep(1)
        yield stack
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shm_unlink(f"/{INSTANCE}")
