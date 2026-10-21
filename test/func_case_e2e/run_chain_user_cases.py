#!/usr/bin/env python3
# 用户生产实测回归（2026-10-19 用户指令：把昨天用户测试的用例添加到回归用例中）
# 来源：/var/log/c4/agent/agent-2026-10-18.log user_input 逐字提取（生产栈手工测试）
# 输入文件（用户指定）：points/modbus/景观中科机组SCADA集控点表V1.5.xlsx
# 覆盖修复回归锚：
#   2.1.85① 接入侧协议归属——重发完整句「…风机scada使用modbus协议转发…使用Asfp2协议…」
#            modbus 绑采集侧、Asfp2 绑转发侧（LLM 弃权不再致命）
#   2.1.83   采集连接「port:502」英文词形捕获；转发目标 127.0.0.1:9907 不误绑采集连接
#   2.1.85② 设备名误提「的应该是2区」——语法垃圾不得进设备名/点 key（device_attribution）
#   流程健壮性——场站/uid/转发地址收摊（「前面提供过转发地址了。」）/起始地址问答与改口
# 规则：逐链独立回零；断言按现行规范应然行为（宽松语境前缀仅提示不硬断）。
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_cases as rc  # noqa: E402
import run_chain_v21 as base  # noqa: E402
import run_chain_real_points as rp  # noqa: E402  (upload_big)

PTS = "/home/wangbo/work/activesys/points/modbus/景观中科机组SCADA集控点表V1.5.xlsx"
SITE_ANS = "华能阿拉善"

MSG_FULL = ("我要接入一区风机数据，风机scada使用modbus协议转发，ip:127.0.0.1, port:502，"
            "转发到2区，使用Asfp2协议，127.0.0.1:9907。")
MSG_FULL_NODEV = ("接入风机数据，风机scada使用modbus协议转发，ip:127.0.0.1, port:502，"
                  "转发到2区，使用Asfp2协议，127.0.0.1:9907。")
MSG_UPLOAD = "请解析此文件中的设备信息"
MSG_UC2_MIS = "接入的应该是2区的数据。"
MSG_UC3_FIX = "我刚才说错了，应该是III区。"
ANS_UID = "uid是1"
ANS_FWD = "127.0.0.1:9907"
ANS_START = "从5000开始"

F = rc.Fail


def mb_insts(cfg):
    return [(iid, inst) for (st, iid), inst in rc.server_instances(cfg).items()
            if st == "c4_modbus_client"]


def fwd_insts(cfg):
    return [(iid, inst) for (st, iid), inst in rc.server_instances(cfg).items()
            if st == "c4_asfp2_client"]


def landed(cfg):
    return bool(mb_insts(cfg)) and bool(fwd_insts(cfg))


def assert_essence(cfg, tag, prefix_sub=None):
    """现行规范应然行为断言：协议双侧归属 / 连接词形 / uid / 9907 转发 / 无垃圾前缀。"""
    bad_conn = [(iid, inst.get("ip"), inst.get("port")) for iid, inst in mb_insts(cfg)
                if str(inst.get("ip")) != "127.0.0.1" or int(inst.get("port", -1)) != 502]
    if not mb_insts(cfg) or bad_conn:
        raise F(f"{tag}: modbus 采集实例缺失或连接不符（2.1.83/85① 回归锚）: "
                f"{[(i, d.get('ip'), d.get('port')) for i, d in mb_insts(cfg)][:2]}")
    pts = [p for _, inst in mb_insts(cfg) for p in inst.get("points", [])]
    if not pts:
        raise F(f"{tag}: 采集零点")
    bad_uid = [p.get("uid") for p in pts if int(p.get("uid", -1)) != 1]
    if bad_uid:
        raise F(f"{tag}: {len(bad_uid)} 点 uid≠1（uid 问答闭环）")
    if not [inst for _, inst in fwd_insts(cfg)
            if str(inst.get("ip")) == "127.0.0.1" and int(inst.get("port", -1)) == 9907]:
        raise F(f"{tag}: asfp2 → 127.0.0.1:9907 转发实例缺失: "
                f"{[(i, d.get('ip'), d.get('port')) for i, d in fwd_insts(cfg)][:2]}")
    for _, inst in mb_insts(cfg):
        for p in inst.get("points", []):
            pid = str(p.get("id", ""))
            if "的应" in pid or pid.startswith("应该"):
                raise F(f"{tag}: 语法垃圾进设备名（2.1.85② 回归锚）: {pid}")
    if prefix_sub:
        pid0 = str(pts[0].get("id", ""))
        if not pid0.startswith(prefix_sub):
            rc.log(f"  UC 提示: 首 id {pid0!r} 不以 {prefix_sub!r} 开头（X数据尾口径未裁定，仅提示）")


def drive(conv, first_msg, upload_first, answers, tag, max_turns=16):
    """发首消息 →（可选紧接上传）→ 模式应答/确认循环，直至采集+转发实例落地。"""
    consumed = [False] * len(answers)
    text = conv.send(first_msg)
    if upload_first:
        text, _ = rp.upload_big(conv.conversation_id, PTS, MSG_UPLOAD)
    clicked = 0
    for _ in range(max_turns):
        if landed(rc.read_config()):
            return rc.read_config(), text
        if "解析失败" in text:
            raise F(f"{tag}: 意外拒收: {text[:300]}")
        if clicked < 2 and ("是否确认" in text or "确认执行" in text
                            or "确认后我将" in text or ("接入方案" in text and "确认" in text)):
            text = conv.send("[C4_BUTTON_CONFIRM] 确认")
            clicked += 1
            rc.wait_idle(timeout=600)
            continue
        hit = False
        for i, (pat, ans) in enumerate(answers):
            if not consumed[i] and re.search(pat, text):
                consumed[i] = True
                text = conv.send(ans)
                hit = True
                break
        if hit:
            rc.wait_idle(timeout=600)
            continue
        time.sleep(10)
        text = conv.send("继续")
    raise F(f"{tag}: {max_turns} 轮未落地，尾回复: {text[:300]}")


ANSWERS = [
    (r"场站", SITE_ANS),
    (r"uid|UID|从站", ANS_UID),
    # 转发点表缺口（2026-10-19 回归实测）：agent 反复问「请提供转发点表……可回复
    # 「与接收侧一致」」，应答表此前只有「转发地址」模式（匹配不上「转发点表」），
    # 驱动只能盲发「继续」直至 16 轮耗尽——本项必须排在转发地址之前（收摊文案
    # 「· 转发点表（各点的转发地址等信息）」两者皆中，先到先得）
    (r"转发点表|与接收侧一致", "与接收侧一致"),
    (r"前面提供过|已提供|转发地址", ANS_FWD),
    (r"从.{0,6}开始|起始|起点", ANS_START),
    (r"监听端口|接收端口", "9000"),
]


def uc1():
    """UC1 完整句首接：协议双侧归属 + port:502 + uid 问答 + 景观中科上传 → 落地。"""
    conv = rc.Conv()
    cfg, _ = drive(conv, MSG_FULL, True, ANSWERS, "UC1")
    assert_essence(cfg, "UC1", "一区风机")
    rc.log("  UC1 PASS ✓（协议双侧归属 + port:502 + uid + 上传落地）")


def uc2():
    """UC2 设备名误提修复：『接入的应该是2区的数据。』——垃圾片段不得绑设备名（2.1.85②）。"""
    conv = rc.Conv()
    conv.send(MSG_FULL)
    text = conv.send(MSG_UC2_MIS)
    if landed(rc.read_config()):
        raise F("UC2: 误指句后意外落地（应仍在收集态）")
    if "的应该" in text:
        raise F(f"UC2: 回复复述垃圾设备名: {text[:200]}")
    cfg, _ = drive(conv, MSG_FULL, True, ANSWERS, "UC2")
    assert_essence(cfg, "UC2", "一区风机")
    rc.log("  UC2 PASS ✓（误指句不入设备名，重述后落地）")


def uc3():
    """UC3 改口（III区）+ 无设备名重述 + 转发地址收摊问答 → 落地。"""
    conv = rc.Conv()
    conv.send(MSG_FULL)
    conv.send(MSG_UC3_FIX)
    text = conv.send(MSG_FULL_NODEV)
    cfg = rc.read_config()
    if landed(cfg):
        raise F("UC3: 上传前意外落地")
    text, _ = rp.upload_big(conv.conversation_id, PTS, MSG_UPLOAD)
    answers = [(r"场站", SITE_ANS)] + ANSWERS + [(r"uid|UID", "UID是1")]
    consumed = [False] * len(answers)
    clicked = 0
    for _ in range(18):
        if landed(rc.read_config()):
            break
        if "解析失败" in text:
            raise F(f"UC3: 意外拒收: {text[:300]}")
        if clicked < 2 and ("是否确认" in text or "确认执行" in text
                            or "确认后我将" in text or ("接入方案" in text and "确认" in text)):
            text = conv.send("[C4_BUTTON_CONFIRM] 确认")
            clicked += 1
            rc.wait_idle(timeout=600)
            continue
        hit = False
        for i, (pat, ans) in enumerate(answers):
            if not consumed[i] and re.search(pat, text):
                consumed[i] = True
                text = conv.send(ans)
                hit = True
                break
        if hit:
            rc.wait_idle(timeout=600)
            continue
        time.sleep(10)
        text = conv.send("继续")
    else:
        raise F(f"UC3: 未落地，尾回复: {text[:300]}")
    assert_essence(rc.read_config(), "UC3", "风机")
    rc.log("  UC3 PASS ✓（改口 + 地址收摊 + 落地）")


CHAINS = [
    ("UC1", uc1),
    ("UC2", uc2),
    ("UC3", uc3),
]


def main():
    only = set(sys.argv[1:])
    t0 = time.time()
    rc.log("════ 用户生产实测回归开始（2026-10-18 生产野测入库，模型 "
           f"{rc.MODEL_PRIMARY}）════")
    passed, failed = [], []
    for name, fn in CHAINS:
        if only and name not in only:
            continue
        try:
            rc.log(f"── {name} 开始 ──")
            base.chain_clean()
            rc.MCP_STACK.up()
            agent = base.V21Agent()
            agent.up()
            rc.AGENT = agent
            rc.PH.reset(name, 3)
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
    rc.log(f"════ 用户实测回归套件结束：PASS={passed} FAIL={failed}"
           f"（{time.time() - t0:.0f}s）════")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
