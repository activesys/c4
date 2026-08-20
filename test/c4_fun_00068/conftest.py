"""复用 c4_fun_00067 的 fixture 与查询 helper。"""

import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_src = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "../c4_fun_00067/conftest.py"
)
_spec = importlib.util.spec_from_file_location("c4_fun_00067_conftest", _src)
assert _spec is not None and _spec.loader is not None
_c67 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_c67)

# fixtures
shm_mgr_client = _c67.shm_mgr_client
isolated_shm = _c67.isolated_shm
prepare_environment = _c67.prepare_environment
influxdb = _c67.influxdb
create_database = _c67.create_database
start_influxdb_client = _c67.start_influxdb_client

# 查询 helper
query_influx = _c67.query_influx
field_type = _c67.field_type
query_latest = _c67.query_latest

# 配置工厂
_make_c4_config = _c67._make_c4_config
_make_influx_instance = _c67._make_influx_instance
_make_influx_point = _c67._make_influx_point
_assert_mcp_success = _c67._assert_mcp_success
_assert_mcp_error = _c67._assert_mcp_error
_run_adjust_shm = _c67._run_adjust_shm

_shm_src = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "../c4_fun_00067/shm_helpers.py"
)
_shm_spec = importlib.util.spec_from_file_location("c4_fun_00067_shm_helpers", _shm_src)
assert _shm_spec is not None and _shm_spec.loader is not None
_shm_mod = importlib.util.module_from_spec(_shm_spec)
_shm_spec.loader.exec_module(_shm_mod)
write_shm_block = _shm_mod.write_shm_block
shm_path = _shm_mod.shm_path
shm_unlink = _shm_mod.shm_unlink
