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
import urllib.parse
import urllib.request

AGENT_JS = "/home/wangbo/work/activesys/c4/agent/dist/index.js"
SHM_BINARY = "/usr/local/bin/c4_shm_manager"
REGISTRY_DIR = "/usr/local/etc/c4/mcp-registry"
AGENT_ENV = "/usr/local/etc/c4/agent.env"
BASE = "http://127.0.0.1:19720"
ASFP2_SERVER = "/usr/local/bin/asfp2_server"
ASFP2_CLIENT = "/usr/local/bin/asfp2_client"
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.log")

PORT_MAP = {"9001": "19001", "9002": "19002", "9900": "19900", "9901": "19901",
            "9902": "19902", "9903": "19903", "9904": "19904",
            "172.16.109.12": "127.0.0.1", "8086": "18086"}
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
            m = re.match(r"^ZHIPU_API_KEY=(.+)$", line.strip())
            if m:
                return m.group(1).strip().strip('"')
    raise Fail(f"{AGENT_ENV} 中未找到 ZHIPU_API_KEY")


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
                "provider": "zhipu",
                "name": "glm-5.3-flash",
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "temperature": 0,
                "max_tokens": 4096,
                "api_key_env": "ZHIPU_API_KEY",
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
        env["ZHIPU_API_KEY"] = load_api_key()
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
def _post(path, body, timeout=300):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"}, method="POST")
    return urllib.request.urlopen(req, timeout=timeout)


def chat(message, history):
    """返回 (完整文本, 事件列表)；事件用于过程健康度断言（README §4）。
    事件形如 (type, name)：("tool_call","output_access_plan") / ("error","") 等。
    新架构（agent.md §2.8）：方案武装信号为 button_arm 语义事件（不再从工具事件推断——
    工具副作用 ≠ 语义状态）；本函数对旧工具事件与新语义事件双兼容探测。"""
    text_parts = []
    events = []
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
                if not isinstance(d, dict):
                    continue
                t = d.get("type")
                if t == "text" and isinstance(d.get("content"), str):
                    text_parts.append(d["content"])
                elif t in ("tool_call", "tool_result"):
                    events.append((t, str(d.get("name", ""))))
                elif t == "error":
                    events.append(("error", ""))

    return "".join(text_parts), events


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
        PH.user(message)
        self.history.append({"role": "user", "content": message})
        text, events = chat(message, self.history[:-1])
        log(f"  >> {message[:60]}...")
        log(f"  << {text[:280]}...")
        tools = {name for (t, name) in events if t == "tool_call"}
        PH.assistant(text, tools)
        for (t, _name) in events:
            if t == "error":
                PH.event("error")
        if text:
            self.history.append({"role": "assistant", "content": text})
        return text

    def send_and_confirm(self, message):
        # 确认通道：按钮唯一确认（agent.md §2.8）——[C4_BUTTON_CONFIRM] 前缀消息；
        # 自由文本"确认"不构成确认（func_test_case 用例 5 定稿方案）
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
    "31": [("端口|从站", "端口502，从站号都是1。"), ("吗", "是asfp2协议。")],
    "33": [("协议", "就用asfp2吧。"), ("吗", "是asfp2协议。")],
    "35": [("端口", "端口是2404。")],
    "38": [("吗", "对，写入influxdb。")],
    "39": [("bucket", "bucket是hnals。"), ("吗", "对，写入influxdb。")],
    "40": [("吗", "对，写入influxdb。")],
    "42": [],
}


def send_flow(conv, case, message, max_turns=10):
    """发送消息并驱动到执行完成：方案级确认句式出现→按钮确认；Agent 追问→按 CASE_ANSWERS 应答。
    点击后不返回而是继续观察响应——闸门若因 access_plan 复位而再次索要确认（用例 7 修复 C 的
    「是否确认执行」句式），则再次点击，镜像真实用户重复点击按钮的行为。"""
    clicked = False
    empty_streak = 0
    def _icount(c):
        if not isinstance(c, dict):
            return 0
        return sum(len(v) for k, v in c.items()
                   if isinstance(v, list) and k != "c4_shm_manager")
    n0 = _icount(read_config())
    text = conv.send(message)
    for _ in range(max_turns):
        if _icount(read_config()) != n0:
            return text, clicked  # 实例数变化＝执行落地——停止驱动，防「继续」诱发幻觉任务
        if clicked < 2 and ("是否确认" in text or "确认执行" in text
                            or ("方案" in text and "是否" in text)):
            text = conv.send("[C4_BUTTON_CONFIRM] 确认")
            clicked += 1
            wait_idle()
            continue
        answered = False
        for pat, ans in CASE_ANSWERS.get(case, []):
            if re.search(pat, text):
                text = conv.send(ans)
                answered = True
                break
        if not answered:
            # LLM 旁白轮或空回复——退避后催促；连续空回复 ≥2 时改发原消息重新触发
            time.sleep(12)
            empty_streak = empty_streak + 1 if not text.strip() else 0
            text = conv.send(message if empty_streak >= 2 else "继续")
            continue
    return text, clicked


# ── 过程健康度断言（func_case_e2e/README.md §4；agent.md §2.4）──
# #1 按钮预算 / #2 无假成功 / #3 无空转轮次 / #4 无同回合自问自答：现已生效
#   （#1 由 runner 侧自计数，无需后端事件；#2/#3/#4 基于会话文本 + tool_call 事件）
# #5 一次成功执行：依赖 config_merge/rollback 事件落线（agent.md §2.4.5），未落地前 SKIP
# 判定粒度 = case 级（回合级的近似）；问询句式定义同 agent.md §2.4.2（含方案确认句式排除清单）

CONFIRM_PHRASE_RE = re.compile(r"是否确认执行|确认执行")
SUCCESS_MARK_RE = re.compile(r"执行完成|已执行成功|已配置完成|接入完成|接入方案已执行")
FAKE_TAIL_RE = re.compile(r"没有真正完成|重新执行|重新提交|接入失败|需人工核验|配置未生效")
IDLE_TEXT_RE = re.compile(r"[\s.。…~]*\Z")
PLAN_TOOLS = {"output_access_plan", "output_plan_steps"}  # 兼容旧实现探测；新架构（agent.md §3.2.0.1）方案层为纯代码，武装信号 = button_arm 事件
BUTTON_BUDGET = {}   # 每用例按钮预算覆盖表；缺省 2（= send_flow 驱动器 clicked<2 上限）


class ProcessHealth:
    def __init__(self):
        self.reset("")

    def reset(self, case, button_budget=2):
        self.case = case
        self.button_budget = button_budget
        self.confirm_sends = 0
        self.tools_seen = set()
        self.entries = []   # {"kind":"assistant","text":str,"tools":set} / {"kind":"event","etype","name"}

    def user(self, msg):
        if msg.startswith("[C4_BUTTON_CONFIRM]"):
            self.confirm_sends += 1
        self.entries.append({"kind": "user", "text": msg})

    def assistant(self, text, tools=None):
        self.entries.append({"kind": "assistant", "text": text or "", "tools": set(tools or ())})

    def event(self, etype, name=""):
        self.tools_seen.add(name)
        self.entries.append({"kind": "event", "etype": etype, "name": name})

    def _question_hit(self, text):
        if CONFIRM_PHRASE_RE.search(text):
            return False   # 方案确认句式排除（agent.md §2.4.2）
        for sent in re.split(r"[。；！\n]", text or ""):
            sent = sent.strip()
            if not sent:
                continue
            if sent.endswith("？") or sent.endswith("?"):
                return True
            if re.match(r"^(请提供|请补充|请确认|请问|是否)", sent):
                return True
        return False

    def verdict(self):
        """返回 (violations, notes)；violations 非空即过程 FAIL。"""
        v, notes = [], []

        # 1 按钮预算
        if self.confirm_sends > self.button_budget:
            v.append(f"#1 按钮确认 {self.confirm_sends} 次 > 预算 {self.button_budget}")

        # 2 无假成功：成功表述之后不得出现失败信号/错误事件
        success_idx = [i for i, e in enumerate(self.entries)
                       if e["kind"] == "assistant" and SUCCESS_MARK_RE.search(e["text"])]
        if success_idx:
            for e in self.entries[success_idx[-1]:]:
                if (e["kind"] == "assistant" and FAKE_TAIL_RE.search(e["text"])) \
                        or (e["kind"] == "event" and e.get("etype") == "error"):
                    v.append("#2 假成功：成功表述之后出现失败信号")
                    break

        # 3 无空转轮次：同一回合内连续 >1 轮空文本/省略号且无工具调用（用户消息重置）
        streak = 0
        for e in self.entries:
            if e["kind"] == "user":
                streak = 0
            elif e["kind"] == "assistant" and not e["tools"] and IDLE_TEXT_RE.match(e["text"] or " "):
                streak += 1
                if streak > 1:
                    v.append("#3 连续空转轮次 > 1")
                    break
            else:
                streak = 0

        # 4 无同回合自问自答：问询命中后、下一条用户消息之前调用方案/执行工具
        #   （用户消息 = 回合边界，重置问询态——跨回合的正常追问不违例，agent.md §2.4.2）
        q_hit = False
        for e in self.entries:
            if e["kind"] == "user":
                q_hit = False
                continue
            hit = e["kind"] == "assistant" and self._question_hit(e["text"])
            tools = (e.get("tools") or set()) if e["kind"] == "assistant" \
                else ({e["name"]} if e.get("name") in PLAN_TOOLS else set())
            if (q_hit or hit) and tools & PLAN_TOOLS:
                v.append("#4 问询后同回合调用 output_access_plan/output_plan_steps")
                break
            q_hit = q_hit or hit

        # 5 一次成功执行（SKIP：等 §2.4.5 后端 config_merge/rollback 事件）
        if not self.tools_seen & {"execute_access_plan"}:
            notes.append("#5 SKIP（merge/rollback 事件未上线）")

        return v, notes


PH = ProcessHealth()


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


def wait_config(fn, timeout=180, desc="config 条件"):
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
    if not isinstance(cfg, dict):
        return out
    for st, insts in cfg.items():
        if st == "c4_shm_manager" or not isinstance(insts, list):
            continue
        for inst in insts:
            out[(st, inst.get("id"))] = inst
    return out


def points_of(cfg, st, iid):
    inst = server_instances(cfg).get((st, iid))
    out = {}
    for j, p in enumerate((inst or {}).get("points", [])):
        out[p.get("addr", p.get("field", p.get("id", j)))] = p
    return out


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
        cfg = wait_config(check, timeout=240, desc="2010/5010 成对新增")
        cfg = wait_config(check, timeout=180, desc="2010/5010 成对新增")
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
    wait_config(check, timeout=180, desc="1006/5006 成对删除")
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
    wait_config(check, timeout=180, desc="2#风机移除且 1#风机保留")
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
    wait_config(check, timeout=180, desc="全部风机实例清空")
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


# ══ 用例 30~43：modbus / iec104 / influxdb 真实服务扩展 ══════════════════
# 环境真实服务（无 mock，见 func_test_case.md 用例 30~43）：
#   modbusd  192.168.110.51:502（用例30/42 变桨控制器，点位 3000~3018）
#   modbusd  192.168.110.52:502（用例31 齿轮箱油泵，点位 4000 线圈 + 4100~4112）
#   iec104d  192.168.110.99:2404 CA=1（用例34）/ .199:2404 CA=1（用例35）/ .102:2404 CA=2（用例37）
#   influxd  127.0.0.1:18086（用例38~40/43，auth 关闭，任意 token 可用）
# 端口适配（记录于用例关键点允许范围）：转发目标 9900/9901/9902/9903/9904 → 1990x；
#   influx 写入地址 172.16.109.12:8086 → 127.0.0.1:18086（map_ports 完成，断言按映射后值）。
INFLUXD_BIN = "/home/wangbo/backup/influxdb/influxdb-1.8.10-1/usr/bin/influxd"
INFLUXD_CONF = "/tmp/c4_influxdb/influxdb.conf"
INFLUX_URL = "http://127.0.0.1:18086"
INFLUX_TOKEN = "hnals-influx-2026"
MODBUSD51_CFG = "/tmp/c4_env/modbusd.json"
MODBUSD52_CFG = "/tmp/c4_env/modbusd_52.json"

MSG30 = (
    "现在需要接入1号风机变桨控制器的数据，厂家在变桨控制器上开放了modbus采集口，"
    "设备IP是192.168.110.51，端口502，从站号1。点表10个点：3000:桨叶角度、3002:变桨速度、"
    "3004:变桨电机温度、3006:变桨电机电流、3008:后备电源电压、3010:后备电源温度、"
    "3012:桨叶1位置、3014:桨叶2位置、3016:桨叶3位置、3018:轮毂温度。"
    "除桨叶1/2/3位置是输入寄存器（功能码4）外，其余全是保持寄存器（功能码3），"
    "全部32位浮点、字节交换2（swap=2）。我们需要将这些数据转发到II区服务器上，"
    "转发采用asfp2协议，目标地址是127.0.0.1:9900，点表5000~5009。"
)
MSG31 = (
    "现在需要接入1号风机齿轮箱油泵控制器的数据，modbus协议，设备IP是192.168.110.52。"
    "点表8个点：4000:油泵运行状态是线圈（功能码1、布尔类型、swap为0），"
    "其余7个点4100:油温、4102:油压、4104:油位、4106:泵后压力、4108:电机温度、4110:滤网压差、4112:油流量"
    "全是保持寄存器（功能码3）、32位浮点、swap=2。"
    "转发到II区127.0.0.1:9901，转发采用asfp2协议，点表5100~5107。"
)
MSG32 = (
    "现在需要接入1号风机机舱控制柜的数据，modbus协议，设备IP是192.168.110.53，端口502，从站号1。"
    "点表6个点：3200:机舱温度、3202:机舱振动、3204:塔基温度、3210:机舱湿度、3210:舱外风向、3212:偏航角度，"
    "除3212偏航角度是功能码5外其余都是保持寄存器（功能码3），全部32位浮点、swap=2。"
    "转发到II区127.0.0.1:9902，转发采用asfp2协议，点表5200~5205。"
)
MSG33 = (
    "接入1号风机地面环网柜的数据，modbus协议，设备IP是192.168.110.54，端口502，从站号1，"
    "点表4个点：3300:环网柜温度、3302:环网柜湿度、3304:电缆头温度、3306:局放值，"
    "全是保持寄存器（功能码3）、32位浮点、swap=2。"
    "数据要送到II区127.0.0.1:9900，点表5300~5303。"
)
MSG34 = (
    "现在需要接入1号主变测控装置的数据，装置是IEC104规约，IP是192.168.110.99，端口2404，公共地址1。"
    "点表6个点：16385:UAB电压、16386:UBC电压、16387:UAC电压、1:弹簧未储能、2:装置异常、25601:正向有功电度。"
    "我们需要将这些数据转发到II区服务器上，转发采用asfp2协议，目标地址是127.0.0.1:9902，点表5400~5405。"
)
MSG35 = (
    "接入2号主变测控装置，IEC104规约，装置IP是192.168.110.199，公共地址1。"
    "点表4个点：16385:UAB电压、16386:UBC电压、1:弹簧未储能、25601:正向有功电度。"
    "转发到II区127.0.0.1:9903，转发采用asfp2协议，点表5500~5503。"
)
MSG36 = (
    "现在需要接入1号升压站公用测控装置的数据，IEC104规约，装置IP是192.168.110.101，端口2404，公共地址1。"
    "点表4个点：16385:UAB电压、16385:UAB线电压、16386:UBC电压、1:弹簧未储能。"
    "转发到II区127.0.0.1:9904，转发采用asfp2协议，点表5600~5603。"
)
MSG37 = (
    "再接入3号主变测控装置，IEC104规约，装置IP是192.168.110.102，端口2404，公共地址2。"
    "点表与2号主变完全一样：16385:UAB电压、16386:UBC电压、1:弹簧未储能、25601:正向有功电度。"
    "转发到II区127.0.0.1:9904，转发采用asfp2协议，点表5700~5703。"
)
MSG38 = (
    "现在需要接入1号风机的数据并直接入库。第三方厂家通过asfp2协议给我们转来1#风机数据，10个点，"
    "从1000到1009，分别是1000:风速、1001:功率、1002:风向、1003:桨叶角度、1004:发电机转速、"
    "1005:齿轮箱油温、1006:塔筒温度、1007:空气温度、1008:空气湿度、1009:大气压强，使用端口9001。"
    "数据写入我们的InfluxDB时序库：写入地址http://172.16.109.12:8086，token是hnals-influx-2026，"
    "org是activesys，bucket是hnals。10个点全部写进wind_turbine这个measurement，"
    "字段名跟点名对应（windspeed、power、wind_dir、pitch_angle、gen_speed、gearbox_oil_temp、"
    "tower_temp、air_temp、humidity、pressure），类型统一float。"
)
MSG39 = MSG38.replace("写入地址http://172.16.109.12:8086，token是hnals-influx-2026，"
                      "org是activesys，bucket是hnals。",
                      "写入InfluxDB：写入地址http://172.16.109.12:8086，token是hnals-influx-2026，org是activesys。")
MSG41 = ("给1号风机的入库再加一条：风速除了wind_turbine，也同步写一份到wind_anomaly这个measurement，"
         "字段也叫windspeed，类型float。")
MSG43 = ("这10个点都写一份到另一个bucket：url同，还是http://172.16.109.12:8086，"
         "token是hnals-influx-2026，org是activesys，bucket换成wind_history，"
         "measurement和字段名跟wind_turbine那边一样，类型统一float。")

MB30_VALS = {12.5, 15.5, 45.5, 25.5, 220.5, 28.5, 30.5, 30.0, 35.0, 40.0}
MB31_VALS = {1.0, 41.5, 2.5, 60.0, 3.2, 55.5, 0.8, 12.3}
T104_34_VALS = {1.0, 220.5, 50.2, 1024.0}
T104_35_VALS = {1.0, 220.5, 50.2}
INFLUX_FIELDS = ["windspeed", "power", "wind_dir", "pitch_angle", "gen_speed",
                 "gearbox_oil_temp", "tower_temp", "air_temp", "humidity", "pressure"]


def find_inst(cfg, st, ip=None, port=None, bucket=None):
    """按服务类型 + ip/port/bucket 定位实例（不绑定 LLM 命名）。"""
    for (s, _iid), inst in server_instances(cfg).items():
        if s != st:
            continue
        if ip is not None and str(inst.get("ip") or inst.get("host") or "") != str(ip):
            continue
        if port is not None:
            try:
                if int(inst.get("port") or -1) != int(port):
                    continue
            except (TypeError, ValueError):
                continue
        if bucket is not None and inst.get("bucket") != bucket:
            continue
        return inst
    return None


def shm_values(shm_ids, want=None, timeout=45):
    """轮询 read_points 直至 seq>0 的值集合与 want 匹配（浮点容差）；返回 {shm_id: value}。"""
    ids = sorted(shm_ids)
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            c = SockClient("c4_shm_manager")
            rp = c.read_points(ids)
            c.close()
            vals = {}
            for r in rp.get("reads", []):
                v = r.get("value")
                if isinstance(v, str):
                    try:
                        v = float(v)
                    except ValueError:
                        v = None
                if r.get("seq", 0) and isinstance(v, (int, float)):
                    vals[r["shm_id"]] = float(v)
            last = vals
            if want is None:
                return vals
            got = sorted(last.values())
            exp = sorted(want)
            if len(got) == len(exp) and all(
                    abs(a - b) <= 0.05 + 1e-3 * abs(b) for a, b in zip(got, exp)):
                return vals
        except Exception:
            pass
        time.sleep(1.5)
    raise Fail(f"shm 值未达标（timeout={timeout}s）：got={last} want={sorted(want)}")


def points_values(cfg, st, inst, want, timeout=45, desc=""):
    ids = {p["shm_id"] for p in inst.get("points", []) if p.get("shm_id")}
    if not ids:
        raise Fail(f"{st} 实例无 shm_id")
    return shm_values(ids, want=want, timeout=timeout)


def influx_query(bucket, measurement="wind_turbine"):
    # influxd 1.8 的 /api/v2/query 恒 403（写可用、查不可用）——走 v1 兼容端点
    q = urllib.parse.quote(f'SELECT * FROM "{measurement}" ORDER BY time DESC LIMIT 1')
    req = urllib.request.Request(f"http://127.0.0.1:18086/query?db={bucket}&q={q}")
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read().decode())
    series = (d.get("results") or [{}])[0].get("series") or []
    vals = {}
    for s in series:
        cols, rows = s.get("columns", []), s.get("values", [])
        if rows:
            vals.update({c: v for c, v in zip(cols, rows[0])
                         if c != "time" and isinstance(v, (int, float))})
    return vals


def influx_wait(bucket, fields, timeout=90, measurement="wind_turbine"):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            last = influx_query(bucket, measurement)
            if set(fields) <= set(last):
                return last
        except Exception:
            pass
        time.sleep(2)
    raise Fail(f"InfluxDB bucket={bucket} 未查到字段 {fields}（got={sorted(last)}）")


def _pids(pattern):
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5)
    return [int(x) for x in out.stdout.split()]


def stop_modbusd51():
    for pid in _pids("modbusd -c /tmp/c4_env/modbusd.json$"):
        os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 8
    while time.time() < deadline and _pids("modbusd -c /tmp/c4_env/modbusd.json$"):
        time.sleep(0.3)
    if _pids("modbusd -c /tmp/c4_env/modbusd.json$"):
        raise Fail("modbusd(.51) 停止失败")


def start_modbusd51():
    env = dict(os.environ)
    env["ACQUISITION"] = "/var/acquisition"
    with open("/tmp/oc/mb51_runner.out", "a") as f:
        subprocess.Popen(["/usr/local/bin/modbusd", "-c", MODBUSD51_CFG],
                         stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, env=env,
                         start_new_session=True)
    deadline = time.time() + 10
    while time.time() < deadline:
        if listening(502) or any(_pids("modbusd -c /tmp/c4_env/modbusd.json$")):
            time.sleep(1)
            return
    raise Fail("modbusd(.51) 重启失败")


def stop_influxd():
    for pid in _pids("influxd -config"):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + 15
    while time.time() < deadline and _pids("influxd -config"):
        time.sleep(0.5)
    if _pids("influxd -config"):
        raise Fail("influxd 停止失败")


def start_influxd():
    with open("/tmp/oc/influxd_runner.out", "a") as f:
        subprocess.Popen([INFLUXD_BIN, "-config", INFLUXD_CONF],
                         stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
                         start_new_session=True)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            urllib.request.urlopen(INFLUX_URL + "/ping", timeout=2)
            return
        except Exception:
            time.sleep(0.5)
    raise Fail("influxd 30s 未就绪")


def case_reset():
    """清空隔离环境（等价全新环境）：停实例、清 config/shm、重启 agent。"""
    AGENT.reset()
    log("  reset OK（全新环境）")


def case30():
    recv = start_receiver(P_FWD1)
    try:
        c = Conv()
        text, confirmed = send_flow(c, "30", MSG30)
        if not confirmed:
            raise Fail(f"未进入确认流程: {text[:200]}")

        def check(cfg):
            m = find_inst(cfg, "c4_modbus_client", ip="192.168.110.51", port=502)
            if not m or len(m.get("points", [])) != 10:
                return False
            return forward_of(cfg, 5000) is not None
        cfg = wait_config(check, timeout=240, desc="modbus 实例(.51:502,10点) + 转发实例")
        m = find_inst(cfg, "c4_modbus_client", ip="192.168.110.51", port=502)
        pts = {p["addr"]: p for p in m["points"]}
        for a, p in pts.items():
            exp_fun = 4 if a in (3012, 3014, 3016) else 3
            if p.get("fun") != exp_fun:
                raise Fail(f"addr={a} fun={p.get('fun')} 期望 {exp_fun}")
            if p.get("swap") != 2 or p.get("type") != 10 or p.get("uid") != 1:
                raise Fail(f"addr={a} 字段异常: swap={p.get('swap')} type={p.get('type')} uid={p.get('uid')}")
        fid = forward_of(cfg, 5000)
        fp = points_of(cfg, "c4_asfp2_client", fid)
        if sorted(fp) != list(range(5000, 5010)):
            raise Fail(f"转发点表异常: {sorted(fp)}")
        established_to(502)
        values = points_values(cfg, "c4_modbus_client", m, MB30_VALS, desc="modbus 对点")
        log(f"  modbus 对点 OK（{len(values)} 点真实值）")
        fwd_deadline = time.time() + 60
        while time.time() < fwd_deadline and not recv.out().strip():
            time.sleep(1)
        if not recv.out().strip():
            raise Fail("转发 →19900 无数据")
        log("  用例30 PASS ✓")


    finally:
        recv.stop()
def case31():
    c = Conv()
    text, confirmed = send_flow(c, "31", MSG31)
    if not confirmed:
        raise Fail(f"未进入确认流程: {text[:200]}")

    def check(cfg):
        m = find_inst(cfg, "c4_modbus_client", ip="192.168.110.52", port=502)
        return bool(m) and len(m.get("points", [])) == 8 and forward_of(cfg, 5100) is not None
    cfg = wait_config(check, timeout=240, desc="modbus 实例(.52:502,8点) + 转发实例")
    m = find_inst(cfg, "c4_modbus_client", ip="192.168.110.52", port=502)
    pts = {p["addr"]: p for p in m["points"]}
    if pts[4000].get("fun") != 1 or pts[4000].get("type") != 15 or pts[4000].get("swap") != 0:
        raise Fail(f"线圈点字段异常: {pts[4000]}")
    for a in range(4100, 4113, 2):
        if pts[a].get("fun") != 3 or pts[a].get("type") != 10 or pts[a].get("swap") != 2:
            raise Fail(f"addr={a} 字段异常: {pts[a]}")
    points_values(cfg, "c4_modbus_client", m, MB31_VALS, desc="油泵对点")
    log("  用例31 PASS ✓（线圈 BIT + 浮点真实值对点通过）")


def case32():
    before = read_config()
    c = Conv()
    text, _ = send_flow(c, "32", MSG32)
    after = read_config()
    if json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True):
        raise Fail(f"config 被修改（回复: {text[:150]}）")
    if find_inst(after, "c4_modbus_client", ip="192.168.110.53") is not None:
        raise Fail("冲突实例被写入 config")
    alltext = "\n".join(str(m.get("content", "")) for m in c.history)
    dup = re.search(r"3210", alltext) and re.search(r"重复|冲突|已存在", alltext)
    badfun = re.search(r"功能码|fun|非法", alltext)
    if not dup:
        raise Fail(f"全程未指出 3210 重复: {text[:200]}")
    if not badfun:
        raise Fail(f"全程未指出功能码 5 非法: {text[:200]}")
    log(f"  用例32 PASS ✓（逐项拒绝，config 不变）")


def case33():
    c = Conv()
    text, confirmed = send_flow(c, "33", MSG33)
    if not confirmed:
        raise Fail(f"未进入确认流程（握手死锁?）: {text[:200]}")

    def check(cfg):
        m = find_inst(cfg, "c4_modbus_client", ip="192.168.110.54", port=502)
        return bool(m) and len(m.get("points", [])) == 4 and forward_of(cfg, 5300) is not None
    wait_config(check, timeout=240, desc="环网柜 modbus 实例 + 转发实例（双侧成对）")
    log("  用例33 PASS ✓（转发协议问答握手后接入成功，无死锁）")


def case34():
    recv = start_receiver(P_FWD1 + 2)
    try:
        c = Conv()
        text, confirmed = send_flow(c, "34", MSG34)
        if not confirmed:
            raise Fail(f"未进入确认流程: {text[:200]}")

        def check(cfg):
            i = find_inst(cfg, "c4_iec104_client", ip="192.168.110.99", port=2404)
            return bool(i) and len(i.get("points", [])) == 6 and forward_of(cfg, 5400) is not None
        cfg = wait_config(check, timeout=240, desc="iec104 实例(.99:2404,6点) + 转发实例")
        i = find_inst(cfg, "c4_iec104_client", ip="192.168.110.99", port=2404)
        ca = None
        for k in ("common_address", "ca", "commonAddress"):
            if k in i:
                ca = i[k]
                break
        if ca is not None and int(ca) != 1:
            raise Fail(f"公共地址被改写: {ca}")
        if sorted(p["addr"] for p in i["points"]) != [1, 2, 16385, 16386, 16387, 25601]:
            raise Fail(f"IOA 点表异常: {sorted(p['addr'] for p in i['points'])}")
        established_to(2404)
        points_values(cfg, "c4_iec104_client", i, T104_34_VALS, timeout=60, desc="104 对点")
        fwd_deadline = time.time() + 60
        while time.time() < fwd_deadline and not recv.out().strip():
            time.sleep(1)
        if not recv.out().strip():
            raise Fail("转发 →19902 无数据")
        log("  用例34 PASS ✓")


    finally:
        recv.stop()
def case35():
    recv = start_receiver(P_FWD1 + 3)
    try:
        c = Conv()
        text, confirmed = send_flow(c, "35", MSG35)
        if not confirmed:
            raise Fail(f"未进入确认流程: {text[:200]}")

        def check(cfg):
            i = find_inst(cfg, "c4_iec104_client", ip="192.168.110.199", port=2404)
            return bool(i) and len(i.get("points", [])) == 4 and forward_of(cfg, 5500) is not None
        cfg = wait_config(check, timeout=240, desc="iec104 实例(.199:2404,4点) + 转发实例")
        i = find_inst(cfg, "c4_iec104_client", ip="192.168.110.199", port=2404)
        established_to(2404)
        points_values(cfg, "c4_iec104_client", i, T104_35_VALS, timeout=60, desc="2号主变对点")
        log("  用例35 PASS ✓")


    finally:
        recv.stop()
def case36():
    before = read_config()
    c = Conv()
    text = c.send(MSG36)
    rejected = False
    for _ in range(10):
        if re.search(r"16385", text) and re.search(r"重复|冲突|已存在|唯一|INVALID|不能共用|无法接入", text):
            rejected = True
            break
        if "是否确认" in text or "确认执行" in text:
            break
        if re.search(r"保留|合并|方案|去重", text):
            text = c.send("不合并，两个点都必须保留；若无法接入就明确拒绝本次接入。")
            continue
        text = c.send("继续")
    after = read_config()
    if json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True):
        raise Fail(f"config 被修改（回复: {text[:150]}）")
    if find_inst(after, "c4_iec104_client", ip="192.168.110.101") is not None:
        raise Fail("IOA 重复实例被写入 config")
    if not rejected:
        raise Fail(f"未明确拒绝 IOA 16385 重复: {text[:200]}")
    log(f"  用例36 PASS ✓（IOA 重复可读拒绝，config 不变）")


def case37():
    cfg0 = read_config()
    prev_snap = {}
    for ip in ("192.168.110.99", "192.168.110.199"):
        i = find_inst(cfg0, "c4_iec104_client", ip=ip, port=2404)
        if i:
            prev_snap[ip] = shm_ids_snapshot(cfg0, "c4_iec104_client", i["id"])
    if len(prev_snap) != 2:
        raise Fail(f"前置缺失 1号/2号主变: {sorted(prev_snap)}")
    recv = start_receiver(P_FWD1 + 4)
    try:
        c = Conv()
        text, confirmed = send_flow(c, "37", MSG37)
        if not confirmed:
            raise Fail(f"未进入确认流程: {text[:200]}")

        def check(cfg):
            i = find_inst(cfg, "c4_iec104_client", ip="192.168.110.102", port=2404)
            return bool(i) and len(i.get("points", [])) == 4 and forward_of(cfg, 5700) is not None
        cfg = wait_config(check, timeout=240, desc="3号主变实例(.102:2404) + 转发实例")
        i3 = find_inst(cfg, "c4_iec104_client", ip="192.168.110.102", port=2404)
        for ip, snap in prev_snap.items():
            now = shm_ids_snapshot(read_config(), "c4_iec104_client",
                                   find_inst(read_config(), "c4_iec104_client", ip=ip, port=2404)["id"])
            if now != snap:
                raise Fail(f"{ip} 既有点 shm_id 被重排")
        established_to(2404)
        points_values(cfg, "c4_iec104_client", i3, T104_35_VALS, timeout=60, desc="3号主变对点")
        log("  用例37 PASS ✓（同 IOA 跨实例并存，既有实例无损）")


    finally:
        recv.stop()
def _influx_base_checks(cfg, reply):
    svr = find_inst(cfg, "c4_asfp2_server", port=P_RECV1)
    if not svr or len(svr.get("points", [])) != 10:
        raise Fail(f"asfp2 接收实例(19001,10点)异常: {bool(svr)}")
    if [k for (k, _i) in server_instances(cfg) if k == "c4_asfp2_client"]:
        raise Fail("出现了 asfp2 转发实例（用户声明的是入库不是外转）")
    inf = find_inst(cfg, "c4_influxdb_client")
    if not inf or len(inf.get("points", [])) != 10:
        raise Fail(f"influxdb 实例(10点)异常: {bool(inf)}")
    url = str(inf.get("url") or "")
    if "127.0.0.1:18086" not in url:
        raise Fail(f"写入地址未采纳: {url}")
    if inf.get("org") != "activesys":
        raise Fail(f"org 未采纳: {inf.get('org')}")
    return inf


def case38():
    c = Conv()
    text, confirmed = send_flow(c, "38", MSG38)
    if not confirmed:
        raise Fail(f"未进入确认流程: {text[:200]}")

    def check(cfg):
        inf = find_inst(cfg, "c4_influxdb_client")
        return bool(inf) and len(inf.get("points", [])) == 10
    cfg = wait_config(check, timeout=240, desc="asfp2 接收 + influxdb 入库实例")
    inf = _influx_base_checks(cfg, text)
    if inf.get("bucket") != "hnals":
        raise Fail(f"bucket 未采纳: {inf.get('bucket')}")
    wait_port(P_RECV1, True)
    inject(P_RECV1, 1000, 1010, times=3)
    svr = find_inst(read_config(), "c4_asfp2_server", port=P_RECV1)
    ids = {p["shm_id"] for p in svr["points"]}
    by_field = {p.get("field") or p.get("id"): p["shm_id"] for p in svr["points"]}
    deadline = time.time() + 120
    consistent = False
    while time.time() < deadline:
        q = influx_query("hnals")
        shm = shm_values(ids, timeout=10) if set(INFLUX_FIELDS) <= set(q) else {}
        if set(INFLUX_FIELDS) <= set(q) and len(shm) == len(ids):
            if all(abs(shm[sid] - q[f]) <= 0.05 + 1e-3 * abs(q[f])
                   for f, sid in by_field.items() if sid in shm):
                consistent = True
                break
        time.sleep(3)
    if not consistent:
        raise Fail(f"入库字段与 shm 始终无法对点一致: influx={sorted(influx_query('hnals'))}")
    log("  用例38 PASS ✓（入库 10 字段与 shm 对点一致）")


def case39():
    c = Conv()
    try:
        text, confirmed = send_flow(c, "39", MSG39)
        if not confirmed:
            raise Fail(f"未进入确认流程: {text[:200]}")

        def check(cfg):
            inf = find_inst(cfg, "c4_influxdb_client")
            return bool(inf) and len(inf.get("points", [])) == 10
        cfg = wait_config(check, timeout=240, desc="询问 bucket 后入库实例")
        inf = _influx_base_checks(cfg, text)
        if inf.get("bucket") != "hnals":
            raise Fail(f"补充的 bucket 未采纳: {inf.get('bucket')}")
        log("  用例39 PASS ✓（bucket 缺失询问后接入）")


    finally:
        pass

def case40():
    stop_influxd()
    log("  InfluxDB 已停止（模拟目标不可达）")
    try:
        c = Conv()
        text, confirmed = send_flow(c, "40", MSG38)
        if not confirmed:
            raise Fail(f"未进入确认流程: {text[:200]}")

        def check(cfg):
            inf = find_inst(cfg, "c4_influxdb_client")
            return bool(inf) and len(inf.get("points", [])) == 10
        cfg = wait_config(check, timeout=240, desc="不可达下仍完成接入")
        _influx_base_checks(cfg, text)
        wait_port(P_RECV1, True)
        bad = []
        if re.search(r"已入库|入库成功", text):
            bad.append("已入库")
        if "全部写入" in text:
            bad.append("全部写入")
        if re.search(r"已写入", text) and "配置已写入" not in text:
            bad.append("已写入")
        if bad:
            raise Fail(f"谎报数据已入库（{'/'.join(bad)}）: {text[:200]}")
        log(f"  接入完成且未谎报（回复片段: {text[:120]}）")
        start_influxd()
        log("  InfluxDB 已恢复")
        inject(P_RECV1, 1000, 1010, times=3)
        influx_wait("hnals", INFLUX_FIELDS)
        log("  用例40 PASS ✓（start 成功不谎报，恢复后新数据续写可见）")
    except Exception:
        if not _pids("influxd -config"):
            start_influxd()
        raise


def case41():
    before = read_config()
    c = Conv()
    text = c.send(MSG41)
    wait_idle()
    after = read_config()
    if json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True):
        raise Fail(f"config 被修改（回复: {text[:150]}）")
    if find_inst(after, "c4_influxdb_client", bucket="wind_anomaly") is not None:
        raise Fail("同实例重复引用被写入 config")
    if not re.search(r"重复|拒绝|已(经)?(被)?(引用|存在)|无法|不能", text):
        raise Fail(f"未可读拒绝: {text[:200]}")
    log(f"  用例41 PASS ✓（shm_id 重复引用可读拒绝，config 不变）")



def case42():
    stop_modbusd51()
    log("  变桨控制器已下电（模拟设备不可达）")
    try:
        c = Conv()
        text, confirmed = send_flow(c, "42", MSG30)
        if not confirmed:
            raise Fail(f"未进入确认流程: {text[:200]}")

        def check(cfg):
            m = find_inst(cfg, "c4_modbus_client", ip="192.168.110.51", port=502)
            return bool(m) and len(m.get("points", [])) == 10 and forward_of(cfg, 5000) is not None
        cfg = wait_config(check, timeout=240, desc="设备不可达下仍完成接入")
        if re.search(r"已采集|数据正常|已连上|读取到", text):
            raise Fail(f"谎报数据已采集: {text[:200]}")
        log(f"  接入完成且未谎报（回复片段: {text[:120]}）")
        start_modbusd51()
        log("  变桨控制器已上电")
        m = find_inst(read_config(), "c4_modbus_client", ip="192.168.110.51", port=502)
        points_values(cfg, "c4_modbus_client", m, MB30_VALS, timeout=90, desc="恢复后对点")
        log("  用例42 PASS ✓（start 成功不谎报，设备上电后数据出现）")
    except Exception:
        if not _pids("modbusd -c /tmp/c4_env/modbusd.json$"):
            start_modbusd51()  # 保证环境恢复
        raise


CASES = {
    "prereq": prereq, "16": case16, "17": case17, "18": case18, "19": case19,
    "20": case20, "21": case21, "22": case22, "23": case23, "24": case24,
    "25": case25, "26": case26, "27": case27, "28": case28, "29": case29,
    "10": case10,
}

CASES.update({
    "reset": case_reset,
    "30": case30, "31": case31, "32": case32, "33": case33,
    "34": case34, "35": case35, "36": case36, "37": case37,
    "38": case38, "39": case39, "40": case40, "41": case41,
    "42": case42,
})


SEQUENCE = ["prereq", "16", "17", "10", "18", "19", "20",
            "21", "25", "22", "23", "24", "27", "28", "25", "26", "29"]

SEQUENCE2 = ["reset", "30", "31", "32", "33", "34", "35", "36", "37",
             "38", "41", "reset", "39", "reset", "40", "reset", "42"]


def run_sequence(sequence):
    results = []
    for c in sequence:
        t0 = time.time()
        log(f"════ 用例 {c} 开始 ════")
        PH.reset(c, BUTTON_BUDGET.get(c, 2))
        try:
            MCP_STACK.up()
            if not AGENT.p:
                AGENT.up()
            CASES[c]()
            vio, notes = PH.verdict()
            if vio:
                results.append((c, False, "过程断言: " + "; ".join(vio)))
                log(f"════ 用例 {c} FAIL（过程断言）: {'; '.join(vio)} ════")
            else:
                results.append((c, True, 0.0))
                tail = f"｜{'；'.join(notes)}" if notes else ""
                log(f"════ 用例 {c} PASS（{time.time()-t0:.0f}s）{tail} ════")
        except Fail as e:
            msg = str(e)
            vio, _ = PH.verdict()
            if vio:
                msg += " ｜ 过程断言: " + "; ".join(vio)
            results.append((c, False, msg))
            log(f"════ 用例 {c} FAIL: {msg} ════")
        except Exception as e:
            results.append((c, False, f"{type(e).__name__}: {e}"))
            log(f"════ 用例 {c} FAIL（异常）: {type(e).__name__}: {e} ════")
        time.sleep(1)
    return results


def report(results):
    npass = sum(1 for _, ok, _ in results if ok)
    log("════ 全量结果 ════")
    for c, ok, err in results:
        log(f"  {c}: {'PASS' if ok else 'FAIL ' + str(err)[:120]}")
    log(f"  合计 {npass}/{len(results)} 通过")
    sys.exit(0 if npass == len(results) else 1)


def main():
    case = sys.argv[1] if len(sys.argv) > 1 else ""
    if case == "all":
        report(run_sequence(SEQUENCE))
    if case == "new":
        report(run_sequence(SEQUENCE2))
    if case not in CASES:
        print(f"未知用例: {case}；可选: {' '.join(CASES)} | all", flush=True)
        sys.exit(2)
    t0 = time.time()
    log(f"════ 用例 {case} 开始 ════")
    PH.reset(case, BUTTON_BUDGET.get(case, 2))
    try:
        MCP_STACK.up()
        if not AGENT.p:
            AGENT.up()
        CASES[case]()
        vio, notes = PH.verdict()
        if vio:
            log(f"════ 用例 {case} FAIL（过程断言）: {'; '.join(vio)} ════")
            sys.exit(1)
        tail = f"｜{'；'.join(notes)}" if notes else ""
        log(f"════ 用例 {case} PASS（{time.time()-t0:.0f}s）{tail} ════")
        sys.exit(0)
    except Fail as e:
        vio, _ = PH.verdict()
        extra = " ｜ 过程断言: " + "; ".join(vio) if vio else ""
        log(f"════ 用例 {case} FAIL: {e}{extra} ════")
        sys.exit(1)
    except Exception as e:
        log(f"════ 用例 {case} FAIL（异常）: {type(e).__name__}: {str(e)[:150]} ════")
        sys.exit(1)
    except KeyboardInterrupt:
        AGENT.stop()
        sys.exit(130)


if __name__ == "__main__":
    try:
        main()
    finally:
        pass
