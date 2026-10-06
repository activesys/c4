#!/usr/bin/env python3
# func_test_case.md 场站变更链串行驱动器（用例 68~77，2026-10-06 设计定稿建套）
# 链：A.2（无 site 起步）→ 68 → 69 → 70 → 71 → 72 → 73 → 74 → 75 → 76 → 77
# （串行单 agent 栈，状态按设计累积；77 必须置链尾——各行改写场站绑定不回滚）。
# 覆盖：Web 首绑（API 等价）→ 首绑下接入 → 顶栏改名（改名不搬家 + rebindSite
# 即时生效）→ 四形态归属判定（一致/超集/去品牌 ambiguous/缺区号 other）→ 旧名与
# 显式改绑拒绝 → 无场站信息默认当前 + 既有设备修改流 → 多轮改名（数字/中文数字）
# + 缩写稳定 → influx 下游 measurement 沿用原缩写 → 重启持久化（含 v2.1.53 组绑定
# 落盘缺陷回归）→ POST /api/site 边界（400/422/幂等/三级兜底）。
#
# 与设计文档的执行口径偏差（均在文档授权的自动化等价范围内）：
#   1. 68/70 的【界面】断言（引导层浮层、点击穿透、顶栏 1s）不适用 e2e——API 等价
#      （文档 68②/70⑥ 自认）：siteName null ⇔ 引导层出现；POST /api/site ⇔ 保存。
#   2. 70/74 改名走 POST /api/site 显式带原缩写（文档 70⑥：自动化必须显式带原缩写）。
#   3. abbr 字面值不锚定 hnals（LLM 生成非确定）——锚定 [a-z0-9]{2,12} + 链内稳定性
#      （68 捕获 ABBR0，70/74 原缩写回传断言 abbr 恒等于 ABBR0，即「永不重新生成」）。
#   4. 74⑥ 形态抽查按文档「每轮至少抽查一形态」执行：轮 1 缺区号拒绝（确定性）；
#      轮 2 旧名拒绝 + 去品牌 ambiguous 确认后接入（9号 / 9005 / 9905——71④/72①
#      声明被拒未占用）；超集形态已由 71② 覆盖。
#   5. 77 的「LLM 不可用」行（国电/大唐辽宁三区 → 422）按文档 ⑤ 以无效 ZHIPU_API_KEY
#      独立实例执行，置于可用行之后（链尾，无恢复需求）。
#   6. 71③ ambiguous 确认应答「就是本场站的」后由既有会话草稿续接（实现口径：
#      提示语要求「重新发起接入」，但同文本重发会确定性再触发 ambiguous——确认语
#      不含场站词素、不触发归属判定，草稿在对话内持久）。
#   7. 75 消息措辞用例 43 的下游新增句式「把…写一份到…入库」（文档原文「…加一条
#      入库：…」不命中 CHANGE_INTENT_RE 的变更意图识别，会误入新接入路径对既有
#      设备起同名新草稿）；v2.1.55 已将「加一个点/加个点」补入该正则（73② 文档
#      原文措辞），下游新增句式扩充另行评估。
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_cases as rc  # noqa: E402
import run_chain_v21 as base  # noqa: E402
import run_chain_influx as influx_mod  # noqa: E402

# 本链端口扩充（附录 A.4：900[1-9] / 990[0-9]；9001/9002/9900~9904 已在基表）
rc.PORT_MAP.update({
    "9003": "19003", "9004": "19004", "9005": "19005", "9006": "19006",
    "9007": "19007", "9008": "19008", "9009": "19009",
    "9905": "19905", "9906": "19906", "9907": "19907", "9908": "19908",
    "9909": "19909",
})

PASSED = []
SETUP_ONLY = {"A.2"}

ABBR0 = None  # 68 首绑捕获；70/74 改名显式回传断言「原缩写永不重新生成」

SITE1 = "华能阿拉善"
SITE2 = "国电河北II区"
SITE3 = "华能通辽1区"
SITE4 = "大唐辽宁三区"

POINTS10 = ("10个点，从1000到1009，10个点分别是1000:风速、1001:功率、1002:风向、"
            "1003:桨叶角度、1004:发电机转速、1005:齿轮箱油温、1006:塔筒温度、"
            "1007:空气温度、1008:空气湿度、1009:大气压强")


def access_msg(dev, site_desc, rp, fp):
    """71① 消息模板（文档）：仅替换场站表述 / 设备编号 / 端口三处。"""
    return (f"现在需要接入{site_desc}{dev}风机的数据，第三方厂家通过asfp2协议给我们转来"
            f"{dev}数据，{POINTS10}，使用端口{rp}。我们需要将这些数据转发到II区服务器上，"
            f"转发采用asfp2协议，目标地址是127.0.0.1:{fp}，点表5000~5009。")


MSG69 = access_msg("1号", "", "9001", "9900")  # 无场站信息（模板自带「风机」，勿传全名）
MSG73_2 = "给1号风机加一个点：1010:机舱振动"
MSG73_3 = "删除1号风机的机舱振动点"
MSG74_SYZ = ("现在需要接入大唐辽宁三区升压站的数据，第三方厂家通过asfp2协议给我们转来"
             "升压站测点数据，10个点，从1100到1109，10个点分别是1100:油温、1101:绕组温度、"
             "1102:铁芯温度、1103:油位、1104:瓦斯压力、1105:负载电流、1106:母线电压、"
             "1107:功率因数、1108:有功功率、1109:无功功率，使用端口9009。我们需要将这些"
             "数据转发到II区服务器上，转发采用asfp2协议，目标地址是127.0.0.1:9909，"
             "点表5100~5109。")
# 75 措辞按变更流下游新增的确定性文法构造（三处教训，等价表述偏差见文件头清单）：
#   a. 文档原文「…加一条入库：…」不命中 CHANGE_INTENT_RE，误入新接入路径（v2.1.55 已补
#      「加一个点/加个点」，下游新增句式另行评估）；
#   b. 首选「url是…」形态被点集表达式解析器拒绝（冒号后片段须为以注册设备名开头的
#      点集文法段，multi_target.ts parse_point_set_expr）；
#   c. 按文法构造「：1号风机 全部，写入地址…」——expr 在「，写入地址」处截断（与用例
#      65 的「写入地址http://…」形态一致），measurement 不指定 → 走 §2.7.1 推导路径
#      （本用例被测点）。
MSG75 = ("把1号风机的10个点都写一份到另一个bucket：1号风机 全部，"
         "写入地址http://127.0.0.1:8086，token是hnals-influx-2026，org是activesys，"
         "bucket是hnals，字段名跟点名对应，类型统一float。")
MSG76_GROUP = ("现在接入一个风场的风机：1#~3#是倍福PLC风机，点表为1000:风速、1002:功率、"
               "1004:风向、1006:桨叶角度、1008:发电机转速、1010:齿轮箱油温、1012:塔筒温度、"
               "1014:空气温度、1016:空气湿度、1018:大气压强，它们的ip从192.168.1.101开始"
               "每台加1，端口都是502，从站号都是1；4#~5#是巴赫曼PLC风机，点表为2000:风速、"
               "2002:功率、2004:风向、2006:桨叶角度、2008:发电机转速、2010:齿轮箱油温、"
               "2012:塔筒温度、2014:空气温度、2016:空气湿度、2018:大气压强，"
               "4#的ip是192.168.2.10:502，5#的ip是192.168.2.11:502，从站号都是1。"
               "请接入这5台风机。")

FIELDS_BY_NAME = ["windspeed", "power", "wind_dir", "pitch_angle", "gen_speed",
                  "gearbox_oil_temp", "tower_temp", "air_temp", "humidity", "pressure"]


# ── agent 生命周期（76/77 需要「保留 agent.json 重启」与坏 key 实例）────────
def agent_start(bad_key=False):
    """启动隔离 agent，不重写 agent.json（76 持久化被测前提 / 77 坏 key 复用现态）。"""
    env = dict(os.environ)
    env["ZHIPU_API_KEY"] = ("invalid-key-case77-llm-unavailable" if bad_key
                            else rc.load_api_key())
    env["C4_SOCK_DIR"] = rc.SOCK_DIR
    rc.AGENT.f = open(f"/tmp/e2e_agent_{time.strftime('%H%M%S')}.log", "w")
    rc.AGENT.p = subprocess_popen(env)
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(rc.BASE + "/api/state", timeout=3) as r:
                json.loads(r.read().decode())
            rc.log(f"  隔离 agent 就绪（:19720, 坏key={bad_key}）")
            return
        except Exception:
            time.sleep(0.5)
    raise rc.Fail("隔离 agent 60s 未就绪（agent_start）")


def subprocess_popen(env):
    return subprocess.Popen(
        ["node", rc.AGENT_JS, "--config-dir", rc.AGENT_DIR],
        stdout=rc.AGENT.f, stderr=subprocess.STDOUT, env=env)


def agent_restart(bad_key=False):
    rc.wait_idle()
    rc.AGENT.stop()
    agent_start(bad_key=bad_key)


# ── 场站 API ──────────────────────────────────────────────
def api_get_site():
    with urllib.request.urlopen(rc.BASE + "/api/site", timeout=5) as r:
        return json.loads(r.read().decode())


def api_post_site(name, abbr, timeout=90):
    """POST /api/site；4xx/5xx 以 (status, body) 返回不抛异常（77 边界断言用）。"""
    body = json.dumps({"name": name, "abbr": abbr}).encode("utf-8")
    req = urllib.request.Request(rc.BASE + "/api/site", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def state_site_name():
    st = rc.state()
    return st.get("siteName", (st.get("state") or {}).get("siteName"))


def site_of_agent_json():
    return base.site_of_agent_json()


def assert_site_threefold(tag, name, abbr):
    """改名即时一致性三联：GET /api/site、GET /api/state siteName、agent.json。"""
    got = api_get_site().get("site")
    if not isinstance(got, dict) or got.get("name") != name or got.get("abbr") != abbr:
        raise rc.Fail(f"{tag}: GET /api/site = {got} ≠ {name}/{abbr}")
    sn = state_site_name()
    if sn != name:
        raise rc.Fail(f"{tag}: GET /api/state siteName = {sn!r} ≠ {name!r}")
    aj = site_of_agent_json()
    if aj != {"name": name, "abbr": abbr}:
        raise rc.Fail(f"{tag}: agent.json site = {aj} ≠ {name}/{abbr}")


def dump_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.dumps(json.load(f), sort_keys=True, ensure_ascii=False)
    except OSError:
        return None


def config_dump():
    return dump_file(os.path.join(rc.AGENT_DIR, "config.json"))


def registry_dump():
    return dump_file(os.path.join(rc.AGENT_DIR, "abbr_registry.json"))


def agent_json_dump():
    return dump_file(os.path.join(rc.AGENT_DIR, "agent.json"))


def conv_dialog(conv):
    return "".join(h.get("content", "") for h in conv.history
                   if h.get("role") == "assistant")


def assert_no_site_ask(tag, conv):
    if re.search(r"请提供.{0,12}场站|场站名称|站点名称", conv_dialog(conv)):
        raise rc.Fail(f"{tag}: 场站已绑定仍询问场站（绑定唯一）")


def writer_by_port(cfg, port):
    for (st, iid), inst in rc.server_instances(cfg).items():
        if st == "c4_asfp2_server" and inst.get("port") == port:
            return iid, inst
    return None, None


def assert_writer_prefix(tag, cfg, port, prefix, addrs):
    iid, inst = writer_by_port(cfg, port)
    if iid is None:
        raise rc.Fail(f"{tag}: 缺监听 {port} 的 asfp2_server 实例")
    pts = inst.get("points") or []
    by_addr = {p.get("addr"): p for p in pts}
    for a in addrs:
        pid = str(by_addr.get(a, {}).get("id", ""))
        if not pid.startswith(prefix + "_"):
            raise rc.Fail(f"{tag}: {port} addr={a} 点 key={pid!r} 不以前缀 {prefix}_ 开头")
    return iid, by_addr


def assert_watermark(tag, expect):
    wm = base.registry().get("channelHighWatermark")
    if wm != expect:
        raise rc.Fail(f"{tag}: 水位={wm} ≠ {expect}")


def assert_rejected(tag, text, cfg_before, aj_before):
    """确定性/语义拒绝统一断言：固定文案 + 不出方案 + config 与 agent.json 零变化。"""
    time.sleep(4)
    if not re.search(r"不属于当前场站", text):
        raise rc.Fail(f"{tag}: 未回复「不属于当前场站」: {text[:200]}")
    if re.search(r"是否确认|确认执行|接入方案|###", text):
        raise rc.Fail(f"{tag}: 拒绝路径仍出方案: {text[:200]}")
    if config_dump() != cfg_before:
        raise rc.Fail(f"{tag}: 拒绝路径 config 被改动")
    if agent_json_dump() != aj_before:
        raise rc.Fail(f"{tag}: 拒绝路径 agent.json 被改动")


# ── 链步实现 ───────────────────────────────────────────────
def s68():
    """68：Web 首绑 API 等价——siteName null（引导层出现⇔）→ POST → 落盘+顶栏+可读。"""
    global ABBR0
    if state_site_name() is not None:
        raise rc.Fail("68②: 前置错误——全新环境 siteName 应为 null（引导层出现态）")
    if api_get_site().get("site") is not None:
        raise rc.Fail("68②: 前置错误——GET /api/site 应为 null")
    status, body = api_post_site(SITE1, "")
    if status != 200:
        raise rc.Fail(f"68③: POST /api/site {status}: {body}")
    site = body.get("site") or {}
    if site.get("name") != SITE1:
        raise rc.Fail(f"68③: site.name = {site.get('name')!r} ≠ {SITE1!r}")
    abbr = str(site.get("abbr", ""))
    if not re.fullmatch(r"[a-z0-9]{2,12}", abbr):
        raise rc.Fail(f"68③: 缩写未生成或非法: {site}（LLM 拼音首字母路径）")
    ABBR0 = abbr
    assert_site_threefold("68④", SITE1, ABBR0)  # 顶栏（siteName）+ GET + agent.json
    rc.log(f"  68: 首绑落盘 {SITE1}/{ABBR0}（缩写 LLM 生成，链内恒定）")


def s69():
    """69：首绑后无场站信息接入——不询问场站、全链接入、site 不被对话改写、数据面。"""
    fwd = rc.start_receiver(19900)  # 9900 转发目标——先于配置拉起（s52 教训）
    try:
        conv = rc.Conv()
        base.flow(conv, MSG69, done=lambda: rc.writer_of(rc.read_config(), 1000) is not None)
        cfg = rc.wait_config(lambda c: rc.writer_of(c, 1000) is not None
                             and rc.forward_of(c, 5000) is not None,
                             timeout=240, desc="69: 1号风机 writer+转发")
        base.assert_channel_ids(cfg)
        assert_writer_prefix("69②", cfg, 19001, "wt1", range(1000, 1010))
        assert_no_site_ask("69①", conv)
        if site_of_agent_json() != {"name": SITE1, "abbr": ABBR0}:
            raise rc.Fail(f"69③: 对话改写了 site: {site_of_agent_json()}")
        assert_watermark("69②", 2)
        rc.wait_port(19001, True)
        base.data_smoke(fwd, 19001, 1000, 1010)  # 69④ 冒烟：送数 → 转发侧有数
    finally:
        fwd.stop()
    base.assert_no_handle_leak("69")


def s70():
    """70：顶栏改名 API 等价——原缩写回传、不搬家、rebindSite 即时生效（同进程）。"""
    cfg0, reg0, pid0 = config_dump(), registry_dump(), rc.AGENT.p.pid
    status, body = api_post_site(SITE2, ABBR0)  # ⑥ 显式带原缩写
    if status != 200:
        raise rc.Fail(f"70②: POST /api/site {status}: {body}")
    if (body.get("site") or {}).get("abbr") != ABBR0:
        raise rc.Fail(f"70②: 缩写被重新生成: {body}")
    assert_site_threefold("70③", SITE2, ABBR0)
    if config_dump() != cfg0:
        raise rc.Fail("70④: 改名改动了 config.json（违反改名不搬家）")
    if registry_dump() != reg0:
        raise rc.Fail("70④: 改名改动了 abbr_registry.json")
    # ⑤ 改名后立即对话正常应答（rebindSite 即时回灌，无重启）
    conv = rc.Conv()
    text = conv.send("已接入哪些设备？")
    rc.wait_idle()
    if re.search(r"不属于当前场站", text):
        raise rc.Fail(f"70⑤: 改名后对话被归属拒绝: {text[:150]}")
    if "1号" not in text:
        raise rc.Fail(f"70⑤: 设备清单未含 1号风机: {text[:200]}")
    if rc.AGENT.p.pid != pid0:
        raise rc.Fail("70⑤: agent 进程 pid 变化——改名不应重启服务")
    base.assert_no_handle_leak("70")


def s71():
    """71：变更后四形态——①完整 ②超集直接接入；③去品牌 ambiguous→确认→接入；④缺区号拒绝。"""
    aj0 = agent_json_dump()
    # ① 完整新名——一致直接接入
    conv1 = rc.Conv()
    base.flow(conv1, access_msg("2号", SITE2, "9002", "9901"),
              done=lambda: writer_by_port(rc.read_config(), 19002)[0] is not None)
    cfg = rc.wait_config(lambda c: writer_by_port(c, 19002)[0] is not None,
                         timeout=240, desc="71①: 2号接入")
    assert_writer_prefix("71①", cfg, 19002, "wt2", range(1000, 1010))
    assert_no_site_ask("71⑤", conv1)
    # ② 超集泛化（新名+风电场后缀，包含完整配置名）——一致直接接入
    conv2 = rc.Conv()
    base.flow(conv2, access_msg("3号", SITE2 + "风电场", "9003", "9903"),
              done=lambda: writer_by_port(rc.read_config(), 19003)[0] is not None)
    cfg = rc.wait_config(lambda c: writer_by_port(c, 19003)[0] is not None,
                         timeout=240, desc="71②: 3号接入")
    assert_writer_prefix("71②", cfg, 19003, "wt3", range(1000, 1010))
    assert_no_site_ask("71⑤", conv2)
    # ③ 去品牌子集泛化——site_ambiguous 确定性提醒，确认后接入
    conv3 = rc.Conv()
    base.flow(conv3, access_msg("4号", "河北II区风电场", "9004", "9904"),
              answers=[(r"归属不明确|请确认", "就是本场站的")],
              done=lambda: writer_by_port(rc.read_config(), 19004)[0] is not None)
    if not re.search(r"归属不明确|请确认", conv_dialog(conv3)):
        raise rc.Fail("71③: 去品牌形态未触发 site_ambiguous 确认")
    cfg = rc.wait_config(lambda c: writer_by_port(c, 19004)[0] is not None,
                         timeout=240, desc="71③: 确认后 4号接入")
    assert_writer_prefix("71③", cfg, 19004, "wt4", range(1000, 1010))
    # ④ 品牌保留、缺区号——site_mismatch 严格拒绝（多区常态裁定：点表不可混用）
    cfg0 = config_dump()
    text = rc.Conv().send(access_msg("5号", "国电河北风电场", "9005", "9905"))
    assert_rejected("71④", text, cfg0, aj0)
    assert_watermark("71④", 8)
    if agent_json_dump() != aj0:
        raise rc.Fail("71: agent.json 被改动")
    base.assert_no_handle_leak("71")


def s72():
    """72：旧名/异站/显式改绑声明拒绝 + 对照组（声明当前名放行）。"""
    aj0 = agent_json_dump()
    cfg0 = config_dump()
    # ① 旧场站名（语义层拒绝——确定性标签不触发：华能≠国电品牌）
    text1 = rc.Conv().send(access_msg("6号", SITE1, "9006", "9906"))
    assert_rejected("72①", text1, cfg0, aj0)
    # ② 另一异站完整名
    text2 = rc.Conv().send(access_msg("6号", SITE4 + "风电场", "9006", "9906"))
    assert_rejected("72②", text2, cfg0, aj0)
    # ③ 显式改绑声明（纯声明无设备信息）——对话内不可变更绑定
    text3 = rc.Conv().send(f"场站名称：{SITE1}")
    assert_rejected("72③", text3, cfg0, aj0)
    # ⑤ 对照组：声明当前场站名不构成改绑，按一致放行（无设备则仅确认归属继续）
    text5 = rc.Conv().send(f"场站名称：{SITE2}")
    time.sleep(3)
    if re.search(r"不属于当前场站", text5):
        raise rc.Fail(f"72⑤: 声明当前名被误拒: {text5[:150]}")
    if config_dump() != cfg0 or agent_json_dump() != aj0:
        raise rc.Fail("72⑤: 对照组改动了 config/agent.json")
    base.assert_no_handle_leak("72")


def s73():
    """73：无场站信息默认当前场站 + 改名前既有设备加点/删点（身份与场站名解耦）。"""
    def wt1_addrs():
        iid, _ = writer_by_port(rc.read_config(), 19001)
        if iid is None:
            return set()
        return set(rc.points_of(rc.read_config(), "c4_asfp2_server", iid))

    # ① 7号无场站信息直接接入
    conv = rc.Conv()
    base.flow(conv, access_msg("7号", "", "9007", "9907"),
              done=lambda: writer_by_port(rc.read_config(), 19007)[0] is not None)
    cfg = rc.wait_config(lambda c: writer_by_port(c, 19007)[0] is not None,
                         timeout=240, desc="73①: 7号接入")
    assert_writer_prefix("73①", cfg, 19007, "wt7", range(1000, 1010))
    assert_no_site_ask("73①", conv)
    assert_watermark("73①", 10)
    # ② 对改名前接入的 1号加点（缺转发地址 → 补答 5010，用例 20 语义）
    conv2 = rc.Conv()
    base.flow(conv2, MSG73_2, answers=[(r"转发|5010", "转发地址5010")],
              done=lambda: 1010 in wt1_addrs())
    cfg = rc.wait_config(lambda c: 1010 in (set(rc.points_of(
        c, "c4_asfp2_server", writer_by_port(c, 19001)[0]))
        if writer_by_port(c, 19001)[0] else set()),
        timeout=240, desc="73②: wt1 加点 1010")
    pts = {p.get("addr"): p for p in (writer_by_port(cfg, 19001)[1].get("points") or [])}
    pid1010 = str(pts.get(1010, {}).get("id", ""))
    if not pid1010.startswith("wt1_"):
        raise rc.Fail(f"73②: 新点 key={pid1010!r} 不以前缀 wt1_ 开头")
    if rc.forward_of(cfg, 5010) is None:
        raise rc.Fail("73②: 转发点 5010 缺失（新增点未成对）")
    # ③ 删点复原（用例 17 语义）
    conv3 = rc.Conv()
    base.flow(conv3, MSG73_3, answers=[(r"是否", "是，确认删除机舱振动点")],
              done=lambda: 1010 not in wt1_addrs())
    rc.wait_config(lambda c: 1010 not in (set(rc.points_of(
        c, "c4_asfp2_server", writer_by_port(c, 19001)[0]))
        if writer_by_port(c, 19001)[0] else set()),
        timeout=240, desc="73③: 机舱振动点已删")
    if site_of_agent_json() != {"name": SITE2, "abbr": ABBR0}:
        raise rc.Fail(f"73⑤: site 被改动: {site_of_agent_json()}")
    base.assert_no_handle_leak("73")


def rename_round(tag, new_name):
    cfg0, reg0 = config_dump(), registry_dump()
    status, body = api_post_site(new_name, ABBR0)
    if status != 200 or (body.get("site") or {}).get("abbr") != ABBR0:
        raise rc.Fail(f"{tag}: 改名 {status}: {body}")
    assert_site_threefold(tag, new_name, ABBR0)
    if config_dump() != cfg0 or registry_dump() != reg0:
        raise rc.Fail(f"{tag}: 改名改动了 config/registry（违反改名不搬家）")


def s74():
    """74：多轮改名链——数字/中文数字全名接入、缩写恒定、旧名逐轮失效、形态抽查。"""
    global ABBR0
    # 轮 1：→ 华能通辽1区（阿拉伯数字），8号全名直接接入
    rename_round("74①-轮1", SITE3)
    conv = rc.Conv()
    base.flow(conv, access_msg("8号", SITE3, "9008", "9908"),
              done=lambda: writer_by_port(rc.read_config(), 19008)[0] is not None)
    cfg = rc.wait_config(lambda c: writer_by_port(c, 19008)[0] is not None,
                         timeout=240, desc="74: 8号接入")
    assert_writer_prefix("74②", cfg, 19008, "wt8", range(1000, 1010))
    # 轮 1 抽查：缺区号品牌形态（华能通辽风电场）→ 确定性 other 拒绝
    cfg0, aj0 = config_dump(), agent_json_dump()
    text = rc.Conv().send(access_msg("5号", "华能通辽风电场", "9006", "9906"))
    assert_rejected("74⑥-轮1缺区号", text, cfg0, aj0)
    assert_watermark("74⑤", 12)
    # 轮 2：→ 大唐辽宁三区（中文数字），升压站全名直接接入（单台无编号前缀 syz）
    rename_round("74①-轮2", SITE4)
    conv2 = rc.Conv()
    base.flow(conv2, MSG74_SYZ,
              done=lambda: writer_by_port(rc.read_config(), 19009)[0] is not None)
    cfg = rc.wait_config(lambda c: writer_by_port(c, 19009)[0] is not None,
                         timeout=240, desc="74: 升压站接入")
    assert_writer_prefix("74②", cfg, 19009, "syz", range(1100, 1110))
    base.entry("syz")  # 注册表 syz 条目（§3.2.1.3c）
    # 轮 2 抽查：旧名「国电河北II区」拒绝（语义为主，74③）
    text2 = rc.Conv().send(access_msg("9号", SITE2, "9006", "9906"))
    assert_rejected("74③-轮2旧名", text2, config_dump(), agent_json_dump())
    # 轮 2 抽查：去品牌子集泛化（辽宁三区风电场）→ ambiguous 确认后接入（9005/9905）
    conv3 = rc.Conv()
    base.flow(conv3, access_msg("9号", "辽宁三区风电场", "9005", "9905"),
              answers=[(r"归属不明确|请确认", "就是本场站的")],
              done=lambda: writer_by_port(rc.read_config(), 19005)[0] is not None)
    if not re.search(r"归属不明确|请确认", conv_dialog(conv3)):
        raise rc.Fail("74⑥: 去品牌形态未触发 site_ambiguous 确认")
    cfg = rc.wait_config(lambda c: writer_by_port(c, 19005)[0] is not None,
                         timeout=240, desc="74: 确认后 9号接入")
    assert_writer_prefix("74⑥", cfg, 19005, "wt9", range(1000, 1010))
    assert_watermark("74⑤", 16)
    # ④⑤ 缩写与历史设备恒定：agent.json abbr 全程 ABBR0（四次改名原缩写回传）
    if site_of_agent_json() != {"name": SITE4, "abbr": ABBR0}:
        raise rc.Fail(f"74④: site.abbr 漂移: {site_of_agent_json()}")
    for prefix in ("wt1", "wt2", "wt3", "wt4", "wt7", "wt8", "wt9", "syz"):
        base.entry(prefix)  # 历史设备注册表条目原样（改名不搬家累积验证）
    base.assert_no_handle_leak("74")


def s75():
    """75：改名后新增 influx 下游——measurement 推导沿用原缩写 ABBR0（数据不搬家）。"""
    influx_mod.influx_reset_db()  # 链内清库（写入前 hnals 库必须存在）
    conv = rc.Conv()
    base.flow(conv, MSG75,
              answers=[(r"叫什么|名字|称为|下游目标", "计算库")],  # 目标名必答（§2.12.1，43 同款）
              done=lambda: any(k[0] == "c4_influxdb_client"
                               for k in rc.server_instances(rc.read_config() or {})))
    cfg = rc.wait_config(lambda c: any(k[0] == "c4_influxdb_client"
                                       for k in rc.server_instances(c)),
                         timeout=240, desc="75: influx 入库实例落地")
    base.assert_channel_ids(cfg)
    fid = next(iid for (st, iid) in rc.server_instances(cfg) if st == "c4_influxdb_client")
    inst = rc.server_instances(cfg)[("c4_influxdb_client", fid)]
    if inst.get("bucket") != "hnals":
        raise rc.Fail(f"75: bucket={inst.get('bucket')!r} ≠ hnals")
    wid = writer_by_port(cfg, 19001)[0]
    wpts = {p.get("addr"): p for p in (writer_by_port(cfg, 19001)[1].get("points") or [])}
    fpts = inst.get("points") or []
    if len(fpts) != 10:
        raise rc.Fail(f"75: 入库点数 {len(fpts)} ≠ 10")
    for addr in range(1000, 1010):
        ref = f"{wid}.{wpts.get(addr, {}).get('id', '')}"
        fp = next((p for p in fpts if p.get("key") == ref), None)
        if fp is None:
            raise rc.Fail(f"75: 入库引用 {ref} 缺失")
        if fp.get("measurement") != ABBR0:
            raise rc.Fail(f"75: measurement={fp.get('measurement')!r} ≠ 原缩写 {ABBR0!r}"
                          "（不得按当前场站名重新派生）")
        if str(fp.get("type")) != "float":
            raise rc.Fail(f"75: type={fp.get('type')!r} ≠ float")
    assert_watermark("75", 17)
    # ③ 数据面：注入 → measurement=ABBR0 查得 10 字段（by-name——「字段名跟点名
    # 对应」形态，期望字段 = 采集点 key 去前缀，同 influx 链 by_name 口径）
    expect_fields = sorted(str(wpts.get(a, {}).get("id", "")).split("_", 1)[-1]
                           for a in range(1000, 1010))
    rc.inject(19001, 1000, 1010, times=3)
    deadline = time.time() + 60
    got = None
    while time.time() < deadline:
        got = influx_mod.influx_fields_of(ABBR0)
        if got and set(expect_fields) <= set(got):
            break
        time.sleep(2)
    if not got or sorted(got) != expect_fields:
        raise rc.Fail(f"75: 数据面断链——measurement={ABBR0} 查得字段 {got} ≠ {expect_fields}")
    base.assert_no_handle_leak("75")


def s76():
    """76：重启持久化——①改名后重启四态原样；②组绑定落盘（v2.1.53 缺陷回归）。"""
    # ① 改名持久化：重启后 site/配置/注册表/点 key 全原样，引导层不出现
    cfg0, reg0, aj0 = config_dump(), registry_dump(), agent_json_dump()
    pid0 = rc.AGENT.p.pid
    agent_restart()
    if rc.AGENT.p.pid == pid0:
        raise rc.Fail("76①: pid 未变——重启未发生")
    if config_dump() != cfg0 or registry_dump() != reg0 or agent_json_dump() != aj0:
        raise rc.Fail("76①: 重启后文件态漂移")
    assert_site_threefold("76①", SITE4, ABBR0)  # 引导层不出现 ⇔ siteName 非空
    conv = rc.Conv()
    text = conv.send("已接入哪些设备？")
    rc.wait_idle()
    if re.search(r"不属于当前场站", text):
        raise rc.Fail(f"76①③: 重启后对话被归属拒绝: {text[:150]}")
    # ② 组接入首绑落盘：全新环境（无 site）组批量声明 → 询问场站 → 绑定后中断重启
    rc.AGENT.stop()
    base.chain_clean()
    rc.MCP_STACK.up()
    rc.AGENT.up()  # V21Agent：重写无 site 的 agent.json
    if state_site_name() is not None or api_get_site().get("site") is not None:
        raise rc.Fail("76②: 前置错误——重置后 site 应为 null")
    conv2 = rc.Conv()
    text = conv2.send(MSG76_GROUP)
    if not re.search(r"场站名称|绑定场站", text):
        raise rc.Fail(f"76②: 组接入未先询问场站: {text[:150]}")
    text = conv2.send(SITE3)  # 华能通辽1区——group_bind_site 落盘路径
    if site_of_agent_json() is None or api_get_site().get("site") is None:
        raise rc.Fail("76②: 组绑定未即时落盘（GET /api/site 不可读）")
    if (site_of_agent_json() or {}).get("name") != SITE3:
        raise rc.Fail(f"76②: 组绑定名称不符: {site_of_agent_json()}")
    if state_site_name() != SITE3:
        raise rc.Fail(f"76②: 绑定后 siteName={state_site_name()!r} ≠ {SITE3!r}")
    cfg_now = json.loads(config_dump() or "{}")
    n_inst = sum(len(v) for k, v in cfg_now.items()
                 if isinstance(v, list) and k != "c4_shm_manager")
    if n_inst != 0:
        raise rc.Fail(f"76②: 中断未确认但 config 已有 {n_inst} 实例")
    agent_restart()
    assert_site_threefold("76②", SITE3, (site_of_agent_json() or {}).get("abbr", ""))
    rc.log("  76: 重启持久化两场景通过（引导层均不出现——v2.1.53 缺陷已回归验证）")


def s77():
    """77：POST /api/site 边界（链尾）——400 校验 / 200 幂等 / 三级兜底 / 坏 key 422。"""
    aj0 = agent_json_dump()
    # 名称校验 400
    for name in ("国", "含,逗号"):
        status, body = api_post_site(name, "")
        if status != 400:
            raise rc.Fail(f"77: name={name!r} 期望 400 得 {status}: {body}")
    # 缩写校验 400
    status, body = api_post_site(SITE4, "缩写")
    if status != 400:
        raise rc.Fail(f"77: 非法缩写期望 400 得 {status}: {body}")
    if agent_json_dump() != aj0:
        raise rc.Fail("77③: 400 路径落盘了")
    # 罗马数字名 + 显式合法缩写 → 200（ii 为合法 ASCII；统一转小写）
    status, body = api_post_site(SITE2, "II")
    if status != 200 or (body.get("site") or {}).get("abbr") != "ii":
        raise rc.Fail(f"77: II 行期望 200/ii 得 {status}: {body}")
    aj1 = agent_json_dump()
    status, body = api_post_site(SITE2, "II")  # 幂等
    if status != 200 or agent_json_dump() != aj1:
        raise rc.Fail(f"77: 重复绑定非幂等: {status}, {agent_json_dump() != aj1}")
    # 纯中文名 + LLM 可用 → 200（拼音缩写，如 dtln）
    status, body = api_post_site(SITE4, "")
    abbr = str((body.get("site") or {}).get("abbr", ""))
    if status != 200 or not re.fullmatch(r"[a-z0-9]{2,12}", abbr):
        raise rc.Fail(f"77: LLM 缩写行期望 200/[a-z0-9]{{2,12}} 得 {status}: {body}")
    aj1 = agent_json_dump()  # 基线随最后一行合法写入更新（基线过期会误报「启动即改动」）
    # LLM 不可用（坏 key 独立实例）：纯中文名派生必败 → 422 手填
    agent_restart(bad_key=True)
    if agent_json_dump() != aj1:
        raise rc.Fail("77: 坏 key 实例启动即改动 agent.json")
    for name in ("国电", SITE4):
        status, body = api_post_site(name, "")
        if status != 422:
            raise rc.Fail(f"77: LLM 不可用 name={name!r} 期望 422 得 {status}: {body}")
        if agent_json_dump() != aj1:
            raise rc.Fail(f"77③: 422 路径落盘了（name={name!r}）")
    rc.log("  77: API 边界通过（400×3 / 200+幂等 / LLM 可用 200 / 坏 key 422×2，均不落盘）")


# ── 链调度（串行单链，失败即停）────────────────────────────────
CHAIN_TAG = "C4He1"

# 步内按钮预算：71/73/74 各含 3 个独立方案确认（四形态 / 接入+加点+删点 / 两轮改名
# +抽查）——预算=场景数；任一单个方案出现双重确认仍会超限违例
STEP_BUTTON_BUDGET = {"71": 3, "73": 3, "74": 3}

STEPS = [
    ("A.2", SETUP_ONLY, None),
    ("68", ["68"], s68),
    ("69", ["69"], s69),
    ("70", ["70"], s70),
    ("71", ["71"], s71),
    ("72", ["72"], s72),
    ("73", ["73"], s73),
    ("74", ["74"], s74),
    ("75", ["75"], s75),
    ("76", ["76"], s76),
    ("77", ["77"], s77),
]


def main():
    rc.log("════ 场站变更链串行测试开始（68→77，77 置链尾）════")
    influx = influx_mod.Influxd()
    influx.up()
    only = set(sys.argv[1:])
    try:
        rc.log("════ ── S1 场站变更链开始 ── ════")
        base.chain_clean()
        rc.MCP_STACK.up()
        agent = base.V21Agent()
        agent.up()
        rc.AGENT = agent
        influx.up()
        for label, cases, fn in STEPS:
            if only and label not in only and label != "A.2":
                continue
            rc.log(f"════ 链步 {label} 开始 ════")
            rc.PH.reset(f"S1/{label}",
                        button_budget=STEP_BUTTON_BUDGET.get(label, 2))
            t0 = time.time()
            try:
                if fn is not None:
                    fn()
                rc.PH.check(f"S1/{label}")
            except rc.Fail as e:
                base.dump_diag(f"S1_{label}", e)
                rc.log(f"════ 链步 {label} FAIL: {e} ════")
                rc.log(f"════ 场站变更链在第 {label} 步停止 ════")
                sys.exit(1)
            except Exception as e:
                base.dump_diag(f"S1_{label}", e)
                rc.log(f"════ 链步 {label} 异常 {type(e).__name__}: {str(e)[:200]} ════")
                rc.log(f"════ 场站变更链在第 {label} 步停止 ════")
                sys.exit(1)
            PASSED.extend(cases)
            rc.log(f"════ 链步 {label} PASS（{time.time()-t0:.0f}s）════")
        rc.log(f"════ 场站变更链全部通过：{PASSED} ════")
        rc.log(f"════ 标注清单（AI测试通过 {CHAIN_TAG}）：{PASSED} ════")
    finally:
        influx.down()


if __name__ == "__main__":
    main()
