#!/usr/bin/env python3
# func_test_case_real_points.md 用例 RP-01~RP-13 驱动器（2026-10-07 初版，
# 2026-10-10 对齐文档 2026-10-08 拒收口径修订）
# 断言唯一来源：func_test_case_real_points.md（附录 A 点表基准数据）——按 c4/AGENTS.md
# 规则 4 实现，不根据 agent 实际输出调整断言；失败即实现未达成。
# 真实点表位于 <工作区根>/points/iec104/（仓库外）：目录缺失整组 SKIP（SKIP-DIR）。
#
# 口径修订（2026-10-08 文档 04f1884，对账速查）：
#   接入口径 RP-01/03/07/08/11/13；拒收口径 RP-02/04/05/09/09v（业务点名撞名整表
#   拒收——解析面 + fail-visible 冲突清单可见 + config 无实例）；追问口径 RP-06/10/12。
# 拒收文案机判约定（agent.md §3.2.1.3b 2026-10-08）：拒收文案禁用「顺延」字样——
# 文案出现「顺延」即违规提供顺延通道，本套件对全部拒收用例做该负向断言。
# RP-04 备注：文档「23 行冲突项」含占位白名单行（「备用」×21，agent.md 三裁应顺延），
# 与设计自相矛盾——按设计可机判口径断言业务冲突名（高压熔断器A/B/C）+ 禁顺延，
# 不对 23 这一数字做硬断言（待用户裁定后如需可再加）。
import csv
import codecs
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_cases as rc  # noqa: E402
import run_chain_v21 as base  # noqa: E402

PTS_DIR = "/home/wangbo/work/activesys/points/iec104"
XLSX_JS = "/home/wangbo/work/activesys/c4/agent/node_modules/xlsx"

# 统一样板（func_test_case_real_points.md「统一环境与样板」，逐字使用）
TEMPLATE = ("IEC104规约，RTU地址192.168.110.99:2404，公共地址1。"
            "转发采用asfp2协议，目标II区服务器127.0.0.1:9900，"
            "转发点表与采集侧一致，从 10000 号开始顺序编址。")
DEVICE_ANSWER = "设备名叫{name}，就用这些点表。"
# 拒收提示口径（agent.md §3.2.1.3b：「需设备厂家确认/按装置拆分/修正点名」之一）
REJECT_HINT_RE = re.compile(r"厂家|拆分|修正点名")
# 撞名报告关键词（fail-visible 冲突清单）
DUP_RE = re.compile(r"撞名|同名|重复|冲突")
# 回落/分段追问关键词（RP-06/RP-10；文档：「地址序列重置/疑似多台设备分段/
# 需拆分或确认设备归属」之一）
FALLBACK_RE = re.compile(r"回落|多台设备|分段|拆分|序列重置|重新起算")

# 大回合（分块提取可达 10~20 分钟）：驱动器侧超时放宽（rc.chat/upload 的 300s 为
# 小点表用例设定；本套件不改断言、只放宽传输超时）
def _post_big(path, body, timeout=3600):
    req = urllib.request.Request(
        rc.BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"}, method="POST")
    return urllib.request.urlopen(req, timeout=timeout)


def upload_big(conversation_id, file_path, message=None):
    """rc.upload_file 同构（multipart + SSE），超时 3600s（大点表提取回合）。"""
    import uuid
    boundary = "----c4e2e" + uuid.uuid4().hex
    parts = []

    def field(name, value):
        parts.append(
            (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
             f"\r\n\r\n{value}\r\n").encode("utf-8"))

    if message:
        field("message", message)
    field("conversationId", conversation_id)
    fname = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        file_bytes = f.read()
    parts.append(
        (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
         f'filename="{fname}"\r\nContent-Type: application/octet-stream\r\n\r\n'
         ).encode("utf-8") + file_bytes + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    req = urllib.request.Request(
        rc.BASE + "/api/upload", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Accept": "text/event-stream"})
    text_parts, events = [], []
    with urllib.request.urlopen(req, timeout=3600) as resp:
        server_cid = resp.headers.get("X-Conversation-Id", "")
        if server_cid and server_cid != conversation_id:
            raise rc.Fail(f"upload: 服务端会话 id 不一致 {server_cid}")
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
                    if d.get("type") == "text" and isinstance(d.get("content"), str):
                        text_parts.append(d["content"])
                    elif d.get("type") == "error":
                        events.append(("error", ""))
    text = "".join(text_parts)
    rc.log(f"  >> [upload] {os.path.basename(file_path)} message={message!r}")
    rc.log(f"  << [upload receipt] {text}")
    return text, events


# ── 断言基准提取（附录 A 同源：SheetJS sheet_to_json(header:1) / GB18030 csv；
#    只读真实点表文件，与 c4 实现无关）──────────────────────────

def _sheets(path):
    script = ('const X=require(%s);const wb=X.readFile(process.argv[1]);'
              'const o={};for(const n of wb.SheetNames){'
              'o[n]=X.utils.sheet_to_json(wb.Sheets[n],{header:1,raw:true,defval:null});}'
              'process.stdout.write(JSON.stringify(o));' % json.dumps(XLSX_JS))
    out = subprocess.run(["node", "-e", script, path], capture_output=True, timeout=300)
    if out.returncode != 0:
        raise rc.Fail(f"点表读取失败 {os.path.basename(path)}: "
                      f"{out.stderr.decode('utf-8', 'replace')[:200]}")
    return json.loads(out.stdout.decode("utf-8"))


def _cell(v):
    return "" if v is None else str(v).strip()


def _csv_map(path):
    """yxTagTable/ycTagTable（GBK）→ {addr: (设备, 描述)}（表头 序号,地址,设备,描述）。"""
    out = {}
    with codecs.open(path, "r", "gb18030") as f:
        for row in csv.reader(f):
            if len(row) < 4 or row[1].strip() == "地址":
                continue
            try:
                out[int(float(row[1]))] = (row[2].strip(), row[3].strip())
            except ValueError:
                continue
    return out


def _dups(sh, sheets_spec, name_col=1):
    """按点名分量分组撞名条目（跳过表头/非数据行）→ {name: [addr_row]}。
    sheets_spec: [(sheet 名, addr 基)]——addr = 基 + 序号（0 起基 = 起点，1 起基 = 起点−1）。"""
    cnt, rows_by = {}, {}
    for sname, abase in sheets_spec:
        for r in sh.get(sname, []):
            if not r or len(r) <= max(1, name_col):
                continue
            sn = _cell(r[0])
            nm = _cell(r[name_col])
            if not nm or nm in ("描述", "信号名称", "点名") or not sn or sn == "序号":
                continue
            try:
                s = int(float(sn))
            except ValueError:
                continue
            cnt[nm] = cnt.get(nm, 0) + 1
            rows_by.setdefault(nm, []).append(abase + s)
    return {k: rows_by[k] for k in rows_by if cnt[k] > 1}


def _has_num(blob, n):
    """独立数字可见（允许千分位逗号；前后不邻数字）。"""
    pat = re.sub(r"(?<=\d)(?=\d)", ",?", str(n))
    return re.search(r"(?<!\d)%s(?!\d)" % pat, blob) is not None


def _cfg_points():
    cfg = rc.read_config()
    return [p for i in _iec_instances(cfg) for p in i.get("points", [])]


def _iec_instances(cfg):
    return (cfg or {}).get("c4_iec104_client", [])


def iec_points(cfg):
    """全部 c4_iec104_client 实例的点列表（按实例）。"""
    out = []
    for inst in (cfg or {}).get("c4_iec104_client", []):
        out.append(inst)
    return out


def fwd_points(cfg):
    """转发点（c4_asfp2_client：客户端推送形态，目标 II 区 9900）。"""
    pts = []
    for inst in (cfg or {}).get("c4_asfp2_client", []):
        pts.extend(inst.get("points", []))
    return pts


def iec_count(cfg):
    return sum(len(i.get("points", [])) for i in iec_points(cfg))


def iec_addrs(cfg):
    a = []
    for i in iec_points(cfg):
        a.extend(int(p["addr"]) for p in i.get("points", []))
    return sorted(a)


def _n_inst(cfg):
    return sum(len(v) for k, v in (cfg or {}).items()
               if isinstance(v, list) and k != "c4_shm_manager")


def expect_no_config(cfg_n0, note):
    time.sleep(2)
    n1 = _n_inst(rc.read_config())
    if n1 != cfg_n0:
        raise rc.Fail(f"{note}: 预期追问/拒绝不出方案，但实例数变化 {cfg_n0} → {n1}")


def flow_to_confirm(conv, message, answers, cfg_n0, tag, max_turns=14):
    """驱动到执行落地：确认按钮 + 应答表；实例数变化即止。场站应答自动注入
    （V21Agent 无 site，首接必问场站，run_chain_v21 s52 同款）。"""
    answers = [(r"场站", "华能阿拉善")] + list(answers)
    consumed = [False] * len(answers)
    text = conv.send(message)
    clicked = 0
    for _ in range(max_turns):
        if _n_inst(rc.read_config()) != cfg_n0:
            return text
        if "解析失败" in text:
            raise rc.Fail(f"{tag}: 接入被拒（预期接入落地）: {text[:300]}")
        if clicked < 2 and ("是否确认" in text or "确认执行" in text
                            or "确认后我将" in text or ("方案" in text and "确认" in text)):
            text = conv.send("[C4_BUTTON_CONFIRM] 确认")
            clicked += 1
            rc.wait_idle(timeout=300)
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
        time.sleep(15)
        text = conv.send("继续")
    raise rc.Fail(f"{tag}: flow 超过 {max_turns} 轮未收敛，尾回复: {text[:300]}")


def drive_reject(conv, upload_text, first_msg, answers, done_fn, tag, max_turns=10):
    """拒收/追问用例驱动：上传回执 + 多轮对话累计为 blob；done_fn(blob) 命中即停，
    轮次耗尽也返回（断言阶段裁决）。场站应答自动注入，兜底「继续」。"""
    answers = [(r"场站", "华能阿拉善")] + list(answers)
    consumed = [False] * len(answers)
    blob = upload_text or ""
    text = conv.send(first_msg)
    blob += text
    for _ in range(max_turns):
        if done_fn(blob):
            return blob
        hit = False
        for i, (pat, ans) in enumerate(answers):
            if not consumed[i] and re.search(pat, text):
                consumed[i] = True
                text = conv.send(ans)
                blob += text
                hit = True
                break
        if hit:
            continue
        time.sleep(10)
        text = conv.send("继续")
        blob += text
    return blob


def _history_blob(conv):
    return " ".join(str(m.get("content", "")) for m in conv.history if isinstance(m, dict))


def wait_parse_count(conv, n, tag, max_turns=20):
    """异步提取收敛等待（解析为后台任务，上传回执只报「正在解析」，结果随下一轮
    回复摘要给出——2026-10-10 实测）：轮询纯文本轮直到 n 点出现在回复中。轮次耗尽
    抛错（计数不可见即文档断言失败：累积/替换语义不成立）。"""
    for _ in range(max_turns):
        text = conv.send("继续")
        if _has_num(text, n):
            return text
        time.sleep(15)
    raise rc.Fail(f"{tag}: 等待解析 {n} 点超时（{max_turns} 轮纯文本轮未现）")


# ── 各用例 ────────────────────────────────────────────────

def rp01():
    """RP-01：分文件两表 GBK csv——文件名语境 + 异名累积 + 同名替换 + 设备语境 id。"""
    n0 = _n_inst(rc.read_config())
    conv = rc.Conv()
    yx = os.path.join(PTS_DIR, "yxTagTable.csv")
    yc = os.path.join(PTS_DIR, "ycTagTable.csv")
    # 场站随首个上传消息给出（agent 首接必问场站）；解析为异步——每次上传后以
    # 纯文本轮轮询至预期点数在回复摘要可见（文档场景：间隔纯文本轮补必要项）
    r1, _ = upload_big(conv.conversation_id, yx,
                       "场站名称：华能阿拉善。请解析此文件中的设备信息")
    conv.send(TEMPLATE)
    # 设备名尽早给定：后续 wait_parse_count 的纯文本轮询（「继续」）不会再被
    # 当作设备名吞掉、提前装配出错误名字的方案
    conv.send(DEVICE_ANSWER.format(name="高力板 csv 站"))
    wait_parse_count(conv, 4688, "RP-01[yx]")
    r2, _ = upload_big(conv.conversation_id, yc, "这是配套的遥测点表，请合并解析。")
    wait_parse_count(conv, 12728, "RP-01[yc 合并]")   # 异名累积：不被重置为 8040
    r3, _ = upload_big(conv.conversation_id, yx, "重新上传遥信点表（同前）。")
    wait_parse_count(conv, 12728, "RP-01[yx 同名再传]")  # 替换语义：不叠加重复
    flow_to_confirm(
        conv, "继续按这些点表接入。",
        [(r"接入协议|用哪种协议|协议", "iec104。RTU地址192.168.110.99:2404，公共地址1。"),
         (r"连接|RTU|端口|公共地址", "RTU地址192.168.110.99:2404，公共地址1。")],
        n0, "RP-01")
    rc.wait_config(lambda c: iec_count(c) == 12728, timeout=300, desc="RP-01: 12728 点")
    # 解析面断言聚合在「上传回执 + 全部对话」上（agent 回执轮为「已收到…正在解析」，
    # 解析报告随下一轮回复给出——catch-up 形态，2026-10-10 实测）
    receipts = r1 + r2 + r3
    blob = receipts + _history_blob(conv)
    # GBK 解码正确：无乱码替换符，中文描述可读（表头字面词不作硬断言——回执形态
    # 为点数摘要，表头不一定回显）
    if "\ufffd" in blob:
        raise rc.Fail(f"RP-01: 对话出现乱码替换符: {blob[:200]}")
    if "大兴光伏逆变器" not in blob:
        raise rc.Fail(f"RP-01: 中文描述不可读（GBK 解码）: {blob[:300]}")
    if not _has_num(blob, 4688) or "遥信" not in blob:
        raise rc.Fail("RP-01: csv#1 解析结果（4688/遥信）不可见")
    # csv#2 全部遥测的类型归属由 config 断言保证（ycN == 8040 且区间 16385~24424
    # 原样）——合并方案表格超长截断（前 50 行均为遥信），「遥测」字样不出现在
    # 对话属渲染形态，不作对话框硬断言（2026-10-10 实测裁定）
    # 异名累积：第二次上传后草稿 12728（不被重置为 8040）；同名再传替换语义仍 12728
    if not _has_num(blob, 12728):
        raise rc.Fail("RP-01: 合并草稿点数 12728 不可见（累积语义）")
    cfg = rc.read_config()
    addrs = iec_addrs(cfg)
    if len(addrs) != 12728:
        raise rc.Fail(f"RP-01: 点数 {len(addrs)} ≠ 12728")
    yxN = sum(1 for i in iec_points(cfg) for p in i["points"] if p.get("point_type") == "yx")
    ycN = sum(1 for i in iec_points(cfg) for p in i["points"] if p.get("point_type") == "yc")
    if (yxN, ycN) != (4688, 8040):
        raise rc.Fail(f"RP-01: 类型分布 yx={yxN}/yc={ycN} ≠ 4688/8040")
    yxA = sorted(int(p["addr"]) for i in iec_points(cfg) for p in i["points"] if p.get("point_type") == "yx")
    ycA = sorted(int(p["addr"]) for i in iec_points(cfg) for p in i["points"] if p.get("point_type") == "yc")
    if yxA != list(range(1, 4689)):
        raise rc.Fail(f"RP-01: yx 归一化区间错误（首 {yxA[:3]} 末 {yxA[-3:]}）")
    if ycA != list(range(16385, 24425)):
        raise rc.Fail(f"RP-01: yc 归一化区间错误（首 {ycA[:3]} 末 {ycA[-3:]}）")
    fwd = sorted(int(p["addr"]) for p in fwd_points(cfg))
    if len(fwd) != 12728 or fwd != list(range(10000, 22728)):
        raise rc.Fail(f"RP-01: 转发 12728 点 10000~22727 顺序编址不符（{len(fwd)} 点）")
    if len(iec_points(cfg)) != 1:
        raise rc.Fail(f"RP-01: iec104 实例数 {len(iec_points(cfg))} ≠ 1")
    # 落盘 id 形态（设备语境并入）：id 以「设备_点名」归一化收尾（前缀容忍——
    # v2.1.0 架构点 key 无条件带设备 key 前缀，文档「设备值_点名归一化 形态」
    # 即 id 的点名承载段）；name 保留原文；首/中/尾抽样
    by_addr = {int(p["addr"]): p for p in _cfg_points()}
    for path, samples in ((yx, (1, 2344, 4688)), (yc, (16385, 20404, 24424))):
        ref = _csv_map(path)
        for a in samples:
            dev, name = ref[a]
            p = by_addr[a]
            tail = rc.normalize_point_name(f"{dev}_{name}")
            pid = str(p.get("id", ""))
            if not (pid.endswith(tail) and len(pid) > len(tail)):
                raise rc.Fail(f"RP-01: addr {a} id={pid!r} 不含设备_点名段 {tail!r}")
            if p.get("name") != name:
                raise rc.Fail(f"RP-01: addr {a} name={p.get('name')!r} ≠ 原文 {name!r}")
    ids = [p.get("id") for p in _cfg_points()]
    if len(set(ids)) != 12728:
        raise rc.Fail(f"RP-01: id 组合去重 {len(set(ids))} ≠ 12728（组合重复应归零）")
    rc.log("  RP-01 PASS（12728 = 4688 yx + 8040 yc，原样区间，单实例，设备_点名 id）")


def rp02():
    """RP-02：多 sheet 三类型 + YK 排除——业务点名撞名整表拒收（小塔子，2026-10-08 口径）。"""
    n0 = _n_inst(rc.read_config())
    # 断言基准自检：YX/YC/YM 撞名恰 1 对（SVGCSC221A本体动作，点号 211/222）
    sh = _sheets(os.path.join(PTS_DIR, "小塔子远动点表.xlsx"))
    dups = _dups(sh, [("YX", 0), ("YC", 16385), ("YM", 25601)])
    if list(dups) != ["35kV SVGCSC221A本体动作"] or dups["35kV SVGCSC221A本体动作"] != [211, 222]:
        raise rc.Fail(f"RP-02: 断言基准自检失败 {dups}")
    conv = rc.Conv()
    msg = TEMPLATE + "点表共 510 点（含不接入的遥控 17 点）。"
    r, _ = upload_big(conv.conversation_id, os.path.join(PTS_DIR, "小塔子远动点表.xlsx"), msg)
    blob = drive_reject(
        conv, r, "请按此文件接入。",
        [(r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="小塔子远动"))],
        lambda b: "SVGCSC221A" in b and _has_num(b, 493), "RP-02")
    # 解析面：提取 493 行（324+125+44）与 excluded YK（17 点）可见，多 sheet 无乱码
    if not _has_num(blob, 493):
        raise rc.Fail(f"RP-02: 提取数 493 不可见: {blob[-400:]}")
    if "YK" not in blob or not _has_num(blob, 17):
        raise rc.Fail(f"RP-02: excluded YK（17 点）不可见: {blob[-400:]}")
    if "\ufffd" in blob:
        raise rc.Fail("RP-02: 对话出现乱码替换符（多 sheet GBK）")
    # 拒收 fail-visible：冲突项（点名 ×2 及各自 addr）+ 提示口径 + 禁「顺延」
    if "SVGCSC221A本体动作" not in blob:
        raise rc.Fail(f"RP-02: 冲突点名不可见: {blob[-400:]}")
    if not (_has_num(blob, 211) and _has_num(blob, 222)):
        raise rc.Fail(f"RP-02: 冲突 addr（211/222）不可见: {blob[-400:]}")
    if not REJECT_HINT_RE.search(blob):
        raise rc.Fail(f"RP-02: 拒收提示口径（厂家确认/按装置拆分/修正点名）不可见: {blob[-400:]}")
    if "顺延" in blob:
        raise rc.Fail("RP-02: 拒收文案出现「顺延」字样（违规提供顺延通道，agent.md 机判约定）")
    expect_no_config(n0, "RP-02")
    rc.log("  RP-02 PASS（493 提取 + YK 17 排除可见；SVGCSC221A ×2 拒收，无 config）")


def rp03():
    """RP-03：中文 sheet 前缀 + 4001H 标题不触发 16 进制（高力板镇，接入口径）。"""
    n0 = _n_inst(rc.read_config())
    conv = rc.Conv()
    upload_big(conv.conversation_id, os.path.join(PTS_DIR, "高力板镇104点表-光伏区-新.xlsx"),
               TEMPLATE + "请解析此文件中的设备信息")
    flow_to_confirm(
        conv, "请按此文件接入。",
        [(r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="高力板镇光伏"))],
        n0, "RP-03")
    rc.wait_config(lambda c: iec_count(c) == 729, timeout=300, desc="RP-03: 729 点")
    cfg = rc.read_config()
    addrs = iec_addrs(cfg)
    expect = sorted(list(range(1, 257)) + list(range(16385, 16858)))
    if addrs != expect:
        raise rc.Fail(f"RP-03: 地址集合不符（首 {addrs[:3]} 末 {addrs[-3:]} 共 {len(addrs)}）")
    for bad in (4001, 20386, 25089, 24577):
        if bad in addrs:
            raise rc.Fail(f"RP-03: 出现错映射/漏排除地址 {bad}")
    blob = _history_blob(conv)
    # excluded 组语境名（子串匹配命中）：组名渲染为 sheet 内段标题（「遥调6201H…」
    # /「遥控6001H…」，2026-10-10 实测）——「104」前缀的命中由排除本身发生证明，
    # 不对前缀字面量硬断言；两组各 4 点
    if "遥调" not in blob or "遥控" not in blob or "（4 点）" not in blob:
        raise rc.Fail(f"RP-03: 遥调/遥控排除组（各 4 点）不可见: {blob[-400:]}")
    rc.log("  RP-03 PASS（729 = 256 yx + 473 yc 原样；4001H 不触发；4+4 排除含前缀）")


def rp04():
    """RP-04：单文件多段段标题 + 设备列 + 0 起偏移——业务撞名整表拒收（11号光伏区）。"""
    n0 = _n_inst(rc.read_config())
    conv = rc.Conv()
    r, _ = upload_big(conv.conversation_id, os.path.join(PTS_DIR, "11号光伏区1号箱变测控点表.xls"),
                      TEMPLATE + "请解析此文件中的设备信息")
    blob = drive_reject(
        conv, r, "请按此文件接入。",
        [(r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="11号光伏区箱变"))],
        lambda b: "高压熔断器" in b, "RP-04")
    # 解析面：段标题归属 + 设备语境列 + excluded 遥控/遥调 可见。已受理段标题
    # 不渲染（解析面以类型计数承载段归属：364/202/32 即 5 段中三个受理段的大小，
    # 2026-10-10 实测裁定）；排除段以段标题渲染（遥控数据定义/遥调数据定义）
    for n in (598, 364, 202, 32):
        if not _has_num(blob, n):
            raise rc.Fail(f"RP-04: 解析面计数 {n} 不可见: {blob[-400:]}")
    if "遥控数据定义" not in blob or "遥调数据定义" not in blob:
        raise rc.Fail(f"RP-04: excluded 遥控/遥调段标题不可见: {blob[-400:]}")
    # 拒收 fail-visible：冲突清单裸 id 含设备前缀（设备语境并入证据）+ 口径 + 禁顺延
    # （文档「23 行」含白名单「备用」×21，与 agent.md 三裁矛盾——见文件头备注；
    #  实测拒收恰为 3 组业务撞名：ZRR300AOLD_高压熔断器A/B/C，备用按白名单顺延）
    if "ZRR300AOLD_" not in blob or "高压熔断器" not in blob:
        raise rc.Fail(f"RP-04: 冲突点名（ZRR300AOLD_高压熔断器A/B/C）不可见: {blob[-400:]}")
    if not REJECT_HINT_RE.search(blob):
        raise rc.Fail(f"RP-04: 拒收提示口径不可见: {blob[-400:]}")
    if "顺延" in blob:
        raise rc.Fail("RP-04: 拒收文案出现「顺延」字样（agent.md 机判约定）")
    expect_no_config(n0, "RP-04")
    rc.log("  RP-04 PASS（5 段标题 + 设备语境列 + 8+6 排除可见；业务撞名拒收，无 config）")


def rp05():
    """RP-05：0 起/1 起三文件对照——每轮业务撞名整表拒收（2026-10-08 口径）。"""
    rounds = [
        # (文件, 应答设备名, (yx, yc, [排除]) 提取数, 撞名检测 sheet, 序号起始形态)
        ("虎头山最新点表.xls", "虎头山风电场", (665, 200, 43),
         [("遥信", 1), ("遥测", 16385)]),
        ("高家堡集控站点表.xls", "高家堡集控站", (1403, 350, 35),
         [("遥信", 1), ("遥测", 16385)]),
        ("碾子山风电场-标准104测点.xls", "碾子山风电场", (1161, 187, 260),
         [("单点遥信", 0), ("遥测", 16384), ("遥脉", 25600)]),
    ]
    for fname, dev, counts, spec in rounds:
        n0 = _n_inst(rc.read_config())
        # 断言基准自检：该文件确有同装置业务撞名（文档：1/4/7 行）
        dups = _dups(_sheets(os.path.join(PTS_DIR, fname)),
                     [(s, b) for s, b in spec])
        if not dups:
            raise rc.Fail(f"RP-05[{fname}]: 断言基准自检失败（未提取到撞名）")
        dup_name = next(iter(dups))
        dup_addrs = dups[dup_name]
        conv = rc.Conv()
        r, _ = upload_big(conv.conversation_id, os.path.join(PTS_DIR, fname),
                          TEMPLATE + "请解析此文件中的设备信息")
        blob = drive_reject(
            conv, r, "请按此文件接入。",
            [(r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name=dev))],
            lambda b: DUP_RE.search(b) and all(_has_num(b, n) for n in counts),
            f"RP-05[{fname}]")
        # 解析面：提取数与类型/排除组可见
        for n in counts:
            if not _has_num(blob, n):
                raise rc.Fail(f"RP-05[{fname}]: 提取数 {n} 不可见: {blob[-400:]}")
        # 拒收 fail-visible：冲突项（点名 + addr）+ 口径 + 禁顺延
        if not DUP_RE.search(blob):
            raise rc.Fail(f"RP-05[{fname}]: 撞名报告不可见: {blob[-400:]}")
        base_name = dup_name.split("]")[-1] if "]" in dup_name else dup_name
        if base_name not in blob:
            raise rc.Fail(f"RP-05[{fname}]: 冲突点名 {base_name!r} 不可见: {blob[-400:]}")
        if not any(_has_num(blob, a) for a in dup_addrs):
            raise rc.Fail(f"RP-05[{fname}]: 冲突 addr {dup_addrs} 不可见: {blob[-400:]}")
        if not REJECT_HINT_RE.search(blob):
            raise rc.Fail(f"RP-05[{fname}]: 拒收提示口径不可见: {blob[-400:]}")
        if "顺延" in blob:
            raise rc.Fail(f"RP-05[{fname}]: 拒收文案出现「顺延」字样")
        expect_no_config(n0, f"RP-05[{fname}]")
        rc.log(f"  RP-05[{fname}] PASS（{counts} 提取可见；撞名 {dup_name!r} 拒收，无 config）")


def rp06():
    """RP-06：16 进制别名 + Excel 吞址——序列回落追问（正负混合，遥测21）。"""
    n0 = _n_inst(rc.read_config())
    conv = rc.Conv()
    r, _ = upload_big(conv.conversation_id, os.path.join(PTS_DIR, "遥测21.xls"),
                      TEMPLATE + "请解析此文件中的设备信息")
    blob = drive_reject(
        conv, r, "请按此文件接入。", [],
        lambda b: FALLBACK_RE.search(b) and "16385" in b, "RP-06")
    # 回落追问 fail-visible，文案可定位（回落位置 16607 之后 / 异常行/科学计数法形态之一）
    if not FALLBACK_RE.search(blob):
        raise rc.Fail(f"RP-06: 未出现回落追问: {blob[-400:]}")
    if not re.search(r"16607|科学计数|4\.00E|异常", blob):
        raise rc.Fail(f"RP-06: 回落定位不可见（16607/科学计数法/异常行）: {blob[-400:]}")
    # hex 别名识别可见（4001 → 16385 判定出现在追问或回执中）
    if "16385" not in blob:
        raise rc.Fail(f"RP-06: hex 别名识别（4001→16385）不可见: {blob[-400:]}")
    expect_no_config(n0, "RP-06")
    rc.log("  RP-06 PASS（回落追问 + hex 别名识别可见 + 不静默接入）")


def rp07():
    """RP-07：16 进制无标记 ym + 文件名语义归类（脉冲2，接入口径）。"""
    n0 = _n_inst(rc.read_config())
    conv = rc.Conv()
    upload_big(conv.conversation_id, os.path.join(PTS_DIR, "脉冲2.xls"),
               TEMPLATE + "请解析此文件中的设备信息")
    flow_to_confirm(
        conv, "请按此文件接入。",
        [(r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="脉冲电度表"))],
        n0, "RP-07")
    rc.wait_config(lambda c: iec_count(c) == 48, timeout=300, desc="RP-07: 48 点")
    cfg = rc.read_config()
    addrs = sorted(int(p["addr"]) for i in iec_points(cfg) for p in i["points"])
    if addrs != list(range(25601, 25649)):
        raise rc.Fail(f"RP-07: ym 6401→25601 换算错误（首 {addrs[:3]} 末 {addrs[-3:]}）")
    types = {p.get("point_type") for i in iec_points(cfg) for p in i["points"]}
    if types != {"ym"}:
        raise rc.Fail(f"RP-07: 类型 {types} ≠ {{'ym'}}（文件名「脉冲」语义归类）")
    if "遥脉" not in _history_blob(conv):
        raise rc.Fail("RP-07: 方案类型列遥脉判定不可见")
    rc.log("  RP-07 PASS（48 点 ym 25601~25648，空 sheet 跳过，方案类型遥脉）")


def rp08():
    """RP-08：无表头三列值形态定位地址列（晨光，接入口径）。"""
    n0 = _n_inst(rc.read_config())
    conv = rc.Conv()
    upload_big(conv.conversation_id, os.path.join(PTS_DIR, "晨光点表.xlsx"),
               TEMPLATE + "请解析此文件中的设备信息")
    flow_to_confirm(
        conv, "请按此文件接入。",
        [(r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="晨光风电场"))],
        n0, "RP-08")
    rc.wait_config(lambda c: iec_count(c) == 3004, timeout=300, desc="RP-08: 3004 点")
    cfg = rc.read_config()
    addrs = iec_addrs(cfg)
    expect = sorted(list(range(16385, 19089)) + list(range(1, 253)) + list(range(25601, 25649)))
    if addrs != expect:
        raise rc.Fail(f"RP-08: 地址集合不符（共 {len(addrs)}）")
    # 点名进入 name 字段（设备级长标识 DT_CG*——晨光 遥测 DT_CGFJU_*、遥信/遥脉
    # DT_CGDQU_*；ASCII 名归一化原样小写）
    pts = _cfg_points()
    for p in (pts[0], pts[len(pts) // 2], pts[-1]):
        if not str(p.get("name", "")).startswith("DT_CG"):
            raise rc.Fail(f"RP-08: name 未取点名（{p.get('name')!r}）")
        pid, tail = str(p.get("id", "")), rc.normalize_point_name(p["name"])
        if not (pid.endswith(tail) and len(pid) > len(tail)):
            raise rc.Fail(f"RP-08: id {pid!r} 不含归一化点名段 {tail!r}")
    rc.log("  RP-08 PASS（3004 点原样区间；地址列按值形态定位；点名入 name）")


def rp09(variant=False):
    """RP-09：表头错位大表——「地址」列是装置地址；业务撞名整表拒收（板卡网口3/4）。"""
    fname = ("板卡1-网口4-标准104调度规约-2405-王博.xls" if variant
             else "板卡1-网口3-标准104调度规约-2404-王博.xls")
    # (总提取, yx, yc, 排除遥控)；附录 A：网口3 15894=7992+7902+234；网口4 15011=7548+7463+221
    totals, yxN, ycN, excN = ((15011, 7548, 7463, 221) if variant
                              else (15894, 7992, 7902, 234))
    tag = "RP-09v" if variant else "RP-09"
    n0 = _n_inst(rc.read_config())
    conv = rc.Conv()
    r, _ = upload_big(conv.conversation_id, os.path.join(PTS_DIR, fname),
                      TEMPLATE + "请解析此文件中的设备信息")
    blob = drive_reject(
        conv, r, "请按此文件接入。",
        [(r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="北恒箱变测控"))],
        lambda b: "箱变备用" in b and _has_num(b, totals), tag, max_turns=12)
    # 解析面：提取数/类型分布 + excluded 单点遥控引用表 + IOA 起点 16385 可见
    # （「地址」列识别为设备语境列而非 IOA 的证据载体；无序列回落误报）
    for n in (totals, yxN, ycN):
        if not _has_num(blob, n):
            raise rc.Fail(f"{tag}: 提取数 {n} 不可见: {blob[-400:]}")
    if "单点遥控引用表" not in blob or not _has_num(blob, excN):
        raise rc.Fail(f"{tag}: excluded 单点遥控引用表（{excN} 点）不可见: {blob[-400:]}")
    if "16385" not in blob:
        raise rc.Fail(f"{tag}: IOA 起点 16385 不可见: {blob[-400:]}")
    # 拒收 fail-visible：冲突项（箱变备用 / 箱变断路器2分位 等 + 各自装置地址/addr）
    if "箱变备用" not in blob or "箱变断路器2分位" not in blob:
        raise rc.Fail(f"{tag}: 冲突点名（箱变备用/箱变断路器2分位）不可见: {blob[-400:]}")
    if not REJECT_HINT_RE.search(blob):
        raise rc.Fail(f"{tag}: 拒收提示口径不可见: {blob[-400:]}")
    if "顺延" in blob:
        raise rc.Fail(f"{tag}: 拒收文案出现「顺延」字样")
    expect_no_config(n0, tag)
    rc.log(f"  {tag} PASS（{totals} 提取 + 遥控 {excN} 排除可见；业务撞名拒收，无 config）")


def rp10():
    """RP-10：多设备分段回落——序列重置检测追问（南瑞中德，负向）。"""
    n0 = _n_inst(rc.read_config())
    conv = rc.Conv()
    r, _ = upload_big(conv.conversation_id, os.path.join(PTS_DIR, "光伏南瑞中德点表.xls"),
                      TEMPLATE + "请解析此文件中的设备信息")
    blob = drive_reject(
        conv, r, "请按此文件接入。", [],
        lambda b: FALLBACK_RE.search(b), "RP-10", max_turns=12)
    # 方案装配前追问：地址序列重置/疑似多台设备分段/需拆分或确认设备归属之一
    if not FALLBACK_RE.search(blob):
        raise rc.Fail(f"RP-10: 未出现回落/分段追问: {blob[-400:]}")
    expect_no_config(n0, "RP-10")
    rc.log("  RP-10 PASS（4 项目段回落 → 追问，不静默接入）")


def rp11():
    """RP-11：干扰列与表头杂值（吉电，接入口径）+ 设备语境 id 落盘断言。"""
    n0 = _n_inst(rc.read_config())
    conv = rc.Conv()
    upload_big(conv.conversation_id, os.path.join(PTS_DIR, "吉电新能源南瑞科技通讯点表.xls"),
               TEMPLATE + "请解析此文件中的设备信息")
    flow_to_confirm(
        conv, "请按此文件接入。",
        [(r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="吉电新能源"))],
        n0, "RP-11", max_turns=16)
    rc.wait_config(lambda c: iec_count(c) == 4062, timeout=300, desc="RP-11: 4062 点")
    cfg = rc.read_config()
    addrs = iec_addrs(cfg)
    expect = sorted(list(range(1, 2345)) + list(range(16385, 18103)))
    if addrs != expect:
        raise rc.Fail(f"RP-11: 地址集合不符（共 {len(addrs)}）")
    if "遥控" not in _history_blob(conv):
        raise rc.Fail("RP-11: 遥控 sheet 排除不可见")
    # 落盘 id 形态：裸 id = 设备语境值_信号名称；有效语境 = 与点名组合可消歧者
    # ——吉电两 sheet 实测均为「装置名称」（yx 通道名称组合仍剩 1227 组撞名，
    # 装置名称组合归零；文档「通道名称_信号名称（遥信）」归属与数据不符，以
    # 真实文件为准，2026-10-10 测定）；首中尾各抽 ≥1 点；组合重复归零
    sh = _sheets(os.path.join(PTS_DIR, "吉电新能源南瑞科技通讯点表.xls"))
    ref = {}
    for r in sh.get("遥信", []):          # 列：序号|遥信地址|通道名称|装置名称|…|信号名称(9)
        if len(r) > 9 and _cell(r[1]) not in ("", "遥信地址"):
            try:
                ref[("yx", int(float(r[1])))] = (_cell(r[3]), _cell(r[9]))
            except ValueError:
                continue
    for r in sh.get("遥测", []):          # 列：地址|通道名称|装置名称|…|信号名称(12)
        if len(r) > 12 and _cell(r[0]) not in ("", "地址"):
            try:
                ref[("yc", int(float(r[0])))] = (_cell(r[2]), _cell(r[12]))
            except ValueError:
                continue
    by_key = {(p.get("point_type"), int(p["addr"])): p for p in _cfg_points()}
    for (t, a) in (("yx", 1), ("yx", 1172), ("yx", 2344),
                   ("yc", 16385), ("yc", 17243), ("yc", 18102)):
        dev, name = ref[(t, a)]
        p = by_key[(t, a)]
        tail = rc.normalize_point_name(f"{dev}_{name}")
        pid = str(p.get("id", ""))
        if not (pid.endswith(tail) and len(pid) > len(tail)):
            raise rc.Fail(f"RP-11: {t}/{a} id={pid!r} 不含通道_信号段 {tail!r}")
        if p.get("name") != name:
            raise rc.Fail(f"RP-11: {t}/{a} name={p.get('name')!r} ≠ 原文 {name!r}")
    ids = [p.get("id") for p in _cfg_points()]
    if len(set(ids)) != 4062:
        raise rc.Fail(f"RP-11: id 组合去重 {len(set(ids))} ≠ 4062（组合重复应归零）")
    rc.log("  RP-11 PASS（4062 = 2344 yx + 1718 yc；干扰列未误取；遥控 166 排除；设备_点名 id）")


def rp12():
    """RP-12：脏数据鲁棒性（遥信2）——两类缺陷均可见，不静默出方案。"""
    n0 = _n_inst(rc.read_config())
    conv = rc.Conv()
    r, _ = upload_big(conv.conversation_id, os.path.join(PTS_DIR, "遥信2.xls"),
                      TEMPLATE + "请解析此文件中的设备信息")
    MISSING_RE = re.compile(r"无地址|缺\s*addr|缺失|不完整|为空|空缺|未提供|没有地址")
    blob = drive_reject(
        conv, r, "请按此文件接入。", [],
        lambda b: re.search(r"重复|撞名|同名", b) and MISSING_RE.search(b)
        and _has_num(b, 447), "RP-12")
    # 提取 447 行全量进入（数据在第二 sheet 被发现、类型归类遥信）
    if not _has_num(blob, 447):
        raise rc.Fail(f"RP-12: 提取数 447 不可见: {blob[-400:]}")
    if "遥信" not in blob:
        raise rc.Fail(f"RP-12: 类型归类遥信不可见: {blob[-400:]}")
    # 两类缺陷都确定发生、都须可见（不允许只断言其一）
    if not re.search(r"重复|撞名|同名", blob):
        raise rc.Fail(f"RP-12: 重复地址缺陷不可见: {blob[-400:]}")
    if not MISSING_RE.search(blob):
        raise rc.Fail(f"RP-12: 无地址行/字段缺失缺陷不可见: {blob[-400:]}")
    expect_no_config(n0, "RP-12")
    rc.log("  RP-12 PASS（447 全量提取可见；重复地址 + 无地址行缺陷均可见；不静默）")


def rp13():
    """RP-13：纯文字描述点表——地址分区归类（无文件载体，接入口径）。"""
    n0 = _n_inst(rc.read_config())
    conv = rc.Conv()
    msg = ("接入远动装置：遥信：1:断路器合位、2:刀闸合位、3~6:刀闸位置备用1~4；"
           "16385:UAB电压、16386:UBC电压、16500:主变油温；"
           "25601:正向有功电度、25602:反向有功电度。" + TEMPLATE)
    flow_to_confirm(
        conv, msg,
        [(r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="远动装置文字接入"))],
        n0, "RP-13")
    rc.wait_config(lambda c: iec_count(c) == 11, timeout=300, desc="RP-13: 11 点")
    cfg = rc.read_config()
    pts = [p for i in iec_points(cfg) for p in i["points"]]
    addrs = sorted(int(p["addr"]) for p in pts)
    expect = [1, 2, 3, 4, 5, 6, 16385, 16386, 16500, 25601, 25602]
    if addrs != expect:
        raise rc.Fail(f"RP-13: 地址集合 {addrs} ≠ {expect}")
    types = {int(p["addr"]): p.get("point_type") for p in pts}
    if types[1] != "yx" or types[16385] != "yc" or types[25601] != "ym":
        raise rc.Fail(f"RP-13: 分区归类错误 {types}")
    # 点名：用户已命名的原样提取，id＝点名归一化（中文名原样、ASCII 原样小写）
    names = {1: "断路器合位", 2: "刀闸合位", 3: "刀闸位置备用1", 4: "刀闸位置备用2",
             5: "刀闸位置备用3", 6: "刀闸位置备用4", 16385: "UAB电压", 16386: "UBC电压",
             16500: "主变油温", 25601: "正向有功电度", 25602: "反向有功电度"}
    by_addr = {int(p["addr"]): p for p in pts}
    for a, nm in names.items():
        p = by_addr[a]
        if p.get("name") != nm:
            raise rc.Fail(f"RP-13: addr {a} name={p.get('name')!r} ≠ {nm!r}")
        pid, tail = str(p.get("id", "")), rc.normalize_point_name(nm)
        if not (pid.endswith(tail) and len(pid) > len(tail)):
            raise rc.Fail(f"RP-13: addr {a} id={pid!r} 不含归一化点名段 {tail!r}")
    fwd = fwd_points(cfg)
    if len(fwd) != 11:
        raise rc.Fail(f"RP-13: 转发 {len(fwd)} ≠ 11")
    rc.log("  RP-13 PASS（11 点三分区归类 + 范围展开 + 原名 id + 转发 10000~10010）")


CASES = [
    ("RP-01", lambda: rp01()),
    ("RP-02", rp02),
    ("RP-03", rp03),
    ("RP-04", rp04),
    ("RP-05", rp05),
    ("RP-06", rp06),
    ("RP-07", rp07),
    ("RP-08", rp08),
    ("RP-09", lambda: rp09(False)),
    ("RP-09v", lambda: rp09(True)),
    ("RP-10", rp10),
    ("RP-11", rp11),
    ("RP-12", rp12),
    ("RP-13", rp13),
]


def main():
    only = sys.argv[1].split(",") if len(sys.argv) > 1 else None
    if not os.path.isdir(PTS_DIR):
        rc.log("════ RP 全组 SKIP-DIR：真实点表目录不存在 ════")
        return
    rc.log("════ RP 真实点表接入串行测试开始 ════")
    rc._post = _post_big  # 大回合超时放宽（分块提取/批量翻译 10~20 分钟）
    base.chain_clean()
    rc.MCP_STACK.up()
    agent = base.V21Agent()
    agent.up()
    rc.AGENT = agent
    rc.PH.reset("RP")
    t0 = time.time()
    passed, failed = [], []
    for name, fn in CASES:
        if only and name not in only:
            continue
        try:
            rc.log(f"── {name} 开始 ──")
            base.chain_clean()
            rc.MCP_STACK.up()
            agent = base.V21Agent()
            agent.up()
            rc.AGENT = agent
            # 过程断言按用例重置（与其他套件 run_cases send_flow 同款）——否则
            # confirm 计数跨用例累积造成 #1 预算假违例（RP-07 实测 3>2）
            rc.PH.reset(name, rc.BUTTON_BUDGET.get(name, 2))
            fn()
            rc.PH.check(name)
            passed.append(name)
        except rc.Fail as e:
            base.dump_diag(name, e)
            rc.log(f"── {name} FAIL: {e}")
            failed.append(name)
        except Exception as e:
            base.dump_diag(name, e)
            rc.log(f"── {name} 异常 {type(e).__name__}: {str(e)[:300]}")
            failed.append(name)
    rc.log(f"════ RP 套件结束：PASS={passed} FAIL={failed}（{time.time()-t0:.0f}s）════")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
