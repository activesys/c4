"""POSIX 共享内存操作辅助函数，供测试代码和 conftest 共用。

复用 c4_fun_00012 的读 helper（shm_unlink / shm_path / read_shm_block / read_write_seq /
read_shm_value 等），并新增写入 helper（write_shm_block，模拟 Writer）。
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
            "reserved2": struct.unpack("=Q", data[24:32])[0],
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
#  value 读取（本机序，c4_fun_00012 README §2.4）
# ──────────────────────────────────────────────

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
    """按数据类型以本机序读取 Data Block 的 value 字段（低位字节）。"""
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


# ──────────────────────────────────────────────
#  value 写入（模拟 Writer，本机序低位字节）
# ──────────────────────────────────────────────

_WRITE_FORMATS = {
    0: ("B", 1),   # BOOLEAN
    5: ("i", 4),   # INT32
    10: ("f", 4),  # FLOAT32
}


def _encode_value(data_type: int, value) -> bytes:
    """把 value 按采集类型编码为 8 字节（本机序低位字节，高位补零）。"""
    buf = bytearray(8)
    if data_type == 0:  # BOOLEAN
        struct.pack_into("=B", buf, 0, 1 if value else 0)
    elif data_type == 5:  # INT32
        struct.pack_into("=i", buf, 0, value)
    elif data_type == 10:  # FLOAT32
        struct.pack_into("=f", buf, 0, value)
    elif data_type == 12:  # STRING（非数值，跳过测试用，value 字节无关紧要）
        pass
    else:
        raise ValueError(f"unsupported data_type for shm value write: {data_type}")
    return bytes(buf)


def write_shm_block(shm_path: str, shm_id: int, data_type: int, value, timestamp: int) -> int:
    """模拟 Writer 写入一个 Data Block（32 字节，本机序），返回新 write_seq。

    - magic @0：保持不变（由 shm_manager create_shm 写入 0xC4DA7A00）
    - state @4：写入置 1（激活）
    - type  @7：data_type（ASFP2 枚举）
    - write_seq @8：取当前值 +2（偶数，稳定值），写入
    - timestamp @16：本机序
    - value @24：本机序低位字节，高位补零
    """
    offset = shm_id * 32
    fd = os.open(shm_path, os.O_RDWR)
    shm: Optional[mmap.mmap] = None
    try:
        shm = mmap.mmap(fd, offset + 32, mmap.MAP_SHARED,
                        mmap.PROT_READ | mmap.PROT_WRITE)

        # 读取当前 write_seq（偶数稳定值）
        shm.seek(offset + 8)
        cur_seq = struct.unpack("=Q", shm.read(8))[0]
        new_seq = cur_seq + 2  # 偶数递增

        # 写 type @7
        shm.seek(offset + 7)
        shm.write(bytes([data_type]))
        # 写 value @24
        shm.seek(offset + 24)
        shm.write(_encode_value(data_type, value))
        # 写 timestamp @16
        shm.seek(offset + 16)
        shm.write(struct.pack("=Q", timestamp))
        # 写 state @4（激活）
        shm.seek(offset + 4)
        shm.write(bytes([1]))
        # 写 write_seq @8（偶数稳定值，最后写作为提交点）
        shm.seek(offset + 8)
        shm.write(struct.pack("=Q", new_seq))
        shm.flush()

        return new_seq
    finally:
        if shm is not None:
            shm.close()
        os.close(fd)
