"""
C4_FUN_00016 测试用例 — c4_influxdb_client 数据写入通路。

严格按 README.md 规格实现，不参考 Go 源码。
验证 line protocol 编码、类型转换、tag 转义、timestamp 精度等。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from conftest import (  # noqa: E402
    field_type,
    query_influx,
    query_latest,
    _assert_mcp_success,
    _make_c4_config,
    _make_influx_instance,
    _make_influx_point,
    write_shm_block,
    shm_path,
)


# ──────────────────────────────────────────────
#  轮询等待 helper（README §5.2）
# ──────────────────────────────────────────────

def _wait_field_type(url, db, m, f, expected, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if field_type(url, db, m, f) == expected:
            return
        time.sleep(interval)
    raise AssertionError(f"field {m}.{f} did not become {expected} within {timeout}s")


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


TS = 1768848814264  # 固定毫秒时间戳（2026-01-19）


def _start_and_prepare(prepare_environment, start_influxdb_client, isolated_shm,
                       influxdb, db, instance_id, points, writer_point_ids):
    """准备环境并启动 SUT，返回 (client, config_path)。"""
    isolated_shm(instance_id)
    inst = _make_influx_instance("入库", "test_influx", influxdb, db, points)
    config = _make_c4_config([inst], writer_point_ids)
    config_path, _ = prepare_environment(config, instance_id)
    client = start_influxdb_client()
    resp = client.call_tool(
        "start", {"instance_id": instance_id, "config_path": config_path}
    )
    _assert_mcp_success(resp)
    return client, config_path


class TestInfluxdbWrite:

    # ── TC1: 基本写入 — FLOAT32 跟随采集类型 ──────────

    def test_tc1_float32_auto(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun16tc1"
        db = create_database()
        pt = _make_influx_point(
            "fake_writer.pt1", "wind_turbine", field="windspeed",
            tags={"site": "hnals"}, shm_id=0,
        )
        _start_and_prepare(prepare_environment, start_influxdb_client, isolated_shm,
                           influxdb, db, instance_id, [pt], ["pt1"])

        write_shm_block(shm_path(instance_id), 1, 10, 12.5, TS)

        _wait_field_type(influxdb, db, "wind_turbine", "windspeed", "float")
        _wait_value(influxdb, db, "wind_turbine", "windspeed", 12.5)
        r = query_latest(influxdb, db, "wind_turbine", "windspeed")
        assert r is not None
        tags, _ts, val = r
        assert abs(val - 12.5) < 1e-6
        assert tags.get("site") == "hnals"

    # ── TC2: 显式转换 INT32 → float ──────────

    def test_tc2_int32_to_float(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun16tc2"
        db = create_database()
        pt = _make_influx_point(
            "fake_writer.pt1", "wind_turbine", field="windspeed",
            type_="float", tags={"site": "hnals"}, shm_id=0,
        )
        _start_and_prepare(prepare_environment, start_influxdb_client, isolated_shm,
                           influxdb, db, instance_id, [pt], ["pt1"])

        write_shm_block(shm_path(instance_id), 1, 5, 25, TS)

        _wait_field_type(influxdb, db, "wind_turbine", "windspeed", "float")
        assert field_type(influxdb, db, "wind_turbine", "windspeed") != "integer"
        _wait_value(influxdb, db, "wind_turbine", "windspeed", 25)
        r = query_latest(influxdb, db, "wind_turbine", "windspeed")
        assert r is not None and r[2] == 25

    # ── TC3: 整数 int 后缀 → integer ──────────

    def test_tc3_int32_to_int(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun16tc3"
        db = create_database()
        pt = _make_influx_point(
            "fake_writer.pt1", "wind_turbine", field="windspeed",
            type_="int", tags={"site": "hnals"}, shm_id=0,
        )
        _start_and_prepare(prepare_environment, start_influxdb_client, isolated_shm,
                           influxdb, db, instance_id, [pt], ["pt1"])

        write_shm_block(shm_path(instance_id), 1, 5, 25, TS)

        _wait_field_type(influxdb, db, "wind_turbine", "windspeed", "integer")
        _wait_value(influxdb, db, "wind_turbine", "windspeed", 25)
        r = query_latest(influxdb, db, "wind_turbine", "windspeed")
        assert r is not None and r[2] == 25

    # ── TC4: 布尔类型 → boolean ──────────

    def test_tc4_boolean(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun16tc4"
        db = create_database()
        pt = _make_influx_point(
            "fake_writer.pt1", "wind_turbine", field="status",
            tags={"site": "hnals"}, shm_id=0,
        )
        _start_and_prepare(prepare_environment, start_influxdb_client, isolated_shm,
                           influxdb, db, instance_id, [pt], ["pt1"])

        write_shm_block(shm_path(instance_id), 1, 0, 1, TS)
        _wait_value(influxdb, db, "wind_turbine", "status", True)

        write_shm_block(shm_path(instance_id), 1, 0, 0, TS)
        _wait_value(influxdb, db, "wind_turbine", "status", False)

        assert field_type(influxdb, db, "wind_turbine", "status") == "boolean"

    # ── TC5: 多类型混合（3 point 同实例） ──────────

    def test_tc5_mixed_types(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun16tc5"
        db = create_database()
        pts = [
            _make_influx_point("fake_writer.pt1", "wind_turbine", field="windspeed", shm_id=0),
            _make_influx_point("fake_writer.pt2", "wind_turbine", field="temperature", type_="float", shm_id=0),
            _make_influx_point("fake_writer.pt3", "wind_turbine", field="status", shm_id=0),
        ]
        _start_and_prepare(prepare_environment, start_influxdb_client, isolated_shm,
                           influxdb, db, instance_id, pts, ["pt1", "pt2", "pt3"])

        write_shm_block(shm_path(instance_id), 1, 10, 12.5, TS)
        write_shm_block(shm_path(instance_id), 2, 5, 25, TS)
        write_shm_block(shm_path(instance_id), 3, 0, 1, TS)

        _wait_field_type(influxdb, db, "wind_turbine", "windspeed", "float")
        _wait_field_type(influxdb, db, "wind_turbine", "temperature", "float")
        _wait_field_type(influxdb, db, "wind_turbine", "status", "boolean")

    # ── TC6: tag 转义（中文 / 逗号 / 空格 / 等号） ──────────

    def test_tc6_tag_escaping(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun16tc6"
        db = create_database()
        pt = _make_influx_point(
            "fake_writer.pt1", "wind_turbine", field="windspeed",
            tags={"site": "华能,阿拉善", "region": "I 区", "eq": "a=b"}, shm_id=0,
        )
        _start_and_prepare(prepare_environment, start_influxdb_client, isolated_shm,
                           influxdb, db, instance_id, [pt], ["pt1"])

        write_shm_block(shm_path(instance_id), 1, 10, 3.5, TS)
        _wait_value(influxdb, db, "wind_turbine", "windspeed", 3.5)

        r = query_latest(influxdb, db, "wind_turbine", "windspeed")
        assert r is not None
        tags, _ts, _val = r
        assert tags.get("site") == "华能,阿拉善"
        assert tags.get("region") == "I 区"
        assert tags.get("eq") == "a=b"

    # ── TC7: 数据变化（值更新，同一固定 timestamp UPSERT） ──────────

    def test_tc7_value_update(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun16tc7"
        db = create_database()
        pt = _make_influx_point(
            "fake_writer.pt1", "wind_turbine", field="windspeed",
            tags={"site": "hnals"}, shm_id=0,
        )
        _start_and_prepare(prepare_environment, start_influxdb_client, isolated_shm,
                           influxdb, db, instance_id, [pt], ["pt1"])

        write_shm_block(shm_path(instance_id), 1, 10, 1.5, TS)
        _wait_value(influxdb, db, "wind_turbine", "windspeed", 1.5)

        write_shm_block(shm_path(instance_id), 1, 10, 2.5, TS)
        _wait_value(influxdb, db, "wind_turbine", "windspeed", 2.5)

    # ── TC8: 非数值类型跳过 ──────────

    def test_tc8_non_numeric_skipped(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun16tc8"
        db = create_database()
        pts = [
            _make_influx_point("fake_writer.pt1", "wind_turbine", field="windspeed", shm_id=0),
            _make_influx_point("fake_writer.pt2", "str_measurement", field="sval", shm_id=0),
        ]
        _start_and_prepare(prepare_environment, start_influxdb_client, isolated_shm,
                           influxdb, db, instance_id, pts, ["pt1", "pt2"])

        write_shm_block(shm_path(instance_id), 1, 10, 3.5, TS)
        _wait_value(influxdb, db, "wind_turbine", "windspeed", 3.5)

        # 写 STRING block（type=12，非数值）
        write_shm_block(shm_path(instance_id), 2, 12, None, TS)
        time.sleep(0.5)  # 等待 ≥1 轮询周期

        assert field_type(influxdb, db, "str_measurement", "sval") is None

    # ── TC9: timestamp 精度（precision=ms 透传） ──────────

    def test_tc9_timestamp_precision(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun16tc9"
        db = create_database()
        pt = _make_influx_point(
            "fake_writer.pt1", "wind_turbine", field="windspeed",
            tags={"site": "hnals"}, shm_id=0,
        )
        _start_and_prepare(prepare_environment, start_influxdb_client, isolated_shm,
                           influxdb, db, instance_id, [pt], ["pt1"])

        write_shm_block(shm_path(instance_id), 1, 10, 4.5, TS)
        _wait_value(influxdb, db, "wind_turbine", "windspeed", 4.5)

        r = query_latest(influxdb, db, "wind_turbine", "windspeed")
        assert r is not None
        _tags, time_str, _val = r
        assert time_str is not None
        assert time_str.startswith("2026")

    # ── TC10: 多个 measurement（不同 point 不同表） ──────────

    def test_tc10_multiple_measurements(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun16tc10"
        db = create_database()
        pts = [
            _make_influx_point("fake_writer.pt1", "wind_turbine", field="windspeed", shm_id=0),
            _make_influx_point("fake_writer.pt2", "transformer", field="uab", shm_id=0),
        ]
        _start_and_prepare(prepare_environment, start_influxdb_client, isolated_shm,
                           influxdb, db, instance_id, pts, ["pt1", "pt2"])

        write_shm_block(shm_path(instance_id), 1, 10, 6.0, TS)
        write_shm_block(shm_path(instance_id), 2, 10, 110.0, TS)

        _wait_value(influxdb, db, "wind_turbine", "windspeed", 6.0)
        _wait_value(influxdb, db, "transformer", "uab", 110.0)

    # ── TC11: 整数缺省（auto）→ integer ──────────

    def test_tc11_int32_auto(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun16tc11"
        db = create_database()
        pt = _make_influx_point(
            "fake_writer.pt1", "wind_turbine", field="windspeed",
            tags={"site": "hnals"}, shm_id=0,
        )
        _start_and_prepare(prepare_environment, start_influxdb_client, isolated_shm,
                           influxdb, db, instance_id, [pt], ["pt1"])

        write_shm_block(shm_path(instance_id), 1, 5, 25, TS)

        _wait_field_type(influxdb, db, "wind_turbine", "windspeed", "integer")
        _wait_value(influxdb, db, "wind_turbine", "windspeed", 25)
        r = query_latest(influxdb, db, "wind_turbine", "windspeed")
        assert r is not None and r[2] == 25
