"""
C4 Web 契约一致性测试 — test_contract_web.py

锁定 web.md 声明的后端契约，防止后端变更破坏前端依赖（web.md §1.2「以代码为准」落点）。

测试依据: c4/test/web/README.md §6（契约用例 6.1–6.6）

设计原则:
  - 黑盒契约断言：只断言 HTTP 响应结构、SSE 事件流，不侵入后端内部状态。
  - 确认/取消经 POST /api/chat 发送**关键词**（web.md §3.1.3），
    不使用 agent 测试方案的 interrupt 模型 ChatHelper.confirm()。
  - LLM 驱动用例用 @pytest.mark.llm 标记，无 DEEPSEEK_API_KEY 时自动 skip。
"""

import json
import re
from pathlib import Path
from typing import Any, Optional

import pytest  # type: ignore

# ──────────────────────────────────────────────
#  web.md §3.1.3 关键词正则（契约镜像常量）
# ──────────────────────────────────────────────
# 后端 C4Agent 内两套独立正则（super_worker.ts）：
#   确认正则 /确认|好的|执行|按方案|开始/
#   拒绝正则 /取消|拒绝|放弃|停止|算了|不执行|不要执行|不确认/
# 注意「反向防误判」：拒绝词优先判断——「不执行」「不要执行」含「执行」，
# 「不确认」含「确认」，若仅靠确认正则会被误判为确认。

CONFIRM_RE = re.compile(r"确认|好的|执行|按方案|开始")
REJECT_RE = re.compile(r"取消|拒绝|放弃|停止|算了|不执行|不要执行|不确认")

CONFIRM_KEYWORDS = ["确认", "好的", "执行", "按方案", "开始"]
REJECT_KEYWORDS = ["取消", "拒绝", "放弃", "停止", "算了", "不执行", "不要执行", "不确认"]


# ──────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────


def _payload(evt: Any) -> Optional[dict]:
    """解析 SSE data 载荷为 dict；非 JSON 返回 None。"""
    try:
        data = json.loads(evt.data)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _conversation_ids(events: list) -> list[str]:
    """收集所有事件 payload 中的 conversationId 字段。"""
    ids: list[str] = []
    for evt in events:
        payload = _payload(evt)
        if payload is not None and "conversationId" in payload:
            ids.append(payload["conversationId"])
    return ids


def _write_parseable_txt(tmp_path: Path) -> str:
    """写一个可解析的 .txt 点表文件，返回其路径。"""
    txt = tmp_path / "device_points.txt"
    txt.write_text(
        "设备名称,寄存器地址,数据类型\n1#风机,40001,uint16\n",
        encoding="utf-8",
    )
    return str(txt)


# ──────────────────────────────────────────────
#  §6.1  tool_call.args 恒为 {}
# ──────────────────────────────────────────────


@pytest.mark.llm
def test_tool_call_args_always_empty(agent: Any, tmp_path: Path) -> None:
    """
    6.1: 上传可解析 .txt 触发 txt_parser 工具调用；tool_call 事件 args 恒为 {}。
    web.md §3.1.1 契约：工具卡片只展示 name 与进度，不展示 args。
    """
    txt = _write_parseable_txt(tmp_path)
    with agent.upload(txt, "请解析此文件中的设备信息") as stream:
        events = list(stream)

    tool_calls = [
        p for e in events if (p := _payload(e)) is not None and p.get("type") == "tool_call"
    ]
    assert tool_calls, (
        "上传 .txt 应触发 txt_parser 工具调用（tool_call 事件）；"
        f"实际事件类型: {[e.type for e in events]}"
    )
    for p in tool_calls:
        assert p.get("args") == {}, f"tool_call.args 应恒为 {{}}，实际: {p.get('args')!r}"


# ──────────────────────────────────────────────
#  §6.2  upload 事件不带 conversationId
# ──────────────────────────────────────────────


@pytest.mark.llm
def test_upload_events_lack_conversation_id(agent: Any, tmp_path: Path) -> None:
    """
    6.2: 上传响应的事件对象无 conversationId 字段。
    web.md §3.2.1：upload 的 SSE 事件（text/tool_call/tool_result/done/error）
    均不带 conversationId。
    """
    txt = _write_parseable_txt(tmp_path)
    with agent.upload(txt, "请解析此文件中的设备信息") as stream:
        events = list(stream)

    assert events, "上传应产生 SSE 事件"
    for evt in events:
        payload = _payload(evt)
        if payload is not None:
            assert "conversationId" not in payload, (
                f"upload 事件不应带 conversationId，实际 payload: {payload}"
            )


# ──────────────────────────────────────────────
#  §6.3  不产出 interrupt 事件
# ──────────────────────────────────────────────


@pytest.mark.llm
def test_no_interrupt_event(agent: Any, tmp_path: Path) -> None:
    """
    6.3: 完整对话/上传流中，SSE 事件不出现 interrupt。
    web.md §1.3：后端声明 interrupt 事件但从不产出；确认仅关键词驱动。
    """
    txt = _write_parseable_txt(tmp_path)
    with agent.upload(txt, "请解析此文件中的设备信息") as stream:
        upload_events = list(stream)

    with agent.chat("你好") as stream:
        chat_events = list(stream)

    all_events = upload_events + chat_events
    assert all_events, "对话/上传应产生 SSE 事件"
    for evt in all_events:
        assert evt.type != "interrupt", (
            f"后端不应产出 interrupt 事件，实际事件类型: {evt.type}"
        )


# ──────────────────────────────────────────────
#  §6.4  早退分支返回文本后无 done
# ──────────────────────────────────────────────


def test_early_exit_branches_stream_without_done(agent: Any) -> None:
    """
    6.4: 「请提供场站名称…」与「已记录场站…」早退分支返回文本后流直接关闭、无 done。
    web.md §4.2 流关闭兜底：前端须以 ReadableStream 关闭为终止信号，不等待 done。

    两分支均确定性触发（site 未设置时走 super_worker 的早退路径，不依赖 LLM）：
      - 消息含「华能/风电场/电场/场站」→ 「请提供场站名称和缩写…」（不固化 site）
      - 消息含「场站名称：…，缩写：…」→ 「已记录场站：…」（固化 site）
    """
    # 分支 1：请提供场站名称…（先触发，不固化 site，保证分支 2 仍可触发）
    with agent.chat("接入华能阿拉善1#风机") as stream:
        events_1 = list(stream)
        text_1 = stream.text_content()

    assert "请提供场站名称" in text_1, (
        f"应命中「请提供场站名称…」早退分支，实际回复: {text_1[:200]!r}"
    )
    assert any((p := _payload(e)) is not None and p.get("type") == "text" for e in events_1), (
        "早退分支应产出 text 事件"
    )
    assert all(e.type != "done" for e in events_1), (
        f"早退分支不应产出 done 事件，实际事件类型: {[e.type for e in events_1]}"
    )

    # 分支 2：已记录场站…
    with agent.chat("场站名称：华能阿拉善，缩写：hnals") as stream:
        events_2 = list(stream)
        text_2 = stream.text_content()

    assert "已记录场站" in text_2, (
        f"应命中「已记录场站…」早退分支，实际回复: {text_2[:200]!r}"
    )
    assert any((p := _payload(e)) is not None and p.get("type") == "text" for e in events_2), (
        "早退分支应产出 text 事件"
    )
    assert all(e.type != "done" for e in events_2), (
        f"早退分支不应产出 done 事件，实际事件类型: {[e.type for e in events_2]}"
    )


# ──────────────────────────────────────────────
#  §6.5  conversationId 仅回显
# ──────────────────────────────────────────────


@pytest.mark.llm
def test_conversation_id_echo_only(agent: Any) -> None:
    """
    6.5: 不同 conversationId 的请求不产生服务端会话隔离副作用（纯回显）。
    web.md §3.1.2：后端把 conversationId 写入 X-Conversation-Id 响应头与每个事件
    的 conversationId 字段，但不参与任何服务端状态管理。
    """
    # 第一次对话 conversationId = "conv-aaa"
    with agent.chat("你好", conversation_id="conv-aaa") as stream:
        events_a = list(stream)
        header_a = stream.get_header("X-Conversation-Id")

    assert header_a == "conv-aaa", (
        f"X-Conversation-Id 应回显 conv-aaa，实际: {header_a!r}"
    )
    ids_a = _conversation_ids(events_a)
    assert ids_a, "对话事件应携带 conversationId 回显"
    assert all(c == "conv-aaa" for c in ids_a), f"事件 conversationId 应回显 conv-aaa，实际: {ids_a}"

    # 第二次对话 conversationId = "conv-bbb"（不得被第一次污染）
    with agent.chat("你好", conversation_id="conv-bbb") as stream:
        events_b = list(stream)
        header_b = stream.get_header("X-Conversation-Id")

    assert header_b == "conv-bbb", (
        f"X-Conversation-Id 应回显 conv-bbb，实际: {header_b!r}"
    )
    ids_b = _conversation_ids(events_b)
    assert ids_b, "对话事件应携带 conversationId 回显"
    assert all(c == "conv-bbb" for c in ids_b), f"事件 conversationId 应回显 conv-bbb，实际: {ids_b}"


# ──────────────────────────────────────────────
#  §6.6  确认/拒绝关键词正则
# ──────────────────────────────────────────────


def test_confirm_reject_keyword_regexes() -> None:
    """
    6.6: 确认/拒绝关键词正则与实际行为一致。
    web.md §3.1.3：确认正则 /确认|好的|执行|按方案|开始/、
    拒绝正则 /取消|拒绝|放弃|停止|算了|不执行|不要执行|不确认/。

    断言：
      1. 确认关键词集合全部命中确认正则；
      2. 拒绝关键词集合全部命中拒绝正则；
      3. 中性文本两者都不命中；
      4. 「反向防误判」：含确认子串的拒绝词（不执行/不要执行/不确认）仍由拒绝正则捕获。
    """
    for kw in CONFIRM_KEYWORDS:
        assert CONFIRM_RE.search(kw), f"确认关键词 {kw!r} 应命中确认正则"
    for kw in REJECT_KEYWORDS:
        assert REJECT_RE.search(kw), f"拒绝关键词 {kw!r} 应命中拒绝正则"

    neutral = ["你好", "请解析此文件中的设备信息", "今天天气怎么样"]
    for text in neutral:
        assert not CONFIRM_RE.search(text), f"中性文本 {text!r} 不应命中确认正则"
        assert not REJECT_RE.search(text), f"中性文本 {text!r} 不应命中拒绝正则"

    # 反向防误判：这些拒绝词含确认子串（执行/确认），必须靠拒绝正则优先捕获
    for kw in ["不执行", "不要执行", "不确认"]:
        assert REJECT_RE.search(kw), f"反向防误判：{kw!r} 应命中拒绝正则"
