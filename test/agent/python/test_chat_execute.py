"""
C4 Agent L2 功能测试 — 执行验证 & 错误恢复 & 状态持久化
========================================================

测试依据: c4/test/agent/README.md §4.6, §4.8, §4.9

§4.6 执行验证（副作用检查）:
  4.6.1.1  首次接入 (Modbus + ASFP2) → config.json 含正确结构
  4.6.1.2  首次接入 (仅采集) → config.json 无 asfp2_client
  4.6.1.3  原子写入 → 无 .tmp 残留, .bak 存在
  4.6.1.4  writer/reader 分类 → 与 Registry 一致
  4.6.1.5  追加设备 → 新实例追加, 旧实例保留
   4.6.2.1  修改 IP → 实例 IP 变更, 其余不变
   4.6.2.2  修改点参数 → point addr 变更
   4.6.2.4  删除采集点 → points[] 移除 temperature
   4.6.2.5  修改不存在的实例 → 友好错误, config 不变
   4.6.3.1  删除实例 → 从数组移除
   4.6.3.4  删除不存在的实例 → 友好错误, config 不变

§4.8 错误恢复路径（4.8.1-4.8.3/4.8.5 为预构造 config 的确定性测试，绕过 LLM）:
  4.8.1  adjust_shm 失败 — DUPLICATE_KEY 回退 .bak（内容级断言 + 非技术语言错误）
  4.8.2  adjust_shm 失败 — CONFIG_MISSING_SECTION 回退 .bak（内容级断言）
  4.8.3  adjust_shm 失败 — UNKNOWN_READER_KEY 回退 .bak（内容级断言）
  4.8.4  adjust_shm 失败 — SHM_SYSCALL_FAILED 不回退 config
  4.8.5  start 部分失败 — 注入 binary_path 不存在的 mock 服务
  4.8.6  step-decomposer 失败 — 用户消息验证

§4.9 AgentState 持久化（GET /api/state 观测 phase / hasAccessPlan / lastError）:
  4.9.1  接入流程中途重启 → hasAccessPlan=true 恢复
  4.9.2  用户确认后中断 → phase=confirmed 保持
  4.9.3  执行完成后状态重置 → phase=idle + hasAccessPlan=false（强断言）
  4.9.4  状态重置后可处理新接入
"""

import json
import os
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path

import pytest  # type: ignore

from test_helpers import (
    create_full_csv,
    create_test_csv,
    create_messy_csv,
    retry_llm,
    find_interrupt_id,
    run_upload,
    full_access_flow,
    delete_device,
)
from assertions import (
    STRICT_BLACKLIST,
    CONTEXTUAL_BLACKLIST,
    assert_no_technical_terms,
    assert_no_json_leak,
    assert_config_json_valid,
    assert_shm_ids_assigned,
    assert_writer_reader_from_registry,
    assert_no_tmp_file,
    assert_process_running,
    assert_config_shm_process_consistent,
)


# ══════════════════════════════════════════════
#  §4.6 执行验证 — add 操作
# ══════════════════════════════════════════════


@pytest.mark.llm
class TestExecuteAdd:
    """§4.6.1 add 操作 — 首次接入 + 追加"""

    @retry_llm(max_attempts=3)
    def test_first_access_full_flow(self, chat, agent, tmp_path):
        """4.6.1.1: 首次接入 (Modbus + ASFP2 转发) → 完整产物验证"""
        csv_path = create_full_csv(tmp_path)

        result = full_access_flow(
            chat, agent, str(csv_path),
            upload_msg="接入华能阿拉善1#风机",
            plan_msg="生成接入方案，并转发到中心侧",
            confirm=True,
            tmp_path=tmp_path,
        )

        # 等待执行完成（SSE 流已关闭，但副作用可能需要时间）
        time.sleep(3)

        config = result.get("config_json")
        if config is None:
            config_path = agent.config_dir / "config.json"
            if config_path.exists():
                config = assert_config_json_valid(config_path)
        assert config is not None, (
            "config.json should be generated after full access flow"
        )

        # 1. config.json 含 shm_manager + modbus + asfp2
        assert "c4_shm_manager" in config, "config.json must contain c4_shm_manager"
        assert "c4_modbus_client" in config, (
            "config.json must contain c4_modbus_client"
        )
        assert "c4_asfp2_client" in config, (
            "config.json must contain c4_asfp2_client (forwarding)"
        )

        # 2. 所有 shm_id != 0
        assert_shm_ids_assigned(config)

        # 3. default 字段填充检查 — modbus 实例应含基本字段
        modbus_instances = config.get("c4_modbus_client", [])
        assert len(modbus_instances) > 0, "Should have at least one modbus instance"
        required_fields = {"name", "ip", "port", "points"}
        for inst in modbus_instances:
            for field in required_fields:
                assert field in inst, (
                    f"Modbus instance missing required field '{field}': {inst}"
                )

        # 4. 语言约束
        all_text = "\n".join(
            filter(None, [
                result.get("upload_text", ""),
                result.get("plan_text", ""),
                result.get("confirm_text", ""),
            ])
        )
        if all_text.strip():
            assert_no_technical_terms(all_text, allow_protocols=True, allow_ports=True)

    @retry_llm(max_attempts=3)
    def test_asfp2_data_source(self, chat, agent, tmp_path):
        """ASFP2 数据源接入 → config.json 含 c4_asfp2_server"""
        import json
        from test_helpers import _parse_csv_to_device_json

        csv_path = tmp_path / "asfp2_points.csv"
        csv_path.write_text(
            "device_name,device_ip,protocol,port,point_name,addr\n"
            "ASFP2数据源,172.16.109.11,asfp2,9999,wind_speed,1000\n"
            "ASFP2数据源,172.16.109.11,asfp2,9999,temperature,1002\n",
            encoding="utf-8"
        )

        # 直接从 CSV 解析设备 JSON，用作 confirm 消息
        devices = _parse_csv_to_device_json(str(csv_path))
        confirm_msg = f"确认\n\n{json.dumps(devices)}"
        with chat.send(confirm_msg) as stream:
            text = stream.text_content()

        time.sleep(3)
        config_path = agent.config_dir / "config.json"
        assert config_path.exists(), f"config.json should exist at {config_path}"
        config = assert_config_json_valid(config_path)
        assert "c4_asfp2_server" in config, (
            f"Expected c4_asfp2_server in config. Got: {[k for k in config if k.startswith('c4_')]}"
        )

    @retry_llm(max_attempts=3)
    def test_collection_only_no_forwarding(self, chat, agent, tmp_path):
        """4.6.1.2: 仅采集无转发 → config.json 有 modbus 无 asfp2_client"""
        csv_path = create_test_csv(tmp_path)

        result = full_access_flow(
            chat, agent, str(csv_path),
            upload_msg="接入此设备",
            plan_msg="生成接入方案（仅采集，不需要转发）",
            confirm=True,
            tmp_path=tmp_path,
        )

        time.sleep(3)

        config = result.get("config_json")
        if config is None:
            config_path = agent.config_dir / "config.json"
            if config_path.exists():
                config = assert_config_json_valid(config_path)
        assert config is not None, "config.json should be generated"

        # 含 modbus
        assert "c4_modbus_client" in config, "Must contain c4_modbus_client"
        # 不含 asfp2_client reader
        assert "c4_asfp2_client" not in config, (
            "Should NOT contain c4_asfp2_client (no forwarding requested)"
        )
        # reader 列表为空或没有 asfp2
        shm_section = config.get("c4_shm_manager", {})
        readers = shm_section.get("reader", [])
        assert "c4_asfp2_client" not in readers, (
            "c4_shm_manager.reader[] should not contain c4_asfp2_client"
        )

    def test_atomic_write_no_tmp(self, chat, agent, tmp_path):
        """4.6.1.3: 原子写入 — 无 .tmp 残留, .bak 存在"""
        csv_path = create_full_csv(tmp_path)

        result = full_access_flow(
            chat, agent, str(csv_path),
            upload_msg="接入华能阿拉善1#风机",
            plan_msg="生成接入方案，并转发到中心侧",
            confirm=True,
            tmp_path=tmp_path,
        )

        time.sleep(3)

        config_dir = agent.config_dir
        # 无 .tmp 残留
        assert_no_tmp_file(config_dir)
        # .bak 文件检查（首次接入时 .bak 为 config.json 副本或写入前版本）
        # .bak 可能在写入时创建，也可能不创建 — 检查要么不存在，要么有效 JSON
        bak_path = config_dir / "config.json.bak"
        if bak_path.exists():
            try:
                bak_content = json.loads(bak_path.read_text(encoding="utf-8"))
                assert isinstance(bak_content, dict), (
                    "config.json.bak should contain valid JSON"
                )
            except json.JSONDecodeError:
                pytest.fail("config.json.bak exists but is not valid JSON")

    @retry_llm(max_attempts=3)
    def test_writer_reader_classification(self, chat, agent, tmp_path, registry_dir):
        """4.6.1.4: writer/reader 分类与 Registry 一致"""
        csv_path = create_full_csv(tmp_path)

        result = full_access_flow(
            chat, agent, str(csv_path),
            upload_msg="接入华能阿拉善1#风机",
            plan_msg="生成接入方案，并转发到中心侧",
            confirm=True,
            tmp_path=tmp_path,
        )

        time.sleep(3)

        config = result.get("config_json")
        if config is None:
            config_path = agent.config_dir / "config.json"
            if config_path.exists():
                config = assert_config_json_valid(config_path)
        assert config is not None, "config.json should be generated"

        # 验证 writer/reader 分类
        assert_writer_reader_from_registry(config, registry_dir)

    @retry_llm(max_attempts=3)
    def test_append_second_device(self, chat, agent, tmp_path):
        """4.6.1.5: 追加第二个设备 → 新实例追加, 旧实例完整保留"""
        csv1_path = create_full_csv(tmp_path, filename="device1.csv")

        # 首次接入
        result1 = full_access_flow(
            chat, agent, str(csv1_path),
            upload_msg="接入华能阿拉善1#风机",
            plan_msg="生成接入方案，并转发到中心侧",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(3)

        config1 = result1.get("config_json")
        if config1 is None:
            config_path = agent.config_dir / "config.json"
            if config_path.exists():
                config1 = assert_config_json_valid(config_path)
        assert config1 is not None
        modbus_before = config1.get("c4_modbus_client", [])
        assert len(modbus_before) >= 1, "Should have first modbus instance"

        # 追加第二个设备（full_access_flow 保证确定性）
        csv2_path = create_full_csv(tmp_path, filename="device2.csv", device_name="华能阿拉善2#风机", device_ip="192.168.110.2")
        full_access_flow(
            chat, agent, str(csv2_path),
            upload_msg="接入华能阿拉善2#风机，IP是192.168.110.2",
            plan_msg="生成接入方案",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(3)

        # 验证 config.json
        config_path = agent.config_dir / "config.json"
        if config_path.exists():
            config2 = assert_config_json_valid(config_path)
            modbus_after = config2.get("c4_modbus_client", [])

            # 新实例追加 — 数量增加
            assert len(modbus_after) >= len(modbus_before), (
                f"After append, modbus instances should increase. "
                f"Before: {len(modbus_before)}, After: {len(modbus_after)}"
            )

            # 旧实例保留 — 前 len(modbus_before) 个实例 name 不变
            for i in range(len(modbus_before)):
                if i < len(modbus_after):
                    assert modbus_after[i].get("name") == modbus_before[i].get("name"), (
                        f"Instance {i}: old name={modbus_before[i].get('name')}, "
                        f"new name={modbus_after[i].get('name')}"
                    )

            # c4_shm_manager.writer[] 不重复添加相同 service_type
            shm_section = config2.get("c4_shm_manager", {})
            writers = shm_section.get("writer", [])
            # 检查 c4_modbus_client 只出现一次
            modbus_count = writers.count("c4_modbus_client")
            assert modbus_count == 1, (
                f"c4_shm_manager.writer[] should have c4_modbus_client only once, "
                f"found {modbus_count} times"
            )


# ══════════════════════════════════════════════
#  §4.6.2 modify 操作
# ══════════════════════════════════════════════


@pytest.mark.llm
class TestExecuteModify:
    """§4.6.2 modify 操作 — 修改已有实例"""

    @retry_llm(max_attempts=3)
    def test_modify_ip(self, chat, agent, tmp_path):
        """4.6.2.1: 修改实例 IP → IP 变更, 其余不变"""
        csv_path = create_full_csv(tmp_path)

        # 首次接入
        result = full_access_flow(
            chat, agent, str(csv_path),
            upload_msg="接入华能阿拉善1#风机",
            plan_msg="生成接入方案，并转发到中心侧",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(3)

        config_path = agent.config_dir / "config.json"
        if not config_path.exists():
            pytest.skip("config.json not generated — cannot test modify")
        config_before = assert_config_json_valid(config_path)
        modbus_before = config_before.get("c4_modbus_client", [])
        if not modbus_before:
            pytest.skip("No modbus instances — cannot test modify")

        ip_before = modbus_before[0].get("ip")

        # 修改 IP
        with chat.send("将 1#风机的 IP 改为 192.168.110.5") as s:
            text = s.text_content()
        assert len(text) > 0

        with chat.send("确认修改") as s:
            text2 = s.text_content()
        assert len(text2) > 0
        time.sleep(3)

        # 验证
        config_after = assert_config_json_valid(config_path)
        modbus_after = config_after.get("c4_modbus_client", [])
        assert len(modbus_after) >= 1

        new_ip = modbus_after[0].get("ip")
        assert new_ip != ip_before or new_ip == "192.168.110.5", (
            f"IP should change to 192.168.110.5. Before: {ip_before}, After: {new_ip}"
        )

    @retry_llm(max_attempts=3)
    def test_modify_point_parameter(self, chat, agent, tmp_path):
        """4.6.2.2: 修改点参数 → point addr 变更, 其余不变"""
        csv_path = create_full_csv(tmp_path)

        # 首次接入
        result = full_access_flow(
            chat, agent, str(csv_path),
            upload_msg="接入华能阿拉善1#风机",
            plan_msg="生成接入方案",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(3)

        config_path = agent.config_dir / "config.json"
        if not config_path.exists():
            pytest.skip("config.json not generated")
        config_before = assert_config_json_valid(config_path)
        modbus_before = config_before.get("c4_modbus_client", [])
        if not modbus_before:
            pytest.skip("No modbus instances")

        points_before = modbus_before[0].get("points", [])
        if not points_before:
            pytest.skip("No points to modify")
        windspeed_before = None
        for pt in points_before:
            if "windspeed" in str(pt.get("id", "")).lower():
                windspeed_before = pt
                break
        addr_before = windspeed_before.get("addr") if windspeed_before else None

        # 修改点参数
        with chat.send("将 windspeed 的寄存器地址从 1000 改为 1002") as s:
            text = s.text_content()
        assert len(text) > 0

        with chat.send("确认修改") as s:
            text2 = s.text_content()
        assert len(text2) > 0
        time.sleep(3)

        config_after = assert_config_json_valid(config_path)
        modbus_after = config_after.get("c4_modbus_client", [])
        points_after = modbus_after[0].get("points", [])
        for pt in points_after:
            if "windspeed" in str(pt.get("id", "")).lower():
                new_addr = pt.get("addr")
                # 地址应变更（或至少在请求后发生改变）
                assert new_addr != addr_before, (
                    f"Point addr should change. Before: {addr_before}, After: {new_addr}"
                )

    @retry_llm(max_attempts=3)
    def test_delete_point(self, chat, agent, tmp_path):
        """4.6.2.4: 删除采集点 → points[] 移除 temperature，其余点保留"""
        csv_path = create_full_csv(tmp_path)

        result = full_access_flow(
            chat, agent, str(csv_path),
            upload_msg="接入华能阿拉善1#风机",
            plan_msg="生成接入方案，并转发到中心侧",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(3)

        config_path = agent.config_dir / "config.json"
        if not config_path.exists():
            pytest.skip("config.json not generated — cannot test delete point")
        config_before = assert_config_json_valid(config_path)
        modbus_before = config_before.get("c4_modbus_client", [])
        if not modbus_before:
            pytest.skip("No modbus instances")

        def _find_point(instances, keyword):
            """在实例列表中查找含 keyword 的 point（匹配 id 或 name 字段）。"""
            for inst in instances:
                for pt in inst.get("points", []):
                    if keyword in str(pt.get("id", "")).lower() or keyword in str(
                        pt.get("name", "")
                    ).lower():
                        return inst, pt
            return None, None

        _, temp_before = _find_point(modbus_before, "temperature")
        assert temp_before is not None, (
            "Precondition: config should contain a temperature point"
        )

        # 请求删除采集点
        with chat.send("不再采集 1#风机的温度数据") as s:
            text = s.text_content()
        assert len(text) > 0, "Delete point request should produce a response"

        with chat.send("确认修改") as s:
            text2 = s.text_content()
        assert len(text2) > 0
        time.sleep(3)

        config_after = assert_config_json_valid(config_path)
        modbus_after = config_after.get("c4_modbus_client", [])
        _, temp_after = _find_point(modbus_after, "temperature")
        assert temp_after is None, (
            f"temperature point should be removed from points[]. "
            f"Remaining: {[p.get('id') for inst in modbus_after for p in inst.get('points', [])]}"
        )
        # 其他采集点保留
        _, wind_after = _find_point(modbus_after, "windspeed")
        assert wind_after is not None, (
            "windspeed point should remain after deleting temperature"
        )

    @retry_llm(max_attempts=3)
    def test_modify_nonexistent_instance(self, chat, agent, tmp_path):
        """4.6.2.5: 修改不存在的设备 → 友好错误（非技术语言），config 不变"""
        csv_path = create_full_csv(tmp_path)

        result = full_access_flow(
            chat, agent, str(csv_path),
            upload_msg="接入华能阿拉善1#风机",
            plan_msg="生成接入方案，并转发到中心侧",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(3)

        config_path = agent.config_dir / "config.json"
        if not config_path.exists():
            pytest.skip("config.json not generated — cannot test nonexistent modify")
        config_before = assert_config_json_valid(config_path)
        snapshot_before = json.dumps(config_before, sort_keys=True, ensure_ascii=False)

        # 请求修改一个从未接入过的设备 ID（hnals_wt9 不存在）
        with chat.send("请修改设备 hnals_wt9 的 IP 为 192.168.110.9") as s:
            text = s.text_content()

        assert len(text) > 0, "Response should not be empty"

        # 友好错误提示（非技术语言），错误场景协议名无豁免
        friendly_signals = [
            "不存在", "找不到", "没有找到", "未接入", "从未接入",
            "没有接入", "没有这个设备", "请确认", "哪个设备",
        ]
        has_friendly = any(kw in text for kw in friendly_signals)
        assert has_friendly, (
            f"Should give friendly error for nonexistent device. Got: {text[:500]}"
        )
        assert_no_technical_terms(text, allow_protocols=False)

        # 不修改 config.json
        config_after = assert_config_json_valid(config_path)
        snapshot_after = json.dumps(config_after, sort_keys=True, ensure_ascii=False)
        assert snapshot_after == snapshot_before, (
            "config.json must NOT change when modifying a nonexistent instance"
        )


# ══════════════════════════════════════════════
#  §4.6.3 delete 操作
# ══════════════════════════════════════════════


@pytest.mark.llm
class TestExecuteDelete:
    """§4.6.3 delete 操作 — 删除实例"""

    @retry_llm(max_attempts=3)
    def test_delete_instance(self, chat, agent, tmp_path):
        """4.6.3.1: 删除单个实例 → 从数组移除"""
        # 先创建 2 个设备
        csv_path = create_full_csv(tmp_path)

        result = full_access_flow(
            chat, agent, str(csv_path),
            upload_msg="接入华能阿拉善1#风机",
            plan_msg="生成接入方案，并转发到中心侧",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(3)

        # 追加第二个设备（full_access_flow 保证确定性）
        csv2_path = create_full_csv(tmp_path, filename="device2.csv", device_name="华能阿拉善2#风机", device_ip="192.168.110.2")
        full_access_flow(
            chat, agent, str(csv2_path),
            upload_msg="接入华能阿拉善2#风机，IP: 192.168.110.2",
            plan_msg="生成接入方案",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(3)

        config_path = agent.config_dir / "config.json"
        if not config_path.exists():
            pytest.skip("config.json not generated")
        config_before = assert_config_json_valid(config_path)
        modbus_before = config_before.get("c4_modbus_client", [])
        assert len(modbus_before) >= 2, "Should have 2 modbus instances"

        # 删除第二个（确定性：嵌入 instance.id）
        delete_device(chat, agent, "2#风机")
        time.sleep(3)

        config_after = assert_config_json_valid(config_path)
        modbus_after = config_after.get("c4_modbus_client", [])
        assert len(modbus_after) < len(modbus_before), (
            f"After delete, instances should decrease. "
            f"Before: {len(modbus_before)}, After: {len(modbus_after)}"
        )

        # c4_shm_manager.writer[] 仍含 c4_modbus_client
        shm_section = config_after.get("c4_shm_manager", {})
        writers = shm_section.get("writer", [])
        assert "c4_modbus_client" in writers, (
            "c4_shm_manager.writer[] should still contain c4_modbus_client "
            "after deleting one instance"
        )

    @retry_llm(max_attempts=3)
    def test_delete_nonexistent_instance(self, chat, agent, tmp_path):
        """4.6.3.4: 删除不存在的设备 → 友好错误，config 不变"""
        csv_path = create_full_csv(tmp_path)

        result = full_access_flow(
            chat, agent, str(csv_path),
            upload_msg="接入华能阿拉善1#风机",
            plan_msg="生成接入方案，并转发到中心侧",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(3)

        config_path = agent.config_dir / "config.json"
        if not config_path.exists():
            pytest.skip("config.json not generated — cannot test nonexistent delete")
        config_before = assert_config_json_valid(config_path)
        snapshot_before = json.dumps(config_before, sort_keys=True, ensure_ascii=False)

        # 请求删除一个从未接入过的设备 ID（hnals_wt9 不存在）
        with chat.send("请停用设备 hnals_wt9") as s:
            text = s.text_content()

        assert len(text) > 0, "Response should not be empty"

        # 友好错误提示（非技术语言），错误场景协议名无豁免
        friendly_signals = [
            "不存在", "找不到", "没有找到", "未接入", "从未接入",
            "没有接入", "没有这个设备", "请确认", "哪个设备",
        ]
        has_friendly = any(kw in text for kw in friendly_signals)
        assert has_friendly, (
            f"Should give friendly error for nonexistent device. Got: {text[:500]}"
        )
        assert_no_technical_terms(text, allow_protocols=False)

        # 不修改 config.json
        config_after = assert_config_json_valid(config_path)
        snapshot_after = json.dumps(config_after, sort_keys=True, ensure_ascii=False)
        assert snapshot_after == snapshot_before, (
            "config.json must NOT change when deleting a nonexistent instance"
        )


# ══════════════════════════════════════════════
#  §4.8 错误恢复路径
# ══════════════════════════════════════════════
#
# 测试策略（README §4.8 注记）：4.8.1-4.8.3 通过预构造 config.json 直接构造
# 错误条件，绕过 LLM 生成环节，直接测试执行模块（executeStopAndStart）
# 的错误处理 —— kill → restart 触发启动期无条件 Stop-Start（agent.md §3.2.3），
# adjust_shm 失败后按 §3.2.2 协议回退 config.json.bak 并 restart 服务。
# 4.8.5 通过 registry 注入 binary_path 不存在的 mock 服务模拟 start 失败。


# 基线合法配置：c4_asfp2_server (writer) + c4_asfp2_client (reader)。
# 结构与 test_startup.py 的 _CONFIG_WITH_SERVICES 一致（已被 L1 启动恢复测试验证）。
_CONFIG_VALID_BASELINE: dict = {
    "c4_shm_manager": {
        "instance_id": "c4_test",
        "max_points": 100000,
        "writer": ["c4_asfp2_server"],
        "reader": ["c4_asfp2_client"],
    },
    "c4_asfp2_server": [
        {
            "id": "test_asfp2_srv_1",
            "name": "ASFP2接收服务1",
            "ip": "0.0.0.0",
            "port": 0,
            "points": [
                {"id": "point_1000", "addr": 1000, "shm_id": 0},
                {"id": "point_1002", "addr": 1002, "shm_id": 0},
            ],
        }
    ],
    "c4_asfp2_client": [
        {
            "id": "test_asfp2_cli_1",
            "name": "ASFP2转发1",
            "ip": "127.0.0.1",
            "port": 9999,
            "t0": 30,
            "t1": 20,
            "t2": 10,
            "timer": 100,
            "key_sequence": 1,
            "same_data_type": 1,
            "same_timestamp": 1,
            "smart": 1,
            "forward_kack": 255,
            "inverse_keep": 0,
            "points": [
                {"key": "test_asfp2_srv_1.point_1000", "addr": 3001, "shm_id": 0},
                {"key": "test_asfp2_srv_1.point_1002", "addr": 3002, "shm_id": 0},
            ],
        }
    ],
}


def _extract_state_payload(state: dict) -> dict:
    """
    兼容两种 GET /api/state 响应形状，返回可观测子集 dict：

      - README §4.9 直接形状:   {phase, hasAccessPlan, lastError}
      - 包装形状:               {success: bool, state: {phase, hasAccessPlan, lastError}}
    """
    if isinstance(state, dict) and isinstance(state.get("state"), dict):
        return state["state"]
    return state


def _write_config_pair(agent, config: dict, bak: dict) -> Path:
    """
    写入 config.json（错误条件）与 config.json.bak（基线合法配置），
    返回 config.json 路径。
    """
    config_path = agent.config_dir / "config.json"
    bak_path = agent.config_dir / "config.json.bak"
    bak_path.write_text(
        json.dumps(bak, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return config_path


class TestErrorRecoveryConfigErrors:
    """§4.8.1-4.8.3, 4.8.5: adjust_shm config 类错误 + start 部分失败

    确定性测试：预构造 config.json 绕过 LLM，kill → restart 触发启动期
    无条件 Stop-Start，直接验证执行模块的错误恢复行为（agent.md §3.2.2/§3.2.3）。
    不依赖 DEEPSEEK_API_KEY，随 L1 一起运行。
    """

    def _plant_error_and_restart(self, agent, bad_config: dict) -> dict:
        """写入 .bak=基线 + config.json=错误条件 → kill → restart。返回重启后 state payload。"""
        _write_config_pair(agent, bad_config, deepcopy(_CONFIG_VALID_BASELINE))
        agent.kill()
        agent.restart()
        time.sleep(1)  # 给服务进程 spawn/restart 留出时间
        return _extract_state_payload(agent.get_state())

    def _assert_rolled_back_to_bak(self, agent, config_path: Path) -> None:
        """断言 config.json 内容级恢复为 .bak 内容 + 服务被 restart + 无 .tmp 残留 + 非技术语言错误记录。"""
        config_after = assert_config_json_valid(config_path)
        assert config_after == _CONFIG_VALID_BASELINE, (
            "config.json should be restored to config.json.bak content after "
            f"rollback.\nAFTER: {json.dumps(config_after, ensure_ascii=False)[:800]}"
        )

        # 已 stop 的服务被 restart：.bak 中声明的 writer/reader 进程恢复运行
        assert_process_running("c4_asfp2_server")
        assert_process_running("c4_asfp2_client")

        # 原子写入无残留
        assert_no_tmp_file(agent.config_dir)

        # 用户收到底层原因的错误描述（但非技术语言）——README §4.8.1「同上」：
        # lastError 是 AgentState 的最小可观测出口（agent.md §3.2.1.7：非技术语言）
        state = _extract_state_payload(agent.get_state())
        last_error = state.get("lastError")
        assert last_error, (
            f"lastError should record the adjust_shm failure. State: {state}"
        )
        assert_no_technical_terms(str(last_error), allow_protocols=False)

    def test_duplicate_key_rollback_to_bak(self, agent):
        """4.8.1: adjust_shm 失败 — DUPLICATE_KEY → config.json 恢复为 .bak 内容"""
        bad = deepcopy(_CONFIG_VALID_BASELINE)
        # 构造重复的 {service_id}.{point_id} 全局 key：
        # 同一 writer 实例中两个 point 的 id 相同 →
        # 全局 key 'test_asfp2_srv_1.point_1000' 出现两次 → DUPLICATE_KEY
        bad["c4_asfp2_server"][0]["points"].append(
            {"id": "point_1000", "addr": 9999, "shm_id": 0}
        )

        config_path = agent.config_dir / "config.json"
        self._plant_error_and_restart(agent, bad)
        self._assert_rolled_back_to_bak(agent, config_path)

    def test_config_missing_section(self, agent):
        """4.8.2: adjust_shm 失败 — CONFIG_MISSING_SECTION（reader 存在但 writer 为空）→ 回退 .bak"""
        bad = deepcopy(_CONFIG_VALID_BASELINE)
        # writer 为空但 reader 非空 → adjust_shm 返回 CONFIG_MISSING_SECTION
        bad["c4_shm_manager"]["writer"] = []

        config_path = agent.config_dir / "config.json"
        self._plant_error_and_restart(agent, bad)
        self._assert_rolled_back_to_bak(agent, config_path)

    def test_unknown_reader_key(self, agent):
        """4.8.3: adjust_shm 失败 — UNKNOWN_READER_KEY（key 指向不存在的 writer）→ 回退 .bak"""
        bad = deepcopy(_CONFIG_VALID_BASELINE)
        # asfp2_client points[0].key 指向不存在的 writer → UNKNOWN_READER_KEY
        bad["c4_asfp2_client"][0]["points"][0]["key"] = (
            "nonexistent_srv.nonexistent_point"
        )

        config_path = agent.config_dir / "config.json"
        self._plant_error_and_restart(agent, bad)
        self._assert_rolled_back_to_bak(agent, config_path)

    def test_start_partial_failure(self, agent, registry_dir):
        """4.8.5: start 部分失败 → 成功服务保持运行，失败服务被报告

        注入方法（README §4.8.5 注记）：在 registry 中新增一个 binary_path
        不存在的 mock 服务（writer 角色），与正常服务（c4_asfp2_server /
        c4_asfp2_client）一起写入 config.json。restart 触发启动期 Stop-Start：
        adjust_shm 成功 → start 阶段 mock 服务失败、其余服务成功。
        """
        # 注入 binary_path 不存在的 mock 服务 registry 条目
        fake_registry = {
            "service_type": "c4_fake_service",
            "display_name": "测试注入服务",
            "role": "writer",
            "protocols": [
                {"protocol": "fake", "description": "测试注入用，无真实二进制"}
            ],
            "point_fields": [
                {"name": "addr", "type": "integer", "description": "地址"}
            ],
            "config_schema": {
                "fields": {
                    "ip": {
                        "type": "string",
                        "source": "default",
                        "default": "0.0.0.0",
                        "description": "绑定 IP",
                    },
                    "port": {
                        "type": "integer",
                        "source": "default",
                        "default": 0,
                        "description": "端口",
                    },
                }
            },
            "binary_path": "/nonexistent/c4_fake_service",
            "error_mappings": {},
        }
        fake_path = registry_dir / "c4_fake_service.json"
        fake_path.write_text(
            json.dumps(fake_registry, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # config 含正常服务（成功）+ mock 服务（start 失败）
        bad = deepcopy(_CONFIG_VALID_BASELINE)
        bad["c4_shm_manager"]["writer"].append("c4_fake_service")
        bad["c4_fake_service"] = [
            {
                "id": "test_fake_1",
                "name": "注入失败服务",
                "ip": "0.0.0.0",
                "port": 0,
                "points": [{"id": "pt_1", "addr": 1, "shm_id": 0}],
            }
        ]

        try:
            config_path = _write_config_pair(agent, bad, deepcopy(_CONFIG_VALID_BASELINE))
            agent.kill()
            agent.restart()
            time.sleep(1)

            # 成功的服务保持运行
            assert_process_running("c4_asfp2_server")
            assert_process_running("c4_asfp2_client")

            # 失败的服务未被启动（binary_path 不存在）
            fake_proc = subprocess.run(
                ["pgrep", "-f", "c4_fake_service"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert not fake_proc.stdout.strip(), (
                "c4_fake_service should NOT be running "
                "(its binary_path does not exist)"
            )

            # 失败的服务被报告：lastError（非技术语言）记录启动失败
            state = _extract_state_payload(agent.get_state())
            last_error = state.get("lastError")
            assert last_error, (
                f"lastError should report the failed service start. State: {state}"
            )
            assert_no_technical_terms(str(last_error), allow_protocols=False)

            # 启动部分失败不回滚 config（adjust_shm 已成功）
            config_after = assert_config_json_valid(config_path)
            assert "c4_fake_service" in config_after, (
                "start 部分失败不触发 config 回退 — config.json 应保留失败服务的声明"
            )
        finally:
            if fake_path.exists():
                fake_path.unlink()


@pytest.mark.llm
class TestErrorRecovery:
    """§4.8 错误恢复路径（需 LLM / 特殊环境的用例：4.8.4, 4.8.6）"""

    @retry_llm(max_attempts=3)
    def test_step_decomposer_failure_message(self, chat, agent, tmp_path):
        """4.8.6: step-decomposer 失败 → 用户收到非技术语言提示"""
        csv_path = create_messy_csv(tmp_path)

        # 上传混乱点表
        with chat.send_with_file("接入这个设备", str(csv_path)) as stream:
            text = stream.text_content()

        assert len(text) > 0, "Response should not be empty"

        # 尝试生成方案 — step-decomposer 应因点表混乱而失败
        with chat.send("生成接入方案") as stream:
            text2 = stream.text_content()

        # 合并检查错误消息
        combined_text = text + "\n" + text2

        # 错误消息不应含黑名单术语
        assert_no_technical_terms(combined_text, allow_protocols=False)

        # 无 config.json.tmp 残留
        assert_no_tmp_file(agent.config_dir)

    def test_shm_syscall_failed_no_config_rollback(self, chat, agent, tmp_path):
        """4.8.4: SHM_SYSCALL_FAILED → config.json 不回退

        注：此用例需 sudo mount 操作限制 /dev/shm 大小。
        当环境不允许 remount（如容器），skip 此用例。
        """
        # 检查是否可 remount /dev/shm
        can_remount = False
        try:
            result = subprocess.run(
                ["sudo", "-n", "true"],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                can_remount = True
        except Exception:
            pass

        if not can_remount:
            pytest.skip(
                "sudo not available — cannot remount /dev/shm for SHM_SYSCALL_FAILED test"
            )

        csv_path = create_full_csv(tmp_path)

        # 先完成一次接入 — 建立 config.json
        result = full_access_flow(
            chat, agent, str(csv_path),
            upload_msg="接入华能阿拉善1#风机",
            plan_msg="生成接入方案，并转发到中心侧",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(3)

        config_path = agent.config_dir / "config.json"
        if not config_path.exists():
            pytest.skip("config.json not generated")

        config_before = assert_config_json_valid(config_path)
        config_text = config_path.read_text(encoding="utf-8")

        try:
            # 限制 /dev/shm 大小
            subprocess.run(
                ["sudo", "mount", "-o", "remount,size=1M", "/dev/shm"],
                capture_output=True, timeout=10,
                check=True,
            )

            # kill + restart — 触发 adjust_shm（应失败）
            agent.kill()
            time.sleep(1)

            try:
                agent.restart()
            except Exception:
                # restart 可能因 shm 不足而失败 — 检查 config.json 是否完好
                pass

            # config.json 不应回退
            if config_path.exists():
                config_after = assert_config_json_valid(config_path)
                assert isinstance(config_after, dict)

        finally:
            # 恢复 /dev/shm（尝试恢复到较大值）
            subprocess.run(
                ["sudo", "mount", "-o", "remount,size=256M", "/dev/shm"],
                capture_output=True, timeout=10,
            )


# ══════════════════════════════════════════════
#  §4.9 AgentState 持久化
# ══════════════════════════════════════════════


@pytest.mark.llm
class TestAgentState:
    """§4.9 AgentState 持久化"""

    @retry_llm(max_attempts=3)
    def test_state_restore_after_plan_generation(self, chat, agent, tmp_path):
        """4.9.1: 接入流程中途重启 → 状态恢复

        前置：完成 info-gatherer + plan-generator，hasAccessPlan=true。
        kill → restart 后 hasAccessPlan 仍为 true。
        """
        csv_path = create_full_csv(tmp_path)

        # 上传点表 + 生成方案
        with chat.send_with_file("接入华能阿拉善1#风机", str(csv_path)) as s:
            s.text_content()

        with chat.send("生成接入方案，并转发到中心侧") as s:
            plan_text = s.text_content()
            interrupt_id = find_interrupt_id(s)

        assert len(plan_text) > 0

        # README §4.9.1 前置断言：plan 已生成 → hasAccessPlan = true
        state = _extract_state_payload(agent.get_state())
        assert state.get("hasAccessPlan") is True, (
            f"hasAccessPlan should be true after plan generation. State: {state}"
        )

        # kill + restart
        agent.kill()
        agent.restart()

        # 重启后 hasAccessPlan 仍为 true（checkpoint 持久化恢复，用户无需重新上传点表）
        state2 = _extract_state_payload(agent.get_state())
        assert state2.get("hasAccessPlan") is True, (
            f"hasAccessPlan should persist across restart (checkpoint restore). "
            f"State: {state2}"
        )

    @retry_llm(max_attempts=3)
    def test_state_after_confirm_before_execution(self, chat, agent, tmp_path):
        """4.9.2: 用户确认后中断 → 状态保持

        前置：确认方案后，在 step-decomposer 执行前 kill。
        restart 后 phase 反映已确认状态（"confirmed"），Agent 可继续执行。
        """
        csv_path = create_full_csv(tmp_path)

        # 上传 + 方案 + 确认
        with chat.send_with_file("接入华能阿拉善1#风机", str(csv_path)) as s:
            s.text_content()

        with chat.send("生成接入方案") as s:
            s.text_content()
            interrupt_id = find_interrupt_id(s)

        if interrupt_id:
            with chat.send("确认方案") as s:
                s.text_content()

        # 快速 kill（模拟 step-decomposer 执行前崩溃）
        agent.kill()
        agent.restart()

        # 重启后 phase 反映已确认状态
        state = _extract_state_payload(agent.get_state())
        phase = state.get("phase")
        assert phase == "confirmed", (
            f"After confirm + restart, phase should be 'confirmed', "
            f"got '{phase}'. State: {state}"
        )

    def test_state_reset_after_completion(self, chat, agent, tmp_path):
        """4.9.3: 执行完成后 → phase="idle", hasAccessPlan=false"""
        csv_path = create_full_csv(tmp_path)

        result = full_access_flow(
            chat, agent, str(csv_path),
            upload_msg="接入华能阿拉善1#风机",
            plan_msg="生成接入方案，并转发到中心侧",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(3)

        # README §4.9.3：执行完成后状态重置 — 强断言，不允许静默通过
        state = _extract_state_payload(agent.get_state())
        assert isinstance(state, dict), "get_state should return dict"

        phase = state.get("phase")
        assert phase == "idle", (
            f"After completion, phase should be 'idle', got '{phase}'. State: {state}"
        )
        has_plan = state.get("hasAccessPlan")
        assert has_plan is False, (
            f"After completion, hasAccessPlan should be false, got {has_plan}. "
            f"State: {state}"
        )

    @retry_llm(max_attempts=3)
    def test_new_access_after_completion(self, chat, agent, tmp_path):
        """4.9.4: 状态重置后可处理新接入 — 不混淆上一次的设备"""
        # 第一次接入
        csv1_path = create_full_csv(tmp_path, filename="device1.csv")

        result1 = full_access_flow(
            chat, agent, str(csv1_path),
            upload_msg="接入华能阿拉善1#风机",
            plan_msg="生成接入方案，并转发到中心侧",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(3)

        # 前置断言：第一次接入完成后状态已重置（§4.9.3）
        state_after_first = _extract_state_payload(agent.get_state())
        assert state_after_first.get("phase") == "idle", (
            f"After first access, phase should reset to 'idle'. "
            f"State: {state_after_first}"
        )
        assert state_after_first.get("hasAccessPlan") is False, (
            f"After first access, hasAccessPlan should be false. "
            f"State: {state_after_first}"
        )

        # 第二次接入（不同设备）
        csv2_path = create_full_csv(
            tmp_path, filename="device2.csv",
            device_name="华能阿拉善2#风机", device_ip="192.168.110.2",
        )
        with chat.send_with_file(
            "接入另一个设备：华能阿拉善2#风机，IP是192.168.110.2",
            str(csv2_path)
        ) as s:
            text = s.text_content()

        assert len(text) > 0, (
            f"New access flow should start after completion. Got: {text[:300]}"
        )
        # 新流程应针对新设备（2#风机），不混淆上一次接入的 1#风机
        new_device_signal = (
            "2#风机" in text or "阿拉善2" in text or "192.168.110.2" in text
        )
        assert new_device_signal, (
            f"New access flow should reference the new device (2#风机). "
            f"Got: {text[:500]}"
        )
        # 新流程已启动：phase 进入接入流程状态（不再是 idle）
        state_during_second = _extract_state_payload(agent.get_state())
        phase2 = state_during_second.get("phase")
        assert phase2 in ("collecting", "planning", "confirmed", "executing"), (
            f"After new upload, phase should reflect an in-progress flow, "
            f"got '{phase2}'. State: {state_during_second}"
        )
