#!/usr/bin/env python3
# 双 Agent 用例组回归（func_test_case.md 用例 78~99，2026-10-24 设计定稿）
# 链序（组头，五轮评审）：80(结构断言,独立) → 97(空配置段) → A.5 重置 → 78 → 79 →
#   81 → 82 → 83[UI 跳过] → 95 → 84 → 88 → 89 → 90 → 91[UI 跳过] → 96 → 92 →
#   接入2号 → 85 → 86 → 87 → 94 → 98 → 99(托管重启收尾)；93[注入依赖跳过]
# B 级 API 面：【界面】断言（83/91 全部、89⑥ 呈现边界等）不在本驱动范围——人工核对。
# 环境约定：e2e 隔离栈（rc）；agent.json 追加 display.stale_threshold_ms=2000（89 陈旧态）。
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_cases as rc  # noqa: E402
import run_chain_site_change as sc  # noqa: E402  (agent_restart / agent_start)

F = rc.Fail
PTS_1 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "points", "1#风机点表.xlsx")
MSG_UPLOAD = "请解析此文件中的设备信息"
MSG_2HAO = ("接入2号风机，asfp2协议，端口9002，10个点从1000到1009，"
            "10个点分别是1000:风速、1001:功率、1002:风向、1003:桨叶角度、1004:发电机转速、"
            "1005:齿轮箱油温、1006:塔筒温度、1007:空气温度、1008:空气湿度、1009:大气压强，"
            "转发asfp2到127.0.0.1:9900，点表5010~5019。")
SKIPS = []


def skip(tag, why):
    SKIPS.append((tag, why))
    rc.log(f"  [SKIP-{tag}] {why}")


# ── HTTP / SSE（带 agent 回显解析）────────────────────────
def _read_sse_data(resp):
    """解析 SSE，返回 (text, data_dicts, err_texts)——data 字典全量保留（agent 回显用）。"""
    text_parts, datas, errs = [], [], []
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
            if isinstance(d, dict):
                datas.append(d)
                if d.get("type") == "text" and isinstance(d.get("content"), str):
                    text_parts.append(d["content"])
                elif d.get("type") == "error":
                    errs.append(str(d.get("message") or ""))
    return "".join(text_parts), datas, errs


def dchat(msg, history, cid, agent=None, timeout=300):
    """POST /api/chat（可带 agent 字段）；返回 (text, datas, agents_echo)。额度防护同 rc.chat。"""
    payload = {"message": msg, "history": history}
    if cid:
        payload["conversationId"] = cid
    if agent:
        payload["agent"] = agent
    with rc._post("/api/chat", payload, timeout=timeout) as resp:
        text, datas, errs = _read_sse_data(resp)
    if rc._quota_tripped(errs) and rc._switch_model_and_restart():
        with rc._post("/api/chat", payload, timeout=timeout) as resp:
            text, datas, errs = _read_sse_data(resp)
    if rc._quota_tripped(errs):
        raise F("LLM 额度耗尽——dual 链停止")
    if errs:
        # 错误事件是终止性的（路由 error 后 res.end()）——大声失败而非空回复超时
        raise F(f"chat error 事件: {str(errs[0])[:200]}")
    agents = {d.get("agent") for d in datas if d.get("agent") is not None}
    return text, datas, agents


class DConv:
    """双桶会话：access / display 各自 history，发送时只回传目标桶（web.md §3.1.2 分桶）。"""

    def __init__(self, tag="dual"):
        self.cid = f"dual-{uuid.uuid4().hex[:10]}"
        self.h_access, self.h_display = [], []
        self.tag = tag

    def send(self, msg, agent="access"):
        hist = self.h_access if agent == "access" else self.h_display
        m = rc.map_ports(msg)
        rc.log(f"  [{self.tag}/{agent}] >> {m[:90]}")
        hist.append({"role": "user", "content": m})
        text, datas, agents = dchat(m, hist[:-1], self.cid, agent=agent)
        rc.log(f"  [{self.tag}/{agent}] << {text[:160]}")
        if text:
            hist.append({"role": "assistant", "content": text})
        return text, agents


# ── display / cancel REST ─────────────────────────────────
def api_json(path, body=None, timeout=10):
    if body is None:
        req = urllib.request.Request(rc.BASE + path)
    else:
        req = urllib.request.Request(rc.BASE + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except json.JSONDecodeError:
            return e.code, {}


def display_payload(since=None):
    st, obj = api_json("/api/display" + (f"?since={since}" if since is not None else ""))
    if st != 200:
        raise F(f"GET /api/display HTTP {st}")
    return obj


def dcancel():
    st, obj = api_json("/api/cancel", body={})
    if st != 200:
        raise F(f"POST /api/cancel HTTP {st}: {obj}")
    return obj.get("cancelled")


def _display_summary(p):
    """超时诊断载荷：点级 state 计数 + 会话要点（比整包 JSON 截断可读）"""
    s = p.get("session") or p.get("lastSession") or {}
    states = {}
    for q in s.get("points", []):
        states[q.get("state")] = states.get(q.get("state"), 0) + 1
    return json.dumps({"active": p.get("active"), "endedReason": s.get("endedReason"),
                       "tick": s.get("tick"), "mode": s.get("mode"),
                       "intervalMs": s.get("intervalMs"), "n": len(s.get("points", [])),
                       "states": states}, ensure_ascii=False)


def wait_display(pred, timeout=15, gap=0.4, desc=""):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = display_payload()
        if pred(last):
            return last
        time.sleep(gap)
    raise F(f"等待显示状态超时（{desc}）: {_display_summary(last)}")


def stop_display_if_any():
    if display_payload().get("active"):
        api_json("/api/display/stop", body={})
        wait_display(lambda p: not p.get("active"), timeout=8, desc="停止显示")


def recv_count(recv):
    return len(recv.out().splitlines())


def points_keys(flt):
    st, obj = api_json(f"/api/points?filter={urllib.parse.quote(flt)}")
    if st != 200:
        raise F(f"GET /api/points HTTP {st}")
    return obj.get("points", [])


def land_access(conv, msg, answers=(), max_turns=14, tag="land", landed=None):
    """access 侧接入循环：缺口应答 + 确认按钮，直至采集/转发实例落地。
    answers=[(正则,应答)]；landed=落地谓词（缺省：存在 server+client 实例——
    环境已有实例时（如第二台设备接入）必须传针对性谓词，否则首轮即误判落地）。"""
    consumed = [False] * len(answers)
    text, _ = conv.send(msg, "access")
    clicked = 0
    busy = 0
    for _ in range(max_turns):
        cfg = rc.read_config()
        if landed is not None:
            ok = False
            try:
                ok = landed(cfg)
            except Exception:
                ok = False
        else:
            w = [inst for (st, iid), inst in rc.server_instances(cfg).items()
                 if st == "c4_asfp2_server" and iid != "channel0"]
            r = [inst for (st, iid), inst in rc.server_instances(cfg).items() if st == "c4_asfp2_client"]
            ok = bool(w and r)
        if ok:
            return cfg, text
        if "解析失败" in text:
            raise F(f"{tag}: 意外拒收: {text[:240]}")
        if "有配置变更正在执行" in text:
            # 配置通道忙（瀑布收敛窗口）：方案保留、按钮重武装——稍候重发确认
            if busy >= 5:
                raise F(f"{tag}: 配置通道持续忙（重试 {busy} 次未落地）")
            busy += 1
            time.sleep(2)
            text, _ = conv.send("[C4_BUTTON_CONFIRM] 确认", "access")
            continue
        if "当前接入已完成" in text:
            # 已消耗守卫话术但实例不在 config——状态与数据面脱节，立即报错
            #（不再用『继续』空转 14 轮污染会话）
            raise F(f"{tag}: 方案标记已完成但实例未落地: {text[:160]}")
        if clicked < 2 and ("是否确认" in text or "确认执行" in text or "确认后我将" in text
                            or ("接入方案" in text and "确认" in text)):
            text, _ = conv.send("[C4_BUTTON_CONFIRM] 确认", "access")
            clicked += 1
            rc.wait_idle(timeout=600)
            continue
        hit = False
        for i, (pat, ans) in enumerate(answers):
            if not consumed[i] and re.search(pat, text):
                consumed[i] = True
                text, _ = conv.send(ans, "access")
                hit = True
                break
        if hit:
            continue
        text, _ = conv.send("继续", "access")
    raise F(f"{tag}: {max_turns} 轮未落地")


def patch_agent_json():
    """agent.json 追加 display.stale_threshold_ms=2000（89 用例陈旧态 3s 内可达）。"""
    p = os.path.join(rc.AGENT_DIR, "agent.json")
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["display"] = {"stale_threshold_ms": 2000}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    rc.log("  agent.json: display.stale_threshold_ms=2000")


def fresh_env():
    # MCP 常驻栈先于 agent（重）启就位：启动瀑布 L1 等待 shm socket 期间持有
    # 配置单飞锁（上限 300s），栈不在场会让所有 confirm 撞「配置变更正在执行」
    rc.MCP_STACK.up()
    rc.case_reset()
    patch_agent_json()
    sc.agent_restart()


# ══ 用例 ══════════════════════════════════════════════════
def case80():
    """装配面结构断言（P1 劫持防线，A 级 grep + 行为等价）。"""
    with open(rc.AGENT_JS.replace("dist/index.js",
                                  "dist/orchestrator/orchestrator.js"), encoding="utf-8") as f:
        orch = f.read()
    if "handle_display_intent" in orch:
        raise F("80: dist 接入编排器仍含 handle_display_intent（劫持通道复活）")
    tools_p = rc.AGENT_JS.replace("dist/index.js", "dist/display/tools.js")
    with open(tools_p, encoding="utf-8") as f:
        tools = f.read()
    if 'name = "stop_display"' in tools or '"stop_display"' in tools:
        raise F("80: display 工具面仍含 stop_display")
    if "suggest" not in tools:
        raise F("80: list_points 缺 suggest 模式")
    rc.log("  80 结构断言 OK（handle_display_intent 无 / stop_display 无 / suggest 在）")


def case97():
    """甲段：解析流中双击 ESC 等价取消（POST /api/cancel）→ 同名点表重传 → 接入完成。
    乙段（执行回滚）依赖 93 注入，SKIP-97B。"""
    conv = DConv("c97")
    results = {}

    def upload_thread():
        try:
            text, evs, cid = rc.upload_file(conv.cid, PTS_1, MSG_UPLOAD)
            results["text"] = text
        except Exception as e:  # 取消引发的流中断——预期路径之一
            results["aborted"] = str(e)[:120]

    for attempt in range(3):
        results.clear()
        th = threading.Thread(target=upload_thread)
        th.start()
        time.sleep(0.6)
        cancelled = dcancel()
        th.join(timeout=120)
        if cancelled == "turn":
            break
        if th.is_alive():
            th.join(timeout=120)
        time.sleep(1)
    else:
        raise F("97: 三次尝试均未中止在途回合（cancel != turn）")
    rc.log(f"  97 解析中止 OK（cancelled=turn, upload_out={list(results)[:2]}）")
    # 同名重传 + 全量声明 → 落地（同名替换：仅 B 表点位）
    cfg, text = land_access(
        conv, rc.MSG_CASE1,
        answers=[(r"端口|监听", "使用端口9001"), (r"转发|转往|写入", "转发到II区服务器，采用asfp2协议，"
                                                        "目标地址是127.0.0.1:9900，点表5000~5009")],
        tag="97")
    w = [inst for (st, iid), inst in rc.server_instances(cfg).items() if st == "c4_asfp2_server"]
    if not w or len(w[0].get("points", [])) != 10:
        raise F(f"97: 落地点位≠10（同名替换语义）: {[len(i.get('points', [])) for i in w]}")
    skip("97B", "执行回滚子段依赖 93 执行窗口注入（装配留白）")


def case78():
    conv = DConv("c78")
    t1, ag1 = conv.send("你好")
    if ag1 != {"access"}:
        raise F(f"78: 缺省请求 SSE agent 回显≠access: {ag1}")
    t2, ag2 = conv.send("你好", agent="access")
    if ag2 != {"access"}:
        raise F(f"78: 显式 access 回显异常: {ag2}")
    t3, ag3 = conv.send("显示1号风机的风速点", agent="display")
    if ag3 != {"display"}:
        raise F(f"78: display 回显异常: {ag3}")
    if display_payload().get("active"):
        raise F("78: 空环境下 display 回复不应建立显示会话")
    st, obj = api_json("/api/chat", body={"message": "hi", "agent": "foo"})
    if st != 400:
        raise F(f"78: 非法 agent 应 400，得到 {st}")
    rc.log("  78 三态路由 + 非法 400 + display 零副作用 OK")


def case79():
    conv = DConv("c79")
    t, _ = conv.send("接入3号风机，第三方通过asfp2协议转来数据，使用端口9010", "access")
    # display 同 cid：不得复述接入进度（桶隔离）；无已接入点 → 报告没有
    td, _ = conv.send("1号风机有哪些点？现在接入进行到哪了？", "display")
    if "正在接入" in td or "9010" in td or "3号" in td:
        raise F(f"79: display 桶泄漏接入进度: {td[:200]}")
    # access 续走：断点续接（协议/端口产物仍在）→ 补转发 → 落地
    cfg, _ = land_access(
        conv, "转发到II区服务器，采用asfp2协议，目标地址是127.0.0.1:9900，点表7000~7009",
        answers=[(r"点|名称", "1000:风速、1001:功率"), (r"端口|监听", "使用端口9010")],
        tag="79")
    keys = [p["key"] for p in points_keys("3号风机")]
    if not keys:
        raise F("79: display 桶经数据通道看不到 3号风机点位（跨域可见只经 list_points）")
    rc.log(f"  79 分桶 + 断点续接 + 数据通道可见 OK（{len(keys)} 点）")


def case81():
    conv = DConv("c81")
    text, evs, cid = rc.upload_file(conv.cid, PTS_1, MSG_UPLOAD)
    if cid != conv.cid:
        raise F("81: upload 会话 id 未续接")
    td, _ = conv.send("刚才上传的文件里有哪些设备和点？", "display")
    if "风速" in td and "1000" in td:
        # display 桶拿不到 access 桶的文件证据——回复不应列出上传文件明细
        raise F(f"81: display 桶可见 access 上传证据（桶隔离破坏）: {td[:200]}")
    ta, _ = conv.send("刚才上传的点表能用吗？继续用它接入5号风机，asfp2端口9012，"
                      "转发到127.0.0.1:9900，点表7200~7209，10个点从1000到1009，"
                      "点名分别是1000:风速、1001:功率、1002:风向、1003:桨叶角度、1004:发电机转速、"
                      "1005:齿轮箱油温、1006:塔筒温度、1007:空气温度、1008:空气湿度、1009:大气压强",
                      "access")
    rc.log("  81 upload 固定 access + display 桶不可见 OK（明细断言宽松——LLM 措辞不定）")


def case82():
    # ① 收集态：零认领 → 缺口重申，无显示行为
    c1 = DConv("c82a")
    t, _ = c1.send("接入6号风机，asfp2协议，端口9013", "access")
    t, _ = c1.send("显示风速点", "access")
    if "数据展示" in t or "切换" in t:
        raise F(f"82①: 收集态出现切换引导（禁令）: {t[:200]}")
    if display_payload().get("active"):
        raise F("82①: 收集态零认领消息建立了显示会话")
    # ② 武装态：方案重展示（确认仍在）；哨兵负向：方案/按钮不变
    c2 = DConv("c82b")
    t, _ = c2.send(rc.MSG_CASE1, "access")
    if "确认" not in t:
        raise F(f"82②: 未进入方案确认: {t[:200]}")
    t, _ = c2.send("显示风速点", "access")
    if "数据展示" in t or "切换" in t:
        raise F(f"82②: 武装态出现切换引导: {t[:200]}")
    st1 = rc.state()
    t, _ = c2.send("[C4_BUTTON_CANCEL] 取消，不执行", "access")
    if "已取消" in t:
        raise F(f"82②: 废除哨兵仍触发取消: {t[:200]}")
    if display_payload().get("active"):
        raise F("82②: 武装态零认领建立显示")
    # ③ 已消耗态：简短说明，不重装配
    c3 = DConv("c82c")
    land_access(c3, rc.MSG_CASE1,
                answers=[(r"端口|监听", "使用端口9001"),
                         (r"转发|转往|写入", "转发到II区，asfp2到127.0.0.1:9900，点表7400~7409")],
                tag="82c")
    t, _ = c3.send("显示风速点", "access")
    if "当前接入已完成" not in t:
        raise F(f"82③: 已消耗守卫未触发（应简短说明）: {t[:200]}")
    if "是否确认" in t or "确认执行" in t:
        raise F("82③: 已执行方案被重装配（button 复活）")
    if display_payload().get("active"):
        raise F("82③: 已消耗态建立显示")
    rc.log("  82 零认领三态 + 哨兵负向 + 切换引导禁令 OK")


def case95():
    conv = DConv("c95")
    # 接收端先于落地就位（对齐 v2 prereq）：转发实例启动即首连成功——落地后才
    # 起接收端会撞 30s 重连退避窗口，冒烟必然 0 收数
    recv = rc.start_receiver(19900)
    try:
        text, agents = conv.send(rc.MSG_CASE1, "access")
        cfg = None
        busy = 0
        for _ in range(14):
            if "有配置变更正在执行" in text:
                if busy >= 5:
                    raise F("95: 配置通道持续忙（重试 5 次未落地）")
                busy += 1
                time.sleep(2)
                text, ag = conv.send("[C4_BUTTON_CONFIRM] 确认", "access")
                if "有配置变更正在执行" not in text:
                    rc.wait_idle(timeout=600)
                    break
                continue
            if "确认" in text:
                text, ag = conv.send("[C4_BUTTON_CONFIRM] 确认", "access")
                rc.wait_idle(timeout=600)
                break
            if "当前接入已完成" in text:
                break  # 落地与否交给下方 config 轮询判定
            text, ag = conv.send("继续", "access")
        servers = []
        deadline = time.time() + 40
        while time.time() < deadline:
            cfg = rc.read_config()
            servers = [inst for (st, iid), inst in rc.server_instances(cfg).items()
                       if st == "c4_asfp2_server"]
            fwd = [inst for (st, iid), inst in rc.server_instances(cfg).items()
                   if st == "c4_asfp2_client"]
            if servers and fwd:
                break
            time.sleep(1)
        fwd = [inst for (st, iid), inst in rc.server_instances(cfg).items()
               if st == "c4_asfp2_client"]
        if not servers or not fwd:
            raise F(f"95: 实例对未落地 servers={len(servers)} fwd={len(fwd)}")
        if int(servers[0].get("port", -1)) != 19001:
            raise F(f"95: 接收端口≠19001: {servers[0].get('port')}")
        if int(fwd[0].get("port", -1)) != 19900:
            raise F(f"95: 转发端口≠19900: {fwd[0].get('port')}")
        if display_payload().get("active"):
            raise F("95: 接入旅程出现显示副作用")
        # 数据面冒烟：等采集端口真监听，转发已首连（接收端先在场），注入即可流动；
        # 注意 asfp2_client 模拟器 -e 为排他端点：要覆盖 1000~1009 需传 1010
        rc.wait_port(19001, True)
        time.sleep(1)
        rc.inject(19001, 1000, 1010, times=3)
        time.sleep(2)
        if recv_count(recv) < 10:
            raise F(f"95: 转发侧收数不足: {recv_count(recv)}")
    finally:
        recv.stop()
    rc.log("  95 默认接入完整链 + 数据面冒烟 OK")


def case84():
    conv = DConv("c84")
    text, _ = conv.send("显示1号风机的风速点", "display")
    p = wait_display(lambda x: x.get("active") is True, desc="84 建立")
    s = p["session"]
    if s["intervalMs"] != 1000:
        raise F(f"84: 缺省 intervalMs≠1000: {s['intervalMs']}")
    if s.get("terminateRemaining") is None:
        raise F("84: 缺省 30s 终止缺失")
    if s["points"][0].get("device") != "1号风机":
        raise F(f"84: device 字段缺失/错误: {s['points'][0]}")
    for need in ("1", "30"):
        if need not in text:
            raise F(f"84: 回显缺三要素『{need}』: {text[:200]}")
    if "双击 ESC" not in text and "双击ESC" not in text:
        rc.log("  84 提示: 回显未含『双击 ESC』字样（措辞宽松提示）")
    c = dcancel()
    if c != "display":
        raise F(f"84: cancel 应路由 display，得到 {c}")
    p = wait_display(lambda x: not x.get("active") and x.get("lastSession", {}).get(
        "endedReason") == "stopped", desc="84 终止摘要")
    rc.log("  84 单点订阅 + 三要素回显 + 双击 ESC 终止 OK")


def case88():
    conv = DConv("c88")
    rows = [
        ("显示1号风机的风速点，每2秒刷新一次，显示3分钟", 2000, "dur"),
        ("显示1号风机的功率点，每分钟一次", 60000, "dur"),
        ("显示1号风机的风向点，尽快刷新", 1000, "default"),
        ("显示1号风机的桨叶角度点，每0.5秒", 500, "dur"),
        ("显示1号风机的齿轮箱油温点，每0.2秒", None, "reject"),
        ("显示1号风机的塔筒温度点，刷新20次", 1000, "count"),
        ("显示1号风机的空气湿度点，一直显示", 1000, "manual"),
        ("显示1号风机的大气压强点，显示一会儿", 1000, "default"),
    ]
    for msg, iv, kind in rows:
        stop_display_if_any()
        text, _ = conv.send(msg, "display")
        if kind == "reject":
            if "250" not in text:
                raise F(f"88: <250ms 未拒绝: {text[:200]}")
            if display_payload().get("active"):
                raise F("88: 拒绝后仍建立会话")
            continue
        p = wait_display(lambda x: x.get("active") is True, desc=f"88 {msg[:14]}")
        s = p["session"]
        if s["intervalMs"] != iv:
            raise F(f"88: {msg[:16]} intervalMs={s['intervalMs']} ≠ {iv}")
        if kind == "manual":
            if s.get("terminateRemaining") is not None:
                raise F("88: manual 会话不应有 terminateRemaining")
        elif kind == "count":
            if "/" not in str(s.get("terminateRemaining")):
                raise F(f"88: count 终止形态异常: {s.get('terminateRemaining')}")
        else:
            if s.get("terminateRemaining") is None:
                raise F(f"88: {msg[:16]} 缺时长终止")
        if kind == "default" and ("30" not in text and "缺省" not in text and "默认" not in text):
            rc.log(f"  88 提示: 缺省回显未见『30/默认』字样（宽松）: {text[:120]}")
    stop_display_if_any()
    rc.log("  88 词形表 8 行（含拒绝/manual/缺省）OK")


def case89():
    conv = DConv("c89")
    conv.send("显示1号风机的所有点", "display")
    p = wait_display(lambda x: x.get("active") is True, desc="89 建立")
    s = p["session"]
    if len(s["points"]) != 10:
        raise F(f"89: 订阅点数≠10: {len(s['points'])}")
    bad = [q["key"] for q in s["points"] if q["state"] != "no_data"]
    if bad:
        raise F(f"89: 送数前应全部 no_data: {bad[:3]}")
    # -e 排他端点：覆盖 1000~1009 需传 1010（否则大气压强永远 no_data）
    rc.inject(19001, 1000, 1010, times=2)
    p = wait_display(lambda x: x.get("active") and all(
        q["state"] == "ok" for q in x["session"]["points"]), timeout=20, desc="89 全部 ok")
    for q in p["session"]["points"]:
        if "freq" not in q or "count" not in q["freq"] or "intervalMs" not in q["freq"]:
            raise F(f"89: freq 两指标缺失: {q['key']}")
    # 停送数（不再 inject）→ 陈旧（阈值 2s）
    time.sleep(5)
    p = wait_display(lambda x: x.get("active") and all(
        q["state"] == "stale" for q in x["session"]["points"]), timeout=12, desc="89 全部 stale")
    if not any(q.get("staleForMs", 0) > 0 for q in p["session"]["points"]):
        raise F("89: stale 无 staleForMs")
    stop_display_if_any()
    rc.log("  89 三阶段（no_data→ok→stale）+ freq 两指标 OK")


def case90():
    conv = DConv("c90")
    conv.send("显示1号风机的风速点，刷新5次", "display")
    p = wait_display(lambda x: x.get("active") is True, desc="90 建立")
    if p["session"]["mode"] != "cumulative":
        raise F(f"90: mode≠cumulative: {p['session']['mode']}")
    rc.inject(19001, 1000, 1009, times=3)
    p = wait_display(lambda x: not x.get("active") and x.get("lastSession", {}).get(
        "endedReason") == "completed_count", timeout=25, desc="90 次数到期")
    p0 = display_payload(since=0)
    if p0.get("active"):
        raise F("90: 已到期仍 active")
    rc.log("  90 累积模式 + 次数到期 OK（>100 上限属装配留白——合成点表另行覆盖）")


def case96():
    conv = DConv("c96")
    conv.send("显示1号风机的风速点，刷新6次", "display")
    p = wait_display(lambda x: x.get("active") is True, desc="96 建立")
    s = p["session"]
    dcancel()
    wait_display(lambda x: not x.get("active"), desc="96 停止")
    st, obj = api_json("/api/display", body={
        "pointKeys": [q["key"] for q in s["points"]],
        "displayNames": {q["key"]: "风速" for q in s["points"] if q.get("name")},
        "mode": s["mode"], "refreshCount": 6})
    if st != 200 or not obj.get("active"):
        raise F(f"96: 重新订阅失败: {st} {obj}")
    wait_display(lambda x: not x.get("active") and x.get("lastSession", {}).get(
        "endedReason") == "completed_count", timeout=25, desc="96 自然到期")
    rc.log("  96 订阅→停→重订阅→自然到期 OK")
    skip("96⑤", "LLM 不可用注入（77 同款手法）待专项轮（需坏 key 重启，链上其余状态会被扰动）")


def case92():
    # ② 非阻塞空操作：收集态 cancel → cancelled==null、状态不变
    c = DConv("c92")
    t, _ = c.send("接入7号风机，asfp2协议，端口9014", "access")  # 缺口停等（非阻塞）
    st_before = rc.state().get("state", {})
    got = dcancel()
    if got is not None:
        raise F(f"92②: 收集态 cancel 应空操作，得到 {got}")
    if rc.state().get("state", {}).get("phase") != st_before.get("phase"):
        raise F("92②: 空操作改变了 phase")
    # ⑦ 订阅回合窗口并存：异步订阅 + 立即 cancel → 显示停、回合流照常收尾
    out = {}

    def sub():
        try:
            out["text"], _, out["agents"] = dchat(
                rc.map_ports("显示1号风机的风速点"), [], c.cid, agent="display")
        except Exception as e:
            out["err"] = str(e)[:120]

    th = threading.Thread(target=sub)
    th.start()
    time.sleep(0.4)
    dcancel()
    th.join(timeout=120)
    deadline = time.time() + 10
    while time.time() < deadline and display_payload().get("active"):
        time.sleep(0.4)
    if display_payload().get("active"):
        raise F("92⑦: 订阅窗口 cancel 后显示未停")
    if out.get("err") and "取消" not in out["err"]:
        rc.log(f"  92⑦ 提示: 订阅流异常收尾（可接受）: {out['err'][:80]}")
    rc.log("  92 非阻塞空操作 + 订阅窗口并存裁决 OK（①=97 / ③=84 / ④⑤⑥ UI 或注入面）")


def land_2hao():
    conv = DConv("c2h")

    def two_servers(cfg):
        # 第二台落地 = 出现第二个 asfp2_server 实例（监听 19002）
        return any(int(i.get("port", -1)) == 19002
                   for (st, iid), i in rc.server_instances(cfg).items()
                   if st == "c4_asfp2_server")

    cfg, _ = land_access(conv, MSG_2HAO,
                         answers=[(r"端口|监听", "使用端口9002")], tag="2号",
                         landed=two_servers)
    servers = {iid: inst for (st, iid), inst in rc.server_instances(cfg).items()
               if st == "c4_asfp2_server"}
    if len(servers) != 2:
        raise F(f"2号: asfp2_server 实例数≠2: {list(servers)}")
    ports = sorted(int(i.get("port", -1)) for i in servers.values())
    if ports != [19001, 19002]:
        raise F(f"2号: 监听端口对异常: {ports}")
    fwd = [i for (st, iid), i in rc.server_instances(cfg).items() if st == "c4_asfp2_client"]
    addrs = sorted(a for i in fwd for a in [p["addr"] for p in i.get("points", [])])
    if not set(range(5000, 5020)).issubset(set(addrs)):
        raise F(f"2号: 两段转发地址未并存: {addrs[:5]}...{addrs[-5:]}")
    rc.log("  2号 接入 OK（独立 19002 实例 + 同转发目标两段地址）")


def case85():
    conv = DConv("c85")
    conv.send("显示风速点", "display")
    p = wait_display(lambda x: x.get("active") is True, desc="85 统称")
    devs = {q["device"] for q in p["session"]["points"]}
    if devs != {"1号风机", "2号风机"}:
        raise F(f"85: 统称展开设备集异常: {devs}")
    stop_display_if_any()
    conv.send("显示1号风机的所有点", "display")
    p = wait_display(lambda x: x.get("active") and len(x["session"]["points"]) == 10,
                     desc="85 设备级")
    stop_display_if_any()
    t, _ = conv.send("显示1号风机的风速和2号风机的功率", "display")
    if "1号" in t and "2号" in t and ("分别" in t or "先" in t or "哪台" in t):
        pass  # 引导逐设备
    else:
        rc.log(f"  85 提示: 跨设备列表引导措辞未匹配关键词（宽松）: {t[:160]}")
    if display_payload().get("active"):
        # 引导语义下若直接订阅了两点也接受为展开变体；校验设备集即可
        p = display_payload()
        devs = {q["device"] for q in p.get("session", {}).get("points", [])}
        if devs != {"1号风机", "2号风机"}:
            raise F(f"85: 跨设备列表结果异常: {devs}")
        stop_display_if_any()
    t, _ = conv.send("显示升压站所有电压值", "display")
    if "升压站" not in t and "没有" not in t and "已接入" not in t:
        rc.log(f"  85 提示: 升压站落空回复形态宽松: {t[:160]}")
    if display_payload().get("active"):
        stop_display_if_any()
        raise F("85: deviceRef 落空不应建立显示（不猜测）")
    rc.log("  85 统称展开 + 设备级 + 跨设备引导 + 落空候选 OK")


def case86():
    conv = DConv("c86")
    t, _ = conv.send("显示3号风机的风速点", "display")
    if "1号" not in t and "2号" not in t:
        rc.log(f"  86 提示: 3号落空未列候选（宽松）: {t[:160]}")
    if display_payload().get("active"):
        stop_display_if_any()
        raise F("86: 未接入设备被静默改订（不猜测禁令）")
    t, _ = conv.send("显示那台风机的功率", "display")
    if display_payload().get("active"):
        stop_display_if_any()
        raise F("86: 指代模糊被静默订阅")
    t, _ = conv.send("就 1号风机", "display")
    p = wait_display(lambda x: x.get("active") is True, timeout=30, desc="86 候选闭环")
    if p["session"]["points"][0]["device"] != "1号风机":
        raise F("86: 候选闭环订错设备")
    stop_display_if_any()
    rc.log("  86 歧义候选三形态 + 闭环 OK")


def case87():
    conv = DConv("c87")
    t, _ = conv.send("显示桨叶角速度", "display")
    if "桨叶角度" not in t:
        raise F(f"87: suggest 候选未含『桨叶角度』: {t[:200]}")
    if display_payload().get("active"):
        stop_display_if_any()
        raise F("87: 零命中被静默订阅（应先给候选）")
    t, _ = conv.send("显示桨叶角度", "display")
    wait_display(lambda x: x.get("active") is True, desc="87 选定订阅")
    stop_display_if_any()
    t, _ = conv.send("显示母线电压", "display")
    if "没有" not in t and "未" not in t:
        raise F(f"87: 无相似点未明确报告: {t[:200]}")
    if display_payload().get("active"):
        stop_display_if_any()
        raise F("87: 无候选被静默订阅（不杜撰）")
    rc.log("  87 suggest 闭环 + 明确报告没有 OK")


def case94():
    # ② 先于 ③（构造序）：cidA 收集态草稿；cidB 武装态（API 直连另一 cid）
    ca = DConv("c94a")
    ca.send("接入8号风机，asfp2协议，端口9015", "access")
    cb = DConv("c94b")
    t, _ = cb.send("接入9号风机，asfp2协议，端口9016，10个点从1000到1009，"
                   "点名分别是1000:风速、1001:功率、1002:风向、1003:桨叶角度、1004:发电机转速、"
                   "1005:齿轮箱油温、1006:塔筒温度、1007:空气温度、1008:空气湿度、1009:大气压强，"
                   "转发asfp2到127.0.0.1:9900，点表7600~7609", "access")
    for _ in range(6):
        if "确认" in t:
            break
        t, _ = cb.send("继续", "access")
    if "确认" not in t:
        raise F(f"94: cidB 未到武装态: {t[:160]}")
    if not rc.state().get("state", {}).get("hasAccessPlan"):
        raise F("94: 武装态未登记 hasAccessPlan")
    # ③ 活跃显示会话
    cd = DConv("c94d")
    cd.send("显示1号风机的风速点", "display")
    wait_display(lambda x: x.get("active") is True, desc="94 显示在场")
    # Reset：三重一次丢弃
    st, obj = api_json("/api/state/reset", body={})
    if st != 200:
        raise F(f"94: reset HTTP {st}")
    p = wait_display(lambda x: not x.get("active") and x.get("lastSession", {}).get(
        "endedReason") == "stopped", desc="94 级联停显")
    if rc.state().get("state", {}).get("hasAccessPlan"):
        raise F("94: reset 后武装未撤销")
    # 新 cid 无残留续接：提问不含 cidA/cidB 已答字段
    ce = DConv("c94e")
    t, _ = ce.send("接入10号风机，asfp2协议，端口9017", "access")
    for bad in ("9015", "9016", "7600", "9号", "8号"):
        if bad in t:
            raise F(f"94: 新会话泄漏旧会话已答字段『{bad}』: {t[:200]}")
    if "确认" in t and "方案" in t:
        raise F("94: 部分声明被误装配方案")
    rc.log("  94 Reset 级联（草稿+武装+显示）+ 无残留续接 OK")


def case98():
    conv = DConv("c98")
    conv.send("显示风速点", "display")
    p = wait_display(lambda x: x.get("active") and len(x["session"]["points"]) == 2,
                     desc="98 统称两点")
    devs = {q["device"] for q in p["session"]["points"]}
    if devs != {"1号风机", "2号风机"}:
        raise F(f"98: 设备列区分失效: {devs}")
    conv.send("显示所有风机的所有点", "display")
    p = wait_display(lambda x: x.get("active") and len(x["session"]["points"]) >= 20,
                     timeout=25, desc="98 全量")
    if len(p["session"]["points"]) != 20:
        raise F(f"98: 全量点数≠20: {len(p['session']['points'])}")
    dcancel()
    wait_display(lambda x: not x.get("active"), desc="98 收尾")
    cfg = rc.read_config()
    servers = [1 for (st, iid), i in rc.server_instances(cfg).items() if st == "c4_asfp2_server"]
    if len(servers) < 2:
        raise F("98: 两实例并存断言失败")
    rc.log("  98 两台统称对点 + 全量 20 点 + 双实例并存 OK")


def case99():
    conv = DConv("c99")
    conv.send("显示1号风机的风速点，一直显示", "display")
    wait_display(lambda x: x.get("active") is True, desc="99 manual 在场")
    cfg_before = rc.read_config()
    recv = rc.start_receiver(19900)
    try:
        base = recv_count(recv)
        rc.inject(19001, 1000, 1010, times=2)
        sc.agent_restart()
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                rc.state()
                break
            except Exception:
                time.sleep(0.5)
        p = display_payload()
        if p.get("active"):
            raise F("99: 重启后显示会话不应存活（内存态）")
        cfg_after = rc.read_config()
        if json.dumps(cfg_before, sort_keys=True) != json.dumps(cfg_after, sort_keys=True):
            raise F("99: 重启改写了 config")
        with open(os.path.join(rc.AGENT_DIR, "agent.json"), encoding="utf-8") as f:
            aj = json.load(f)
        if aj.get("site", {}).get("abbr") != "hnals":
            raise F("99: agent.json 场站绑定丢失")
        rc.inject(19001, 1000, 1010, times=2)
        time.sleep(1.5)
        if recv_count(recv) <= base:
            raise F("99: Agent 重启中断了数据管道（MCP 独立守护失效）")
        st, obj = api_json("/api/display", body={
            "pointKeys": [q["key"] for q in points_keys("风速")
                          if q["device"] == "1号风机"],
            "mode": "realtime", "manual": True})
        if st != 200 or not obj.get("active"):
            raise F(f"99: 重启后重新订阅失败: {st}")
        dcancel()
    finally:
        recv.stop()
    rc.log("  99 重启中止 + 管道不断 + 重订阅 OK（组链收尾）")


# ══ main（链序 = 组头）════════════════════════════════════
def main():
    steps = [
        ("80-structure", case80),
        ("BOOT", lambda: fresh_env()),  # 97 前置：启动隔离栈（agent + MCP，全新环境）
        ("97-fresh", case97),
        ("RESET-A5", lambda: fresh_env()),
        ("78-routing", case78),
        ("79-buckets", case79),
        ("RESET-A5b", lambda: fresh_env()),  # 79 落了 3号——95 需要干净「无已接入」前提
        ("81-upload", case81),
        ("82-zeroclaim", case82),
        ("RESET-A5c", lambda: fresh_env()),  # 82 各态落了 5/6/7 号临时草稿实例——95 前清场
        ("95-journey", case95),
        ("84-subscribe", case84),
        ("88-wordforms", case88),
        ("89-states", case89),
        ("90-cumulative", case90),
        ("96-cycle", case96),
        ("92-cancel", case92),
        ("2hao-land", land_2hao),
        ("85-unwrap", case85),
        ("86-ambig", case86),
        ("87-suggest", case87),
        ("94-reset", case94),
        ("98-two-dev", case98),
        ("99-restart", case99),
    ]
    skip("83", "UI-only（@/Tab/指示器人工核对，web.md §3.7）")
    skip("91", "UI-only（门控为前端行为；API 面由 84/88/92 覆盖）")
    skip("93", "需执行窗口注入装配（Stop-Start sleep 钩子）——单独小链另行实现")
    failed = []
    for name, fn in steps:
        rc.log(f"── DUAL {name} ──────────────────────────────")
        # 用例间隔离：清掉上一用例遗留的活跃显示会话（失败中断常来不及自清，
        # 遗留 active 会让下一用例的首查「零副作用」断言误报——85→86 实测级联）
        try:
            stop_display_if_any()
        except Exception:
            pass
        t0 = time.time()
        try:
            fn()
            rc.log(f"── DUAL {name} PASS ({time.time()-t0:.0f}s)")
        except F as e:
            rc.log(f"── DUAL {name} FAIL: {e}")
            failed.append((name, str(e)[:200]))
        except Exception as e:  # noqa: BLE001
            rc.log(f"── DUAL {name} ERROR: {type(e).__name__}: {e}")
            failed.append((name, f"{type(e).__name__}: {e}"[:200]))
        try:
            stop_display_if_any()
        except Exception:
            pass
    rc.log("══ DUAL 汇总 ══")
    for tag, why in SKIPS:
        rc.log(f"  SKIP {tag}: {why}")
    if failed:
        for name, err in failed:
            rc.log(f"  FAIL {name}: {err}")
        rc.log(f"DUAL RESULT: FAIL ({len(failed)} 步失败 / {len(steps)} 步)")
        sys.exit(1)
    rc.log(f"DUAL RESULT: PASS ({len(steps)} 步全过，{len(SKIPS)} 项标注跳过)")
    try:
        rc.AGENT.stop()
    except Exception:
        pass


if __name__ == "__main__":
    main()
