"""
C4_FUN_00067 测试公共基础设施 — MCP 客户端 + 真实 InfluxDB 1.8.10 fixtures。

复用 c4_fun_00062 的 shm_manager/隔离 fixture（importlib.util，同 c4_fun_00012），
新增真实 InfluxDB 1.8.10 启动、database 准备、c4_influxdb_client SUT 启动。

对应 c4_fun_00067 README §2.5 的 fixture 契约：
  shm_mgr_client        (function) 复用 c4_fun_00062——启动 c4_shm_manager
  isolated_shm          (function) 复用 c4_fun_00062——shm 隔离/清理
  prepare_environment   (function) 复用 c4_fun_00062——create_shm + adjust_shm
  influxdb              (session)  启动真实 InfluxDB 1.8.10，返回 URL
  create_database       (function) 创建独立 database，返回 db 名，teardown DROP
  start_influxdb_client (function) 启动 c4_influxdb_client（MCP initialize）

共享内存操作见 shm_helpers.py。
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # type: ignore


# ──────────────────────────────────────────────
#  复用 c4_fun_00062 的 fixture 与 helper
# ──────────────────────────────────────────────

_src_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "../c4_fun_00062/conftest.py"
)
_spec = importlib.util.spec_from_file_location("c4_fun_00062_conftest", _src_path)
assert _spec is not None and _spec.loader is not None
_c62 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_c62)

shm_mgr_client = _c62.shm_mgr_client
isolated_shm = _c62.isolated_shm
prepare_environment = _c62.prepare_environment

McpClient = _c62.McpClient
_free_port = _c62._free_port
_assert_mcp_success = _c62._assert_mcp_success
_assert_mcp_error = _c62._assert_mcp_error
_run_adjust_shm = _c62._run_adjust_shm


# ──────────────────────────────────────────────
#  InfluxDB 查询 helper（供 c4_fun_00016 复用）
# ──────────────────────────────────────────────

def query_influx(url: str, db, q: str) -> list:
    """POST /query 执行查询，返回 results[0].series（无结果返回 []）。"""
    full_url = url + "/query"
    if db:
        full_url += "?db=" + urllib.parse.quote(db)
    data = urllib.parse.urlencode({"q": q}).encode("utf-8")
    req = urllib.request.Request(full_url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        body = json.loads(r.read().decode("utf-8"))
    results = body.get("results", [])
    if not results:
        return []
    return results[0].get("series", [])


def field_type(url: str, db: str, measurement: str, field: str):
    """SHOW FIELD KEYS 查询指定 field 的 fieldType，不存在返回 None。"""
    series = query_influx(url, db, f"SHOW FIELD KEYS FROM {measurement}")
    for s in series:
        columns = s.get("columns", [])
        values = s.get("values", [])
        if columns == ["fieldKey", "fieldType"]:
            for row in values:
                if row[0] == field:
                    return row[1]
    return None


def query_latest(url: str, db: str, measurement: str, field: str):
    """SELECT 查询指定 field 的最新值（含 tag 列与 time 列）。

    返回最新一行对应的 (tags_dict, time_str, value)，若无数据返回 None。
    """
    series = query_influx(url, db, f"SELECT * FROM {measurement}")
    for s in series:
        columns = s.get("columns", [])
        values = s.get("values", [])
        if not values:
            continue
        # 取最后一行（InfluxDB 默认按 time 升序，最后一行为最新）
        row = values[-1]
        tags = {}
        time_str = None
        value = None
        for c, v in zip(columns, row):
            if c == "time":
                time_str = v
            elif c == field:
                value = v
            else:
                tags[c] = v
        return tags, time_str, value
    return None


# ──────────────────────────────────────────────
#  SUT 二进制发现
# ──────────────────────────────────────────────

def _find_influxdb_client_binary() -> str:
    """查找或编译 c4_influxdb_client 二进制。"""
    path = os.environ.get("C4_INFLUXDB_CLIENT_PATH")
    if path and os.path.isfile(path):
        return path

    test_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(test_dir, "../../mcp/c4_influxdb_client/c4_influxdb_client"),
        os.path.join(test_dir, "../../mcp/c4_influxdb_client/build/c4_influxdb_client"),
        os.path.join(test_dir, "../../build/mcp/c4_influxdb_client/c4_influxdb_client"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return os.path.abspath(p)

    src_dir = os.path.abspath(os.path.join(test_dir, "../../mcp/c4_influxdb_client"))
    if os.path.isdir(src_dir):
        result = subprocess.run(
            ["go", "build", "-o", "c4_influxdb_client", "."],
            cwd=src_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return os.path.join(src_dir, "c4_influxdb_client")
        else:
            pytest.skip(f"Failed to build c4_influxdb_client: {result.stderr.strip()}")

    pytest.skip(
        "c4_influxdb_client binary not found. "
        "Set C4_INFLUXDB_CLIENT_PATH env var or build from c4/mcp/c4_influxdb_client"
    )
    return ""  # unreachable


# ──────────────────────────────────────────────
#  配置工厂
# ──────────────────────────────────────────────

def _make_placeholder_writer(point_ids):
    """占位 c4_modbus_client 实例（不启动），point_ids 如 ["pt1", "pt2"]。"""
    points = [
        {"id": pid, "uid": 1, "addr": i, "fun": 3, "type": 5, "swap": 0, "shm_id": 0}
        for i, pid in enumerate(point_ids)
    ]
    return {
        "name": "占位采集", "id": "fake_writer",
        "ip": "127.0.0.1", "port": 502,
        "t0": 5, "t1": 5, "retries": 1,
        "coils_quantity_max": 2000, "registers_quantity_max": 125,
        "hton_register": 1, "hton_total": 0, "timer": 1000,
        "points": points,
    }


def _make_influx_point(key, measurement, field=None, type_=None, tags=None, shm_id=0):
    pt = {"key": key, "measurement": measurement, "shm_id": shm_id}
    if field is not None:
        pt["field"] = field
    if type_ is not None:
        pt["type"] = type_
    if tags is not None:
        pt["tags"] = tags
    return pt


def _make_influx_instance(name, iid, url, bucket, points,
                          precision="ms", batch_size=5000, flush_interval=100,
                          timer=100, gzip=0, t0=5, retries=1,
                          token="test-token", org="activesys"):
    return {
        "name": name, "id": iid,
        "url": url, "token": token, "org": org, "bucket": bucket,
        "precision": precision,
        "batch_size": batch_size,
        "flush_interval": flush_interval,
        "timer": timer, "gzip": gzip, "t0": t0, "retries": retries,
        "points": points,
    }


def _make_c4_config(influx_instances, writer_point_ids):
    return {
        "c4_shm_manager": {"writer": ["c4_modbus_client"], "reader": ["c4_influxdb_client"]},
        "c4_modbus_client": [_make_placeholder_writer(writer_point_ids)],
        "c4_influxdb_client": influx_instances,
    }


# ──────────────────────────────────────────────
#  Fixtures
# ──────────────────────────────────────────────

def _wait_ping(url: str, timeout: float = 15.0, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url + "/ping", method="GET")
            with urllib.request.urlopen(req, timeout=1.0) as r:
                if r.status == 204:
                    return
        except Exception:
            pass
        time.sleep(interval)
    raise RuntimeError(f"influxd did not become ready within {timeout}s")


@pytest.fixture(scope="session")
def influxdb():
    """启动真实 InfluxDB 1.8.10（独立端口 + 临时数据目录），返回 http://127.0.0.1:<port>。"""
    influxd = os.environ.get(
        "INFLUXD_PATH",
        "/home/wangbo/backup/influxdb/influxdb-1.8.10-1/usr/bin/influxd",
    )
    if not os.path.isfile(influxd):
        pytest.skip(f"influxd binary not found at {influxd}, set INFLUXD_PATH")

    port = _free_port()
    tmpdir = tempfile.mkdtemp(prefix="c4_influxdb_")
    meta_dir = os.path.join(tmpdir, "meta")
    data_dir = os.path.join(tmpdir, "data")
    wal_dir = os.path.join(tmpdir, "wal")
    for d in (meta_dir, data_dir, wal_dir):
        os.makedirs(d)

    cfg_path = os.path.join(tmpdir, "influxdb.conf")
    with open(cfg_path, "w") as f:
        f.write(
            "reporting-disabled = true\n"
            "[meta]\n"
            f'  dir = "{meta_dir}"\n'
            "[data]\n"
            f'  dir = "{data_dir}"\n'
            f'  wal-dir = "{wal_dir}"\n'
            "[http]\n"
            f'  bind-address = "127.0.0.1:{port}"\n'
            "  auth-enabled = false\n"
        )

    log_path = os.path.join(tmpdir, "influxd.log")
    logf = open(log_path, "w")
    proc = subprocess.Popen(
        [influxd, "run", "-config", cfg_path],
        stdout=logf,
        stderr=logf,
        start_new_session=True,  # setsid，脱离测试进程
    )

    url = f"http://127.0.0.1:{port}"
    try:
        _wait_ping(url, timeout=15.0)
    except RuntimeError:
        proc.kill()
        proc.wait()
        logf.close()
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

    yield url

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    logf.close()
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def create_database(influxdb):
    """在 influxdb 上创建独立 database，返回 db 名，teardown 时 DROP。"""
    dbs = []

    def _create():
        db = f"test_{uuid.uuid4().hex[:8]}"
        query_influx(influxdb, None, f"CREATE DATABASE {db}")
        dbs.append(db)
        return db

    yield _create

    for db in dbs:
        try:
            query_influx(influxdb, None, f"DROP DATABASE {db}")
        except Exception:
            pass


@pytest.fixture
def start_influxdb_client():
    """启动 c4_influxdb_client（MCP initialize），返回 MCP 客户端句柄。"""
    clients = []

    def _start():
        binary = _find_influxdb_client_binary()
        client = McpClient(binary)
        clients.append(client)
        return client

    yield _start

    for client in clients:
        client.close()
