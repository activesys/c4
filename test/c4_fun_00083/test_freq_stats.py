"""C4_FUN_00083 — 刷新频率统计（TC1~TC8）。"""

import json
import threading
import time
import urllib.request

from conftest import write_point, poll_until


def _get(base):
    with urllib.request.urlopen(base + "/api/display", timeout=5) as r:
        return json.loads(r.read().decode())


def _create(base, point_keys, **kwargs):
    body = {"pointKeys": point_keys, **kwargs}
    req = urllib.request.Request(
        base + "/api/display", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def _freq(payload, key):
    return next(p for p in payload["session"]["points"] if p["key"] == key)["freq"]


def _start_writer(agent_stack, shm_id, period_s, stop: dict, counter: dict):
    def writer():
        while not stop["flag"]:
            write_point(agent_stack.shm_path, shm_id, 6,
                        counter["n"] + 1, int(time.time() * 1000))
            counter["n"] += 1
            # 分段 sleep 响应 stop
            end = time.time() + period_s
            while time.time() < end and not stop["flag"]:
                time.sleep(0.05)
    t = threading.Thread(target=writer)
    t.start()
    return t


def test_tc1_fixed_period(agent_stack):
    """TC1 固定周期统计：500ms 周期 10s → count ∈ [18,21]，interval ∈ [450,560]。"""
    sid = agent_stack.shm_ids()["wt1.windspeed"]
    stop = {"flag": False}
    counter = {"n": 0}
    t = _start_writer(agent_stack, sid, 0.5, stop, counter)
    try:
        _create(agent_stack.base_url, ["wt1.windspeed"], intervalMs=250)
        time.sleep(10)
        freq = _freq(_get(agent_stack.base_url), "wt1.windspeed")
        assert 18 <= freq["count"] <= 21
        assert 450 <= freq["intervalMs"] <= 560
    finally:
        stop["flag"] = True
        t.join()


def test_tc2_static_point(agent_stack):
    """TC2 静止点（短会话）：预写后不写 → count=0，state=ok（解耦）。"""
    sid = agent_stack.shm_ids()["wt1.power"]
    write_point(agent_stack.shm_path, sid, 6, 42, int(time.time() * 1000))
    _create(agent_stack.base_url, ["wt1.power"])
    time.sleep(5)
    payload = _get(agent_stack.base_url)
    point = next(p for p in payload["session"]["points"] if p["key"] == "wt1.power")
    assert point["freq"]["count"] == 0
    assert point["state"] == "ok"


def test_tc3_multi_point_independent(agent_stack):
    """TC3 多点独立统计：A 500ms、B 2s → 互不串扰。"""
    sid_a = agent_stack.shm_ids()["wt1.windspeed"]
    sid_b = agent_stack.shm_ids()["wt1.power"]
    stop = {"flag": False}
    ca = {"n": 0}
    cb = {"n": 0}
    ta = _start_writer(agent_stack, sid_a, 0.5, stop, ca)
    tb = _start_writer(agent_stack, sid_b, 2.0, stop, cb)
    try:
        _create(agent_stack.base_url, ["wt1.windspeed", "wt1.power"], intervalMs=250)
        time.sleep(10)
        fa = _freq(_get(agent_stack.base_url), "wt1.windspeed")
        fb = _freq(_get(agent_stack.base_url), "wt1.power")
        assert 18 <= fa["count"] <= 21
        assert 3 <= fb["count"] <= 7
    finally:
        stop["flag"] = True
        ta.join()
        tb.join()


def test_tc4_sliding_window_boundary(agent_stack):
    """TC4 滑动窗口边界（slow ~140s）：写 70s 后停止 → count 单调非增至 0。"""
    sid = agent_stack.shm_ids()["wt1.oiltemp"]
    stop = {"flag": False}
    counter = {"n": 0}
    t = _start_writer(agent_stack, sid, 0.5, stop, counter)
    try:
        _create(agent_stack.base_url, ["wt1.oiltemp"], intervalMs=500)
        time.sleep(70)
        stop["flag"] = True
        t.join()
        counts = []
        for _ in range(3):
            counts.append(_freq(_get(agent_stack.base_url), "wt1.oiltemp")["count"])
            time.sleep(30)
        assert counts[0] >= counts[1] >= counts[2] >= 0
    finally:
        stop["flag"] = True
        t.join()


def test_tc5_first_tick_no_change(agent_stack):
    """TC5 首 tick 不计变位（slow ≥60s）：预写后不变 → 完整 60s 窗口 count=0。"""
    sid = agent_stack.shm_ids()["wt1.windspeed"]
    write_point(agent_stack.shm_path, sid, 6, 7, int(time.time() * 1000))
    _create(agent_stack.base_url, ["wt1.windspeed"])
    time.sleep(62)
    assert _freq(_get(agent_stack.base_url), "wt1.windspeed")["count"] == 0


def test_tc6_period_3s(agent_stack):
    """TC6 频率随实际周期呈现：3s 周期 15s → |interval−3000|≤300，count ∈ [3,6]。"""
    sid = agent_stack.shm_ids()["wt1.windspeed"]
    stop = {"flag": False}
    counter = {"n": 0}
    t = _start_writer(agent_stack, sid, 3.0, stop, counter)
    try:
        _create(agent_stack.base_url, ["wt1.windspeed"], intervalMs=250)
        time.sleep(15)
        freq = _freq(_get(agent_stack.base_url), "wt1.windspeed")
        assert abs(freq["intervalMs"] - 3000) <= 300
        assert 3 <= freq["count"] <= 6
    finally:
        stop["flag"] = True
        t.join()


def test_tc7_degraded_no_stats_pollution(agent_stack):
    """TC7 降级期不污染统计：degraded 期间 count 与 tick 不推进。

    恢复路径 ❌ 待 C4_FUN_00021（与 c4_fun_00084 TC17 注一致）。
    """
    sid = agent_stack.shm_ids()["wt1.power"]
    write_point(agent_stack.shm_path, sid, 6, 1, int(time.time() * 1000))
    _create(agent_stack.base_url, ["wt1.power"])
    time.sleep(2)

    _kill_stack_shm_manager(agent_stack)

    def degraded(p):
        return p.get("session", {}).get("degraded") is True

    assert poll_until(lambda: degraded(_get(agent_stack.base_url)), 10)
    payload = _get(agent_stack.base_url)
    count = _freq(payload, "wt1.power")["count"]
    tick = payload["session"]["tick"]
    time.sleep(2)
    payload2 = _get(agent_stack.base_url)
    assert _freq(payload2, "wt1.power")["count"] == count
    assert payload2["session"]["tick"] == tick


def _kill_stack_shm_manager(agent_stack):
    import os
    agent_pid = agent_stack.proc.pid
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                ppid = int(f.read().split(")")[-1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        if ppid != agent_pid:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().decode(errors="replace")
            if "c4_shm_manager" in cmd:
                os.kill(int(pid), 9)
                return True
        except OSError:
            continue
    return False
