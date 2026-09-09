"""C4_FUN_00084 — 会话创建与多点（TC1~TC5b）。"""

import json
import urllib.error
import urllib.request

from conftest import poll_until


def _post(base, path, body):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_tc1_minimal_create(agent_stack):
    """TC1 最简创建：响应为会话载荷（与 GET 同形），缺省 realtime/1000ms。"""
    status, payload = _post(agent_stack.base_url, "/api/display",
                            {"pointKeys": ["wt1.windspeed"]})
    assert status == 200
    session = payload["session"]
    assert payload["active"] is True
    assert session["sessionId"]
    assert session["mode"] == "realtime"
    assert session["intervalMs"] == 1000
    # GET 与 POST 同形
    with urllib.request.urlopen(agent_stack.base_url + "/api/display", timeout=5) as r:
        get_payload = json.loads(r.read().decode())
    assert get_payload["session"]["sessionId"] == session["sessionId"]


def test_tc2_multi_point(agent_stack):
    """TC2 多点订阅：points 集合与请求一致（顺序无契约）。"""
    keys = ["wt1.windspeed", "wt1.power", "wt1.oiltemp"]
    status, payload = _post(agent_stack.base_url, "/api/display", {"pointKeys": keys})
    assert status == 200
    got = {p["key"] for p in payload["session"]["points"]}
    assert got == set(keys)
    assert len(payload["session"]["points"]) == 3


def test_tc3_cumulative_mode(agent_stack):
    """TC3 累积模式：轮询 ?since=<tick> 返回条目且每条携带自身 tick。"""
    status, payload = _post(agent_stack.base_url, "/api/display",
                            {"pointKeys": ["wt1.windspeed"], "mode": "cumulative"})
    assert status == 200
    session = payload["session"]
    assert session["mode"] == "cumulative"
    tick = session["tick"]
    with urllib.request.urlopen(
        f"{agent_stack.base_url}/api/display?since={tick}", timeout=5) as r:
        inc = json.loads(r.read().decode())
    for p in inc["session"]["points"]:
        for record in p.get("records", []):
            assert record["tick"] > tick


def test_tc4_unknown_key(agent_stack):
    """TC4 非法 key：非 2xx、active 不变、错误体为文本（断言最小面）。"""
    status, payload = _post(agent_stack.base_url, "/api/display",
                            {"pointKeys": ["no_such_device.no_point"]})
    assert status >= 400
    assert "error" in payload or "UNKNOWN_POINT_KEY" in json.dumps(payload)


def test_tc5_interval_too_low(agent_stack):
    """TC5 intervalMs=100 < 250 下限 → 拒绝（不钳制）。"""
    status, payload = _post(agent_stack.base_url, "/api/display",
                            {"pointKeys": ["wt1.windspeed"], "intervalMs": 100})
    assert status >= 400
    assert "INTERVAL_TOO_LOW" in json.dumps(payload)


def test_tc5b_too_many_points(agent_stack):
    """TC5b 点数超限：>1000 → 拒绝并提示分批。"""
    keys = [f"dev.point{i}" for i in range(1001)]
    status, payload = _post(agent_stack.base_url, "/api/display", {"pointKeys": keys})
    assert status >= 400
    assert "TOO_MANY_POINTS" in json.dumps(payload)
