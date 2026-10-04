#!/usr/bin/env python3
# 用例 46 自检（func_test_case.md §用例 46；func_case_e2e/README.md §4 过程断言）。
# 46 的主体实现 = ProcessHealth.check() 接入全部链段套件 PASS 路径（数据断言 ∧ 过程断言）。
# 本脚本对 verdict 判定做**负向自检**：人工喂违例序列，证明 #1~#4 真能抓到——
# 防止闸门「接了但永远空转」。纯内存单测，秒级，可进 CI。
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_cases import ProcessHealth  # noqa: E402


def expect_violation(name, ph, rule):
    vio, _ = ph.verdict()
    if not vio:
        raise SystemExit(f"════ 用例 46 FAIL: {name} 未被抓到（verdict 为空）════")
    if not any(rule in v for v in vio):
        raise SystemExit(f"════ 用例 46 FAIL: {name} 抓到但规则不符: {vio} ════")
    print(f"  ✓ {name} → {vio[0]}")


def expect_clean(name, ph):
    vio, _ = ph.verdict()
    if vio:
        raise SystemExit(f"════ 用例 46 FAIL: {name} 误报: {vio} ════")
    print(f"  ✓ {name} → 无违例（正确放行）")


def main():
    print("════ 用例 46 过程断言负向自检开始 ════")

    # #1 按钮预算：3 次按钮确认 > 预算 2
    ph = ProcessHealth(); ph.reset("t1", button_budget=2)
    for _ in range(3):
        ph.user("[C4_BUTTON_CONFIRM] 确认")
    expect_violation("#1 按钮超预算", ph, "#1")

    # #1 反例：预算内不违例
    ph = ProcessHealth(); ph.reset("t1b", button_budget=2)
    ph.user("[C4_BUTTON_CONFIRM] 确认")
    ph.assistant("接入已完成！")
    expect_clean("#1 预算内", ph)

    # #2 假成功：「执行完成 ✅」之后出现 error 事件
    ph = ProcessHealth(); ph.reset("t2")
    ph.assistant("执行完成 ✅ 配置已写入并启动")
    ph.event("error")
    expect_violation("#2 成功后 error", ph, "#2")

    # #2 反例：error 在成功之前（失败后汇报）不违例
    ph = ProcessHealth(); ph.reset("t2b")
    ph.event("error")
    ph.assistant("执行失败，本次变更已恢复原样")
    expect_clean("#2 error在成功前", ph)

    # #3 空转：同一回合连续 2 轮空文本且无工具
    ph = ProcessHealth(); ph.reset("t3")
    ph.assistant("", tools=set())
    ph.assistant("…", tools=set())
    expect_violation("#3 连续空转", ph, "#3")

    # #3 反例：空转一轮后用户接话（回合重置）不违例
    ph = ProcessHealth(); ph.reset("t3b")
    ph.assistant("", tools=set())
    ph.user("继续")
    ph.assistant("", tools=set())
    expect_clean("#3 隔回合重置", ph)

    # #4 自问自答：问询后同回合（无用户消息间隔）调用 output_access_plan
    ph = ProcessHealth(); ph.reset("t4")
    ph.assistant("请问监听端口是多少？")
    ph.assistant("已生成方案", tools={"output_access_plan"})
    expect_violation("#4 问询后同回合出方案", ph, "#4")

    # #4 反例：方案确认句式不视作问询（agent.md §2.4.2 排除清单）
    ph = ProcessHealth(); ph.reset("t4b")
    ph.assistant("方案如下，是否确认执行？")
    ph.assistant("方案已生成", tools={"output_access_plan"})
    expect_clean("#4 确认句式排除", ph)

    # #4 反例：问询后用户应答再出方案（跨回合）不违例
    ph = ProcessHealth(); ph.reset("t4c")
    ph.assistant("请问监听端口是多少？")
    ph.user("9001")
    ph.assistant("方案已生成", tools={"output_access_plan"})
    expect_clean("#4 跨回合放行", ph)

    print("════ 用例 46 自检 PASS（#1~#4 全部具备抓违例能力）════")


if __name__ == "__main__":
    main()
