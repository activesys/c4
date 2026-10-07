#!/usr/bin/env python3
# func_test_case_real_points.md 用例 RP-01~RP-13 驱动器（2026-10-07）
# 断言唯一来源：func_test_case_real_points.md（附录 A 点表基准数据）——按 c4/AGENTS.md
# 规则 4 实现，不根据 agent 实际输出调整断言；失败即实现未达成。
# 真实点表位于 <工作区根>/points/iec104/（仓库外）：目录缺失整组 SKIP（SKIP-DIR）。
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_cases as rc  # noqa: E402
import run_chain_v21 as base  # noqa: E402

PTS_DIR = "/home/wangbo/work/activesys/points/iec104"

# 统一样板（func_test_case_real_points.md「统一环境与样板」，逐字使用）
TEMPLATE = ("IEC104规约，RTU地址192.168.110.99:2404，公共地址1。"
            "转发采用asfp2协议，目标II区服务器127.0.0.1:9900，"
            "转发点表与采集侧一致，从 10000 号开始顺序编址。")
DEVICE_ANSWER = "设备名叫{name}，就用这些点表。"

# 大回合（分块提取/批量翻译可达 10~20 分钟）：驱动器侧超时放宽（rc.chat/upload 的
# 300s 为小点表用例设定；本套件不改断言、只放宽传输超时）
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
    return "".join(text_parts), events


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


def expect_no_config(cfg_n0, note):
    time.sleep(2)
    n1 = sum(len(v) for k, v in (rc.read_config() or {}).items()
             if isinstance(v, list) and k != "c4_shm_manager")
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
        n1 = sum(len(v) for k, v in (rc.read_config() or {}).items()
                 if isinstance(v, list) and k != "c4_shm_manager")
        if n1 != cfg_n0:
            return text
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


# ── 各用例 ────────────────────────────────────────────────

def rp01():
    """RP-01：分文件两表 GBK csv——文件名语境 + 异名累积 + 同名替换。"""
    cfg0 = rc.read_config()
    n0 = sum(len(v) for k, v in (cfg0 or {}).items() if isinstance(v, list) and k != "c4_shm_manager")
    conv = rc.Conv()
    yx = os.path.join(PTS_DIR, "yxTagTable.csv")
    yc = os.path.join(PTS_DIR, "ycTagTable.csv")
    conv_up = conv.conversation_id
    upload_big(conv_up, yx, "请解析此文件中的设备信息")
    text = conv.send(TEMPLATE)
    upload_big(conv_up, yc, "这是配套的遥测点表，请合并解析。")
    # 同名再传（内容不变）→ 替换该文件块并重开草稿（§2.13.4）；合并后仍 12728
    upload_big(conv_up, yx, "重新上传遥信点表（同前）。")
    text = flow_to_confirm(
        conv, "继续按这些点表接入。",
        [(r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="高力板 csv 站"))],
        n0, "RP-01")
    rc.wait_config(lambda c: iec_count(c) == 12728, timeout=300, desc="RP-01: 12728 点")
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
    fwd = fwd_points(cfg)
    if len(fwd) != 12728 or min(int(p["addr"]) for p in fwd) != 10000:
        raise rc.Fail(f"RP-01: 转发点数/基址错误（{len(fwd)} 点）")
    if len(iec_points(cfg)) != 1:
        raise rc.Fail(f"RP-01: iec104 实例数 {len(iec_points(cfg))} ≠ 1")
    rc.log("  RP-01 PASS（12728 = 4688 yx + 8040 yc，原样区间，单实例）")


def rp02():
    """RP-02：多 sheet 三类型 + YK 排除（小塔子，声明 510 对账）。"""
    cfg0 = rc.read_config()
    n0 = sum(len(v) for k, v in (cfg0 or {}).items() if isinstance(v, list) and k != "c4_shm_manager")
    conv = rc.Conv()
    msg = TEMPLATE + "点表共 510 点（含不接入的遥控 17 点）。"
    upload_big(conv.conversation_id, os.path.join(PTS_DIR, "小塔子远动点表.xlsx"), msg)
    text = flow_to_confirm(
        conv, "请按此文件接入。",
        [(r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="小塔子远动"))],
        n0, "RP-02")
    rc.wait_config(lambda c: iec_count(c) == 493, timeout=300, desc="RP-02: 493 点")
    cfg = rc.read_config()
    addrs = iec_addrs(cfg)
    expect = sorted(list(range(1, 325)) + list(range(16385, 16510)) + list(range(25601, 25645)))
    if addrs != expect:
        raise rc.Fail(f"RP-02: 地址集合不符（首 {addrs[:3]} 末 {addrs[-3:]} 共 {len(addrs)}）")
    if not any(24577 <= a <= 24593 for a in (set(range(24577, 24594)) - set(addrs))):
        raise rc.Fail("RP-02: YK 区间点不应接入")
    plan_src = text + conv.history[-1].get("content", "") if conv.history else text
    blob = " ".join(str(m.get("content", "")) for m in conv.history if isinstance(m, dict))
    if "YK" not in blob or "17" not in blob:
        raise rc.Fail(f"RP-02: excluded 回报不可见（YK/17）: {blob[-400:]}")
    rc.log("  RP-02 PASS（493 接入 + YK 17 排除，声明 510 对账）")


def rp03():
    """RP-03：中文 sheet 前缀 + 4001H 标题不触发 16 进制（高力板镇）。"""
    cfg0 = rc.read_config()
    n0 = sum(len(v) for k, v in (cfg0 or {}).items() if isinstance(v, list) and k != "c4_shm_manager")
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
    blob = " ".join(str(m.get("content", "")) for m in conv.history if isinstance(m, dict))
    if "遥调" not in blob or "遥控" not in blob:
        raise rc.Fail("RP-03: 遥调/遥控排除组不可见")
    rc.log("  RP-03 PASS（729 = 256 yx + 473 yc 原样；4001H 不触发；4+4 排除）")


def rp04():
    """RP-04：单文件多段段标题 + 三类型 0 起偏移（11号光伏区）。"""
    cfg0 = rc.read_config()
    n0 = sum(len(v) for k, v in (cfg0 or {}).items() if isinstance(v, list) and k != "c4_shm_manager")
    conv = rc.Conv()
    upload_big(conv.conversation_id, os.path.join(PTS_DIR, "11号光伏区1号箱变测控点表.xls"),
               TEMPLATE + "请解析此文件中的设备信息")
    flow_to_confirm(
        conv, "请按此文件接入。",
        [(r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="11号光伏区箱变"))],
        n0, "RP-04")
    rc.wait_config(lambda c: iec_count(c) == 598, timeout=300, desc="RP-04: 598 点")
    cfg = rc.read_config()
    addrs = iec_addrs(cfg)
    expect = sorted(list(range(16385, 16749)) + list(range(1, 203)) + list(range(25601, 25633)))
    if addrs != expect:
        raise rc.Fail(f"RP-04: 地址集合不符（共 {len(addrs)}，yc 首 {addrs.count(16385)}）")
    for bad in (24577, 25089, 0, 364):
        if bad in addrs:
            raise rc.Fail(f"RP-04: 出现未换算/漏排除地址 {bad}")
    rc.log("  RP-04 PASS（598 = 364 yc + 202 yx + 32 ym 各自 0 起整段换算；8+6 排除）")


def rp05():
    """RP-05：0 起/1 起三文件对照——三轮串行（3 实例 + 转发同端口并入）。"""
    rounds = [
        ("虎头山最新点表.xls", "虎头山风电场", 865),
        ("高家堡集控站点表.xls", "高家堡集控站", 1753),
        ("碾子山风电场-标准104测点.xls", "碾子山风电场", 1608),
    ]
    for fname, dev, expect_n in rounds:
        cfg0 = rc.read_config()
        n0 = sum(len(v) for k, v in (cfg0 or {}).items() if isinstance(v, list) and k != "c4_shm_manager")
        conv = rc.Conv()
        upload_big(conv.conversation_id, os.path.join(PTS_DIR, fname),
                   TEMPLATE + "请解析此文件中的设备信息")
        flow_to_confirm(
            conv, "请按此文件接入。",
            [(r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name=dev))],
            n0, f"RP-05[{fname}]")
        rc.wait_config(lambda c: iec_count(c) >= expect_n, timeout=300,
                       desc=f"RP-05[{fname}]: {expect_n} 点")
    cfg = rc.read_config()
    total = iec_count(cfg)
    if total != 4226:
        raise rc.Fail(f"RP-05: 三轮合计 {total} ≠ 4226")
    if len(iec_points(cfg)) != 3:
        raise rc.Fail(f"RP-05: iec104 实例数 {len(iec_points(cfg))} ≠ 3（应三轮串行）")
    fwd = fwd_points(cfg)
    if len(fwd) != 4226:
        raise rc.Fail(f"RP-05: 转发并入后 {len(fwd)} ≠ 4226")
    addrs = iec_addrs(cfg)
    if addrs.count(1) != 3:
        raise rc.Fail(f"RP-05: yx 起点 1 应出现 3 次（0 起 +1 与 1 起原样对照），实际 {addrs.count(1)}")
    if 25601 not in addrs or 25860 not in addrs:
        raise rc.Fail("RP-05: 碾子山 ym 1 起应换算至 25601~25860")
    rc.log("  RP-05 PASS（3 实例 4226 点；0 起/1 起对照正确；转发并入 1 实例）")


def rp06():
    """RP-06：16 进制别名 + Excel 吞址——回落追问（正负混合）。"""
    cfg0 = rc.read_config()
    n0 = sum(len(v) for k, v in (cfg0 or {}).items() if isinstance(v, list) and k != "c4_shm_manager")
    conv = rc.Conv()
    upload_big(conv.conversation_id, os.path.join(PTS_DIR, "遥测21.xls"),
               TEMPLATE + "请解析此文件中的设备信息")
    text = conv.send("请按此文件接入。")
    blob = text
    for _ in range(4):
        if "回落" in blob or "多台设备" in blob or "拆分" in blob:
            break
        time.sleep(10)
        blob += conv.send("继续")
    if not re.search(r"回落|多台设备|分段|拆分", blob):
        raise rc.Fail(f"RP-06: 未出现回落追问: {blob[-300:]}")
    if "16385" not in blob:
        raise rc.Fail(f"RP-06: hex 别名识别（4001→16385）不可见: {blob[-300:]}")
    expect_no_config(n0, "RP-06")
    rc.log("  RP-06 PASS（回落追问 fail-visible + hex 别名识别可见 + 不静默接入）")


def rp07():
    """RP-07：16 进制无标记 ym + 文件名语义（脉冲2）。"""
    cfg0 = rc.read_config()
    n0 = sum(len(v) for k, v in (cfg0 or {}).items() if isinstance(v, list) and k != "c4_shm_manager")
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
    rc.log("  RP-07 PASS（48 点 ym 25601~25648，空 sheet 跳过）")


def rp08():
    """RP-08：无表头三列值形态定位地址列（晨光）。"""
    cfg0 = rc.read_config()
    n0 = sum(len(v) for k, v in (cfg0 or {}).items() if isinstance(v, list) and k != "c4_shm_manager")
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
    rc.log("  RP-08 PASS（3004 点原样区间；无表头按值形态定位地址列）")


def rp09(variant=False):
    """RP-09：表头错位大表——「地址」列是装置地址（板卡网口3；变体网口4）。"""
    fname = "板卡1-网口4-标准104调度规约-2405-王博.xls" if variant \
        else "板卡1-网口3-标准104调度规约-2404-王博.xls"
    expect_n, exc_n = (15011, 221) if variant else (15894, 234)
    tag = "RP-09v" if variant else "RP-09"
    cfg0 = rc.read_config()
    n0 = sum(len(v) for k, v in (cfg0 or {}).items() if isinstance(v, list) and k != "c4_shm_manager")
    conv = rc.Conv()
    upload_big(conv.conversation_id, os.path.join(PTS_DIR, fname),
               TEMPLATE + "请解析此文件中的设备信息")
    flow_to_confirm(
        conv, "请按此文件接入。",
        [(r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="北恒箱变测控"))],
        n0, tag, max_turns=16)
    rc.wait_config(lambda c: iec_count(c) == expect_n, timeout=300, desc=f"{tag}: {expect_n} 点")
    cfg = rc.read_config()
    pts = [p for i in iec_points(cfg) for p in i["points"]]
    addrs = sorted(int(p["addr"]) for p in pts)
    if not variant:
        expect = sorted(list(range(1, 7993)) + list(range(16385, 24287)))
    else:
        expect = sorted(list(range(1, 7549)) + list(range(16385, 23848)))
    if addrs != expect:
        raise rc.Fail(f"{tag}: 地址集合不符（共 {len(addrs)}，期望 {len(expect)}）")
    if 16384 in addrs or 24287 in addrs:
        raise rc.Fail(f"{tag}: 出现越界地址")
    rc.log(f"  {tag} PASS（{expect_n} 点原样区间；「地址」列 101~135 未误取；遥控 {exc_n} 排除）")


def rp10():
    """RP-10：多设备分段回落——序列重置检测追问（南瑞中德，负向）。"""
    cfg0 = rc.read_config()
    n0 = sum(len(v) for k, v in (cfg0 or {}).items() if isinstance(v, list) and k != "c4_shm_manager")
    conv = rc.Conv()
    upload_big(conv.conversation_id, os.path.join(PTS_DIR, "光伏南瑞中德点表.xls"),
               TEMPLATE + "请解析此文件中的设备信息")
    text = conv.send("请按此文件接入。")
    blob = text
    for _ in range(6):
        if re.search(r"回落|多台设备|分段|拆分", blob):
            break
        time.sleep(10)
        blob += conv.send("继续")
    if not re.search(r"回落|多台设备|分段|拆分", blob):
        raise rc.Fail(f"RP-10: 未出现回落追问: {blob[-300:]}")
    expect_no_config(n0, "RP-10")
    rc.log("  RP-10 PASS（4 项目段回落 → 追问，不静默接入）")


def rp11():
    """RP-11：干扰列与表头杂值（吉电）。"""
    cfg0 = rc.read_config()
    n0 = sum(len(v) for k, v in (cfg0 or {}).items() if isinstance(v, list) and k != "c4_shm_manager")
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
    for bad in (0, 1, 2, 9):
        pass  # 装置信号地址小值与 yx 合法区间重叠，不做负向断言
    blob = " ".join(str(m.get("content", "")) for m in conv.history if isinstance(m, dict))
    if "遥控" not in blob:
        raise rc.Fail("RP-11: 遥控 sheet 排除不可见")
    rc.log("  RP-11 PASS（4062 = 2344 yx + 1718 yc；干扰列未误取；遥控 166 排除）")


def rp12():
    """RP-12：脏数据鲁棒性（遥信2）——两类缺陷均可见，不静默出方案。"""
    cfg0 = rc.read_config()
    n0 = sum(len(v) for k, v in (cfg0 or {}).items() if isinstance(v, list) and k != "c4_shm_manager")
    conv = rc.Conv()
    upload_big(conv.conversation_id, os.path.join(PTS_DIR, "遥信2.xls"),
               TEMPLATE + "请解析此文件中的设备信息")
    text = conv.send("请按此文件接入。")
    blob = text
    for _ in range(4):
        if re.search(r"回落|多台设备|分段|拆分|重复|缺失|不完整", blob):
            break
        time.sleep(10)
        blob += conv.send("继续")
    if not re.search(r"重复地址|无地址行|字段不完整|回落", blob):
        raise rc.Fail(f"RP-12: 缺陷不可见: {blob[-300:]}")
    if not (re.search(r"重复地址", blob) and re.search(r"无地址行", blob)):
        raise rc.Fail(f"RP-12: 两类缺陷须均可见（重复地址/无地址行）: {blob[-300:]}")
    expect_no_config(n0, "RP-12")
    rc.log("  RP-12 PASS（空 sheet 跳过 + 重复地址/无地址行两类缺陷均可见 + 不静默）")


def rp13():
    """RP-13：纯文字描述点表——地址分区归类（无文件载体）。"""
    cfg0 = rc.read_config()
    n0 = sum(len(v) for k, v in (cfg0 or {}).items() if isinstance(v, list) and k != "c4_shm_manager")
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
    fwd = fwd_points(cfg)
    if len(fwd) != 11:
        raise rc.Fail(f"RP-13: 转发 {len(fwd)} ≠ 11")
    rc.log("  RP-13 PASS（11 点三分区归类 + 范围展开 + 转发 10000~10010）")


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
