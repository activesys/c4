#!/usr/bin/env python3
# func_test_case.md 用例 7 驱动器——文件上传点表接入（2026-10-04 按现行架构改写）
# 历史口径（ReAct 时代：轮次级闸门复位/谎报执行完成）已随机制退役删除；
# 现行验收：① 上传轮解析可见（parse_file_table 确定性列映射 + LLM <file_data>
# 兜底通道，不依赖模型文本输出）；② 会话连续——客户端 conversationId 全程复用
#（web.md §3.1.2），上传轮与文本轮同会话（原用例 12 的行为级验收点并入本链段）；
# ③ 必要项仍逐项询问（端口/转发，无默认值原则）；④ 方案确认执行成功 + 数据面 smoke。
# 依赖：驱动器 /api/upload 客户端（run_cases.upload_file / Conv.upload，
# multipart + X-Conversation-Id 回传校验）；点表文件 test/func_case_e2e/points/。
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_cases as rc  # noqa: E402
import run_chain_v21 as base  # noqa: E402

POINTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "points")

# 后续文本轮（map_ports 于发送时改写为隔离端口）：补充端口与转发必要项
MSG7B = ("监听9001端口，我们需要将这些数据转发到II区服务器上，转发采用asfp2协议，"
         "目标地址是127.0.0.1:9900，点表5000~5009。")

NAMES10 = ["风速", "功率", "风向", "桨叶角度", "发电机转速",
           "齿轮箱油温", "塔筒温度", "空气温度", "空气湿度", "大气压强"]


def s7():
    """用例 7：上传 points/1#风机点表.txt → 解析可见 → 同会话补充 → 确认执行 → 数据面。"""
    fwd = rc.start_receiver(rc.P_FWD1)
    try:
        conv = rc.Conv()
        # ① 上传轮：multipart 上传 + 解析回执可见（文件名不乱码、点表被识别）
        text = conv.upload(os.path.join(POINTS_DIR, "1#风机点表.txt"))
        if not re.search(r"风速|点表|10\s*个点|设备", text):
            raise rc.Fail(f"7①: 上传轮无解析回执信号: {text[:200]}")
        # ② 文本轮（同会话）补齐必要项——上传轮解析的点表不得丢失（10 点全量出方案）
        base.flow(
            conv, MSG7B,
            answers=[(r"场站", "华能阿拉善"), (r"这台设备叫", "1号风机")],
            done=lambda: rc.writer_of(rc.read_config(), 1000) is not None)
        cfg = rc.wait_config(
            lambda c: rc.writer_of(c, 1000) is not None
            and rc.forward_of(c, 5000) is not None,
            timeout=240, desc="7: 上传接入完成态（10 点 + 转发实例）")
        base.assert_channel_ids(cfg)
        # ③ config 断言：10 点 wt1_ 前缀、addr 1000~1009、点名与文件一致、转发成对
        w, wid = base.assert_writer_keys(cfg, "wt1", range(1000, 1010))
        for p in w.values():
            if p.get("name") not in NAMES10:
                raise rc.Fail(f"7③: 点名 {p.get('name')!r} 与文件点表不符")
        base.assert_forward_key(cfg, 5000, wid, 1000)
        e = base.entry("wt1")
        if not e or e.get("host") != wid:
            raise rc.Fail(f"7③: 注册表 wt1 条目缺失或宿主不符: {e}")
        # ④ 数据面：采集 1000~1009 → 转发 5000~5009（19900 接收端）
        rc.wait_port(rc.P_RECV1, True)
        base.data_smoke(fwd, rc.P_RECV1, 1000, 1010)
        base.assert_no_handle_leak("7")
    finally:
        fwd.stop()


def main():
    rc.log("════ 用例 7 文件上传接入串行测试开始 ════")
    base.chain_clean()
    rc.MCP_STACK.up()
    agent = base.V21Agent()
    agent.up()
    rc.AGENT = agent
    rc.PH.reset("7")
    t0 = time.time()
    try:
        base.chain_clean()
        rc.MCP_STACK.up()
        agent = base.V21Agent()
        agent.up()
        rc.AGENT = agent
        s7()
        rc.PH.check("7")
    except rc.Fail as e:
        base.dump_diag("7", e)
        rc.log(f"════ 链段 7 FAIL: {e} ════")
        sys.exit(1)
    except Exception as e:
        base.dump_diag("7", e)
        rc.log(f"════ 链段 7 异常 {type(e).__name__}: {str(e)[:400]} ════")
        sys.exit(1)
    rc.log(f"════ 链段 7 PASS（{time.time()-t0:.0f}s）════")
    rc.log("════ 用例 7 全部通过：['7'] ════")
    rc.log("════ 标注清单（AI测试通过 C4He1）：['7'] ════")


if __name__ == "__main__":
    main()
