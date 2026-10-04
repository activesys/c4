#!/usr/bin/env python3
# func_test_case.md 并入组 B1 串行驱动器（v2.1.0 命名体系）
# 链序：A.3 前置（= A.2 + 用例 1 输入接入，场站询问答「华能阿拉善」→ 用例 1 完成态）
#       → 23（3号风机同端口 9001 自动并入宿主）→ 54（共用形态加点）
#       → 48（共用形态摘除 3号——前缀点组手术）→ 55（共用宿主删空——空实例移除）
# 规则（用户指令）：逐项串行、不并行；任一链步 FAIL 立即停止全链、落盘诊断、不修改。
# 断言口径：func_test_case.md v2.1.0 用例 23/54/48/55 —— 同端口自动并入零交互、
# 注册表双条目同宿主、逐点 key 随方案明示、转发成对追加不新建第二实例、
# 前缀点组手术/空实例移除、水位永不回退；对话/方案文本不得出现 channel 与「通道」。
# B1 链水位预期：A.3 占 1/2 → 23 转发实例占 3 → 48/55 删除不回退（终态水位=3）。
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_cases as rc  # noqa: E402
import run_chain_v21 as base  # noqa: E402

P_RECV1, P_FWD1, P_FWD2 = rc.P_RECV1, rc.P_FWD1, rc.P_FWD2
PASSED = []          # 命中的 func_test_case.md 用例号（用于标注 AI测试通过 C4He1）
SETUP_ONLY = {"A.3"}  # 链首准备步，不对应独立用例

WT1_ID1000 = None  # A.3 首接 wt1 addr=1000 点 id（s23 并入不变性基准，见 prep 记录处）

MSG23 = ("再接入3号风机，第三方厂家通过asfp2协议转来3#风机数据，10个点，从1200到1209，"
         "分别是1200:风速、1201:功率、1202:风向、1203:桨叶角度、1204:发电机转速、"
         "1205:齿轮箱油温、1206:塔筒温度、1207:空气温度、1208:空气湿度、1209:大气压强，"
         "使用端口9001。转发到II区服务器127.0.0.1:9901，转发采用asfp2协议，点表7000~7009。")
MSG54 = "给3号风机增加一个数据点，地址1210:机舱振动，转发地址7010。"
MSG48 = "删除3号风机。"
MSG55 = "删除1号风机。"


def asfp_count(cfg, st):
    return sum(1 for k in rc.server_instances(cfg) if k[0] == st)


def s_prep_a3():
    """A.3 链首准备：全新环境按用例 1 输入接入（场站询问答华能阿拉善）→ 用例 1 完成态。

    断言：channel1/channel2、wt1_* 十点、转发引用 channel1.wt1_*、水位=2、site 已绑定。"""
    conv = rc.Conv()
    flow_text, _ = base.flow(conv, rc.MSG_CASE1, answers=[(r"场站", "华能阿拉善")])
    cfg = rc.wait_config(lambda c: rc.writer_of(c, 1000) is not None
                         and rc.forward_of(c, 5000) is not None,
                         timeout=240, desc="A.3: 用例 1 完成态（1号风机在线）")
    assert base.site_of_agent_json() is not None, "A.3: site 未绑定"
    base.assert_channel_ids(cfg)
    wid, fid = rc.writer_of(cfg, 1000), rc.forward_of(cfg, 5000)
    if (wid, fid) != ("channel1", "channel2"):
        raise rc.Fail(f"A.3: 句柄 {wid}/{fid} ≠ channel1/channel2（全新环境首接预期）")
    base.assert_writer_keys(cfg, "wt1", range(1000, 1010))
    base.assert_forward_key(cfg, 5000, wid, 1000)
    # 记录首接点 id（s23 并入不变性断言基准，2026-10-04：LLM 译名 windspeed/
    # wind_speed 均属合法 snake_case，断言钉「并入后原点不变」，不硬编码译名）
    global WT1_ID1000
    WT1_ID1000 = rc.points_of(cfg, "c4_asfp2_server", wid).get(1000, {}).get("id")
    reg = base.registry()
    if reg.get("channelHighWatermark") != 2:
        raise rc.Fail(f"A.3: 水位={reg.get('channelHighWatermark')} ≠ 2")
    if (reg.get("entries") or [{}])[0].get("host") != wid:
        raise rc.Fail(f"A.3: wt1 宿主 {((reg.get('entries') or [{}])[0].get('host'))} ≠ {wid}")


def s23():
    """用例 23：3号风机同端口 9001 自动并入——零交互、无新采集实例、注册表双条目同宿主。"""
    n_srv0 = asfp_count(rc.read_config(), "c4_asfp2_server")
    n_cli0 = asfp_count(rc.read_config(), "c4_asfp2_client")
    fwd9901 = rc.start_receiver(P_FWD2)
    try:
        conv = rc.Conv()
        flow_text, clicked = base.flow(
            conv, MSG23, done=lambda: rc.writer_of(rc.read_config(), 1200) is not None)
        cfg = rc.wait_config(lambda c: rc.writer_of(c, 1200) is not None
                             and rc.forward_of(c, 7000) is not None,
                             timeout=240, desc="23: wt3 十点并入宿主 + 转发实例→9901")
        dialog = "".join(base._assistant_texts())
        # ① 零交互并入：无端口冲突询问/警告，方案以白话陈述共用事实
        if re.search(r"占用|冲突|无法接入|请更换端口", dialog):
            raise rc.Fail(f"23: 同端口并入被误判为冲突: "
                          f"{[m for m in re.findall(r'.{20}(?:占用|冲突).{20}', dialog)][:1]}")
        if "共用端口" not in dialog:
            raise rc.Fail(f"23: 方案未以白话陈述共用事实（缺「共用端口」）: {dialog[:200]}")
        # ② 逐点列出最终点 key（wt3_* 形态随方案明示）
        if "wt3_" not in dialog:
            raise rc.Fail(f"23: 方案未逐点明示 wt3_* 点 key: {dialog[:200]}")
        assert base.site_of_agent_json() is not None
        base.assert_channel_ids(cfg)
        # ③ 无新 c4_asfp2_server 实例：宿主唯一，1200 与 1000 同宿主
        n_srv = asfp_count(cfg, "c4_asfp2_server")
        if n_srv != n_srv0:
            raise rc.Fail(f"23: c4_asfp2_server 实例数 {n_srv0}→{n_srv}——并入不得新建采集实例")
        wid = rc.writer_of(cfg, 1200)
        host1 = rc.writer_of(cfg, 1000)
        if wid != host1:
            raise rc.Fail(f"23: addr 1200 落在 {wid}，1号风机在 {host1}——未并入宿主实例")
        w, wid = base.assert_writer_keys(cfg, "wt3", range(1200, 1210))
        if w.get(1000, {}).get("id") != WT1_ID1000:
            raise rc.Fail(f"23: 宿主上 1号 wt1 点被破坏: {w.get(1000)}")
        # 转发侧：每设备一实例——3号新建独立转发实例（≠1号的），引用键 channel1.wt3_*
        fid3 = base.assert_forward_key(cfg, 7000, wid, 1200)
        base.assert_forward_key(cfg, 5000, host1, 1000)
        if rc.forward_of(cfg, 7000) == rc.forward_of(cfg, 5000):
            raise rc.Fail(f"23: 3号转发实例与1号共用 {fid3}——应每设备一实例")
        n_cli = asfp_count(cfg, "c4_asfp2_client")
        if n_cli != n_cli0 + 1:
            raise rc.Fail(f"23: c4_asfp2_client 实例数 {n_cli0}→{n_cli} ≠ +1")
        # 注册表双条目指向同一宿主 + 水位 +1（仅转发实例）
        base.entry("wt3", name="3号风机", host=wid)
        base.entry("wt1", name="1号风机", host=wid)
        reg = base.registry()
        if reg.get("channelHighWatermark") != 3:
            raise rc.Fail(f"23: 水位={reg.get('channelHighWatermark')} ≠ 3（新转发实例占 3）")
        # ④ 数据面：并入宿主按 9001 收数 → 3号专属转发链路 → 9901 出数
        base.data_smoke(fwd9901, P_RECV1, 1200, 1210)
        base.assert_no_handle_leak("23")
    finally:
        fwd9901.stop()


def s54():
    """用例 54：共用形态加点——注册表宿主定位、点入宿主、转发成对追加不新建实例。"""
    before = rc.snapshot_w1(rc.read_config())
    n_cli0 = asfp_count(rc.read_config(), "c4_asfp2_client")
    conv = rc.Conv()
    base.flow(conv, MSG54, done=lambda: rc.writer_of(rc.read_config(), 1210) is not None)
    cfg = rc.wait_config(lambda c: rc.writer_of(c, 1210) is not None
                         and rc.forward_of(c, 7010) is not None,
                         timeout=240, desc="54: 1210/7010 成对新增")
    base.assert_channel_ids(cfg)
    wid = rc.writer_of(cfg, 1210)
    host1 = rc.writer_of(cfg, 1000)
    if wid != host1 or wid != rc.writer_of(cfg, 1200):
        raise rc.Fail(f"54: 新点落在 {wid} ≠ 3号/1号宿主 {host1}——未按注册表宿主定位")
    w, wid = base.assert_writer_keys(cfg, "wt3", list(range(1200, 1211)))
    pid = str(w[1210]["id"])
    # ② 转发成对追加到 3号既有转发实例（→9901），不得新建第二转发实例
    fid7010 = base.assert_forward_key(cfg, 7010, wid, 1210)
    if fid7010 != rc.forward_of(cfg, 7000):
        raise rc.Fail(f"54: 7010 落在 {fid7010} ≠ 3号既有转发实例 "
                      f"{rc.forward_of(cfg, 7000)}——转发归属检查失效")
    if asfp_count(cfg, "c4_asfp2_client") != n_cli0:
        raise rc.Fail(f"54: 转发实例数 {n_cli0}→{asfp_count(cfg, 'c4_asfp2_client')}——加点不得新建实例")
    # ③ 注册表 pointMap 增项「机舱振动 → wt3_<裸id>」（完整点 key）
    pm = base.entry("wt3", name="3号风机", host=wid).get("pointMap", {})
    if pm.get("机舱振动") != pid:
        raise rc.Fail(f"54: pointMap[机舱振动]={pm.get('机舱振动')!r} ≠ {pid!r}")
    # ④⑤ 1号/3号既有点 shm_id 无损、新点分配新 shm_id
    after = rc.snapshot_w1(cfg)
    for addr, sid in before.items():
        if after.get(addr) != sid:
            raise rc.Fail(f"54: 既有点 addr={addr} shm_id 被重排（{sid}→{after.get(addr)}）")
    if after.get(1210) in (None, 0):
        raise rc.Fail(f"54: 新点 shm_id 异常: {after.get(1210)}")
    base.assert_no_handle_leak("54")


def s48():
    """用例 48：共用形态摘除 3号——前缀点组手术、宿主保留、专属转发实例整体移除。"""
    before = rc.snapshot_w1(rc.read_config())
    conv = rc.Conv()
    base.flow(conv, MSG48, done=lambda: rc.writer_of(rc.read_config(), 1200) is None)
    cfg = rc.wait_config(lambda c: rc.writer_of(c, 1200) is None
                         and rc.writer_of(c, 1000) is not None
                         and rc.forward_of(c, 7000) is None,
                         timeout=180, desc="48: wt3 摘除 + 专属转发实例移除 + 宿主保留")
    dialog = "".join(base._assistant_texts())
    # ① 方案逐点列出待删 key（前缀点组手术形态）
    if "wt3_" not in dialog:
        raise rc.Fail(f"48: 方案未逐点列出 wt3_* 待删 key: {dialog[:200]}")
    base.assert_channel_ids(cfg)
    # ② 宿主实例保留——wt1_* 点表与 shm_id 完全无损；wt3_* 全部消失
    if asfp_count(cfg, "c4_asfp2_server") != 1:
        raise rc.Fail(f"48: 采集实例数 ≠ 1（宿主应保留）: "
                      f"{[k for k in rc.server_instances(cfg) if k[0] == 'c4_asfp2_server']}")
    host = rc.points_of(cfg, "c4_asfp2_server", rc.writer_of(cfg, 1000))
    leftover = sorted(a for a, p in host.items() if str(p.get("id", "")).startswith("wt3_"))
    if leftover:
        raise rc.Fail(f"48: wt3_* 点残留: {leftover}")
    after = rc.snapshot_w1(cfg)
    for addr, sid in before.items():
        if 1200 <= addr <= 1210:
            continue
        if after.get(addr) != sid:
            raise rc.Fail(f"48: 1号既有点 addr={addr} shm_id 被重排（{sid}→{after.get(addr)}）")
    # 3号专属转发实例（→9901）整体移除，不得残留空转发实例；1号转发无损
    if rc.forward_of(cfg, 7000) is not None or rc.forward_of(cfg, 7010) is not None:
        raise rc.Fail("48: 3号转发实例未整体移除（7000/7010 仍有 Reader）")
    if asfp_count(cfg, "c4_asfp2_client") != 1:
        raise rc.Fail(f"48: 转发实例数 ≠ 1: "
                      f"{[k for k in rc.server_instances(cfg) if k[0] == 'c4_asfp2_client']}")
    base.assert_forward_key(cfg, 5000, rc.writer_of(cfg, 1000), 1000)
    # ④ 注册表 {3号风机, wt3} 物理删除、wt1 保留；channel 序号不回收
    reg = base.registry()
    prefixes = [e.get("prefix") for e in reg.get("entries", [])]
    if "wt3" in prefixes:
        raise rc.Fail("48: 注册表 wt3 条目未删除")
    if "wt1" not in prefixes:
        raise rc.Fail(f"48: 注册表 wt1 条目丢失: {prefixes}")
    if reg.get("channelHighWatermark") != 3:
        raise rc.Fail(f"48: 水位回退 {reg.get('channelHighWatermark')} ≠ 3")
    base.assert_no_handle_leak("48")


def s55():
    """用例 55：共用宿主删空——最后一台设备删除 → 空实例移除、注册表清空、回等待首接态。"""
    conv = rc.Conv()
    base.flow(conv, MSG55,
              done=lambda: not [k for k in rc.server_instances(rc.read_config())
                                if k[0] in ("c4_asfp2_server", "c4_asfp2_client")])
    rc.wait_config(lambda c: not [k for k in rc.server_instances(c)
                                  if k[0] in ("c4_asfp2_server", "c4_asfp2_client")],
                   timeout=180, desc="55: 宿主删空（采集/转发实例全部移除）")
    # ② 9001 释放
    rc.wait_port(P_RECV1, False)
    # ④ 注册表条目全部删除（entries 空初始态）；channel 序号不回收
    reg = base.registry()
    if (reg or {}).get("entries") not in ([], None):
        raise rc.Fail(f"55: 注册表残留: {json.dumps(reg.get('entries'), ensure_ascii=False)[:150]}")
    if (reg or {}).get("channelHighWatermark") != 3:
        raise rc.Fail(f"55: 水位回退 {(reg or {}).get('channelHighWatermark')} ≠ 3")
    base.assert_no_handle_leak("55")


# ── 链调度（串行，失败即停）────────────────────────────────
STEPS = [
    ("A.3",  SETUP_ONLY,   s_prep_a3),
    ("23",   ["23"],       s23),
    ("54",   ["54"],       s54),
    ("48",   ["48"],       s48),
    ("55",   ["55"],       s55),
]

CHAIN_TAG = "C4He1"
CHAIN_FAIL_DIR = base.CHAIN_FAIL_DIR


def main():
    rc.log("════ 并入组 B1 串行测试开始（A.3 → 23 → 54 → 48 → 55）════")
    base.chain_clean()
    rc.MCP_STACK.up()
    agent = base.V21Agent()
    agent.up()
    rc.AGENT = agent          # 让 run_cases 的 read_config/Conv 全部指向本实例
    for label, cases, fn in STEPS:
        rc.log(f"════ 链步 {label} 开始 ════")
        rc.PH.reset(label)
        t0 = time.time()
        try:
            fn()
            rc.PH.check(label)
        except rc.Fail as e:
            base.dump_diag(label, e)
            rc.log(f"════ 链步 {label} FAIL: {e} ════")
            rc.log(f"════ B1 链在第 {label} 步停止（已通过用例: {PASSED or '无'}）════")
            sys.exit(1)
        except Exception as e:
            base.dump_diag(label, e)
            rc.log(f"════ 链步 {label} 异常 {type(e).__name__}: {str(e)[:200]} ════")
            rc.log(f"════ B1 链在第 {label} 步停止（已通过用例: {PASSED or '无'}）════")
            sys.exit(1)
        PASSED.extend(cases)
        rc.log(f"════ 链步 {label} PASS（{time.time()-t0:.0f}s）════")
    rc.log(f"════ B1 链全部通过：{PASSED} ════")
    rc.log(f"════ 标注清单（AI测试通过 {CHAIN_TAG}）：{PASSED} ════")


if __name__ == "__main__":
    main()
