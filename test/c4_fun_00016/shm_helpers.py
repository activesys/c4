"""复用 c4_fun_00067 的 shm_helpers.py（不复制实现，仅复用）。"""

import importlib.util
import os

_src = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "../c4_fun_00067/shm_helpers.py"
)
_spec = importlib.util.spec_from_file_location("c4_fun_00067_shm_helpers", _src)
assert _spec is not None and _spec.loader is not None
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)

shm_unlink = _m.shm_unlink
shm_path = _m.shm_path
read_shm_header = _m.read_shm_header
read_shm_block = _m.read_shm_block
get_shm_size = _m.get_shm_size
read_write_seq = _m.read_write_seq
read_shm_value = _m.read_shm_value
write_shm_block = _m.write_shm_block
