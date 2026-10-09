#!/usr/bin/env python3
# 多下游接入串行驱动器（func_test_case.md 用例 63~67；agent.md §2.12）。
# 链段（各自独立回零；65/67 内含各自的前置接入）：
#   63 —— 首接一次声明：两设备 + 三路目标声明（同批同参装配期被拒）+ 扣留字段问序
#   64 —— 首接必问下游：纯接入被拒、两次收摊、整链撤回回到必问缺口
#   65 —— 点集表达式：12 点净集 + 同段多选择器 7 点 + 修改下游澄清拒绝
#   66 —— 首接收缩明示 + 入口 B 既有目标追加点组/Writer 补建 + 同实例重复拒绝
#   67 —— 删除下游收缩 + 收缩至空停用 + 删除设备级联 + 同事务删 A 增 D
# 规则：逐项串行、任一链段 FAIL 立即停止并落盘诊断；过程断言闸门（用例 46）。
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_cases as rc  # noqa: E402
import run_chain_v21 as base  # noqa: E402

PASSED = []
SITE_PRE = "场站名称：华能阿拉善。"

MSG63 = (
    "现在需要接入1号风机和2号风机的数据，都是asfp2协议，1号风机10个点从1000到1009，"
    "10个点分别是1000:风速、1001:功率、1002:风向、1003:桨叶角度、1004:发电机转速、"
    "1005:齿轮箱油温、1006:塔筒温度、1007:空气温度、1008:空气湿度、1009:大气压强，使用端口9001；"
    "2号风机10个点从1100到1109，点名跟1号风机一样对应1100~1109，使用端口9002。"
    "1号风机的数据转发到II区服务器，转发采用asfp2协议，点表5000~5009；"
    "1号风机的数据也转发到II区备份服务器，转发采用asfp2协议，目标地址是127.0.0.1:9900，点表同样5000~5009；"
    "2号风机的数据写入计算库，写入地址http://127.0.0.1:8086，token是hnals-influx-2026，org是activesys，"
    "10个点全部写进turbine_compute，字段名跟点名对应，类型统一float。"
)
MSG63_ADDR = "127.0.0.1:9900"
MSG63_BUCKET = "compute"
# 2026-10-09 裁定：入库标识字段不得中文——「字段名跟点名对应」须追问后显式给出
ANS63_FIELDS = ("字段名跟点名对应（windspeed、power、wind_dir、pitch_angle、gen_speed、"
                "gearbox_oil_temp、tower_temp、air_temp、humidity、pressure）。")
ANS65_FIELDS = ("字段按行序（windspeed、power、wind_dir、pitch_angle、gen_speed、"
                "gearbox_oil_temp、tower_temp、air_temp、humidity、pressure、windspeed、power）。")
ANS65V_FIELDS = ("字段按行序（windspeed、power、wind_dir、pitch_angle、gen_speed、"
                 "gearbox_oil_temp、tower_temp）。")
ANS_MEAS = "measurement用设备前缀，1号风机wt1、2号风机wt2。"

MSG64 = (
    "现在需要接入1号风机的数据，第三方厂家通过asfp2协议给我们转来1#风机数据，10个点，从1000到1009，"
    "10个点分别是1000:风速、1001:功率、1002:风向、1003:桨叶角度、1004:发电机转速、1005:齿轮箱油温、"
    "1006:塔筒温度、1007:空气温度、1008:空气湿度、1009:大气压强，使用端口9001。"
)
MSG64_DOWN = "转发到II区服务器，转发采用asfp2协议，目标地址是127.0.0.1:9900，点表5000~5009。"

MSG65 = (
    "再写一份统计库：1号风机的前5个点、2号风机的后5个点，1号风机和2号风机的风速/功率；"
    "写入地址http://127.0.0.1:8086，token是hnals-influx-2026，org是activesys，bucket是stats，"
    "measurement按设备名，字段名跟点名对应，类型统一float。"
)
MSG65_VAR = "再写一份明细库：1号风机的1000~1004 1002~1006，写入地址http://127.0.0.1:8086，token是hnals-influx-2026，org是activesys，bucket是stats_detail，measurement按设备名，字段名跟点名对应，类型统一float。"

MSG66_1 = (
    "现在需要接入1号风机的数据，第三方厂家通过asfp2协议给我们转来1#风机数据，12个点，从1000到1011，"
    "10个点分别是1000:风速、1001:功率、1002:风向、1003:桨叶角度、1004:发电机转速、1005:齿轮箱油温、"
    "1006:塔筒温度、1007:空气温度、1008:空气湿度、1009:大气压强，另外还有1010:机舱振动、1011:机舱温度，"
    "使用端口9001。数据转发到II区服务器，转发采用asfp2协议，目标地址是127.0.0.1:9900，点表5000~5009。"
)
MSG66_2 = "把1010:机舱振动和1011:机舱温度两点也转给II区服务器，转发地址分别是5010和5011。"
MSG66_3 = "把1号风机的风速再转一份给II区服务器，转发地址5012。"

MSG67_0 = (
    "现在需要接入1号风机的数据，第三方厂家通过asfp2协议给我们转来1#风机数据，10个点，从1000到1009，"
    "10个点分别是1000:风速、1001:功率、1002:风向、1003:桨叶角度、1004:发电机转速、1005:齿轮箱油温、"
    "1006:塔筒温度、1007:空气温度、1008:空气湿度、1009:大气压强，使用端口9001。"
    "1号风机的数据转发到II区服务器，转发采用asfp2协议，目标地址是127.0.0.1:9900，点表5000~5009；"
    "同时这10个点写入计算库，写入地址http://127.0.0.1:8086，token是hnals-influx-2026，org是activesys，"
    "bucket是compute，10个点全部写进turbine_compute，字段名跟点名对应，类型统一float。"
)
MSG67_1 = "删除II区服务器转发。"
MSG67_2 = "把计算库也删了。"
MSG67_3 = "删除1号风机。"
MSG67_4 = "删除II区服务器转发，同时把1号风机的数据转发到第三方，转发采用asfp2协议，目标地址是127.0.0.1:9901，点表5000~5009。"


def influx_instances(cfg):
    return rc.server_instances(cfg).get(("c4_influxdb_client", None)) and [
        inst
        for (st, _iid), inst in rc.server_instances(cfg).items()
        if st == "c4_influxdb_client"
    ] or []


def inst_by_name(cfg, name):
    for (st, _iid), inst in rc.server_instances(cfg).items():
        if String(inst.get("name")) == name:
            return st, inst
    return None, None


def String(v):
    return str(v) if v is not None else ""


def s63():
    """首接一次声明：两设备 + 三路目标（同参被拒）+ 扣留字段问序。"""
    conv = rc.Conv()
    base.flow(
        conv, SITE_PRE + MSG63,
        answers=[(r"提供场站名称", "华能阿拉善"),
                 (r"目标地址", MSG63_ADDR),
                 (r"bucket", MSG63_BUCKET),
                 (r"field", ANS63_FIELDS)],
        done=lambda: rc.forward_of(rc.read_config() or {}, 5000) is not None,
    )
    cfg = rc.wait_config(
        lambda c: rc.writer_of(c, 1000) is not None and rc.writer_of(c, 1100) is not None
        and rc.forward_of(c, 5000) is not None,
        timeout=240, desc="63: 两 Writer + 转发落地",
    )
    w1 = rc.points_of(cfg, "c4_asfp2_server", rc.writer_of(cfg, 1000))
    w2 = rc.points_of(cfg, "c4_asfp2_server", rc.writer_of(cfg, 1100))
    if len(w1) != 10 or len(w2) != 10:
        raise rc.Fail(f"63④: 采集点数 {len(w1)}/{len(w2)} ≠ 10/10")
    db = [inst for (st, _i), inst in rc.server_instances(cfg).items() if st == "c4_influxdb_client"]
    if len(db) != 1:
        raise rc.Fail(f"63⑤: influxdb 实例数 {len(db)} ≠ 1（同参目标应被拒）")
    reg = base.registry()
    names = [e.get("name") for e in reg.get("entries", [])]
    for want in ("1号风机", "2号风机", "II区服务器", "计算库"):
        if want not in names:
            raise rc.Fail(f"63⑥: 注册表缺条目 {want}: {names}")
    if reg.get("channelHighWatermark") != 4:
        raise rc.Fail(f"63⑦: 水位 {reg.get('channelHighWatermark')} ≠ 4")
    rc.PH.check("63")


def s64():
    """必问下游：纯接入被拒；拒绝两次收摊；声明后目标级撤回回到必问缺口。
    注意：flow 的空转 filler「继续」会被必问下游应答闸门视为表达转发意图
    （2026-09-29 用例11 语义），故本段全部用 stop_on 停在缺口处手动驱动。"""
    conv = rc.Conv()
    text, _ = base.flow(conv, MSG64, answers=[(r"提供场站名称", "华能阿拉善")],
                        stop_on=(r"转往哪里|写入哪里",))
    if not re.search(r"转往哪里|写入哪里", text):
        raise rc.Fail(f"64⓪: 纯接入未触发必问下游缺口: {text[:200]}")
    t1 = conv.send("没有下游，先接进来就行。")
    if "必须伴随" not in t1 and "转往哪里" not in t1 and "转发" not in t1:
        raise rc.Fail(f"64①: 首次拒绝话术缺失: {t1[:200]}")
    t2 = conv.send("真的不需要。")
    if "收尾终止" not in t2 and "收" not in t2:
        raise rc.Fail(f"64②: 第二次未强制收摊: {t2[:200]}")
    if rc.read_config() is not None:
        raise rc.Fail("64③: 拒绝路径 config 不应落盘")
    # 变体：声明下游 → 方案展示（不确认）→ 目标级撤回 → 回到必问缺口
    conv2 = rc.Conv()
    text2, _ = base.flow(conv2, MSG64,
                         answers=[(r"提供场站名称", "华能阿拉善"),
                                  (r"转往哪里|写入哪里", MSG64_DOWN)],
                         stop_on=(r"是否确认|转发点表",))
    if not re.search(r"是否确认|转发点表|5000", text2):
        raise rc.Fail(f"64④: 下游声明未被受理: {text2[:200]}")
    if rc.read_config() is not None:
        raise rc.Fail("64④-: 确认前 config 不应落盘")
    a2 = conv2.send("取消转发目标。")
    if "转往哪里" not in a2 and "写入哪里" not in a2:
        raise rc.Fail(f"64⑤: 撤回后未回到必问缺口: {a2[:200]}")
    rc.PH.check("64")


def s65():
    """点集表达式：12 点净集；同段多选择器 7 点；修改下游澄清拒绝。"""
    conv = rc.Conv()
    base.flow(conv, SITE_PRE + MSG63,
              answers=[(r"提供场站名称", "华能阿拉善"),
                       (r"目标地址", MSG63_ADDR),
                       (r"bucket", MSG63_BUCKET),
                       (r"field", ANS63_FIELDS)],
              done=lambda: rc.writer_of(rc.read_config() or {}, 1100) is not None)
    cfg0 = rc.read_config()
    if rc.forward_of(cfg0, 5000) is None:
        raise rc.Fail("65②: 前置（63 完成态）未就绪")
    # 入口 B：点集表达式新建统计库
    text, _ = base.flow(conv, MSG65,
                        answers=[(r"field", ANS65_FIELDS), (r"measurement", ANS_MEAS)],
                        done=lambda: len(
        [inst for (st, _i), inst in rc.server_instances(rc.read_config() or {}).items()
         if st == "c4_influxdb_client"]) >= 2)
    cfg = rc.wait_config(
        lambda c: len([k for k in rc.server_instances(c)
                       if k[0] == "c4_influxdb_client"]) >= 2,
        timeout=240, desc="65: 统计库实例落地",
    )
    db = [inst for (st, _i), inst in rc.server_instances(cfg).items() if st == "c4_influxdb_client"]
    stats = next((i for i in db if i.get("bucket") == "stats"), None)
    if stats is None:
        raise rc.Fail(f"65③: 未找到 bucket=stats 实例: {[i.get('bucket') for i in db]}")
    if len(stats.get("points", [])) != 12:
        raise rc.Fail(f"65④: 统计库点数 {len(stats.get('points', []))} ≠ 12")
    meas = sorted({str(p.get("measurement")) for p in stats.get("points", [])})
    # 2026-10-09 裁定：入库标识字段不得中文——「measurement按设备名」落设备前缀
    if meas != sorted(["wt1", "wt2"]):
        raise rc.Fail(f"65⑤: measurement 按设备名期望前缀 wt1/wt2，实得 {meas}")
    reg = base.registry()
    if not any(e.get("name") == "统计库" for e in reg.get("entries", [])):
        raise rc.Fail("65⑥: 注册表缺统计库目标条目")
    # 同段多选择器（空格并列）→ 7 点
    base.flow(conv, MSG65_VAR,
              answers=[(r"field", ANS65V_FIELDS), (r"measurement", ANS_MEAS)],
              done=lambda: len(
        [i for (st, _i), i in rc.server_instances(rc.read_config() or {}).items()
         if st == "c4_influxdb_client"]) >= 3)
    cfg2 = rc.wait_config(
        lambda c: len([k for k in rc.server_instances(c)
                       if k[0] == "c4_influxdb_client"]) >= 3,
        timeout=240, desc="65: 明细库实例落地",
    )
    db2 = [i for (st, _i), i in rc.server_instances(cfg2).items() if st == "c4_influxdb_client"]
    detail = next((i for i in db2 if i.get("bucket") == "stats_detail"), None)
    if detail is None or len(detail.get("points", [])) != 7:
        raise rc.Fail(f"65⑦: 明细库 7 点断言失败: {detail and len(detail.get('points', []))}")
    # 修改下游澄清拒绝
    t = conv.send("把统计库的bucket改成stats2。")
    if "无法" not in t and "不支持" not in t and "澄清" not in t and "修改" not in t:
        raise rc.Fail(f"65⑧: 修改下游未被澄清拒绝: {t[:200]}")
    reg65 = base.registry()
    tgt = next((e for e in reg65.get("entries", []) if e.get("name") == "统计库"), None)
    inst = next((i for i in db2 if str(i.get("name")) == "统计库"), None)
    if tgt is not None and inst is not None and inst.get("bucket") == "stats2":
        raise rc.Fail("65⑨: 修改下游被错误执行")
    rc.PH.check("65")


def s66():
    """首接收缩明示 + 追加补建 + 同实例重复拒绝。"""
    conv = rc.Conv()
    base.flow(conv, SITE_PRE + MSG66_1, answers=[(r"提供场站名称", "华能阿拉善")],
              done=lambda: rc.writer_of(rc.read_config() or {}, 1000) is not None)
    cfg = rc.wait_config(lambda c: rc.writer_of(c, 1000) is not None,
                         timeout=240, desc="66①: 收缩接入完成")
    w = rc.points_of(cfg, "c4_asfp2_server", rc.writer_of(cfg, 1000))
    if len(w) != 10 or 1010 in w or 1011 in w:
        raise rc.Fail(f"66①: 收缩后应 10 点（无 1010/1011），实得 {len(w)}")
    # 入口 B：既有目标追加点组 + Writer 补建
    base.flow(conv, MSG66_2, done=lambda: len(
        rc.points_of(rc.read_config() or {}, "c4_asfp2_server",
                     rc.writer_of(rc.read_config() or {}, 1000))) == 12)
    cfg2 = rc.wait_config(
        lambda c: len(rc.points_of(c, "c4_asfp2_server", rc.writer_of(c, 1000))) == 12,
        timeout=240, desc="66②: 补建后 12 点",
    )
    w2 = rc.points_of(cfg2, "c4_asfp2_server", rc.writer_of(cfg2, 1000))
    if 1010 not in w2 or 1011 not in w2:
        raise rc.Fail("66③: 补建点 1010/1011 缺失")
    rd = rc.points_of(cfg2, "c4_asfp2_client", rc.forward_of(cfg2, 5000))
    if 5010 not in rd or 5011 not in rd:
        raise rc.Fail(f"66④: II区 实例未追加 5010/5011: {sorted(rd)}")
    # 同实例重复拒绝
    t = conv.send(MSG66_3)
    if "已有一份映射" not in t and "无法在同一实例内" not in t and "重复" not in t:
        raise rc.Fail(f"66⑤: 同实例重复未被拒绝: {t[:200]}")
    cfg3 = rc.read_config()
    rd3 = rc.points_of(cfg3, "c4_asfp2_client", rc.forward_of(cfg3, 5000))
    if 5012 in rd3:
        raise rc.Fail("66⑥: 同实例重复被错误执行")
    rc.PH.check("66")


def s67():
    """删除下游收缩、收缩至空停用、级联、同事务删 A 增 D。"""
    # 前置：自足单设备 + 两路下游
    conv = rc.Conv()
    base.flow(conv, SITE_PRE + MSG67_0,
              answers=[(r"提供场站名称", "华能阿拉善"), (r"field", ANS63_FIELDS)],
              done=lambda: rc.forward_of(rc.read_config() or {}, 5000) is not None)
    cfg0 = rc.wait_config(
        lambda c: rc.forward_of(c, 5000) is not None and len(
            [k for k in rc.server_instances(c)
             if k[0] == "c4_influxdb_client"]) >= 1,
        timeout=240, desc="67 前置: 两路下游完成态",
    )
    # 场景①：删 II区（共享点保留，Writer 零收缩）
    base.flow(conv, MSG67_1, stop_on=[r"恢复原样", "无法"],
              done=lambda: rc.forward_of(rc.read_config() or {}, 5000) is None)
    cfg1 = rc.wait_config(lambda c: rc.forward_of(c, 5000) is None,
                          timeout=240, desc="67①: II区 删除")
    if rc.writer_of(cfg1, 1000) is None:
        raise rc.Fail("67①: Writer 不应收缩（计算库仍引用）")
    if len([k for k in rc.server_instances(cfg1) if k[0] == "c4_asfp2_client"]) != 0:
        raise rc.Fail("67①: asfp2_client 段应清空")
    reg1 = base.registry()
    if any(e.get("name") == "II区服务器" for e in reg1.get("entries", [])):
        raise rc.Fail("67①: II区服务器 目标条目未删除（幽灵条目）")
    # 场景②：删计算库（收缩至空 → 设备停用明示）
    text2, _ = base.flow(conv, MSG67_2, stop_on=[r"恢复原样", "无法"],
                         done=lambda: rc.read_config() is not None and len(
                             rc.server_instances(rc.read_config())) <= 1)
    cfg2 = rc.read_config()
    writers = [k for k in rc.server_instances(cfg2 or {})
               if k[0] == "c4_asfp2_server"]
    if writers:
        raise rc.Fail(f"67②: 收缩至空后 Writer 实例仍存在: {rc.server_instances(cfg2)}")
    # 停用明示在方案展示文案（确认后的执行回复是「接入已完成」）——从会话历史取
    plan2 = next((h.get("content", "") for h in conv.history
                  if h.get("role") == "assistant" and "整体停用" in h.get("content", "")), "")
    if not plan2:
        raise rc.Fail(f"67②: 收缩至空未明示整体停用: {text2[:300]}")
    reg2 = base.registry()
    if any(e.get("name") == "1号风机" for e in reg2.get("entries", [])):
        raise rc.Fail("67②: 设备条目 1号风机 未同步删除")
    rc.PH.check("67")

    # 场景③：删除设备级联（独立回零重跑前置）
    conv2 = rc.Conv()
    base.flow(conv2, SITE_PRE + MSG67_0,
              answers=[(r"提供场站名称", "华能阿拉善"), (r"field", ANS63_FIELDS)],
              done=lambda: rc.forward_of(rc.read_config() or {}, 5000) is not None)
    base.flow(conv2, MSG67_3, stop_on=[r"恢复原样", "无法"],
              done=lambda: rc.read_config() is None or len(rc.server_instances(rc.read_config())) == 0)
    cfg3 = rc.read_config()
    data_insts = [k for k in rc.server_instances(cfg3 or {})
                  if k[0] != "c4_shm_manager"]
    if data_insts:
        raise rc.Fail(f"67③: 删除设备后仍有数据实例: {rc.server_instances(cfg3)}")
    reg3 = base.registry()
    leftover = [e.get("name") for e in reg3.get("entries", [])
                if e.get("name") in ("1号风机", "II区服务器", "计算库")]
    if leftover:
        raise rc.Fail(f"67③: 幽灵条目残留: {leftover}")
    rc.PH.check("67")

    # 场景④：同事务删 A 增 D（独立回零重跑前置）
    conv3 = rc.Conv()
    base.flow(conv3, SITE_PRE + MSG67_0,
              answers=[(r"提供场站名称", "华能阿拉善"), (r"field", ANS63_FIELDS)],
              done=lambda: rc.forward_of(rc.read_config() or {}, 5000) is not None)
    # 退出/等待按端口判定：第三方同为点表 5000~5009，forward_of(5000) 无法区分新旧
    def _third_on(c, port):
        return any(i.get("port") == port
                   for (st, _i), i in rc.server_instances(c or {}).items()
                   if st == "c4_asfp2_client")
    base.flow(conv3, MSG67_4, stop_on=[r"恢复原样", "无法"],
              done=lambda: _third_on(rc.read_config(), 19901))
    cfg4 = rc.wait_config(
        lambda c: _third_on(c, 19901) and not _third_on(c, 19900),
        timeout=240, desc="67④: II区 删除+第三方 新增",
    )
    third = [i for (st, _i), i in rc.server_instances(cfg4).items()
             if st == "c4_asfp2_client" and i.get("port") == 19901]
    if not third:
        raise rc.Fail(f"67④: 第三方实例未落地: {rc.server_instances(cfg4)}")
    if rc.writer_of(cfg4, 1000) is None:
        raise rc.Fail("67④: Writer 应零收缩（第三方引用计入）")
    rc.PH.check("67")


CHAINS = [
    ("63", ["63"], s63),
    ("64", ["64"], s64),
    ("65", ["65"], s65),
    ("66", ["66"], s66),
    ("67", ["67"], s67),
]


def main():
    only = set(sys.argv[1:])
    chains = [c for c in CHAINS if not only or c[0] in only]
    rc.log(f"════ 多下游接入串行测试开始：{'/'.join(c[0] for c in chains)} ════")
    for label, cases, fn in chains:
        rc.log(f"════ 链段 {label} 开始 ════")
        # 67 全段 5 次合法确认（前置+①+②+③前置+③删除）——同 51 先例的按段预算
        rc.PH.reset(label, button_budget={"67": 7}.get(label, 4))
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
            rc.log(f"════ 多下游在第 {label} 段停止（已通过: {PASSED or '无'}）════")
            sys.exit(1)
        except Exception as e:
            base.dump_diag(label, e)
            rc.log(f"════ 链段 {label} 异常 {type(e).__name__}: {str(e)[:400]} ════")
            rc.log(f"════ 多下游在第 {label} 段停止（已通过: {PASSED or '无'}）════")
            sys.exit(1)
        PASSED.extend(cases)
        rc.log(f"════ 链段 {label} PASS（{time.time()-t0:.0f}s）════")
    rc.log(f"════ 多下游接入全部通过：{PASSED} ════")
    rc.log(f"════ 标注清单（AI测试通过 C4He1）：{PASSED} ════")


if __name__ == "__main__":
    main()
