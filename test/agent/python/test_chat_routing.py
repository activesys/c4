"""
C4 Agent L2 功能测试 — 对话路由 & 文档解析
============================================

测试依据: c4/test/agent/README.md §4.2, §4.3

§4.2 对话路由 (SuperWorker 意图识别与子代理调度):
  4.2.1  上传文档触发 info-gatherer — 解析结果出现在对话文本中
  4.2.2  查询类消息不触发子代理 — 无结构化设备枚举
  4.2.3  问候类消息直接回答 — SSE 正常关闭，有 assistant 回复
  4.2.4  空消息处理 — 不崩溃，返回引导性回复

§4.3 文档解析 (info-gatherer 子代理):
  4.3.1  解析合法 xlsx 点表
  4.3.2  解析合法 csv 点表
  4.3.3  上传不支持的文件格式
  4.3.4  上传损坏的 xlsx
  4.3.5  点表缺少关键字段
  4.3.6  协议：从点表字段唯一推断 (uid/fun/type/swap → Modbus，不询问)
  4.3.7  协议：从用户描述推断 (多协议歧义点表 + "采集 Modbus 设备"，不询问)
  4.3.8  协议：前两层无法确定 → 询问 (歧义点表 + 未提协议，主动询问)
  4.3.9  协议：Reader（转发协议）推断 ("转发到上级系统" → ASFP2，不询问)
"""

import json
import os
from pathlib import Path

import pytest  # type: ignore

# 同目录模块导入
from test_helpers import (
    create_test_csv,
    create_test_xlsx,
    create_full_csv,
    create_full_xlsx,
    create_csv_missing_ip,
    create_missing_ip_xlsx,
    create_binary_file,
    create_text_file,
    create_corrupted_xlsx,
    create_device_txt,
    create_ambiguous_csv,
    retry_llm,
)
from assertions import assert_no_technical_terms


# ══════════════════════════════════════════════
#  §4.2 对话路由
# ══════════════════════════════════════════════


@pytest.mark.llm
class TestChatRouting:
    """§4.2 对话路由 — SuperWorker 意图识别与子代理调度"""

    @retry_llm(max_attempts=3)
    def test_info_gatherer_triggered_by_upload_xlsx(
        self, chat, agent, tmp_path
    ):
        """4.2.1: 上传 xlsx 点表 + "接入华能阿拉善1#风机" → 解析结果含设备信息"""
        csv_path = create_test_csv(tmp_path)

        with chat.send_with_file("接入华能阿拉善1#风机", str(csv_path)) as stream:
            text = stream.text_content()

        # 可观察副作用：文本非空，非"无法解析"，含设备相关信息
        assert len(text) > 0, "Response text should not be empty"
        assert "无法解析" not in text, (
            f"info-gatherer should return parsed content, got: {text[:300]}"
        )
        # 点表中的数据应出现在文本中
        keywords = ["windspeed", "1000", "temperature"]
        found = any(kw.lower() in text.lower() for kw in keywords)
        assert found, (
            f"Expected parsed device/point info in response, got: {text[:500]}"
        )

    @retry_llm(max_attempts=3)
    def test_query_message_no_subagent(self, chat, agent):
        """4.2.2: 查询 "现在有哪些设备在运行" → 不触发 info-gatherer 子代理"""
        with chat.send("现在有哪些设备在运行") as stream:
            text = stream.text_content()

        assert len(text) > 0, "Response should not be empty"

        # 不应出现结构化设备枚举（info-gatherer 输出特征）
        structured_indicators = [
            "设备名", "协议", "数据点", "寄存器", "Modbus",
        ]
        # 注意："Modbus" 在能力介绍中可能豁免，但作为结构化枚举不应出现
        # 这里检查不出现密集的结构化术语组合
        indicator_count = sum(
            1 for ind in structured_indicators if ind in text
        )
        assert indicator_count <= 2, (
            f"Query response should NOT contain structured device enumeration, "
            f"found {indicator_count}/5 indicators. Text: {text[:500]}"
        )

    def test_greeting_reply(self, chat, agent):
        """4.2.3: "你好" → SSE 正常关闭，无 error，有 assistant 回复"""
        with chat.send("你好") as stream:
            text = stream.text_content()

        assert len(text) > 0, "Greeting should get a reply"
        # SSE 流正常关闭（如果 stream 上下文管理器正常退出，说明 HTTP 200）

    def test_empty_message_no_crash(self, chat, agent):
        """4.2.4: 空消息 → 不崩溃，返回引导性回复"""
        with chat.send("") as stream:
            text = stream.text_content()

        # Agent 不崩溃（响应正常返回）
        assert len(text) > 0, (
            f"Empty message should get guidance reply, got empty text"
        )


# ══════════════════════════════════════════════
#  §4.3 文档解析 (info-gatherer)
# ══════════════════════════════════════════════


@pytest.mark.llm
class TestInfoGatherer:
    """§4.3 文档解析 — info-gatherer 子代理"""

    @retry_llm(max_attempts=3)
    def test_parse_valid_xlsx(self, chat, agent, tmp_path):
        """4.3.1: 上传合法 xlsx 点表 → 解析结果含设备名、协议、数据点列表"""
        xlsx_path = create_full_xlsx(tmp_path)

        with chat.send_with_file("请解析这个点表文件", str(xlsx_path)) as stream:
            text = stream.text_content()

        assert len(text) > 0, "Response should not be empty"
        assert "华能阿拉善" in text or "风机" in text, (
            f"Expected device name in info-gatherer result, got: {text[:500]}"
        )
        # README §4.3.1: 收集结果含设备名、协议、数据点列表 — 点表数据应出现在文本中
        point_signal = any(
            kw in text for kw in ("windspeed", "temperature", "1000", "1002")
        )
        assert point_signal, (
            f"Expected parsed point info in info-gatherer result, got: {text[:500]}"
        )
        assert_no_technical_terms(text, allow_protocols=True)

    @retry_llm(max_attempts=3)
    def test_parse_valid_csv(self, chat, agent, tmp_path):
        """4.3.2: 上传合法 csv 点表 → 同 4.3.1"""
        csv_path = create_full_csv(tmp_path, filename="test_points.csv")

        with chat.send_with_file("请解析这个点表文件", str(csv_path)) as stream:
            text = stream.text_content()

        assert len(text) > 0, "Response should not be empty"
        assert "华能阿拉善" in text or "风机" in text, (
            f"Expected device name in info-gatherer result, got: {text[:500]}"
        )

    @retry_llm(max_attempts=3)
    def test_upload_unsupported_format(self, chat, agent, tmp_path):
        """4.3.3: 上传不支持的格式 → Agent 给出友好提示，不崩溃"""
        txt_path = create_text_file(tmp_path, filename="notes.dat")

        with chat.send_with_file("请解析这个文件", str(txt_path)) as stream:
            text = stream.text_content()

        assert len(text) > 0, "Response should not be empty"
        # 不应出现解析成功标记（文档实际不是点表）
        # 友好错误不应含技术术语
        assert_no_technical_terms(text, allow_protocols=False)

    @retry_llm(max_attempts=3)
    def test_upload_binary_file(self, chat, agent, tmp_path):
        """4.3.3: 上传二进制文件 → 友好提示，不崩溃"""
        bin_path = create_binary_file(tmp_path)

        with chat.send_with_file("请解析这个文件", str(bin_path)) as stream:
            text = stream.text_content()

        assert len(text) > 0, "Response should not be empty"
        assert_no_technical_terms(text, allow_protocols=False)

    @retry_llm(max_attempts=3)
    def test_upload_corrupted_xlsx(self, chat, agent, tmp_path):
        """4.3.4: 上传损坏的 xlsx → 友好提示，不崩溃"""
        corrupt_path = create_corrupted_xlsx(tmp_path)

        with chat.send_with_file("请解析这个文件", str(corrupt_path)) as stream:
            text = stream.text_content()

        assert len(text) > 0, "Response should not be empty"
        # 错误场景：不暴露技术黑名单术语
        assert_no_technical_terms(text, allow_protocols=False)

    @retry_llm(max_attempts=3)
    def test_upload_missing_key_fields_csv(self, chat, agent, tmp_path):
        """4.3.5: 上传缺少 IP 字段的点表 → Agent 列出已有 + 指出缺失"""
        csv_path = create_csv_missing_ip(tmp_path)

        with chat.send_with_file("请解析这个点表文件", str(csv_path)) as stream:
            text = stream.text_content()

        assert len(text) > 0, "Response should not be empty"

        # 应列出已有信息（设备名、采集点）
        has_device = "华能阿拉善" in text or "风机" in text or "windspeed" in text
        assert has_device, (
            f"Should list existing info (device name/points). Got: {text[:500]}"
        )
        # 应明确指出缺失字段或要求补充
        missing_signal = (
            "缺少" in text or "缺失" in text or "补充" in text or
            "ip" in text.lower() or "还需要" in text or "不完整" in text or
            "missing" in text.lower()
        )
        assert missing_signal, (
            f"Should point out missing fields. Got: {text[:500]}"
        )

    @retry_llm(max_attempts=3)
    def test_upload_missing_key_fields_xlsx(self, chat, agent, tmp_path):
        """4.3.5: 上传缺少 IP 字段的 xlsx 点表"""
        xlsx_path = create_missing_ip_xlsx(tmp_path)

        with chat.send_with_file("请解析这个点表文件", str(xlsx_path)) as stream:
            text = stream.text_content()

        assert len(text) > 0, "Response should not be empty"
        # 应有缺失字段提示
        missing_signal = (
            "缺少" in text or "缺失" in text or "补充" in text or
            "还需要" in text or "不完整" in text or
            "missing" in text.lower() or "ip" in text.lower()
        )
        assert missing_signal, (
            f"Should point out missing fields. Got: {text[:500]}"
        )

    @retry_llm(max_attempts=3)
    def test_parse_text_description(self, chat, agent):
        """纯文字描述接入设备 → 提取设备名、协议、数据点信息"""
        description = (
            "请帮我接入华能阿拉善1#风机，"
            "通信方式是 Modbus TCP，设备 IP 是 192.168.110.1，端口 502，"
            "需要采集 5 个数据点：windspeed 地址 1000、"
            "temperature 地址 1002、power 地址 1004、"
            "pressure 地址 1006、vibration 地址 1008。"
            "这些点都要转发到中心侧。"
        )

        with chat.send(description) as stream:
            text = stream.text_content()

        assert len(text) > 0, "Response should not be empty"
        assert "华能阿拉善" in text or "风机" in text or "windspeed" in text.lower(), (
            f"Should recognize device name. Got: {text[:500]}"
        )
        assert_no_technical_terms(text, allow_protocols=True, allow_ports=True)

    @retry_llm(max_attempts=3)
    def test_parse_txt_file(self, chat, agent, tmp_path):
        """上传 txt 格式设备描述文件 → 提取设备信息"""
        txt_path = create_device_txt(tmp_path)

        with chat.send_with_file("请解析这个设备描述文件", str(txt_path)) as stream:
            text = stream.text_content()

        assert len(text) > 0, "Response should not be empty"
        assert "华能阿拉善" in text or "风机" in text or "windspeed" in text.lower(), (
            f"Should recognize device from txt. Got: {text[:500]}"
        )
        assert_no_technical_terms(text, allow_protocols=True)


# ══════════════════════════════════════════════
#  §4.3.6-4.3.9 协议推断 (info-gatherer)
# ══════════════════════════════════════════════


@pytest.mark.llm
class TestProtocolInference:
    """§4.3 协议推断三层（agent.md §3.2）— 推断成功不打断用户，无法确定才询问

    断言策略（README §4.3 注）：
      协议推断是 LLM 行为，不做精确文本比对。用「是否询问协议」
      这一可观察副作用断言：询问类关键词出现即视为打断了用户。
    """

    # 询问协议时的典型措辞（推断成功时这些信号不应出现）
    PROTOCOL_ASK_SIGNALS = [
        "什么协议", "哪种协议", "哪个协议", "何种协议",
        "通信协议是", "如何通信", "怎么通信", "用哪种方式采集",
        "which protocol", "what protocol",
    ]

    @retry_llm(max_attempts=3)
    def test_protocol_infer_from_fields(self, chat, agent, tmp_path):
        """4.3.6: 点表含 uid/fun/type/swap（仅 Modbus 匹配）→ 确定 Modbus，不询问"""
        csv_path = create_test_csv(tmp_path)

        with chat.send_with_file("接入这个设备", str(csv_path)) as stream:
            text = stream.text_content()

        assert len(text) > 0, "Response should not be empty"

        # 推断成功 → 不打断用户（协议不单独询问）
        asked = [kw for kw in self.PROTOCOL_ASK_SIGNALS if kw in text]
        assert not asked, (
            f"Should NOT ask protocol when inferable from point table fields. "
            f"Matched ask signals: {asked}. Got: {text[:500]}"
        )

        # 解析应继续进行（点表数据被识别，而非卡在协议询问）
        has_point_info = (
            "windspeed" in text.lower()
            or "1000" in text
            or "temperature" in text.lower()
            or "风机" in text
            or "点" in text
        )
        assert has_point_info, (
            f"Info-gatherer should proceed parsing the point table. Got: {text[:500]}"
        )
        # 协议名可作为解析结果出现在摘要中（README §4.3 注：不单独确认即可）

    @retry_llm(max_attempts=3)
    def test_protocol_infer_from_user_description(self, chat, agent, tmp_path):
        """4.3.7: 歧义点表 + 用户说"采集 Modbus 设备" → 确定 Modbus，不询问"""
        csv_path = create_ambiguous_csv(tmp_path)

        with chat.send_with_file(
            "请解析这个点表，我们是要采集 Modbus 设备的数据", str(csv_path)
        ) as stream:
            text = stream.text_content()

        assert len(text) > 0, "Response should not be empty"

        asked = [kw for kw in self.PROTOCOL_ASK_SIGNALS if kw in text]
        assert not asked, (
            f"Should NOT ask protocol when inferable from user description. "
            f"Matched ask signals: {asked}. Got: {text[:500]}"
        )

        # 收集继续进行（设备/数据点信息被理解）
        has_info = "windspeed" in text.lower() or "点" in text or "风机" in text
        assert has_info, (
            f"Info-gatherer should continue collecting after protocol inference. "
            f"Got: {text[:500]}"
        )

    @retry_llm(max_attempts=3)
    def test_protocol_unknown_asks_user(self, chat, agent, tmp_path):
        """4.3.8: 歧义点表 + 未提协议 → Agent 主动询问协议（非技术语言）"""
        csv_path = create_ambiguous_csv(tmp_path)

        with chat.send_with_file("接入这个设备", str(csv_path)) as stream:
            text = stream.text_content()

        assert len(text) > 0, "Response should not be empty"

        # 主动询问协议（问句信号，用自然语言表达即可）
        ask_signals = [
            "协议", "通信方式", "怎么通信", "哪种方式", "如何采集",
            "请告知", "请提供", "告诉我", "是什么",
        ]
        asked = any(kw in text for kw in ask_signals)
        assert asked, (
            f"Agent should proactively ask the protocol in plain language. "
            f"Got: {text[:500]}"
        )

        # 未确认协议前不应进入方案确认流程
        assert "确认方案" not in text, (
            f"Should NOT enter plan confirmation before protocol is known. "
            f"Got: {text[:500]}"
        )

    @retry_llm(max_attempts=3)
    def test_reader_protocol_inferred_from_forwarding_target(
        self, chat, agent, tmp_path
    ):
        """4.3.9: 用户说"转发到上级系统" → 转发协议推断（→ ASFP2），不询问"""
        csv_path = create_test_csv(tmp_path)

        with chat.send_with_file(
            "接入这个设备，采集到的数据需要转发到上级系统", str(csv_path)
        ) as stream:
            text = stream.text_content()

        assert len(text) > 0, "Response should not be empty"

        # 不应询问转发协议（从转发目标描述推断）
        forward_ask_signals = [
            "什么协议转发", "用哪种协议转发", "转发方式", "如何转发",
            "哪种方式转发",
        ]
        asked = [kw for kw in forward_ask_signals if kw in text]
        assert not asked, (
            f"Should NOT ask forwarding protocol when inferable from target "
            f"description. Matched: {asked}. Got: {text[:500]}"
        )

        # 转发目标被识别（对话体现转发意图被理解）
        has_forward = any(kw in text for kw in ["转发", "上级", "中心侧", "中心"])
        assert has_forward, (
            f"Forwarding target should be recognized. Got: {text[:500]}"
        ) 
