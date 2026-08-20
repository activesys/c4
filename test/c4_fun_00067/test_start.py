"""
C4_FUN_00067 测试用例 — c4_influxdb_client start 工具。

严格按 README.md 规格实现，不参考 Go 源码。
"""

import json
import mmap
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # type: ignore

from conftest import (  # noqa: E402
    _assert_mcp_error,
    _assert_mcp_success,
    _free_port,
    _make_c4_config,
    _make_influx_instance,
    _make_influx_point,
)
from shm_helpers import shm_path  # noqa: E402


# ──────────────────────────────────────────────
#  辅助
# ──────────────────────────────────────────────

def _write_config(config_dict):
    fd, path = tempfile.mkstemp(suffix=".json", prefix="c4_config_")
    with os.fdopen(fd, "w") as f:
        json.dump(config_dict, f)
    return path


def _create_shm_default(shm_mgr_client, instance_id):
    """create_shm 不带 config_path → 默认 10 万点、不回填 shm_id。

    （create_shm 带 config_path 会回填 shm_id，故错误场景须用不带 config_path 的方式，
    保持配置中 shm_id 为手工填写的值。）
    """
    resp = shm_mgr_client.call_tool("create_shm", {"instance_id": instance_id})
    assert not resp["result"].get("isError", False), resp


def _corrupt_header_magic(instance_id):
    full = shm_path(instance_id)
    fd = os.open(full, os.O_RDWR)
    shm = mmap.mmap(fd, 4, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
    shm.seek(0)
    shm.write(struct.pack("=I", 0xDEADBEEF))
    shm.flush()
    shm.close()
    os.close(fd)


def _valid_instance(url, bucket):
    pt = _make_influx_point(
        "fake_writer.pt1", "wind_turbine", field="windspeed",
        type_="float", tags={"site": "hnals"}, shm_id=1,
    )
    return _make_influx_instance("入库", "test_influx", url, bucket, [pt])


def _valid_config(url, bucket):
    return _make_c4_config([_valid_instance(url, bucket)], ["pt1"])


class TestInfluxdbClientStart:

    # ── TC1: 基本启动 — 单实例单点 ──────────

    def test_tc1_basic_startup(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun67tc1"
        isolated_shm(instance_id)
        db = create_database()
        config = _valid_config(influxdb, db)
        config_path, _ = prepare_environment(config, instance_id)

        sut = start_influxdb_client()
        resp = sut.call_tool(
            "start", {"instance_id": instance_id, "config_path": config_path}
        )
        _assert_mcp_success(resp)

    # ── TC2: 多实例启动 — 3 个实例 ──────────

    def test_tc2_multi_instance(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun67tc2"
        isolated_shm(instance_id)
        db = create_database()
        insts = []
        for i in range(3):
            pt = _make_influx_point(
                f"fake_writer.pt{i+1}", "wind_turbine", field="windspeed",
                type_="float", tags={"site": "hnals"}, shm_id=0,
            )
            insts.append(_make_influx_instance(f"入库{i}", f"db{i}", influxdb, db, [pt]))
        config = _make_c4_config(insts, ["pt1", "pt2", "pt3"])
        config_path, _ = prepare_environment(config, instance_id)

        sut = start_influxdb_client()
        resp = sut.call_tool(
            "start", {"instance_id": instance_id, "config_path": config_path}
        )
        _assert_mcp_success(resp)

    # ── TC3: 空实例列表 — 0 个实例 ──────────

    def test_tc3_empty_instances(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun67tc3"
        isolated_shm(instance_id)
        db = create_database()
        config = _make_c4_config([], ["pt1"])  # 占位 writer 保留，influxdb 空数组
        config_path, _ = prepare_environment(config, instance_id)

        sut = start_influxdb_client()
        resp = sut.call_tool(
            "start", {"instance_id": instance_id, "config_path": config_path}
        )
        _assert_mcp_success(resp)

    # ── TC4: 重复调用 start → ALREADY_RUNNING ──────────

    def test_tc4_already_running(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun67tc4"
        isolated_shm(instance_id)
        db = create_database()
        config = _valid_config(influxdb, db)
        config_path, _ = prepare_environment(config, instance_id)

        sut = start_influxdb_client()
        resp = sut.call_tool(
            "start", {"instance_id": instance_id, "config_path": config_path}
        )
        _assert_mcp_success(resp)

        resp2 = sut.call_tool(
            "start", {"instance_id": instance_id, "config_path": config_path}
        )
        _assert_mcp_error(resp2, "ALREADY_RUNNING")

    # ── TC5: start 未调用前调用 stop → 幂等 success ──────────

    def test_tc5_stop_before_start(
        self, start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun67tc5"
        isolated_shm(instance_id)

        sut = start_influxdb_client()
        resp = sut.call_tool("stop", {})
        _assert_mcp_success(resp)

    # ── TC6: config_path 缺失 → CONFIG_PATH_MISSING ──────────

    def test_tc6_missing_config_path(
        self, start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun67tc6"
        isolated_shm(instance_id)

        sut = start_influxdb_client()
        resp = sut.call_tool("start", {"instance_id": instance_id})
        _assert_mcp_error(resp, "CONFIG_PATH_MISSING")

    # ── TC7: 配置文件格式错误 → CONFIG_PARSE_ERROR ──────────

    @pytest.mark.parametrize(
        "bad_content",
        [
            "{invalid json",
            json.dumps({"c4_shm_manager": {"writer": ["c4_modbus_client"], "reader": ["c4_influxdb_client"]}}),
        ],
        ids=["json_syntax_error", "missing_section"],
    )
    def test_tc7_config_parse_error(
        self, bad_content, start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun67tc7"
        isolated_shm(instance_id)
        fd, config_path = tempfile.mkstemp(suffix=".json", prefix="c4_config_")
        with os.fdopen(fd, "w") as f:
            f.write(bad_content)

        sut = start_influxdb_client()
        resp = sut.call_tool(
            "start", {"instance_id": instance_id, "config_path": config_path}
        )
        _assert_mcp_error(resp, "CONFIG_PARSE_ERROR")

    # ── TC8: 共享内存不存在 → SHM_OPEN_FAILED ──────────

    def test_tc8_shm_open_failed(
        self, influxdb, create_database, start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun67tc8"
        isolated_shm(instance_id)
        db = create_database()
        config = _valid_config(influxdb, db)  # point shm_id 手工为 1（非 0）
        config_path = _write_config(config)
        # 不创建共享内存

        sut = start_influxdb_client()
        resp = sut.call_tool(
            "start", {"instance_id": instance_id, "config_path": config_path}
        )
        _assert_mcp_error(resp, "SHM_OPEN_FAILED")

    # ── TC9: 共享内存 magic 损坏 → SHM_CORRUPTED ──────────

    def test_tc9_shm_corrupted(
        self, influxdb, create_database, prepare_environment,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun67tc9"
        isolated_shm(instance_id)
        db = create_database()
        config = _valid_config(influxdb, db)
        config_path, _ = prepare_environment(config, instance_id)

        _corrupt_header_magic(instance_id)

        sut = start_influxdb_client()
        resp = sut.call_tool(
            "start", {"instance_id": instance_id, "config_path": config_path}
        )
        _assert_mcp_error(resp, "SHM_CORRUPTED")

    # ── TC10: shm_id 未分配（=0）→ SHM_ID_NOT_ASSIGNED ──────────

    def test_tc10_shm_id_not_assigned(
        self, influxdb, create_database, shm_mgr_client,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun67tc10"
        isolated_shm(instance_id)
        db = create_database()
        # point shm_id=0（未分配）
        pt = _make_influx_point(
            "fake_writer.pt1", "wind_turbine", field="windspeed",
            type_="float", tags={"site": "hnals"}, shm_id=0,
        )
        inst = _make_influx_instance("入库", "test_influx", influxdb, db, [pt])
        config = _make_c4_config([inst], ["pt1"])
        config_path = _write_config(config)
        # 只 create_shm，跳过 adjust_shm（shm_id 仍为 0）
        _create_shm_default(shm_mgr_client, instance_id)

        sut = start_influxdb_client()
        resp = sut.call_tool(
            "start", {"instance_id": instance_id, "config_path": config_path}
        )
        _assert_mcp_error(resp, "SHM_ID_NOT_ASSIGNED")

    # ── TC11: 实例级字段非法 → INVALID_CONFIG ──────────

    @pytest.mark.parametrize(
        "label,mutate",
        [
            ("url_missing", lambda i: i.pop("url") or i),
            ("url_bad_format", lambda i: i.update({"url": "not-a-url"}) or i),
            ("token_missing", lambda i: i.pop("token") or i),
            ("token_empty", lambda i: i.update({"token": ""}) or i),
            ("org_missing", lambda i: i.pop("org") or i),
            ("bucket_missing", lambda i: i.pop("bucket") or i),
            ("batch_size_zero", lambda i: i.update({"batch_size": 0}) or i),
            ("flush_interval_negative", lambda i: i.update({"flush_interval": -1}) or i),
        ],
    )
    def test_tc11_invalid_config(
        self, label, mutate, influxdb, create_database, shm_mgr_client,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun67tc11"
        isolated_shm(instance_id)
        db = create_database()
        inst = _valid_instance(influxdb, db)
        mutate(inst)
        config = _make_c4_config([inst], ["pt1"])
        config_path = _write_config(config)
        _create_shm_default(shm_mgr_client, instance_id)

        sut = start_influxdb_client()
        resp = sut.call_tool(
            "start", {"instance_id": instance_id, "config_path": config_path}
        )
        _assert_mcp_error(resp, "INVALID_CONFIG")

    # ── TC12: point 字段非法 → INVALID_POINT ──────────

    @pytest.mark.parametrize(
        "label,mutate_point",
        [
            ("type_invalid", lambda p: p.update({"type": "string"}) or p),
            ("measurement_empty", lambda p: p.update({"measurement": ""}) or p),
            ("tag_key_invalid", lambda p: p.update({"tags": {"bad.key": "v"}}) or p),
            ("field_key_invalid", lambda p: p.update({"field": "bad-key"}) or p),
            ("shm_id_duplicate", None),
        ],
    )
    def test_tc12_invalid_point(
        self, label, mutate_point, influxdb, create_database, shm_mgr_client,
        start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun67tc12"
        isolated_shm(instance_id)
        db = create_database()

        if label == "shm_id_duplicate":
            pt1 = _make_influx_point(
                "fake_writer.pt1", "wind_turbine", field="windspeed",
                type_="float", tags={"site": "hnals"}, shm_id=1,
            )
            pt2 = _make_influx_point(
                "fake_writer.pt2", "transformer", field="uab",
                type_="float", tags={"site": "hnals"}, shm_id=1,
            )
            inst = _make_influx_instance("入库", "test_influx", influxdb, db, [pt1, pt2])
        else:
            pt = _make_influx_point(
                "fake_writer.pt1", "wind_turbine", field="windspeed",
                type_="float", tags={"site": "hnals"}, shm_id=1,
            )
            mutate_point(pt)
            inst = _make_influx_instance("入库", "test_influx", influxdb, db, [pt])

        config = _make_c4_config([inst], ["pt1", "pt2"])
        config_path = _write_config(config)
        _create_shm_default(shm_mgr_client, instance_id)

        sut = start_influxdb_client()
        resp = sut.call_tool(
            "start", {"instance_id": instance_id, "config_path": config_path}
        )
        _assert_mcp_error(resp, "INVALID_POINT")

    # ── TC13: InfluxDB 不可达 — start 仍返回 success ──────────

    def test_tc13_unreachable_influxdb(
        self, prepare_environment, start_influxdb_client, isolated_shm,
    ):
        instance_id = "c4_fun67tc13"
        isolated_shm(instance_id)
        unreachable_port = _free_port()
        unreachable_url = f"http://127.0.0.1:{unreachable_port}"
        config = _valid_config(unreachable_url, "testdb")
        config_path, _ = prepare_environment(config, instance_id)

        sut = start_influxdb_client()
        resp = sut.call_tool(
            "start", {"instance_id": instance_id, "config_path": config_path}
        )
        _assert_mcp_success(resp)
