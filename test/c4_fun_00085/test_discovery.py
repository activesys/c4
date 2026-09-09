"""C4_FUN_00085 — 点位发现与批量选择（TC1~TC8）。"""

import json
import sys
import time
import urllib.request
from pathlib import Path

from conftest import INSTANCE, shm_unlink, write_point

_TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_TEST_DIR.parent / "c4_fun_00053"))

import shm_helpers  # noqa: E402


def _points(base, filter_=None):
    q = f"?filter={filter_}" if filter_ else ""
    with urllib.request.urlopen(base + "/api/points" + q, timeout=5) as r:
        return json.loads(r.read().decode())


def test_tc1_full_enumeration_dedup(agent_stack):
    """TC1 全量枚举 + writer-only 去重：恰 5 条（3+2+0），reader 引用不重复。"""
    data = _points(agent_stack.base_url)
    assert data["count"] == 5
    keys = [p["key"] for p in data["points"]]
    assert len(keys) == len(set(keys))  # 无重复
    assert set(keys) == {
        "wt1.windspeed", "wt1.power", "wt1.oiltemp",
        "pv1.windspeed", "pv1.voltage",
    }


def test_tc2_key_composition(agent_stack):
    """TC2 key 组成：{实例}.{点名}，addr/shm_id 正确。"""
    data = _points(agent_stack.base_url)
    entry = next(p for p in data["points"] if p["key"] == "wt1.windspeed")
    assert entry["addr"] == 3000
    assert entry["shm_id"] > 0
    assert entry["instance"] == "wt1"


def test_tc3_filter_by_device(agent_stack):
    """TC3 按设备筛选：wt1 → 3 条；pv1 → 2 条。"""
    wt1 = _points(agent_stack.base_url, "wt1")
    pv1 = _points(agent_stack.base_url, "pv1")
    assert all(p["instance"] == "wt1" for p in wt1["points"]) and wt1["count"] == 3
    assert all(p["instance"] == "pv1" for p in pv1["points"]) and pv1["count"] == 2


def test_tc4_filter_keyword_ambiguity(agent_stack):
    """TC4 关键词筛选（歧义候选）：windspeed → 恰 2 条（wt1 与 pv1 各一）。"""
    data = _points(agent_stack.base_url, "windspeed")
    keys = {p["key"] for p in data["points"]}
    assert keys == {"wt1.windspeed", "pv1.windspeed"}


def test_tc5_no_match(agent_stack):
    """TC5 无匹配：空列表（200，非错误）。"""
    data = _points(agent_stack.base_url, "nothing_matches")
    assert data["count"] == 0 and data["points"] == []


def test_tc6_batch_grouping(agent_stack):
    """TC6 批量选择数据支撑：按实例分组后 wt1 组 3 点可整体订阅。"""
    data = _points(agent_stack.base_url)
    by_instance = {}
    for p in data["points"]:
        by_instance.setdefault(p["instance"], []).append(p["key"])
    assert len(by_instance["wt1"]) == 3


def test_tc7_shm_id_mapping(agent_stack):
    """TC7 shm_id 与 shm 实际一致：预写探针值 → read_points 按 shm_id 读取一致。"""
    # 通过 Agent REST 会话拿到 windspeed 的当前值（探针），再与 shm 直读比对
    data = _points(agent_stack.base_url)
    entry = next(p for p in data["points"] if p["key"] == "wt1.windspeed")
    probe = int(time.time() * 1000) % 100000 + 1
    write_point(agent_stack.shm_path, entry["shm_id"], 6, probe, int(time.time() * 1000))

    # 经 c4_shm_manager read_points（独立 stdio 栈不可用时跳过——依赖声明见 README §3）
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ft82_conftest", _TEST_DIR.parent / "c4_fun_00082" / "conftest.py")
    assert spec is not None and spec.loader is not None
    ft82 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ft82)

    binary = ft82._shm_manager_binary()
    client = ft82.McpClient(binary)
    try:
        # 新进程 state.sm 为 nil：adjust_shm 幂等挂载既有共享内存（无变更 → no-op）
        resp_attach = client.call_tool("adjust_shm", {
            "instance_id": INSTANCE,
            "config_path": str(agent_stack.tmp / "config.json"),
        })
        assert not resp_attach["result"].get("isError", False), resp_attach
        resp = client.call_tool("read_points", {"shm_ids": [entry["shm_id"]]})
        text = resp["result"]["content"][0]["text"]
        reads = json.loads(text)["reads"]
        match = next(e for e in reads if e["shm_id"] == entry["shm_id"])
        assert match["status"] == "ok"
        assert match["value"] == probe
    finally:
        client.close()


_TEST_DIR = __import__("pathlib").Path(__file__).resolve().parent
