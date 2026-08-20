"""
C4_FUN_00068 测试用例 — c4_influxdb_client Stop-Start 协议。

严格按 README.md 规格实现，不参考 Go 源码。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import tempfile

from conftest import (  # noqa: E402
    query_latest,
    _assert_mcp_error,
    _assert_mcp_success,
    _make_c4_config,
    _make_influx_instance,
    _make_influx_point,
    _run_adjust_shm,
    write_shm_block,
    shm_path,
)


TS = 1768848814264


def _wait_value(url, db, m, f, expected, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = query_latest(url, db, m, f)
        if r is not None:
            val = r[2]
            if isinstance(expected, float) and isinstance(val, (int, float)):
                if abs(val - expected) < 1e-6:
                    return
            elif val == expected:
                return
        time.sleep(interval)
    raise AssertionError(f"field {m}.{f} did not become {expected} within {timeout}s")


def _single_point(key="fake_writer.pt1", measurement="wind_turbine", field="windspeed"):
    return _make_influx_point(key, measurement, field=field,
                              type_="float", tags={"site": "hnals"}, shm_id=0)


def _start(prepare_environment, start_influxdb_client, isolated_shm,
           influxdb, db, instance_id, points, writer_point_ids,
           flush_interval=100):
    isolated_shm(instance_id)
    inst = _make_influx_instance(
        "入库", "test_influx", influxdb, db, points, flush_interval=flush_interval
    )
    config = _make_c4_config([inst], writer_point_ids)
    config_path, _ = prepare_environment(config, instance_id)
    client = start_influxdb_client()
    resp = client.call_tool(
        "start", {"instance_id": instance_id, "config_path": config_path}
    )
    _assert_mcp_success(resp)
    return client, config_path


class TestInfluxdbStopRestart:

    # ── TC1: stop — 运行中停止 ──────────

    def test_tc1_stop_running(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun68tc1"
        db = create_database()
        client, config_path = _start(
            prepare_environment, start_influxdb_client, isolated_shm,
            influxdb, db, instance_id, [_single_point()], ["pt1"],
        )

        write_shm_block(shm_path(instance_id), 1, 10, 1.5, TS)
        _wait_value(influxdb, db, "wind_turbine", "windspeed", 1.5)

        resp = client.call_tool("stop", {})
        _assert_mcp_success(resp)

        # 写新数据，等待 ≥2×timer 后确认无新增
        write_shm_block(shm_path(instance_id), 1, 10, 9.9, TS)
        time.sleep(0.3)
        r = query_latest(influxdb, db, "wind_turbine", "windspeed")
        assert r is not None and r[2] != 9.9

    # ── TC2: stop — 未启动时调用（幂等） ──────────

    def test_tc2_stop_idempotent(
        self, start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun68tc2"
        isolated_shm(instance_id)
        client = start_influxdb_client()
        resp = client.call_tool("stop", {})
        _assert_mcp_success(resp)

    # ── TC3: start — 已运行时重复调用 ──────────

    def test_tc3_start_already_running(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun68tc3"
        db = create_database()
        client, config_path = _start(
            prepare_environment, start_influxdb_client, isolated_shm,
            influxdb, db, instance_id, [_single_point()], ["pt1"],
        )
        resp = client.call_tool(
            "start", {"instance_id": instance_id, "config_path": config_path}
        )
        _assert_mcp_error(resp, "ALREADY_RUNNING")

    # ── TC4: 简单重启（stop → start） ──────────

    def test_tc4_simple_restart(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun68tc4"
        db = create_database()
        client, config_path = _start(
            prepare_environment, start_influxdb_client, isolated_shm,
            influxdb, db, instance_id, [_single_point()], ["pt1"],
        )

        write_shm_block(shm_path(instance_id), 1, 10, 1.5, TS)
        _wait_value(influxdb, db, "wind_turbine", "windspeed", 1.5)

        resp = client.call_tool("stop", {})
        _assert_mcp_success(resp)

        resp = client.call_tool(
            "start", {"instance_id": instance_id, "config_path": config_path}
        )
        _assert_mcp_success(resp)

        write_shm_block(shm_path(instance_id), 1, 10, 2.5, TS)
        _wait_value(influxdb, db, "wind_turbine", "windspeed", 2.5)

    # ── TC5: 完整 Stop-Start（stop → adjust_shm → start） ──────────

    def test_tc5_full_stop_start(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun68tc5"
        db = create_database()
        client, config_path = _start(
            prepare_environment, start_influxdb_client, isolated_shm,
            influxdb, db, instance_id, [_single_point()], ["pt1"],
        )

        write_shm_block(shm_path(instance_id), 1, 10, 1.5, TS)
        _wait_value(influxdb, db, "wind_turbine", "windspeed", 1.5)

        resp = client.call_tool("stop", {})
        _assert_mcp_success(resp)

        _run_adjust_shm(config_path, instance_id)

        resp = client.call_tool(
            "start", {"instance_id": instance_id, "config_path": config_path}
        )
        _assert_mcp_success(resp)

        write_shm_block(shm_path(instance_id), 1, 10, 2.5, TS)
        _wait_value(influxdb, db, "wind_turbine", "windspeed", 2.5)

    # ── TC6: 多次 stop/start 循环 ──────────

    def test_tc6_multiple_cycles(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun68tc6"
        db = create_database()
        client, config_path = _start(
            prepare_environment, start_influxdb_client, isolated_shm,
            influxdb, db, instance_id, [_single_point()], ["pt1"],
        )

        for _ in range(2):
            resp = client.call_tool("stop", {})
            _assert_mcp_success(resp)
            resp = client.call_tool(
                "start", {"instance_id": instance_id, "config_path": config_path}
            )
            _assert_mcp_success(resp)

        write_shm_block(shm_path(instance_id), 1, 10, 3.0, TS)
        _wait_value(influxdb, db, "wind_turbine", "windspeed", 3.0)

    # ── TC7: 重启时配置变更生效 ──────────

    def test_tc7_config_change(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun68tc7"
        db = create_database()
        # 初始：占位 writer 2 point，influxdb 1 point（引用 pt1）
        client, config_path = _start(
            prepare_environment, start_influxdb_client, isolated_shm,
            influxdb, db, instance_id,
            [_single_point("fake_writer.pt1", "wind_turbine", "windspeed")],
            ["pt1", "pt2"],
        )

        write_shm_block(shm_path(instance_id), 1, 10, 1.0, TS)
        _wait_value(influxdb, db, "wind_turbine", "windspeed", 1.0)

        resp = client.call_tool("stop", {})
        _assert_mcp_success(resp)

        # 修改配置：新增 influxdb point（引用 pt2）
        with open(config_path) as f:
            cfg = json.load(f)
        new_point = _single_point("fake_writer.pt2", "transformer", "uab")
        cfg["c4_influxdb_client"][0]["points"].append(new_point)
        fd, new_config_path = tempfile.mkstemp(suffix=".json", prefix="c4_config_")
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)

        _run_adjust_shm(new_config_path, instance_id)

        resp = client.call_tool(
            "start", {"instance_id": instance_id, "config_path": new_config_path}
        )
        _assert_mcp_success(resp)

        write_shm_block(shm_path(instance_id), 1, 10, 2.0, TS)
        write_shm_block(shm_path(instance_id), 2, 10, 220.0, TS)

        _wait_value(influxdb, db, "wind_turbine", "windspeed", 2.0)
        _wait_value(influxdb, db, "transformer", "uab", 220.0)

    # ── TC8: stop 时 flush 缓冲 ──────────

    def test_tc8_stop_flush(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun68tc8"
        db = create_database()
        client, config_path = _start(
            prepare_environment, start_influxdb_client, isolated_shm,
            influxdb, db, instance_id, [_single_point()], ["pt1"],
            flush_interval=10000,  # 10s，不自动 flush
        )

        write_shm_block(shm_path(instance_id), 1, 10, 8.8, TS)
        time.sleep(0.5)  # ≥3×timer，确保数据已读入缓冲

        # 确认尚无数据（数据停留在缓冲）
        r = query_latest(influxdb, db, "wind_turbine", "windspeed")
        assert r is None

        resp = client.call_tool("stop", {})
        _assert_mcp_success(resp)

        # stop 尽力 flush 缓冲，数据写入
        _wait_value(influxdb, db, "wind_turbine", "windspeed", 8.8)
