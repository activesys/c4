#!/usr/bin/env python3
# func_test_case.md 并入组 B3/B4/B2 串行驱动器（v2.1.0 命名体系）
# 三链各自独立回零（链首 A.3 = 全新环境按用例 1 输入接入，场站询问答「华能阿拉善」）：
#   B3: 用例 22 —— 2号风机独占 9002（接收不同端口）+ 转发同目标 9900（连接型每设备
#       一实例，双转发实例并存），点表 6000~6009 与既有 5000~5009 无重叠
#   B4: 用例 49 —— 同名设备消歧与前缀顺延（同名描述不匹配 → 追问；坚持同名 →
#       wt2 顺延 + 并入宿主 + 注册表以 description 区分）
#   B2: 用例 24 —— 并入 + 转发地址冲突混合（① 9900 上 5000~5009 完全重叠 → 方案期
#       拒绝；② 改 7100~7109 后并入成功）
# 规则（用户指令）：逐项串行、不并行；任一链步 FAIL 立即停止全链、落盘诊断、不修改。
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_cases as rc  # noqa: E402
import run_chain_v21 as base  # noqa: E402

P_RECV1, P_RECV2, P_FWD1 = rc.P_RECV1, rc.P_RECV2, rc.P_FWD1
PASSED = []           # 命中的 func_test_case.md 用例号
SETUP_ONLY = {"A.3"}

MSG22 = ("再接入2号风机，第三方厂家通过asfp2协议转来2#风机数据，10个点，从1100到1109，"
         "分别是1100:风速、1101:功率、1102:风向、1103:桨叶角度、1104:发电机转速、"
         "1105:齿轮箱油温、1106:塔筒温度、1107:空气温度、1108:空气湿度、1109:大气压强，"
         "使用端口9002。转发到II区服务器127.0.0.1:9900（与1#风机相同的目标），"
         "转发采用asfp2协议，点表6000~6009。")
MSG49A = ("再接入1号风机的数据，这是另一台同型号机组（备用机），第三方厂家通过asfp2协议"
          "转来数据，10个点，从1400到1409，1400:风速、1401:功率、1402:风向、"
          "1403:桨叶角度、1404:发电机转速、1405:齿轮箱油温、1406:塔筒温度、"
          "1407:空气温度、1408:空气湿度、1409:大气压强，使用端口9001。")
MSG49B = "就是新增一台，也叫1号风机。"
MSG24A = ("再接入4号风机，第三方厂家通过asfp2协议转来4#风机数据，10个点，从1300到1309，"
          "分别是1300:风速、1301:功率、1302:风向、1303:桨叶角度、1304:发电机转速、"
          "1305:齿轮箱油温、1306:塔筒温度、1307:空气温度、1308:空气湿度、1309:大气压强，"
          "使用端口9001。转发到II区服务器127.0.0.1:9900，转发采用asfp2协议，点表5000~5009。")
MSG24B = "转发点表改为7100~7109。"


def asfp_count(cfg, st):
    return sum(1 for k in rc.server_instances(cfg) if k[0] == st)


def prep_a3():
    """A.3 链首准备（同 B1）：全新环境接入 1号风机 → 用例 1 完成态，水位=2。"""
    conv = rc.Conv()
    base.flow(conv, rc.MSG_CASE1, answers=[(r"场站", "华能阿拉善")])
    cfg = rc.wait_config(lambda c: rc.writer_of(c, 1000) is not None
                         and rc.forward_of(c, 5000) is not None,
                         timeout=240, desc="A.3: 用例 1 完成态")
    if base.site_of_agent_json() is None:
        raise rc.Fail("A.3: site 未绑定")
    if (rc.writer_of(cfg, 1000), rc.forward_of(cfg, 5000)) != ("channel1", "channel2"):
        raise rc.Fail(f"A.3: 句柄 {rc.writer_of(cfg, 1000)}/{rc.forward_of(cfg, 5000)} "
                      "≠ channel1/channel2")
    if base.registry().get("channelHighWatermark") != 2:
        raise rc.Fail(f"A.3: 水位 ≠ 2: {base.registry().get('channelHighWatermark')}")


# ── B3：用例 22 ────────────────────────────────────────────
def s22():
    """2号风机独占 9002 + 转发同目标 9900 双实例并存，两路数据面均通。"""
    fwd = rc.start_receiver(P_FWD1)   # 9900：将同时收到 1号与2号 两路
    try:
        conv = rc.Conv()
        base.flow(conv, MSG22, done=lambda: rc.writer_of(rc.read_config(), 1100) is not None)
        cfg = rc.wait_config(lambda c: rc.writer_of(c, 1100) is not None
                             and rc.forward_of(c, 6000) is not None,
                             timeout=240, desc="22: 2号采集+转发实例落地")
        base.assert_channel_ids(cfg)
        # ① 独立采集实例（不并入——9002 无既有监听），wt2_* 十点
        wid2 = rc.writer_of(cfg, 1100)
        if wid2 != "channel3":
            raise rc.Fail(f"22: 2号采集实例 {wid2} ≠ channel3（水位接续）")
        base.assert_writer_keys(cfg, "wt2", range(1100, 1110))
        # ② 连接型转发每设备一实例：9900 上两个转发实例并存（channel2/ch4）
        fid2 = base.assert_forward_key(cfg, 6000, wid2, 1100)
        fid1 = rc.forward_of(cfg, 5000)
        if fid2 == fid1 or fid1 != "channel2":
            raise rc.Fail(f"22: 转发实例并存不成立: 1号={fid1} 2号={fid2}")
        if asfp_count(cfg, "c4_asfp2_client") != 2:
            raise rc.Fail("22: c4_asfp2_client 实例数 ≠ 2（同目标应两实例并存）")
        # ③ 无重叠（6000~6009 vs 5000~5009）方可并存——本例正常执行
        # ④ 1号既有转发表逐字节不变
        f1 = rc.points_of(cfg, "c4_asfp2_client", fid1)
        for a in range(5000, 5010):
            if a not in f1:
                raise rc.Fail(f"22: 1号转发表 addr={a} 丢失")
        base.entry("wt2", name="2号风机", host=wid2)
        if base.registry().get("channelHighWatermark") != 4:
            raise rc.Fail(f"22: 水位={base.registry().get('channelHighWatermark')} ≠ 4")
        # 数据面：两路分别注入、9900 同一接收端均出数
        base.data_smoke(fwd, P_RECV2, 1100, 1110)   # 2号链路
        base.data_smoke(fwd, P_RECV1, 1000, 1010)   # 1号链路无损
        base.assert_no_handle_leak("22")
    finally:
        fwd.stop()


# ── B4：用例 49 ────────────────────────────────────────────
def s49():
    """同名消歧：同名描述不匹配 → 追问；坚持同名 → wt2 顺延并入宿主、注册表双条目。"""
    conv = rc.Conv()
    text, clicked = base.flow(conv, MSG49A,
                              done=lambda: rc.writer_of(rc.read_config(), 1400) is not None,
                              max_turns=6)
    # ① 同名命中 → 描述仲裁 → 必有区分追问（不得静默复用 wt1、不得静默新建）
    dialog = "".join(base._assistant_texts())
    if not re.search(r"同名|已有一台|区分|另一台|重新说明", dialog):
        raise rc.Fail(f"49①: 无同名消歧追问: {text[:200]}")
    text, _ = base.flow(conv, MSG49B,
                        done=lambda: rc.writer_of(rc.read_config(), 1400) is not None,
                        max_turns=8)
    cfg = rc.wait_config(lambda c: rc.writer_of(c, 1400) is not None,
                         timeout=240, desc="49②: 坚持同名后 wt2 接入")
    base.assert_channel_ids(cfg)
    # ② 坚持同名 → 前缀顺延 wt2、并入宿主 channel1（同端口同协议）
    wid = rc.writer_of(cfg, 1400)
    if wid != "channel1":
        raise rc.Fail(f"49②: 新设备落在 {wid} ≠ 宿主 channel1（同端口应并入）")
    base.assert_writer_keys(cfg, "wt2", range(1400, 1410))
    if rc.points_of(cfg, "c4_asfp2_server", wid).get(1000, {}).get("id") != "wt1_windspeed":
        raise rc.Fail("49②: 宿主上 1号 wt1 点被破坏")
    # 注册表以 description 区分两台同名
    e2 = base.entry("wt2", name="1号风机", host=wid)
    if "备用机" not in str(e2.get("description", "")):
        raise rc.Fail(f"49②: wt2 description 未含「备用机」: {e2.get('description', '')[:80]}")
    base.entry("wt1", name="1号风机")
    # 全程并入+沿用既有转发链路，无新实例 → 水位不推进
    if asfp_count(cfg, "c4_asfp2_server") != 1:
        raise rc.Fail(f"49②: 采集实例数 ≠ 1（应并入宿主）")
    if base.registry().get("channelHighWatermark") != 2:
        raise rc.Fail(f"49②: 水位={base.registry().get('channelHighWatermark')} ≠ 2（无新实例）")
    base.assert_no_handle_leak("49")


# ── B2：用例 24 ────────────────────────────────────────────
def s24():
    """① 转发同目标完全重叠 → 方案期拒绝；② 改不重叠点表后并入成功。"""
    # ① 5000~5009 与 1号转发表完全重叠 → 必须拒绝且不出可确认方案
    before = rc.read_config()
    conv = rc.Conv()
    text = conv.send(MSG24A)
    time.sleep(5)
    after = rc.read_config()
    if json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True):
        raise rc.Fail("24①: 冲突请求写入了 config")
    if re.search(r"是否确认|确认执行", text):
        raise rc.Fail(f"24①: 冲突请求进入可确认方案（应方案期拒绝）: {text[:200]}")
    if not re.search(r"冲突|重叠|已被占用|重复|无法|请更换|调整", text):
        raise rc.Fail(f"24①: 无可读冲突信号: {text[:200]}")
    base.assert_no_handle_leak("24①")
    # ② 改 7100~7109 → 并入宿主 + 新转发实例 →9900
    fwd = rc.start_receiver(P_FWD1)
    try:
        base.flow(conv, MSG24B,
                  done=lambda: rc.writer_of(rc.read_config(), 1300) is not None)
        cfg = rc.wait_config(lambda c: rc.writer_of(c, 1300) is not None
                             and rc.forward_of(c, 7100) is not None,
                             timeout=240, desc="24②: 改点表后 wt4 并入")
        base.assert_channel_ids(cfg)
        wid4 = rc.writer_of(cfg, 1300)
        if wid4 != "channel1":
            raise rc.Fail(f"24②: 4号落在 {wid4} ≠ 宿主 channel1（同端口应并入）")
        base.assert_writer_keys(cfg, "wt4", range(1300, 1310))
        fid4 = base.assert_forward_key(cfg, 7100, wid4, 1300)
        if fid4 == rc.forward_of(cfg, 5000):
            raise rc.Fail(f"24②: 4号转发与1号共用实例 {fid4}——应每设备一实例")
        # 1号既有转发表逐字节不变
        f1 = rc.points_of(cfg, "c4_asfp2_client", rc.forward_of(cfg, 5000))
        for a in range(5000, 5010):
            if a not in f1:
                raise rc.Fail(f"24②: 1号转发表 addr={a} 丢失")
        base.entry("wt4", name="4号风机", host=wid4)
        if base.registry().get("channelHighWatermark") != 3:
            raise rc.Fail(f"24②: 水位={base.registry().get('channelHighWatermark')} ≠ 3")
        base.data_smoke(fwd, P_RECV1, 1300, 1310)
        base.assert_no_handle_leak("24②")
    finally:
        fwd.stop()


# ── 链调度（三链串行，链间回零，失败即停）────────────────────
CHAIN_TAG = "C4He1"
CHAIN_FAIL_DIR = base.CHAIN_FAIL_DIR

CHAINS = [
    ("B3", [("A.3", SETUP_ONLY, prep_a3), ("22", ["22"], s22)]),
    ("B4", [("A.3", SETUP_ONLY, prep_a3), ("49", ["49"], s49)]),
    ("B2", [("A.3", SETUP_ONLY, prep_a3), ("24", ["24"], s24)]),
]


def main():
    rc.log("════ 并入组 B3/B4/B2 串行测试开始（每链 A.3 回零起步）════")
    for chain_name, steps in CHAINS:
        rc.log(f"════ ── {chain_name} 链开始 ── ════")
        base.chain_clean()
        rc.MCP_STACK.up()
        agent = base.V21Agent()
        agent.up()
        rc.AGENT = agent
        for label, cases, fn in steps:
            rc.log(f"════ 链步 {label} 开始 ════")
            rc.PH.reset(f"{chain_name}/{label}")
            t0 = time.time()
            try:
                fn()
            except rc.Fail as e:
                base.dump_diag(f"{chain_name}_{label}", e)
                rc.log(f"════ 链步 {label} FAIL: {e} ════")
                rc.log(f"════ {chain_name} 链在第 {label} 步停止"
                       f"（已通过用例: {PASSED or '无'}）════")
                sys.exit(1)
            except Exception as e:
                base.dump_diag(f"{chain_name}_{label}", e)
                rc.log(f"════ 链步 {label} 异常 {type(e).__name__}: {str(e)[:200]} ════")
                rc.log(f"════ {chain_name} 链在第 {label} 步停止"
                       f"（已通过用例: {PASSED or '无'}）════")
                sys.exit(1)
            PASSED.extend(cases)
            rc.log(f"════ 链步 {label} PASS（{time.time()-t0:.0f}s）════")
    rc.log(f"════ B3/B4/B2 全部通过：{PASSED} ════")
    rc.log(f"════ 标注清单（AI测试通过 {CHAIN_TAG}）：{PASSED} ════")


if __name__ == "__main__":
    main()
