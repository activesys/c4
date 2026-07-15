"""
C4_FUN_00053 测试用例 — 配置文件不存在或为 null

验证 c4_shm_manager 在无配置文件时正确创建默认 10 万点共享内存空间。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shm_helpers import get_shm_size, read_shm_block, read_shm_header, shm_path

# ──────────────────────────────────────────────
#  常量
# ──────────────────────────────────────────────

MAGIC = 0xC4DA7A00
VERSION = 1
DEFAULT_MAX_POINTS = 100000
BLOCK_SIZE = 32
EXPECTED_SHM_SIZE = (DEFAULT_MAX_POINTS + 1) * BLOCK_SIZE  # 3,200,032


# ──────────────────────────────────────────────
#  roots/list 回调工厂
# ──────────────────────────────────────────────


def _roots_callback(roots_list):
    """创建 on_request 回调：对 roots/list 返回 roots_list。"""

    def callback(method, params, request_id):
        if method == "roots/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"roots": roots_list},
            }
        return None

    return callback


def _roots_error_callback():
    """创建 on_request 回调：对 roots/list 返回 MCP 错误。"""

    def callback(method, params, request_id):
        if method == "roots/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        return None

    return callback


# ──────────────────────────────────────────────
#  辅助断言
# ──────────────────────────────────────────────


def _assert_default_shm(instance_id: str):
    """验证共享内存是否符合默认 10 万点模式。"""
    path = shm_path(instance_id)
    assert os.path.exists(path), f"Shared memory {path} does not exist"

    # 文件大小
    assert get_shm_size(path) == EXPECTED_SHM_SIZE, (
        f"Expected shm size {EXPECTED_SHM_SIZE}, got {get_shm_size(path)}"
    )

    # Header 校验
    h = read_shm_header(path)
    assert h["magic"] == MAGIC, f"Header magic: {hex(h['magic'])}"
    assert h["version"] == VERSION, f"Header version: {h['version']}"
    assert h["remap_version"] == 0, f"Header remap_version: {h['remap_version']}"
    assert h["point_count"] == 0, f"Header point_count: {h['point_count']}"
    assert h["max_points"] == DEFAULT_MAX_POINTS, (
        f"Header max_points: {h['max_points']}"
    )
    assert h["global_write_seq"] == 0, (
        f"Header global_write_seq: {h['global_write_seq']}"
    )
    assert h["reserved"] == 0, f"Header reserved: {h['reserved']}"


def _assert_data_block(path: str, shm_id: int):
    """验证单个 Data Block 的初始化状态。"""
    b = read_shm_block(path, shm_id)
    assert b["magic"] == MAGIC, f"Block {shm_id} magic: {hex(b['magic'])}"
    assert b["state"] == 0, f"Block {shm_id} state: {b['state']}"
    assert b["reserved"] == 0, f"Block {shm_id} reserved: {b['reserved']}"
    assert b["type"] == 0, f"Block {shm_id} type: {b['type']}"
    assert b["write_seq"] == 0, f"Block {shm_id} write_seq: {b['write_seq']}"
    assert b["timestamp"] == 0, f"Block {shm_id} timestamp: {b['timestamp']}"
    assert b["value"] == 0, f"Block {shm_id} value: {b['value']}"


def _assert_mcp_success(resp):
    assert resp["result"].get("isError", False) is False
    assert resp["result"]["content"][0]["text"] == "success"


def _assert_mcp_error(resp, expected_prefix):
    """验证 MCP 响应是业务错误且前缀匹配。"""
    assert resp["result"]["isError"] is True, (
        f"Expected isError=true, got: {resp}"
    )
    text = resp["result"]["content"][0]["text"]
    assert text.startswith(expected_prefix), (
        f"Expected prefix '{expected_prefix}', got: '{text}'"
    )


# ──────────────────────────────────────────────
#  测试用例
# ──────────────────────────────────────────────


class TestNoConfigShmCreation:
    """C4_FUN_00053: 配置文件不存在或为 null → 默认 10 万点共享内存。"""

    # ── TC1: roots/list 返回空 ─────────────────────

    def test_tc1_roots_empty(self, mcp, isolated_shm):
        """TC1: roots/list 返回 [] → 创建 100k 默认 shm。"""
        iid = "test_tc1"
        isolated_shm(iid)

        resp = mcp.call_tool(
            "create_shm",
            {"instance_id": iid},
            on_request=_roots_callback([]),
        )
        assert resp["result"]["content"][0]["text"] == "success"

        _assert_default_shm(iid)

    # ── TC2: roots/list 返回路径，文件不存在 ────────

    def test_tc2_file_not_found(self, mcp, isolated_shm):
        """TC2: 返回路径但文件不存在 → 100k 默认 shm。"""
        iid = "test_tc2"
        isolated_shm(iid)

        resp = mcp.call_tool(
            "create_shm",
            {"instance_id": iid},
            on_request=_roots_callback(
                [{"uri": "file:///tmp/c4_no_such_config.json"}]
            ),
        )
        assert resp["result"]["content"][0]["text"] == "success"

        _assert_default_shm(iid)

    # ── TC3: roots/list 返回路径，文件为空 JSON ─────

    def test_tc3_empty_json(self, mcp, isolated_shm):
        """TC3: 文件内容 {} → 100k 默认 shm，且原文件不被修改。"""
        iid = "test_tc3"
        isolated_shm(iid)

        # 创建临时空 JSON 文件
        fd, tmp_path = tempfile.mkstemp(
            suffix=".json", prefix="c4_empty_", text=True
        )
        os.close(fd)
        try:
            with open(tmp_path, "w") as f:
                f.write("{}")

            resp = mcp.call_tool(
                "create_shm",
                {"instance_id": iid},
                on_request=_roots_callback(
                    [{"uri": f"file://{tmp_path}"}]
                ),
            )
            assert resp["result"].get("isError", False) is False
            assert resp["result"]["content"][0]["text"] == "success"

            _assert_default_shm(iid)

            # 额外验证：原文件未被修改
            with open(tmp_path, "r") as f:
                content = f.read()
            assert content.strip() in ("{}", "{} "), (
                f"Config file was modified: '{content}'"
            )
        finally:
            os.unlink(tmp_path)

    # ── TC4: 重复创建 → SHM_ALREADY_EXISTS ──────────

    def test_tc4_duplicate_create(self, mcp, isolated_shm):
        """TC4: 同一 instance_id 创建两次 → 第二次返回 SHM_ALREADY_EXISTS。"""
        iid = "test_tc4"
        isolated_shm(iid)

        # 第一次创建 — 成功
        resp1 = mcp.call_tool(
            "create_shm",
            {"instance_id": iid},
            on_request=_roots_callback([]),
        )
        assert resp1["result"].get("isError", False) is False

        # 第二次创建 — 应失败
        resp2 = mcp.call_tool(
            "create_shm",
            {"instance_id": iid},
            on_request=_roots_callback([]),
        )
        _assert_mcp_error(resp2, "SHM_ALREADY_EXISTS")

    # ── TC5: Header 全字段校验 ─────────────────────

    def test_tc5_header_validation(self, mcp, isolated_shm):
        """TC5: 创建后读取 shm Header，逐字段校验 + 文件大小。"""
        iid = "test_tc5"
        isolated_shm(iid)

        mcp.call_tool(
            "create_shm",
            {"instance_id": iid},
            on_request=_roots_callback([]),
        )
        _assert_default_shm(iid)

    # ── TC6: Data Block 初始化校验 ──────────────────

    def test_tc6_block_initialization(self, mcp, isolated_shm):
        """TC6: 抽检 shm_id=1, 50000, 100000 三个 Data Block。"""
        iid = "test_tc6"
        isolated_shm(iid)

        mcp.call_tool(
            "create_shm",
            {"instance_id": iid},
            on_request=_roots_callback([]),
        )

        path = shm_path(iid)
        # 首 / 中 / 尾 抽检
        for sid in (1, 50000, 100000):
            _assert_data_block(path, sid)

    # ── TC7: query_status 交叉验证 ──────────────────

    def test_tc7_query_status(self, mcp, isolated_shm):
        """TC7: create_shm 后调用 query_status，验证返回值一致性。"""
        iid = "test_tc7"
        isolated_shm(iid)

        mcp.call_tool(
            "create_shm",
            {"instance_id": iid},
            on_request=_roots_callback([]),
        )

        resp = mcp.call_tool("query_status", {})
        assert resp["result"].get("isError", False) is False

        # query_status 返回嵌套 JSON: result.content[0].text 是 JSON 字符串
        import json

        inner = json.loads(resp["result"]["content"][0]["text"])
        assert inner["magic"] == "valid"
        assert inner["version"] == VERSION
        assert inner["remap_version"] == 0
        assert inner["point_count"] == 0
        assert inner["max_points"] == DEFAULT_MAX_POINTS
        assert inner["free_blocks"] == DEFAULT_MAX_POINTS  # max - point_count
        assert inner["global_write_seq"] == 0

    # ── TC8: roots/list MCP 调用失败 ────────────────

    def test_tc8_roots_list_failure(self, mcp, isolated_shm):
        """TC8: roots/list 返回 MCP 错误 → CONFIG_PATH_MISSING。"""
        iid = "test_tc8"
        isolated_shm(iid)

        resp = mcp.call_tool(
            "create_shm",
            {"instance_id": iid},
            on_request=_roots_error_callback(),
        )
        _assert_mcp_error(resp, "CONFIG_PATH_MISSING:")

        # 不应创建共享内存
        path = shm_path(iid)
        assert not os.path.exists(path), (
            f"Shared memory should not exist for failed create: {path}"
        )
