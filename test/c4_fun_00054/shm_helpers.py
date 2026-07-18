"""POSIX 共享内存操作辅助函数，供测试代码和 conftest 共用。"""

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
    return f"/dev/shm/c4_{instance_id}"


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
