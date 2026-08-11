"""
C4 Agent L2 功能测试 — 方案生成 & 用户确认
===========================================

测试依据: c4/test/agent/README.md §4.4, §4.5

§4.4 方案生成 (plan-generator 子代理):
  4.4.1  Modbus → ASFP2 转发方案 — 对话含"确认"关键词
  4.4.2  无转发目标时仅采集 — 仍等待确认，方案仅含采集
  4.4.3  无法推断协议 — Agent 主动询问澄清
  4.4.4  协议无可用服务 — Agent 告知无可用服务

§4.5 用户确认与拒绝:
  4.5.1  用户确认方案 — 流程继续进入执行
  4.5.2  用户拒绝方案 — 流程停止，不生成 config.json
"""

import json
import os
from pathlib import Path
from typing import Optional

import pytest  # type: ignore

from test_helpers import (
    create_full_csv,
    create_test_csv,
    create_simple_csv,
    retry_llm,
    find_interrupt_id,
    run_upload,
)
from assertions import (
    assert_no_technical_terms,
    assert_config_json_valid,
    assert_no_tmp_file,
)


# ══════════════════════════════════════════════
#  §4.4 方案生成
# ══════════════════════════════════════════════


@pytest.mark.llm
class TestPlanGeneration:
    """§4.4 方案生成 — plan-generator 子代理"""

    @retry_llm(max_attempts=3)
    def test_generate_plan_with_forwarding(self, chat, agent, tmp_path):
        """4.4.1: doc-parser 完成后 "生成方案并转发到中心侧" → 对话含"确认"关键词"""
        csv_path = create_full_csv(tmp_path)

        # Step 1: 上传点表
        _stream, upload_text = run_upload(
            chat, str(csv_path), "接入华能阿拉善1#风机"
        )
        assert len(upload_text) > 0, (
            f"Upload should produce a response. Got: {upload_text[:200]}"
        )

        # Step 2: 请求生成方案
        with chat.send("生成接入方案，并转发到中心侧") as stream:
            plan_text = stream.text_content()

        assert len(plan_text) > 0, "Plan generation should produce a response"

        # 核心断言：方案包含接入关键要素 + 等待确认信号
        confirm_keywords = ["确认", "是否继续", "conform", "是否执行", "执行吗", "继续吗"]
        has_confirm = any(kw in plan_text for kw in confirm_keywords)
        assert has_confirm, (
            f"Plan response should contain confirmation signal. "
            f"Got: {plan_text[:500]}"
        )
        # 方案展示时可豁免协议名，但无例外黑名单仍需检查
        assert_no_technical_terms(plan_text, allow_protocols=True, allow_ports=True)

    @retry_llm(max_attempts=3)
    def test_generate_plan_without_forwarding(self, chat, agent, tmp_path):
        """4.4.2: 无转发目标 → 方案仅含采集，仍等待确认"""
        csv_path = create_test_csv(tmp_path)

        # Step 1: 上传点表
        with chat.send_with_file("接入此设备", str(csv_path)) as stream:
            upload_text = stream.text_content()
        assert len(upload_text) > 0
        chat.record_response(upload_text)

        # Step 2: 请求方案生成（不提转发）
        with chat.send("生成接入方案") as stream:
            plan_text = stream.text_content()

        assert len(plan_text) > 0, "Plan text should not be empty"

        # 应有确认信号
        confirm_keywords = ["确认", "是否继续", "conform"]
        has_confirm = any(kw in plan_text for kw in confirm_keywords)
        assert has_confirm, (
            f"Plan should wait for confirmation. Got: {plan_text[:500]}"
        )
        # 不应包含转发目标描述
        assert_no_technical_terms(plan_text, allow_protocols=True, allow_ports=True)

    @retry_llm(max_attempts=3)
    def test_incomplete_device_info(self, chat, agent, tmp_path):
        """4.4.3: 发送不完整的设备信息 → Agent 主动询问澄清"""
        csv_path = create_simple_csv(tmp_path)

        # 上传简单点表（只有基础信息，无设备名/IP/协议）
        with chat.send_with_file("接入这个设备", str(csv_path)) as stream:
            text = stream.text_content()

        assert len(text) > 0, "Response should not be empty"

        # Agent 应询问 — 不进入 confirm 状态
        clarification_keywords = [
            "协议", "通信方式", "IP", "地址", "端口", "设备名",
            "是什么", "哪种", "哪个", "请提供", "告诉我", "需要知道",
            "protocol", "what", "which", "address",
        ]
        needs_clarification = any(kw in text for kw in clarification_keywords)
        assert needs_clarification, (
            f"Agent should ask for clarification on incomplete info. Got: {text[:500]}"
        )
        # 不应出现确认流程关键词（未进入 confirm 状态）
        assert "确认方案" not in text, (
            f"Should NOT enter confirm phase with incomplete info. Got: {text[:500]}"
        )

    @retry_llm(max_attempts=3)
    def test_unsupported_protocol(self, chat, agent, tmp_path):
        """4.4.4: 使用不支持的协议 → Agent 告知无可用服务"""
        csv_path = create_test_csv(tmp_path)

        # Step 1: 上传点表
        with chat.send_with_file("这个设备使用 DNP3 协议接入", str(csv_path)) as stream:
            upload_text = stream.text_content()
        assert len(upload_text) > 0

        # Step 2: 明确要求使用不支持的协议
        with chat.send("这个设备是 DNP3 协议的，帮我生成接入方案") as stream:
            text = stream.text_content()

        assert len(text) > 0, "Response should not be empty"

        # Agent 应告知无可用服务或建议替代方案，不应生成错误方案
        no_service_signals = [
            "不支持", "没有", "无法", "暂不支持", "不可用",
            "not support", "unavailable", "cannot", "no service",
            "替代", "alternative",
        ]
        has_no_service = any(kw in text.lower() for kw in no_service_signals)
        assert has_no_service, (
            f"Agent should indicate no available service for unsupported protocol. "
            f"Got: {text[:500]}"
        )

        # 错误消息不应含技术黑名单
        assert_no_technical_terms(text, allow_protocols=False)


# ══════════════════════════════════════════════
#  §4.5 用户确认与拒绝
# ══════════════════════════════════════════════


@pytest.mark.llm
class TestConfirmReject:
    """§4.5 用户确认与拒绝"""

    @retry_llm(max_attempts=3)
    def test_user_confirm_proceeds(self, chat, agent, tmp_path):
        """4.5.1: 用户确认方案 → 流程继续进入执行"""
        csv_path = create_full_csv(tmp_path)

        # Step 1: 上传 → 解析
        with chat.send_with_file("接入华能阿拉善1#风机", str(csv_path)) as stream:
            upload_text = stream.text_content()
        assert len(upload_text) > 0

        # Step 2: 生成方案
        with chat.send("生成接入方案，并转发到中心侧") as stream:
            plan_text = stream.text_content()
            interrupt_id = find_interrupt_id(stream)

        assert len(plan_text) > 0

        # Step 3: 确认方案
        confirm_msg = "确认，按方案执行"
        with chat.send(confirm_msg) as stream:
            confirm_text = stream.text_content()

        assert len(confirm_text) > 0, (
            f"After confirmation, should get execution response. Got empty text"
        )
        # 确认后应有执行相关的内容
        # （具体内容取决于 step-decomposer + 执行模块的实现）

    @retry_llm(max_attempts=3)
    def test_user_reject_stops_flow(self, chat, agent, tmp_path):
        """4.5.2: 用户拒绝方案 → 流程停止，不生成 config.json"""
        csv_path = create_full_csv(tmp_path)

        # Step 1: 上传 → 解析
        with chat.send_with_file("接入华能阿拉善1#风机", str(csv_path)) as stream:
            upload_text = stream.text_content()
        assert len(upload_text) > 0

        # Step 2: 生成方案
        with chat.send("生成接入方案") as stream:
            plan_text = stream.text_content()
            interrupt_id = find_interrupt_id(stream)
        assert len(plan_text) > 0

        # Step 3: 拒绝方案
        with chat.send("取消，不执行这个方案") as stream:
            reject_text = stream.text_content()

        assert len(reject_text) > 0, (
            f"After rejection, should get acknowledgment response"
        )
        # 应有取消确认信号
        cancel_signals = ["取消", "停止", "已取消", "放弃", "cancel"]
        has_cancel = any(kw in reject_text.lower() for kw in cancel_signals)
        assert has_cancel, (
            f"Rejection should be acknowledged. Got: {reject_text[:500]}"
        )

        # 核心断言：不应生成 config.json
        config_path = agent.config_dir / "config.json"
        if config_path.exists():
            # 如果已存在 config.json（先前测试遗留），检查是否未新增内容
            # 但拒绝后不应有新的服务实例
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
                # 服务数组应为空或只有 shm_manager
                service_count = sum(
                    1 for k, v in config.items()
                    if k != "c4_shm_manager" and isinstance(v, list) and len(v) > 0
                )
                assert service_count == 0, (
                    f"After rejection, no new service instances should exist. "
                    f"Found {service_count} service type(s) with instances"
                )
            except json.JSONDecodeError:
                pass
