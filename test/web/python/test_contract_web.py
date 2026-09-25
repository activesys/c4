"""
C4 Web 契约一致性测试 — test_contract_web.py

锁定 web.md 声明的后端契约，防止后端变更破坏前端依赖（web.md §1.2「以代码为准」落点）。

测试依据: c4/test/web/README.md §6（契约用例 6.1–6.6）

设计原则:
  - 黑盒契约断言：只断言 HTTP 响应结构、SSE 事件流，不侵入后端内部状态。
  - 确认/取消经 POST /api/chat 发送**关键词**（web.md §3.1.3），
    不使用 agent 测试方案的 interrupt 模型 ChatHelper.confirm()。
  - LLM 驱动用例用 @pytest.mark.llm 标记，无 ZHIPU_API_KEY 时自动 skip。
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
#  §6.1  上传解析为确定性步骤（无 tool_call 卡片事件）
# ──────────────────────────────────────────────


@pytest.mark.llm
def test_upload_parses_without_tool_call(agent: Any, tmp_path: Path) -> None:
    """
    6.1: 上传可解析 .txt → 确定性解析并流式返回结果文本；流中不含 tool_call 事件。
    web.md §3.2.1/§3.2.2（2026-09-23 更新）：九阶段流水线将文件解析收敛为上传
    接口的确定性步骤（解析文本经 <file_data> 注入提取层），不再产出解析工具的
    tool_call/tool_result 卡片事件。
    """
    txt = _write_parseable_txt(tmp_path)
    with agent.upload(txt, "请解析此文件中的设备信息") as stream:
        events = list(stream)
        text = stream.text_content()

    assert events, "上传应产生 SSE 事件"
    assert text.strip(), (
        f"上传解析应产出解析结果文本，实际为空。事件类型: {[e.type for e in events]}"
    )
    tool_calls = [
        p for e in events if (p := _payload(e)) is not None and p.get("type") == "tool_call"
    ]
    assert not tool_calls, (
        "上传解析不应产出 tool_call 事件（确定性解析，无工具卡片）；"
        f"实际: {[p.get('name') for p in tool_calls]}"
    )


# ──────────────────────────────────────────────
#  §6.2  upload 的 done 事件携带 conversationId
# ──────────────────────────────────────────────


@pytest.mark.llm
def test_upload_done_carries_conversation_id(agent: Any, tmp_path: Path) -> None:
    """
    6.2: 上传 SSE 的 done 事件携带 conversationId（其余事件不带）。
    web.md §3.2.1/§3.2.2：upload 接收/回传 conversationId
    （X-Conversation-Id 头 + done 事件），用于上传轮与后续对话轮的会话关联。
    """
    txt = _write_parseable_txt(tmp_path)
    with agent.upload(txt, "请解析此文件中的设备信息") as stream:
        events = list(stream)
        header = stream.get_header("X-Conversation-Id")

    assert events, "上传应产生 SSE 事件"
    done_events = [e for e in events if e.type == "done"]
    assert done_events, (
        f"上传流应以 done 事件收尾，实际事件类型: {[e.type for e in events]}"
    )
    done_payloads = [p for e in done_events if (p := _payload(e)) is not None]
    assert done_payloads, "done 事件应有 JSON 载荷"
    for p in done_payloads:
        assert p.get("conversationId"), f"done 事件应携带 conversationId，实际: {p!r}"
    if header:
        assert done_payloads[0].get("conversationId") == header, (
            f"done.conversationId 应与 X-Conversation-Id 头一致: {done_payloads[0]!r} vs {header!r}"
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
#  §6.4  缺口聚合提问回合以 done 收尾
# ──────────────────────────────────────────────


def test_site_gap_question_round_completes_with_done(agent: Any) -> None:
    """
    6.4: site 未绑定时首条接入消息 → 缺口聚合提问（含场站名称与缩写缺口），
    回合以 done 事件正常收尾。
    web.md §4.2（2026-09-23 更新）：旧「请提供场站名称…」早退无 done 分支已被
    缺口驱动流水线取代——提问即终局，events 以 done 收尾；前端仍保留流关闭兜底。
    分支 1 为确定性渲染（缺口聚合，不依赖 LLM）。
    """
    with agent.chat("接入华能阿拉善1#风机") as stream:
        events_1 = list(stream)
        text_1 = stream.text_content()

    # 聚合提问文本：指出场站名称与缩写缺口
    assert "场站" in text_1, (
        f"聚合提问应包含场站名称与缩写缺口，实际回复: {text_1[:200]!r}"
    )
    assert any((p := _payload(e)) is not None and p.get("type") == "text" for e in events_1), (
        "聚合提问应产出 text 事件"
    )
    assert any(e.type == "done" for e in events_1), (
        f"缺口提问回合应以 done 收尾，实际事件类型: {[e.type for e in events_1]}"
    )

    # 分支 2：绑定场站 → 正常回合收尾（LLM 提取，宽松断言）
    with agent.chat("场站名称：华能阿拉善，缩写：hnals") as stream:
        events_2 = list(stream)
        text_2 = stream.text_content()

    assert "场站" in text_2, (
        f"场站绑定回复应提及场站，实际回复: {text_2[:200]!r}"
    )
    assert any((p := _payload(e)) is not None and p.get("type") == "text" for e in events_2), (
        "场站绑定回合应产出 text 事件"
    )
    assert any(e.type == "done" for e in events_2), (
        f"场站绑定回合应以 done 收尾，实际事件类型: {[e.type for e in events_2]}"
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
