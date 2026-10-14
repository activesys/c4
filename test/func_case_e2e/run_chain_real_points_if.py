#!/usr/bin/env python3
# func_test_case_real_points.md 用例 RP-IF-01~02 驱动器（2026-10-13 初版）
# 断言唯一来源：func_test_case_real_points.md influxdb 组（域规则 5 条 + 附录 C
# 基准，2026-10-13 实测）——按 c4/AGENTS.md 规则 4 实现，不根据 agent 实际输出
# 调整断言；失败即实现未达成。
# 方向：asfp2 接收（c4 作 asfp2 server，文本 9002 经 PORT_MAP → 19002）→
# InfluxDB 入库（c4_influxdb_client；文本 172.16.109.12:8086 → 127.0.0.1:18086）。
# 真实点表 <工作区根>/points/influxdb/AZGF_172.24.1.46_2404.csv（仓库外）：
# 目录/文件缺失整组 SKIP-DIR。InfluxDB 托管/清库/查询复用 run_chain_influx.py
# （本机 1.8.10 @18086，auth 关闭）。
# 数据面：asfp2_client 注入 → c4_influxdb_client flush → /query（v1）验证，
# 属主套件附录 B A 级脚本段同款。
import csv
import os
import re
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_cases as rc  # noqa: E402
import run_chain_v21 as base  # noqa: E402
import run_chain_real_points as rp  # noqa: E402  (upload_big/_n_inst/_post_big)
import run_chain_influx as ifx  # noqa: E402  (Influxd/influx_query/influx_reset_db)

PTS = "/home/wangbo/work/activesys/points/influxdb/AZGF_172.24.1.46_2404.csv"
F = rc.Fail
PORT_REAL = rc.P_RECV2                      # 19002（文本 9002 经 PORT_MAP）
N_POINTS, ADDR_LO, ADDR_HI = 1212, 1000, 2211
N_YX, N_YC = 393, 819
SIX_TAGS = ("plant_id", "project_id", "line_id", "device_id",
            "device_type_id", "type_id")

MSG01 = ("第三方厂家通过 asfp2 协议把数据转给我们，接收端口 9002。"
         "CSV 第 1 列是 asfp2 点地址，第 4 列 jd 是 measurement，tags、"
         "字段名（field）、类型都按 CSV 里的列来，1212 个点全部入库。"
         "数据写入 InfluxDB：写入地址 http://172.16.109.12:8086，"
         "token 是 hnals-influx-2026，org 是 activesys，bucket 是 hnals。")
MSG02 = MSG01.replace("，bucket 是 hnals。", "。")
ANS_BUCKET = "bucket是hnals。"


def load_pts():
    """附录 C 基准：GB18030 解码 CSV，返回 addr→期望映射 dict。
    measurement = 第 4 列 jd（2026-10-13 用户裁定）；第 5 列首 token `device`
    为厂家占位不读，其余 k=v 为 tags。"""
    rows = {}
    with open(PTS, encoding="gb18030") as f:
        for r in csv.reader(f):
            if not r or len(r) < 7:
                continue
            tags = dict(kv.split("=", 1) for kv in r[4].split(",")[1:])
            rows[int(r[0])] = {"measurement": r[3], "field": r[5], "type": r[6],
                               "device_id": tags.get("device_id"),
                               "tags": tags}
    return rows


def _influx_id(cfg):
    for (st, iid) in rc.server_instances(cfg):
        if st == "c4_influxdb_client":
            return iid
    return None


def _influx_points_n(cfg):
    iid = _influx_id(cfg)
    if iid is None:
        return 0
    for (st, i), inst in rc.server_instances(cfg).items():
        if st == "c4_influxdb_client" and i == iid:
            return len(inst.get("points") or [])
    return 0


def flow_land(conv, receipt, answers, tag, max_turns=14):
    """接入口径驱动：上传回执起，答场站/缺口 → 点确认 → config 出现 1212 点
    入库实例即停。解析失败/拒收即 FAIL（本组是接入口径，非拒收口径）。
    返回 (落地前全部回复文本列表, 消耗的应答槽列表)。"""
    answers = [(r"场站", "华能阿拉善")] + list(answers)
    consumed = [False] * len(answers)
    turns = [receipt]
    text = receipt
    clicked = 0
    for _ in range(max_turns):
        if _influx_points_n(rc.read_config()) >= N_POINTS:
            return turns, consumed
        if "解析失败" in text or "取消」结束" in text:
            raise F(f"{tag}: 接入被拒（预期接入落地）: {text[:200]}")
        if clicked < 2 and ("是否确认" in text or "确认执行" in text
                            or "确认后我将" in text
                            or ("方案" in text and "确认" in text)):
            text = conv.send("[C4_BUTTON_CONFIRM] 确认")
            clicked += 1
            turns.append(text)
            rc.wait_idle(timeout=300)
            continue
        hit = False
        for i, (pat, ans) in enumerate(answers):
            if not consumed[i] and re.search(pat, text):
                consumed[i] = True
                text = conv.send(ans)
                turns.append(text)
                hit = True
                break
        if hit:
            continue
        time.sleep(10)
        text = conv.send("继续")
        turns.append(text)
    raise F(f"{tag}: flow 超过 {max_turns} 轮未收敛，尾回复: {text[:200]}")


def assert_mapping(tag, cfg, rows):
    """配置面全量断言：1+1 实例、采集 1212 点 addr 连续、入库逐点映射对照 CSV
    （measurement/field/type/tags 六键/device_id）、类型与台别计数、首中尾抽样。"""
    base.assert_channel_ids(cfg)
    insts = rc.server_instances(cfg)
    n_srv = sum(1 for k in insts if k[0] == "c4_asfp2_server")
    n_infl = sum(1 for k in insts if k[0] == "c4_influxdb_client")
    if (n_srv, n_infl) != (1, 1):
        raise F(f"{tag}: 实例数 srv={n_srv} influx={n_infl} ≠ 1+1（双侧成对）")
    if any(k[0] == "c4_asfp2_client" for k in insts):
        raise F(f"{tag}: 出现 asfp2 转发实例——用户声明的是入库不是外转")
    # asfp2 侧：1212 点 addr 连续（asfp2 点无 type 字段，c4_asfp2_server.md §2.3）
    wid = rc.writer_of(cfg, ADDR_LO)
    wpt = rc.points_of(cfg, "c4_asfp2_server", wid)
    addrs = set(int(a) for a in wpt)
    if addrs != set(range(ADDR_LO, ADDR_HI + 1)):
        raise F(f"{tag}: 采集点 addr 集合不符（{len(wpt)} 点）")
    # influx 侧实例连接参数（文本 172.16.109.12:8086 → 配置 127.0.0.1:18086）
    fid = _influx_id(cfg)
    inst = insts[("c4_influxdb_client", fid)]
    if "127.0.0.1:18086" not in str(inst.get("url") or ""):
        raise F(f"{tag}: 写入地址未采纳: {inst.get('url')}")
    if inst.get("org") != "activesys":
        raise F(f"{tag}: org 未采纳: {inst.get('org')}")
    if inst.get("bucket") != "hnals":
        raise F(f"{tag}: bucket 未采纳: {inst.get('bucket')}")
    # 逐点全量对照 CSV（附录 C）
    fpts = inst.get("points") or []
    if len(fpts) != N_POINTS:
        raise F(f"{tag}: 入库点数 {len(fpts)} ≠ {N_POINTS}")
    by_key = {str(p.get("key")): p for p in fpts}
    type_c, dev_c = Counter(), Counter()
    for addr, exp in rows.items():
        pid = str(wpt.get(addr, {}).get("id") or "")
        if not pid:
            raise F(f"{tag}: 采集 addr={addr} 点缺失")
        fp = by_key.get(f"{wid}.{pid}")
        if fp is None:
            raise F(f"{tag}: 入库引用 key {wid}.{pid} 缺失（addr={addr}）")
        if fp.get("measurement") != exp["measurement"]:
            raise F(f"{tag}: addr={addr} measurement={fp.get('measurement')!r} "
                    f"≠ {exp['measurement']!r}（jd 列裁定口径）")
        if str(fp.get("field")) != exp["field"]:
            raise F(f"{tag}: addr={addr} field={fp.get('field')!r} ≠ {exp['field']!r}"
                    f"（field 原样采纳，2026-10-13 放宽护栏）")
        if str(fp.get("type")) != exp["type"]:
            raise F(f"{tag}: addr={addr} type={fp.get('type')!r} ≠ {exp['type']!r}")
        tags = fp.get("tags") or {}
        for k in SIX_TAGS:
            if k not in tags:
                raise F(f"{tag}: addr={addr} tags 缺键 {k}: {tags}")
        if tags.get("device_id") != exp["device_id"]:
            raise F(f"{tag}: addr={addr} device_id={tags.get('device_id')!r} "
                    f"≠ {exp['device_id']!r}")
        type_c[exp["type"]] += 1
        dev_c[exp["device_id"]] += 1
    if type_c != Counter({"float": N_YC, "bool": N_YX}):
        raise F(f"{tag}: 类型计数 {dict(type_c)} ≠ bool {N_YX} / float {N_YC}")
    if len(dev_c) != 16:
        raise F(f"{tag}: device_id {len(dev_c)} 台 ≠ 16")
    # 首/中/尾抽样（附录 C 基准行）
    for addr, dev, fld, typ in [(1000, "AZGF_XB_014", "yx0001", "bool"),
                                (1393, "AZGF_XB_014", "yc0001", "float"),
                                (2211, "AZGF_HLX_014_012", "yc0054", "float")]:
        pid = str(wpt.get(addr, {}).get("id") or "")
        fp = by_key.get(f"{wid}.{pid}") or {}
        got = (fp.get("field"), (fp.get("tags") or {}).get("device_id"),
               fp.get("type"))
        if got != (fld, dev, typ):
            raise F(f"{tag}: 抽样 addr={addr} 不符: {got} ≠ ({fld}, {dev}, {typ})")
    return wid, fid


def influx_tag_values(field="device_id"):
    data = ifx.influx_query(f'show tag values on hnals with key="{field}"')
    vals = set()
    for s in (data.get("results") or [{}])[0].get("series") or []:
        for row in s.get("values", []):
            vals.add(row[-1])
    return vals


def influx_series_count(field):
    """按 device_id 分组的 series 数（跨装置同名 field 的区分断言）。"""
    data = ifx.influx_query(f"select count({field}) from jd group by device_id")
    return len((data.get("results") or [{}])[0].get("series") or [])


def influx_first_value(field, device_id):
    data = ifx.influx_query(
        f"select {field} from jd where device_id='{device_id}' limit 1")
    for s in (data.get("results") or [{}])[0].get("series") or []:
        for row in s.get("values", []):
            return row[1]
    return None


def wait_data(deadline_s, check):
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        try:
            if check():
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def rpif01(influx):
    """RP-IF-01：全链路主例（配置面全量对照 + 数据面 16 台 series）。"""
    tag = "RP-IF-01"
    rows = load_pts()
    conv = rc.Conv()
    receipt, _ = rp.upload_big(conv.conversation_id, PTS, rc.map_ports(MSG01))
    flow_land(conv, receipt,
              [(r"这台设备叫|设备名称|设备名",
                rp.DEVICE_ANSWER.format(name="AZGF光伏14区"))], tag)
    cfg = rc.wait_config(lambda c: _influx_points_n(c) >= N_POINTS,
                         timeout=900, desc="RP-IF-01: 1212 点入库映射落地")
    assert_mapping(tag, cfg, rows)
    if base.registry().get("channelHighWatermark") != 2:
        raise F(f"{tag}: 水位={base.registry().get('channelHighWatermark')} ≠ 2")
    # 数据面：全段注入 → flush → /query
    rc.inject(PORT_REAL, ADDR_LO, ADDR_HI, times=3)
    if not wait_data(90, lambda: len(influx_tag_values("device_id")) == 16):
        raise F(f"{tag}: 数据面 device_id 不足 16 台")
    if not wait_data(60, lambda: influx_series_count("yc0001") == 16):
        raise F(f"{tag}: 跨装置同名 field yc0001 series ≠ 16（tag 区分失败）")
    if not wait_data(60, lambda: influx_series_count("yx0001") == 16):
        raise F(f"{tag}: 跨装置同名 field yx0001 series ≠ 16")
    v_bool = influx_first_value("yx0001", "AZGF_XB_014")
    if not isinstance(v_bool, bool):
        raise F(f"{tag}: yx0001 值非 bool 形态: {v_bool!r}")
    v_float = influx_first_value("yc0001", "AZGF_XB_014")
    if not isinstance(v_float, (int, float)) or isinstance(v_float, bool):
        raise F(f"{tag}: yc0001 值非 float 形态: {v_float!r}")
    base.assert_no_handle_leak(tag)
    rc.log(f"  RP-IF-01 PASS（{N_POINTS} 点双侧映射全量对照；数据面 16 台 "
           f"series，bool/float 值形态正确）")


def rpif02(influx):
    """RP-IF-02：缺 bucket 追问后接入；field 由 CSV 承载、无 field 追问。"""
    tag = "RP-IF-02"
    ifx.influx_reset_db()          # 回零——防 IF-01 残留 series 干扰抽样断言
    rows = load_pts()
    conv = rc.Conv()
    receipt, _ = rp.upload_big(conv.conversation_id, PTS, rc.map_ports(MSG02))
    turns, consumed = flow_land(
        conv, receipt,
        [(r"这台设备叫|设备名称|设备名",
          rp.DEVICE_ANSWER.format(name="AZGF光伏14区")),
         (r"bucket", ANS_BUCKET)], tag)
    if not consumed[1]:
        raise F(f"{tag}: bucket 追问未出现（必填不猜）——应答槽未消耗")
    pre = " ".join(turns[:-1])
    if "bucket" not in pre:
        raise F(f"{tag}: 追问文案未见 bucket: {pre[-300:]}")
    if re.search(r"(字段名|field).{0,24}(？|\?)", pre):
        raise F(f"{tag}: 出现 field/字段名类追问（CSV 已承载 field）: {pre[-300:]}")
    cfg = rc.wait_config(lambda c: _influx_points_n(c) >= N_POINTS,
                         timeout=900, desc="RP-IF-02: 补 bucket 后 1212 点落地")
    assert_mapping(tag, cfg, rows)
    # 数据面抽样：注入前 11 点（#14箱变 yx 段）→ 任一 field 可查
    rc.inject(PORT_REAL, ADDR_LO, ADDR_LO + 10, times=3)
    if not wait_data(60, lambda: influx_first_value("yx0001", "AZGF_XB_014")
                     is not None):
        raise F(f"{tag}: 数据面断链——补 bucket 后 yx0001 不可查")
    base.assert_no_handle_leak(tag)
    rc.log(f"  RP-IF-02 PASS（bucket 追问→补齐落地；field 零追问；数据面抽样可查）")


CASES = [
    ("RP-IF-01", rpif01),
    ("RP-IF-02", rpif02),
]


def main():
    only = sys.argv[1].split(",") if len(sys.argv) > 1 else None
    if not os.path.isfile(PTS):
        rc.log("════ RP-IF 全组 SKIP-DIR：真实点表不存在 ════")
        return
    rc.log("════ RP-IF influxdb 组真实点表接入串行测试开始 ════")
    rc._post = rp._post_big       # 1212 行提取回合超时放宽
    influx = ifx.Influxd()
    influx.up()
    ifx.influx_reset_db()
    base.chain_clean()
    rc.MCP_STACK.up()
    agent = base.V21Agent()
    agent.up()
    rc.AGENT = agent
    rc.PH.reset("RP-IF")
    t0 = time.time()
    passed, failed = [], []
    for name, fn in CASES:
        if only and name not in only:
            continue
        try:
            rc.log(f"── {name} 开始 ──")
            base.chain_clean()
            rc.MCP_STACK.up()
            agent = base.V21Agent()
            agent.up()
            rc.AGENT = agent
            rc.PH.reset(name, rc.BUTTON_BUDGET.get(name, 2))
            fn(influx)
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
    influx.down()
    rc.log(f"════ RP-IF 套件结束：PASS={passed} FAIL={failed}"
           f"（{time.time() - t0:.0f}s）════")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
