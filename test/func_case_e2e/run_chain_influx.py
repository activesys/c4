#!/usr/bin/env python3
# func_test_case.md influxdb 组串行驱动器（v2.1.0 命名体系）
# 三链各自独立回零（链首 A.2 = 全新环境，场站询问答「华能阿拉善」）：
#   D1: 38（入库信息齐全接入 + 数据面查询）→ 41（同采集点双 measurement 映射，可读拒绝）
#   D2: 39（缺 bucket，询问后接入）
#   D3: 40（注入停 InfluxDB——start 成功不谎报、恢复后续写；用例内含恢复后验证）
# InfluxDB server：本机 1.8.10（/home/wangbo/backup/influxdb），18086（auth 关闭——
# 鉴权形态不在被测范围，c4_influxdb_client 的 Token 头在 auth-off 下被忽略）；
# /api/v2/write 兼容端点写入、/query（v1）查询；db=hnals 由链首 A.2 自建（influx_reset_db）。
# influxd 生命周期由本驱动器托管（环境 Bash 会话结束会回收后台进程）。
# 数据面（38⑤/40③）：asfp2_client 注入 → c4_influxdb_client flush → /query 验证，
# 属附录 B A 级脚本段。
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_cases as rc  # noqa: E402
import run_chain_v21 as base  # noqa: E402

P_RECV1 = rc.P_RECV1
PASSED = []
SETUP_ONLY = {"A.2"}

INFLUXD = "/home/wangbo/backup/influxdb/influxdb-1.8.10-1/usr/bin/influxd"
INFLUX_CONF = "/tmp/influxdb_test/influxdb.conf"
INFLUX_URL = "http://127.0.0.1:18086"

MSG38 = ("现在需要接入1号风机的数据并直接入库。第三方厂家通过asfp2协议给我们转来1#风机数据，"
         "10个点，从1000到1009，分别是1000:风速、1001:功率、1002:风向、1003:桨叶角度、"
         "1004:发电机转速、1005:齿轮箱油温、1006:塔筒温度、1007:空气温度、1008:空气湿度、"
         "1009:大气压强，使用端口9001。数据写入我们的InfluxDB时序库：写入地址http://127.0.0.1:8086，"
         "token是hnals-influx-2026，org是activesys，bucket是hnals。10个点全部写进wind_turbine"
         "这个measurement，字段名跟点名对应（windspeed、power、wind_dir、pitch_angle、gen_speed、"
         "gearbox_oil_temp、tower_temp、air_temp、humidity、pressure），类型统一float。")
MSG39 = MSG38.replace(
    "数据写入我们的InfluxDB时序库：写入地址http://127.0.0.1:8086，token是hnals-influx-2026，"
    "org是activesys，bucket是hnals。",
    "数据写入InfluxDB：写入地址http://127.0.0.1:8086，token是hnals-influx-2026，org是activesys。",
).replace("wind_turbine这个measurement，字段名跟点名对应（windspeed、power、wind_dir、pitch_angle、gen_speed、gearbox_oil_temp、tower_temp、air_temp、humidity、pressure）",
          "wind_turbine，字段名跟点名对应")
ANS39 = "bucket是hnals。"
# 2026-10-09 起「字段名跟点名对应」不再自动映射（点名已中文化，field 仅允许
# [a-zA-Z_]+、不推导不翻译）——Agent 追问后须显式给出逐点 field 名
ANS39_FIELDS = ("字段名跟点名对应（windspeed、power、wind_dir、pitch_angle、gen_speed、"
                "gearbox_oil_temp、tower_temp、air_temp、humidity、pressure）。")
MSG43 = ("这10个点都写一份到另一个bucket：url同，还是http://127.0.0.1:8086，"
         "token是hnals-influx-2026，org是activesys，bucket换成wind_history，"
         "measurement和字段名跟wind_turbine那边一样，类型统一float。")
MSG41 = ("给1号风机的入库再加一条：风速除了wind_turbine，也同步写一份到wind_anomaly这个measurement，"
         "字段也叫windspeed，类型float。")

FIELDS38 = ["windspeed", "power", "wind_dir", "pitch_angle", "gen_speed",
            "gearbox_oil_temp", "tower_temp", "air_temp", "humidity", "pressure"]


# ── InfluxDB 托管 ─────────────────────────────────────────
class Influxd:
    def __init__(self):
        self.p = None

    @staticmethod
    def kill_orphans():
        """清扫孤儿 influxd（上一驱动进程被强杀时遗留、仍占 18086）。"""
        subprocess.run(["pkill", "-f", "influxd -config /tmp/influxdb_test"],
                       capture_output=True)
        time.sleep(1)

    def up(self, wait=True):
        self.kill_orphans()
        if self.p is None or self.p.poll() is not None:
            # 自举：目录与最小配置缺失时补建（/tmp 清理后驱动器仍可独立运行；
            # auth 关闭——鉴权形态不在被测范围，见文件头注）
            os.makedirs("/tmp/influxdb_test", exist_ok=True)
            if not os.path.exists(INFLUX_CONF):
                with open(INFLUX_CONF, "w", encoding="utf-8") as f:
                    f.write('[meta]\n  dir = "/tmp/influxdb_test/meta"\n'
                            '[data]\n  dir = "/tmp/influxdb_test/data"\n'
                            '  wal-dir = "/tmp/influxdb_test/wal"\n'
                            '[http]\n  enabled = true\n  bind-address = ":18086"\n')
            self.p = subprocess.Popen(
                [INFLUXD, "-config", INFLUX_CONF],
                stdout=open("/tmp/influxdb_test/influxd_driver.log", "ab"),
                stderr=subprocess.STDOUT)
        if wait:
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(INFLUX_URL + "/ping", timeout=2) as r:
                        if r.status in (200, 204):
                            return
                except Exception:
                    time.sleep(0.5)
            raise rc.Fail("influxd 20s 未就绪（18086 /ping）")

    def down(self):
        if self.p is not None and self.p.poll() is None:
            self.p.send_signal(signal.SIGTERM)
            try:
                self.p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.p.kill()
        self.p = None
        self.kill_orphans()
        # 端口释放确认（40 注入「不可达」即 down 后立即可用）
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                urllib.request.urlopen(INFLUX_URL + "/ping", timeout=1)
                time.sleep(0.5)
            except Exception:
                return
        raise rc.Fail("influxd down 后 18086 仍可达")


def influx_query(q):
    url = INFLUX_URL + "/query?db=hnals&q=" + urllib.parse.quote(q)
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def influx_fields_of(measurement):
    data = influx_query(f'select * from "{measurement}" limit 1')
    series = (data.get("results") or [{}])[0].get("series") or []
    if not series:
        return None
    return [c for c in series[0].get("columns", []) if c not in ("time",)]


# ── 断言助手 ───────────────────────────────────────────────
def assert_influx_pair(tag, cfg):
    """38/39 通用：双侧成对（asfp2_server 9001 + influxdb 入库实例，无 asfp2_client）、
    逐点 field/measurement/type 原样、引用 key = {采集实例id}.{点key}。"""
    base.assert_channel_ids(cfg)
    if any(k[0] == "c4_asfp2_client" for k in rc.server_instances(cfg)):
        raise rc.Fail(f"{tag}: 出现 asfp2 转发实例——用户声明的是入库不是外转")
    n_srv = sum(1 for k in rc.server_instances(cfg) if k[0] == "c4_asfp2_server")
    n_infl = sum(1 for k in rc.server_instances(cfg) if k[0] == "c4_influxdb_client")
    if (n_srv, n_infl) != (1, 1):
        raise rc.Fail(f"{tag}: 实例数 srv={n_srv} influx={n_infl} ≠ 1+1（双侧成对）")
    wid = rc.writer_of(cfg, 1000)
    wpt = rc.points_of(cfg, "c4_asfp2_server", wid)
    fid = None
    for (st, iid), inst in rc.server_instances(cfg).items():
        if st == "c4_influxdb_client":
            fid = iid
    if fid is None:
        raise rc.Fail(f"{tag}: 无入库实例")
    # influx 点无 addr——points_of 以 field 为键；断言用 key 维度
    fpts = {}
    for p in next(inst["points"] for (st, iid), inst in rc.server_instances(cfg).items()
                  if st == "c4_influxdb_client" and iid == fid):
        fpts[str(p.get("key"))] = p
    if len(fpts) != 10:
        raise rc.Fail(f"{tag}: 入库点数 {len(fpts)} ≠ 10")
    for addr in range(1000, 1010):
        pid = str(wpt.get(addr, {}).get("id", ""))
        if not pid:
            raise rc.Fail(f"{tag}: 采集 addr={addr} 点缺失")
        fp = fpts.get(f"{wid}.{pid}")
        if fp is None:
            raise rc.Fail(f"{tag}: 入库引用 key {wid}.{pid} 缺失（addr={addr}）")
        if fp.get("measurement") != "wind_turbine":
            raise rc.Fail(f"{tag}: addr={addr} measurement={fp.get('measurement')} ≠ wind_turbine")
        if str(fp.get("type")) != "float":
            raise rc.Fail(f"{tag}: addr={addr} type={fp.get('type')} ≠ float")
    fields = sorted(str(fp.get("field")) for fp in fpts.values())
    if fields != sorted(FIELDS38):
        raise rc.Fail(f"{tag}: field 映射不符（原样采纳）: {fields}")
    return wid, fid


# ── 链步实现 ───────────────────────────────────────────────
def s38(influx):
    conv = rc.Conv()
    base.flow(conv, MSG38, answers=[(r"场站", "华能阿拉善")],
              done=lambda: cfg_has_influx())
    cfg = rc.wait_config(lambda c: sum(1 for k in rc.server_instances(c)
                                       if k[0] == "c4_influxdb_client") >= 1,
                         timeout=240, desc="38: 入库实例落地")
    assert_influx_pair("38", cfg)
    if base.registry().get("channelHighWatermark") != 2:
        raise rc.Fail(f"38: 水位={base.registry().get('channelHighWatermark')} ≠ 2")
    # ⑤ 数据面：注入 → flush → InfluxDB 查询可见 10 字段
    rc.inject(P_RECV1, 1000, 1010, times=3)
    deadline = time.time() + 40
    got = None
    while time.time() < deadline:
        got = influx_fields_of("wind_turbine")
        if got and set(FIELDS38) <= set(got):
            break
        time.sleep(2)
    if not got or not set(FIELDS38) <= set(got):
        raise rc.Fail(f"38: 数据面断链——wind_turbine 查询字段 {got} 不含全部 10 field")
    base.assert_no_handle_leak("38")


def s41(influx):
    """同采集点双 measurement 映射：可读拒绝、config 不变。"""
    before = rc.read_config()
    conv = rc.Conv()
    text = conv.send(MSG41)
    time.sleep(6)
    after = rc.read_config()
    if json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True):
        raise rc.Fail("41: 冲突请求写入了 config")
    if re.search(r"是否确认|确认执行", text):
        raise rc.Fail(f"41: 冲突请求进入可确认方案: {text}")
    if not re.search(r"重复|已.{0,6}引用|无法|不能|不支持|同一", text):
        raise rc.Fail(f"41: 无可读拒绝信号: {text}")
    base.assert_no_handle_leak("41")


def s39(influx):
    conv = rc.Conv()
    base.flow(conv, MSG39,
              answers=[(r"场站", "华能阿拉善"), (r"bucket", ANS39),
                       (r"field", ANS39_FIELDS)],
              done=lambda: cfg_has_influx())
    cfg = rc.wait_config(lambda c: sum(1 for k in rc.server_instances(c)
                                       if k[0] == "c4_influxdb_client") >= 1,
                         timeout=240, desc="39: 补 bucket 后接入")
    assert_influx_pair("39", cfg)
    if base.registry().get("channelHighWatermark") != 2:
        raise rc.Fail(f"39: 水位={base.registry().get('channelHighWatermark')} ≠ 2")
    base.assert_no_handle_leak("39")


def s40(influx):
    """停 InfluxDB → 接入成功不谎报 → 重试期丢弃 → 恢复后续写。"""
    influx.down()
    conv = rc.Conv()
    base.flow(conv, MSG38, answers=[(r"场站", "华能阿拉善")],
              done=lambda: cfg_has_influx())
    cfg = rc.wait_config(lambda c: sum(1 for k in rc.server_instances(c)
                                       if k[0] == "c4_influxdb_client") >= 1,
                         timeout=240, desc="40: influxd 不可达下接入完成")
    assert_influx_pair("40", cfg)
    # ① 不谎报已入库：接入完成回复只描述配置层面
    dialog = "".join(base._assistant_texts())
    if re.search(r"已入库|已写入|入库成功|写入成功|数据已.{0,4}库", dialog):
        raise rc.Fail(f"40: 谎报数据已入库: "
                      f"{[m for m in re.findall(r'.{15}(?:已入库|已写入).{15}', dialog)][:1]}")
    # ② 不可达期注入（批次重试耗尽丢弃，管道不阻塞）
    rc.inject(P_RECV1, 1000, 1010, times=2)
    time.sleep(3)
    # ③ 恢复 → 新数据续写可见
    influx.up()
    rc.inject(P_RECV1, 1000, 1010, times=3)
    deadline = time.time() + 40
    got = None
    while time.time() < deadline:
        got = influx_fields_of("wind_turbine")
        if got and set(FIELDS38) <= set(got):
            break
        time.sleep(2)
    if not got or not set(FIELDS38) <= set(got):
        raise rc.Fail(f"40: 恢复后续写断链——wind_turbine 查询字段 {got}")
    base.assert_no_handle_leak("40")


def s43(influx):
    """43: 入口 B 第二实例共享读——目标名必答、跨实例同 key、既有实例不变。"""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", 18086, timeout=10)
    # 表单体必须显式 Content-Type——缺省时 Go 不解析 body，静默 400「missing q」
    conn.request("POST", "/query", urlencode({"q": "CREATE DATABASE wind_history"}),
                 {"Content-Type": "application/x-www-form-urlencoded"})
    conn.getresponse().read()
    conn.close()

    before = rc.read_config()
    before_old = next((i for i in rc.server_instances(before or {}).values()
                       if isinstance(i, dict) and i.get("bucket") == "hnals"), None)
    conv = rc.Conv()
    # ① 目标名必答（「另一个bucket」不构成目标名——§2.12.1）→ 答「历史库」
    base.flow(conv, MSG43, answers=[(r"叫什么|名字|称为", "历史库")],
              done=lambda: sum(1 for k in rc.server_instances(rc.read_config() or {})
                               if k[0] == "c4_influxdb_client") >= 2)
    asked = any(re.search(r"叫什么|名字|称为", h.get("content", ""))
                for h in conv.history if h.get("role") == "assistant")
    if not asked:
        raise rc.Fail("43①: 目标名未必答（消息仅描述「另一个bucket」）")
    cfg = rc.wait_config(lambda c: sum(1 for k in rc.server_instances(c)
                                       if k[0] == "c4_influxdb_client") >= 2,
                         timeout=240, desc="43: 第二实例落地")
    insts = [i for (st, _i), i in rc.server_instances(cfg).items()
             if st == "c4_influxdb_client"]
    hist = next((i for i in insts if i.get("bucket") == "wind_history"), None)
    old = next((i for i in insts if i.get("bucket") == "hnals"), None)
    if hist is None or old is None:
        raise rc.Fail(f"43②: 实例形态不符: {[i.get('bucket') for i in insts]}")
    # ③ 目标名采纳「历史库」（bucket 名不构成目标名——假失败即此处）
    reg = base.registry()
    if not any(e.get("prefix") == "" and e.get("name") == "历史库"
               for e in reg.get("entries", [])):
        raise rc.Fail(f"43③: 注册表缺「历史库」目标条目: {reg.get('entries')}")
    # ④ 跨实例共享读：两实例引用相同采集 key（10 点）
    keys_old = sorted(str(p.get("key")) for p in old.get("points", []))
    keys_new = sorted(str(p.get("key")) for p in hist.get("points", []))
    if keys_old != keys_new or len(keys_new) != 10:
        raise rc.Fail(f"43④: 跨实例引用不一致: {len(keys_old)} vs {len(keys_new)}")
    # ⑥ 既有实例不受影响
    if json.dumps(old, sort_keys=True) != json.dumps(before_old, sort_keys=True):
        raise rc.Fail("43⑥: 既有 hnals 实例被改动")
    # ⑤ 数据面：注入 → hnals 与 wind_history 两库均可见（measurement=wind_turbine）
    rc.inject(P_RECV1, 1000, 1010, times=3)
    deadline = time.time() + 40
    got_h = got_w = None
    while time.time() < deadline:
        got_h = influx_fields_of("wind_turbine")
        url = (INFLUX_URL + "/query?db=wind_history&q="
               + urllib.parse.quote('select * from "wind_turbine" limit 1'))
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
            series = (data.get("results") or [{}])[0].get("series") or []
            got_w = [c for c in (series[0].get("columns", []) if series else [])
                     if c != "time"] or None
        except Exception:
            got_w = None
        if (got_h and got_w and set(FIELDS38) <= set(got_h)
                and set(FIELDS38) <= set(got_w)):
            break
        time.sleep(2)
    if not (got_h and got_w and set(FIELDS38) <= set(got_h)
            and set(FIELDS38) <= set(got_w)):
        raise rc.Fail(f"43⑤: 数据面断链 hnals={got_h} wind_history={got_w}")
    # ⑦ 负向：修改下游澄清拒绝，config 不变
    t7 = conv.send("把历史库的bucket改成stats2。")
    if not re.search(r"不支持|无法|暂不|澄清", t7):
        raise rc.Fail(f"43⑦: 修改下游未被澄清拒绝: {t7[:200]}")
    time.sleep(3)
    if json.dumps(rc.read_config(), sort_keys=True) != json.dumps(cfg, sort_keys=True):
        raise rc.Fail("43⑦: 拒绝路径 config 被改动")
    base.assert_no_handle_leak("43")


def influx_reset_db():
    """链首清库（D2/D3 回零的一部分——D1 写入的 series 会干扰数据面断言）。"""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", 18086, timeout=10)
    # 表单体必须显式 Content-Type——缺省时 Go 不解析 body，静默 400「missing q」
    conn.request("POST", "/query", urlencode({"q": "DROP DATABASE hnals"}),
                 {"Content-Type": "application/x-www-form-urlencoded"})
    conn.getresponse().read()
    conn.request("POST", "/query", urlencode({"q": "CREATE DATABASE hnals"}),
                 {"Content-Type": "application/x-www-form-urlencoded"})
    conn.getresponse().read()
    conn.close()


from urllib.parse import urlencode  # noqa: E402


def cfg_has_influx():
    return sum(1 for k in rc.server_instances(rc.read_config() or {})
               if k[0] == "c4_influxdb_client") >= 1


# ── 链调度（三链串行，链间回零，失败即停）────────────────────
CHAIN_TAG = "C4He1"
CHAIN_FAIL_DIR = base.CHAIN_FAIL_DIR

CHAINS = [
    ("D1", [("A.2", SETUP_ONLY, lambda influx: influx_reset_db()), ("38", ["38"], s38), ("41", ["41"], s41)]),
    ("D2", [("A.2", SETUP_ONLY, lambda influx: influx_reset_db()), ("39", ["39"], s39)]),
    ("D3", [("A.2", SETUP_ONLY, lambda influx: influx_reset_db()), ("40", ["40"], s40)]),
    ("D4", [("A.2", SETUP_ONLY, lambda influx: influx_reset_db()), ("38", ["38"], s38),
            ("43", ["43"], s43)]),
]


def main():
    rc.log("════ influxdb 组串行测试开始（D1: 38→41 / D2: 39 / D3: 40）════")
    influx = Influxd()
    influx.up()
    only = set(sys.argv[1:])
    try:
        for chain_name, steps in CHAINS:
            if only and chain_name not in only:
                continue
            rc.log(f"════ ── {chain_name} 链开始 ── ════")
            base.chain_clean()
            rc.MCP_STACK.up()
            agent = base.V21Agent()
            agent.up()
            rc.AGENT = agent
            influx.up()
            for label, cases, fn in steps:
                rc.log(f"════ 链步 {label} 开始 ════")
                rc.PH.reset(f"{chain_name}/{label}")
                t0 = time.time()
                try:
                    fn(influx)
                    rc.PH.check(f"{chain_name}/{label}")
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
        rc.log(f"════ influxdb 组全部通过：{PASSED} ════")
        rc.log(f"════ 标注清单（AI测试通过 {CHAIN_TAG}）：{PASSED} ════")
    finally:
        influx.down()


if __name__ == "__main__":
    main()
