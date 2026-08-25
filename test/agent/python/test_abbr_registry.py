"""
C4 Agent 功能测试 — id 稳定性（abbr 记忆库，§4.6.4）
====================================================

测试依据: c4/test/agent/README.md §4.6.4

核心不变式: 同一采集/转发目标的 instance.id 跨会话稳定——首次接入固化，
后续 modify/delete/加点复用已存 id，不重新提取 abbr。
实例 id 格式: {site_abbr}_{target_abbr}（如 hnals_wt1）。

记忆库 abbr_registry.json（agent 内部状态，位于 agent.config_dir，
~/.local/c4/ 等效路径）:

    {
        "site": {"name": "华能阿拉善", "abbr": "hnals"},
        "entries": [
            {"id": "hnals_wt1", "name": "1#风机", "abbr": "wt1",
             "service_type": "c4_modbus_client", "role": "writer",
             "description": "采集 1#风机的数据"}
        ]
    }

L1 用例（无 LLM 依赖，可用 `pytest -m "not llm"` 单独运行）:
  4.6.4.8  entries 丢失 → 从 config.json 重建
  4.6.4.9  site 丢失 → 重新询问场站（entries 保留不重建）
  4.6.4.12 记忆库损坏 JSON → 不崩溃 + 从 config.json 重建 entries

L2 用例（@pytest.mark.llm，需要 DEEPSEEK_API_KEY，无 key 自动 skip）:
  4.6.4.1  首次接入固化 id + 写入记忆库
  4.6.4.2  modify 复用同一 id（跨会话稳定）
  4.6.4.3  同一设备加点 → 合并，不新建实例
  4.6.4.4  不同设备撞 abbr → 重新生成
  4.6.4.5  delete 物理删除记忆
  4.6.4.6  删除后 abbr 释放可复用
  4.6.4.7  modify/delete 目标不在记忆库
  4.6.4.10 site 首次询问固化
  4.6.4.11 场站归属校验（三态）

断言面: abbr_registry.json 文件内容（确定性副作用）+ config.json 的 instance.id 稳定性。
abbr 提取是 LLM 非确定性操作，故 4.6.4.4 断言「id 不同且不覆盖旧实例」，而非精确 abbr 值。
记忆库写入/删除（固化）与 entries 重建为确定性代码执行，可通过预构造
abbr_registry.json + config.json 绕过 LLM 做 L1 级精确断言（L1 用例 4.6.4.8/9/12）。
"""

import json
import time
from pathlib import Path
from typing import Any, Callable, Optional

import pytest  # type: ignore

from assertions import assert_config_json_valid
from conftest import write_config_json
from test_helpers import (
    create_full_csv,
    delete_device,
    full_access_flow,
    modify_device,
    retry_llm,
)

# ──────────────────────────────────────────────
#  常量与测试数据
# ──────────────────────────────────────────────

_SITE_NAME = "华能阿拉善"
_SITE_ABBR = "hnals"
_WT1_ID = "hnals_wt1"
_WT1_NAME = "1#风机"
_WT1_ABBR = "wt1"

# L1 用例预构造的 config.json：已接入 hnals_wt1（1#风机）
_CONFIG_WITH_WT1: dict = {
    "c4_shm_manager": {
        "instance_id": "c4_test",
        "max_points": 100000,
        "writer": ["c4_modbus_client"],
        "reader": [],
    },
    "c4_modbus_client": [
        {
            "id": _WT1_ID,
            "name": _WT1_NAME,
            "ip": "192.168.110.1",
            "port": 502,
            "points": [
                {
                    "id": "hnals_wt1_windspeed",
                    "name": "windspeed",
                    "addr": 1000,
                    "shm_id": 0,
                },
                {
                    "id": "hnals_wt1_temperature",
                    "name": "temperature",
                    "addr": 1002,
                    "shm_id": 0,
                },
            ],
        }
    ],
}


# ──────────────────────────────────────────────
#  文件系统断言辅助
# ──────────────────────────────────────────────


def _read_json(path: Path) -> Optional[dict]:
    """读取 JSON 文件；不存在或损坏返回 None。"""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _read_abbr_registry(config_dir: Path) -> Optional[dict]:
    """读取 abbr_registry.json；不存在或损坏返回 None。"""
    return _read_json(config_dir / "abbr_registry.json")


def _read_agent_config(config_dir: Path) -> Optional[dict]:
    """读取 agent.json；不存在或损坏返回 None。"""
    return _read_json(config_dir / "agent.json")


def _load_config(config_dir: Path) -> Optional[dict]:
    """读取 config.json；不存在返回 None，损坏则断言失败。"""
    config_path = config_dir / "config.json"
    if not config_path.exists():
        return None
    return assert_config_json_valid(config_path)


def _modbus_ids(config: dict) -> list[str]:
    """返回 config.json 中 c4_modbus_client 实例的 id 列表（保持顺序）。"""
    instances = config.get("c4_modbus_client", [])
    return [
        str(inst.get("id"))
        for inst in instances
        if isinstance(inst, dict) and inst.get("id")
    ]


def _registry_entry_ids(config_dir: Path) -> list[str]:
    """返回 abbr_registry.json 中 entries 的 id 列表。"""
    registry = _read_abbr_registry(config_dir)
    if registry is None:
        return []
    return [
        str(e.get("id"))
        for e in registry.get("entries", [])
        if isinstance(e, dict) and e.get("id")
    ]


def _registry_with_entries(config_dir: Path) -> Optional[dict]:
    """记忆库有效且 entries 非空时返回 dict，否则 None。"""
    registry = _read_abbr_registry(config_dir)
    if registry is not None and registry.get("entries"):
        return registry
    return None


def _agent_config_with_site(config_dir: Path) -> Optional[dict]:
    """agent.json 已固化 site 字段时返回 agent 配置，否则 None。"""
    agent_cfg = _read_agent_config(config_dir)
    if agent_cfg is not None and agent_cfg.get("site"):
        return agent_cfg
    return None


def _registry_without_entry(config_dir: Path, entry_id: str) -> bool:
    """记忆库有效（site 保留）且不含 entry_id 记录时返回 True。"""
    registry = _read_abbr_registry(config_dir)
    if registry is None:
        return False
    return all(e.get("id") != entry_id for e in registry.get("entries", []))


def _config_with_id(config_dir: Path, instance_id: str) -> Optional[dict]:
    """config.json 含指定 instance.id 时返回 config，否则 None。"""
    config = _load_config(config_dir)
    if config is not None and instance_id in _modbus_ids(config):
        return config
    return None


def _config_with_new_instance(
    config_dir: Path, old_id: str, min_count: int
) -> Optional[dict]:
    """config.json 保留 old_id 且 modbus 实例数 >= min_count 时返回 config。"""
    config = _load_config(config_dir)
    if config is None:
        return None
    ids = _modbus_ids(config)
    if old_id in ids and len(ids) >= min_count:
        return config
    return None


def _wait_until(
    predicate: Callable[[], Any],
    timeout: float = 30.0,
    interval: float = 0.5,
    desc: str = "",
) -> Optional[Any]:
    """轮询 predicate 直到返回真值；超时返回 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return None


def _phase_is_idle(agent: Any) -> bool:
    """GET /api/state 的 phase 是否为 idle（执行阶段结束）。"""
    try:
        state = agent.get_state()
    except Exception:
        return False
    return isinstance(state, dict) and state.get("phase") == "idle"


def _wait_execution_idle(agent: Any, timeout: float = 20.0) -> None:
    """等待执行阶段结束（phase == idle），尽力同步确定性副作用。"""
    _wait_until(
        lambda: _phase_is_idle(agent),
        timeout=timeout,
        interval=0.5,
        desc="phase==idle",
    )


# ══════════════════════════════════════════════
#  §4.6.4 id 稳定性 — L1 记忆库恢复（无 LLM 依赖）
# ══════════════════════════════════════════════
#
# 记忆库固化/entries 重建为确定性代码执行（agent.md §3.2.1.3a），
# 通过预构造 abbr_registry.json + config.json + 重启 Agent 触发，无需 LLM。
# 运行方式: pytest -m "not llm" 可单独运行本类。


class TestAbbrRegistryRecovery:
    """§4.6.4.8 / 4.6.4.9 / 4.6.4.12 记忆库丢失与损坏恢复（L1）。

    断言面：abbr_registry.json 文件内容 + config.json 副作用，
    不依赖 LLM 推理输出。
    """

    def test_entries_missing_rebuild_from_config(
        self, agent: Any, abbr_registry: Callable[..., Path]
    ) -> None:
        """4.6.4.8: entries 丢失 → 重启后从 config.json 重建 entries。

        预期：id/name 恢复，abbr 由 id 反推（去 {site_abbr}_ 前缀），
        description 退化为 name；site 保留。
        """
        write_config_json(agent.config_dir, _CONFIG_WITH_WT1)
        abbr_registry("entries_missing")

        agent.kill()
        agent.restart()

        registry = _wait_until(
            lambda: _registry_with_entries(agent.config_dir),
            timeout=30.0,
            desc="entries 重建",
        )
        assert registry is not None, (
            "abbr_registry.json 未从 config.json 重建 entries"
        )

        # site 保留在 agent.json（entries_missing 模式只删 entries）
        agent_cfg = _agent_config_with_site(agent.config_dir)
        assert agent_cfg is not None, (
            "entries 重建后 site 应保留在 agent.json"
        )
        site = agent_cfg.get("site", {})
        assert site.get("name") == _SITE_NAME, (
            f"site.name 应保留为 {_SITE_NAME}，实际: {site}"
        )
        assert site.get("abbr") == _SITE_ABBR, (
            f"site.abbr 应保留为 {_SITE_ABBR}，实际: {site}"
        )

        # entries 从 config.json 恢复
        by_id = {e.get("id"): e for e in registry.get("entries", [])}
        assert _WT1_ID in by_id, (
            f"entries 应包含 {_WT1_ID}，实际: {list(by_id)}"
        )
        entry = by_id[_WT1_ID]
        assert entry.get("name") == _WT1_NAME, (
            f"name 应恢复为 {_WT1_NAME}，实际: {entry}"
        )
        assert entry.get("abbr") == _WT1_ABBR, (
            f"abbr 应由 id 反推为 {_WT1_ABBR}，实际: {entry.get('abbr')}"
        )
        assert entry.get("description") == _WT1_NAME, (
            f"description 应退化为 name（{_WT1_NAME}），实际: "
            f"{entry.get('description')}"
        )

    def test_site_missing_entries_preserved(
        self, agent: Any, abbr_registry: Callable[..., Path]
    ) -> None:
        """4.6.4.9: site 缺失（agent.json 无 site）→ 重新询问场站，entries 保留不重建。

        L1 断言面（确定性）：
        - Agent 不崩溃（GET /api/services 就绪）；
        - abbr_registry.json 不被删除，entries 与预构造内容完全一致（不重建）；
        - agent.json 的 site 字段不凭空重建（维持缺失 → Agent 必然重新询问场站）。
        """
        write_config_json(agent.config_dir, _CONFIG_WITH_WT1)
        abbr_registry("site_missing")
        entries_before = (_read_abbr_registry(agent.config_dir) or {}).get(
            "entries", []
        )

        agent.kill()
        agent.restart()
        time.sleep(3)  # 留出启动恢复逻辑运行时间

        # Agent 不崩溃
        services = agent.get_services()
        assert isinstance(services, list), "site 缺失后 Agent 应保持就绪"

        registry = _read_abbr_registry(agent.config_dir)
        assert registry is not None, (
            "site 缺失后 abbr_registry.json 不应被删除"
        )
        agent_cfg = _read_agent_config(agent.config_dir)
        assert agent_cfg is not None, "agent.json 应存在"
        assert "site" not in agent_cfg, (
            "site 缺失后不应凭空重建 site（应重新询问场站）"
        )
        assert registry.get("entries", []) == entries_before, (
            "site 缺失时 entries 应保留不重建，"
            f"实际: {registry.get('entries')}"
        )

    def test_corrupted_registry_recovery(
        self, agent: Any, abbr_registry: Callable[..., Path]
    ) -> None:
        """4.6.4.12: 记忆库损坏 JSON → 不崩溃，从 config.json 重建 entries。

        site 存于 agent.json（权威配置），损坏只丢失 entries；entries 从
        config.json 重建，abbr 用 agent.json 的 site_abbr 反推（去前缀）。
        """
        write_config_json(agent.config_dir, _CONFIG_WITH_WT1)
        abbr_registry("corrupted")

        agent.kill()
        agent.restart()

        registry = _wait_until(
            lambda: _registry_with_entries(agent.config_dir),
            timeout=30.0,
            desc="损坏恢复",
        )
        assert registry is not None, (
            "损坏的 abbr_registry.json 未恢复为有效 JSON（Agent 不应崩溃）"
        )

        agent_cfg = _agent_config_with_site(agent.config_dir)
        assert agent_cfg is not None, (
            "损坏恢复后 site 应保留在 agent.json"
        )
        by_id = {e.get("id"): e for e in registry.get("entries", [])}
        assert _WT1_ID in by_id, (
            f"损坏恢复后 entries 应从 config.json 重建含 {_WT1_ID}，"
            f"实际: {list(by_id)}"
        )
        entry = by_id[_WT1_ID]
        assert entry.get("name") == _WT1_NAME
        assert entry.get("abbr") == _WT1_ABBR
        assert entry.get("description") == _WT1_NAME

# ══════════════════════════════════════════════
#  §4.6.4 id 稳定性 — L2 核心流程（需要 LLM）
# ══════════════════════════════════════════════


@pytest.mark.llm
class TestAbbrIdStability:
    """§4.6.4.1 ~ 4.6.4.7 id 稳定性核心流程（L2）。"""

    def _do_first_access(
        self,
        chat: Any,
        agent: Any,
        tmp_path: Path,
        filename: str = "device1.csv",
        upload_msg: str = "采集1#风机",
    ) -> None:
        """执行一次完整接入（1#风机），同步等待 config.json 生成 hnals_wt1。"""
        csv_path = create_full_csv(tmp_path, filename=filename)
        full_access_flow(
            chat,
            agent,
            str(csv_path),
            upload_msg=upload_msg,
            plan_msg="生成接入方案，并转发到中心侧",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(3)
        _wait_execution_idle(agent)

        config = _wait_until(
            lambda: _config_with_id(agent.config_dir, _WT1_ID),
            timeout=30.0,
            desc=f"config.json 含 {_WT1_ID}",
        )
        assert config is not None, (
            f"首次接入后 config.json 应生成 {_WT1_ID}"
        )

    @retry_llm(max_attempts=3)
    def test_first_access_fixes_id_and_writes_registry(
        self, chat: Any, agent: Any, tmp_path: Path
    ) -> None:
        """4.6.4.1: 首次接入固化 id + 写入记忆库。

        预期：config.json 生成 hnals_wt1；abbr_registry.json 出现
        {id: "hnals_wt1", abbr: "wt1"} 记录；site 固化。
        """
        self._do_first_access(chat, agent, tmp_path)

        # config.json 生成 hnals_wt1（_do_first_access 已断言）

        # abbr_registry.json 写入记忆（确定性固化）
        registry = _wait_until(
            lambda: _registry_with_entries(agent.config_dir),
            timeout=30.0,
            desc="记忆库写入",
        )
        assert registry is not None, (
            "首次接入后应写入 abbr_registry.json"
        )
        agent_cfg = _agent_config_with_site(agent.config_dir)
        assert agent_cfg is not None, (
            "首次接入后应写入 agent.json 的 site 字段"
        )
        site = agent_cfg.get("site", {})
        assert site.get("name") == _SITE_NAME
        assert site.get("abbr") == _SITE_ABBR
        by_id = {e.get("id"): e for e in registry.get("entries", [])}
        assert _WT1_ID in by_id, (
            f"记忆库应含 {_WT1_ID} 记录，实际: {list(by_id)}"
        )
        entry = by_id[_WT1_ID]
        assert entry.get("abbr") == _WT1_ABBR, (
            f"记忆库记录 abbr 应为 {_WT1_ABBR}，实际: {entry.get('abbr')}"
        )
        assert _WT1_NAME in entry.get("name", ""), (
            f"记忆库记录 name 应含 {_WT1_NAME}，实际: {entry.get('name')}"
        )

    @retry_llm(max_attempts=3)
    def test_modify_reuses_id_across_restart(
        self, chat: Any, agent: Any, tmp_path: Path
    ) -> None:
        """4.6.4.2: modify 复用同一 id（跨会话稳定）。

        4.6.4.1 后重启 Agent（模拟新会话），请求修改 1#风机 IP。
        预期：config.json 仍是 hnals_wt1，不重新提取 abbr，不产生新 id。
        """
        self._do_first_access(chat, agent, tmp_path)

        # 重启 Agent — 模拟跨会话
        agent.kill()
        agent.restart()

        modify_device(chat, agent, _WT1_NAME, "ip", "192.168.110.5")

        # IP 修改生效（说明 modify 作用于已有实例）
        config = _wait_until(
            lambda: self._config_ip_is(agent.config_dir, "192.168.110.5"),
            timeout=30.0,
            desc="IP 修改生效",
        )
        assert config is not None, "modify 后 IP 应变为 192.168.110.5"

        # 核心不变式：id 稳定，不产生新 id
        ids = _modbus_ids(config)
        assert ids == [_WT1_ID], (
            f"modify 应复用 {_WT1_ID}，不应产生新 id，实际: {ids}"
        )

    @retry_llm(max_attempts=3)
    def test_same_device_add_points_merges(
        self, chat: Any, agent: Any, tmp_path: Path
    ) -> None:
        """4.6.4.3: 同一设备加点 → 合并，不新建实例。

        4.6.4.1 后再次 add「采集1#风机」（复提 abbr=wt1 且描述匹配）。
        预期：判为同一设备，询问是否在 hnals_wt1 上加点 → 合并，
        c4_modbus_client[] 实例数不变。
        """
        self._do_first_access(chat, agent, tmp_path)
        config_before = _load_config(agent.config_dir)
        assert config_before is not None
        count_before = len(_modbus_ids(config_before))

        # 再次 add 同一设备
        csv_path = create_full_csv(tmp_path, filename="device1_again.csv")
        result = full_access_flow(
            chat,
            agent,
            str(csv_path),
            upload_msg="采集1#风机",
            plan_msg="生成接入方案",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(3)
        _wait_execution_idle(agent)

        config_after = _load_config(agent.config_dir)
        assert config_after is not None
        ids_after = _modbus_ids(config_after)
        assert len(ids_after) == count_before, (
            f"同一设备加点应合并，不新建实例。"
            f"Before: {count_before}, After: {len(ids_after)} ({ids_after})"
        )
        assert _WT1_ID in ids_after, (
            f"合并后应保留 {_WT1_ID}，实际: {ids_after}"
        )

        # 询问合并（LLM 文本，retry_llm 容忍非确定性）
        all_text = "\n".join(
            filter(
                None,
                [
                    result.get("upload_text", ""),
                    result.get("plan_text", ""),
                ],
            )
        )
        assert ("加点" in all_text) or (_WT1_ID in all_text), (
            "同一设备再次接入时应询问是否在已有实例上加点"
        )

    @retry_llm(max_attempts=3)
    def test_different_device_abbr_collision_regenerates(
        self, chat: Any, agent: Any, tmp_path: Path
    ) -> None:
        """4.6.4.4: 不同设备撞 abbr → 重新生成。

        已有 hnals_wt1（1#风机）→ 新 add「采集2#风机」（LLM 也可能提取成 wt1）。
        预期：生成不同 abbr（如 wt1_2）→ 新实例 hnals_wt1_2；hnals_wt1 不受影响。
        abbr 提取是 LLM 非确定性操作，故断言「id 不同且不覆盖旧实例」。
        """
        self._do_first_access(chat, agent, tmp_path)

        # 新 add 2#风机（与 1#风机 撞 abbr 风险场景）
        csv2_path = create_full_csv(
            tmp_path,
            filename="device2.csv",
            device_name="华能阿拉善2#风机",
            device_ip="192.168.110.2",
        )
        full_access_flow(
            chat,
            agent,
            str(csv2_path),
            upload_msg="采集2#风机",
            plan_msg="生成接入方案",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(3)
        _wait_execution_idle(agent)

        config = _wait_until(
            lambda: _config_with_new_instance(
                agent.config_dir, _WT1_ID, min_count=2
            ),
            timeout=30.0,
            desc="新实例生成",
        )
        assert config is not None, "2#风机接入后应新增一个 modbus 实例"

        ids = _modbus_ids(config)
        new_ids = [i for i in ids if i != _WT1_ID]
        assert len(new_ids) >= 1, f"应生成与 {_WT1_ID} 不同的新 id，实际: {ids}"
        new_id = new_ids[0]
        assert new_id != _WT1_ID, f"新实例 id 不应复用 {_WT1_ID}"
        assert new_id.startswith(_SITE_ABBR + "_"), (
            f"新实例 id 应带站点前缀 {_SITE_ABBR}_，实际: {new_id}"
        )

        # hnals_wt1 不受影响
        wt1_instances = [
            inst
            for inst in config.get("c4_modbus_client", [])
            if isinstance(inst, dict) and inst.get("id") == _WT1_ID
        ]
        assert len(wt1_instances) == 1, f"{_WT1_ID} 应完整保留"
        assert wt1_instances[0].get("ip") == "192.168.110.1", (
            f"{_WT1_ID} 的 ip 不应被 2#风机 接入影响"
        )

        # 记忆库：旧记录保留 + 新记录写入
        entry_ids = _registry_entry_ids(agent.config_dir)
        assert _WT1_ID in entry_ids, (
            f"记忆库应保留 {_WT1_ID}，实际: {entry_ids}"
        )
        assert new_id in entry_ids, (
            f"记忆库应写入新记录 {new_id}，实际: {entry_ids}"
        )

    @retry_llm(max_attempts=3)
    def test_delete_physically_removes_registry_entry(
        self, chat: Any, agent: Any, tmp_path: Path
    ) -> None:
        """4.6.4.5: delete 物理删除记忆。

        预期：config.json 移除 hnals_wt1；abbr_registry.json 中该记录
        物理删除（不保留历史）。
        """
        self._do_first_access(chat, agent, tmp_path)

        # 前置：记忆库已含 hnals_wt1
        registry_before = _wait_until(
            lambda: _registry_with_entries(agent.config_dir),
            timeout=30.0,
            desc="记忆库写入",
        )
        assert registry_before is not None
        assert _WT1_ID in _registry_entry_ids(agent.config_dir)

        # delete（确定性：嵌入 instance.id）
        delete_device(chat, agent, _WT1_NAME)

        # config.json 移除实例
        config = _wait_until(
            lambda: self._config_without_id(agent.config_dir, _WT1_ID),
            timeout=30.0,
            desc=f"config.json 移除 {_WT1_ID}",
        )
        assert config is not None, f"delete 后 config.json 应移除 {_WT1_ID}"

        # 记忆库物理删除
        removed = _wait_until(
            lambda: _registry_without_entry(agent.config_dir, _WT1_ID),
            timeout=30.0,
            desc=f"记忆库移除 {_WT1_ID}",
        )
        assert removed is True, (
            f"delete 后 abbr_registry.json 应物理删除 {_WT1_ID} 记录"
        )

    @retry_llm(max_attempts=3)
    def test_abbr_released_and_reusable_after_delete(
        self, chat: Any, agent: Any, tmp_path: Path
    ) -> None:
        """4.6.4.6: 删除后 abbr 释放可复用。

        4.6.4.5 后重新 add「采集1#风机」。
        预期：可用 wt1 生成新 hnals_wt1（无冲突，旧记录已删除）。
        """
        self._do_first_access(chat, agent, tmp_path)

        delete_device(chat, agent, _WT1_NAME)

        # 等待删除副作用完成（config 与记忆库均无 hnals_wt1）
        config_removed = _wait_until(
            lambda: self._config_without_id(agent.config_dir, _WT1_ID),
            timeout=30.0,
            desc=f"config.json 移除 {_WT1_ID}",
        )
        assert config_removed is not None
        registry_removed = _wait_until(
            lambda: _registry_without_entry(agent.config_dir, _WT1_ID),
            timeout=30.0,
            desc=f"记忆库移除 {_WT1_ID}",
        )
        assert registry_removed is True

        # 重新 add 1#风机 — abbr 已释放，可复用 wt1
        csv_path = create_full_csv(tmp_path, filename="device1_re_add.csv")
        full_access_flow(
            chat,
            agent,
            str(csv_path),
            upload_msg="采集1#风机",
            plan_msg="生成接入方案，并转发到中心侧",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(3)
        _wait_execution_idle(agent)

        config = _wait_until(
            lambda: _config_with_id(agent.config_dir, _WT1_ID),
            timeout=30.0,
            desc=f"重新生成 {_WT1_ID}",
        )
        assert config is not None, (
            f"删除后重新接入应可用 wt1 重新生成 {_WT1_ID}"
        )

    @retry_llm(max_attempts=3)
    def test_modify_delete_target_not_in_registry(
        self, agent: Any, chat: Any, abbr_registry: Callable[..., Path]
    ) -> None:
        """4.6.4.7: modify/delete 目标不在记忆库 → 报错且 config 不变。

        前置：记忆库含 hnals_wt1；请求 modify/delete 记忆库中无记录的
        设备（2#风机，从未接入过）。
        预期：info-gatherer 检索记忆库阶段即报错「目标不存在，可能已删除
        或从未接入」，不进入方案生成，不修改 config.json。
        """
        write_config_json(agent.config_dir, _CONFIG_WITH_WT1)
        abbr_registry("normal")
        agent.kill()
        agent.restart()

        config_path = agent.config_dir / "config.json"
        snapshot = config_path.read_text(encoding="utf-8")

        with chat.send("停用 2#风机") as stream:
            delete_text = stream.text_content()
        chat.record_response(delete_text)
        with chat.send("修改 2#风机的 IP") as stream:
            modify_text = stream.text_content()

        assert "不存在" in delete_text, (
            f"delete 目标不在记忆库应报错「目标不存在」，实际: {delete_text[:300]}"
        )
        assert "不存在" in modify_text, (
            f"modify 目标不在记忆库应报错「目标不存在」，实际: {modify_text[:300]}"
        )

        # config.json 不变（含 hnals_wt1 未被误删）
        config_after = json.loads(config_path.read_text(encoding="utf-8"))
        assert config_after == json.loads(snapshot), (
            "目标不在记忆库时不应对 config.json 做任何修改"
        )

    # ── 类内轮询谓词 ──

    @staticmethod
    def _config_ip_is(config_dir: Path, ip: str) -> Optional[dict]:
        """config.json 中首个 modbus 实例 ip 为指定值时返回 config。"""
        config = _load_config(config_dir)
        if config is None:
            return None
        instances = config.get("c4_modbus_client", [])
        if instances and instances[0].get("ip") == ip:
            return config
        return None

    @staticmethod
    def _config_without_id(config_dir: Path, instance_id: str) -> Optional[dict]:
        """config.json 存在且不含指定 instance.id 时返回 config。"""
        config = _load_config(config_dir)
        if config is not None and instance_id not in _modbus_ids(config):
            return config
        return None


# ══════════════════════════════════════════════
#  §4.6.4 id 稳定性 — L2 site 固化与归属校验
# ══════════════════════════════════════════════


@pytest.mark.llm
class TestAbbrSiteFlow:
    """§4.6.4.10 site 首次询问固化 + §4.6.4.11 场站归属校验（L2）。"""

    @staticmethod
    def _reload_with_registry(
        agent: Any, abbr_registry: Callable[..., Path]
    ) -> None:
        """写入 normal 记忆库并重启 Agent，使站点信息生效。"""
        abbr_registry("normal")
        agent.kill()
        agent.restart()

    @retry_llm(max_attempts=3)
    def test_first_start_asks_site_then_fixes(
        self, chat: Any, agent: Any, tmp_path: Path
    ) -> None:
        """4.6.4.10: site 首次询问固化。

        首次启动（无 abbr_registry.json，无 config.json）→ 发起接入。
        预期：Agent 询问场站名称+缩写；用户提供后写入 site 字段。
        """
        # 前置：agent fixture 默认无 config.json / abbr_registry.json
        assert not (agent.config_dir / "abbr_registry.json").exists()
        assert not (agent.config_dir / "config.json").exists()

        csv_path = create_full_csv(tmp_path)
        with chat.send_with_file("接入华能阿拉善1#风机", str(csv_path)) as stream:
            upload_text = stream.text_content()
        chat.record_response(upload_text)

        assert "场站" in upload_text, (
            "首次接入应询问场站名称+缩写，实际回复: "
            f"{upload_text[:300]}"
        )

        # 用户提供场站信息 → 确定性固化 site 字段
        with chat.send("场站名称：华能阿拉善，缩写：hnals") as stream:
            site_text = stream.text_content()
        chat.record_response(site_text)

        agent_cfg = _wait_until(
            lambda: _agent_config_with_site(agent.config_dir),
            timeout=30.0,
            desc="site 固化",
        )
        assert agent_cfg is not None, (
            "用户提供场站后应写入 agent.json 的 site 字段"
        )
        site = agent_cfg.get("site", {})
        assert site.get("name") == _SITE_NAME, (
            f"site.name 应为 {_SITE_NAME}，实际: {site}"
        )
        assert site.get("abbr") == _SITE_ABBR, (
            f"site.abbr 应为 {_SITE_ABBR}，实际: {site}"
        )

    @retry_llm(max_attempts=3)
    def test_attribution_defaults_to_current_site(
        self, chat: Any, agent: Any, tmp_path: Path, abbr_registry: Callable
    ) -> None:
        """4.6.4.11 ①: 点表无场站信息 → 默认当前场站。

        预期：接入流程按当前场站（华能阿拉善）进行，新实例 id
        带 hnals_ 前缀。
        """
        self._reload_with_registry(agent, abbr_registry)

        # 点表无场站信息（设备名不含场站前缀）
        csv_path = create_full_csv(
            tmp_path,
            filename="no_site_info.csv",
            device_name="3#箱变",
            device_ip="192.168.110.3",
        )
        full_access_flow(
            chat,
            agent,
            str(csv_path),
            upload_msg="接入这个设备",
            plan_msg="生成接入方案，并转发到中心侧",
            confirm=True,
            tmp_path=tmp_path,
        )
        time.sleep(3)
        _wait_execution_idle(agent)

        config = _wait_until(
            lambda: _load_config(agent.config_dir),
            timeout=30.0,
            desc="config.json 生成",
        )
        assert config is not None
        ids = _modbus_ids(config)
        assert ids, "应生成 modbus 实例"
        for instance_id in ids:
            assert instance_id.startswith(_SITE_ABBR + "_"), (
                f"无场站信息的点表应默认当前场站（{_SITE_ABBR}_ 前缀），"
                f"实际: {instance_id}"
            )

    @retry_llm(max_attempts=3)
    def test_attribution_ambiguous_asks_confirm(
        self, chat: Any, agent: Any, tmp_path: Path, abbr_registry: Callable
    ) -> None:
        """4.6.4.11 ②: 点表含场站信息但归属不明 → 提醒确认场站归属。"""
        self._reload_with_registry(agent, abbr_registry)

        csv_path = create_full_csv(
            tmp_path,
            filename="ambiguous_site.csv",
            device_name="阿拉善风电场5#机组",
            device_ip="192.168.110.5",
        )
        with chat.send_with_file("接入这个设备", str(csv_path)) as stream:
            text = stream.text_content()

        assert ("归属" in text) or ("确认" in text), (
            "归属不明的场站信息应提醒用户确认场站归属，实际回复: "
            f"{text[:300]}"
        )

    @retry_llm(max_attempts=3)
    def test_attribution_other_site_rejected(
        self, chat: Any, agent: Any, tmp_path: Path, abbr_registry: Callable
    ) -> None:
        """4.6.4.11 ③: 点表明确标注其他场站 → 提醒「不属于当前场站」。"""
        self._reload_with_registry(agent, abbr_registry)

        csv_path = create_full_csv(
            tmp_path,
            filename="other_site.csv",
            device_name="华能大青山1#风机",
            device_ip="192.168.110.9",
        )
        with chat.send_with_file("接入这个设备", str(csv_path)) as stream:
            text = stream.text_content()

        assert "不属于" in text, (
            "明确标注其他场站的点表应提醒「该资料不属于当前场站」，"
            f"实际回复: {text[:300]}"
        )

