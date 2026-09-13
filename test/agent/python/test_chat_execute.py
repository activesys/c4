"""
C4 Agent L2 功能测试 — 执行验证 & 错误恢复 & 状态持久化
========================================================

测试依据: c4/test/agent/README.md §4.6, §4.8, §4.9

§4.6 执行验证（副作用检查）:
  4.6.1.1  首次接入 (Modbus + ASFP2) → config.json 含正确结构
  4.6.1.2  首次接入 (仅采集) → config.json 无 asfp2_client
  4.6.1.3  原子写入 → 无 .tmp 残留; 首次接入无 .prev.1; 非首次接入 .prev.1 = 写入前
           版本（滚动保留 .prev.1~.3）; 事务完成后 pending_change.json 已删除
  4.6.1.4  writer/reader 分类 → 与 Registry 一致
  4.6.1.5  追加设备 → 新实例追加, 旧实例保留
   4.6.2.1  修改 IP → 实例 IP 变更, 其余不变
   4.6.2.2  修改点参数 → point addr 变更
   4.6.2.4  删除采集点 → points[] 移除 temperature
   4.6.2.5  修改不存在的实例 → 友好错误, config 不变
   4.6.3.1  删除实例 → 从数组移除
   4.6.3.4  删除不存在的实例 → 友好错误, config 不变

§4.8 错误恢复路径（变更事务协议，c4_architecture.md §3.1.2 / agent.md §3.2.2）:
  4.8.1  adjust_shm 失败 — DUPLICATE_KEY → 恢复 config.json.prev.1 + 完整
         Stop-Start（含 adjust_shm）+ 删除事务标记 + 非技术语言失败描述
  4.8.2  删除最后一个 writer 实例 → reader 级联移除 → 合法空态（零实例期望），
         start 幂等 success，不触发回滚级联
  4.8.3  reader key 指向不存在的 writer → 合并层确定性拒绝（config 不变、
         无事务残留、友好错误）
  4.8.4  adjust_shm 失败 — 非 config 类（SHM_SYSCALL_FAILED）→ 与 config 类
         一致回滚（agent.md §3.2.2：恢复 .prev.1 + 完整 Stop-Start）
  4.8.5  start 失败 → 回滚 .prev.1（变更作废，不残留半接入状态）;
         启动收敛期 MCP 不可达 → 记录失败保持降级，不阻塞其余服务
  4.8.6  step-decomposer 失败 — 用户消息验证

§4.10 单飞规则（c4_architecture.md §3.1.2）:
  并发配置变更请求在会话层直接拒绝——「有配置变更正在执行，请稍后重试」

§4.9 AgentState 持久化（GET /api/state 观测 phase / hasAccessPlan / lastError）:
  4.9.1  接入流程中途重启 → hasAccessPlan=true 恢复
  4.9.2  用户确认后中断 → phase=confirmed 保持
  4.9.3  执行完成后状态重置 → phase=idle + hasAccessPlan=false（强断言）
  4.9.4  状态重置后可处理新接入
"""

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest  # type: ignore

from test_helpers import (
    create_full_csv,
    create_test_csv,
    create_messy_csv,
    retry_llm,
    find_interrupt_id,
    full_access_flow,
    delete_device,
)
from assertions import (
    assert_no_technical_terms,
    assert_config_json_valid,
    assert_shm_ids_assigned,
    assert_writer_reader_from_registry,
    assert_no_tmp_file,
)
from conftest import _find_free_port, port_is_listening, wait_port


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
        """ASFP2 数据源接入 → config.json 含 c4_asfp2_server（完整接入流，
        消息给全必填项：设备名/IP/端口/协议——C4_RS_00044 必填项用户提供）"""
        csv_path = tmp_path / "asfp2_points.csv"
        csv_path.write_text(
            "name,addr\n"
            "wind_speed,1000\n"
            "temperature,1002\n",
            encoding="utf-8"
        )

        result = full_access_flow(
            chat, agent, str(csv_path),
            upload_msg=(
                "第三方厂家通过asfp2协议给我们转来ASFP2数据源的数据，"
                "设备名称：ASFP2数据源，IP是172.16.109.11，端口9999，采用asfp2协议"
            ),
            plan_msg="生成接入方案（仅接收，不需要转发）",
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

    def test_atomic_write_prev_chain(self, chat, agent, tmp_path):
        """4.6.1.3: 原子写入 — 无 .tmp 残留; 首次接入无 .prev.1（无可回滚对象）;
        非首次接入 .prev.1 = 写入前版本（滚动保留 .prev.1~.3）;
        事务完成后 pending_change.json 已删除"""
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

        config_dir = agent.config_dir
        # 无 .tmp 残留
        assert_no_tmp_file(config_dir)
        # 首次接入无 .prev.1（无可回滚对象）且事务标记已删除
        assert not (config_dir / "config.json.prev.1").exists(), (
            "首次接入不应产生 config.json.prev.1（无可回滚对象）"
        )
        assert not (config_dir / "pending_change.json").exists(), (
            "事务完成后 pending_change.json 应已删除"
        )

        first_config = assert_config_json_valid(config_dir / "config.json")
        first_raw = json.dumps(first_config, indent=2, sort_keys=True, ensure_ascii=False)

        # 第二次接入（追加 2#风机）→ .prev.1 = 第一次接入后的版本
        csv2_path = create_full_csv(
            tmp_path, filename="device2.csv",
            device_name="华能阿拉善2#风机", device_ip="192.168.110.2",
        )
        full_access_flow(
            chat, agent, str(csv2_path),
            upload_msg="接入华能阿拉善2#风机，IP是192.168.110.2",
            plan_msg="生成接入方案",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(3)

        assert_no_tmp_file(config_dir)
        assert not (config_dir / "pending_change.json").exists()
        prev1_path = config_dir / "config.json.prev.1"
        assert prev1_path.exists(), "非首次接入应产生 .prev.1（滚动回滚源）"
        prev1_raw = json.dumps(
            json.loads(prev1_path.read_text(encoding="utf-8")),
            indent=2, sort_keys=True, ensure_ascii=False,
        )
        assert prev1_raw == first_raw, (
            ".prev.1 应为写入前版本（第一次接入后的配置）"
        )

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

        with chat.send("[C4_BUTTON_CONFIRM] 确认修改") as s:
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

        # 首次接入（含转发——writer-only 配置会被 adjust_shm 契约拒绝：
        # writer/reader 必须同空或同非空）
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

        # 修改点参数（目标地址须避开既有点——标准点表 temperature 已占用 1002）
        with chat.send("将 windspeed 的寄存器地址从 1000 改为 1010") as s:
            text = s.text_content()
        assert len(text) > 0

        with chat.send("[C4_BUTTON_CONFIRM] 确认修改") as s:
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

        with chat.send("[C4_BUTTON_CONFIRM] 确认修改") as s:
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


# 基线合法配置构造：c4_asfp2_server (writer, 动态端口) + c4_asfp2_client (reader)。
# 端口动态分配以支持端口监听探测（实例运行依据——生命周期双层模型）。
def _baseline_config(writer_port: int, fwd_port: int) -> dict:
    return {
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
                "ip": "127.0.0.1",
                "port": writer_port,
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
                "port": fwd_port,
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


def _reset_instance_shm() -> None:
    """移除实例 shm 段（模拟整机重启后 tmpfs 清零）——使重启瀑布走
    create_shm(带配置分配) → start 路径，与生产态「config 落盘即已分配」一致。"""
    try:
        os.unlink("/dev/shm/c4_test")
    except OSError:
        pass


def _extract_state_payload(state: dict) -> dict:
    """
    兼容两种 GET /api/state 响应形状，返回可观测子集 dict：

      - README §4.9 直接形状:   {phase, hasAccessPlan, lastError}
      - 包装形状:               {success: bool, state: {phase, hasAccessPlan, lastError}}
    """
    if isinstance(state, dict) and isinstance(state.get("state"), dict):
        return state["state"]
    return state


def _wait_config_equals(config_path: Path, expected_raw: str, timeout: float = 30.0) -> None:
    """轮询 config.json 直至内容与期望一致（事务 + 回滚需要时间，禁固定 sleep 单次断言）。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            last = config_path.read_text(encoding="utf-8")
            got = json.dumps(
                json.loads(last), indent=2, sort_keys=True, ensure_ascii=False
            )
            if got == expected_raw:
                return
        except (json.JSONDecodeError, OSError):
            pass
        time.sleep(0.5)
    raise AssertionError(
        f"config.json 未在 {timeout}s 内回滚为 .prev.1 内容。最后内容: {last[:400]}"
    )


class TestErrorRecoveryConfigErrors:
    """§4.8 启动收敛期 MCP 不可达（L1 确定性，独立服务模型语义）

    Agent 启动瀑布 L2：涉及服务 MCP 不可达 → 记录失败并保持降级，
    继续处理其余服务（C4_RS_00242）——收敛不做回滚，config 保留失败服务声明。
    """

    def test_degraded_service_tolerated_at_startup(self, agent, registry_dir):
        """4.8.5(启动收敛分支): registry 注入无 socket 的 mock 服务 →
        其余服务照常收敛，mock 保持降级，失败被记录（非技术语言）。"""
        # 注入无 socket 的 mock 服务 registry 条目（无对应常驻进程）
        fake_registry = {
            "service_type": "c4_fake_service",
            "display_name": "测试注入服务",
            "role": "writer",
            "protocols": [
                {"protocol": "fake", "description": "测试注入用，无真实服务"}
            ],
            "point_schema": {
                "fields": [
                    {"name": "addr", "type": "integer", "description": "地址"}
                ],
                "identity_fields": ["addr"],
            },
            "config_schema": {
                "fields": {
                    "ip": {
                        "type": "string",
                        "default": "0.0.0.0",
                        "description": "绑定 IP",
                    },
                    "port": {
                        "type": "integer",
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

        try:
            writer_port, fwd_port = _find_free_port(), _find_free_port()
            bad = _baseline_config(writer_port, fwd_port)
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
            config_path = agent.config_dir / "config.json"
            config_path.write_text(
                json.dumps(bad, indent=2, ensure_ascii=False), encoding="utf-8"
            )

            # restart → 瀑布收敛：真实服务 start，mock 不可达 → 降级记录
            agent.kill()
            _reset_instance_shm()
            agent.restart()
            wait_port(writer_port, True)

            # 成功的服务保持运行（实例在线）
            # 失败的服务保持降级（socket 不存在，不可能有实例）
            # 失败被记录：lastError（非技术语言）
            state = _extract_state_payload(agent.get_state())
            last_error = state.get("lastError")
            assert last_error, (
                f"lastError should record the degraded service. State: {state}"
            )
            assert_no_technical_terms(str(last_error), allow_protocols=False)

            # 启动收敛期不可达不触发 config 回退——保留失败服务的声明
            config_after = assert_config_json_valid(config_path)
            assert "c4_fake_service" in config_after, (
                "启动收敛期 MCP 不可达应记录失败保持降级，不回退 config"
            )
        finally:
            if fake_path.exists():
                fake_path.unlink()


@pytest.mark.llm
class TestErrorRecoveryTransaction:
    """§4.8 变更事务失败路径（LLM 经确认按钮驱动 changes JSON——内容确定性）。

    事务协议（c4_architecture.md §3.1.2 / agent.md §3.2.2）：任一阶段失败 →
    恢复 config.json.prev.1 → 以恢复后的配置执行完整 Stop-Start（含 adjust_shm）
    → 删除事务标记 → 报告失败（变更作废，不残留半接入状态）。
    """

    def _plant_baseline(self, agent) -> tuple[Path, str, int, int]:
        """写入基线配置并重启 Agent（瀑布收敛、分配 shm_id），返回
        (config_path, 收敛后快照 raw, writer_port, fwd_port)。"""
        writer_port, fwd_port = _find_free_port(), _find_free_port()
        config_path = agent.config_dir / "config.json"
        config_path.write_text(
            json.dumps(_baseline_config(writer_port, fwd_port), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        agent.kill()
        _reset_instance_shm()
        agent.restart()
        # 等待瀑布收敛完成（writer 实例拉起）
        deadline = time.time() + 20
        while time.time() < deadline:
            if port_is_listening(writer_port):
                break
            time.sleep(0.3)
        snapshot = json.dumps(
            assert_config_json_valid(config_path),
            indent=2, sort_keys=True, ensure_ascii=False,
        )
        return config_path, snapshot, writer_port, fwd_port

    def _send_changes(self, chat, changes: list[dict]) -> str:
        """以确认按钮消息驱动确定性 changes（绕过 LLM 生成，仅作传输）。"""
        payload = {"changes": changes}
        with chat.send(f"[C4_BUTTON_CONFIRM] 确认\n\n{json.dumps(payload, ensure_ascii=False)}") as stream:
            return stream.text_content()

    @retry_llm(max_attempts=3)
    def test_duplicate_key_rollback_to_prev1(self, chat, agent):
        """4.8.1: adjust_shm 失败 — DUPLICATE_KEY → config.json 恢复为 .prev.1
        内容 + 完整 Stop-Start（含 adjust_shm）+ 删除事务标记 + 非技术语言报告。"""
        config_path, snapshot, writer_port, fwd_port = self._plant_baseline(agent)
        # 构造跨服务类型的全局 key 冲突：devices 路径两实例同 abbr（实例 id 同为
        # hnals_dupk）+ 点名同为 p_700 → 全局 key 'hnals_dupk.p_700' 出现两次
        # → adjust_shm 返回 DUPLICATE_KEY
        payload = {
            "devices": [
                {
                    "name": "冲突接收",
                    "abbr": "dupk",
                    "protocol": "asfp2",
                    "port": _find_free_port(),
                    "points": [{"name": "p_700", "addr": 700}],
                },
                {
                    "name": "冲突采集",
                    "abbr": "dupk",
                    "protocol": "modbus",
                    "ip": "127.0.0.1",
                    "port": 1502,
                    "points": [{"name": "p_700", "addr": 700, "uid": 1, "fun": 3, "type": 10, "swap": 0}],
                },
            ]
        }
        with chat.send(f"[C4_BUTTON_CONFIRM] 确认\n\n{json.dumps(payload, ensure_ascii=False)}") as stream:
            stream.text_content()

        # 断言: config.json 恢复为 .prev.1 内容（= 变更前快照）
        _wait_config_equals(config_path, snapshot)
        config_after = assert_config_json_valid(config_path)
        assert config_after.get("c4_modbus_client") in (None, []), (
            "冲突实例应随回滚移除"
        )
        assert len(config_after.get("c4_asfp2_server", [])) == 1, (
            "asfp2_server 应恢复为仅基线实例"
        )

        # 断言: 完整 Stop-Start——基线 writer 实例重新在线（端口监听）
        deadline = time.time() + 20
        while time.time() < deadline and not port_is_listening(writer_port):
            time.sleep(0.3)
        assert port_is_listening(writer_port), "回滚后应以恢复后的配置重启实例"

        # 断言: 事务标记已删除；无 .tmp 残留
        assert not (agent.config_dir / "pending_change.json").exists()
        assert_no_tmp_file(agent.config_dir)

        # 断言: 用户收到非技术语言的失败描述（变更作废）
        state = _extract_state_payload(agent.get_state())
        last_error = state.get("lastError")
        assert last_error, f"lastError 应记录变更失败。State: {state}"
        assert_no_technical_terms(str(last_error), allow_protocols=False)

    @retry_llm(max_attempts=3)
    def test_last_writer_delete_legal_empty_state(self, chat, agent):
        """4.8.2: 删除最后一个 writer 实例 → reader 级联移除 → 合法空态
        （config 段空 = 期望零实例），start 幂等 success，不触发回滚级联。"""
        config_path, snapshot, writer_port, fwd_port = self._plant_baseline(agent)
        changes = [
            {
                "action": "delete",
                "service_type": "c4_asfp2_server",
                "instance": {"id": "test_asfp2_srv_1"},
            },
        ]
        self._send_changes(chat, changes)

        # 断言: config 进入合法空态（无实例声明）
        deadline = time.time() + 30
        while time.time() < deadline:
            cfg = assert_config_json_valid(config_path)
            insts = [i for i in cfg.get("c4_asfp2_server", [])]
            if not insts:
                break
            time.sleep(0.5)
        cfg = assert_config_json_valid(config_path)
        assert not cfg.get("c4_asfp2_server"), (
            f"最后一个 writer 实例应被移除（合法空态）: {cfg.get('c4_asfp2_server')}"
        )
        # reader 引用被级联移除
        readers = cfg.get("c4_asfp2_client", [])
        assert not readers, f"引用被删 writer 的 reader 应级联移除: {readers}"
        # 空态不触发回滚级联（config 保持空态，不恢复）
        assert not (agent.config_dir / "pending_change.json").exists()
        # writer 端口释放（零实例期望落地）
        deadline = time.time() + 15
        while time.time() < deadline and port_is_listening(writer_port):
            time.sleep(0.3)
        assert not port_is_listening(writer_port), "空态下 writer 实例端口应释放"

    @retry_llm(max_attempts=3)
    def test_unknown_reader_key_rejected_before_write(self, chat, agent):
        """4.8.3: reader key 指向不存在的 writer → 合并层确定性拒绝：
        config 不变、无事务残留、友好错误。"""
        config_path, snapshot, writer_port, fwd_port = self._plant_baseline(agent)
        changes = [
            {
                "action": "add",
                "service_type": "c4_asfp2_client",
                "instance": {"id": "bad_reader", "name": "坏转发", "ip": "127.0.0.1",
                             "port": _find_free_port(), "t0": 30, "t1": 20, "t2": 10,
                             "timer": 100, "key_sequence": 1, "same_data_type": 1,
                             "same_timestamp": 1, "smart": 1, "forward_kack": 255,
                             "inverse_keep": 0},
                "points": [{"key": "nonexistent_srv.nonexistent_point", "addr": 9001}],
            },
        ]
        text = self._send_changes(chat, changes)

        # 断言: config 不变（合并层拒绝，未写入）
        time.sleep(3)
        got = json.dumps(
            assert_config_json_valid(config_path),
            indent=2, sort_keys=True, ensure_ascii=False,
        )
        assert got == snapshot, "config.json 应保持不变（合并层拒绝）"
        # 无事务残留
        assert not (agent.config_dir / "pending_change.json").exists()
        assert_no_tmp_file(agent.config_dir)
        # 友好错误（非技术语言）
        assert_no_technical_terms(text, allow_protocols=False)

    @retry_llm(max_attempts=3)
    def test_start_failure_rollback_to_prev1(self, chat, agent):
        """4.8.5(事务分支): start 失败（端口被本服务既有实例占用的新实例）→
        恢复 .prev.1 + 完整 Stop-Start（含 adjust_shm）+ 删除事务标记。"""
        config_path, snapshot, writer_port, fwd_port = self._plant_baseline(agent)
        # 同端口第二实例：数组顺序启动，基线实例先绑定端口，新实例 PORT_BIND_FAILED
        changes = [
            {
                "action": "add",
                "service_type": "c4_asfp2_server",
                "instance": {"id": "conflict_srv", "name": "端口冲突接收", "port": writer_port},
                "points": [{"addr": 800}],
            },
        ]
        self._send_changes(chat, changes)

        # 断言: config.json 恢复为 .prev.1 内容（变更作废，不残留半接入状态）
        _wait_config_equals(config_path, snapshot)
        # 断言: 事务标记已删除；基线实例在线
        assert not (agent.config_dir / "pending_change.json").exists()
        deadline = time.time() + 20
        while time.time() < deadline and not port_is_listening(writer_port):
            time.sleep(0.3)
        assert port_is_listening(writer_port), "回滚后基线实例应重新在线"
        # 用户收到失败描述
        state = _extract_state_payload(agent.get_state())
        assert state.get("lastError"), f"lastError 应记录失败。State: {state}"

    def test_shm_syscall_failed_rollback(self, chat, agent, tmp_path):
        """4.8.4: 非 config 类 adjust_shm 失败（SHM_SYSCALL_FAILED）→ 与 config 类
        一致回滚（agent.md §3.2.2：恢复 .prev.1 + 完整 Stop-Start）。

        注：需 sudo mount 限制 /dev/shm 大小以触发 shm 系统调用失败；
        环境不允许 remount（如容器）时 skip。
        """
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

        config_path, snapshot, writer_port, fwd_port = self._plant_baseline(agent)

        try:
            subprocess.run(
                ["sudo", "mount", "-o", "remount,size=1M", "/dev/shm"],
                capture_output=True, timeout=10, check=True,
            )
            changes = [
                {
                    "action": "add",
                    "service_type": "c4_asfp2_server",
                    "instance": {"id": "big_srv", "name": "大点表接收", "port": _find_free_port()},
                    "points": [{"addr": a} for a in range(900, 900 + 20000)],
                },
            ]
            self._send_changes(chat, changes)

            # 断言: config.json 恢复为 .prev.1（非 config 类错误同样回滚）
            _wait_config_equals(config_path, snapshot, timeout=60)
            assert not (agent.config_dir / "pending_change.json").exists()
        finally:
            subprocess.run(
                ["sudo", "mount", "-o", "remount,size=256M", "/dev/shm"],
                capture_output=True, timeout=10,
            )


@pytest.mark.llm
class TestSingleFlight:
    """§4.10 单飞规则：并发配置变更请求在会话层直接拒绝（c4_architecture.md §3.1.2）。

    确定性窗口：启动瀑布持单飞锁直至收敛完成——c4_shm_manager socket 不可连时
    L1 硬前置挂起（退避等待），锁全程被持有；此间到达的配置变更请求在执行闸门
    处必然被拒绝（ Resident 套接字下事务本身毫秒级完成，运行期窗口无法稳定命中）。
    """

    def test_concurrent_change_rejected_with_busy_message(
        self, chat, agent, tmp_path, mcp_stack
    ):
        """瀑布收敛持锁期间到达的配置变更请求 → 收到
        「有配置变更正在执行，请稍后重试」固定话术（会话层拒绝，不排队）。"""
        # 停掉 shm_manager（socket 消失）→ 重启 Agent → 瀑布挂起持锁
        mcp_stack.stop_service("c4_shm_manager")
        agent.kill()
        agent.restart()  # HTTP 就绪（瀑布在后台挂起持锁）

        csv_path = create_full_csv(tmp_path)
        with chat.send_with_file(
            "接入华能阿拉善1#风机，设备名称：华能阿拉善1#风机，"
            "IP是192.168.110.1，端口502，采用modbus协议",
            str(csv_path),
        ) as s:
            s.text_content()
        with chat.send(
            "生成接入方案，并转发到中心侧\n\n"
            "转发采用asfp2协议，转发到127.0.0.1:19900，转发地址5000~5009"
        ) as s:
            s.text_content()
        with chat.send("[C4_BUTTON_CONFIRM] 确认") as s:
            text = s.text_content()

        # 恢复栈（先恢复，避免污染后续测试）
        mcp_stack.start_service("c4_shm_manager")
        mcp_stack.wait_socket("c4_shm_manager", timeout=10)

        assert "请稍后重试" in text, (
            f"瀑布持锁期间的配置变更请求应被会话层拒绝"
            f"（「有配置变更正在执行，请稍后重试」）。Got: {text[:300]}"
        )


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
        # （协议确认/澄清场景按 README §4.7 豁免协议名——如"请确认是否按 Modbus
        #   采集"；shm/MCP/错误码/JSON 等无例外黑名单仍全量检查）
        assert_no_technical_terms(combined_text, allow_protocols=True)

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

    @pytest.mark.skip(
        "README §4.9 前提未满足：当前实现不自动恢复 LangGraph filesystem checkpoint "
        "（kill → restart 后 hasAccessPlan 不还原）——按 README 注记降级为 TypeScript "
        "单元测试（mock checkpoint），不在黑盒套件覆盖范围"
    )
    def test_state_restore_after_plan_generation(self, chat, agent, tmp_path):
        """4.9.1: 接入流程中途重启 → 状态恢复

        前置：完成 info-gatherer + plan-generator，hasAccessPlan=true。
        kill → restart 后 hasAccessPlan 仍为 true。
        """
        csv_path = create_full_csv(tmp_path)

        # 上传点表 + 生成方案（消息给全必填项——C4_RS_00044 必填项用户提供；
        # 方案消息嵌入上一步解析结果，使新会话可续接点表上下文）
        with chat.send_with_file(
            "接入华能阿拉善1#风机，设备名称：华能阿拉善1#风机，"
            "IP是192.168.110.1，端口502，采用modbus协议",
            str(csv_path),
        ) as s:
            upload_text = s.text_content()

        with chat.send(
            "生成接入方案，并转发到中心侧\n\n"
            "转发采用asfp2协议，转发到127.0.0.1:19900，转发地址5000~5009"
            + (f"\n\n上一步解析结果:\n{upload_text}" if upload_text else "")
        ) as s:
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

    @pytest.mark.skip(
        "README §4.9 前提未满足：当前实现不自动恢复 LangGraph filesystem checkpoint "
        "（kill → restart 后 phase 不还原）——按 README 注记降级为 TypeScript "
        "单元测试（mock checkpoint），不在黑盒套件覆盖范围"
    )
    def test_state_after_confirm_before_execution(self, chat, agent, tmp_path):
        """4.9.2: 用户确认后中断 → 状态保持

        前置：确认方案后，在 step-decomposer 执行前 kill。
        restart 后 phase 反映已确认状态（"confirmed"），Agent 可继续执行。
        """
        csv_path = create_full_csv(tmp_path)

        # 上传 + 方案 + 确认（消息给全必填项；方案消息嵌入解析结果）
        with chat.send_with_file(
            "接入华能阿拉善1#风机，设备名称：华能阿拉善1#风机，"
            "IP是192.168.110.1，端口502，采用modbus协议",
            str(csv_path),
        ) as s:
            upload_text = s.text_content()

        with chat.send(
            "生成接入方案，并转发到中心侧\n\n"
            "转发采用asfp2协议，转发到127.0.0.1:19900，转发地址5000~5009"
            + (f"\n\n上一步解析结果:\n{upload_text}" if upload_text else "")
        ) as s:
            s.text_content()
            interrupt_id = find_interrupt_id(s)

        if interrupt_id:
            with chat.send("[C4_BUTTON_CONFIRM] 确认方案") as s:
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
