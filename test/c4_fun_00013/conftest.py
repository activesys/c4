"""
C4_FUN_00013 测试公共 fixture — 复用 c4_fun_00065 的 redis + iec104d + 授权 fixture。

复用方式同 c4_fun_00012 复用 c4_fun_00062（importlib.util）。
"""

import importlib.util
import os

_src_path = os.path.join(
    os.path.dirname(__file__), "../c4_fun_00065/conftest.py"
)
_spec = importlib.util.spec_from_file_location("c4_fun_00065_conftest", _src_path)
assert _spec is not None and _spec.loader is not None
_c65 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_c65)

# fixtures
license_env = _c65.license_env
redis_server = _c65.redis_server
write_redis = _c65.write_redis
start_iec104d = _c65.start_iec104d
shm_mgr_client = _c65.shm_mgr_client
isolated_shm = _c65.isolated_shm
prepare_environment = _c65.prepare_environment
start_iec104_client = _c65.start_iec104_client

# helpers
McpClient = _c65.McpClient
_free_port = _c65._free_port
_write_config_file = _c65._write_config_file
_wait_port_listening = _c65._wait_port_listening
_wait_port_released = _c65._wait_port_released
_stop_process = _c65._stop_process
_assert_mcp_success = _c65._assert_mcp_success
_assert_mcp_error = _c65._assert_mcp_error
wait_write_seq_advanced = _c65.wait_write_seq_advanced
_run_adjust_shm = _c65._run_adjust_shm

# config factories
_make_iec104d_config = _c65._make_iec104d_config
_make_iec104d_point = _c65._make_iec104d_point
_make_c4_config = _c65._make_c4_config
_make_c4_instance = _c65._make_c4_instance
_make_c4_point = _c65._make_c4_point
