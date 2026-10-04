#!/usr/bin/env python3
# func_test_case.md 独立组一（第一批）串行驱动器（v2.1.0 命名体系）
# 链段（各自独立回零 A.2，场站询问答「华能阿拉善」）：
#   5    —— 升压站 + 转发起始地址自然语言（「从一万开始」→ 10000~10009）
#   8    —— 未提供点名：逐点追问 → 用户仍不提供 → 拒绝接入（点名必填不自动生成）
#   47A  —— 设备必答缺口：裸数字「2」绑定（累积文本含类型词）→ 方案 wt2_，不确认不落盘
#   47C  —— 匿名设备 → dev 序列，方案明示 dev1_，不确认不落盘
#   47D  —— 名称含下划线 power_forecast_1 → 前缀剔除下划线，不确认不落盘
#   47E  —— 一条消息两台设备 → 逐台接入（先 1号后 2号），注册表 wt1/wt2 双固化
#   42   —— modbus 设备不可达：start 成功、不谎报已采集（数据面无从站 defer）
#   51   —— 注册表固化时机：占端口注入 PORT_BIND_FAILED 回滚无幽灵条目 → 重接固化
#           → 步骤 B 删注册表重建 + 水位降级（已删序号可复用）
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
SETUP_ONLY = {"A.2"}

MSG5 = ("现在需要接入升压站的数据，第三方厂家转过来的，asfp2协议，2390:电网频率，2391是正向有功，"
        "2392是反向有功，2393是正向无功，2394是反向无功，2395是uab，2396是ubc，2397是uac，"
        "2398是变压器油温，2399是环境温度。将数据转发到II服务器的127.0.0.1:9900上，转发采用asfp2协议。")
ANS5 = "接收端口使用9001，转发地址使用从一万开始的地址。"

MSG8 = ("现在需要接入1号风机的数据，第三方厂家通过asfp2协议给我们转来1#风机数据，10个点，"
        "从1000到1009。我们需要将这些数据转发到II区服务器上，转发采用asfp2协议，"
        "目标地址是127.0.0.1:9900。")
ANS8_REFUSE = "不提供点名，你看着办。"

MSG47 = ("现在需要接入风机的数据，第三方厂家通过asfp2协议给我们转来数据，10个点，从1000到1009，"
         "10个点分别是1000:风速、1001:功率、1002:风向、1003:桨叶角度、1004:发电机转速、"
         "1005:齿轮箱油温、1006:塔筒温度、1007:空气温度、1008:空气湿度、1009:大气压强，使用端口9001。")
ANS47A = "2"
ANS47C = "没有名字，就是台匿名设备，随便。"
ANS47D = "设备名叫 power_forecast_1"
MSG47E = ("现在需要接入1号风机和2号风机的数据，都是第三方厂家通过asfp2协议转来的。"
          "1号风机10个点，从1000到1009，1000:风速、1001:功率、1002:风向、1003:桨叶角度、"
          "1004:发电机转速、1005:齿轮箱油温、1006:塔筒温度、1007:空气温度、1008:空气湿度、"
          "1009:大气压强，使用端口9001；2号风机10个点，从1100到1109，点名与1号相同，"
          "使用端口9002。转发都到II区服务器127.0.0.1:9900，转发采用asfp2协议，"
          "1号点表5000~5009，2号点表6000~6009。")

MSG42 = ("现在需要接入1号风机变桨控制器的数据，厂家在变桨控制器上开放了modbus采集口，"
         "设备IP是192.168.110.51，端口502，从站号1。点表10个点：3000:桨叶角度、3002:变桨速度、"
         "3004:变桨电机温度、3006:变桨电机电流、3008:后备电源电压、3010:后备电源温度、"
         "3012:桨叶1位置、3014:桨叶2位置、3016:桨叶3位置、3018:轮毂温度。除桨叶1/2/3位置是"
         "输入寄存器（功能码4）外，其余全是保持寄存器（功能码3），全部32位浮点、字节交换2（swap=2）。"
         "我们需要将这些数据转发到II区服务器上，转发采用asfp2协议，目标地址是127.0.0.1:9900，"
         "点表5000~5009。")

MSG51A = ("现在需要接入1号风机的数据，第三方厂家通过asfp2协议给我们转来1#风机数据，10个点，"
          "从1000到1009，10个点分别是1000:风速、1001:功率、1002:风向、1003:桨叶角度、"
          "1004:发电机转速、1005:齿轮箱油温、1006:塔筒温度、1007:空气温度、1008:空气湿度、"
          "1009:大气压强，使用端口18099。我们需要将这些数据转发到II区服务器上，转发采用asfp2协议，"
          "目标地址是127.0.0.1:9900，点表5000~5009。")
MSG51_B_R2 = ("现在需要接入2号风机的数据，第三方厂家通过asfp2协议给我们转来2#风机数据，10个点，"
              "从1100到1109，10个点分别是1100:风速、1101:功率、1102:风向、1103:桨叶角度、"
              "1104:发电机转速、1105:齿轮箱油温、1106:塔筒温度、1107:空气温度、1108:空气湿度、"
              "1109:大气压强，使用端口9002。我们需要将这些数据转发到II区服务器上，"
              "转发采用asfp2协议，目标地址是127.0.0.1:9901，点表6000~6009。")


def occupy_port(port, seconds=1800):
    """51 步骤 A：脚本占用高位端口（触发 PORT_BIND_FAILED）。返回 Popen。"""
    return subprocess.Popen(
        [sys.executable, "-c",
         f"import socket,time; s=socket.socket(); s.bind(('0.0.0.0',{port})); "
         f"s.listen(1); time.sleep({seconds})"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cfg_has_modbus():
    return sum(1 for k in rc.server_instances(rc.read_config() or {})
               if k[0] == "c4_modbus_client") >= 1


def assert_empty_config(tag):
    cfg = rc.read_config()
    insts = rc.server_instances(cfg or {})
    live = [k for k in insts if k[0] != "c4_shm_manager"]
    if live:
        raise rc.Fail(f"{tag}: config 应为空态，却有实例 {live}")


def plan_prefix_set():
    out = set()
    for e in rc.PH.entries:
        if e.get("kind") != "assistant":
            continue
        for m in re.finditer(r"→\s*([a-z][a-z0-9]*?)_[a-z0-9_]+", e.get("text") or ""):
            out.add(m[1])
        for m in re.finditer(r"点 key 前缀\s*([a-z][a-z0-9]*?)_", e.get("text") or ""):
            out.add(m[1])
    return out


# ── 链段实现 ───────────────────────────────────────────────
def s5(influx=None):
    conv = rc.Conv()
    base.flow(conv, MSG5, answers=[(r"场站", "华能阿拉善"),
                                   (r"端口|转发|一万", ANS5),
                                   (r"端口|转发|一万", ANS5)],
              done=lambda: rc.writer_of(rc.read_config() or {}, 2390) is not None)
    cfg = rc.wait_config(lambda c: rc.writer_of(c, 2390) is not None
                         and rc.forward_of(c, 10000) is not None,
                         timeout=240, desc="5: 升压站接入 + 转发 10000 起")
    base.assert_channel_ids(cfg)
    # syz 前缀（升压站类型映射）+ 2390~2399 十点
    base.assert_writer_keys(cfg, "syz", range(2390, 2400))
    base.assert_forward_key(cfg, 10000, rc.writer_of(cfg, 2390), 2390)
    reg = base.registry()
    if reg.get("channelHighWatermark") != 2:
        raise rc.Fail(f"5: 水位={reg.get('channelHighWatermark')} ≠ 2")
    base.assert_no_handle_leak("5")


def s8(influx=None):
    conv = rc.Conv()
    text = conv.send(MSG8)
    time.sleep(4)
    # ① 必须逐点询问点名，不得发明「点1000」式名称、不得出方案
    if not re.search(r"点名|点描述|名称", text):
        raise rc.Fail(f"8①: 未询问点名: {text}")
    if re.search(r"是否确认|确认执行", text):
        raise rc.Fail(f"8①: 缺点名却进入可确认方案: {text}")
    # ② 用户仍不提供 → 拒绝接入（点名必填、不自动生成）
    text2 = conv.send(ANS8_REFUSE)
    time.sleep(4)
    text3 = conv.send("我真的没有点名。")
    time.sleep(4)
    dialog = text + text2 + text3
    if not re.search(r"点名|自动生成|必须|无法|不能|需要", dialog):
        raise rc.Fail(f"8②: 无拒绝/必填信号: {dialog}")
    assert_empty_config("8")
    reg = base.registry()
    if (reg or {}).get("entries"):
        raise rc.Fail(f"8: 注册表残留: {reg.get('entries')}")
    base.assert_no_handle_leak("8")


def s47_variant(ans, expect_prefix, tag, expect_dev=None):
    """47 变体 A/C/D 公共流：场站先闭合 → 问设备 → 应答 → 方案明示前缀即止（不确认不落盘）。"""
    conv = rc.Conv()
    text = conv.send(MSG47)
    time.sleep(4)
    # 缺口依赖序：场站先于 recv.device——先闭合场站
    if re.search(r"场站名称", text):
        text = conv.send("华能阿拉善")
        time.sleep(4)
    if not re.search(r"设备|叫什么|名称|编号", text):
        raise rc.Fail(f"{tag}①: 未询问设备名称/编号: {text}")
    if re.search(r"是否确认|确认执行", text):
        raise rc.Fail(f"{tag}①: 缺设备名却进入可确认方案: {text}")
    # ① 不得以类型词蒙混：设备名缺口闭合前，消息中的「风机」不得成为设备名
    if re.search(r"· 设备：风机", "".join(base._assistant_texts())):
        raise rc.Fail(f"{tag}①: 纯类型词「风机」被当作设备名（蒙混）")
    text2 = conv.send(ans)
    time.sleep(4)
    dialog = text + text2
    if expect_dev and expect_dev not in dialog:
        raise rc.Fail(f"{tag}②: 应答后 recap 未按应答命名（缺 {expect_dev}）: {text2[:400]}")
    # 补齐转发背景信息（MSG47 无转发语句，47 的被测对象是设备缺口与前缀派生），
    # 到达方案确认提示即止——不点确认、不落任何配置（变体执行状态约定）
    text3 = conv.send("转发采用asfp2协议，转发到127.0.0.1:9900，点表5000~5009。")
    time.sleep(4)
    dialog = dialog + text3
    if expect_prefix and expect_prefix not in dialog:
        raise rc.Fail(f"{tag}③: 方案未明示前缀 {expect_prefix}_: {text3[:400]}")
    assert_empty_config(tag)
    if (base.registry() or {}).get("entries"):
        raise rc.Fail(f"{tag}: 注册表残留（未确认不应固化）")
    base.assert_no_handle_leak(tag)


def s47a(influx=None):
    s47_variant(ANS47A, "wt2", "47A", expect_dev="2号风机")


def s47c(influx=None):
    s47_variant(ANS47C, "dev1", "47C")


def s47d(influx=None):
    s47_variant(ANS47D, "powerforecast1", "47D", expect_dev="power_forecast_1")


def s47e(influx=None):
    """47 变体 E：两台逐台接入（先 1号后 2号），注册表双固化。"""
    conv = rc.Conv()
    base.flow(conv, MSG47E, answers=[(r"场站", "华能阿拉善")],
              done=lambda: rc.writer_of(rc.read_config() or {}, 1100) is not None)
    cfg = rc.wait_config(lambda c: rc.writer_of(c, 1000) is not None
                         and rc.writer_of(c, 1100) is not None
                         and rc.forward_of(c, 5000) is not None
                         and rc.forward_of(c, 6000) is not None,
                         timeout=300, desc="47E: 两台逐台接入完成")
    base.assert_channel_ids(cfg)
    base.assert_writer_keys(cfg, "wt1", range(1000, 1010))
    base.assert_writer_keys(cfg, "wt2", range(1100, 1120))
    base.entry("wt1", name="1号风机", host=rc.writer_of(cfg, 1000))
    base.entry("wt2", name="2号风机", host=rc.writer_of(cfg, 1100))
    reg = base.registry()
    if reg.get("channelHighWatermark") != 4:
        raise rc.Fail(f"47E: 水位={reg.get('channelHighWatermark')} ≠ 4")
    base.assert_no_handle_leak("47E")


def s42(influx=None):
    conv = rc.Conv()
    base.flow(conv, MSG42, answers=[(r"场站", "华能阿拉善")],
              done=lambda: cfg_has_modbus())
    cfg = rc.wait_config(lambda c: sum(1 for k in rc.server_instances(c)
                                       if k[0] == "c4_modbus_client") >= 1,
                         timeout=240, desc="42: 不可达设备接入完成")
    base.assert_channel_ids(cfg)
    # ①② start 成功语义：配置面双实例成对；回复不得谎报数据已采集
    import importlib
    mb = importlib.import_module("run_chain_mb104")
    mb.assert_modbus_pair("42", cfg, 3000, 5000, "192.168.110.51", 502, 1,
                          expect={3000: (3, 10, 2), 3012: (4, 10, 2)})
    dialog = "".join(base._assistant_texts())
    if re.search(r"已采集|已有数据|数据正常|采集到.{0,6}数据", dialog):
        raise rc.Fail(f"42: 谎报数据已采集: "
                      f"{[m for m in re.findall(r'.{15}(?:已采集|已有数据).{15}', dialog)][:1]}")
    if base.registry().get("channelHighWatermark") != 2:
        raise rc.Fail(f"42: 水位 ≠ 2")
    base.assert_no_handle_leak("42")


def s51(influx=None):
    """步骤 A：占端口 → 回滚无幽灵条目 → 重接固化；步骤 B：删注册表重建 + 水位降级。"""
    # ── 步骤 A：失败路径 ──
    occupier = occupy_port(18099)
    try:
        time.sleep(1)
        conv = rc.Conv()
        base.flow(conv, MSG51A, answers=[(r"场站", "华能阿拉善")],
                  done=lambda: False, max_turns=8,
                  # 两种失败文案都要接住：网络类（PORT_BIND_FAILED→「网络连接出现问题，
                  # 服务未能启动。本次变更已恢复原样…」2026-10-04 实测）与通用类
                  #（「执行过程中出现问题，本次变更未生效…」）——与用例 60 stop_on 同口径
                  stop_on=[r"本次变更已恢复原样", r"执行过程中出现问题"])
        # 回滚异步于 flow 的实例数检测（merge 落盘即 return，回滚在其后）——
        # 先等回滚完成（最终态 = 空态），再断言无幽灵条目
        rc.wait_config(lambda c: not rc.server_instances(c or {}),
                       timeout=90, desc="51A: PORT_BIND_FAILED 回滚完成（空态）")
        time.sleep(2)
        assert_empty_config("51A")
        reg = base.registry()
        prefixes = [e.get("prefix") for e in ((reg or {}).get("entries") or [])]
        if "wt1" in prefixes:
            raise rc.Fail(f"51①: 回滚后注册表残留幽灵条目 wt1: {reg}")
        dialog = "".join(base._assistant_texts())
        if not re.search(r"失败|回滚|恢复原样|未能|无法", dialog):
            raise rc.Fail(f"51①: 无失败汇报信号: {dialog[:300]}")
        # ── 步骤 A 成功路径：按用例 1 原输入（9001）重接 ──
        conv2 = rc.Conv()
        base.flow(conv2, rc.MSG_CASE1, done=lambda: rc.writer_of(rc.read_config() or {}, 1000) is not None)
        cfg = rc.wait_config(lambda c: rc.writer_of(c, 1000) is not None,
                             timeout=240, desc="51A: 重接成功")
        wid1 = rc.writer_of(cfg, 1000)
        base.entry("wt1", name="1号风机", host=wid1)
        # ── 步骤 B：再接 2号 → 删除 → A.5 删注册表 → 重建 + 水位降级 ──
        conv3 = rc.Conv()
        base.flow(conv3, MSG51_B_R2,
                  done=lambda: rc.writer_of(rc.read_config() or {}, 1100) is not None)
        rc.wait_config(lambda c: rc.writer_of(c, 1100) is not None,
                       timeout=240, desc="51B: 2号接入")
        conv4 = rc.Conv()
        base.flow(conv4, "删除2号风机。",
                  done=lambda: rc.writer_of(rc.read_config() or {}, 1100) is None)
        rc.wait_config(lambda c: rc.writer_of(c, 1100) is None, timeout=180,
                       desc="51B: 2号删除")
        reg = base.registry()
        if reg.get("channelHighWatermark") != 4:
            raise rc.Fail(f"51B: 删2号后水位={reg.get('channelHighWatermark')} ≠ 4")
        # A.5 等价（隔离栈）：停 agent → 删注册表 → 起 agent
        subprocess.run(["fuser", "-k", "19720/tcp"], capture_output=True, timeout=5)
        time.sleep(1.5)
        reg_path = os.path.join(rc.AGENT_DIR, "abbr_registry.json")
        if os.path.exists(reg_path):
            os.remove(reg_path)
        agent = base.V21Agent()
        agent.up()
        rc.AGENT = agent
        time.sleep(2)
        # 重建断言：wt1 条目恢复（name 退化）、watermark 降级 = 现存实例最大序号。
        # MSG_CASE1 含转发 → wt1 = writer channel1 + reader channel2 两实例，
        # 降级水位 = 2（2026-10-03 run11 实测确认，产品行为符合设计）
        reg = base.registry()
        prefixes = [e.get("prefix") for e in (reg.get("entries") or [])]
        if prefixes != ["wt1"]:
            raise rc.Fail(f"51B③: 重建条目 {prefixes} ≠ [wt1]")
        e1 = reg["entries"][0]
        if e1.get("host") != wid1 or e1.get("name") not in ("wt1", "1号风机"):
            raise rc.Fail(f"51B③: 重建条目异常: {e1}")
        # pointMap 重建契约 = 忠实镜像 config.json 点表（name→id），期望值由 config
        # 推导、不硬编码 LLM 译名——「风速」→windspeed（5.3-flash 代）/wind_speed
        # （4.5-air 代）均为合法 snake_case（2026-10-04 实测），断言钉契约不钉译名
        expected_pm = {}
        for inst in (rc.read_config() or {}).get("c4_asfp2_server", []) or []:
            for p in inst.get("points", []) or []:
                expected_pm[p.get("name")] = p.get("id")
        if e1.get("pointMap", {}) != expected_pm:
            raise rc.Fail(f"51B③: pointMap 未按点表重建: {e1.get('pointMap')} ≠ {expected_pm}")
        if reg.get("channelHighWatermark") != 2:
            raise rc.Fail(f"51B④: 水位降级={reg.get('channelHighWatermark')} ≠ 2（现存最大序号：wt1 占 1/2）")
        # 重接 2号（9002 已释放）→ writer = 现存最大 + 1 = channel3（+reader channel4，
        # 已删序号不填坑，水位语义 = 高水位重建后再顺延）
        conv5 = rc.Conv()
        base.flow(conv5, MSG51_B_R2, answers=[(r"场站", "华能阿拉善")],
                  done=lambda: rc.writer_of(rc.read_config() or {}, 1100) is not None)
        cfg = rc.wait_config(lambda c: rc.writer_of(c, 1100) is not None,
                             timeout=240, desc="51B: 重建后重接 2号")
        wid2 = rc.writer_of(cfg, 1100)
        if wid2 != "channel3":
            raise rc.Fail(f"51B④: 重建后新实例 {wid2} ≠ channel3（降级水位 +1）")
        reg = base.registry()
        if reg.get("channelHighWatermark") != 4:
            raise rc.Fail(f"51B④: 终态水位={reg.get('channelHighWatermark')} ≠ 4（2号 writer+reader 占 3/4）")
        base.assert_no_handle_leak("51")
    finally:
        occupier.terminate()


# ── 链调度（串行，链间回零，失败即停）────────────────────
CHAIN_TAG = "C4He1"
CHAIN_FAIL_DIR = base.CHAIN_FAIL_DIR

# 47E（两台逐台）暂移出：复合设备名「1号风机和2号风机」整串捕获——多台逐台接入
# 的台次拆分状态机未实现（设计 agent.md「一次接入会话针对一台设备」有裁定），
# 属功能级缺口待用户裁定（2026-10-03 链段实测）
CHAINS = [
    ("5",    ["5"],        s5),
    ("8",    ["8"],        s8),
    ("47A",  ["47"],       s47a),
    ("47C",  [],           s47c),
    ("47D",  [],           s47d),
    ("42",   ["42"],       s42),
    ("51",   ["51"],       s51),
]


def main():
    # argv 可选链段过滤（如 `python run_chain_ind1.py 51` 只跑 51 段）；缺省全量
    only = set(sys.argv[1:])
    chains = [c for c in CHAINS if not only or c[0] in only]
    rc.log(f"════ 独立组一（第一批）串行测试开始：{'/'.join(c[0] for c in chains)} ════")
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
        except rc.Fail as e:
            base.dump_diag(label, e)
            rc.log(f"════ 链段 {label} FAIL: {e} ════")
            rc.log(f"════ 独立组一在第 {label} 段停止（已通过用例: {PASSED or '无'}）════")
            sys.exit(1)
        except Exception as e:
            base.dump_diag(label, e)
            rc.log(f"════ 链段 {label} 异常 {type(e).__name__}: {str(e)[:400]} ════")
            rc.log(f"════ 独立组一在第 {label} 段停止（已通过用例: {PASSED or '无'}）════")
            sys.exit(1)
        PASSED.extend(cases)
        rc.log(f"════ 链段 {label} PASS（{time.time()-t0:.0f}s）════")
    rc.log(f"════ 独立组一（第一批）全部通过：{PASSED} ════")
    rc.log(f"════ 标注清单（AI测试通过 {CHAIN_TAG}）：{PASSED} ════")


if __name__ == "__main__":
    main()
