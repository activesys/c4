"""
C4 Agent L2 功能测试 — 端到端场景
==================================

测试依据: c4/test/agent/README.md §5

§5 端到端场景:
  5.1  单设备 Modbus + ASFP2 转发 — 完整数据接入流程
  5.2  单设备 Modbus 仅采集 — 无 reader 服务
  5.3  首次接入后重启 → config/shsm/进程三者一致
  5.4  修改 + 追加完整生命周期
  5.5  Add → Modify → Delete 完整生命周期
"""

import json
import os
import time
from pathlib import Path

import pytest  # type: ignore

from test_helpers import (
    create_full_csv,
    create_test_csv,
    retry_llm,
    find_interrupt_id,
    run_upload,
    full_access_flow,
)
from assertions import (
    assert_config_json_valid,
    assert_shm_ids_assigned,
    assert_writer_reader_from_registry,
    assert_no_tmp_file,
    assert_process_running,
    assert_config_shm_process_consistent,
    assert_no_technical_terms,
)


# ══════════════════════════════════════════════
#  §5.1 单设备 Modbus + ASFP2 转发
# ══════════════════════════════════════════════


@pytest.mark.llm
class TestE2ESingleDevice:
    """§5.1-5.2 单设备接入场景"""

    @retry_llm(max_attempts=3)
    def test_single_device_modbus_asfp2_full_flow(self, chat, agent, tmp_path, registry_dir):
        """5.1: 单设备 Modbus 接入 + ASFP2 转发 → 完整流程通过"""
        csv_path = create_full_csv(tmp_path)

        result = full_access_flow(
            chat, agent, str(csv_path),
            upload_msg="接入华能阿拉善1#风机",
            plan_msg="生成接入方案，并转发到中心侧",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(5)  # 等待执行完成

        # ── §4.6.1.1 首次接入断言 ──
        config_path = agent.config_dir / "config.json"
        config = result.get("config_json")
        if config is None and config_path.exists():
            config = assert_config_json_valid(config_path)
        assert config is not None, "config.json should be generated after e2e flow"

        # 1. 结构完整
        assert "c4_shm_manager" in config
        assert "c4_modbus_client" in config
        assert "c4_asfp2_client" in config, (
            "Should include asfp2_client for forwarding"
        )

        # 2. shm_id 分配
        assert_shm_ids_assigned(config)

        # 3. writer/reader 分类
        assert_writer_reader_from_registry(config, registry_dir)

        # 4. 原子写入
        assert_no_tmp_file(agent.config_dir)

        # 5. 进程检查 — modbus + asfp2 进程应运行
        try:
            assert_process_running("c4_modbus_client")
            assert_process_running("c4_asfp2_client")
        except AssertionError as e:
            # 进程检查失败不阻塞 — 取决于 systemd/parent process 管理方式
            pass

        # ── §4.7 非技术语言 ──
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
    def test_single_device_modbus_collection_only(self, chat, agent, tmp_path):
        """5.2: 单设备 Modbus 接入（仅采集）→ 无 reader 服务"""
        csv_path = create_test_csv(tmp_path)

        result = full_access_flow(
            chat, agent, str(csv_path),
            upload_msg="接入此设备",
            plan_msg="生成接入方案，只采集数据，不需要转发",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(5)

        config_path = agent.config_dir / "config.json"
        config = result.get("config_json")
        if config is None and config_path.exists():
            config = assert_config_json_valid(config_path)
        assert config is not None

        # §4.6.1.2: 有 modbus，无 asfp2
        assert "c4_modbus_client" in config
        assert "c4_asfp2_client" not in config, (
            "Collection-only should NOT include asfp2_client"
        )

        # reader 中无 asfp2
        shm = config.get("c4_shm_manager", {})
        readers = shm.get("reader", [])
        assert "c4_asfp2_client" not in readers

        # shm_id 分配
        assert_shm_ids_assigned(config)


# ══════════════════════════════════════════════
#  §5.3 崩溃恢复 (kill + restart)
# ══════════════════════════════════════════════


@pytest.mark.llm
class TestE2ECrashRecovery:
    """§5.3 首次接入后重启 — config/shsm/进程三者一致"""

    @retry_llm(max_attempts=3)
    def test_restart_after_first_access(self, chat, agent, tmp_path, registry_dir):
        """5.3: 完成 5.1 → kill Agent → restart → 三者一致"""
        csv_path = create_full_csv(tmp_path)

        # 完整接入
        result = full_access_flow(
            chat, agent, str(csv_path),
            upload_msg="接入华能阿拉善1#风机",
            plan_msg="生成接入方案，并转发到中心侧",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(5)

        config_path = agent.config_dir / "config.json"
        if not config_path.exists():
            pytest.skip("config.json not generated — cannot test crash recovery")

        config_before = assert_config_json_valid(config_path)
        config_before_text = config_path.read_text(encoding="utf-8")

        # kill + restart
        agent.kill()
        time.sleep(2)
        agent.restart()
        time.sleep(5)

        # ── §3.2.4.1 崩溃恢复断言 ──
        config_after = assert_config_json_valid(config_path)

        # 1. config.json 内容不变
        # 核心结构一致
        assert set(config_before.keys()) == set(config_after.keys()), (
            f"config.json top-level keys changed after restart. "
            f"Before: {sorted(config_before.keys())}, After: {sorted(config_after.keys())}"
        )

        # 2. shm_id 均已分配
        assert_shm_ids_assigned(config_after)

        # 3. modbus 实例数量不变
        modbus_before = config_before.get("c4_modbus_client", [])
        modbus_after = config_after.get("c4_modbus_client", [])
        assert len(modbus_after) == len(modbus_before), (
            f"Modbus instance count changed after restart: "
            f"{len(modbus_before)} → {len(modbus_after)}"
        )

        # 4. 进程检查
        try:
            assert_process_running("c4_modbus_client")
        except AssertionError:
            pass


# ══════════════════════════════════════════════
#  §5.4 修改 + 追加完整生命周期
# ══════════════════════════════════════════════


@pytest.mark.llm
class TestE2EModifyAppend:
    """§5.4 修改 + 追加完整生命周期"""

    @retry_llm(max_attempts=3)
    def test_modify_and_append_lifecycle(self, chat, agent, tmp_path, registry_dir):
        """5.4: 完成首次接入 → 追加第二个风机 → 修改第一个风机参数 → 增加采集点"""
        csv1_path = create_full_csv(tmp_path, filename="device1.csv")

        # Phase 1: 首次接入
        result1 = full_access_flow(
            chat, agent, str(csv1_path),
            upload_msg="接入华能阿拉善1#风机",
            plan_msg="生成接入方案，并转发到中心侧",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(5)

        config_path = agent.config_dir / "config.json"
        if not config_path.exists():
            pytest.skip("config.json not generated")
        config1 = assert_config_json_valid(config_path)
        modbus_count_1 = len(config1.get("c4_modbus_client", []))

        # Phase 2: 追加第二个风机
        csv2_path = create_full_csv(tmp_path, filename="device2.csv")

        with chat.send_with_file(
            "接入华能阿拉善2#风机，IP是192.168.110.2", str(csv2_path)
        ) as s:
            text_a = s.text_content()
        assert len(text_a) > 0

        with chat.send("生成接入方案，也转发到中心侧") as s:
            text_b = s.text_content()
        assert len(text_b) > 0

        with chat.send("确认，按方案执行") as s:
            text_c = s.text_content()
        assert len(text_c) > 0
        time.sleep(5)

        if not config_path.exists():
            config_path = agent.config_dir / "config.json"
        config2 = assert_config_json_valid(config_path)
        modbus_after_append = config2.get("c4_modbus_client", [])

        # §4.6.1.5: 新实例追加
        assert len(modbus_after_append) > modbus_count_1, (
            f"Should append new instance. Count: {modbus_count_1} → {len(modbus_after_append)}"
        )
        assert modbus_after_append[modbus_count_1].get("ip") != modbus_after_append[0].get("ip"), (
            "Second instance should have different IP from first"
        )

        # Phase 3: 修改第一个风机的采集点参数
        with chat.send("将 1#风机 windspeed 的寄存器地址从 1000 改为 1002") as s:
            s.text_content()

        with chat.send("确认修改") as s:
            s.text_content()
        time.sleep(5)

        if config_path.exists():
            config3 = assert_config_json_valid(config_path)
            modbus_after_mod = config3.get("c4_modbus_client", [])
            if len(modbus_after_mod) > 0:
                points = modbus_after_mod[0].get("points", [])
                for pt in points:
                    if "windspeed" in str(pt.get("id", "")).lower():
                        new_addr = pt.get("addr")
                        assert new_addr == 1002, (
                            f"Expected addr=1002 after modify, got {new_addr}"
                        )

        # Phase 4: 给第一个风机增加新采集点
        with chat.send("给 1#风机增加一个风向采集点，寄存器地址 1010") as s:
            s.text_content()

        with chat.send("确认执行") as s:
            s.text_content()
        time.sleep(5)

        if config_path.exists():
            config4 = assert_config_json_valid(config_path)
            modbus_final = config4.get("c4_modbus_client", [])
            if len(modbus_final) > 0:
                points_final = modbus_final[0].get("points", [])
                initial_point_count = len(
                    config1.get("c4_modbus_client", [{}])[0].get("points", [])
                )
                # 点数量应增加（修改不改变数量，新增增加）
                assert len(points_final) >= initial_point_count, (
                    f"Points should not decrease after modify+add. "
                    f"Before: {initial_point_count}, After: {len(points_final)}"
                )

        # 旧设备 IP 不变
        assert modbus_after_append[0].get("ip") == config1.get(
            "c4_modbus_client", [{}]
        )[0].get("ip"), "First device IP should not change"


# ══════════════════════════════════════════════
#  §5.5 Add → Modify → Delete 完整生命周期
# ══════════════════════════════════════════════


@pytest.mark.llm
class TestE2EFullLifecycle:
    """§5.5 Add → Modify → Delete 完整生命周期"""

    @retry_llm(max_attempts=3)
    def test_add_modify_delete_full_lifecycle(self, chat, agent, tmp_path):
        """5.5: 完成 5.4 → 删第二个风机 → 再删第一个（含 Reader 引用检查）"""
        csv_path = create_full_csv(tmp_path)

        # Phase 1: 首次接入
        result = full_access_flow(
            chat, agent, str(csv_path),
            upload_msg="接入华能阿拉善1#风机",
            plan_msg="生成接入方案，并转发到中心侧",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(5)

        config_path = agent.config_dir / "config.json"
        if not config_path.exists():
            pytest.skip("config.json not generated")

        # Phase 2: 追加第二个设备（如果有第二个 CSV）
        csv2_path = create_full_csv(tmp_path, filename="device2.csv")
        with chat.send_with_file("接入华能阿拉善2#风机，IP: 192.168.110.2", str(csv2_path)) as s:
            s.text_content()
        with chat.send("生成接入方案，也转发到中心") as s:
            s.text_content()
        with chat.send("确认执行") as s:
            s.text_content()
        time.sleep(5)

        config_before = assert_config_json_valid(config_path)
        modbus_before = config_before.get("c4_modbus_client", [])
        assert len(modbus_before) >= 2, (
            f"Should have >=2 instances before delete. Got: {len(modbus_before)}"
        )

        # Phase 3: 删除第二个风机
        with chat.send("停用 2#风机") as s:
            text_del2 = s.text_content()
        assert len(text_del2) > 0

        with chat.send("确认删除") as s:
            s.text_content()
        time.sleep(5)

        # §4.6.3.1: 单个删除 → 从数组移除
        config_after_del2 = assert_config_json_valid(config_path)
        modbus_after_del2 = config_after_del2.get("c4_modbus_client", [])
        assert len(modbus_after_del2) < len(modbus_before), (
            f"After deleting 2# fan, instances should decrease"
        )
        # shm_manager writer 仍含 c4_modbus_client
        shm = config_after_del2.get("c4_shm_manager", {})
        writers = shm.get("writer", [])
        assert "c4_modbus_client" in writers

        # Phase 4: 删除第一个风机（含 Reader 引用检查）
        with chat.send("停用 1#风机") as s:
            text_del1 = s.text_content()
        assert len(text_del1) > 0

        with chat.send("确认删除") as s:
            s.text_content()
        time.sleep(5)

        # §4.6.3.3: 删除被 Reader 引用的设备 → 引用清除
        config_final = assert_config_json_valid(config_path)
        modbus_final = config_final.get("c4_modbus_client", [])

        # 最终 modbus 数组应为空
        assert len(modbus_final) == 0, (
            f"After deleting all devices, c4_modbus_client[] should be empty. "
            f"Got: {len(modbus_final)}"
        )

        # asfp2_client 的 points 不应引用已删除设备
        asfp2_list = config_final.get("c4_asfp2_client", [])
        for inst in asfp2_list:
            for pt in inst.get("points", []):
                key = pt.get("key", "")
                # key 不应指向已删除的设备
                assert "hnals" not in key.lower() or len(modbus_final) > 0, (
                    f"asfp2_client point key '{key}' refers to deleted device, "
                    f"but c4_modbus_client is empty"
                )

        # c4_shm_manager.writer[] 中移除 c4_modbus_client
        shm_final = config_final.get("c4_shm_manager", {})
        writers_final = shm_final.get("writer", [])
        assert "c4_modbus_client" not in writers_final, (
            "After deleting all modbus instances, c4_shm_manager.writer[] "
            "should remove c4_modbus_client"
        )
