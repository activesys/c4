"""C4_FUN_00084 — 两种展示模式与游标（TC6~TC9）。"""

import json
import threading
import time
import urllib.request

from conftest import write_point


def _get(base, since=None):
    q = f"?since={since}" if since is not None else ""
    with urllib.request.urlopen(base + "/api/display" + q, timeout=5) as r:
        return json.loads(r.read().decode())


def _create(base, body):
    req = urllib.request.Request(
        base + "/api/display", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def test_tc6_realtime_advance(agent_stack):
    """TC6 实时值模式推进：value/timestampMs 反映最新写入（后 ≥ 前）。"""
    sid = agent_stack.shm_ids()["wt1.windspeed"]
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            write_point(agent_stack.shm_path, sid, 6, 200 + i, int(time.time() * 1000))
            i += 1
            time.sleep(0.5)

    t = threading.Thread(target=writer)
    t.start()
    try:
        _create(agent_stack.base_url, {"pointKeys": ["wt1.windspeed"]})

        def ready(p):
            point = next(x for x in p["session"]["points"] if x["key"] == "wt1.windspeed")
            return point["value"] is not None

        deadline = time.time() + 5
        while time.time() < deadline:
            payload = _get(agent_stack.base_url)
            if ready(payload):
                break
            time.sleep(0.3)

        ts_list = []
        for _ in range(3):
            payload = _get(agent_stack.base_url)
            point = next(x for x in payload["session"]["points"] if x["key"] == "wt1.windspeed")
            ts_list.append(point["timestampMs"])
            time.sleep(0.6)
        assert ts_list == sorted(ts_list)
    finally:
        stop.set()
        t.join()


def test_tc7_cumulative_accumulation(agent_stack):
    """TC7 累积模式：intervalMs=250 钉死，预写初值 w0 → 5s 内 20±2 条记录。"""
    sid = agent_stack.shm_ids()["wt1.power"]
    write_point(agent_stack.shm_path, sid, 6, 900, int(time.time() * 1000))  # 预写初值 w0
    stop = threading.Event()
    written = [900]
    counter = {"n": 0}

    def writer():
        i = 1
        while not stop.is_set():
            v = 900 + i
            write_point(agent_stack.shm_path, sid, 6, v, int(time.time() * 1000))
            written.append(v)
            i += 1
            time.sleep(0.3)

    t = threading.Thread(target=writer)
    t.start()
    try:
        _create(agent_stack.base_url,
                {"pointKeys": ["wt1.power"], "mode": "cumulative", "intervalMs": 250})
        time.sleep(5)
        payload = _get(agent_stack.base_url)
        point = next(x for x in payload["session"]["points"] if x["key"] == "wt1.power")
        records = point.get("records", [])
        assert 18 <= len(records) <= 22
        ticks = [r["tick"] for r in records]
        assert ticks == sorted(ticks)
        for r in records:
            assert r["v"] in written  # 已写值（允许跳值，不允许错值）
    finally:
        stop.set()
        t.join()


def test_tc8_cursor_increment(agent_stack):
    """TC8 游标增量：?since=<tick> 两次拉取，第二次只返回新增。"""
    _create(agent_stack.base_url,
            {"pointKeys": ["wt1.power"], "mode": "cumulative", "intervalMs": 250})
    stop = threading.Event()
    counter = {"n": 0}

    def writer():
        i = 0
        while not stop.is_set():
            write_point(agent_stack.shm_path, sid, 6, 1000 + i, int(time.time() * 1000))
            i += 1
            time.sleep(0.4)

    sid = agent_stack.shm_ids()["wt1.power"]
    t = threading.Thread(target=writer)
    t.start()
    try:
        time.sleep(2)
        first = _get(agent_stack.base_url, since=0)
        point_first = next(x for x in first["session"]["points"] if x["key"] == "wt1.power")
        records_first = point_first.get("records", [])
        last_tick = records_first[-1]["tick"] if records_first else 0
        time.sleep(1)
        second = _get(agent_stack.base_url, since=last_tick)
        point_second = next(x for x in second["session"]["points"] if x["key"] == "wt1.power")
        records_second = point_second.get("records", [])
        for r in records_second:
            assert r["tick"] > last_tick
    finally:
        stop.set()
        t.join()


def test_tc9_cursor_cross_session(agent_stack):
    """TC9 游标跨会话：sessionId 变化 → 旧游标按新会话返回全量（安全默认），不混合。"""
    _create(agent_stack.base_url, {"pointKeys": ["wt1.power"], "mode": "cumulative"})
    time.sleep(1.5)
    # 切换会话（旧会话被替换）
    _create(agent_stack.base_url, {"pointKeys": ["wt1.windspeed"], "mode": "cumulative"})
    time.sleep(1)
    # 旧游标（tick=0）拉取：返回新会话全量，且只含新会话的点
    payload = _get(agent_stack.base_url, since=0)
    keys = {p["key"] for p in payload["session"]["points"]}
    assert "wt1.windspeed" in keys
    assert "wt1.power" not in keys
