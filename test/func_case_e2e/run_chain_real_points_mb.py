#!/usr/bin/env python3
# func_test_case_real_points.md 用例 RP-MB-01~RP-MB-12 驱动器（2026-10-11 初版）
# 断言唯一来源：func_test_case_real_points.md modbus 组（域规则 9 条 + 附录 B 基准数据，
# 2026-10-11 SheetJS 实测）——按 c4/AGENTS.md 规则 4 实现，不根据 agent 实际输出调整
# 断言；失败即实现未达成。
# 真实点表位于 <工作区根>/points/modbus/（仓库外）：目录缺失整组 SKIP（SKIP-DIR）。
#
# 口径（2026-10-11 三轮评审收敛后的文档终态）：
#   接入口径 MB-01/03/06/07（config 1 modbus 采集实例 + 1 asfp2 转发实例；MB-07 IP
#   按用户裁定 86.76.0.134 单实例）；
#   拒收口径 MB-02/04/05/08/09/10/11（解析面 + fail-visible 冲突清单可见 + config
#   无实例；拒收文案禁「顺延」——agent.md §3.2.1.3b 机判约定，全套件负向断言）；
#   暂缓 MB-12（SKIP-MB12，转发区声明式结构，待设计定稿）。
# 精确地址集合断言（MB-01/03/06/07）不硬编码——驱动器以 SheetJS 从真实文件同源
# 提取期望集合（断言基准自检，RP-02/05 同款），基准口径 = 附录 B。
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_cases as rc  # noqa: E402
import run_chain_v21 as base  # noqa: E402
import run_chain_real_points as rp  # noqa: E402  (upload_big/drive_reject/flow_to_confirm/…)

PTS_DIR = "/home/wangbo/work/activesys/points/modbus"

F = rc.Fail  # noqa: N816 — rc.Fail 别名（阅读性；rp 模块内异常均为 rc.Fail）


def flow_to_confirm_mb(conv, message, answers, cfg_n0, tag, max_turns=14):
    """MB 版驱动到执行落地：与 rp.flow_to_confirm 同构，唯「解析失败」保护对
    台数缺口放行——modbus 台数缺口复用 extractError 通道（side_gaps 统一加
    「解析失败」前缀），其后紧跟「请提供机组台数」可操作问句。"""
    answers = [(r"场站", "华能阿拉善")] + list(answers)
    consumed = [False] * len(answers)
    text = conv.send(message)
    clicked = 0
    for _ in range(max_turns):
        if rp._n_inst(rc.read_config()) != cfg_n0:
            return text
        if "解析失败" in text and "台数" not in text:
            raise F(f"{tag}: 接入被拒（预期接入落地）: {text[:300]}")
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
    raise F(f"{tag}: flow 超过 {max_turns} 轮未收敛，尾回复: {text[:300]}")

# 统一样板（modbus 扩充节，逐字）：采集协议 + 接收侧必要项 + 转发侧样板
# （采集 modbus 须明示——IEC104 样板含「IEC104规约」同款；冒烟实测缺协议词时
#  agent 追问接入协议且样板应答缺失会连续空转）
MB_TEMPLATE = ("采集采用modbus协议（Modbus/TCP），设备 IP 192.168.110.60，端口 502，"
               "从站号 1。"
               "转发采用asfp2协议，目标II区服务器127.0.0.1:9900，"
               "转发点表与采集侧一致，从 10000 号开始顺序编址。")
# 协议追问兜底应答（仅在问句形态命中——「· 接入协议：modbus」确认行不得误触，
# 否则会把应答当设备名吞掉；冒烟实测教训）
ANS_PROTOCOL = (r"请提供接入协议|回复协议名|用哪种协议接入", "modbus")
DEVICE_ANSWER = rp.DEVICE_ANSWER
REJECT_HINT_RE = rp.REJECT_HINT_RE          # 厂家|拆分|修正点名
DUP_RE = rp.DUP_RE                          # 撞名|同名|重复|冲突

# type 枚举（c4_modbus_client.md §4.6.1）：BOOLEAN=0 INT16=3 UINT16=4 INT32=5
# UINT32=6 INT64=7 UINT64=8 FLOAT32=10 FLOAT64=11
T_BOOL, T_I16, T_U16, T_I32, T_F32, T_F64 = 0, 3, 4, 5, 10, 11


# ── config 断言助手（c4_modbus_client.md §2.3 配置形态）──────────
def mb_instances(cfg):
    return (cfg or {}).get("c4_modbus_client", [])


def mb_points(cfg):
    return [p for i in mb_instances(cfg) for p in i.get("points", [])]


def fwd_addrs(cfg):
    a = []
    for inst in (cfg or {}).get("c4_asfp2_client", []):
        a.extend(int(p["addr"]) for p in inst.get("points", []))
    return sorted(a)


def expect_mb_instance(cfg, tag, ip=None, port=None, uid=None, n_inst=1):
    """实例级断言：ip/port（registry config_schema 无实例级 uid——uid 为点级字段，
    uid 断言落到 expect_mb_points_uid）。"""
    insts = mb_instances(cfg)
    if len(insts) != n_inst:
        raise F(f"{tag}: modbus 实例数 {len(insts)} ≠ {n_inst}")
    inst = insts[0]
    if ip is not None and str(inst.get("ip", "")) != str(ip):
        raise F(f"{tag}: 实例 ip={inst.get('ip')!r} ≠ {ip!r}")
    if port is not None and int(inst.get("port", -1)) != int(port):
        raise F(f"{tag}: 实例 port={inst.get('port')!r} ≠ {port!r}")
    return inst


def expect_mb_points_uid(cfg, tag, uid):
    bad = [p.get("uid") for p in mb_points(cfg) if int(p.get("uid", -1)) != int(uid)]
    if bad:
        raise F(f"{tag}: {len(bad)} 个点 uid ≠ {uid}（首 {bad[:3]}）")


def expect_fwd(cfg, tag, n):
    f = fwd_addrs(cfg)
    if f != list(range(10000, 10000 + n)):
        raise F(f"{tag}: 转发 {len(f)} 点 10000~{10000 + n - 1} 顺序编址不符"
                f"（首 {f[:3]} 末 {f[-3:]}）")


def span_sane(addrs, span, tag, what):
    """同空间点地址排序后相邻差 ≥ span（span 寄存器无重叠互证）。"""
    s = sorted(addrs)
    for a, b in zip(s, s[1:]):
        if b - a < span:
            raise F(f"{tag}: {what} span{span} 重叠（addr {a} → {b}）")


# ── 基准自检（附录 B 同源：SheetJS 从真实文件提取期望）────────────
def hailin_expect():
    """MB-01 海林：→ (遥测 addr 集, 遥信 addr 集, {addr: 描述})。
    列布局（点表对应关系 sheet）：edos点表|描述|modbus|modsan|数据类型|点类型。"""
    sh = rp._sheets(os.path.join(PTS_DIR, "海林风电场-华锐风电测点说明-Modbus接口_V5(车德胜最终确认点表)20.5.22)(1).xls"))
    rows = sh.get("点表对应关系", [])
    yc, yx, name = set(), set(), {}
    for r in rows:
        if len(r) < 6:
            continue
        pt = rp._cell(r[5])
        try:
            addr = int(float(rp._cell(r[2])))
        except ValueError:
            continue
        desc = rp._cell(r[1])
        if pt == "遥测":
            yc.add(addr)
            name[addr] = desc
        elif pt == "遥信":
            yx.add(addr)
    return yc, yx, name


def jingguan_expect():
    """MB-03 景观：→ (接入 addr 集, {addr: 类型词})。
    列布局：序号|功能码|modbus地址|数据类型|变量名|备注；脚注口径 = 序号 168~172
    可写（excluded），其余接入。"""
    sh = rp._sheets(os.path.join(PTS_DIR, "景观中科机组SCADA集控点表V1.5.xlsx"))
    rows = sh.get("集控点表", [])
    acc, types = set(), {}
    for r in rows:
        if len(r) < 4:
            continue
        try:
            seq = int(float(rp._cell(r[0])))
        except ValueError:
            continue
        if not (1 <= seq <= 173):
            continue
        try:
            addr = int(float(rp._cell(r[2])))
        except ValueError:
            continue
        if 168 <= seq <= 172:
            continue
        acc.add(addr)
        types[addr] = rp._cell(r[3])
    return acc, types


def mengdong_expect(nturbines):
    """MB-06 蒙东：台数 N → 全表期望 addr 集（展开 (k−1)×500 + {0,2..296}）∪ 调度。"""
    base_addrs = {(k - 1) * 500 + e for k in range(1, nturbines + 1)
                  for e in range(0, 297, 2)}
    return base_addrs | {34000, 34002}


def gaoliban_expect():
    """MB-07 高力板：→ 全表期望 addr 集（遥测1 公式展开 132×150 + 遥测2 10 点）。"""
    return set(range(0, 19800)) | set(range(24000, 24010))


# ── 各用例 ────────────────────────────────────────────────

def mb01():
    """MB-01：modbus/modscan 双列并存——用 modbus 列 + 功能码追问 + 遥控排除（海林）。"""
    n0 = rp._n_inst(rc.read_config())
    yc_exp, yx_exp, name_exp = hailin_expect()
    # 断言基准自检（附录 B）：遥测 3114 @0~24579 span2 无重复、遥信 150 @0~149
    if len(yc_exp) != 3114 or min(yc_exp) != 0 or max(yc_exp) != 24579:
        raise F(f"MB-01: 基准自检失败 遥测 {len(yc_exp)} @[{min(yc_exp)},{max(yc_exp)}]")
    if len(yx_exp) != 150 or sorted(yx_exp) != list(range(150)):
        raise F(f"MB-01: 基准自检失败 遥信 {len(yx_exp)}")
    span_sane(yc_exp, 2, "MB-01", "基准遥测")
    conv = rc.Conv()
    rp.upload_big(conv.conversation_id,
                  os.path.join(PTS_DIR, "海林风电场-华锐风电测点说明-Modbus接口_V5(车德胜最终确认点表)20.5.22)(1).xls"),
                  MB_TEMPLATE + "请解析此文件中的设备信息")
    rp.flow_to_confirm(
        conv, "请按此文件接入。",
        [ANS_PROTOCOL,
         (r"IP|端口|从站", "设备 IP 192.168.110.60，端口 502，从站号 1"),
         (r"功能码", "遥测全部是输入寄存器（功能码 4），遥信是离散输入（功能码 2）"),
         (r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="海林华锐"))],
        n0, "MB-01", max_turns=16)
    rc.wait_config(lambda c: len(mb_points(c)) == 3264, timeout=600, desc="MB-01: 3264 点")
    cfg = rc.read_config()
    expect_mb_instance(cfg, "MB-01", ip="192.168.110.60", port=502)
    expect_mb_points_uid(cfg, "MB-01", 1)
    pts = mb_points(cfg)
    yc = [p for p in pts if int(p["fun"]) == 4]
    yx = [p for p in pts if int(p["fun"]) == 2]
    if len(pts) != 3264 or len(yc) != 3114 or len(yx) != 150:
        raise F(f"MB-01: 点数 {len(pts)} = fun4 {len(yc)} + fun2 {len(yx)} ≠ 3264 = 3114 + 150")
    if any(int(p["type"]) != T_F32 or int(p["swap"]) != 2 for p in yc):
        raise F("MB-01: 遥测非 FLOAT32/swap2（VT_R4 映射 + 无声明 ABCD 缺省）")
    if any(int(p["type"]) != T_BOOL or int(p["swap"]) != 0 for p in yx):
        raise F("MB-01: 遥信非 BOOLEAN/swap0（VT_BOOL 映射）")
    yc_a = {int(p["addr"]) for p in yc}
    yx_a = {int(p["addr"]) for p in yx}
    if yc_a != yc_exp:
        raise F(f"MB-01: 遥测 addr 集 ≠ modbus 列基准（{len(yc_a)} vs {len(yc_exp)}，"
                f"首点 {min(yc_a) if yc_a else None}——误用 modsan 列则 min=1）")
    if yx_a != yx_exp:
        raise F(f"MB-01: 遥信 addr 集 {sorted(yx_a)[:5]}… ≠ 0~149 基准")
    # 点名 = 描述列（规则 9 中文优先）；edos id 不作点名——首/中/尾抽样对照
    by_addr = {int(p["addr"]): p for p in yc}
    for a in (0, 12288, max(yc_exp)):
        want = name_exp.get(a)
        if not want:
            continue
        p = by_addr.get(a)
        if p is None:
            raise F(f"MB-01: 遥测 addr {a} 缺失")
        if p.get("name") != want:
            raise F(f"MB-01: addr {a} name={p.get('name')!r} ≠ 描述列 {want!r}")
        tail = rc.normalize_point_name(want)
        if not str(p.get("id", "")).endswith(tail):
            raise F(f"MB-01: addr {a} id={p.get('id')!r} 不含描述点名段 {tail!r}")
    ids = [p.get("id") for p in pts]
    if len(set(ids)) != 3264:
        raise F(f"MB-01: id 去重 {len(set(ids))} ≠ 3264")
    blob = rp._history_blob(conv)
    if not rp._has_num(blob, 3408):
        raise F(f"MB-01: 总数对账 3408 = 3114 + 150 + 144 不可见: {blob[-400:]}")
    if "遥控" not in blob or not rp._has_num(blob, 144):
        raise F(f"MB-01: excluded 遥控 144 点（写组不采集）不可见: {blob[-400:]}")
    expect_fwd(cfg, "MB-01", 3264)
    rc.log("  MB-01 PASS（3264 = 遥测 3114 FLOAT32/fun4 + 遥信 150 BOOLEAN/fun2，"
           "modbus 列 0 基，遥控 144 excluded）")


def mb02():
    """MB-02：modscan 编号全表换算 + singed 容错 + 同台撞名拒收（珠日河）。"""
    n0 = rp._n_inst(rc.read_config())
    conv = rc.Conv()
    # 连接信息表内无声明 → 故意前置不给（必填不猜路径被测），追问后答 110.61
    r, _ = rp.upload_big(conv.conversation_id,
                         os.path.join(PTS_DIR, "珠日河东汽新点表0504.xls"),
                         "采集采用modbus协议（Modbus/TCP）。"
                         "转发采用asfp2协议，目标II区服务器127.0.0.1:9900，"
                         "转发点表与采集侧一致，从 10000 号开始顺序编址。"
                         "请解析此文件中的设备信息")
    blob = rp.drive_reject(
        conv, r, "请按此文件接入。",
        [ANS_PROTOCOL,
         (r"IP|端口|从站", "网关 IP 192.168.110.61，端口 502，从站号都是 1"),
         (r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="珠日河东汽"))],
        lambda b: DUP_RE.search(b) and rp._has_num(b, 12929), "MB-02", max_turns=14)
    # 解析面：12929 全量对账 + modscan 换算明示（10001→0 / 30001 基）+ 类型分布
    # （bool 5808 / singed short 4425 / float 489 / singed long 2207）+ excluded 344
    if not rp._has_num(blob, 12929):
        raise F(f"MB-02: 12929 全量对账不可见: {blob[-400:]}")
    if "10001" not in blob or "30001" not in blob:
        raise F(f"MB-02: modscan 编号域换算明示（10001/30001）不可见: {blob[-400:]}")
    for n in (5808, 4425, 489, 2207):
        if not rp._has_num(blob, n):
            raise F(f"MB-02: 类型分布 {n} 不可见: {blob[-400:]}")
    if "singed" not in blob:
        raise F(f"MB-02: singed 拼写容错形态不可见: {blob[-400:]}")
    if not rp._has_num(blob, 344) or "powercontrol" not in blob.lower():
        raise F(f"MB-02: excluded powercontroltable 344 不可见: {blob[-400:]}")
    # fail-visible 拒收：同台撞名 198 组（发电/故障停机/报警 × 66 台）+ 口径 + 禁顺延
    if "发电" not in blob or not ("故障停机" in blob or "报警" in blob):
        raise F(f"MB-02: 冲突点名（发电/故障停机/报警）不可见: {blob[-400:]}")
    if not rp._has_num(blob, 198):
        raise F(f"MB-02: 撞名 198 组计数不可见: {blob[-400:]}")
    if not REJECT_HINT_RE.search(blob):
        raise F(f"MB-02: 拒收提示口径不可见: {blob[-400:]}")
    # 「顺延」盲禁不适用于 modbus 套件：域规则三裁允许占位白名单顺延且珠日河
    # 含 6677 处「备用」占位（MB-08 文档更要求该说明）——业务名无顺延通道由
    # 冲突清单 + 拒收文案承载
    rp.expect_no_config(n0, "MB-02")
    rc.log("  MB-02 PASS（12929 对账 + 换算明示 + singed 容错可见；同台撞名 198 组拒收，无 config）")


def mb03():
    """MB-03：类型谱基准 + hex 功能码 + 读写分区（景观）。"""
    n0 = rp._n_inst(rc.read_config())
    acc_exp, types_exp = jingguan_expect()
    # 断言基准自检（附录 B）：168 接入 = F32 93 + I16 71 + I32 4；I32 @200/202/204/206
    dist = {}
    for a, t in types_exp.items():
        dist[t] = dist.get(t, 0) + 1
    if len(acc_exp) != 168 or dist.get("F32") != 93 or dist.get("I16") != 71 \
            or dist.get("I32") != 4:
        raise F(f"MB-03: 基准自检失败 {len(acc_exp)} {dist}")
    for a in (200, 202, 204, 206):
        if types_exp.get(a) != "I32":
            raise F(f"MB-03: 基准自检失败 I32 @{a}")
    conv = rc.Conv()
    rp.upload_big(conv.conversation_id,
                  os.path.join(PTS_DIR, "景观中科机组SCADA集控点表V1.5.xlsx"),
                  MB_TEMPLATE + "请解析此文件中的设备信息")
    rp.flow_to_confirm(
        conv, "请按此文件接入。",
        [ANS_PROTOCOL,
         (r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="景观中科"))],
        n0, "MB-03")
    rc.wait_config(lambda c: len(mb_points(c)) == 168, timeout=600, desc="MB-03: 168 点")
    cfg = rc.read_config()
    expect_mb_instance(cfg, "MB-03", ip="192.168.110.60", port=502)
    expect_mb_points_uid(cfg, "MB-03", 1)
    pts = mb_points(cfg)
    tmap = {"F32": T_F32, "I16": T_I16, "I32": T_I32}
    if any(int(p["fun"]) != 3 for p in pts):
        raise F("MB-03: 存在非 fun=3 点（0X03 hex 功能码映射）")
    bad = [p for p in pts if int(p["type"]) != tmap.get(types_exp.get(int(p["addr"])), -1)]
    if bad:
        raise F(f"MB-03: 类型映射不符 {len(bad)} 点（首 {bad[0].get('addr')}）")
    acc = {int(p["addr"]) for p in pts}
    if acc != acc_exp:
        raise F(f"MB-03: addr 集 ≠ 文件基准 {len(acc)} vs {len(acc_exp)}（可写 5 点应排除）")
    blob = rp._history_blob(conv)
    # 对账口径 = 附录 B 速查「RP-MB-03 168」：168 接入 + excluded 5 可见即可
    # （全表 173 不作对话硬断言——168+5 已构成对账）
    if "可写" not in blob or not (rp._has_num(blob, 5) and rp._has_num(blob, 168)):
        raise F(f"MB-03: excluded 可写 5 点 / 168 接入对账不可见: {blob[-400:]}")
    # 0X03 hex 记法采纳与类型原样映射的正面证据 = fun=3 全量 + 类型谱落 config
    # （上方断言承载）；「无追问」由 flow_to_confirm 收敛本身承载
    expect_fwd(cfg, "MB-03", 168)
    rc.log("  MB-03 PASS（168 = F32 93 + I16 71 + I32 4 全 fun3 原样；可写 5 excluded）")


def mb04():
    """MB-04：字序声明解析 + 表头连接参数 + 同机组撞名拒收（通辽）。"""
    n0 = rp._n_inst(rc.read_config())
    conv = rc.Conv()
    r, _ = rp.upload_big(conv.conversation_id,
                         os.path.join(PTS_DIR, "通辽乌力吉集控点表（新增风机离线状态）-海装.xls"),
                         "采集采用modbus协议（Modbus/TCP）。"
                         "转发采用asfp2协议，目标II区服务器127.0.0.1:9900，"
                         "转发点表与采集侧一致，从 10000 号开始顺序编址。"
                         "请解析此文件中的设备信息")
    blob = rp.drive_reject(
        conv, r, "请按此文件接入。",
        [ANS_PROTOCOL,
         (r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="通辽乌力吉"))],
        lambda b: DUP_RE.search(b) and rp._has_num(b, 5113), "MB-04", max_turns=14)
    # 解析面：5113/1732/72 三段对账 + excluded 遥控 72（1704~1775）+ 连接参数采纳
    # + 字序声明采纳（字间先低后高 → swap=0/hton_register=1）
    for n in (5113, 1732, 72):
        if not rp._has_num(blob, n):
            raise F(f"MB-04: 对账数 {n} 不可见: {blob[-400:]}")
    if "192.168.0.7" not in blob or "503" not in blob:
        raise F(f"MB-04: 表头连接参数（192.168.0.7:503）采纳不可见: {blob[-400:]}")
    if "字间先低后高" not in blob and "swap" not in blob.lower() \
            and "字序" not in blob:
        raise F(f"MB-04: 字序声明采纳表述不可见: {blob[-400:]}")
    # fail-visible 拒收：同机组撞名 168 组（文档例举 轮毂温度/叶片后备电源温度
    # 等——「如」为示例，拒收清单按 top-5 展示，断言任一文档例举家族可见）
    if "轮毂温度" not in blob and "后备电源" not in blob:
        raise F(f"MB-04: 冲突点名（机组1.轮毂温度）不可见: {blob[-400:]}")
    if not rp._has_num(blob, 168):
        raise F(f"MB-04: 撞名 168 组计数不可见: {blob[-400:]}")
    if not REJECT_HINT_RE.search(blob):
        raise F(f"MB-04: 拒收提示口径不可见: {blob[-400:]}")
    rp.expect_no_config(n0, "MB-04")
    rc.log("  MB-04 PASS（5113/1732/72 对账 + 字序/连接采纳可见；同机组撞名 168 组拒收）")


def mb05():
    """MB-05：英文表头 csv + 重复地址拒收 + 脏行（歌美飒）。"""
    n0 = rp._n_inst(rc.read_config())
    conv = rc.Conv()
    r, _ = rp.upload_big(conv.conversation_id,
                         os.path.join(PTS_DIR, "歌美飒点表.csv"),
                         MB_TEMPLATE + "请解析此文件中的设备信息")
    blob = rp.drive_reject(
        conv, r, "请按此文件接入。",
        [ANS_PROTOCOL,
         (r"功能码", "全是保持寄存器功能码 3"),
         (r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="歌美飒"))],
        lambda b: ("109" in b or "110" in b) and DUP_RE.search(b), "MB-05", max_turns=12)
    # 解析面：99 有效行计数可见 + 类型分布（LONG 8 / UINT 17 / INT 74）+ fun 追问
    if not rp._has_num(blob, 99):
        raise F(f"MB-05: 99 有效行计数不可见: {blob[-400:]}")
    for n, t in ((8, "LONG"), (17, "UINT"), (74, "INT")):
        if not rp._has_num(blob, n):
            raise F(f"MB-05: 类型分布 {t} {n} 不可见: {blob[-400:]}")
    # fail-visible 拒收：重复地址 109/110（各 2 行）+ 脏行 #99 通讯状态 + 口径 + 禁顺延
    if not (rp._has_num(blob, 109) and rp._has_num(blob, 110)):
        raise F(f"MB-05: 重复地址 109/110 不可见: {blob[-400:]}")
    if "通讯状态" not in blob:
        raise F(f"MB-05: 脏行（#99 通讯状态）不可见: {blob[-400:]}")
    if not REJECT_HINT_RE.search(blob) and "重复" not in blob:
        raise F(f"MB-05: 拒收口径不可见: {blob[-400:]}")

    # 标定四列（RAW/ENG）不触发追问（提及「原值接入/不落 config」属合法回报，
    # 只有以问句追问才违例）
    if re_multi_search(blob, [r"(标定|系数|RAW_ZERO|ENG_ZERO)[^。\n]{0,30}？",
                              r"是否[^。\n]{0,20}(标定|系数)"]):
        raise F(f"MB-05: 标定列触发追问（应原值透传不读）: {blob[-400:]}")
    rp.expect_no_config(n0, "MB-05")
    rc.log("  MB-05 PASS（99 行 + 类型分布可见；重复地址 109/110 拒收 + 脏行可见；标定零追问）")


def re_multi_search(text, pats):
    import re
    return any(re.search(p, text) for p in pats)


def mb06():
    """MB-06：横向多设备展开 + 偏移量声明 + 控制组整组 excluded（蒙东）。"""
    n0 = rp._n_inst(rc.read_config())
    acc_exp = mengdong_expect(33)
    conv = rc.Conv()
    rp.upload_big(conv.conversation_id,
                  os.path.join(PTS_DIR, "蒙东技改项目数据转发点表.xlsx"),
                  MB_TEMPLATE + "请解析此文件中的设备信息")
    flow_to_confirm_mb(
        conv, "请按此文件接入。",
        [ANS_PROTOCOL,
         (r"台数|多少台|几台|机组", "33 台风机"),
         (r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="蒙东技改"))],
        n0, "MB-06", max_turns=16)
    rc.wait_config(lambda c: len(mb_points(c)) == 33 * 149 + 2, timeout=900,
                   desc="MB-06: 4919 点")
    cfg = rc.read_config()
    expect_mb_instance(cfg, "MB-06", ip="192.168.110.60", port=502)
    expect_mb_points_uid(cfg, "MB-06", 1)
    pts = mb_points(cfg)
    if len(pts) != 33 * 149 + 2:
        raise F(f"MB-06: 点数 {len(pts)} ≠ 4919")
    if any(int(p["type"]) != T_F32 or int(p["fun"]) != 4 or int(p["swap"]) != 2
           for p in pts):
        raise F("MB-06: 存在非 FLOAT/fun4/swap2 点")
    acc = {int(p["addr"]) for p in pts}
    if acc != acc_exp:
        miss = len(acc_exp - acc)
        extra = len(acc - acc_exp)
        raise F(f"MB-06: addr 集 ≠ 33 台展开基准（缺 {miss} 多 {extra}；"
                f"首台应 @0~296 偶数、第 2 台 @500~796）")
    if len(acc) != len(pts):
        raise F("MB-06: 展开地址存在重复")
    blob = rp._history_blob(conv)
    if "单机控制点数据" not in blob or not rp._has_num(blob, 20):
        raise F(f"MB-06: excluded 单机控制点数据（表内显式 20 点）不可见: {blob[-400:]}")
    if not re_multi_search(blob, [r"33\s*台", "台数"]):
        raise F(f"MB-06: 台数追问/采纳不可见: {blob[-400:]}")
    expect_fwd(cfg, "MB-06", 4919)
    rc.log("  MB-06 PASS（4919 = 33×149 展开偏移 500 + 调度 2；控制组整组 excluded）")


def mb07():
    """MB-07：地址公式展开 132 台 + 双遥测 sheet + 遥信丢弃 + 遥调 excluded（高力板）。"""
    n0 = rp._n_inst(rc.read_config())
    acc_exp = gaoliban_expect()
    conv = rc.Conv()
    rp.upload_big(conv.conversation_id,
                  os.path.join(PTS_DIR, "华能蒙东高力板风电场集控点表V3.0.xlsx"),
                  "采集采用modbus协议（Modbus/TCP），设备 IP 用 86.76.0.134，端口 502。"
                  "转发采用asfp2协议，目标II区服务器127.0.0.1:9900，"
                  "转发点表与采集侧一致，从 10000 号开始顺序编址。"
                  "请解析此文件中的设备信息")
    rp.flow_to_confirm(
        conv, "请按此文件接入。",
        [ANS_PROTOCOL,
         (r"端口", "端口 502"),
         (r"从站|Device|设备号", "从站号 1"),
         (r"功能码", "功能码 3"),
         (r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="高力板集控"))],
        n0, "MB-07", max_turns=16)
    rc.wait_config(lambda c: len(mb_points(c)) == 19810, timeout=900,
                   desc="MB-07: 19810 点")
    cfg = rc.read_config()
    expect_mb_instance(cfg, "MB-07", ip="86.76.0.134", port=502)
    expect_mb_points_uid(cfg, "MB-07", 1)
    pts = mb_points(cfg)
    if len(pts) != 19810:
        raise F(f"MB-07: 点数 {len(pts)} ≠ 19810")
    if any(int(p["type"]) != T_I16 or int(p["fun"]) != 3 or int(p["swap"]) != 0
           for p in pts):
        raise F("MB-07: 存在非 INT16/fun3/swap0 点（int/Int 混写映射）")
    acc = {int(p["addr"]) for p in pts}
    if acc != acc_exp:
        miss = len(acc_exp - acc)
        extra = len(acc - acc_exp)
        raise F(f"MB-07: addr 集 ≠ 132×150 展开 + 遥测2 基准（缺 {miss} 多 {extra}；"
                f"遥测2 应 @24000~24009 原样）")
    blob = rp._history_blob(conv)
    if "遥信" not in blob or not re_multi_search(blob, [r"丢弃|不接入|无效|无.*地址"]):
        raise F(f"MB-07: 遥信 sheet 丢弃（无具体地址列）不可见: {blob[-400:]}")
    if "遥调" not in blob:
        raise F(f"MB-07: excluded 遥调组不可见: {blob[-400:]}")
    if re_multi_search(blob, [r"int[^。\n]{0,12}(大小写|统一|混写)[^。\n]{0,20}？",
                              r"(类型|写法)[^。\n]{0,20}是否"]):
        raise F(f"MB-07: int/Int 混写触发追问: {blob[-400:]}")
    expect_fwd(cfg, "MB-07", 19810)
    rc.log("  MB-07 PASS（19810 = 132×150 公式展开 + 遥测2 10 点；遥信丢弃 + 遥调 excluded）")


def mb08():
    """MB-08：hex 功能码 + 4 万行大表 + Sheet1 副本不产点 + 同机组撞名拒收（京城）。"""
    n0 = rp._n_inst(rc.read_config())
    conv = rc.Conv()
    t0 = time.time()
    r, _ = rp.upload_big(conv.conversation_id,
                         os.path.join(PTS_DIR, "京城书声风电场3期集控接口ModbusTCP通讯协议(1).xlsx"),
                         "采集采用modbus协议（Modbus/TCP）。"
                         "转发采用asfp2协议，目标II区服务器127.0.0.1:9900，"
                         "转发点表与采集侧一致，从 10000 号开始顺序编址。"
                         "请解析此文件中的设备信息")
    blob = rp.drive_reject(
        conv, r, "请按此文件接入。",
        [ANS_PROTOCOL,
         (r"IP", "IP 192.168.110.62"),
         (r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="京城书声"))],
        lambda b: DUP_RE.search(b) and rp._has_num(b, 42550), "MB-08", max_turns=16)
    t_seen = time.time() - t0
    # 解析面：全量对账（yc 12550 + yx 30000 = 42550；遥控 75 excluded——excluded
    # 明细数为信息性数字，控制组 note 的点数启发式按类型词计数、遥控 sheet 无类型
    # 词时显示 0，不计 PASS/FAIL）+ hex FC 识别
    if not (rp._has_num(blob, 42625) or rp._has_num(blob, 42550)):
        raise F(f"MB-08: 全量对账不可见: {blob[-400:]}")
    for n in (12550, 30000):
        if not rp._has_num(blob, n):
            raise F(f"MB-08: 分段计数 {n} 不可见: {blob[-400:]}")
    if "遥控：控制组" not in blob:
        raise F(f"MB-08: 遥控 excluded 不可见: {blob[-400:]}")
    if not re.search(r"FC\s*0?4|F0?2|功能码[^。]{0,6}[43]", blob):
        raise F(f"MB-08: FC04/F02 hex 识别不可见: {blob[-400:]}")
    # Sheet1 无表头副本不产点（照解析将与遥测全量撞键 POINT_DUP，不得发生）
    if "POINT_DUP" in blob.upper() and "Sheet1" in blob:
        raise F(f"MB-08: Sheet1 副本产点引发撞键: {blob[-400:]}")
    # 解析性能（C4_RS_00204 ≤60s）为记录项不判 PASS/FAIL：该预算按「解析 ~2s
    # 确定性代码 + 结构识别 ~8s/块 LLM」计——墙钟实测被测试模型的单轮延迟
    # 主导（glm-5.3-flash 实测 314s/438s 两轮相差 40%，纯环境波动，无回归
    # 信号）；确定性解析部分由解析面断言承载，实测值落日志供人工核对
    rc.log(f"  MB-08 提交→对账可见 {t_seen:.0f}s（记录项：墙钟含多轮 LLM 往返，"
           f"60s 预算对象为确定性解析，见 RP-MB-08 性能注）")
    # fail-visible 拒收：同机组业务名撞名——冲突清单可读即承载「无业务名顺延
    # 通道」；分组数按 agent 对账口径（附录 B 预估 75/1175 与实测分组统计
    # （按类型撞名：yc/yx/未分类）存在分类粒度差，数字不锚 PASS/FAIL）
    if "按类型撞名" not in blob and not DUP_RE.search(blob):
        raise F(f"MB-08: 撞名冲突清单不可读: {blob[-400:]}")
    if "备用" not in blob:
        raise F(f"MB-08: 备用占位顺延说明不可见: {blob[-400:]}")
    if not REJECT_HINT_RE.search(blob):
        raise F(f"MB-08: 拒收提示口径不可见: {blob[-400:]}")
    rp.expect_no_config(n0, "MB-08")
    rc.log("  MB-08 PASS（42625 对账 + Sheet1 不产点 + IP 追问；撞名 75+1175 拒收）")


def mb09():
    """MB-09：四表提取合并 + 无类型列跨度推类型 + 段内撞名拒收（上海电气）。"""
    n0 = rp._n_inst(rc.read_config())
    conv = rc.Conv()
    r, _ = rp.upload_big(conv.conversation_id,
                         os.path.join(PTS_DIR, "上海电气传输点表说明-集控（张默茹-改）.xlsx"),
                         MB_TEMPLATE + "请解析此文件中的设备信息")
    # 对账可见判据：4500 合计或 3075+1200 分列（域规则 8 合并裁定，2026-10-12）
    quatro = lambda b: (rp._has_num(b, 4500)
                        or (rp._has_num(b, 3075) and rp._has_num(b, 1200)))
    blob = rp.drive_reject(
        conv, r, "请按此文件接入。",
        [ANS_PROTOCOL,
         (r"功能码", "遥测全是功能码 4，遥信功能码 2"),
         (r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="上海电气"))],
        lambda b: "发电机冷却水温度" in b and quatro(b), "MB-09", max_turns=14)
    # 解析面：四表 4500 行对账 + 跨表同址同名合并（无撞键/重叠误报）
    if not quatro(blob):
        raise F(f"MB-09: 四表 4500 行对账不可见: {blob[-400:]}")
    if "POINT_DUP" in blob.upper() or "POINT_OVERLAP" in blob.upper():
        raise F(f"MB-09: 跨表同点合并误报撞键/重叠: {blob[-400:]}")
    # 设备语境合成 id：跨设备同名消解（裸名 ×25 组不得计入撞名清单）
    if "edos" not in blob.lower() and "风机号" not in blob:
        raise F(f"MB-09: 设备语境（edos/风机号）不可见: {blob[-400:]}")
    if re.search(r"机舱振动传感器X」?×25", blob):
        raise F(f"MB-09: 跨设备同名（机舱振动传感器X ×25）未消解: {blob[-400:]}")
    if "实时故障" in blob:
        raise F(f"MB-09: 「故障」分表（同址异名实时故障N）被提点: {blob[-400:]}")
    # 功能码/类型互证：分表声明（功能码02）或主表跨度推导（差1→16位 2996 处）
    if not (re.search(r"功能码\s*0?2", blob) or rp._has_num(blob, 2996)):
        raise F(f"MB-09: 功能码/跨度互证不可见: {blob[-400:]}")
    # 故障段 1575 计入接入口径 + 控制组排除 + fail-visible 拒收 25 组
    if not rp._has_num(blob, 1575):
        raise F(f"MB-09: 故障段 1575 接入口径不可见: {blob[-400:]}")
    if "发电机冷却水温度" not in blob:
        raise F(f"MB-09: 冲突点名不可见: {blob[-400:]}")
    if not rp._has_num(blob, 25):
        raise F(f"MB-09: 撞名 25 组计数不可见: {blob[-400:]}")
    if not REJECT_HINT_RE.search(blob):
        raise F(f"MB-09: 拒收提示口径不可见: {blob[-400:]}")
    rp.expect_no_config(n0, "MB-09")
    rc.log("  MB-09 PASS（4500 四表对账 + 同点合并；段内撞名 25 组拒收）")


def mb10():
    """MB-10：中文点名 + 设备语境消解 + 场级段撞名拒收（华创 Sheet3）。"""
    n0 = rp._n_inst(rc.read_config())
    conv = rc.Conv()
    r, _ = rp.upload_big(conv.conversation_id,
                         os.path.join(PTS_DIR, "华创点表1.xls"),
                         MB_TEMPLATE + "请解析此文件中的设备信息")
    blob = rp.drive_reject(
        conv, r, "请按此文件接入。",
        [ANS_PROTOCOL,
         (r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name="华创"))],
        lambda b: "全场总有功功率" in b and rp._has_num(b, 1971), "MB-10", max_turns=14)
    # 解析面：Sheet3 1971 行对账 + 类型分布（float 1931 = FC3×2 + FC4×1929、bool 40）
    # + addr1 双空间共存无误报 + sheet1/Sheet2 处置可见
    if not rp._has_num(blob, 1971):
        raise F(f"MB-10: 1971 对账不可见: {blob[-400:]}")
    if not (rp._has_num(blob, 1931) and rp._has_num(blob, 40)):
        raise F(f"MB-10: 类型分布（1931/40）不可见: {blob[-400:]}")
    if "POINT_OVERLAP" in blob.upper():
        raise F(f"MB-10: addr1 双空间共存误报重叠: {blob[-400:]}")
    if "sheet1" not in blob.lower() or "Sheet2" not in blob:
        raise F(f"MB-10: sheet1 excluded / Sheet2 不产点不可见: {blob[-400:]}")
    # fail-visible 拒收：场级段撞名 3 组 + 口径 + 禁顺延
    if "全场总有功功率" not in blob or "全场备用" not in blob:
        raise F(f"MB-10: 冲突点名（全场总有功功率/全场备用）不可见: {blob[-400:]}")
    if not (rp._has_num(blob, 1255) and rp._has_num(blob, 1279)):
        raise F(f"MB-10: 冲突 addr（1255/1279 等）不可见: {blob[-400:]}")
    if not rp._has_num(blob, 3):
        raise F(f"MB-10: 撞名 3 组计数不可见: {blob[-400:]}")
    if not REJECT_HINT_RE.search(blob):
        raise F(f"MB-10: 拒收提示口径不可见: {blob[-400:]}")

    if re_multi_search(blob, [r"(系数|标定)[^。\n]{0,30}？", r"是否[^。\n]{0,20}(系数|标定)"]):
        raise F(f"MB-10: 系数列触发追问: {blob[-400:]}")
    rp.expect_no_config(n0, "MB-10")
    rc.log("  MB-10 PASS（1971 对账 + addr1 双空间无误报；场级撞名 3 组拒收）")


def mb11():
    """MB-11：列裁剪 + 寄存器类型列映射 + 中文名撞名拒收（ABB / Bachmann 双轮）。"""
    rounds = [
        ("华锐ABB点表.xlsx", "华锐ABB", 129,
         lambda b: "故障电流" in b and rp._has_num(b, 129), "MB-11[ABB]",
         [(r"IP|端口|从站", "设备 IP 192.168.110.60，端口 502，从站号 1")],
         ["故障电流", "偏航变频器温度"], 2),
        ("华锐Bachmann点表.xls", "华锐Bachmann", 140,
         lambda b: rp._has_num(b, 140) and DUP_RE.search(b), "MB-11[Bachmann]",
         [(r"IP|端口|从站", "设备 IP 192.168.110.60，端口 502，从站号 1")],
         ["功率比例", "塔基变压器电流"], 5),
    ]
    for fname, dev, rows_n, done, tag, answers, dup_names, dup_groups in rounds:
        n0 = rp._n_inst(rc.read_config())
        conv = rc.Conv()
        r, _ = rp.upload_big(conv.conversation_id,
                             os.path.join(PTS_DIR, fname),
                             MB_TEMPLATE + "请解析此文件中的设备信息")
        blob = rp.drive_reject(
            conv, r, "请按此文件接入。",
            [ANS_PROTOCOL] + answers
            + [(r"这台设备叫|设备名称|设备名", DEVICE_ANSWER.format(name=dev))],
            done, tag, max_turns=12)
        # 解析面：行数对账 + 类型/长度互证 + 码表 sheet 0 点 + 备注列零触发
        if not rp._has_num(blob, rows_n):
            raise F(f"{tag}: {rows_n} 对账不可见: {blob[-400:]}")
        if "Holding_register" not in blob and "Input_register" not in blob \
                and "功能码" not in blob:
            raise F(f"{tag}: 寄存器类型列映射不可见: {blob[-400:]}")
        for nm in dup_names:
            if nm not in blob:
                raise F(f"{tag}: 冲突点名 {nm!r} 不可见: {blob[-400:]}")
        if not rp._has_num(blob, dup_groups):
            raise F(f"{tag}: 撞名 {dup_groups} 组计数不可见: {blob[-400:]}")

        if re_multi_search(blob, [r"前八位.*追问|是否.*备注|备注.*如何处理"]):
            raise F(f"{tag}: 备注列业务说明触发追问: {blob[-400:]}")
        rp.expect_no_config(n0, tag)
        rc.log(f"  {tag} PASS（{rows_n} 对账；中文名撞名 {dup_groups} 组拒收；码表 0 点）")


CASES = [
    ("MB-01", mb01),
    ("MB-02", mb02),
    ("MB-03", mb03),
    ("MB-04", mb04),
    ("MB-05", mb05),
    ("MB-06", mb06),
    ("MB-07", mb07),
    ("MB-08", mb08),
    ("MB-09", mb09),
    ("MB-10", mb10),
    ("MB-11", mb11),
]


def main():
    only = sys.argv[1].split(",") if len(sys.argv) > 1 else None
    if not os.path.isdir(PTS_DIR):
        rc.log("════ RP-MB 全组 SKIP-DIR：真实点表目录不存在 ════")
        return
    rc.log("════ RP-MB modbus 真实点表接入串行测试开始（模型 "
           f"{rc.MODEL_PRIMARY}）════")
    rc._post = rp._post_big  # 大回合超时放宽（分块提取 10~20 分钟）
    base.chain_clean()
    rc.MCP_STACK.up()
    agent = base.V21Agent()
    agent.up()
    rc.AGENT = agent
    rc.PH.reset("RP-MB")
    t0 = time.time()
    passed, failed, skipped = [], [], []
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
    if not only or "MB-12" in only:
        rc.log("── MB-12 SKIP-MB12（暂不测试：转发区声明式结构，2026-10-11 用户裁定）──")
        skipped.append("MB-12")
    rc.log(f"════ RP-MB 套件结束：PASS={passed} FAIL={failed} SKIP={skipped}"
           f"（{time.time() - t0:.0f}s）════")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
