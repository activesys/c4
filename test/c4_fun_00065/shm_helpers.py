"""POSIX 共享内存操作辅助函数，供测试代码和 conftest 共用。

复用 c4_fun_00057 的 shm_helpers.py，并扩展本机序 value 读取（对应 c4_modbus_client
设计文档 §5.2「值以本机序写入 value 字段低位字节」）。
"""

import ctypes
import ctypes.util
import mmap
import os
import struct
from typing import Optional


def _get_libc():
    """加载 libc，用于 shm_unlink。"""
    libc_path: Optional[str] = ctypes.util.find_library("c")
    if not libc_path:
        libc_path = "libc.so.6"
    libc = ctypes.CDLL(libc_path, use_errno=True)
    libc.shm_unlink.argtypes = [ctypes.c_char_p]
    libc.shm_unlink.restype = ctypes.c_int
    return libc


_libc: Optional[ctypes.CDLL] = None


def shm_unlink(name: str) -> None:
    """删除 POSIX 共享内存对象 '/{name}'。"""
    global _libc
    if _libc is None:
        _libc = _get_libc()
    name_bytes = name.encode("utf-8")
    result = _libc.shm_unlink(name_bytes)
    if result != 0:
        err = ctypes.get_errno()
        if err != 2:  # ENOENT 可忽略
            raise OSError(err, f"shm_unlink({name}) failed")


def shm_path(instance_id: str) -> str:
    return f"/dev/shm/{instance_id}"


def read_shm_header(full_path: str) -> dict:
    """读取并解析共享内存的 32 字节 Header 块。"""
    fd = os.open(full_path, os.O_RDONLY)
    shm: Optional[mmap.mmap] = None
    try:
        shm = mmap.mmap(fd, 32, mmap.MAP_SHARED, mmap.PROT_READ)
        data = shm.read(32)
        return {
            "magic": struct.unpack("=I", data[0:4])[0],
            "version": struct.unpack("=H", data[4:6])[0],
            "reserved": struct.unpack("=H", data[6:8])[0],
            "point_count": struct.unpack("=I", data[8:12])[0],
            "max_points": struct.unpack("=I", data[12:16])[0],
            "global_write_seq": struct.unpack("=Q", data[16:24])[0],
            "reserved": struct.unpack("=Q", data[24:32])[0],
        }
    finally:
        if shm is not None:
            shm.close()
        os.close(fd)


def read_shm_block(full_path: str, shm_id: int) -> dict:
    """读取并解析共享内存中第 shm_id 个 Data Block（32 字节）。"""
    offset = shm_id * 32
    fd = os.open(full_path, os.O_RDONLY)
    shm: Optional[mmap.mmap] = None
    try:
        shm = mmap.mmap(fd, offset + 32, mmap.MAP_SHARED, mmap.PROT_READ)
        shm.seek(offset)
        data = shm.read(32)
        return {
            "magic": struct.unpack("=I", data[0:4])[0],
            "state": data[4],
            "reserved": struct.unpack("=H", data[5:7])[0],
            "type": data[7],
            "write_seq": struct.unpack("=Q", data[8:16])[0],
            "timestamp": struct.unpack("=Q", data[16:24])[0],
            "value": struct.unpack("=Q", data[24:32])[0],
        }
    finally:
        if shm is not None:
            shm.close()
        os.close(fd)


def get_shm_size(full_path: str) -> int:
    """获取共享内存文件大小（字节）。"""
    return os.path.getsize(full_path)


def read_write_seq(full_path: str, shm_id: int) -> int:
    """读取第 shm_id 个 Data Block 的 write_seq。"""
    return read_shm_block(full_path, shm_id)["write_seq"]


# ──────────────────────────────────────────────
#  value 大端读取（c4_modbus_client 设计文档 §5.2）
# ──────────────────────────────────────────────

# 各数据类型在 8B value 字段中的有效字节数（ASFP2_TYPE_* 枚举值）。
# 仅覆盖 modbusd 2.3.0 已实现的 DAM 写入类型（见 c4_fun_00012 README §5.4）；
# INT64/UINT64/FLOAT64 未实现，属单元测试范围，此处不支持。
_VALUE_FORMATS = {
    0: ("B", 1),   # BOOLEAN
    3: ("h", 2),   # INT16
    4: ("H", 2),   # UINT16
    5: ("i", 4),   # INT32
    6: ("I", 4),   # UINT32
    10: ("f", 4),  # FLOAT32
    15: ("B", 1),  # BIT
}


def read_shm_value(full_path: str, shm_id: int, data_type: int):
    """按数据类型以本机序读取 Data Block 的 value 字段（低位字节）。

    对应 c4_fun_00012 README §2.4 的读取区间表：value 以本机序写入 8B value
    字段的低位字节，高位补零。返回解码后的 Python 数值。
    """
    if data_type not in _VALUE_FORMATS:
        raise ValueError(f"unsupported data_type for shm value read: {data_type}")

    fmt, size = _VALUE_FORMATS[data_type]
    offset = shm_id * 32 + 24
    fd = os.open(full_path, os.O_RDONLY)
    shm: Optional[mmap.mmap] = None
    try:
        shm = mmap.mmap(fd, offset + 8, mmap.MAP_SHARED, mmap.PROT_READ)
        shm.seek(offset)
        data = shm.read(8)
        return struct.unpack("=" + fmt, data[:size])[0]
    finally:
        if shm is not None:
            shm.close()
        os.close(fd)
