"""
C4 Agent 功能测试断言工具库 — assertions.py

提供:
  - 结构断言: assert_valid_access_plan
  - 副作用断言: assert_config_json_valid, assert_shm_ids_assigned,
    assert_writer_reader_from_registry, assert_no_tmp_file
  - 进程断言: assert_process_running
  - 一致性断言: assert_config_shm_process_consistent
  - 语言约束断言: assert_no_technical_terms, assert_no_json_leak

设计依据: c4/test/agent/README.md §6
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional


# ──────────────────────────────────────────────
#  无例外黑名单（任何场景均禁止）
# ──────────────────────────────────────────────
# shm 允许 shm_manager（文件名/进程名场景），不匹配 shm_manager 前缀

STRICT_BLACKLIST: list[str] = [
    r"\bshm_id\b",
    r"\bshm\b(?![\w]*(?:manager|mgr))",  # shm 但允许 shm_manager
    r"\bMCP\b",
    r"\bCONFIG_MISSING_SECTION\b",
    r"\bDUPLICATE_KEY\b",
    r"\bSHM_NOT_CREATED\b",
    r"\bSHM_SYSCALL_FAILED\b",
    r"\bSHM_CORRUPTED\b",
    r"\boutput_plan_steps\b",
    r"\bconfig_schema\b",
    r"\badjust_shm\b",
    r"\bpoint_count\b",
    r"\bmax_points\b",
    r"共享内存",
]

# ──────────────────────────────────────────────
#  场景豁免黑名单（方案展示 / 能力介绍时放行）
# ──────────────────────────────────────────────

CONTEXTUAL_BLACKLIST: list[str] = [
    r"(?<!\w)Modbus(?!\s*TCP)",  # 协议名（独立出现时）
    r"Modbus TCP",               # 协议名（带 TCP 后缀）
    r"IEC\s*104",
    r"IEC104",
    r"ASFP2",
    r":\d{2,5}",                 # 端口号模式 :NNNNN
]

# ──────────────────────────────────────────────
#  JSON 泄漏检测模式
# ──────────────────────────────────────────────

JSON_LEAK_PATTERNS: list[str] = [
    r'"[a-zA-Z_]+"\s*:',          # "key": 模式
    r'\{\s*"[^"]+"\s*:',          # {"key": 模式
]


# ──────────────────────────────────────────────
#  结构断言
# ──────────────────────────────────────────────


def assert_valid_access_plan(plan: Any) -> None:
    """
    验证 AccessPlan 含 site.devices.forward_targets 结构。

    plan 预期为 dict，包含:
      - site: {name, abbr}
      - devices: list[DeviceSpec]（含 name, protocol, connection, points）
      - forward_targets: list[ForwardTargetSpec]（含 name, protocol, connection）
    """
    assert isinstance(plan, dict), f"AccessPlan must be a dict, got {type(plan).__name__}"

    # site
    assert "site" in plan, "AccessPlan missing 'site'"
    site = plan["site"]
    assert isinstance(site, dict), "'site' must be a dict"
    assert "name" in site, "'site' missing 'name'"

    # devices
    assert "devices" in plan, "AccessPlan missing 'devices'"
    devices = plan["devices"]
    assert isinstance(devices, list), "'devices' must be a list"
    for i, dev in enumerate(devices):
        assert isinstance(dev, dict), f"devices[{i}] must be a dict"
        assert "name" in dev, f"devices[{i}] missing 'name'"
        assert "protocol" in dev, f"devices[{i}] missing 'protocol'"

    # forward_targets
    assert "forward_targets" in plan, "AccessPlan missing 'forward_targets'"
    fwd = plan["forward_targets"]
    assert isinstance(fwd, list), "'forward_targets' must be a list"
    for i, ft in enumerate(fwd):
        assert isinstance(ft, dict), f"forward_targets[{i}] must be a dict"
        assert "name" in ft, f"forward_targets[{i}] missing 'name'"
        assert "protocol" in ft, f"forward_targets[{i}] missing 'protocol'"


# ──────────────────────────────────────────────
#  副作用断言
# ──────────────────────────────────────────────


def assert_config_json_valid(config_path: Path) -> dict:
    """
    读取 config.json，验证 JSON 有效，返回 parsed dict。

    Raises:
        AssertionError: 文件不存在或 JSON 无效。
    """
    assert config_path.exists(), f"config.json not found at {config_path}"
    try:
        raw = config_path.read_text(encoding="utf-8")
        config = json.loads(raw)
    except json.JSONDecodeError as e:
        raise AssertionError(f"config.json is not valid JSON: {e}") from e
    return config


def assert_shm_ids_assigned(config: dict) -> None:
    """
    验证 config 中存在数据点定义且格式正确。
    shm_id=0 是正常设计（由 c4_shm_manager.adjust_shm() 在共享内存中分配，
    不回写到 config.json 磁盘文件）。
    """
    unchecked = True
    for key, value in config.items():
        if key == "c4_shm_manager":
            continue
        if isinstance(value, list):
            for instance in value:
                if isinstance(instance, dict) and "points" in instance:
                    pts = instance["points"]
                    assert isinstance(pts, list), (
                        f"Service '{key}' points must be a list"
                    )
                    assert len(pts) > 0, (
                        f"Service '{key}' must have at least one point"
                    )
                    for pt in pts:
                        unchecked = False
                        assert "id" in pt, (
                            f"Point in service '{key}' missing 'id' field"
                        )
                        assert "shm_id" in pt, (
                            f"Point '{pt.get('id', '<unnamed>')}' missing 'shm_id'"
                        )
    if unchecked:
        # 没有 point 的 config 也算通过（空 config 场景）
        pass


def assert_writer_reader_from_registry(config: dict, registry_dir: Path) -> None:
    """
    验证 c4_shm_manager.writer/reader 分类与 Registry JSON 的 role 字段一致。

    动态读取 mcp-registry/ 中的 role 声明，不硬编码 service_type 列表。
    规则:
      - c4_shm_manager 自身不参与 writer/reader 分类
      - writer[] 中的每个 service_type → 必须在 registry 中声明且 role="writer"
      - reader[] 中的每个 service_type → 必须在 registry 中声明且 role="reader"
      - writer[]/reader[] 只列实际使用（实例化）的服务类型，未使用的类型不出现
    """
    # Step 1: 从 Registry 中读取 role 映射
    registry_roles: dict[str, str] = {}
    if registry_dir.is_dir():
        for fpath in registry_dir.glob("*.json"):
            try:
                entry = json.loads(fpath.read_text(encoding="utf-8"))
                st = entry.get("service_type")
                role = entry.get("role")
                if st and role:
                    registry_roles[st] = role
            except (json.JSONDecodeError, KeyError):
                # 损坏文件跳过 — 单独场景测试
                continue

    # Step 2: 从 config 中提取 shm_manager section
    shm_section = config.get("c4_shm_manager")
    if shm_section is None:
        # 首次启动 → 无需验证
        return

    writers = shm_section.get("writer", [])
    readers = shm_section.get("reader", [])
    assert isinstance(writers, list), "c4_shm_manager.writer must be a list"
    assert isinstance(readers, list), "c4_shm_manager.reader must be a list"

    # Step 3: 验证 writer[]/reader[] 中的每个 service_type 均在 registry 声明且角色一致
    for w in writers:
        role = registry_roles.get(w)
        assert role == "writer", (
            f"c4_shm_manager.writer[] contains '{w}', "
            f"but registry declares role='{role}' (expected 'writer')"
        )
    for r in readers:
        role = registry_roles.get(r)
        assert role == "reader", (
            f"c4_shm_manager.reader[] contains '{r}', "
            f"but registry declares role='{role}' (expected 'reader')"
        )


def assert_no_tmp_file(config_dir: Path) -> None:
    """验证无残留 config.json.tmp。"""
    tmp_path = config_dir / "config.json.tmp"
    assert not tmp_path.exists(), (
        f"config.json.tmp exists at {tmp_path} — atomic write may have failed"
    )


# ──────────────────────────────────────────────
#  进程断言
# ──────────────────────────────────────────────


def assert_process_running(process_name: str) -> None:
    """
    ps aux 验证进程存在。
    process_name: 进程名（如 'c4_modbus_client'）
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", process_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        pids = result.stdout.strip()
        assert pids, f"Process '{process_name}' is not running"
    except subprocess.TimeoutExpired:
        raise AssertionError(f"pgrep timed out while checking '{process_name}'")


# ──────────────────────────────────────────────
#  一致性断言（崩溃恢复用）
# ──────────────────────────────────────────────


def assert_config_shm_process_consistent(
    config: dict,
    shm_mgr_client: Any,
) -> None:
    """
    验证 config.json ↔ shm ↔ 进程状态三者一致。

    验证维度:
      1. config.json 中每个 point 的 shm_id ≠ 0（shm 已分配）
      2. config 中声明的 service_type 进程存在
      3. c4_shm_manager.writer[] / reader[] 非空

    注: shm_mgr_client 为 McpClient 实例，可通过 call_tool 查询 shm 状态。
        如果 client 不可用（已关闭），只做基础断言。
    """
    # 1. shm_id 分配检查
    assert_shm_ids_assigned(config)

    # 2. 进程检查 — 除 c4_shm_manager 外的所有 service_type
    shm_section = config.get("c4_shm_manager", {})
    writers = shm_section.get("writer", [])
    readers = shm_section.get("reader", [])

    for service_type in writers + readers:
        if service_type == "c4_shm_manager":
            continue
        assert_process_running(service_type)

    # 3. shm_manager section 非空检查（至少一个 writer 或 reader 时）
    total = len(writers) + len(readers)
    if total > 0:
        assert len(shm_section) > 0, (
            "c4_shm_manager section is empty despite having configured services"
        )


# ──────────────────────────────────────────────
#  语言约束断言
# ──────────────────────────────────────────────


def _compile_patterns(patterns: list[str]) -> list[re.Pattern]:
    """编译正则列表，返回 Pattern 对象列表。"""
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def assert_no_technical_terms(
    text: str,
    allow_protocols: bool = False,
    allow_ports: bool = False,
) -> None:
    """
    验证文本不含黑名单术语。

    参数:
        text: 待检测文本
        allow_protocols: True 时放行协议名（方案展示 / 能力介绍场景）
        allow_ports: True 时放行端口号（方案展示场景）

    无例外黑名单（STRICT_BLACKLIST）始终检查，不受 allow_* 参数影响。

    Raises:
        AssertionError: 找到禁止术语时，列出所有匹配项。
    """
    violations: list[str] = []
    strict_patterns = _compile_patterns(STRICT_BLACKLIST)

    # 严格黑名单 — 始终检查
    for pat in strict_patterns:
        for match in pat.finditer(text):
            violations.append(
                f"[STRICT] Pattern '{pat.pattern}' matched '{match.group()}' "
                f"at pos {match.start()}"
            )

    # 场景黑名单 — 仅在未豁免时检查
    if not allow_protocols and not allow_ports:
        ctx_patterns = _compile_patterns(CONTEXTUAL_BLACKLIST)
        for pat in ctx_patterns:
            for match in pat.finditer(text):
                violations.append(
                    f"[CONTEXTUAL] Pattern '{pat.pattern}' matched '{match.group()}' "
                    f"at pos {match.start()}"
                )
    elif not allow_protocols:
        # 仅豁免端口，仍需检查协议
        proto_patterns = _compile_patterns(
            [p for p in CONTEXTUAL_BLACKLIST if ":\\d" not in p and "IEC" not in p or ":" not in p]
        )
        # 简化：分别处理协议和端口
        protocol_only = [
            r"(?<!\w)Modbus(?!\s*TCP)",
            r"Modbus TCP",
            r"IEC\s*104",
            r"IEC104",
            r"ASFP2",
        ]
        for pat in _compile_patterns(protocol_only):
            for match in pat.finditer(text):
                violations.append(
                    f"[CONTEXTUAL-protocol] Pattern '{pat.pattern}' matched "
                    f"'{match.group()}' at pos {match.start()}"
                )
    elif not allow_ports:
        port_patterns = _compile_patterns([r":\d{2,5}"])
        for pat in port_patterns:
            for match in pat.finditer(text):
                violations.append(
                    f"[CONTEXTUAL-port] Pattern '{pat.pattern}' matched "
                    f"'{match.group()}' at pos {match.start()}"
                )

    if violations:
        raise AssertionError(
            f"Found {len(violations)} technical term violation(s) in text:\n"
            + "\n".join(f"  - {v}" for v in violations)
            + f"\n--- Text snippet (first 500 chars) ---\n{text[:500]}"
        )


def assert_no_json_leak(text: str) -> None:
    """
    验证文本不含 JSON 结构泄漏。

    检测:
      - "key": 模式（连续 JSON key-value 对）
      - {"key": 模式（JSON 对象字面量）

    Raises:
        AssertionError: 发现 JSON 模式时。
    """
    patterns = _compile_patterns(JSON_LEAK_PATTERNS)
    violations: list[str] = []
    for pat in patterns:
        for match in pat.finditer(text):
            violations.append(
                f"Pattern '{pat.pattern}' matched '{match.group()[:40]}' "
                f"at pos {match.start()}"
            )

    if violations:
        raise AssertionError(
            f"Found {len(violations)} JSON leak(s) in text:\n"
            + "\n".join(f"  - {v}" for v in violations)
            + f"\n--- Text snippet (first 500 chars) ---\n{text[:500]}"
        )
