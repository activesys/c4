#!/usr/bin/env python3
# func_test_case.md 设备组批量接入串行驱动器（agent.md §2.11，用例 57~62，2026-10-03）
# 链段（各自独立回零 A.2，场站询问答「华能阿拉善」）：
#   57 —— 两组模板点表 + 每台独立 ip:port（倍福规则 ip / 巴赫曼显式列表）：
#         5 个 modbus 实例、key wt{N}_*、模板相对点名后缀、注册表批量固化
#   58 —— 共用 ip:port + 从站号范围 1~6：6 实例同 ip:port 不同 uid（永不并入）
#   59 —— 地址偏移每台 +1000：key 模板相对（wt2_windspeed，不得出现 wt2_2000）
#   60 —— asfp2 逐台监听端口 + 组级回滚：占 9102 → 全组回滚空态无幽灵条目
#         → 解除占用后整组重接成功
#   61 —— IEC104 共用 IP + 端口范围 2404~2413 + 非纯数字编号 A01~A10（nbA01 型前缀）
#   62 —— asfp2 共用监听端口 + 偏移：并入单实例 20 点、注册表双条目同 host、水位=2
# 规则（用户指令）：逐项串行、不并行；任一链段 FAIL 立即停止、落盘诊断、不修改。
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_cases as rc  # noqa: E402
import run_chain_v21 as base  # noqa: E402

PASSED = []

FWD_ANS = "转发采用asfp2协议到127.0.0.1:21501"
GROUP_ANSWERS = lambda proto: [
    (r"场站名称", "华能阿拉善"),
    (r"哪种协议", proto),
    (r"还差.*说明", "全部是保持寄存器（功能码3），32位浮点。各台点表相同。"),
    (r"同时配置转发|转发协议与目标", FWD_ANS),
]

MSG57 = ("现在接入一个风场的风机：1#~3#是倍福PLC风机，点表为1000:风速、1002:功率、"
         "1004:风向、1006:桨叶角度、1008:发电机转速、1010:齿轮箱油温、1012:塔筒温度、"
         "1014:空气温度、1016:空气湿度、1018:大气压强，它们的ip从192.168.1.101开始每台加1，"
         "端口都是502，从站号都是1；4#~5#是巴赫曼PLC风机，点表为2000:风速、2002:功率、"
         "2004:风向、2006:桨叶角度、2008:发电机转速、2010:齿轮箱油温、2012:塔筒温度、"
         "2014:空气温度、2016:空气湿度、2018:大气压强，4#的ip是192.168.2.10:502，"
         "5#的ip是192.168.2.11:502，从站号都是1。请接入这5台风机。")
NAMES10 = ["风速", "功率", "风向", "桨叶角度", "发电机转速",
           "齿轮箱油温", "塔筒温度", "空气温度", "空气湿度", "大气压强"]

MSG58 = ("现在接入6台巴赫曼PLC风机，1#~6#共用ip 192.168.2.1:502，从站号1~6，每台点表相同："
         "3000:风速、3002:功率、3004:风向、3006:桨叶角度、3008:发电机转速、3010:齿轮箱油温、"
         "3012:塔筒温度、3014:空气温度、3016:空气湿度、3018:大气压强。")

MSG59 = ("现在接入3台倍福PLC风机，1#~3#共用ip 192.168.1.200:502、从站号都是7，点表也相同，"
         "1#从1000开始：1000:风速、1002:功率、1004:风向、1006:桨叶角度、1008:发电机转速、"
         "1010:齿轮箱油温、1012:塔筒温度、1014:空气温度、1016:空气湿度、1018:大气压强，"
         "2#从2000开始，3#从3000开始，点名和相对位置与1#一致。")

MSG60 = ("现在接入3台asfp2数据源风机，1#监听9101，2#监听9102，3#监听9103，点表都是："
         "1000:风速、1001:功率、1002:风向、1003:桨叶角度、1004:发电机转速、1005:齿轮箱油温、"
         "1006:塔筒温度、1007:空气温度、1008:空气湿度、1009:大气压强。")

MSG61 = ("我们要接入10台逆变器，编号A01~A10，使用IEC104协议接入，IP是统一的172.16.228.45，"
         "每台逆变器对应一个端口，从2404开始一直到2413，每台点表相同，都是：100:风速、101:功率、"
         "102:风向、103:机舱温度、104:桨叶角度、105:发电机转速、106:齿轮箱油温、107:电网频率、"
         "108:有功功率、109:无功功率。")
NAMES61 = ["风速", "功率", "风向", "机舱温度", "桨叶角度",
           "发电机转速", "齿轮箱油温", "电网频率", "有功功率", "无功功率"]

MSG62 = ("现在接入2台asfp2数据源风机，都发到我们这边的9201端口，1#点表从1000开始："
         "1000:风速、1001:功率、1002:风向、1003:桨叶角度、1004:发电机转速、1005:齿轮箱油温、"
         "1006:塔筒温度、1007:空气温度、1008:空气湿度、1009:大气压强，2#点表与1#相同但从1020开始。")


# ── 断言助手 ───────────────────────────────────────────────
def insts_of(cfg, st):
    return [inst for (s, _), inst in rc.server_instances(cfg).items() if s == st]


def inst_by(cfg, st, pred):
    for inst in insts_of(cfg, st):
        if pred(inst):
            return inst
    return None


def reg_prefix_map():
    reg = base.registry()
    if not reg or not isinstance(reg.get("entries"), list):
        raise rc.Fail(f"注册表缺失或无 entries: {json.dumps(reg, ensure_ascii=False)[:200]}")
    return {e.get("prefix"): e for e in reg["entries"]}


def key_suffix(key):
    return key.split("_", 1)[1] if "_" in key else key


def assert_member_points(tag, prefix, inst, names, addrs, uid=None):
    """成员实例断言：点名集合、key 前缀、地址集合、（modbus）从站号。"""
    pts = inst.get("points", [])
    if len(pts) != len(names):
        raise rc.Fail(f"{tag}: {prefix} 点数 {len(pts)} ≠ {len(names)}")
    by_name = {}
    for p in pts:
        nm = p.get("name")
        if nm not in names:
            raise rc.Fail(f"{tag}: {prefix} 出现意外点名 {nm!r}")
        by_name[nm] = p
        if not str(p.get("id", "")).startswith(f"{prefix}_"):
            raise rc.Fail(f"{tag}: {prefix} 点 key 未带设备前缀: {p.get('id')}")
        if uid is not None and p.get("uid") != uid:
            raise rc.Fail(f"{tag}: {prefix} 点 {nm} uid={p.get('uid')} ≠ {uid}")
    got_names = sorted(by_name)
    if got_names != sorted(names):
        raise rc.Fail(f"{tag}: {prefix} 点名集合不符: {got_names}")
    got_addrs = sorted(p["addr"] for p in pts)
    if got_addrs != sorted(addrs):
        raise rc.Fail(f"{tag}: {prefix} 地址集合不符: {got_addrs}")


def assert_suffix_shared(tag, prefixes, name, pname):
    """模板相对 key 裁定：同一点名在各台的 key 后缀一致（wt1_windspeed / wt2_windspeed）。"""
    reg = reg_prefix_map()
    suffixes = set()
    for pre in prefixes:
        e = reg.get(pre)
        if e is None:
            raise rc.Fail(f"{tag}: 注册表缺 {pre} 条目")
        key = (e.get("pointMap") or {}).get(name)
        if not key:
            raise rc.Fail(f"{tag}: {pre} pointMap 缺 {name}: {e.get('pointMap')}")
        if not key.startswith(f"{pre}_"):
            raise rc.Fail(f"{tag}: {pre} pointMap key 未带前缀: {key}")
        suffixes.add(key_suffix(key))
    if len(suffixes) != 1:
        raise rc.Fail(f"{tag}: {name} 的 key 后缀跨台不一致: {sorted(suffixes)}")
    return suffixes.pop()


def assert_reader(tag, total_points, ip="127.0.0.1", port=21501):
    rd = insts_of(rc.read_config(), "c4_asfp2_client")
    if len(rd) != 1:
        raise rc.Fail(f"{tag}: reader 实例数 {len(rd)} ≠ 1")
    r = rd[0]
    if str(r.get("ip")) != ip or r.get("port") != port:
        raise rc.Fail(f"{tag}: reader 目标 {r.get('ip')}:{r.get('port')} ≠ {ip}:{port}")
    if len(r.get("points", [])) != total_points:
        raise rc.Fail(f"{tag}: reader 点数 {len(r.get('points', []))} ≠ {total_points}")
    addrs = [p.get("addr") for p in r.get("points", [])]
    if len(set(addrs)) != len(addrs):
        raise rc.Fail(f"{tag}: reader 转发地址重复（同目标 addr 必须全局唯一）")
    return r


def dialog_text():
    return "".join(base._assistant_texts())


def assert_template_once(tag, addr, name, at_most=1):
    """组模式等价形态断言：模板点表每组展示一次（不得逐台重复罗列）。
    v2.1.41 方案 Markdown 表格化后，模板行形态「| 地址 | 点名 | 点 key 后缀 |」。"""
    n = len(re.findall(rf"^\| {addr} \| {name} \|", dialog_text(), re.M))
    if n != at_most:
        raise rc.Fail(f"{tag}: 模板行「| {addr} | {name} |」出现 {n} 次 ≠ {at_most}")


# ── 链段 ───────────────────────────────────────────────────
def s57():
    conv = rc.Conv()
    base.flow(conv, MSG57, answers=GROUP_ANSWERS("采用modbus协议"),
              done=lambda: len(insts_of(rc.read_config() or {}, "c4_modbus_client")) >= 5)
    cfg = rc.wait_config(lambda c: len(insts_of(c, "c4_modbus_client")) == 5,
                         timeout=240, desc="57: 5 个 modbus 实例")
    # 组1：wt1~wt3 ip 递增 101~103；组2：wt4/wt5 显式列表；全部端口 502、从站号 1
    expect = [("wt1", "192.168.1.101", 1000), ("wt2", "192.168.1.102", 1000),
              ("wt3", "192.168.1.103", 1000), ("wt4", "192.168.2.10", 2000),
              ("wt5", "192.168.2.11", 2000)]
    for prefix, ip, base_addr in expect:
        inst = inst_by(cfg, "c4_modbus_client", lambda i, ip=ip: i.get("ip") == ip)
        if inst is None:
            raise rc.Fail(f"57: 缺 ip={ip} 的 modbus 实例: "
                          f"{[(i.get('ip'), i.get('port')) for i in insts_of(cfg, 'c4_modbus_client')]}")
        assert_member_points("57", prefix, inst, NAMES10,
                             list(range(base_addr, base_addr + 19, 2)), uid=1)
    assert_suffix_shared("57", ["wt1", "wt2", "wt3", "wt4", "wt5"], "风速", "windspeed")
    assert_reader("57", 50)
    assert_template_once("57", 1000, "风速")
    # v2.1.41 表格化后按台前缀在成员设备表「点 key 前缀」列（反引号单元格）
    if not re.search(r"\| 设备名 \| 点 key 前缀 \|", dialog_text()) \
            or "`wt1_`" not in dialog_text():
        raise rc.Fail("57: 方案未按台明示前缀（成员设备表缺「点 key 前缀」列或 wt1_ 单元格）")
    reg = base.registry()
    if reg.get("channelHighWatermark") != 6:
        raise rc.Fail(f"57: 水位 {reg.get('channelHighWatermark')} ≠ 6（5 writer + 1 reader）")
    base.assert_no_handle_leak("57")


def s58():
    conv = rc.Conv()
    base.flow(conv, MSG58, answers=GROUP_ANSWERS("采用modbus协议"),
              done=lambda: len(insts_of(rc.read_config() or {}, "c4_modbus_client")) >= 6)
    cfg = rc.wait_config(lambda c: len(insts_of(c, "c4_modbus_client")) == 6,
                         timeout=240, desc="58: 6 个 modbus 实例")
    for i in range(1, 7):
        prefix = f"wt{i}"
        inst = inst_by(cfg, "c4_modbus_client",
                       lambda x, p=prefix: str((x.get("points") or [{}])[0].get("id", "")).startswith(f"{p}_"))
        if inst is None:
            raise rc.Fail(f"58: 缺 {prefix} 实例（6 实例: "
                          f"{[str((x.get('points') or [{}])[0].get('id')) for x in insts_of(cfg, 'c4_modbus_client')]}）")
        if inst.get("port") != 502:
            raise rc.Fail(f"58: 实例端口 {inst.get('port')} ≠ 502")
        assert_member_points("58", prefix, inst, NAMES10,
                             list(range(3000, 3019, 2)), uid=i)
    assert_suffix_shared("58", [f"wt{i}" for i in range(1, 7)], "风速", "windspeed")
    assert_reader("58", 60)
    reg = base.registry()
    if reg.get("channelHighWatermark") != 7:
        raise rc.Fail(f"58: 水位 {reg.get('channelHighWatermark')} ≠ 7（6 writer + 1 reader）")
    base.assert_no_handle_leak("58")


def s59():
    conv = rc.Conv()
    base.flow(conv, MSG59, answers=GROUP_ANSWERS("采用modbus协议"),
              done=lambda: len(insts_of(rc.read_config() or {}, "c4_modbus_client")) >= 3)
    cfg = rc.wait_config(lambda c: len(insts_of(c, "c4_modbus_client")) == 3,
                         timeout=240, desc="59: 3 个 modbus 实例")
    bases = {"wt1": 1000, "wt2": 2000, "wt3": 3000}
    for prefix, b in bases.items():
        inst = inst_by(cfg, "c4_modbus_client",
                       lambda x, p=prefix: str((x.get("points") or [{}])[0].get("id", "")).startswith(f"{p}_"))
        if inst is None:
            raise rc.Fail(f"59: 缺 {prefix} 实例")
        if inst.get("ip") != "192.168.1.200":
            raise rc.Fail(f"59: {prefix} ip {inst.get('ip')} ≠ 192.168.1.200")
        assert_member_points("59", prefix, inst, NAMES10,
                             list(range(b, b + 19, 2)), uid=7)
    suffix = assert_suffix_shared("59", ["wt1", "wt2", "wt3"], "风速", "windspeed")
    if suffix == "2000" or suffix == "3000":
        raise rc.Fail(f"59: key 用了全局地址（{suffix}）——违反模板相对裁定")
    if "wt2_2000" in json.dumps(rc.read_config(), ensure_ascii=False):
        raise rc.Fail("59: config 出现 wt2_2000 形态 key（全局地址 key）")
    assert_reader("59", 30)
    reg = base.registry()
    if reg.get("channelHighWatermark") != 4:
        raise rc.Fail(f"59: 水位 {reg.get('channelHighWatermark')} ≠ 4（3 writer + 1 reader）")
    base.assert_no_handle_leak("59")


def occupy_port(port, seconds=1800):
    return subprocess.Popen(
        [sys.executable, "-c",
         f"import socket,time; s=socket.socket(); s.bind(('0.0.0.0',{port})); "
         f"s.listen(1); time.sleep({seconds})"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def s60():
    occupier = occupy_port(9102)
    try:
        time.sleep(1)
        conv = rc.Conv()
        base.flow(conv, MSG60, answers=GROUP_ANSWERS("采用asfp2协议"),
                  done=lambda: False, max_turns=8,
                  stop_on=[r"本次变更已恢复原样", r"执行过程中出现问题"])
        # 组级回滚（§2.11）：2# 启动失败 → 全组回滚到空态（首接无 .prev 空态裁定）
        rc.wait_config(lambda c: len(rc.server_instances(c or {})) == 0,
                       timeout=120, desc="60A: 组级回滚到空态")
        reg = base.registry()
        prefixes = [e.get("prefix") for e in ((reg or {}).get("entries") or [])]
        if prefixes:
            raise rc.Fail(f"60A: 回滚后注册表残留 {prefixes}")
        if not re.search(r"失败|恢复原样", dialog_text()):
            raise rc.Fail("60A: 无失败汇报信号")
        # 解除占用 → 重发同输入 → 整组成功
        occupier.terminate()
        time.sleep(2)
        base.flow(conv, MSG60, answers=GROUP_ANSWERS("采用asfp2协议"),
                  done=lambda: len(insts_of(rc.read_config() or {}, "c4_asfp2_server")) >= 3)
        cfg = rc.wait_config(lambda c: len(insts_of(c, "c4_asfp2_server")) == 3,
                             timeout=240, desc="60B: 3 个 asfp2_server 实例")
        for i, port in enumerate([9101, 9102, 9103], start=1):
            inst = inst_by(cfg, "c4_asfp2_server", lambda x, p=port: x.get("port") == p)
            if inst is None:
                raise rc.Fail(f"60B: 缺监听 {port} 的实例")
            assert_member_points("60", f"wt{i}", inst, NAMES10, list(range(1000, 1010)))
        assert_reader("60", 30)
        reg = base.registry()
        if reg.get("channelHighWatermark") != 4:
            raise rc.Fail(f"60B: 水位 {reg.get('channelHighWatermark')} ≠ 4（3 writer + 1 reader）")
        base.assert_no_handle_leak("60")
    finally:
        occupier.terminate()


def s61():
    conv = rc.Conv()
    base.flow(conv, MSG61, answers=GROUP_ANSWERS("采用iec104协议"),
              done=lambda: len(insts_of(rc.read_config() or {}, "c4_iec104_client")) >= 10)
    cfg = rc.wait_config(lambda c: len(insts_of(c, "c4_iec104_client")) == 10,
                         timeout=300, desc="61: 10 个 iec104 实例")
    for i in range(1, 11):
        prefix = f"nbA{i:02d}"
        port = 2403 + i
        inst = inst_by(cfg, "c4_iec104_client", lambda x, p=port: x.get("port") == p)
        if inst is None:
            raise rc.Fail(f"61: 缺端口 {port} 的 iec104 实例: "
                          f"{[(x.get('ip'), x.get('port')) for x in insts_of(cfg, 'c4_iec104_client')]}")
        if inst.get("ip") != "172.16.228.45":
            raise rc.Fail(f"61: {prefix} ip {inst.get('ip')} ≠ 172.16.228.45")
        assert_member_points("61", prefix, inst, NAMES61, list(range(100, 110)))
    assert_suffix_shared("61", [f"nbA{i:02d}" for i in range(1, 11)], "风速", "windspeed")
    assert_reader("61", 100)
    assert_template_once("61", 100, "风速")
    reg = base.registry()
    if reg.get("channelHighWatermark") != 11:
        raise rc.Fail(f"61: 水位 {reg.get('channelHighWatermark')} ≠ 11（10 writer + 1 reader）")
    base.assert_no_handle_leak("61")


def s62():
    conv = rc.Conv()
    base.flow(conv, MSG62, answers=GROUP_ANSWERS("采用asfp2协议"),
              done=lambda: len(insts_of(rc.read_config() or {}, "c4_asfp2_server")) >= 1)
    cfg = rc.wait_config(lambda c: len(insts_of(c, "c4_asfp2_server")) == 1,
                         timeout=240, desc="62: 并入后仅 1 个 asfp2_server 实例")
    inst = insts_of(cfg, "c4_asfp2_server")[0]
    if inst.get("port") != 9201:
        raise rc.Fail(f"62: 监听端口 {inst.get('port')} ≠ 9201")
    pts = inst.get("points", [])
    if len(pts) != 20:
        raise rc.Fail(f"62: 单实例点数 {len(pts)} ≠ 20（两台并入）")
    wt1 = sorted(p["addr"] for p in pts if str(p.get("id", "")).startswith("wt1_"))
    wt2 = sorted(p["addr"] for p in pts if str(p.get("id", "")).startswith("wt2_"))
    if wt1 != list(range(1000, 1010)):
        raise rc.Fail(f"62: wt1 地址 {wt1}")
    if wt2 != list(range(1020, 1030)):
        raise rc.Fail(f"62: wt2 地址 {wt2}（偏移 +20）")
    reg = base.registry()
    entries = {e.get("prefix"): e for e in reg.get("entries", [])}
    if sorted(entries) != ["wt1", "wt2"]:
        raise rc.Fail(f"62: 注册表条目 {sorted(entries)} ≠ [wt1, wt2]")
    hosts = {entries["wt1"].get("host"), entries["wt2"].get("host")}
    if len(hosts) != 1:
        raise rc.Fail(f"62: 两台 host 不一致: {hosts}（应并入同一实例）")
    if entries["wt1"].get("host") != inst.get("id"):
        raise rc.Fail(f"62: 注册表 host {entries['wt1'].get('host')} ≠ 实例 {inst.get('id')}")
    if reg.get("channelHighWatermark") != 2:
        raise rc.Fail(f"62: 水位 {reg.get('channelHighWatermark')} ≠ 2（1 writer + 1 reader）")
    if len(insts_of(rc.read_config(), "c4_asfp2_client")) != 1:
        raise rc.Fail("62: reader 实例数 ≠ 1")
    base.assert_no_handle_leak("62")


# ── 链调度（串行，链间回零，失败即停）────────────────────
CHAIN_TAG = "C4He1"
CHAIN_FAIL_DIR = base.CHAIN_FAIL_DIR

CHAINS = [
    ("57", ["57"], s57),
    ("58", ["58"], s58),
    ("59", ["59"], s59),
    ("60", ["60"], s60),
    ("61", ["61"], s61),
    ("62", ["62"], s62),
]


def main():
    only = set(sys.argv[1:])
    chains = [c for c in CHAINS if not only or c[0] in only]
    rc.log(f"════ 设备组批量接入串行测试开始：{'/'.join(c[0] for c in chains)} ════")
    base.chain_clean()
    rc.MCP_STACK.up()
    agent = base.V21Agent()
    agent.up()
    rc.AGENT = agent
    for label, cases, fn in chains:
        rc.log(f"════ 链段 {label} 开始 ════")
        rc.PH.reset(label)
        t0 = time.time()
        try:
            base.chain_clean()
            rc.MCP_STACK.up()
            agent = base.V21Agent()
            agent.up()
            rc.AGENT = agent
            fn()
            rc.PH.check(label)
        except rc.Fail as e:
            base.dump_diag(label, e)
            rc.log(f"════ 链段 {label} FAIL: {e} ════")
            rc.log(f"════ 设备组批量接入在第 {label} 段停止（已通过: {PASSED or '无'}）════")
            sys.exit(1)
        except Exception as e:
            base.dump_diag(label, e)
            rc.log(f"════ 链段 {label} 异常 {type(e).__name__}: {str(e)[:400]} ════")
            rc.log(f"════ 设备组批量接入在第 {label} 段停止（已通过: {PASSED or '无'}）════")
            sys.exit(1)
        PASSED.extend(cases)
        rc.log(f"════ 链段 {label} PASS（{time.time()-t0:.0f}s）════")
    rc.log(f"════ 设备组批量接入全部通过：{PASSED} ════")
    rc.log(f"════ 标注清单（AI测试通过 {CHAIN_TAG}）：{PASSED} ════")


if __name__ == "__main__":
    main()
