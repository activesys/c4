"""
C4 Agent L2 功能测试 — 非技术语言约束
======================================

测试依据: c4/test/agent/README.md §4.7

§4.7 非技术语言约束 (SuperWorker 系统提示):
  4.7.1  正常对话不含技术术语 — 能力介绍场景，协议名豁免
  4.7.2  方案展示含通俗解释 — 方案展示场景，协议名+端口号豁免
  4.7.3  错误场景不暴露内部信息 — 无豁免
  4.7.4  全程不展示 JSON 结构 — JSON 泄漏检测
"""

import json
import os
from pathlib import Path

import pytest  # type: ignore

from test_helpers import (
    create_full_csv,
    create_test_csv,
    create_corrupted_xlsx,
    create_binary_file,
    retry_llm,
    run_upload,
    run_chat,
)
from assertions import (
    STRICT_BLACKLIST,
    CONTEXTUAL_BLACKLIST,
    JSON_LEAK_PATTERNS,
    assert_no_technical_terms,
    assert_no_json_leak,
)


# ══════════════════════════════════════════════
#  §4.7 非技术语言约束
# ══════════════════════════════════════════════


@pytest.mark.llm
class TestNonTechnicalLanguage:
    """§4.7 非技术语言约束 — 黑名单检查"""

    def test_intro_no_strict_blacklist(self, chat, agent):
        """4.7.1: "介绍一下你能做什么" → 不含无例外黑名单术语"""
        with chat.send("你好，介绍一下你能做什么") as stream:
            text = stream.text_content()

        assert len(text) > 0, "Intro response should not be empty"
        # 能力介绍场景：协议名豁免 (allow_protocols=True)
        assert_no_technical_terms(text, allow_protocols=True)

    @retry_llm(max_attempts=3)
    def test_plan_display_no_strict_blacklist(self, chat, agent, tmp_path):
        """4.7.2: 方案展示 → 含设备名+描述，无无例外黑名单"""
        csv_path = create_full_csv(tmp_path)

        # 上传点表
        with chat.send_with_file("接入华能阿拉善1#风机", str(csv_path)) as stream:
            upload_text = stream.text_content()
        assert len(upload_text) > 0

        # 生成方案
        with chat.send("生成接入方案，并转发到中心侧") as stream:
            plan_text = stream.text_content()

        assert len(plan_text) > 0, "Plan text should not be empty"

        # 方案展示场景：协议名+端口号豁免
        assert_no_technical_terms(plan_text, allow_protocols=True, allow_ports=True)

        # 方案应包含设备名
        has_device = "华能阿拉善" in plan_text or "风机" in plan_text
        assert has_device, (
            f"Plan should mention the device name. Got: {plan_text[:500]}"
        )

    @retry_llm(max_attempts=3)
    def test_error_message_no_blacklist(self, chat, agent, tmp_path):
        """4.7.3: 错误场景（损坏文件）→ 错误消息不含任何黑名单术语"""
        corrupt_path = create_corrupted_xlsx(tmp_path)

        with chat.send_with_file("请解析这个文件", str(corrupt_path)) as stream:
            text = stream.text_content()

        assert len(text) > 0, "Error response should not be empty"

        # 错误场景：无豁免。协议名在黑名单中（错误场景无豁免）
        assert_no_technical_terms(text, allow_protocols=False, allow_ports=False)

    @retry_llm(max_attempts=3)
    def test_error_binary_file_no_blacklist(self, chat, agent, tmp_path):
        """4.7.3 扩展: 二进制文件 → 错误消息不含黑名单"""
        bin_path = create_binary_file(tmp_path)

        with chat.send_with_file("请解析", str(bin_path)) as stream:
            text = stream.text_content()

        assert len(text) > 0
        assert_no_technical_terms(text, allow_protocols=False)

    def test_full_conversation_no_json_leak(self, chat, agent, tmp_path):
        """4.7.4: 全程对话不展示 JSON 结构"""
        csv_path = create_full_csv(tmp_path)

        # 收集所有对话文本
        all_texts: list[str] = []

        # 1. 能力介绍
        with chat.send("介绍一下你能做什么") as stream:
            text1 = stream.text_content()
            all_texts.append(text1)
        assert len(text1) > 0, "Intro should have reply"

        # 2. 上传点表
        with chat.send_with_file("接入华能阿拉善1#风机", str(csv_path)) as stream:
            text2 = stream.text_content()
            all_texts.append(text2)
        assert len(text2) > 0

        # 3. 生成方案
        with chat.send("生成接入方案") as stream:
            text3 = stream.text_content()
            all_texts.append(text3)
        assert len(text3) > 0, "Plan generation should produce text"

        # 合并验证
        combined = "\n".join(all_texts)
        assert_no_json_leak(combined)

    def test_no_json_leak_in_error(self, chat, agent, tmp_path):
        """4.7.4 扩展: 错误场景也不泄漏 JSON"""
        corrupt_path = create_corrupted_xlsx(tmp_path)

        with chat.send_with_file("解析这个文件", str(corrupt_path)) as stream:
            text = stream.text_content()

        assert_no_json_leak(text)

    def test_no_json_leak_in_greeting(self, chat, agent):
        """4.7.4 扩展: 问候回复不泄漏 JSON"""
        with chat.send("你好") as stream:
            text = stream.text_content()

        assert_no_json_leak(text)
