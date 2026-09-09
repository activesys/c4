"""C4_FUN_00084 — 降级与重启（TC15 降级不终止、TC17 降级保值、TC18 重启）。

本文件置于 test_c_terminate.py（TC16 摘要冻结）之后运行：
kill shm_manager 的降级场景会破坏后续会话推进，故放最后。
"""

import json
import os
import time
import urllib.request

from conftest import poll_until, write_point


def _get(base):
    with urllib.request.urlopen(base + "/api/display", timeout=5) as r:
        return json.loads(r.read().decode())


def _create(base, body):
    req = urllib.request.Request(
        base + "/api/display", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def _kill_stack_shm_manager(agent_stack) -> bool:
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


def test_tc15_degraded_not_terminating(agent_stack):
    """TC15 降级不终止：kill shm_manager → degraded=true 但会话仍 active（终止途径封闭）。"""
    _create(agent_stack.base_url, {"pointKeys": ["wt1.power"]})
    time.sleep(1)
    assert _kill_stack_shm_manager(agent_stack), "未找到栈内 c4_shm_manager"

    def degraded(payload):
        return payload["session"].get("degraded") is True

    assert poll_until(lambda: degraded(_get(agent_stack.base_url)), 10)
    payload = _get(agent_stack.base_url)
    assert payload["active"] is True


def test_tc17_degraded_values_held(agent_stack):
    """TC17 降级态：各点 value/state 保持最后成功值（不杜撰）。恢复 ❌ 待 C4_FUN_00021。

    TC15 已杀栈内 shm_manager 且无自愈——先重启测试栈恢复读取能力，
    建立成功基线后再制造降级，验证降级时保持最后成功值。
    """
    agent_stack.restart()  # TC15 杀过 shm_manager：重启栈（无自愈，整机重启恢复）
    sid = agent_stack.shm_ids()["wt1.windspeed"]
    write_point(agent_stack.shm_path, sid, 6, 555, int(time.time() * 1000))
    _create(agent_stack.base_url, {"pointKeys": ["wt1.windspeed"]})

    def has_value(payload):
        point = next(x for x in payload["session"]["points"] if x["key"] == "wt1.windspeed")
        return point["value"] is not None

    assert poll_until(lambda: has_value(_get(agent_stack.base_url)), 20), "基线读取失败"

    assert _kill_stack_shm_manager(agent_stack), "未找到栈内 c4_shm_manager"

    def degraded(payload):
        return payload["session"].get("degraded") is True

    assert poll_until(lambda: degraded(_get(agent_stack.base_url)), 15)
    payload = _get(agent_stack.base_url)
    point = next(x for x in payload["session"]["points"] if x["key"] == "wt1.windspeed")
    assert point["value"] == 555
    assert point["state"] in ("ok", "stale")


def test_tc18_agent_restart(agent_stack):
    """TC18 Agent 重启（测试进程内，非 systemctl）：

    active=false 且无 lastSession（会话仅内存）；shm 持久——write_seq 不归零，
    测试直写仍可继续。
    """
    import shm_helpers
    _create(agent_stack.base_url, {"pointKeys": ["wt1.windspeed"]})
    time.sleep(1)
    before = shm_helpers.read_shm_block(agent_stack.shm_path, 1)

    agent_stack.restart()

    payload = _get(agent_stack.base_url)
    assert payload["active"] is False
    assert "lastSession" not in payload  # 会话仅内存，重启即消失

    after = shm_helpers.read_shm_block(agent_stack.shm_path, 1)
    assert after["write_seq"] == before["write_seq"]  # shm 持久（不归零）
    write_point(agent_stack.shm_path, 1, 6, 1, int(time.time() * 1000))  # 直写仍可继续
