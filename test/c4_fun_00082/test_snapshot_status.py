"""C4_FUN_00082 §3 — 快照状态标注（TC13~TC17，经 Agent REST）。"""

import json
import os
import threading
import time
import urllib.request

from conftest import write_point, poll_until


def _get(agent_stack, since=None):
    q = f"?since={since}" if since is not None else ""
    with urllib.request.urlopen(agent_stack.base_url + "/api/display" + q, timeout=5) as r:
        return json.loads(r.read().decode())


def _create(agent_stack, point_keys, **kwargs):
    body = {"pointKeys": point_keys, **kwargs}
    req = urllib.request.Request(
        agent_stack.base_url + "/api/display",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def _point(payload, key):
    return next(p for p in payload["session"]["points"] if p["key"] == key)


def test_tc13_normal_state(agent_stack):
    """TC13 正常：写线程每 500ms 写一次 → state=ok，timestampMs 推进。"""
    sid = agent_stack.shm_ids()["wt1.windspeed"]
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            write_point(agent_stack.shm_path, sid, 6, 100 + i, int(time.time() * 1000))
            i += 1
            time.sleep(0.5)

    t = threading.Thread(target=writer)
    t.start()
    try:
        _create(agent_stack, ["wt1.windspeed"])

        def check(payload):
            point = _point(payload, "wt1.windspeed")
            return point["state"] == "ok" and point["value"] is not None

        assert poll_until(lambda: check(_get(agent_stack)), 5)
        first = _get(agent_stack)
        time.sleep(1.5)
        second = _get(agent_stack)
        ts_first = _point(first, "wt1.windspeed")["timestampMs"]
        ts_second = _point(second, "wt1.windspeed")["timestampMs"]
        assert ts_second >= ts_first
    finally:
        stop.set()
        t.join()


def test_tc14_no_data(agent_stack):
    """TC14 暂无数据：块 state=0 → state=no_data，value 不得为数值。"""
    _create(agent_stack, ["wt1.oiltemp"])  # oiltemp 从未播种

    def check(payload):
        point = _point(payload, "wt1.oiltemp")
        return point["state"] == "no_data"

    assert poll_until(lambda: check(_get(agent_stack)), 5)
    point = _point(_get(agent_stack), "wt1.oiltemp")
    assert point["value"] is None or "value" not in point


def test_tc15_stale_annotation(agent_stack):
    """TC15 已停止刷新：预写 ts=now−12min → stale + staleForMs ∈ [700000, 740000]。"""
    sid = agent_stack.shm_ids()["wt1.windspeed"]
    write_point(agent_stack.shm_path, sid, 6, 42, int(time.time() * 1000) - 720_000)
    _create(agent_stack, ["wt1.windspeed"])

    def check(payload):
        point = _point(payload, "wt1.windspeed")
        return point["state"] == "stale" and point.get("staleForMs") is not None

    assert poll_until(lambda: check(_get(agent_stack)), 5)
    point = _point(_get(agent_stack), "wt1.windspeed")
    assert 700_000 <= point["staleForMs"] <= 740_000


def test_tc16_adaptive_threshold(agent_stack):
    """TC16 阈值自适应（slow ~150s）：25s 周期点建立观测（≥2 变位）后，

    窗口剪空 → 保留最近计算阈值 75s → 静默 65s 仍 ok；
    静默 ≥80s 后轮询至 stale（截止静默 90s）。
    """
    sid = agent_stack.shm_ids()["wt1.windspeed"]
    stop = threading.Event()
    writes = {"n": 0}
    last_write = {"t": 0.0}

    def writer():
        for _ in range(3):
            if stop.is_set():
                return
            write_point(agent_stack.shm_path, sid, 6, writes["n"] + 1, int(time.time() * 1000))
            writes["n"] += 1
            last_write["t"] = time.time()
            for _ in range(250):
                if stop.is_set():
                    return
                time.sleep(0.1)

    t = threading.Thread(target=writer)
    t.start()
    try:
        _create(agent_stack, ["wt1.windspeed"])
        # 等待观测建立：≥3 次写入（0/25/50s）≈ 55s
        end = time.time() + 60
        while time.time() < end and writes["n"] < 3:
            time.sleep(0.5)
        assert writes["n"] >= 3, "观测建立阶段写入不足"
        stop.set()
        t.join()

        # 静默 65s（自最后一次写入起）：仍 ok（保留阈值 75s > 65s）
        silence = time.time() - last_write["t"]
        if silence < 65:
            time.sleep(65 - silence)
        payload = _get(agent_stack)
        assert _point(payload, "wt1.windspeed")["state"] == "ok"

        # 轮询至 stale（截止静默 90s；量化边界余量 ~2s，以轮询截止兜底）
        def turned_stale(payload):
            return _point(payload, "wt1.windspeed")["state"] == "stale"

        deadline = last_write["t"] + 90
        assert poll_until(lambda: turned_stale(_get(agent_stack)),
                          max(1, deadline - time.time())), \
            "静默 90s 内未转为 stale"
    finally:
        stop.set()
        t.join()


def test_tc17_degraded(agent_stack):
    """TC17 降级态：kill 栈内 c4_shm_manager → degraded=true，各点保持上次值。

    恢复路径 ❌ 待 C4_FUN_00021（与 00083 TC7 / 00084 TC17 注一致）。
    本用例置于文件末尾：kill 后本 Agent 栈即降级。
    """
    # 建立正常基线
    sid = agent_stack.shm_ids()["wt1.windspeed"]
    write_point(agent_stack.shm_path, sid, 6, 777, int(time.time() * 1000))
    _create(agent_stack, ["wt1.windspeed"])

    def degraded(payload):
        return payload.get("session", {}).get("degraded") is True

    def has_value(payload):
        return _point(payload, "wt1.windspeed")["value"] == 777

    # 先确认读到基线值，再制造降级（否则 kill 早于首读，value=None 属正确"不杜撰"）
    assert poll_until(lambda: has_value(_get(agent_stack)), 5), "基线读取失败"

    def alive(payload):
        return payload.get("session", {}).get("degraded") is not True

    assert poll_until(lambda: alive(_get(agent_stack)), 5), "基线未建立"

    # kill 栈内 c4_shm_manager（Agent 的直接子进程）
    agent_pid = agent_stack.proc.pid
    killed = False
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                fields = f.read()
            ppid = int(fields.split(")")[-1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        if ppid != agent_pid:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().decode(errors="replace")
            if "c4_shm_manager" in cmd:
                os.kill(int(pid), 9)
                killed = True
        except OSError:
            continue
    assert killed, "未找到栈内 c4_shm_manager 子进程"

    # 连续 ≥3 轮失败 → degraded=true（轮询截止 10s）
    assert poll_until(lambda: degraded(_get(agent_stack)), 10)
    payload = _get(agent_stack)
    point = _point(payload, "wt1.windspeed")
    assert point["value"] is not None  # 保持最后成功值（不杜撰）
