"""C4_FUN_00084 — 五类终止与摘要冻结（TC10~TC16）。"""

import json
import time
import urllib.request

from conftest import poll_until


def _get(base):
    with urllib.request.urlopen(base + "/api/display", timeout=5) as r:
        return json.loads(r.read().decode())


def _create(base, body):
    req = urllib.request.Request(
        base + "/api/display", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def _stop(base, point_keys=None):
    body = {} if point_keys is None else {"pointKeys": point_keys}
    req = urllib.request.Request(
        base + "/api/display/stop", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def test_tc10_duration_terminate(agent_stack):
    """TC10 时长终止：durationMinutes=0.1 → completed_duration，finalTick ∈ [4,8]。"""
    _create(agent_stack.base_url, {"pointKeys": ["wt1.windspeed"],
                                   "durationMinutes": 0.1, "intervalMs": 1000})

    def ended(payload):
        return (not payload["active"]
                and payload.get("lastSession", {}).get("endedReason") == "completed_duration")

    assert poll_until(lambda: ended(_get(agent_stack.base_url)), 15)
    last = _get(agent_stack.base_url)["lastSession"]
    assert 4 <= last["finalTick"] <= 8


def test_tc11_count_terminate(agent_stack):
    """TC11 次数终止：refreshCount=5 → 恰 5 tick 后 completed_count，finalTick=5。"""
    _create(agent_stack.base_url, {"pointKeys": ["wt1.windspeed"],
                                   "refreshCount": 5, "intervalMs": 1000})

    def ended(payload):
        return (not payload["active"]
                and payload.get("lastSession", {}).get("endedReason") == "completed_count")

    assert poll_until(lambda: ended(_get(agent_stack.base_url)), 15)
    assert _get(agent_stack.base_url)["lastSession"]["finalTick"] == 5


def test_tc12_manual_stop(agent_stack):
    """TC12 主动停止：stop（无参）→ stopped。"""
    _create(agent_stack.base_url, {"pointKeys": ["wt1.windspeed"]})
    time.sleep(1.5)
    _stop(agent_stack.base_url)
    payload = _get(agent_stack.base_url)
    assert payload["active"] is False
    assert payload["lastSession"]["endedReason"] == "stopped"


def test_tc13_replace_cancels(agent_stack):
    """TC13 切换即取消：A 运行中创建 B → B 为唯一活跃会话，A 被替换。

    lastSession（replaced）按设计保留至下一会话建立——B 建立即被清除，
    故本用例只断言新会话的唯一性（replaced 原因的可见性属前端 lastSession 语义）。
    """
    _create(agent_stack.base_url, {"pointKeys": ["wt1.windspeed"]})
    time.sleep(1.5)
    _create(agent_stack.base_url, {"pointKeys": ["wt1.power"]})
    payload = _get(agent_stack.base_url)
    keys = {p["key"] for p in payload["session"]["points"]}
    assert keys == {"wt1.power"}


def test_tc14_point_level_stop(agent_stack):
    """TC14 点级停止：双点会话停 A → A 消失 B 仍在；再停 B → 会话结束。"""
    _create(agent_stack.base_url, {"pointKeys": ["wt1.windspeed", "wt1.power"]})
    time.sleep(1)
    _stop(agent_stack.base_url, ["wt1.windspeed"])
    payload = _get(agent_stack.base_url)
    keys = {p["key"] for p in payload["session"]["points"]}
    assert keys == {"wt1.power"}
    _stop(agent_stack.base_url, ["wt1.power"])
    payload = _get(agent_stack.base_url)
    assert payload["active"] is False
    assert payload["lastSession"]["endedReason"] == "stopped"


def test_tc16_summary_frozen(agent_stack):
    """TC16 终止后摘要冻结：lastSession 保留至下一会话建立，finalTick 不再变化。

    注意：本用例必须在 kill shm_manager 的降级用例（test_d_degraded TC15）之前运行。
    """
    _create(agent_stack.base_url, {"pointKeys": ["wt1.windspeed"],
                                   "refreshCount": 3, "intervalMs": 1000})

    def ended(payload):
        return (not payload["active"]
                and payload.get("lastSession", {}).get("endedReason") == "completed_count")

    assert poll_until(lambda: ended(_get(agent_stack.base_url)), 15)
    first = _get(agent_stack.base_url)["lastSession"]
    time.sleep(2)
    second = _get(agent_stack.base_url)["lastSession"]
    assert first == second
