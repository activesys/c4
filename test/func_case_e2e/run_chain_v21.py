#!/usr/bin/env python3
# func_test_case.md A.0 主链串行驱动器（v2.1.0 命名体系）
# 链序：52 → 删1号(借26) → 2 → 3 → 删1号(借26) → 1 → 20(补答5010=16的操作) → 17 → 18
#       → 19 → 21 → 27 → 25 → 26 → 复接入(26③) → 28
# 规则（用户指令）：逐项串行、不并行；任一链步 FAIL 立即停止全链、落盘诊断、不修改。
# 断言口径：func_test_case.md v2.1.0 —— channel{N} 句柄、wt1_* 点 key、channelX.wt1_* 转发
# 引用键、abbr_registry.json 注册表、site 首绑、对话/方案文本不得出现 channel 与「通道」。
# 主链句柄为接续水位（52 占 1/2 → 1 得 5/6 → 21 得 7/8 → 复接入得 9/10），链上精确断言。
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_cases as rc  # noqa: E402

P_RECV1, P_RECV2, P_FWD1, P_FWD2 = rc.P_RECV1, rc.P_RECV2, rc.P_FWD1, rc.P_FWD2
CHAIN_FAIL_DIR = "/tmp/c4_e2e_chain_fail"
PASSED = []          # 命中的 func_test_case.md 用例号（用于标注 AI测试通过 C4He1）
SETUP_ONLY = {"借26-1", "借26-2"}   # 纯清场链步，不对应独立用例

MSG1 = rc.MSG_CASE1
MSG2 = MSG1.replace("现在需要接入1号风机的数据",
                    "现在需要接入华能阿拉善风电场1号风机的数据")
MSG3 = MSG1.replace("现在需要接入1号风机的数据",
                    "现在需要接入华能通辽风电场1号风机的数据")
MSG20 = "给1#风机增加一个数据点，地址2010:振动。"
MSG17 = "删除1#风机的塔筒温度点（地址1006）。"
MSG18 = "给1#风机增加一个数据点，地址1000:振动，转发地址5010。"
MSG19 = "删除1#风机的地址3000的点。"
MSG21 = (rc.MSG_WT2_BODY
         + "转发到II区服务器127.0.0.1:9901，转发采用asfp2协议，点表6000~6009。")
MSG27 = "删除5号风机。"
MSG25 = "删除2号风机。"
MSG26 = "删除1号风机。"
MSG28 = "把风机都删了。"


# ── 无 site 的隔离 agent（52 首绑流程的被测前提）──────────────
class V21Agent(rc.Agent):
    """agent.json 不含 site 字段——场站绑定走首接询问流程（用例 52 被测行为）。"""

    def up(self):
        subprocess.run(["fuser", "-k", "19720/tcp"], capture_output=True, timeout=5)
        time.sleep(1)
        self.dir = rc.AGENT_DIR
        os.makedirs(self.dir, exist_ok=True)
        agent_json = {
            "instance_id": "c4_e2e",
            "model": {
                "provider": "zhipu",
                "name": "glm-4.5-air",
                "thinking": "disabled",
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "temperature": 0,
                "max_tokens": 4096,
                "api_key_env": "ZHIPU_API_KEY",
            },
            "server": {"host": "127.0.0.1", "port": 19720, "cors_origin": "*"},
            "mcp_registry": {"path": rc.REGISTRY_DIR},
            "shm_manager": {
                "binary": rc.SHM_BINARY,
                "config_path": os.path.join(self.dir, "config.json"),
            },
            "state": {"backend": "filesystem", "path": os.path.join(self.dir, "state")},
            "logging": {"level": "info", "dir": os.path.join(self.dir, "logs")},
        }
        with open(os.path.join(self.dir, "agent.json"), "w", encoding="utf-8") as f:
            json.dump(agent_json, f, ensure_ascii=False, indent=2)
        env = dict(os.environ)
        env["ZHIPU_API_KEY"] = rc.load_api_key()
        env["C4_SOCK_DIR"] = rc.SOCK_DIR
        self.f = open(f"/tmp/e2e_agent_{time.strftime('%H%M%S')}.log", "w")
        self.p = subprocess.Popen(
            ["node", rc.AGENT_JS, "--config-dir", self.dir],
            stdout=self.f, stderr=subprocess.STDOUT, env=env)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(rc.BASE + "/api/state", timeout=3) as r:
                    json.loads(r.read().decode())
                rc.log("  无site隔离 agent 就绪（:19720, instance=c4_e2e, site 未绑定）")
                return
            except Exception:
                time.sleep(0.5)
        raise rc.Fail("隔离 agent 60s 未就绪（site 缺失导致启动失败？见 agent 日志）")


def _kill_orphan_stack(sig: int) -> None:
    """按 environ 精确匹配并杀掉测试栈 MCP 进程（sig=SIGTERM/SIGKILL 两段清扫）。

    孤儿栈防护（2026-10-02 链步52 数据面断链实测）：McpStack.up() 对存活 unix
    socket 直接复用，上一驱动进程退出后遗留的常驻 MCP 进程即孤儿——本函数清盘
    config/registry/shm 后其运行态（19001 端口监听、shm mmap）全部陈旧，数据面
    断言必然失败。匹配依据 = 二进制路径 + environ C4_SOCK_DIR 指向测试 sock 目录；
    生产栈（c4 用户、/home/c4/.local/c4）env 不同，不受影响。
    """
    binaries = tuple(f"/usr/local/bin/{svc}" for svc in rc.ALL_MCP_SERVICES)
    marker = f"C4_SOCK_DIR={rc.SOCK_DIR}"
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                cmd = f.read().decode("utf-8", "replace")
            if not any(b in cmd for b in binaries):
                continue
            with open(f"/proc/{entry}/environ", "rb") as f:
                env = f.read().decode("utf-8", "replace")
            if marker in env:
                os.kill(int(entry), sig)
        except (OSError, ValueError):
            continue


def chain_clean():
    """链首清场（等价 func_test_case.md 附录 A.2 的隔离栈形态）。"""
    subprocess.run(["fuser", "-k", "19720/tcp"], capture_output=True, timeout=5)
    time.sleep(1)
    rc.MCP_STACK.stop_instances()
    _kill_orphan_stack(signal.SIGTERM)
    time.sleep(1.5)
    _kill_orphan_stack(signal.SIGKILL)
    time.sleep(0.5)
    d = rc.AGENT_DIR
    for name in ("config.json", "abbr_registry.json", "pending_change.json",
                 "config.json.prev.1", "config.json.prev.2", "config.json.prev.3"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            os.remove(p)
    shutil.rmtree(os.path.join(d, "state"), ignore_errors=True)
    shutil.rmtree(os.path.join(d, "logs"), ignore_errors=True)
    subprocess.run(["rm", "-f", "/dev/shm/c4_e2e"], timeout=5)


# ── v2.1.0 断言助手 ─────────────────────────────────────────
def registry():
    p = os.path.join(rc.AGENT.dir, "abbr_registry.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def site_of_agent_json():
    p = os.path.join(rc.AGENT.dir, "agent.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("site")
    except (OSError, json.JSONDecodeError):
        return None


def assert_channel_ids(cfg):
    bad = [f"{st}/{iid}" for (st, iid) in rc.server_instances(cfg)
           if not re.fullmatch(r"channel\d+", str(iid))]
    if bad:
        raise rc.Fail(f"实例 id 非 channel{{N}} 句柄: {bad}")


def writer_points(cfg, wid=None):
    wid = wid or rc.writer_of(cfg, 1000)
    return rc.points_of(cfg, "c4_asfp2_server", wid), wid


def assert_writer_keys(cfg, prefix, addrs):
    # 锚点取断言点表的首地址：s21 校验 wt2（首点 1100）时写死 1000 会错拿
    # 1号风机实例（2026-10-02 链步21 误报实测：channel7 上 wt2_* 全部正确，
    # 断言却对 channel5 校验 1100~1109）
    w, wid = writer_points(cfg, rc.writer_of(cfg, addrs[0]))
    for a in addrs:
        pid = str(w.get(a, {}).get("id", ""))
        if not pid.startswith(prefix + "_"):
            raise rc.Fail(f"writer addr={a} 点 key={pid!r} 不以前缀 {prefix}_ 开头（实例 {wid}）")
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", pid):
            raise rc.Fail(f"writer addr={a} 点 key 非法: {pid!r}")
    return w, wid


def assert_forward_key(cfg, fwd_addr, wid, writer_addr):
    fid = rc.forward_of(cfg, fwd_addr)
    if fid is None:
        raise rc.Fail(f"转发地址 {fwd_addr} 无 Reader 实例")
    fp = rc.points_of(cfg, "c4_asfp2_client", fid).get(fwd_addr, {})
    wpid = str(rc.points_of(cfg, "c4_asfp2_server", wid).get(writer_addr, {}).get("id", ""))
    expect = f"{wid}.{wpid}"
    if fp.get("key") != expect:
        raise rc.Fail(f"转发引用 key={fp.get('key')!r} ≠ 全局键 {expect!r}（Reader 实例 {fid}）")
    if "name" in fp:
        raise rc.Fail(f"转发点含 name 字段（冗余落盘）: {fp}")
    return fid


def assert_no_handle_leak(step):
    for e in rc.PH.entries:
        if e.get("kind") != "assistant":
            continue
        t = e.get("text") or ""
        if re.search(r"channel", t):
            raise rc.Fail(f"对话文本泄漏实例句柄 channel（链步 {step}）: "
                          f"{[m for m in re.findall(r'.{20}channel.{20}', t)][:1]}")
        if "通道" in t:
            raise rc.Fail(f"对话文本出现「通道」概念（链步 {step}）: "
                          f"{[m for m in re.findall(r'.{15}通道.{15}', t)][:1]}")


def entry(prefix, name=None, host=None):
    reg = registry()
    if not reg or not isinstance(reg.get("entries"), list):
        raise rc.Fail(f"注册表缺失或无 entries: {json.dumps(reg, ensure_ascii=False)[:150]}")
    for e in reg["entries"]:
        if e.get("prefix") == prefix and (name is None or e.get("name") == name):
            if host is not None and e.get("host") != host:
                raise rc.Fail(f"注册表 {prefix} host={e.get('host')} ≠ 预期 {host}")
            return e
    raise rc.Fail(f"注册表无 prefix={prefix} 条目: {json.dumps(reg, ensure_ascii=False)[:200]}")


def data_smoke(fwd_proc, recv_port, begin, end):
    """数据面烟囱：注入 → 等转发端收到数据（ func_test_case 用例1 数据链路）。"""
    rc.inject(recv_port, begin, end, times=3)
    deadline = time.time() + 20
    while time.time() < deadline:
        if fwd_proc.out().strip():
            return
        time.sleep(1)
    raise rc.Fail(f"数据面断链：注入 {begin}~{end} 后 →{recv_port} 转发端 {20}s 无数据")


# ── 驱动流（一次性应答 + 按钮确认 + 完成谓词/实例数变化/失败信号即停）────
def flow(conv, message, answers=(), done=None, max_turns=12, stop_on=()):
    def icount(cfg):
        if not isinstance(cfg, dict):
            return 0
        return sum(len(v) for k, v in cfg.items()
                   if isinstance(v, list) and k != "c4_shm_manager")
    # 预期失败信号（如「已恢复原样」回滚汇报）：命中即交还调用方断言后续状态。
    # 失败回滚到空态时实例数回到初值，「实例数变化」早退对此路径天然失效，
    # 且失败文案含「方案已保留…确认」会误触自动点击——必须先于点击检查
    def hit_stop(t):
        return any(re.search(p, t) for p in stop_on)
    n0 = icount(rc.read_config())
    consumed = [False] * len(answers)
    text = conv.send(message)
    clicked = 0
    for _ in range(max_turns):
        if done is not None and done():
            return text, clicked
        if icount(rc.read_config()) != n0:
            return text, clicked          # 执行落地（实例数变化）
        if hit_stop(text):
            return text, clicked          # 预期失败汇报
        if clicked < 2 and ("是否确认" in text or "确认执行" in text or "确认后我将" in text
                            or ("方案" in text and "确认" in text)
                            or ("方案" in text and "是否" in text)):
            text = conv.send("[C4_BUTTON_CONFIRM] 确认")
            clicked += 1
            if hit_stop(text):
                return text, clicked      # 失败汇报先于 wait_idle，避免 90s 空等
            rc.wait_idle()
            continue
        hit = False
        for i, (pat, ans) in enumerate(answers):
            if not consumed[i] and re.search(pat, text):
                consumed[i] = True
                text = conv.send(ans)
                hit = True
                break
        if hit:
            continue
        time.sleep(10)
        text = conv.send("继续")
    raise rc.Fail(f"flow 超过 {max_turns} 轮未收敛，尾回复: {text}")


# ── 链步实现 ───────────────────────────────────────────────
def s52():
    """用例 52+1：无 site 全新环境首接——场站询问、缩写固化、接入、注册表、句柄与 key。"""
    if site_of_agent_json() is not None:
        raise rc.Fail("前置错误：agent.json 已含 site（应无 site 启动）")
    # 接收器先于配置执行拉起（对齐 s21 编排）：转发实例随配置生效立即首连目标，
    # 首连 refused 后按 30s 退避重试——接收器若晚于首连才拉起（曾放在
    # wait_port 之后），data_smoke 的 20s 断言窗口必然落空
    #（2026-10-02 链步52 数据面断链实测：首连 19:45:03 refused → 重试 19:45:33）
    fwd = rc.start_receiver(P_FWD1)
    try:
        conv = rc.Conv()
        text, _ = flow(conv, MSG1, answers=[(r"场站", "华能阿拉善")])
        first = next((t for t in _assistant_texts() if t.strip()), "")
        if not re.search(r"场站", first):
            raise rc.Fail(f"首次接入未先询问场站名称（52①），首轮回复: {first[:150]}")
        site = site_of_agent_json()
        if not isinstance(site, dict) or site.get("name") != "华能阿拉善":
            raise rc.Fail(f"site 未固化或名称不符: {site}")
        if not re.fullmatch(r"[a-z]+", str(site.get("abbr", ""))):
            raise rc.Fail(f"拼音缩写未生成或非法: {site}")
        cfg = rc.wait_config(lambda c: rc.writer_of(c, 1000) is not None
                             and rc.forward_of(c, 5000) is not None,
                             timeout=240, desc="1号风机 writer+转发实例（52 首接）")
        assert_channel_ids(cfg)
        w, wid = assert_writer_keys(cfg, "wt1", range(1000, 1010))
        if w.get(1000, {}).get("name") != "风速":
            raise rc.Fail(f"采集点 name 未原样落盘: {w.get(1000)}")
        assert_forward_key(cfg, 5000, wid, 1000)
        e = entry("wt1", name="1号风机", host=wid)
        if not isinstance(e.get("pointMap"), dict) or "风速" not in e["pointMap"]:
            raise rc.Fail(f"pointMap 未登记「风速」: {e}")
        reg = registry()
        if reg.get("channelHighWatermark") != 2:
            raise rc.Fail(f"水位={reg.get('channelHighWatermark')} ≠ 2（52 占 1/2）")
        rc.wait_port(P_RECV1, True)
        data_smoke(fwd, P_RECV1, 1000, 1010)
    finally:
        fwd.stop()
    assert_no_handle_leak("52")


def s_borrow26(tag):
    """借 26 输入删除 1号风机（清场步）：空态 + 注册表清空 + 端口释放 + 水位不回退。"""
    wm_before = (registry() or {}).get("channelHighWatermark")
    conv = rc.Conv()
    text, clicked = flow(conv, MSG26,
                         done=lambda: not [k for k in rc.server_instances(rc.read_config())
                                           if k[0] in ("c4_asfp2_server", "c4_asfp2_client")])
    if clicked == 0 and not re.search(r"删除|确认", text):
        raise rc.Fail(f"删除流程未进入: {text}")
    rc.wait_config(lambda c: not [k for k in rc.server_instances(c)
                                  if k[0] in ("c4_asfp2_server", "c4_asfp2_client")],
                   timeout=180, desc=f"{tag}: 全部实例清空")
    rc.wait_port(P_RECV1, False)
    reg = registry()
    if (reg or {}).get("entries") not in ([], None):
        raise rc.Fail(f"删空后注册表残留: {json.dumps(reg, ensure_ascii=False)[:150]}")
    wm_after = (registry() or {}).get("channelHighWatermark")
    if wm_before is not None and wm_after != wm_before:
        raise rc.Fail(f"水位回退 {wm_before}→{wm_after}")


def s2():
    """用例 2：场站一致直接接入——不得再询问场站。"""
    conv = rc.Conv()
    flow(conv, MSG2, done=lambda: rc.writer_of(rc.read_config(), 1000) is not None)
    cfg = rc.wait_config(lambda c: rc.writer_of(c, 1000) is not None
                         and rc.forward_of(c, 5000) is not None,
                         timeout=240, desc="2: 1号风机再次接入")
    assert_channel_ids(cfg)
    assert_writer_keys(cfg, "wt1", range(1000, 1010))
    dialog = "".join(_assistant_texts())
    if re.search(r"请提供.{0,12}场站|场站名称|站点名称", dialog):
        raise rc.Fail("场站已绑定仍询问场站（2②）")
    assert_no_handle_leak("2")


def s3():
    """用例 3：异场站拒绝——config 完全不变。"""
    before = rc.read_config()
    conv = rc.Conv()
    text = conv.send(MSG3)
    time.sleep(3)
    after = rc.read_config()
    if json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True):
        raise rc.Fail("3: 异场站消息改变了 config")
    if not re.search(r"不属于当前场站", text):
        raise rc.Fail(f"3: 未回复「不属于当前场站」: {text}")


def s1():
    """用例 1：第三次全新接入（句柄接续 5/6——永不复用断言）。"""
    conv = rc.Conv()
    flow(conv, MSG1, done=lambda: rc.writer_of(rc.read_config(), 1000) is not None)
    cfg = rc.wait_config(lambda c: rc.writer_of(c, 1000) is not None
                         and rc.forward_of(c, 5000) is not None,
                         timeout=240, desc="1: 1号风机全新接入")
    assert_channel_ids(cfg)
    wid = rc.writer_of(cfg, 1000)
    fid = rc.forward_of(cfg, 5000)
    if (wid, fid) != ("channel5", "channel6"):
        raise rc.Fail(f"1: 句柄 {wid}/{fid} ≠ 接续水位预期 channel5/channel6（永不复用）")
    assert_writer_keys(cfg, "wt1", range(1000, 1010))
    assert_forward_key(cfg, 5000, wid, 1000)
    entry("wt1", name="1号风机", host=wid)
    reg = registry()
    if reg.get("channelHighWatermark") != 6:
        raise rc.Fail(f"1: 水位={reg.get('channelHighWatermark')} ≠ 6")
    assert_no_handle_leak("1")


def s20_16():
    """用例 20+16：加点询问转发地址 → 补答 5010 → 双侧成对（点 key wt1_*、引用键接续）。"""
    conv = rc.Conv()
    text = conv.send(MSG20)
    if not re.search(r"转发|5010", text):
        raise rc.Fail(f"20: 未询问转发地址即推进: {text}")
    before = rc.snapshot_w1(rc.read_config())
    text, _ = flow(conv, "转发地址5010",
                   done=lambda: rc.writer_of(rc.read_config(), 2010) is not None)
    def check(cfg):
        wid = rc.writer_of(cfg, 1000)
        if wid is None or 2010 not in rc.points_of(cfg, "c4_asfp2_server", wid):
            return False
        return rc.forward_of(cfg, 5010) is not None
    cfg = rc.wait_config(check, timeout=240, desc="16: 2010/5010 成对新增")
    assert_channel_ids(cfg)
    w, wid = assert_writer_keys(cfg, "wt1", list(range(1000, 1010)) + [2010])
    if str(w[2010]["id"]) == str(w[1000]["id"]):
        raise rc.Fail("16: 新点与 addr=1000 撞 key")
    assert_forward_key(cfg, 5010, wid, 2010)
    entry("wt1", host=wid)
    pm = entry("wt1").get("pointMap", {})
    if "振动" not in pm:
        raise rc.Fail(f"16: pointMap 未登记「振动」: {pm}")
    after = rc.snapshot_w1(cfg)
    for addr, sid in before.items():
        if after.get(addr) != sid:
            raise rc.Fail(f"16: 既有点 addr={addr} shm_id 被重排")
    if after.get(2010) in (None, 0):
        raise rc.Fail(f"16: 新点 shm_id 异常: {after.get(2010)}")
    rc.wait_port(P_RECV1, True)
    assert_no_handle_leak("16/20")


def s17():
    """用例 17：删点——双侧成对删除、其余 shm_id 不变。"""
    before = rc.snapshot_w1(rc.read_config())
    if 1006 not in before:
        raise rc.Fail("17: 前置缺 addr=1006")
    conv = rc.Conv()
    text, clicked = flow(conv, MSG17,
                         done=lambda: rc.writer_of(rc.read_config(), 1006) is None)
    def check(cfg):
        wid = rc.writer_of(cfg, 1000)
        if wid is None:
            return False
        w = rc.points_of(cfg, "c4_asfp2_server", wid)
        fid = rc.forward_of(cfg, 5000)
        f = rc.points_of(cfg, "c4_asfp2_client", fid) if fid else {}
        return 1006 not in w and 5006 not in f
    cfg = rc.wait_config(check, timeout=180, desc="17: 1006/5006 成对删除")
    after = rc.snapshot_w1(cfg)
    for addr, sid in before.items():
        if addr != 1006 and after.get(addr) != sid:
            raise rc.Fail(f"17: 未删点 addr={addr} shm_id 被重排")
    rc.wait_port(P_RECV1, True)
    assert_no_handle_leak("17")


def s18():
    """用例 18：三重冲突拒绝——核心断言 = 拒绝 + config 不变 + 不重启。"""
    before = rc.read_config()
    conv = rc.Conv()
    text = conv.send(MSG18)
    time.sleep(5)
    after = rc.read_config()
    if json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True):
        raise rc.Fail(f"18: 冲突请求写入了 config（回复: {text}）")
    if re.search(r"是否确认|确认执行", text):
        raise rc.Fail(f"18: 冲突请求进入了可确认方案（应方案期拒绝）: {text}")
    if not rc.listening(P_RECV1):
        raise rc.Fail("18: 19001 监听丢失（服务被重启）")
    if not re.search(r"已被占用|重复|重名|冲突|已存在|无法|不能|拒绝", text):
        raise rc.Fail(f"18: 无可读拒绝信号: {text}")


def s19():
    """用例 19：删除不存在的点——拒绝 + config 不变 + 不得出可确认方案。"""
    before = rc.read_config()
    conv = rc.Conv()
    text = conv.send(MSG19)
    time.sleep(3)
    if json.dumps(before, sort_keys=True) != json.dumps(rc.read_config(), sort_keys=True):
        raise rc.Fail("19: config 被修改")
    if re.search(r"是否确认|确认执行", text):
        raise rc.Fail(f"19: 不存在的点进入了可确认方案: {text}")
    if not re.search(r"不存在|失败|没有", text):
        raise rc.Fail(f"19: 未指明点不存在: {text}")


def s21():
    """用例 21：2号风机独占接入（9002→9901）——wt2_* key、4 实例并存、注册表两条目。"""
    fwd = rc.start_receiver(P_FWD2)
    try:
        conv = rc.Conv()
        flow(conv, MSG21, done=lambda: rc.writer_of(rc.read_config(), 1100) is not None)
        cfg = rc.wait_config(lambda c: rc.writer_of(c, 1100) is not None
                             and sum(1 for k in rc.server_instances(c)
                                     if k[0] == "c4_asfp2_client") >= 2,
                             timeout=240, desc="21: 2号风机 writer+第二转发实例")
        assert_channel_ids(cfg)
        wid2 = rc.writer_of(cfg, 1100)
        rc.points_of(cfg, "c4_asfp2_server", wid2)
        assert_writer_keys(cfg, "wt2", range(1100, 1110))
        fid2 = rc.forward_of(cfg, 6000)
        fp = rc.points_of(cfg, "c4_asfp2_client", fid2)[6000]
        wpid = str(rc.points_of(cfg, "c4_asfp2_server", wid2)[1100]["id"])
        if fp.get("key") != f"{wid2}.{wpid}":
            raise rc.Fail(f"21: 2号转发引用 key={fp.get('key')!r} ≠ {wid2}.{wpid!r}")
        n_srv = sum(1 for k in rc.server_instances(cfg) if k[0] == "c4_asfp2_server")
        n_cli = sum(1 for k in rc.server_instances(cfg) if k[0] == "c4_asfp2_client")
        if (n_srv, n_cli) != (2, 2):
            raise rc.Fail(f"21: 实例数 {n_srv}+{n_cli} ≠ 4 并存")
        if not rc.listening(P_RECV1) or not rc.listening(P_RECV2):
            raise rc.Fail("21: 9001/9002 双监听不成立")
        e1 = entry("wt1")
        e2 = entry("wt2", name="2号风机", host=wid2)
        if e1.get("host") == e2.get("host"):
            raise rc.Fail("21: wt1/wt2 宿主相同（9002 应为独占新实例）")
        reg = registry()
        if reg.get("channelHighWatermark") != 8:
            raise rc.Fail(f"21: 水位={reg.get('channelHighWatermark')} ≠ 8")
        rc.wait_port(P_RECV2, True)
        rc.inject(P_RECV2, 1100, 1110, times=3)
        assert_no_handle_leak("21")
    finally:
        fwd.stop()


def s27():
    """用例 27：删除不存在的设备——拒绝 + config/registry 不变。"""
    before = rc.read_config()
    reg_before = registry()
    conv = rc.Conv()
    text = conv.send(MSG27)
    time.sleep(3)
    if json.dumps(before, sort_keys=True) != json.dumps(rc.read_config(), sort_keys=True):
        raise rc.Fail("27: config 被修改")
    if json.dumps(reg_before, sort_keys=True) != json.dumps(registry(), sort_keys=True):
        raise rc.Fail("27: 注册表被修改")
    if not re.search(r"不存在|未接入|从未|没有", text):
        raise rc.Fail(f"27: 未指明设备不存在: {text}")


def s25():
    """用例 25：删除 2号风机（独占形态）——成对删除、端口释放、注册表条目删除。"""
    conv = rc.Conv()
    text, clicked = flow(conv, MSG25,
                         done=lambda: rc.writer_of(rc.read_config(), 1100) is None)
    def check(cfg):
        return (rc.writer_of(cfg, 1100) is None and rc.writer_of(cfg, 1000) is not None
                and rc.forward_of(cfg, 6000) is None and rc.forward_of(cfg, 5000) is not None)
    rc.wait_config(check, timeout=180, desc="25: 2号成对删除且 1号保留")
    rc.wait_port(P_RECV2, False)
    rc.wait_port(P_RECV1, True)
    entry("wt1")
    reg = registry()
    names = [e.get("prefix") for e in reg.get("entries", [])]
    if "wt2" in names:
        raise rc.Fail("25: 注册表 wt2 条目未删除")
    if reg.get("channelHighWatermark") != 8:
        raise rc.Fail(f"25: 水位回退 → {reg.get('channelHighWatermark')}")


def s26():
    """用例 26：删最后一台——合法空态、条目清空、水位保持。"""
    conv = rc.Conv()
    flow(conv, MSG26,
         done=lambda: not [k for k in rc.server_instances(rc.read_config())
                           if k[0] in ("c4_asfp2_server", "c4_asfp2_client")])
    rc.wait_config(lambda c: not [k for k in rc.server_instances(c)
                                  if k[0] in ("c4_asfp2_server", "c4_asfp2_client")],
                   timeout=180, desc="26: 删至 0 台")
    rc.wait_port(P_RECV1, False)
    reg = registry()
    if (reg or {}).get("entries") != []:
        raise rc.Fail(f"26: 注册表未清空: {json.dumps(reg, ensure_ascii=False)[:150]}")
    if (reg or {}).get("channelHighWatermark") != 8:
        raise rc.Fail(f"26: 水位 ≠ 8（应保持不回退）: {reg}")


def s_reaccess():
    """26③：空态重新接入——句柄接续 9/10（永不复用），完整注册表固化。"""
    conv = rc.Conv()
    flow(conv, MSG1, done=lambda: rc.writer_of(rc.read_config(), 1000) is not None)
    cfg = rc.wait_config(lambda c: rc.writer_of(c, 1000) is not None
                         and rc.forward_of(c, 5000) is not None,
                         timeout=240, desc="26③: 空态重新接入")
    wid = rc.writer_of(cfg, 1000)
    fid = rc.forward_of(cfg, 5000)
    if (wid, fid) != ("channel9", "channel10"):
        raise rc.Fail(f"26③: 句柄 {wid}/{fid} ≠ channel9/channel10（已删序号被复用？）")
    assert_writer_keys(cfg, "wt1", range(1000, 1010))
    assert_forward_key(cfg, 5000, wid, 1000)
    entry("wt1", name="1号风机", host=wid)
    assert_no_handle_leak("26③")


def s28():
    """用例 28：模糊批量删除——列现存设备清单 + 确认后删空（链上仅 1号，折算口径）。"""
    conv = rc.Conv()
    text = conv.send(MSG28)
    if not re.search(r"1号|1#", text):
        raise rc.Fail(f"28: 未列受影响设备清单: {text}")
    if not re.search(r"确认", text):
        raise rc.Fail(f"28: 列清单后未索要确认: {text}")
    conv.send("[C4_BUTTON_CONFIRM] 确认")
    rc.wait_idle()
    rc.wait_config(lambda c: not [k for k in rc.server_instances(c)
                                  if k[0] in ("c4_asfp2_server", "c4_asfp2_client")],
                   timeout=180, desc="28: 确认后删空")
    rc.wait_port(P_RECV1, False)


def _assistant_texts():
    return [e.get("text") or "" for e in rc.PH.entries if e.get("kind") == "assistant"]


# ── 链调度（串行，失败即停）────────────────────────────────
STEPS = [
    ("52",            ["52"],            s52),
    ("借26-1",        SETUP_ONLY,        lambda: s_borrow26("借26-1")),
    ("2",             ["2"],             s2),
    ("3",             ["3"],             s3),
    ("借26-2",        SETUP_ONLY,        lambda: s_borrow26("借26-2")),
    ("1",             ["1"],             s1),
    ("20+16",         ["20", "16"],      s20_16),
    ("17",            ["17"],            s17),
    ("18",            ["18"],            s18),
    ("19",            ["19"],            s19),
    ("21",            ["21"],            s21),
    ("27",            ["27"],            s27),
    ("25",            ["25"],            s25),
    ("26",            ["26"],            s26),
    ("26③复接入",     [],                s_reaccess),
    ("28",            ["28"],            s28),
]


def dump_diag(step, err):
    d = f"{CHAIN_FAIL_DIR}_{step}"
    os.makedirs(d, exist_ok=True)
    try:
        with open(os.path.join(d, "dialog.txt"), "w", encoding="utf-8") as f:
            for e in rc.PH.entries:
                kind = e.get("kind")
                if kind == "user":
                    f.write(f"\n[USER] {e.get('text')}\n")
                elif kind == "assistant":
                    f.write(f"\n[ASSISTANT] tools={sorted(e.get('tools') or [])}\n{e.get('text')}\n")
                elif kind == "event":
                    f.write(f"[EVENT] {e.get('etype')} {e.get('name')}\n")
        for name, obj in (("config.json", rc.read_config()), ("registry.json", registry()),
                          ("agent.json", site_of_agent_json())):
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=1)
        agent_log = max([f"/tmp/{f}" for f in os.listdir("/tmp")
                         if f.startswith("e2e_agent_")], default=None)
        if agent_log:
            with open(agent_log, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-8000:]
            with open(os.path.join(d, "agent_log_tail.txt"), "w", encoding="utf-8") as f:
                f.write(tail)
    except Exception as e:
        rc.log(f"  [warn] 诊断落盘失败: {e}")
    rc.log(f"  诊断已落盘: {d}")


def main():
    rc.log("════ v2.1.0 主链串行测试开始（A.0 主链，16 链步）════")
    chain_clean()
    rc.MCP_STACK.up()
    agent = V21Agent()
    agent.up()
    rc.AGENT = agent          # 让 run_cases 的 read_config/Conv 全部指向本实例
    for label, cases, fn in STEPS:
        rc.log(f"════ 链步 {label} 开始 ════")
        rc.PH.reset(label)
        t0 = time.time()
        try:
            fn()
        except rc.Fail as e:
            dump_diag(label, e)
            rc.log(f"════ 链步 {label} FAIL: {e} ════")
            rc.log(f"════ 主链在第 {label} 步停止（已通过用例: {PASSED or '无'}）════")
            sys.exit(1)
        except Exception as e:
            dump_diag(label, e)
            rc.log(f"════ 链步 {label} 异常 {type(e).__name__}: {str(e)[:200]} ════")
            rc.log(f"════ 主链在第 {label} 步停止（已通过用例: {PASSED or '无'}）════")
            sys.exit(1)
        PASSED.extend(cases)
        rc.log(f"════ 链步 {label} PASS（{time.time()-t0:.0f}s）════")
    rc.log(f"════ 主链全部通过：{PASSED} ════")
    rc.log(f"════ 标注清单（AI测试通过 {CHAIN_TAG}）：{PASSED} ════")


CHAIN_TAG = "C4He1"

if __name__ == "__main__":
    main()
