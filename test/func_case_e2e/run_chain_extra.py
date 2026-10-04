#!/usr/bin/env python3
# func_test_case.md 剩余用例第一批串行驱动器（2026-10-04，用例 4/10/13/14/44/45/53/56）
# 链段（各自独立回零；10/53/56 段内先按用例 1 接入 1号风机作前置）：
#   13A —— 协议必供：消息无协议信息 → 先询问协议、不得出方案；补答后接入成功
#   13B —— 未知协议 fail-fast：「abc」→ 可读错误 + 已部署协议清单；改声明 asfp2 后成功
#   14  —— 联合声明：「接收和转发都是用asfp2协议」单句双侧命中（BOTH_SIDES_RE），
#          无逐侧协议追问直接出方案
#   4   —— 转发点表等价应答：「点表与I区一致」→ 方案层镜像推导（转发地址=采集地址）
#   44  —— 提问即终局：缺从站号/端口/转发信息时单缺口提问、无方案；补答后正常推进
#         （modbus 设备 IP 不可达，仅 config 断言，无数据面）
#   45  —— 寄存器重叠方案期拒绝（3008/3009 相邻 float32）→ 取消 → 修正 3010 重发成功
#   10  —— 追加点撞名：与既有点同名 → 澄清拒绝（禁自动改名）→ 换名后成对写入
#   53  —— site_ambiguous：子集场站名（阿拉善风电场）→ 归属确认（不误拒）→ 确认后接入
#   56  —— 源点名改名：pointMap 重绑定、key/shm_id 不变、翻译漂移负断言
# 注意：转发接收器一律先于执行拉起（转发客户端首连 refused 后 30s 退避，
# 数据面烟囱 20s 窗口会错过晚启的接收器，2026-10-04 13A 实测）。
# 规则：逐项串行、任一链段 FAIL 立即停止并落盘诊断。
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_cases as rc  # noqa: E402
import run_chain_v21 as base  # noqa: E402

PASSED = []

SITE_PRE = "场站名称：华能阿拉善。"

MSG13A = (SITE_PRE +
          "现在需要接入1号风机的数据，第三方厂家给我们转来1#风机数据，10个点，从1000到1009，"
          "10个点分别是1000:风速、1001:功率、1002:风向、1003:桨叶角度、1004:发电机转速、"
          "1005:齿轮箱油温、1006:塔筒温度、1007:空气温度、1008:空气湿度、1009:大气压强，"
          "使用端口9001。我们需要将这些数据转发到II区服务器上，目标地址是127.0.0.1:9900，"
          "点表5000~5009。")

MSG13B = (SITE_PRE +
          "使用abc协议接入1号风机的数据，第三方厂家给我们转来1#风机数据，10个点，从1000到1009，"
          "10个点分别是1000:风速、1001:功率、1002:风向、1003:桨叶角度、1004:发电机转速、"
          "1005:齿轮箱油温、1006:塔筒温度、1007:空气温度、1008:空气湿度、1009:大气压强，"
          "使用端口9001。我们需要将这些数据转发到II区服务器上，转发采用asfp2协议，"
          "目标地址是127.0.0.1:9900，点表5000~5009。")

MSG14 = (SITE_PRE +
         "现在需要接入1号风机的数据，第三方厂家给我们转来1#风机数据，10个点，从1000到1009，"
         "10个点分别是1000:风速、1001:功率、1002:风向、1003:桨叶角度、1004:发电机转速、"
         "1005:齿轮箱油温、1006:塔筒温度、1007:空气温度、1008:空气湿度、1009:大气压强，"
         "使用端口9001。我们需要将这些数据转发到II区服务器上，目标地址是127.0.0.1:9900，"
         "点表5000~5009。接收和转发都是用asfp2协议。")

MSG4 = (SITE_PRE +
        "现在需要接入升压站的数据，第三方厂家转过来的，asfp2协议，2390:电网频率，"
        "2391是正向有功，2392是反向有功，2393是正向无功，2394是反向无功，2395是uab，"
        "2396是ubc，2397是uac，2398是变压器油温，2399是环境温度。"
        "II服务器地址是127.0.0.1:9900，点表与I区一致。")

MSG44 = (SITE_PRE +
         "现在需要接入1号风机齿轮箱油泵控制器的数据，modbus协议，设备IP是192.168.110.52。"
         "点表8个点：40001:油泵电流、40003:油泵温度、40005:油压、40007:油位、40009:电机转速、"
         "40011:阀开度、40013:油温2、40015:滤网压差，全部float32、swap=2。"
         # 句读注意：点表句不可与「转发到…」同句——句级作用域筛选把含转发词的整句
         # 划给转发侧，采集侧提取将看不到点表（2026-10-04 44 实测）
         "转发到II区服务器。")

MSG45_BODY = ("现在需要接入1号风机变桨控制器的数据，厂家在变桨控制器上开放了modbus采集口，"
              "设备IP是192.168.110.51，端口502，从站号1。点表10个点：3000:桨叶角度、"
              "3002:变桨速度、3004:变桨电机温度、3006:变桨电机电流、3008:后备电源电压、"
              "{temp_addr}:后备电源温度、3012:桨叶1位置、3014:桨叶2位置、3016:桨叶3位置、"
              "3018:轮毂温度。除桨叶1/2/3位置是输入寄存器（功能码4）外，其余全是保持寄存器"
              "（功能码3），全部32位浮点、字节交换2（swap=2）。我们需要将这些数据转发到"
              "II区服务器上，转发采用asfp2协议，目标地址是127.0.0.1:9900，点表5000~5009。")
MSG45A = SITE_PRE + MSG45_BODY.format(temp_addr=3009)   # 与 3008 相邻 → float32 跨度重叠
MSG45B = SITE_PRE + MSG45_BODY.format(temp_addr=3010)   # 修正版

MSG10 = "给1#风机再追加一个数据点，地址1010"

MSG53 = ("现在需要接入阿拉善风电场2号风机的数据，第三方厂家通过asfp2协议给我们转来2#风机数据，"
         "10个点，从1100到1109，10个点分别是1100:风速、1101:功率、1102:风向、1103:桨叶角度、"
         "1104:发电机转速、1105:齿轮箱油温、1106:塔筒温度、1107:空气温度、1108:空气湿度、"
         "1109:大气压强，使用端口9002。我们需要将这些数据转发到II区服务器上，转发采用asfp2协议，"
         "目标地址是127.0.0.1:9900，点表5100~5109。")

MSG56 = "把1号风机的齿轮箱油温点改个名字，叫主轴承温度，地址1005不变。"


def json_unchanged(before):
    return (json.dumps(before, sort_keys=True)
            == json.dumps(rc.read_config(), sort_keys=True))


def modbus_writer_of(cfg, addr):
    """modbus 采集实例定位（rc.writer_of 仅查 c4_asfp2_server）。"""
    for (st, iid), inst in rc.server_instances(cfg).items():
        if st == "c4_modbus_client" and addr in {p["addr"] for p in inst.get("points", [])}:
            return iid
    return None


def wt1_point(cfg, addr):
    return rc.points_of(cfg, "c4_asfp2_server", rc.writer_of(cfg, 1000) or "").get(addr, {})


def prep_case1():
    """段内前置：全新环境按用例 1 输入接入 1号风机（顺带绑定场站）。"""
    fwd = rc.start_receiver(rc.P_FWD1)
    try:
        conv = rc.Conv()
        base.flow(conv, rc.MSG_CASE1, answers=[(r"场站", "华能阿拉善")],
                  done=lambda: rc.writer_of(rc.read_config(), 1000) is not None)
        cfg = rc.wait_config(lambda c: rc.writer_of(c, 1000) is not None
                             and rc.forward_of(c, 5000) is not None,
                             timeout=240, desc="段内前置：1号风机完成态")
        return fwd, conv, cfg
    except Exception:
        fwd.stop()
        raise


# ── 链段实现 ───────────────────────────────────────────────
def s13a():
    """13①：消息无协议信息 → 先询问协议（不出方案）；补答 asfp2 后接入成功。"""
    fwd = rc.start_receiver(rc.P_FWD1)
    try:
        conv = rc.Conv()
        text1 = conv.send(MSG13A)
        if not re.search(r"协议", text1):
            raise rc.Fail(f"13A①: 无协议信息未询问协议: {text1[:200]}")
        if "是否确认" in text1 or "确认执行" in text1:
            raise rc.Fail(f"13A①: 协议缺失即推进到方案: {text1[:200]}")
        base.flow(conv, "asfp2", done=lambda: rc.writer_of(rc.read_config(), 1000) is not None)
        cfg = rc.wait_config(lambda c: rc.writer_of(c, 1000) is not None
                             and rc.forward_of(c, 5000) is not None,
                             timeout=240, desc="13A: 补答协议后接入完成")
        base.assert_writer_keys(cfg, "wt1", range(1000, 1010))
        rc.wait_port(rc.P_RECV1, True)
        base.data_smoke(fwd, rc.P_RECV1, 1000, 1009)
    finally:
        fwd.stop()
    base.assert_no_handle_leak("13A")


def s13b():
    """13②：未知协议 abc → fail-fast 列清单不出方案；改声明 asfp2 后接入成功。"""
    fwd = rc.start_receiver(rc.P_FWD1)
    try:
        conv = rc.Conv()
        text1 = conv.send(MSG13B)
        if "abc" not in text1:
            raise rc.Fail(f"13B①: 未回显未知协议名: {text1[:200]}")
        if not (re.search(r"asfp2", text1) and re.search(r"modbus", text1)
                and re.search(r"iec104", text1)):
            raise rc.Fail(f"13B①: 未列出已部署协议清单: {text1[:250]}")
        if "是否确认" in text1 or "确认执行" in text1:
            raise rc.Fail(f"13B①: 未知协议即推进到方案: {text1[:200]}")
        base.flow(conv, "使用asfp2协议",
                  done=lambda: rc.writer_of(rc.read_config(), 1000) is not None)
        cfg = rc.wait_config(lambda c: rc.writer_of(c, 1000) is not None
                             and rc.forward_of(c, 5000) is not None,
                             timeout=240, desc="13B: 纠正协议后接入完成")
        base.assert_writer_keys(cfg, "wt1", range(1000, 1010))
        rc.wait_port(rc.P_RECV1, True)
        base.data_smoke(fwd, rc.P_RECV1, 1000, 1009)
    finally:
        fwd.stop()
    base.assert_no_handle_leak("13B")


def s14():
    """联合声明单句双侧命中：无逐侧协议追问，方案双侧 asfp2，接入成功。"""
    fwd = rc.start_receiver(rc.P_FWD1)
    try:
        conv = rc.Conv()
        base.flow(conv, MSG14, done=lambda: rc.writer_of(rc.read_config(), 1000) is not None)
        cfg = rc.wait_config(lambda c: rc.writer_of(c, 1000) is not None
                             and rc.forward_of(c, 5000) is not None,
                             timeout=240, desc="14: 联合声明接入完成")
        dialog = "".join(base._assistant_texts())
        if re.search(r"请提供.{0,10}协议", dialog):
            raise rc.Fail("14: 联合声明后仍逐侧追问协议")
        plan_text = next((t for t in base._assistant_texts() if "是否确认" in t), "")
        if plan_text.count("asfp2") < 2:
            raise rc.Fail(f"14: 方案未双侧展示 asfp2: {plan_text[:200]}")
        base.assert_writer_keys(cfg, "wt1", range(1000, 1010))
        rc.wait_port(rc.P_RECV1, True)
        base.data_smoke(fwd, rc.P_RECV1, 1000, 1009)
    finally:
        fwd.stop()
    base.assert_no_handle_leak("14")


def s4():
    """等价应答：「点表与I区一致」→ 镜像推导，转发地址=采集地址 2390~2399。"""
    fwd = rc.start_receiver(rc.P_FWD1)
    try:
        conv = rc.Conv()
        base.flow(conv, MSG4,
                  answers=[(r"端口", "监听9001端口"),
                           (r"同时配置转发|转发协议与目标", "转发采用asfp2协议到127.0.0.1:9900"),
                           (r"转发点表", "点表与I区一致")],
                  done=lambda: rc.writer_of(rc.read_config(), 2390) is not None)
        cfg = rc.wait_config(lambda c: rc.writer_of(c, 2390) is not None,
                             timeout=240, desc="4: 升压站等价应答接入完成")
        base.assert_channel_ids(cfg)
        w, wid = base.assert_writer_keys(cfg, "syz", range(2390, 2400))
        if w.get(2390, {}).get("name") != "电网频率":
            raise rc.Fail(f"4: 混述点表未逐点解析: {w.get(2390)}")
        fid = rc.forward_of(cfg, 2390)
        if fid is None:
            raise rc.Fail("4: 转发侧未落盘")
        fpts = rc.points_of(cfg, "c4_asfp2_client", fid)
        if sorted(p["addr"] for p in fpts.values()) != list(range(2390, 2400)):
            raise rc.Fail(f"4: 转发地址未镜像采集地址: {sorted(p['addr'] for p in fpts.values())}")
        rc.wait_port(rc.P_RECV1, True)
        base.data_smoke(fwd, rc.P_RECV1, 2390, 2399)
    finally:
        fwd.stop()
    base.assert_no_handle_leak("4")


def s44():
    """提问即终局：缺必要项时单缺口提问（无方案不落盘）；补答后正常推进。"""
    conv = rc.Conv()
    before = rc.read_config()
    text1 = conv.send(MSG44)
    if not re.search(r"从站号|uid|端口", text1):
        raise rc.Fail(f"44①: 缺失必要项未提问: {text1[:200]}")
    if "是否确认" in text1 or "确认执行" in text1:
        raise rc.Fail(f"44①: 信息不完整即出方案: {text1[:200]}")
    if not json_unchanged(before):
        raise rc.Fail("44①: 提问回合写入了 config")
    base.flow(conv, "从站号都是1",
              answers=[(r"端口", "端口502"),
                       (r"转发协议|请提供.{0,12}转发", "转发采用asfp2协议到127.0.0.1:9900"),
                       (r"转发点表", "点表5000~5007")],
              done=lambda: modbus_writer_of(rc.read_config(), 40001) is not None)
    cfg = rc.wait_config(lambda c: modbus_writer_of(c, 40001) is not None,
                         timeout=240, desc="44②: 补答后接入完成")
    wid = modbus_writer_of(cfg, 40001)
    w = rc.points_of(cfg, "c4_modbus_client", wid)
    if len(w) != 8:
        raise rc.Fail(f"44②: 点数 {len(w)} ≠ 8")
    if w.get(40001, {}).get("uid") != 1:
        raise rc.Fail(f"44②: 从站号未落盘: {w.get(40001)}")


def s45():
    """重叠方案期拒绝 → 取消 → 修正 3010 重发成功（不落盘坏配置）。"""
    conv = rc.Conv()
    before = rc.read_config()
    text1 = conv.send(MSG45A)
    if not re.search(r"重叠", text1):
        raise rc.Fail(f"45①: 相邻 float32 未触发重叠拒绝: {text1[:250]}")
    if "是否确认" in text1 or "确认执行" in text1:
        raise rc.Fail(f"45①: 冲突未决进入可确认方案: {text1[:200]}")
    if not json_unchanged(before):
        raise rc.Fail("45①: 冲突请求写入了 config")
    conv.send("取消")
    base.flow(conv, MSG45B,
              done=lambda: modbus_writer_of(rc.read_config(), 3000) is not None)
    cfg = rc.wait_config(lambda c: modbus_writer_of(c, 3000) is not None
                         and rc.forward_of(c, 5000) is not None,
                         timeout=240, desc="45②: 修正后接入完成")
    wid = modbus_writer_of(cfg, 3000)
    w = rc.points_of(cfg, "c4_modbus_client", wid)
    if len(w) != 10:
        raise rc.Fail(f"45②: 点数 {len(w)} ≠ 10")
    if 3009 in w:
        raise rc.Fail("45②: 重叠地址 3009 仍落盘")
    if w.get(3010, {}).get("name") != "后备电源温度":
        raise rc.Fail(f"45②: 修正地址未生效: {w.get(3010)}")
    if w.get(3000, {}).get("type") != 10 or w.get(3012, {}).get("fun") != 4:
        raise rc.Fail(f"45②: fun/type 声明未落盘: {w.get(3000)} / {w.get(3012)}")
    base.assert_no_handle_leak("45")


def s10():
    """撞名澄清：新点与既有点同名 → 拒绝澄清（禁自动改名）→ 换名后成对写入。"""
    fwd, conv, cfg0 = prep_case1()
    try:
        n0 = len(rc.points_of(cfg0, "c4_asfp2_server", rc.writer_of(cfg0, 1000)))
        text1 = conv.send(MSG10)
        if not re.search(r"点名", text1):
            raise rc.Fail(f"10①: 追加点未询问点名: {text1[:200]}")
        text2 = conv.send("风向")
        if not (re.search(r"重名", text2) and "1002" in text2):
            raise rc.Fail(f"10②: 与既有点同名未澄清拒绝: {text2[:250]}")
        base.flow(conv, "塔筒振动",
                  answers=[(r"转发", "转发地址5010")],
                  done=lambda: rc.writer_of(rc.read_config(), 1010) is not None)
        cfg = rc.wait_config(lambda c: rc.writer_of(c, 1010) is not None
                             and rc.forward_of(c, 5010) is not None,
                             timeout=240, desc="10③: 换名后成对写入")
        wid = rc.writer_of(cfg, 1000)
        w = rc.points_of(cfg, "c4_asfp2_server", wid)
        if len(w) != n0 + 1:
            raise rc.Fail(f"10③: 点数 {len(w)} ≠ {n0 + 1}（静默去重或多写）")
        if w[1002].get("name") != "风向" or w.get(1010, {}).get("name") == "风向":
            raise rc.Fail(f"10③: 既有点被覆盖或新点仍撞名: {w.get(1010)}")
        if str(w[1010].get("id")) == str(w[1002].get("id")):
            raise rc.Fail(f"10③: 新点 id 与既有点相同: {w[1010]}")
        base.assert_forward_key(cfg, 5010, wid, 1010)
        base.data_smoke(fwd, rc.P_RECV1, 1000, 1010)
    finally:
        fwd.stop()
    base.assert_no_handle_leak("10")


def s53():
    """site_ambiguous：子集场站名触发归属确认（不误拒），确认后正常接入。"""
    fwd, conv, _ = prep_case1()
    try:
        text1 = conv.send(MSG53)
        if not re.search(r"归属不明确|请确认", text1):
            raise rc.Fail(f"53①: 子集场站名未触发归属确认: {text1[:250]}")
        if "不属于当前场站" in text1:
            raise rc.Fail(f"53①: 误判为他站拒绝: {text1[:250]}")
        base.flow(conv, "就是本场站的，请继续接入",
                  done=lambda: rc.writer_of(rc.read_config(), 1100) is not None)
        cfg = rc.wait_config(lambda c: rc.writer_of(c, 1100) is not None
                             and rc.forward_of(c, 5100) is not None,
                             timeout=240, desc="53②: 确认归属后接入完成")
        base.assert_writer_keys(cfg, "wt2", range(1100, 1110))
        base.data_smoke(fwd, rc.P_RECV2, 1100, 1109)
    finally:
        fwd.stop()
    base.assert_no_handle_leak("53")


def s56():
    """改名重绑定：name 更新、key/shm_id 不变、pointMap 重绑、无翻译漂移新点。"""

    def renamed(cfg=None):
        # wait_config 以 fn(cfg) 轮询、flow 以 done() 无参调用——参数兼容两种入口
        #（2026-10-04 实测：零参闭包被 wait_config 每次以 TypeError 拒绝且被
        # except 静默吞掉，240s 假超时——config 实际早已改名生效）
        c = cfg if cfg is not None else rc.read_config()
        return wt1_point(c, 1005).get("name") == "主轴承温度"

    fwd, conv, cfg0 = prep_case1()
    try:
        wid0 = rc.writer_of(cfg0, 1000)
        old = rc.points_of(cfg0, "c4_asfp2_server", wid0)[1005]
        old_fid = rc.forward_of(cfg0, 5005)
        base.flow(conv, MSG56, done=renamed)
        cfg = rc.wait_config(renamed, timeout=240, desc="56: 改名生效")
        wid = rc.writer_of(cfg, 1000)
        w = rc.points_of(cfg, "c4_asfp2_server", wid)
        if len(w) != 10:
            raise rc.Fail(f"56: 点数 {len(w)} ≠ 10——漂移误建新点")
        p = w[1005]
        if p.get("id") != old.get("id") or p.get("shm_id") != old.get("shm_id"):
            raise rc.Fail(f"56: key/shm_id 被改动: {old} → {p}")
        if rc.forward_of(cfg, 5005) != old_fid:
            raise rc.Fail("56: 转发侧被变更（引用 key 不变原则）")
        e = base.entry("wt1")
        pm = (e or {}).get("pointMap", {})
        if pm.get("主轴承温度") != "wt1_gearbox_oil_temp":
            raise rc.Fail(f"56: pointMap 未重绑定新名: {pm}")
    finally:
        fwd.stop()
    base.assert_no_handle_leak("56")


CHAINS = [
    ("13A", ["13"], s13a),
    ("13B", ["13"], s13b),
    ("14", ["14"], s14),
    ("4", ["4"], s4),
    ("44", ["44"], s44),
    ("45", ["45"], s45),
    ("10", ["10"], s10),
    ("53", ["53"], s53),
    ("56", ["56"], s56),
]


def main():
    only = set(sys.argv[1:])
    chains = [c for c in CHAINS if not only or c[0] in only]
    rc.log(f"════ 剩余用例第一批串行测试开始：{'/'.join(c[0] for c in chains)} ════")
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
            rc.log(f"════ 剩余用例第一批在第 {label} 段停止（已通过: {PASSED or '无'}）════")
            sys.exit(1)
        except Exception as e:
            base.dump_diag(label, e)
            rc.log(f"════ 链段 {label} 异常 {type(e).__name__}: {str(e)[:400]} ════")
            rc.log(f"════ 剩余用例第一批在第 {label} 段停止（已通过: {PASSED or '无'}）════")
            sys.exit(1)
        PASSED.extend(cases)
        rc.log(f"════ 链段 {label} PASS（{time.time()-t0:.0f}s）════")
    rc.log(f"════ 剩余用例第一批全部通过：{PASSED} ════")
    rc.log(f"════ 标注清单（AI测试通过 C4He1）：{PASSED} ════")


if __name__ == "__main__":
    main()
