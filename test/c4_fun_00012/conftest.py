"""
C4_FUN_00012 测试公共 fixture — 复用 c4_fun_00062 的 redis + modbusd + 授权 fixture。

复用方式同 c4_fun_00058 复用 c4_fun_00057（importlib.util）：
  license_env / start_modbusd / write_redis / prepare_environment / start_modbus_client
另补充 shm_mgr_client / isolated_shm / redis_server 及配置工厂与断言 helper。
"""

import importlib.util
import os

# ──────────────────────────────────────────────
#  复用 c4_fun_00062 的 fixture 与 helper
# ──────────────────────────────────────────────

_src_path = os.path.join(os.path.dirname(__file__), "../c4_fun_00062/conftest.py")
_spec = importlib.util.spec_from_file_location("c4_fun_00062_conftest", _src_path)
assert _spec is not None and _spec.loader is not None
_c62 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_c62)

# fixtures
license_env = _c62.license_env
redis_server = _c62.redis_server
start_modbusd = _c62.start_modbusd
write_redis = _c62.write_redis
shm_mgr_client = _c62.shm_mgr_client
isolated_shm = _c62.isolated_shm
prepare_environment = _c62.prepare_environment
start_modbus_client = _c62.start_modbus_client

# helpers
McpClient = _c62.McpClient
_free_port = _c62._free_port
_write_config_file = _c62._write_config_file
_wait_port_listening = _c62._wait_port_listening
_wait_port_released = _c62._wait_port_released
_stop_process = _c62._stop_process
_assert_mcp_success = _c62._assert_mcp_success
_assert_mcp_error = _c62._assert_mcp_error
wait_write_seq_advanced = _c62.wait_write_seq_advanced

# config factories
_make_modbusd_config = _c62._make_modbusd_config
_make_modbusd_point = _c62._make_modbusd_point
_make_c4_config = _c62._make_c4_config
_make_c4_instance = _c62._make_c4_instance
_make_c4_point = _c62._make_c4_point
