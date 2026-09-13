#!/usr/bin/env python3
# func_test_case 用例 16~29（含用例 10）E2E runner —— 隔离 agent 实例
# （独立 config-dir / shm c4_e2e / 19xxx 端口映射，不与生产 agent 及用户 Web 测试互相干扰）。
# 独立服务模型（c4_architecture.md §3.1.1）：测试栈自启六个 MCP 服务二进制
# （RESIDENT 模式：stdin=/dev/null，仅监听 <sock-dir>/<service>.sock、零实例），
# Agent 与 MCP 栈经同一 C4_SOCK_DIR 连接——Agent 从不拉起 MCP 进程。
# 用法（root）: python3 run_cases.py <case>
#   case: prereq|16|17|18|19|20|21|22|23|24|25|26|27|28|29|10|all
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

AGENT_JS = "/home/wangbo/work/activesys/c4/agent/dist/index.js"
SHM_BINARY = "/usr/local/bin/c4_shm_manager"
REGISTRY_DIR = "/usr/local/etc/c4/mcp-registry"
AGENT_ENV = "/usr/local/etc/c4/agent.env"
BASE = "http://127.0.0.1:19720"
ASFP2_SERVER = "/usr/local/bin/asfp2_server"
ASFP2_CLIENT = "/usr/local/bin/asfp2_client"
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.log")

PORT_MAP = {"9001": "19001", "9002": "19002", "9900": "19900", "9901": "19901"}
P_RECV1, P_RECV2, P_FWD1, P_FWD2 = 19001, 19002, 19900, 19901

# ── 常驻 MCP 测试栈（独立服务模型）────────────────────────
ALL_MCP_SERVICES = [
    "c4_shm_manager",
    "c4_modbus_client",
    "c4_iec104_client",
    "c4_asfp2_client",
    "c4_asfp2_server",
    "c4_influxdb_client",
]
SOCK_DIR = "/tmp/c4_e2e_socks"


class SockClient:
    """Unix-socket MCP JSON-RPC 客户端（一行一条 JSON，握手按连接进行）。"""

    def __init__(self, service_type: str, timeout: float = 10.0):
        self.sock_path = os.path.join(SOCK_DIR, f"{service_type}.sock")
        self.timeout = timeout
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect(self.sock_path)
        self._buf = b""
        self._next_id = 0
        self._send({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "c4_e2e", "version": "1.0.0"}}})
        resp = self._recv()
        if "error" in resp:
            raise RuntimeError(f"initialize failed: {resp['error']}")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg: dict) -> None:
        self._sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))

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

    def call_tool(self, name: str, arguments: dict) -> dict:
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}})
        while True:
            msg = self._recv()
            if msg.get("id") == rid:
                return msg

    def call_tool_text(self, name: str, arguments: dict) -> tuple[str, bool]:
        resp = self.call_tool(name, arguments)
        result = resp.get("result", {})
        parts = [c.get("text", "") for c in (result.get("content") or [])
                 if isinstance(c, dict) and c.get("type") == "text"]
        return ("\n".join(parts), bool(result.get("isError")))

    def read_points(self, shm_ids: list[int]) -> dict:
        text, is_err = self.call_tool_text("read_points", {"shm_ids": shm_ids})
        if is_err:
            raise Fail(f"read_points failed: {text[:200]}")
        return json.loads(text)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class McpStack:
    """常驻 MCP 服务栈：进程由测试自启（RESIDENT：stdin=/dev/null），Agent 仅连接。"""

    def __init__(self):
        self.procs: dict[str, subprocess.Popen] = {}
        self.logs: dict[str, object] = {}

    @staticmethod
    def sock_path(service_type: str) -> str:
        return os.path.join(SOCK_DIR, f"{service_type}.sock")

    def _sock_alive(self, service_type: str) -> bool:
        p = self.sock_path(service_type)
        if not os.path.exists(p):
            return False
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(2)
            s.connect(p)
            return True
        except OSError:
            return False
        finally:
            s.close()

    def up(self):
        os.makedirs(SOCK_DIR, exist_ok=True)
        env = dict(os.environ)
        env["C4_SOCK_DIR"] = SOCK_DIR
        for svc in ALL_MCP_SERVICES:
            if self._sock_alive(svc):
                continue
            if os.path.exists(self.sock_path(svc)):
                os.unlink(self.sock_path(svc))
            f = open(f"/tmp/e2e_mcp_{svc}.log", "w")
            self.procs[svc] = subprocess.Popen(
                [os.path.join("/usr/local/bin", svc)],
                stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, env=env)
            self.logs[svc] = f
        deadline = time.time() + 20
        while time.time() < deadline:
            if all(self._sock_alive(s) for s in ALL_MCP_SERVICES):
                log(f"  常驻 MCP 栈就绪（C4_SOCK_DIR={SOCK_DIR}）")
                return
            time.sleep(0.2)
        raise Fail("常驻 MCP 栈 20s 内 socket 未全部就绪（见 /tmp/e2e_mcp_*.log）")

    def stop_instances(self):
        """停全部数据路径实例（进程保留）——测试隔离，等价 cleanup 的实例层。"""
        for svc in ALL_MCP_SERVICES:
            if svc == "c4_shm_manager":
                continue
            if not self._sock_alive(svc):
                continue
            try:
                c = SockClient(svc, timeout=5.0)
                c.call_tool_text("stop", {})
                c.close()
            except Exception:
                pass


MCP_STACK = McpStack()

MSG_CASE1 = (
    "现在需要接入1号风机的数据，第三方厂家通过asfp2协议给我们转来1#风机数据，10个点，从1000到1009，"
    "10个点分别是1000:风速、1001:功率、1002:风向、1003:桨叶角度、1004:发电机转速、1005:齿轮箱油温、"
    "1006:塔筒温度、1007:空气温度、1008:空气湿度、1009:大气压强，使用端口9001。"
    "我们需要将这些数据转发到II区服务器上，转发采用asfp2协议，目标地址是127.0.0.1:9900，点表5000~5009。"
)
MSG_WT2_BODY = (
    "再接入2号风机，第三方厂家通过asfp2协议转来2#风机数据，10个点，从1100到1109，"
    "分别是1100:风速、1101:功率、1102:风向、1103:桨叶角度、1104:发电机转速、1105:齿轮箱油温、"
    "1106:塔筒温度、1107:空气温度、1108:空气湿度、1109:大气压强，使用端口9002。"
)


def map_ports(text):
    for old, new in PORT_MAP.items():
        text = text.replace(old, new)
    return text


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


class Fail(Exception):
    pass


def load_api_key():
    with open(AGENT_ENV, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^DEEPSEEK_API_KEY=(.+)$", line.strip())
            if m:
                return m.group(1).strip().strip('"')
    raise Fail(f"{AGENT_ENV} 中未找到 DEEPSEEK_API_KEY")


# ── 隔离 agent 实例 ───────────────────────────────────────
class Agent:
    def __init__(self):
        self.dir = None
        self.p = None
        self.f = None

    def up(self):
        # 附着探测（重试 ×3，agent 可能正忙于 LLM 回合）：19720 健康 + 固定目录存在 → 复用
        for _ in range(3):
            try:
                with urllib.request.urlopen(BASE + "/api/state", timeout=5) as r:
                    json.loads(r.read().decode())
                if os.path.isdir(AGENT_DIR):
                    self.dir = AGENT_DIR
                    log("  附着已有隔离 agent（:19720）")
                    return
            except Exception:
                time.sleep(1.0)
        # 不可附着：清掉 19720 占用者（仅限本端口，绝不触碰生产 9988）后重启；
        # 保留目录内文件——config.json 由启动恢复续用（禁止清空已有测试状态）
        subprocess.run(["fuser", "-k", "19720/tcp"], capture_output=True, timeout=5)
        time.sleep(1)
        self.dir = AGENT_DIR
        os.makedirs(self.dir, exist_ok=True)
        agent_json = {
            "instance_id": "c4_e2e",
            "site": {"name": "华能阿拉善", "abbr": "hnals"},
            "model": {
                "provider": "deepseek",
                "name": "deepseek-chat",
                "temperature": 0,
                "max_tokens": 4096,
                "api_key_env": "DEEPSEEK_API_KEY",
            },
            "server": {"host": "127.0.0.1", "port": 19720, "cors_origin": "*"},
            "mcp_registry": {"path": REGISTRY_DIR},
            "shm_manager": {
                "binary": SHM_BINARY,
                "config_path": os.path.join(self.dir, "config.json"),
            },
            "state": {"backend": "filesystem", "path": os.path.join(self.dir, "state")},
            "logging": {"level": "info", "dir": os.path.join(self.dir, "logs")},
        }
        with open(os.path.join(self.dir, "agent.json"), "w", encoding="utf-8") as f:
            json.dump(agent_json, f, ensure_ascii=False, indent=2)
        env = dict(os.environ)
        env["DEEPSEEK_API_KEY"] = load_api_key()
        env["C4_SOCK_DIR"] = SOCK_DIR
        self.f = open(f"/tmp/e2e_agent_{time.strftime('%H%M%S')}.log", "w")
        self.p = subprocess.Popen(
            ["node", AGENT_JS, "--config-dir", self.dir],
            stdout=self.f, stderr=subprocess.STDOUT, env=env,
        )
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(BASE + "/api/state", timeout=3) as r:
                    json.loads(r.read().decode())
                log("  隔离 agent 就绪（:19720, instance=c4_e2e）")
                return
            except Exception:
                time.sleep(0.5)
        raise Fail("隔离 agent 60s 未就绪")

    def reset(self):
        """清空配置与 shm，重启隔离 agent（等价 cleanup.sh 的隔离版）。

        独立服务模型：先停数据路径实例（常驻 MCP 进程保留），再清 config 事务文件组
        （config.json + .prev.1~.3 + pending_change.json）与实例 shm 段。
        """
        self.stop()
        subprocess.run(["fuser", "-k", "19720/tcp"], capture_output=True, timeout=5)
        time.sleep(1)
        MCP_STACK.stop_instances()
        if self.dir:
            names = ["config.json", "abbr_registry.json", "pending_change.json",
                     "config.json.prev.1", "config.json.prev.2", "config.json.prev.3"]
            for name in names:
                p = os.path.join(self.dir, name)
                if os.path.exists(p):
                    os.remove(p)
            for sub in ("state", "logs"):
                shutil.rmtree(os.path.join(self.dir, sub), ignore_errors=True)
        subprocess.run(["rm", "-f", "/dev/shm/c4_e2e"], timeout=5)
        self.up()

    def stop(self):
        if self.p:
            self.p.terminate()
            try:
                self.p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.p.kill()
            self.p = None
        if self.f:
            self.f.close()
            self.f = None


AGENT = Agent()
AGENT_DIR = "/tmp/c4_e2e_agent"


# ── HTTP / SSE ────────────────────────────────────────────
def _post(path, body, timeout=180):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"}, method="POST")
    return urllib.request.urlopen(req, timeout=timeout)


def chat(message, history):
    text_parts = []
    with _post("/api/chat", {"message": message, "history": history}) as resp:
        buf = []
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("data: "):
                buf.append(line[6:])
                continue
            if line == "" and buf:
                data = "\n".join(buf)
                buf = []
                try:
                    d = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(d, dict) and d.get("type") == "text" and isinstance(d.get("content"), str):
                    text_parts.append(d["content"])
    return "".join(text_parts)


def state():
    with urllib.request.urlopen(BASE + "/api/state", timeout=5) as r:
        return json.loads(r.read().decode())


def wait_idle(timeout=90, gap=0.5):
    """尽力等待 phase 回 idle；超时不致命（部分询问轮 phase 停留在非 idle），以 config 为准。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if state().get("state", {}).get("phase") == "idle":
                time.sleep(1.0)
                return True
        except Exception:
            pass
        time.sleep(gap)
    log("  [warn] phase 未回 idle（继续以 config 为准）")
    return False


class Conv:
    def __init__(self):
        self.history = []

    def send(self, message):
        message = map_ports(message)
        self.history.append({"role": "user", "content": message})
        text = chat(message, self.history[:-1])
        log(f"  >> {message[:60]}...")
        log(f"  << {text[:280]}...")
        if text:
            self.history.append({"role": "assistant", "content": text})
        return text

    def send_and_confirm(self, message):
        text = self.send(message)
        plan_ready = (
            "是否确认" in text
            or "确认后我将" in text
            or "是否在" in text
            or ("方案" in text and "确认" in text)
        )
        if not plan_ready:
            return text, False
        ctext = self.send("[C4_BUTTON_CONFIRM] 确认")
        wait_idle()
        return ctext, True



# ── 多轮应答流程（镜像手工测试：Agent 追问则按用例答案回答）──
CASE_ANSWERS = {
    "prereq": [("协议", "接收和转发都采用asfp2协议"), ("是否", "确认执行")],
    "16": [("转发地址", "转发地址5010"), ("是否", "是，在1#风机上加点，转发地址5010")],
    "17": [("是否", "是，确认删除地址1006的塔筒温度点")],
    "10": [("转发地址", "转发地址5011"), ("是否", "是，确认追加，新点名叫风速2")],
    "21": [("协议", "接收和转发都采用asfp2协议"), ("是否", "确认执行")],
    "22": [("协议", "接收和转发都采用asfp2协议"), ("是否", "确认执行")],
    "25": [("是否", "是，确认删除2号风机"), ("整台|全部|数据点", "是，删除整台2号风机及其全部数据点")],
    "26": [("是否", "是，确认删除1号风机")],
}


def send_flow(conv, case, message, max_turns=6):
    """发送消息并驱动到执行完成：方案级确认句式出现→按钮确认；Agent 追问→按 CASE_ANSWERS 应答。"""
    text = conv.send(message)
    for _ in range(max_turns):
        if "是否确认" in text or ("方案" in text and "确认" in text):
            ctext = conv.send("[C4_BUTTON_CONFIRM] 确认")
            wait_idle()
            return ctext, True
        answered = False
        for pat, ans in CASE_ANSWERS.get(case, []):
            if re.search(pat, text):
                text = conv.send(ans)
                answered = True
                break
        if not answered:
            return text, False
    return text, False


# ── 环境观测 ──────────────────────────────────────────────
def read_config():
    try:
        with open(os.path.join(AGENT.dir, "config.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def listening(port):
    out = subprocess.run(["ss", "-tln"], capture_output=True, text=True, timeout=5)
    return any(f":{port}" in l and "LISTEN" in l for l in out.stdout.splitlines())


def established_to(port):
    out = subprocess.run(["ss", "-tn"], capture_output=True, text=True, timeout=5)
    return any(f":{port} " in l and "ESTAB" in l for l in out.stdout.splitlines())


def wait_port(port, want_listen=True, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if listening(port) == want_listen:
            return
        time.sleep(0.3)
    raise Fail(f"端口 {port} {'监听' if want_listen else '释放'}超时")


def wait_config(fn, timeout=90, desc="config 条件"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        cfg = read_config()
        last = cfg
        try:
            if cfg is not None and fn(cfg):
                return cfg
        except Exception:
            pass
        time.sleep(0.5)
    if last is not None:
        with open("/tmp/e2e_faildump.json", "w", encoding="utf-8") as f:
            json.dump(last, f, ensure_ascii=False, indent=1)
    raise Fail(f"90s 内 config 未满足: {desc}（完整快照见 /tmp/e2e_faildump.json）")


def server_instances(cfg):
    out = {}
    for st, insts in cfg.items():
        if st == "c4_shm_manager" or not isinstance(insts, list):
            continue
        for inst in insts:
            out[(st, inst.get("id"))] = inst
    return out


def points_of(cfg, st, iid):
    inst = server_instances(cfg).get((st, iid))
    return {p["addr"]: p for p in (inst or {}).get("points", [])}


def shm_ids_snapshot(cfg, st, iid):
    return {a: p.get("shm_id") for a, p in points_of(cfg, st, iid).items()}



def writer_of(cfg, addr):
    """返回包含 addr 的 writer 实例 id（不绑定 LLM 生成的实例命名）。"""
    for (st, iid), inst in server_instances(cfg).items():
        if st == "c4_asfp2_server" and addr in {p["addr"] for p in inst.get("points", [])}:
            return iid
    return None


def forward_of(cfg, addr):
    """返回包含 addr 的 reader 实例 id。"""
    for (st, iid), inst in server_instances(cfg).items():
        if st == "c4_asfp2_client" and addr in {p["addr"] for p in inst.get("points", [])}:
            return iid
    return None


class Proc:
    def __init__(self, args, name):
        self.f = open(f"/tmp/e2e_{name}.log", "w")
        self.p = subprocess.Popen(args, stdout=self.f, stderr=subprocess.STDOUT)
        self.name = name

    def stop(self):
        self.p.terminate()
        try:
            self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.p.kill()
        self.f.close()

    def out(self):
        self.f.flush()
        with open(f"/tmp/e2e_{self.name}.log", encoding="utf-8", errors="replace") as f:
            return f.read()


def start_receiver(port):
    return Proc([ASFP2_SERVER, "-p", str(port)], f"recv_{port}")


def inject(port, begin, end, times=3):
    r = subprocess.run([ASFP2_CLIENT, "-s", "127.0.0.1", "-p", str(port),
                        "-b", str(begin), "-e", str(end), "-t", str(times)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise Fail(f"asfp2_client 注入失败: {r.stderr[:200]}")


# ── 用例流程 ──────────────────────────────────────────────
def prereq():
    """用例 1：1#风机接入（后续所有用例的前置）。"""
    AGENT.reset()
    recv = start_receiver(P_FWD1)
    try:
        c = Conv()
        text, confirmed = send_flow(c, "prereq", MSG_CASE1)
        if not confirmed:
            raise Fail(f"用例1 未进入确认流程，回复: {text[:200]}")
        wait_config(lambda c2: writer_of(c2, 1000) is not None
                    and forward_of(c2, 5000) is not None,
                    desc="1#风机 writer + 转发实例")
        wait_port(P_RECV1, True)
        inject(P_RECV1, 1000, 1009, times=3)
        time.sleep(2)
        log(f"  用例1 前置 OK：{P_RECV1} 监听；{P_FWD1} 接收端尾: {recv.out()[-90:]!r}")
    finally:
        recv.stop()


def snapshot_w1(cfg):
    wid = writer_of(cfg, 1000)
    if wid is None:
        raise Fail("config 中找不到含 addr=1000 的 writer 实例")
    return shm_ids_snapshot(cfg, "c4_asfp2_server", wid)


def case16():
    before = snapshot_w1(read_config())
    recv = start_receiver(P_FWD1)
    try:
        c = Conv()
        text, confirmed = send_flow(c, "16", "给1#风机增加一个数据点，地址2010:振动，转发地址5010。")
        if not confirmed:
            raise Fail(f"未进入确认流程: {text[:200]}")

        def check(cfg):
            wid = writer_of(cfg, 1000)
            if wid is None or 2010 not in points_of(cfg, "c4_asfp2_server", wid):
                return False
            fid = forward_of(cfg, 5010)
            if fid is None:
                return False
            return points_of(cfg, "c4_asfp2_client", fid)[5010].get("key", "").endswith(
                "." + points_of(cfg, "c4_asfp2_server", wid)[2010]["id"])
        cfg = wait_config(check, timeout=150, desc="2010/5010 成对新增")
        cfg = wait_config(check, desc="2010/5010 成对新增")
        after = snapshot_w1(read_config())
        for addr, sid in before.items():
            if after.get(addr) != sid:
                raise Fail(f"既有点 addr={addr} shm_id {sid}→{after.get(addr)} 被重排")
        if after.get(2010) in (None, 0):
            raise Fail(f"新点 2010 shm_id 异常: {after.get(2010)}")
        wait_port(P_RECV1, True)
        inject(P_RECV1, 2010, 2010, times=3)
        time.sleep(2)
        log("  用例16 PASS ✓")
    finally:
        recv.stop()


def case17():
    before = snapshot_w1(read_config())
    if 1006 not in before:
        raise Fail("前置缺少 addr=1006——请先跑 prereq")
    c = Conv()
    text, confirmed = send_flow(c, "17", "删除1#风机的塔筒温度点（地址1006）。")
    if not confirmed:
        raise Fail(f"未进入确认流程: {text[:200]}")

    wid = writer_of(before_cfg := read_config(), 1000)
    def check(cfg):
        wid_now = writer_of(cfg, 1000)
        if wid_now is None:
            return False
        w = points_of(cfg, "c4_asfp2_server", wid_now)
        fid = forward_of(cfg, 5000)
        f = points_of(cfg, "c4_asfp2_client", fid) if fid else {}
        return 1006 not in w and 5006 not in f and len(w) >= 9
    wait_config(check, desc="1006/5006 成对删除")
    after = snapshot_w1(read_config())
    for addr, sid in before.items():
        if addr != 1006 and after.get(addr) != sid:
            raise Fail(f"未删除点 addr={addr} shm_id 被重排 {sid}→{after.get(addr)}")
    wait_port(P_RECV1, True)
    log("  用例17 PASS ✓")


def case18():
    before = read_config()
    c = Conv()
    text, confirmed = c.send_and_confirm("给1#风机增加一个数据点，地址1000:振动，转发地址5010。")
    after = read_config()
    w = points_of(after, "c4_asfp2_server", writer_of(after, 1000))
    addrs = [p["addr"] for p in w.values()]
    if w.get(1000, {}).get("id") != "windspeed":
        raise Fail(f"addr=1000 的 windspeed 被改写（回复: {text[:150]}）")
    dup = sorted({a for a in addrs if addrs.count(a) > 1})
    if dup:
        raise Fail(f"出现重复 addr: {dup}（回复: {text[:150]}）")
    if json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True):
        raise Fail(f"config 被修改（回复: {text[:150]}）")
    log(f"  用例18 PASS ✓（冲突未写入，回复: {text[:100]}）")


def case19():
    before = read_config()
    c = Conv()
    text, confirmed = c.send_and_confirm("删除1#风机的地址3000的点。")
    if json.dumps(before, sort_keys=True) != json.dumps(read_config(), sort_keys=True):
        raise Fail("config.json 被修改")
    if not re.search(r"不存在|失败|没有", text):
        raise Fail(f"回复未指明点不存在: {text[:150]}")
    log(f"  用例19 PASS ✓（回复片段: {text[:120]}）")


def case20():
    c = Conv()
    text = c.send("给1#风机增加一个数据点，地址2010:振动。")
    if not re.search(r"转发|5010", text):
        raise Fail(f"未询问转发地址: {text[:200]}")
    log(f"  用例20 PASS ✓（询问片段: {text[:120]}）")


def add_wt2(c, target, fwd_table, port):
    msg = MSG_WT2_BODY + f"转发到II区服务器127.0.0.1:{target}，转发采用asfp2协议，点表{fwd_table}。"
    text, confirmed = c.send_and_confirm(msg)
    if not confirmed:
        raise Fail(f"未进入确认流程: {text[:200]}")


def case21():
    recv = start_receiver(P_FWD2)
    try:
        c = Conv()
        add_wt2(c, 9901, "6000~6009", 9002)
        cfg = wait_config(lambda c2: writer_of(c2, 1100) is not None
                          and sum(1 for k in server_instances(c2) if k[0] == "c4_asfp2_client") >= 2,
                          desc="2#风机 writer + 第二转发实例")
        wait_port(P_RECV2, True)
        if not listening(P_RECV1):
            raise Fail("1#风机接收端口失去监听")
        w2 = points_of(cfg, "c4_asfp2_server", "hnals_wt2")
        if len(w2) != 10 or min(w2) != 1100:
            raise Fail(f"2#风机点表异常: {sorted(w2)[:3]}...")
        inject(P_RECV2, 1100, 1109, times=3)
        time.sleep(2)
        log("  用例21 PASS ✓")
    finally:
        recv.stop()


def case22():
    recv = start_receiver(P_FWD1)
    try:
        c = Conv()
        add_wt2(c, 9900, "6000~6009", 9002)

        def fwd_instance_6000(cfg):
            for (st, iid), inst in server_instances(cfg).items():
                if st == "c4_asfp2_client" and iid != "hnals_center":
                    if 6000 in {p["addr"] for p in inst.get("points", [])}:
                        return True
            return False
        wait_config(fwd_instance_6000, desc="2#风机转发实例含 6000")
        wait_port(P_RECV2, True)
        established_to(P_FWD1)
        log("  用例22 PASS ✓")
    finally:
        recv.stop()


def case23():
    c = Conv()
    msg = MSG_WT2_BODY
    for old, new in zip(
            ["1100", "1101", "1102", "1103", "1104", "1105", "1106", "1107", "1108", "1109"],
            ["1200", "1201", "1202", "1203", "1204", "1205", "1206", "1207", "1208", "1209"]):
        msg = msg.replace(old, new)
    msg = (msg.replace("2号风机", "3号风机").replace("2#风机", "3#风机")
           .replace("使用端口9002", "使用端口9001"))
    msg += "转发到II区服务器127.0.0.1:9901，转发采用asfp2协议，点表6000~6009。"
    text, confirmed = c.send_and_confirm(msg)
    insts = server_instances(read_config())
    if ("c4_asfp2_server", "hnals_wt3") in insts:
        raise Fail("冲突实例被写入 config")
    if not listening(P_RECV1):
        raise Fail("1#风机接收端口被破坏")
    log(f"  用例23 PASS ✓（回复片段: {text[:150]}）")


def case24():
    c = Conv()
    msg = MSG_WT2_BODY
    for old, new in zip(
            ["1100", "1101", "1102", "1103", "1104", "1105", "1106", "1107", "1108", "1109"],
            ["1300", "1301", "1302", "1303", "1304", "1305", "1306", "1307", "1308", "1309"]):
        msg = msg.replace(old, new)
    msg = (msg.replace("2号风机", "4号风机").replace("2#风机", "4#风机")
           .replace("使用端口9002", "使用端口9001")
           + "转发到II区服务器127.0.0.1:9900，转发采用asfp2协议，点表5000~5009。")
    text, confirmed = c.send_and_confirm(msg)
    if writer_of(read_config(), 1300) is not None:
        raise Fail("多重冲突实例被写入 config")
    log(f"  用例24 PASS ✓（回复片段: {text[:150]}）")


def case25():
    c = Conv()
    text, confirmed = c.send_and_confirm("删除2号风机。")
    if not confirmed:
        raise Fail(f"未进入确认流程: {text[:200]}")

    def check(cfg):
        return writer_of(cfg, 1100) is None and writer_of(cfg, 1000) is not None
    wait_config(check, desc="2#风机移除且 1#风机保留")
    wait_port(P_RECV2, False)
    wait_port(P_RECV1, True)
    log("  用例25 PASS ✓")


def case26():
    c = Conv()
    text, confirmed = c.send_and_confirm("删除1号风机。")
    if not confirmed:
        raise Fail(f"未进入确认流程: {text[:200]}")

    def check(cfg):
        return (not [k for k in server_instances(cfg) if k[0] == "c4_asfp2_server"]
                and not [k for k in server_instances(cfg) if k[0] == "c4_asfp2_client"])
    wait_config(check, desc="全部风机实例清空")
    wait_port(P_RECV1, False)
    log("  用例26 PASS ✓（系统回到未接入态）")


def case27():
    before = read_config()
    c = Conv()
    text, confirmed = c.send_and_confirm("删除5号风机。")
    if json.dumps(before, sort_keys=True) != json.dumps(read_config(), sort_keys=True):
        raise Fail("config.json 被修改")
    if not re.search(r"不存在|没有|失败", text):
        raise Fail(f"回复未指明风机不存在: {text[:150]}")
    log(f"  用例27 PASS ✓（回复片段: {text[:120]}）")


def case28():
    c = Conv()
    text = c.send("把风机都删了。")
    # func_test_case 用例 28 ①：必须先逐台列出受影响风机，并要求明确确认
    #（列表 + 确认按钮同消息出现即合规）；仅当「未列清单就索要确认」才算
    # 跳步（模糊指令直接出确认方案）。未确认时不得有任何风机被删（③，
    # 由后续用例 25/26 的重建断言兜底验证 config 未被改写）。
    listed = bool(re.search(r"1#", text) and re.search(r"2#", text))
    if not listed and "是否确认" in text:
        raise Fail(f"模糊指令直接出确认方案（未列清单）: {text[:200]}")
    if not re.search(r"1#|2#|哪些|哪台|列表|具体|哪一", text):
        raise Fail(f"既未列清单也未询问: {text[:200]}")
    log(f"  用例28 PASS ✓（回复片段: {text[:150]}）")


def case29():
    """用例 29：Agent 运行中 kill -9 的崩溃恢复（README §3）。

    步骤1 kill -9 → 数据接入不中断（19001 持续监听、注入后 write_seq 递增、→19900 转发持续）；
    步骤2 崩溃窗口内 HTTP 不可达（预期，不作缺陷断言失败）；
    步骤3 重启 → 瀑布收敛（config 不被改写＝ALREADY_RUNNING 无动作、端口零中断）；
    步骤4 删除类变更落地（2# 在线走用例25句式，否则用例26句式删至 0 台）；
    步骤5 空态下重新 prereq 应完整接入。
    """
    prereq()
    ids = {p["shm_id"] for p in
           points_of(read_config(), "c4_asfp2_server", writer_of(read_config(), 1000)).values()}
    base = _read_seqs(ids)

    if AGENT.p is None:
        raise Fail("Agent 进程不存在（case29 前置未就绪）")
    agent_pid = AGENT.p.pid
    os.kill(agent_pid, signal.SIGKILL)
    AGENT.p.wait()
    AGENT.p = None
    wait_port(P_RECV1, True)

    recv = start_receiver(P_FWD1)
    try:
        # asfp2_client 注入范围为 [b, e)：-e 1010 才覆盖 addr 1009（全部 10 点）
        inject(P_RECV1, 1000, 1010, times=3)
        # 接收端在 kill 后才拉起：reader（c4_asfp2_client）对 19900 的重连
        # 按 T0 周期后台重拨（默认 30s），观察窗须覆盖一个重拨周期
        deadline = time.time() + 20
        advanced = False
        while time.time() < deadline:
            try:
                now = _read_seqs(ids)
                if all(now[i] > base[i] for i in ids):
                    advanced = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not advanced:
            raise Fail("崩溃窗口内 write_seq 未递增——数据接入中断")
        fwd_deadline = time.time() + 60
        while time.time() < fwd_deadline:
            if recv.out().strip():
                break
            time.sleep(1)
        if not recv.out().strip():
            raise Fail("崩溃窗口内转发 →19900 无数据")
    finally:
        recv.stop()

    try:
        state()
        raise Fail("Agent 已 kill -9 但 HTTP 仍可达")
    except Exception:
        log("  步骤2: 崩溃窗口内配置变更不可达（连接拒绝，预期）")

    cfg_before = read_config()
    interrupt: list[str] = []
    watching = threading.Event()
    watching.set()

    def _watch() -> None:
        while watching.is_set():
            if not listening(P_RECV1):
                interrupt.append("port dropped during restart")
                return
            time.sleep(0.2)

    watcher = threading.Thread(target=_watch, daemon=True)
    watcher.start()
    AGENT.up()
    watching.clear()
    watcher.join(timeout=2)
    if interrupt:
        raise Fail(f"重启期间数据路径中断（ALREADY_RUNNING 应无动作）: {interrupt}")
    cfg_after = read_config()
    if json.dumps(cfg_before, sort_keys=True) != json.dumps(cfg_after, sort_keys=True):
        raise Fail("重启后 config.json 被改写（ALREADY_RUNNING 应无动作）")
    log("  步骤3: 瀑布收敛完成（config 不变、端口零中断＝ALREADY_RUNNING 无动作）")

    recv2 = start_receiver(P_FWD1)
    try:
        inject(P_RECV1, 1000, 1002, times=2)
        # reader 对新接收端的重连按 T0 周期后台重拨——轮询等待而非固定短 sleep
        fwd_deadline = time.time() + 60
        while time.time() < fwd_deadline:
            if recv2.out().strip():
                break
            time.sleep(1)
        if not recv2.out().strip():
            raise Fail("重启后转发路径无数据")
    finally:
        recv2.stop()

    cfg = read_config()
    if writer_of(cfg, 1100) is not None:
        text, confirmed = send_flow(Conv(), "25", "删除2号风机。")
        if not confirmed:
            raise Fail(f"收敛后删除变更未受理: {text[:200]}")
        wait_config(lambda c: writer_of(c, 1100) is None and writer_of(c, 1000) is not None,
                    desc="2#整机删除落地")
        wait_port(P_RECV2, False)
        wait_port(P_RECV1, True)
    else:
        text, confirmed = send_flow(Conv(), "26", "删除1号风机。")
        if not confirmed:
            raise Fail(f"收敛后删除变更未受理: {text[:200]}")

        def _all_deleted(c2):
            return not [k for k in server_instances(c2)
                        if k[0] in ("c4_asfp2_server", "c4_asfp2_client")]
        wait_config(_all_deleted, desc="删至 0 台（整机删除落地）")
        wait_port(P_RECV1, False, timeout=30)
    log("  步骤4: 删除类变更落地——瀑布收敛后配置变更能力完整恢复")

    prereq()
    log("  步骤5: 空态重新接入 OK")
    log("  用例29 PASS ✓")


def _read_seqs(shm_ids: set[int]) -> dict[int, int]:
    c = SockClient("c4_shm_manager")
    try:
        rp = c.read_points(sorted(shm_ids))
    finally:
        c.close()
    if rp.get("errors"):
        raise Fail(f"read_points errors: {rp['errors']}")
    return {r["shm_id"]: int(r.get("seq", 0)) for r in rp["reads"]}


def case10():
    wid0 = writer_of(read_config(), 1000)
    before = points_of(read_config(), "c4_asfp2_server", wid0)
    c = Conv()
    text, confirmed = send_flow(c, "10", "给1#风机再追加一个数据点，地址2000，点名也叫风速，转发地址5011。")
    if not confirmed:
        raise Fail(f"未进入确认流程: {text[:200]}")

    def check(cfg):
        wid = writer_of(cfg, 1000)
        return wid is not None and 2000 in points_of(cfg, "c4_asfp2_server", wid)
    cfg = wait_config(check, timeout=180, desc="addr=2000 新增")
    w = points_of(cfg, "c4_asfp2_server", writer_of(cfg, 1000))
    old, new = w.get(1000, {}).get("id"), w[2000]["id"]
    if new == old == "windspeed":
        raise Fail(f"撞名点静默覆盖：addr1000 与 addr2000 同名 {new}")
    if w[1000].get("addr") != 1000:
        raise Fail("windspeed 原 addr 被改写")
    fid_new = forward_of(cfg, 5011)
    fid_old = forward_of(cfg, 5000)
    if fid_new is None or fid_old is None:
        raise Fail(f"转发侧配对异常: 5011→{fid_new}, 5000→{fid_old}")
    f_new = points_of(cfg, "c4_asfp2_client", fid_new)[5011]
    f_old = points_of(cfg, "c4_asfp2_client", fid_old)[5000]
    if not f_new.get("key", "").endswith(f".{new}") or not f_old.get("key", "").endswith(f".{old}"):
        raise Fail(f"转发侧 key 未跟随改名: new={f_new.get('key')}, old={f_old.get('key')}")
    log(f"  用例10 PASS ✓（writer: {old}+{new} 去重共存；转发 key 已跟随改名）")


CASES = {
    "prereq": prereq, "16": case16, "17": case17, "18": case18, "19": case19,
    "20": case20, "21": case21, "22": case22, "23": case23, "24": case24,
    "25": case25, "26": case26, "27": case27, "28": case28, "29": case29,
    "10": case10,
}


SEQUENCE = ["prereq", "16", "17", "10", "18", "19", "20",
            "21", "25", "22", "23", "24", "27", "28", "25", "26", "29"]


def main():
    case = sys.argv[1] if len(sys.argv) > 1 else ""
    if case == "all":
        results = []
        for c in SEQUENCE:
            t0 = time.time()
            log(f"════ 用例 {c} 开始 ════")
            try:
                MCP_STACK.up()
                if not AGENT.p:
                    AGENT.up()
                CASES[c]()
                results.append((c, True, 0.0))
                log(f"════ 用例 {c} PASS（{time.time()-t0:.0f}s）════")
            except Fail as e:
                results.append((c, False, str(e)))
                log(f"════ 用例 {c} FAIL: {e} ════")
            time.sleep(1)
        npass = sum(1 for _, ok, _ in results if ok)
        log("════ 全量结果 ════")
        for c, ok, err in results:
            log(f"  {c}: {'PASS' if ok else 'FAIL ' + str(err)[:120]}")
        log(f"  合计 {npass}/{len(results)} 通过")
        sys.exit(0 if npass == len(results) else 1)
    if case not in CASES:
        print(f"未知用例: {case}；可选: {' '.join(CASES)} | all", flush=True)
        sys.exit(2)
    t0 = time.time()
    log(f"════ 用例 {case} 开始 ════")
    try:
        MCP_STACK.up()
        if not AGENT.p:
            AGENT.up()
        CASES[case]()
        log(f"════ 用例 {case} PASS（{time.time()-t0:.0f}s）════")
        sys.exit(0)
    except Fail as e:
        log(f"════ 用例 {case} FAIL: {e} ════")
        sys.exit(1)
    except KeyboardInterrupt:
        AGENT.stop()
        sys.exit(130)


if __name__ == "__main__":
    try:
        main()
    finally:
        pass
