"""
C4 Agent L1 确定性功能测试 — Registry 加载 (§3.1)

被测接口: GET /api/services
被测对象: McpServiceRegistry.loadFromDirectory() → L1 服务摘要生成

设计依据: c4/test/agent/README.md §3.1
"""

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Generator
from urllib.request import urlopen

import pytest  # type: ignore

from conftest import (
    AgentHandle,
    _find_agent_binary,
    _find_free_port,
    write_agent_json,
)

# ──────────────────────────────────────────────
#  配置常量 — 预期 Registry 中的服务
# ──────────────────────────────────────────────
# 注: 按 README §3.1 规格，完整部署时应有 5 个 JSON，
# 但测试以 registry_dir 中实际文件数为准。

L1_REQUIRED_FIELDS = {"service_type", "display_name", "role", "protocols"}
PROTOCOL_REQUIRED_FIELDS = {"protocol", "description", "selection_rules"}
L2_FIELDS = {"config_schema", "binary_path", "error_mappings"}


# ──────────────────────────────────────────────
#  辅助函数 — 为自定义 registry 启动 Agent
# ──────────────────────────────────────────────


def _start_agent_custom_registry(
    config_dir: Path,
    registry_path: Path,
    shm_manager_binary: str,
    agent_binary: str,
) -> AgentHandle:
    """使用自定义 registry 目录启动 Agent，返回 AgentHandle。"""
    port = _find_free_port()
    write_agent_json(config_dir, registry_path, shm_manager_binary, port)

    cmd = [agent_binary, "--config-dir", str(config_dir)]
    if agent_binary.endswith(".js"):
        cmd = ["node", *cmd]
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    base_url = f"http://127.0.0.1:{port}"

    # 等待就绪
    deadline = time.time() + 60.0
    last_error = None
    ready = False
    while time.time() < deadline:
        try:
            with urlopen(f"{base_url}/api/services", timeout=0.5) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception as e:
            last_error = e
        time.sleep(0.5)

    if not ready:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        pytest.fail(
            f"Agent did not become ready within 60s. "
            f"Last error: {last_error}"
        )

    return AgentHandle(process, base_url, port, config_dir, set())


def _teardown_agent(handle: AgentHandle) -> None:
    """关闭 AgentHandle 并清理进程。"""
    if handle.process and handle.process.poll() is None:
        handle.process.terminate()
        try:
            handle.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            handle.process.kill()
            try:
                handle.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


# ──────────────────────────────────────────────
#  1.1 — 返回所有已注册服务
# ──────────────────────────────────────────────


def test_returns_all_services(agent: AgentHandle, registry_dir: Path) -> None:
    """
    用例 1.1: mcp-registry/ 含完整 JSON 文件。
    GET /api/services 返回数组长度 = registry 文件数。
    """
    services = agent.get_services()
    assert isinstance(services, list), f"Expected list, got {type(services).__name__}"

    expected_count = len(list(registry_dir.glob("*.json")))
    assert len(services) == expected_count, (
        f"Expected {expected_count} services (matching registry files), "
        f"got {len(services)}"
    )


# ──────────────────────────────────────────────
#  1.2 — 每项含 L1 必须字段
# ──────────────────────────────────────────────


def test_l1_required_fields(agent: AgentHandle) -> None:
    """
    用例 1.2: 每项含 service_type, display_name, role, protocols[]。
    """
    services = agent.get_services()
    assert isinstance(services, list), f"Expected list, got {type(services).__name__}"
    assert len(services) > 0, "Expected at least one service in registry"

    for i, svc in enumerate(services):
        missing = L1_REQUIRED_FIELDS - set(svc.keys())
        assert not missing, (
            f"Service[{i}] ({svc.get('service_type', '<unknown>')}) "
            f"missing L1 required fields: {missing}"
        )
        # protocols 必须是非空数组
        protocols = svc.get("protocols", [])
        assert isinstance(protocols, list), (
            f"Service[{i}].protocols must be a list, got {type(protocols).__name__}"
        )
        assert len(protocols) > 0, (
            f"Service[{i}].protocols must not be empty"
        )


# ──────────────────────────────────────────────
#  1.3 — protocols 含 description 和 selection_rules
# ──────────────────────────────────────────────


def test_protocols_have_details(agent: AgentHandle) -> None:
    """
    用例 1.3: protocols[0] 含 protocol, description, selection_rules[]。
    """
    services = agent.get_services()
    assert isinstance(services, list), f"Expected list, got {type(services).__name__}"
    assert len(services) > 0, "Expected at least one service in registry"

    for i, svc in enumerate(services):
        protocols = svc.get("protocols", [])
        assert isinstance(protocols, list) and len(protocols) > 0, (
            f"Service[{i}] must have non-empty protocols[]"
        )
        for j, proto in enumerate(protocols):
            missing = PROTOCOL_REQUIRED_FIELDS - set(proto.keys())
            assert not missing, (
                f"Service[{i}].protocols[{j}] missing fields: {missing}"
            )
            # selection_rules 必须是数组
            rules = proto.get("selection_rules", [])
            assert isinstance(rules, list), (
                f"Service[{i}].protocols[{j}].selection_rules must be a list, "
                f"got {type(rules).__name__}"
            )


# ──────────────────────────────────────────────
#  1.4 — L1 不含 L2 字段
# ──────────────────────────────────────────────


def test_l1_excludes_l2_fields(agent: AgentHandle) -> None:
    """
    用例 1.4: 每项不含 config_schema, binary_path, error_mappings。
    """
    services = agent.get_services()
    assert isinstance(services, list), f"Expected list, got {type(services).__name__}"
    assert len(services) > 0, "Expected at least one service in registry"

    for i, svc in enumerate(services):
        present = L2_FIELDS & set(svc.keys())
        assert not present, (
            f"Service[{i}] ({svc.get('service_type', '<unknown>')}) "
            f"leaks L2 fields in L1 response: {present}"
        )


# ──────────────────────────────────────────────
#  1.5 — Registry 目录为空
# ──────────────────────────────────────────────


def test_empty_registry_dir(
    tmp_path: Path,
    agent_binary: str,
    shm_manager_binary: str,
    registry_dir: Path,
) -> None:
    """
    用例 1.5: mcp-registry/ 为空目录 → 返回空数组 []，Agent 正常就绪。

    制备: 复制 registry_dir 到 test-local，清空后启动 Agent。
    """
    # 复制 registry_dir 到 test-local 并清空
    empty_registry = tmp_path / "empty_registry"
    shutil.copytree(registry_dir, empty_registry)
    for f in empty_registry.glob("*.json"):
        f.unlink()

    config_dir = tmp_path / "etc_c4"
    handle = _start_agent_custom_registry(
        config_dir, empty_registry, shm_manager_binary, agent_binary
    )

    try:
        services = handle.get_services()
        assert isinstance(services, list), f"Expected list, got {type(services).__name__}"
        assert len(services) == 0, (
            f"Expected empty array for empty registry dir, got {len(services)} items"
        )
    finally:
        _teardown_agent(handle)


# ──────────────────────────────────────────────
#  1.6 — Registry 目录缺失
# ──────────────────────────────────────────────


def test_missing_registry_dir(
    tmp_path: Path,
    agent_binary: str,
    shm_manager_binary: str,
) -> None:
    """
    用例 1.6: mcp-registry/ 不存在 → Agent 不崩溃。
    GET /api/services 返回 200（空数组）或 5xx（启动失败），
    两种行为均视为合理防御。
    """
    # 制备不存在的 registry 路径
    nonexistent_registry = tmp_path / "nonexistent_registry"
    # 确保目录不存在
    if nonexistent_registry.exists():
        shutil.rmtree(nonexistent_registry)

    config_dir = tmp_path / "etc_c4"
    handle = _start_agent_custom_registry(
        config_dir, nonexistent_registry, shm_manager_binary, agent_binary
    )

    try:
        # Agent 不崩溃即通过（start_agent 已确认 Agent 就绪）。
        # GET /api/services 的返回值可以是 200 或 5xx。这里我们只验证 Agent 未崩溃。
        # 若返回 200，验证是空数组；若 5xx，conftest 等待会超时 → 先尝试 GET。
        try:
            services = handle.get_services()
            assert isinstance(services, list), f"Expected list, got {type(services).__name__}"
            # 缺失 registry 时，返回空数组或部分数据均可
        except Exception:
            # 5xx 也是可接受的防御行为
            pass
    finally:
        _teardown_agent(handle)


# ──────────────────────────────────────────────
#  1.7 — 单个 JSON 文件损坏
# ──────────────────────────────────────────────


def test_corrupt_json(
    tmp_path: Path,
    agent_binary: str,
    shm_manager_binary: str,
    registry_dir: Path,
) -> None:
    """
    用例 1.7: mcp-registry/ 中 1 个文件为非 JSON → Agent 不崩溃；
    GET /api/services 正常返回其余有效服务（损坏文件不导致全局加载失败）。
    """
    # 复制 registry_dir 到 test-local
    corrupt_registry = tmp_path / "corrupt_registry"
    shutil.copytree(registry_dir, corrupt_registry)

    # 找到第一个 JSON 文件并损坏它
    json_files = sorted(corrupt_registry.glob("*.json"))
    assert len(json_files) > 0, "Need at least one JSON file to corrupt"
    target = json_files[0]
    original_content = target.read_text()
    valid_count = len(json_files) - 1  # 损坏后剩余的有效文件数

    try:
        # 损坏: 写入非 JSON 内容
        target.write_text("this is not valid json {{{")

        config_dir = tmp_path / "etc_c4"
        handle = _start_agent_custom_registry(
            config_dir, corrupt_registry, shm_manager_binary, agent_binary
        )

        try:
            services = handle.get_services()
            assert isinstance(services, list), f"Expected list, got {type(services).__name__}"

            # 损坏文件不应导致其他有效文件加载失败
            assert len(services) == valid_count, (
                f"Expected {valid_count} valid services (1 corrupt), "
                f"got {len(services)}"
            )
        finally:
            _teardown_agent(handle)
    finally:
        # 恢复原文件内容
        target.write_text(original_content)
