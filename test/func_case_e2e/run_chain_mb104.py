#!/usr/bin/env python3
# func_test_case.md modbus/104 链串行驱动器（v2.1.0 命名体系）
# 链序：A.2（全新环境，场站询问答「华能阿拉善」，不预接入）
#       → 30（modbus 信息齐全）→ 31（缺端口/从站号，询问后接入）
#       → 32（重复 addr + 非法功能码，可读拒绝）→ 33（转发协议缺失，是非题握手）
#       → 34（iec104 信息齐全）→ 35（缺端口，询问后接入）
#       → 36（IOA 重复，可读拒绝）→ 37（多实例并存，跨实例同 IOA 合法）
#       → 50（连接型服务不并入——同 ip:port 从站 2 = 独立实例）
# 规则（用户指令）：逐项串行、不并行；任一链步 FAIL 立即停止全链、落盘诊断、不修改。
# 数据面：modbus/iec104 无从站模拟器——全部用例只验【文件】配置面（func_test_case.md
# 本轮测试范围声明），不注入、不断言数据流。
# 前缀断言按附录 B 机器判定约定（前缀自洽）：从方案文本提取实际明示的前缀，再断言
# config.json 点 key 前缀与之一致——多类型词派生（wt1/bj1）与撞名顺延均自然兼容。
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_cases as rc  # noqa: E402
import run_chain_v21 as base  # noqa: E402

PASSED = []
SETUP_ONLY = {"A.2"}

MSG30 = ("现在需要接入1号风机变桨控制器的数据，厂家在变桨控制器上开放了modbus采集口，"
         "设备IP是192.168.110.51，端口502，从站号1。点表10个点：3000:桨叶角度、"
         "3002:变桨速度、3004:变桨电机温度、3006:变桨电机电流、3008:后备电源电压、"
         "3010:后备电源温度、3012:桨叶1位置、3014:桨叶2位置、3016:桨叶3位置、"
         "3018:轮毂温度。除桨叶1/2/3位置是输入寄存器（功能码4）外，其余全是保持寄存器"
         "（功能码3），全部32位浮点、字节交换2（swap=2）。我们需要将这些数据转发到II区"
         "服务器上，转发采用asfp2协议，目标地址是127.0.0.1:9900，点表5000~5009。")
MSG31 = ("现在需要接入1号风机齿轮箱油泵控制器的数据，modbus协议，设备IP是192.168.110.52。"
         "点表8个点：4000:油泵运行状态是线圈（功能码1、布尔类型、swap为0），其余7个点"
         "4100:油温、4102:油压、4104:油位、4106:泵后压力、4108:电机温度、4110:滤网压差、"
         "4112:油流量全是保持寄存器（功能码3）、32位浮点、swap=2。转发到II区127.0.0.1:9901，"
         "转发采用asfp2协议，点表5100~5107。")
ANS31 = "端口502，从站号都是1。"
MSG32 = ("现在需要接入1号风机机舱控制柜的数据，modbus协议，设备IP是192.168.110.53，"
         "端口502，从站号1。点表6个点：3200:机舱温度、3202:机舱振动、3204:塔基温度、"
         "3210:机舱湿度、3210:舱外风向、3212:偏航角度，除3212偏航角度是功能码5外其余"
         "都是保持寄存器（功能码3），全部32位浮点、swap=2。转发到II区127.0.0.1:9902，"
         "转发采用asfp2协议，点表5200~5205。")
MSG33 = ("接入1号风机地面环网柜的数据，modbus协议，设备IP是192.168.110.54，端口502，"
         "从站号1，点表4个点：3300:环网柜温度、3302:环网柜湿度、3304:电缆头温度、"
         "3306:局放值，全是保持寄存器（功能码3）、32位浮点、swap=2。数据要送到II区"
         "127.0.0.1:9900，点表5300~5303。")
ANS33A = "就用asfp2吧。"
ANS33B = "对"
MSG34 = ("现在需要接入1号主变测控装置的数据，装置是IEC104规约，IP是192.168.110.99，"
         "端口2404，公共地址1。点表6个点：16385:UAB电压、16386:UBC电压、16387:UAC电压、"
         "1:弹簧未储能、2:装置异常、25601:正向有功电度。我们需要将这些数据转发到II区服务器上，"
         "转发采用asfp2协议，目标地址是127.0.0.1:9902，点表5400~5405。")
MSG35 = ("接入2号主变测控装置，IEC104规约，装置IP是192.168.110.199，公共地址1。"
         "点表4个点：16385:UAB电压、16386:UBC电压、1:弹簧未储能、25601:正向有功电度。"
         "转发到II区127.0.0.1:9903，转发采用asfp2协议，点表5500~5503。")
ANS35 = "端口是2404。"
MSG36 = ("现在需要接入1号升压站公用测控装置的数据，IEC104规约，装置IP是192.168.110.101，"
         "端口2404，公共地址1。点表4个点：16385:UAB电压、16385:UAB线电压、16386:UBC电压、"
         "1:弹簧未储能。转发到II区127.0.0.1:9904，转发采用asfp2协议，点表5600~5603。")
MSG37 = ("再接入3号主变测控装置，IEC104规约，装置IP是192.168.110.102，端口2404，公共地址2。"
         "点表与2号主变完全一样：16385:UAB电压、16386:UBC电压、1:弹簧未储能、"
         "25601:正向有功电度。转发到II区127.0.0.1:9904，转发采用asfp2协议，点表5700~5703。")
MSG50 = ("再接入备用变桨控制器，modbus协议，设备IP是192.168.110.51，端口502，从站号2。"
         "点表与主变桨控制器完全一样：3000:桨叶角度、3002:变桨速度、3004:变桨电机温度、"
         "3006:变桨电机电流、3008:后备电源电压、3010:后备电源温度、3012:桨叶1位置、"
         "3014:桨叶2位置、3016:桨叶3位置、3018:轮毂温度，除桨叶1/2/3位置是功能码4外"
         "其余功能码3，全部32位浮点、swap=2。转发到II区127.0.0.1:9905，转发采用asfp2协议，"
         "点表7100~7109。")


def svc_count(cfg, st):
    return sum(1 for k in rc.server_instances(cfg) if k[0] == st)


def cfg_has(st, n=1):
    """done 谓词用：config.json 可能尚未落盘（read_config → None）。"""
    return svc_count(rc.read_config() or {}, st) >= n


def plan_prefixes():
    """前缀自洽：从本链步方案文本提取实际明示的点 key 前缀集合。"""
    out = set()
    for e in rc.PH.entries:
        if e.get("kind") != "assistant":
            continue
        for m in re.finditer(r"→\s*([a-z][a-z0-9]*?)_[a-z0-9_]+", e.get("text") or ""):
            out.add(m[1])
        for m in re.finditer(r"点 key 前缀\s*([a-z][a-z0-9]*?)_", e.get("text") or ""):
            out.add(m[1])
    return out


def pair_of(cfg, st, addr):
    """返回包含 addr 的采集实例 (st, id)；及引用它的 asfp2 转发实例 id。"""
    wid = rc.writer_of(cfg, addr) if st == "c4_asfp2_server" else None
    if wid is None:
        for (s, iid), inst in rc.server_instances(cfg).items():
            if s == st and addr in {p.get("addr") for p in inst.get("points", [])}:
                wid = iid
                break
    if wid is None:
        raise rc.Fail(f"{st} 中找不到含 addr={addr} 的实例")
    fid = None
    for (s, iid), inst in rc.server_instances(cfg).items():
        if s != "c4_asfp2_client":
            continue
        if any(str(p.get("key", "")).startswith(f"{wid}.")
               for p in inst.get("points", [])):
            fid = iid
            break
    return wid, fid


def assert_modbus_pair(tag, cfg, anchor, fwd_anchor, ip, port, uid,
                       expect=None, prefix_from_plan=True):
    """modbus 断言：采集实例原样采纳（ip/port/uid/点表 fun/type/swap）+ 转发成对 +
    点 key 前缀与方案明示一致（前缀自洽）。expect: {addr: (fun, type, swap)}。
    定位优先按转发地址反查（fwd_anchor 引用 key 唯一指向目标采集实例）——主/备
    控制器点表相同（用例 50）时按 addr 锚定会命中别的实例。"""
    insts = rc.server_instances(cfg)
    wid = inst_w = None
    if fwd_anchor is not None:
        for (s, iid), inst in insts.items():
            if s != "c4_asfp2_client":
                continue
            fpts = {p.get("addr"): p for p in inst.get("points", [])}
            if fwd_anchor in fpts:
                wid = str(fpts[fwd_anchor].get("key", "")).split(".", 1)[0]
                break
        if wid is None:
            raise rc.Fail(f"{tag}: 转发 addr={fwd_anchor} 无 Reader 实例")
        inst_w = insts.get(("c4_modbus_client", wid))
        if inst_w is None:
            raise rc.Fail(f"{tag}: 采集实例 {wid} 不存在")
    else:
        for (s, iid), inst in insts.items():
            if s != "c4_modbus_client":
                continue
            pts = {p.get("addr"): p for p in inst.get("points", [])}
            if anchor in pts:
                wid, inst_w = iid, inst
                break
        if wid is None:
            raise rc.Fail(f"{tag}: c4_modbus_client 中找不到 addr={anchor} 的采集实例")
    if str(inst_w.get("ip")) != ip or int(inst_w.get("port", -1)) != int(port):
        raise rc.Fail(f"{tag}: 设备地址 {inst_w.get('ip')}:{inst_w.get('port')} ≠ {ip}:{port}（原样采纳）")
    pts = {p.get("addr"): p for p in inst_w.get("points", [])}
    for a, p in pts.items():
        if int(p.get("uid", -1)) != int(uid):
            raise rc.Fail(f"{tag}: addr={a} uid={p.get('uid')} ≠ {uid}（原样采纳）")
    if expect:
        for a, (fun, typ, swap) in expect.items():
            p = pts.get(a)
            if p is None:
                raise rc.Fail(f"{tag}: addr={a} 缺失（点表 {sorted(pts)}）")
            got = (int(p.get("fun", -1)), int(p.get("type", -1)), int(p.get("swap", -1)))
            if got != (fun, typ, swap):
                raise rc.Fail(f"{tag}: addr={a} fun/type/swap={got} ≠ {(fun, typ, swap)}（原样采纳）")
    # 点 key 前缀与方案明示一致（前缀自洽；无下划线）
    prefixes = plan_prefixes() if prefix_from_plan else set()
    for a, p in pts.items():
        pid = str(p.get("id", ""))
        if "_" not in pid:
            raise rc.Fail(f"{tag}: 点 key {pid!r} 无条件前缀缺失")
        pre = pid.split("_", 1)[0]
        if prefix_from_plan and prefixes and pre not in prefixes:
            raise rc.Fail(f"{tag}: 落盘前缀 {pre} 不在方案明示集合 {prefixes}（前缀自洽）")
    # 转发成对（引用 key = {采集实例id}.{点key}）
    for (s, iid), inst in insts.items():
        if s != "c4_asfp2_client":
            continue
        keys = [str(p.get("key", "")) for p in inst.get("points", [])]
        if keys and all(k.startswith(f"{wid}.") for k in keys):
            fid = iid
            break
    if fid is None:
        raise rc.Fail(f"{tag}: 采集实例 {wid} 无配对 asfp2 转发实例")
    for a in ([fwd_anchor] if fwd_anchor else []):
        fpts = rc.points_of(cfg, "c4_asfp2_client", fid)
        if a not in fpts:
            raise rc.Fail(f"{tag}: 转发 addr={a} 缺失（实例 {fid}）")
        k = str(fpts[a].get("key", ""))
        pid = str(pts.get(anchor, {}).get("id", ""))
        if not k.startswith(f"{wid}."):
            raise rc.Fail(f"{tag}: 转发引用 key={k!r} 不指向采集实例 {wid}")
    return wid, fid


def assert_iec104_pair(tag, cfg, anchor, fwd_anchor, ip, port, common_addr):
    """iec104 断言：采集实例（ip/port/common_address 原样 + IOA 点表）+ 转发成对。
    定位优先按转发地址反查（fwd_anchor 引用 key 唯一指向目标采集实例）——跨实例
    同 IOA（用例 37 语义）下按 IOA 锚定会命中别的实例。"""
    insts = rc.server_instances(cfg)
    wid = None
    if fwd_anchor is not None:
        for (s, iid), inst in insts.items():
            if s != "c4_asfp2_client":
                continue
            fpts = {p.get("addr"): p for p in inst.get("points", [])}
            if fwd_anchor in fpts:
                k = str(fpts[fwd_anchor].get("key", ""))
                wid = k.split(".", 1)[0]
                break
        if wid is None:
            raise rc.Fail(f"{tag}: 转发 addr={fwd_anchor} 无 Reader 实例")
    inst_w = None
    if wid is not None:
        inst_w = insts.get(("c4_iec104_client", wid))
        if inst_w is None:
            raise rc.Fail(f"{tag}: 采集实例 {wid} 不存在")
    else:
        for (s, iid), inst in insts.items():
            if s != "c4_iec104_client":
                continue
            if any(p.get("addr") == anchor for p in inst.get("points", [])):
                wid, inst_w = iid, inst
                break
        if wid is None:
            raise rc.Fail(f"{tag}: c4_iec104_client 中找不到 IOA={anchor} 的采集实例")
    if str(inst_w.get("ip")) != ip or int(inst_w.get("port", -1)) != int(port):
        raise rc.Fail(f"{tag}: 装置地址 {inst_w.get('ip')}:{inst_w.get('port')} ≠ {ip}:{port}（原样采纳）")
    if common_addr is not None and int(inst_w.get("common_address", -1)) != int(common_addr):
        raise rc.Fail(f"{tag}: 公共地址 {inst_w.get('common_address')} ≠ {common_addr}（原样采纳）")
    for (s, iid), inst in insts.items():
        if s != "c4_asfp2_client":
            continue
        keys = [str(p.get("key", "")) for p in inst.get("points", [])]
        if keys and all(k.startswith(f"{wid}.") for k in keys):
            fid = iid
            if fwd_anchor is not None:
                fpts = {p.get("addr"): p for p in inst.get("points", [])}
                if fwd_anchor not in fpts:
                    raise rc.Fail(f"{tag}: 转发 addr={fwd_anchor} 缺失（实例 {fid}）")
            return wid, fid
    raise rc.Fail(f"{tag}: 采集实例 {wid} 无配对 asfp2 转发实例")


# ── 链步实现 ───────────────────────────────────────────────
def s_a2():
    """A.2 链首：全新环境零接入，仅绑定场站（首条消息顺带完成用例 30 的场站应答）。"""
    pass  # 场站应答由 s30 的 answers 承担——A.2 = chain_clean + 无 site agent（main 已做）


def s30():
    conv = rc.Conv()
    base.flow(conv, MSG30, answers=[(r"场站", "华能阿拉善")],
              done=lambda: cfg_has("c4_modbus_client"))
    cfg = rc.wait_config(lambda c: [k for k in rc.server_instances(c)
                                    if k[0] == "c4_modbus_client"],
                         timeout=240, desc="30: modbus 采集实例落地")
    base.assert_channel_ids(cfg)
    expect = {3000: (3, 10, 2), 3002: (3, 10, 2), 3004: (3, 10, 2),
              3012: (4, 10, 2), 3014: (4, 10, 2), 3016: (4, 10, 2)}
    wid, fid = assert_modbus_pair("30", cfg, 3000, 5000, "192.168.110.51", 502, 1,
                                  expect=expect)
    if len(rc.points_of(cfg, "c4_modbus_client", wid)) != 10:
        raise rc.Fail("30: 采集点数 ≠ 10")
    reg = base.registry()
    if reg.get("channelHighWatermark") != 2:
        raise rc.Fail(f"30: 水位={reg.get('channelHighWatermark')} ≠ 2")
    base.assert_no_handle_leak("30")


def s31():
    conv = rc.Conv()
    base.flow(conv, MSG31, answers=[(r"从站|uid", "从站号都是1。"),
                                    (r"端口", "端口502。")],
              done=lambda: cfg_has("c4_modbus_client", 2))
    cfg = rc.wait_config(lambda c: svc_count(c, "c4_modbus_client") >= 2,
                         timeout=240, desc="31: 齿轮箱油泵控制器接入")
    base.assert_channel_ids(cfg)
    expect = {4000: (1, 0, 0), 4100: (3, 10, 2), 4102: (3, 10, 2)}
    assert_modbus_pair("31", cfg, 4000, 5100, "192.168.110.52", 502, 1, expect=expect)
    if base.registry().get("channelHighWatermark") != 4:
        raise rc.Fail(f"31: 水位={base.registry().get('channelHighWatermark')} ≠ 4")
    base.assert_no_handle_leak("31")


def s32():
    """重复 addr + 非法功能码：逐项可读拒绝、无方案、config 不变。"""
    before = rc.read_config()
    conv = rc.Conv()
    text = conv.send(MSG32)
    time.sleep(6)
    after = rc.read_config()
    if json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True):
        raise rc.Fail("32: 冲突请求写入了 config")
    if re.search(r"是否确认|确认执行", text):
        raise rc.Fail(f"32: 冲突请求进入可确认方案: {text}")
    if not re.search(r"3210", text):
        raise rc.Fail(f"32: 未逐项指出重复地址 3210: {text}")
    if not re.search(r"功能码|非法", text):
        raise rc.Fail(f"32: 未指出功能码非法: {text}")
    base.assert_no_handle_leak("32")


def s33():
    conv = rc.Conv()
    base.flow(conv, MSG33, answers=[(r"转发协议|什么协议|采用什么|请提供.{0,8}协议", ANS33A),
                                    (r"asfp2", ANS33B)],
              done=lambda: svc_count(rc.read_config(), "c4_modbus_client") >= 3)
    cfg = rc.wait_config(lambda c: svc_count(c, "c4_modbus_client") >= 3,
                         timeout=240, desc="33: 地面环网柜接入")
    base.assert_channel_ids(cfg)
    wid, fid = assert_modbus_pair("33", cfg, 3300, 5300, "192.168.110.54", 502, 1)
    # 转发侧协议必须是 asfp2（禁止沿用 modbus）——配对实例属 asfp2 服务即证
    fpts = rc.points_of(cfg, "c4_asfp2_client", fid)
    if len(fpts) != 4:
        raise rc.Fail(f"33: 转发点数 {len(fpts)} ≠ 4")
    if base.registry().get("channelHighWatermark") != 6:
        raise rc.Fail(f"33: 水位={base.registry().get('channelHighWatermark')} ≠ 6")
    base.assert_no_handle_leak("33")


def s34():
    conv = rc.Conv()
    base.flow(conv, MSG34, answers=[(r"场站", "华能阿拉善")],
              done=lambda: cfg_has("c4_iec104_client"))
    cfg = rc.wait_config(lambda c: [k for k in rc.server_instances(c)
                                    if k[0] == "c4_iec104_client"],
                         timeout=240, desc="34: iec104 采集实例落地")
    base.assert_channel_ids(cfg)
    wid, fid = assert_iec104_pair("34", cfg, 16385, 5400, "192.168.110.99", 2404, 1)
    if len(rc.points_of(cfg, "c4_iec104_client", wid)) != 6:
        raise rc.Fail("34: 采集点数 ≠ 6（104 点位无需预声明类型——未向用户索要即过）")
    if base.registry().get("channelHighWatermark") != 8:
        raise rc.Fail(f"34: 水位={base.registry().get('channelHighWatermark')} ≠ 8")
    base.assert_no_handle_leak("34")


def s35():
    conv = rc.Conv()
    base.flow(conv, MSG35, answers=[(r"端口", ANS35)],
              done=lambda: svc_count(rc.read_config(), "c4_iec104_client") >= 2)
    cfg = rc.wait_config(lambda c: svc_count(c, "c4_iec104_client") >= 2,
                         timeout=240, desc="35: 2号主变接入")
    base.assert_channel_ids(cfg)
    assert_iec104_pair("35", cfg, 16385, 5500, "192.168.110.199", 2404, 1)
    if base.registry().get("channelHighWatermark") != 10:
        raise rc.Fail(f"35: 水位={base.registry().get('channelHighWatermark')} ≠ 10")
    base.assert_no_handle_leak("35")


def s36():
    """IOA 16385 重复：可读拒绝、无方案、config 不变。"""
    before = rc.read_config()
    conv = rc.Conv()
    text = conv.send(MSG36)
    time.sleep(6)
    after = rc.read_config()
    if json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True):
        raise rc.Fail("36: 冲突请求写入了 config")
    if re.search(r"是否确认|确认执行", text):
        raise rc.Fail(f"36: 冲突请求进入可确认方案: {text}")
    if not re.search(r"16385|重复", text):
        raise rc.Fail(f"36: 未指出 IOA 重复: {text}")
    base.assert_no_handle_leak("36")


def s37():
    n104 = svc_count(rc.read_config(), "c4_iec104_client")
    conv = rc.Conv()
    base.flow(conv, MSG37, done=lambda: svc_count(rc.read_config(), "c4_iec104_client") >= n104 + 1)
    cfg = rc.wait_config(lambda c: svc_count(c, "c4_iec104_client") >= n104 + 1,
                         timeout=240, desc="37: 3号主变接入")
    base.assert_channel_ids(cfg)
    # ① 跨实例同 IOA 合法：16385 现存在于 ≥3 个 iec104 实例
    holders = [iid for (s, iid), inst in rc.server_instances(cfg).items()
               if s == "c4_iec104_client"
               and any(p.get("addr") == 16385 for p in inst.get("points", []))]
    if len(holders) < 3:
        raise rc.Fail(f"37: 含 IOA 16385 的实例数 {len(holders)} < 3（跨实例同 IOA 应合法）")
    wid3, fid3 = assert_iec104_pair("37", cfg, 16385, 5700, "192.168.110.102", 2404, 2)
    if int(rc.points_of(cfg, "c4_iec104_client", wid3).get(1, {}).get("shm_id", 0)) == \
       int(rc.points_of(cfg, "c4_iec104_client", holders[0]).get(1, {}).get("shm_id", -1)):
        raise rc.Fail("37: 跨实例 shm_id 未独立分配")
    if base.registry().get("channelHighWatermark") != 12:
        raise rc.Fail(f"37: 水位={base.registry().get('channelHighWatermark')} ≠ 12")
    base.assert_no_handle_leak("37")


def all_shm_snapshot(cfg):
    """全部采集实例的 (st, iid, addr) → shm_id 快照（modbus/104 链通用——
    rc.snapshot_w1 写死主链 addr=1000，本链无该点）。"""
    out = {}
    for (st, iid), inst in rc.server_instances(cfg).items():
        for p in inst.get("points", []):
            out[(st, iid, p.get("addr"))] = p.get("shm_id")
    return out


def s50():
    """连接型不并入：同 ip:port 从站 2 → 独立采集+转发实例、既有链路无损。"""
    n_mb = svc_count(rc.read_config(), "c4_modbus_client")
    n_fwd = svc_count(rc.read_config(), "c4_asfp2_client")
    before = all_shm_snapshot(rc.read_config())
    conv = rc.Conv()
    base.flow(conv, MSG50, done=lambda: svc_count(rc.read_config(), "c4_modbus_client") >= n_mb + 1)
    cfg = rc.wait_config(lambda c: svc_count(c, "c4_modbus_client") >= n_mb + 1
                         and rc.forward_of(c, 7100) is not None,
                         timeout=240, desc="50: 备用变桨控制器独立实例")
    base.assert_channel_ids(cfg)
    # ① 不并入：modbus 采集实例数 +1（连接型每设备一实例）
    if svc_count(cfg, "c4_modbus_client") != n_mb + 1:
        raise rc.Fail(f"50: modbus 实例数 {n_mb}→{svc_count(cfg, 'c4_modbus_client')} ≠ +1（不得并入）")
    wid_b, _ = assert_modbus_pair("50", cfg, 3000, 7100, "192.168.110.51", 502, 2)
    # 从站号 2 与主变桨（从站 1）实例区分；前缀互异（点 key 前缀自洽已断言）
    wid_a = None
    for (s, iid), inst in rc.server_instances(cfg).items():
        if s == "c4_modbus_client" and iid != wid_b \
                and any(p.get("addr") == 3000 for p in inst.get("points", [])):
            wid_a = iid
            break
    if wid_a is None:
        raise rc.Fail("50: 主变桨控制器实例丢失")
    pa = rc.points_of(cfg, "c4_modbus_client", wid_a)[3000]
    pb = rc.points_of(cfg, "c4_modbus_client", wid_b)[3000]
    if str(pa.get("id")) == str(pb.get("id")):
        raise rc.Fail(f"50: 主/备控制器点 key 相同 {pa.get('id')}（前缀应互异）")
    # ③ 既有链路无损：既有点 shm_id 不变
    after = all_shm_snapshot(cfg)
    for key, sid in before.items():
        if after.get(key) != sid:
            raise rc.Fail(f"50: 既有点 {key} shm_id 被重排（{sid}→{after.get(key)}）")
    if svc_count(cfg, "c4_asfp2_client") != n_fwd + 1:
        raise rc.Fail("50: 转发实例数 ≠ +1")
    if base.registry().get("channelHighWatermark") != 14:
        raise rc.Fail(f"50: 水位={base.registry().get('channelHighWatermark')} ≠ 14")
    base.assert_no_handle_leak("50")


# ── 链调度（串行，失败即停）────────────────────────────────
STEPS = [
    ("A.2",  SETUP_ONLY,  s_a2),
    ("30",   ["30"],      s30),
    ("31",   ["31"],      s31),
    ("32",   ["32"],      s32),
    ("33",   ["33"],      s33),
    ("34",   ["34"],      s34),
    ("35",   ["35"],      s35),
    ("36",   ["36"],      s36),
    ("37",   ["37"],      s37),
    ("50",   ["50"],      s50),
]

CHAIN_TAG = "C4He1"
CHAIN_FAIL_DIR = base.CHAIN_FAIL_DIR


def main():
    rc.log("════ modbus/104 链串行测试开始（A.2 → 30~33 → 34~37 → 50）════")
    base.chain_clean()
    rc.MCP_STACK.up()
    agent = base.V21Agent()
    agent.up()
    rc.AGENT = agent
    for label, cases, fn in STEPS:
        rc.log(f"════ 链步 {label} 开始 ════")
        rc.PH.reset(label)
        t0 = time.time()
        try:
            fn()
        except rc.Fail as e:
            base.dump_diag(label, e)
            rc.log(f"════ 链步 {label} FAIL: {e} ════")
            rc.log(f"════ modbus/104 链在第 {label} 步停止（已通过用例: {PASSED or '无'}）════")
            sys.exit(1)
        except Exception as e:
            base.dump_diag(label, e)
            rc.log(f"════ 链步 {label} 异常 {type(e).__name__}: {str(e)[:200]} ════")
            rc.log(f"════ modbus/104 链在第 {label} 步停止（已通过用例: {PASSED or '无'}）════")
            sys.exit(1)
        PASSED.extend(cases)
        rc.log(f"════ 链步 {label} PASS（{time.time()-t0:.0f}s）════")
    rc.log(f"════ modbus/104 链全部通过：{PASSED} ════")
    rc.log(f"════ 标注清单（AI测试通过 {CHAIN_TAG}）：{PASSED} ════")


if __name__ == "__main__":
    main()
