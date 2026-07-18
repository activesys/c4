"""
C4_FUN_00047 测试公共基础设施 — MCP 客户端 + fixtures + 双路验证 helpers。
共享内存操作函数见 shm_helpers.py。
"""

import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # type: ignore
from shm_helpers import shm_unlink


# ──────────────────────────────────────────────
#  MCP Stdio Client
# ──────────────────────────────────────────────


class McpClient:
    """通过 MCP stdio JSON-RPC 与 SUT 进程通信。"""

    def __init__(self, binary_path: str):
        self.process = subprocess.Popen(
            [binary_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self._stdin = self.process.stdin
        self._stdout = self.process.stdout
        self._next_id = 0
        self._closed = False
        self._initialize()

    def _send(self, msg: dict) -> None:
        line = json.dumps(msg, ensure_ascii=False)
        self._stdin.write(line + "\n")
        self._stdin.flush()

    def _recv(self) -> dict:
        line = self._stdout.readline()
        if not line:
            raise EOFError("SUT process exited unexpectedly")
        return json.loads(line)

    def _initialize(self) -> None:
        """MCP 握手: initialize → 读响应 → initialized 通知。"""
        self._next_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"roots": {"listChanged": True}},
                    "clientInfo": {"name": "c4_test", "version": "1.0.0"},
                },
            }
        )
        resp = self._recv()
        if "error" in resp:
            raise RuntimeError(f"Initialize failed: {resp['error']}")
        self._stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
            + "\n"
        )
        self._stdin.flush()

    def list_tools(self) -> dict:
        """发送 tools/list 请求并返回响应。"""
        self._next_id += 1
        req_id = self._next_id
        self._send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/list",
                "params": {},
            }
        )
        while True:
            msg = self._recv()
            if "id" in msg and msg["id"] == req_id:
                return msg
            if "method" in msg:
                pass

    def call_tool(
        self,
        tool_name: str,
        arguments: dict,
        on_request: Optional[Callable] = None,
    ) -> dict:
        """
        调用 MCP 工具。on_request 签名 (method, params, request_id) → dict | None。
        """
        self._next_id += 1
        req_id = self._next_id

        self._send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
        )

        while True:
            msg = self._recv()
            if "id" in msg and msg["id"] == req_id:
                return msg
            if "method" in msg and on_request is not None:
                method = msg["method"]
                params = msg.get("params", {})
                response = on_request(method, params, msg["id"])
                if response is not None:
                    self._send(response)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._stdin.close()
        except Exception:
            pass
        try:
            self._stdout.close()
        except Exception:
            pass
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self._closed = True


# ──────────────────────────────────────────────
#  二进制发现
# ──────────────────────────────────────────────


def _find_c4_asfp2_client_binary() -> str:
    """查找或编译 c4_asfp2_client 二进制。"""
    path = os.environ.get("C4_ASFP2_CLIENT_PATH")
    if path and os.path.isfile(path):
        return path

    test_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(test_dir, "../../mcp/c4_asfp2_client/c4_asfp2_client"),
        os.path.join(test_dir, "../../mcp/c4_asfp2_client/build/c4_asfp2_client"),
        os.path.join(test_dir, "../../build/mcp/c4_asfp2_client/c4_asfp2_client"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return os.path.abspath(p)

    src_dir = os.path.abspath(os.path.join(test_dir, "../../mcp/c4_asfp2_client"))
    if os.path.isdir(src_dir):
        result = subprocess.run(
            ["go", "build", "-o", "c4_asfp2_client", "."],
            cwd=src_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return os.path.join(src_dir, "c4_asfp2_client")
        else:
            pytest.skip(
                f"Failed to build c4_asfp2_client: {result.stderr.strip()}"
            )

    pytest.skip(
        "c4_asfp2_client binary not found. "
        "Set C4_ASFP2_CLIENT_PATH env var or build from c4/mcp/c4_asfp2_client"
    )
    return ""


def _find_c4_asfp2_server_binary() -> str:
    """查找或编译 c4_asfp2_server 二进制。"""
    path = os.environ.get("C4_ASFP2_SERVER_PATH")
    if path and os.path.isfile(path):
        return path

    test_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(test_dir, "../../mcp/c4_asfp2_server/c4_asfp2_server"),
        os.path.join(test_dir, "../../mcp/c4_asfp2_server/build/c4_asfp2_server"),
        os.path.join(test_dir, "../../build/mcp/c4_asfp2_server/c4_asfp2_server"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return os.path.abspath(p)

    src_dir = os.path.abspath(os.path.join(test_dir, "../../mcp/c4_asfp2_server"))
    if os.path.isdir(src_dir):
        result = subprocess.run(
            ["go", "build", "-o", "c4_asfp2_server", "."],
            cwd=src_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return os.path.join(src_dir, "c4_asfp2_server")
        else:
            pytest.skip(
                f"Failed to build c4_asfp2_server: {result.stderr.strip()}"
            )

    pytest.skip(
        "c4_asfp2_server binary not found. "
        "Set C4_ASFP2_SERVER_PATH env var or build from c4/mcp/c4_asfp2_server"
    )
    return ""


def _find_shm_manager_binary() -> str:
    """查找或编译 c4_shm_manager 二进制。"""
    path = os.environ.get("C4_SHM_MANAGER_PATH")
    if path and os.path.isfile(path):
        return path

    test_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(test_dir, "../../mcp/c4_shm_manager/c4_shm_manager"),
        os.path.join(test_dir, "../../mcp/c4_shm_manager/build/c4_shm_manager"),
        os.path.join(test_dir, "../../build/mcp/c4_shm_manager/c4_shm_manager"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return os.path.abspath(p)

    src_dir = os.path.abspath(os.path.join(test_dir, "../../mcp/c4_shm_manager"))
    if os.path.isdir(src_dir):
        result = subprocess.run(
            ["go", "build", "-o", "c4_shm_manager", "."],
            cwd=src_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return os.path.join(src_dir, "c4_shm_manager")
        else:
            pytest.skip(
                f"Failed to build c4_shm_manager: {result.stderr.strip()}"
            )

    pytest.skip(
        "c4_shm_manager binary not found. "
        "Set C4_SHM_MANAGER_PATH env var or build from c4/mcp/c4_shm_manager"
    )
    return ""


# ──────────────────────────────────────────────
#  roots/list 回调工厂
# ──────────────────────────────────────────────


def _roots_callback(roots_list):
    """创建 on_request 回调：对 roots/list 返回 roots_list。"""

    def cb(method, params, request_id):
        if method == "roots/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"roots": roots_list},
            }
        return None

    return cb


# ──────────────────────────────────────────────
#  配置工厂
# ──────────────────────────────────────────────


def _make_standard_config():
    """标准配置：2 points (addr 1000,1001), inject_port=9700, verify_port=9800, smart=1, timer=100。"""
    return {
        "c4_shm_manager": {
            "writer": ["c4_asfp2_server"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_asfp2_server": [
            {
                "name": "注入接收端",
                "id": "rx_inject",
                "port": 9700,
                "t1": 0,
                "t2": 0,
                "forward_kack": 255,
                "inverse_keep": 0,
                "points": [
                    {"id": "p1", "addr": 1000, "shm_id": 0},
                    {"id": "p2", "addr": 1001, "shm_id": 0},
                ],
            }
        ],
        "c4_asfp2_client": [
            {
                "name": "SUT 发送端",
                "ip": "127.0.0.1",
                "port": 9800,
                "t0": 30,
                "t1": 0,
                "t2": 0,
                "smart": 1,
                "forward_kack": 255,
                "inverse_keep": 0,
                "timer": 100,
                "points": [
                    {"key": "rx_inject.p1", "addr": 1000, "shm_id": 0},
                    {"key": "rx_inject.p2", "addr": 1001, "shm_id": 0},
                ],
            }
        ],
    }


def _make_5points_config():
    """5 points 配置：addr 1000~1004，其余同标准配置。"""
    server_points = []
    client_points = []
    for i in range(5):
        addr = 1000 + i
        pid = f"p{i + 1}"
        server_points.append({"id": pid, "addr": addr, "shm_id": 0})
        client_points.append({"key": f"rx_inject.{pid}", "addr": addr, "shm_id": 0})

    return {
        "c4_shm_manager": {
            "writer": ["c4_asfp2_server"],
            "reader": ["c4_asfp2_client"],
            "max_points": 20,
        },
        "c4_asfp2_server": [
            {
                "name": "注入接收端",
                "id": "rx_inject",
                "port": 9700,
                "t1": 0,
                "t2": 0,
                "forward_kack": 255,
                "inverse_keep": 0,
                "points": server_points,
            }
        ],
        "c4_asfp2_client": [
            {
                "name": "SUT 发送端",
                "ip": "127.0.0.1",
                "port": 9800,
                "t0": 30,
                "t1": 0,
                "t2": 0,
                "smart": 1,
                "forward_kack": 255,
                "inverse_keep": 0,
                "timer": 100,
                "points": client_points,
            }
        ],
    }


def _make_gapped_config():
    """不连续 key 配置：4 points (addr 1000,1001,1005,1006)。"""
    addrs = [1000, 1001, 1005, 1006]
    server_points = []
    client_points = []
    for i, addr in enumerate(addrs):
        pid = f"p{i + 1}"
        server_points.append({"id": pid, "addr": addr, "shm_id": 0})
        client_points.append({"key": f"rx_inject.{pid}", "addr": addr, "shm_id": 0})

    return {
        "c4_shm_manager": {
            "writer": ["c4_asfp2_server"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_asfp2_server": [
            {
                "name": "注入接收端",
                "id": "rx_inject",
                "port": 9700,
                "t1": 0,
                "t2": 0,
                "forward_kack": 255,
                "inverse_keep": 0,
                "points": server_points,
            }
        ],
        "c4_asfp2_client": [
            {
                "name": "SUT 发送端",
                "ip": "127.0.0.1",
                "port": 9800,
                "t0": 30,
                "t1": 0,
                "t2": 0,
                "smart": 1,
                "forward_kack": 255,
                "inverse_keep": 0,
                "timer": 100,
                "points": client_points,
            }
        ],
    }


def _make_smart0_config():
    """smart=0 配置：2 points，smart=0，其余同标准配置。"""
    config = _make_standard_config()
    config["c4_asfp2_client"][0]["smart"] = 0
    return config


def _make_3points_config():
    """3 points 配置：addr 1000~1002（TC8 非数值过滤测试）。"""
    server_points = []
    client_points = []
    for i in range(3):
        addr = 1000 + i
        pid = f"p{i + 1}"
        server_points.append({"id": pid, "addr": addr, "shm_id": 0})
        client_points.append({"key": f"rx_inject.{pid}", "addr": addr, "shm_id": 0})

    return {
        "c4_shm_manager": {
            "writer": ["c4_asfp2_server"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_asfp2_server": [
            {
                "name": "注入接收端",
                "id": "rx_inject",
                "port": 9700,
                "t1": 0,
                "t2": 0,
                "forward_kack": 255,
                "inverse_keep": 0,
                "points": server_points,
            }
        ],
        "c4_asfp2_client": [
            {
                "name": "SUT 发送端",
                "ip": "127.0.0.1",
                "port": 9800,
                "t0": 30,
                "t1": 0,
                "t2": 0,
                "smart": 1,
                "forward_kack": 255,
                "inverse_keep": 0,
                "timer": 100,
                "points": client_points,
            }
        ],
    }


# ──────────────────────────────────────────────
#  MCP 进程启动 helpers（非 pytest fixture）
# ──────────────────────────────────────────────


def start_asfp2_server(config_path):
    """启动 c4_asfp2_server MCP 进程，调用 start 工具，返回 McpClient。"""
    binary = _find_c4_asfp2_server_binary()
    client = McpClient(binary)
    resp = client.call_tool(
        "start",
        {},
        on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
    )
    if resp["result"].get("isError", False):
        client.close()
        raise RuntimeError(
            f"c4_asfp2_server start failed: {resp['result']['content'][0]['text']}"
        )
    return client


def start_asfp2_client(config_path):
    """启动 c4_asfp2_client (SUT) MCP 进程，返回 McpClient（不调用 start）。"""
    binary = _find_c4_asfp2_client_binary()
    return McpClient(binary)


def start_sut(client, config_path):
    """在 SUT client 上调用 start 工具。"""
    resp = client.call_tool(
        "start",
        {},
        on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
    )
    if resp["result"].get("isError", False):
        raise RuntimeError(
            f"c4_asfp2_client start failed: {resp['result']['content'][0]['text']}"
        )
    return client


# ──────────────────────────────────────────────
#  采集侧二进制 helpers
# ──────────────────────────────────────────────


def run_asfp2_server(port, t1=0, t2=0, timeout=None):
    """启动采集 asfp2_server 验证端，stdout 重定向到临时文件。

    返回 (subprocess.Popen, stdout_path, stdout_file_handle)。
    调用方在终止进程后需关闭 stdout_file_handle。
    """
    fd, stdout_path = tempfile.mkstemp(
        suffix=".txt", prefix="asfp2_server_rx_"
    )
    os.close(fd)
    stdout_file = open(stdout_path, "w")
    cmd = [
        "/usr/local/bin/asfp2_server",
        "--t1", str(t1),
        "--t2", str(t2),
        "-p", str(port),
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=stdout_file,
        stderr=subprocess.STDOUT,
    )
    return proc, stdout_path, stdout_file


def run_asfp2_client_inject(
    port,
    begin,
    end,
    data_type,
    ts_start=None,
    val_begin=None,
    val_end=None,
    extra_args=None,
):
    """运行采集 asfp2_client CLI 注入数据到端口 port。

    参数：
        port:      目标端口
        begin:     起始地址
        end:       结束地址
        data_type: 数据类型 (0=BOOLEAN, 4=UINT16, 10=FLOAT32, 12=STRING)
        ts_start:  起始时间戳
        val_begin: 值范围起始 (-B)
        val_end:   值范围结束 (-E)
        extra_args:额外的命令行参数列表 (如 ["-z", "5", "-P", "8"])
    """
    cmd = [
        "/usr/local/bin/asfp2_client",
        "-s", "127.0.0.1",
        "-p", str(port),
        "-b", str(begin),
        "-e", str(end),
        "-t", "1",
        "--type", str(data_type),
    ]
    if ts_start is not None:
        cmd.extend(["--ts-start", str(ts_start), "--ts-step", "1"])
    if val_begin is not None:
        cmd.extend(["-B", str(val_begin)])
    if val_end is not None:
        cmd.extend(["-E", str(val_end)])
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


# ──────────────────────────────────────────────
#  tcpdump helpers
# ──────────────────────────────────────────────


def start_tcpdump(port, pcap_path):
    """启动 tcpdump 抓包指定端口，写入 pcap 文件。返回 subprocess.Popen。"""
    cmd = (
        f"script -c 'echo tm1122 | sudo -S /usr/bin/tcpdump -i lo -c 100 -w {pcap_path} port {port}' /dev/null"
    )
    proc = subprocess.Popen(
        ["bash", "-c", cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2.0)
    return proc


def stop_tcpdump(proc):
    """停止 tcpdump 进程（由 timeout 自动终止，这里仅 wait）。"""
    if proc is None:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# ──────────────────────────────────────────────
#  pcap 解析
# ──────────────────────────────────────────────


# pcap 全局头 (24 bytes)
PCAP_GLOBAL_HEADER_FMT = "<IHHiIII"
PCAP_GLOBAL_HEADER_SIZE = 24

# pcap 包记录头 (16 bytes)
PCAP_PKT_HEADER_FMT = "<IIII"
PCAP_PKT_HEADER_SIZE = 16

# ASFP2 header magic
ASFP2_MAGIC = b"ASFPV211"
ASFP2_HEADER_SIZE = 16


def parse_asfp2_packets(pcap_path, port):
    """解析 pcap 文件，提取所有 ASFP2 数据包。

    扫描每个 TCP segment payload 中的 ASFPV211 magic，
    解析 Header 字段 (Flag, Length, Count, Attribute)，返回包列表。

    返回 list[dict]:
        [{"flag": "ASFPV211", "length": N, "count": N, "attr": 0xNN}, ...]
    """
    if not os.path.isfile(pcap_path):
        return []

    with open(pcap_path, "rb") as f:
        data = f.read()

    if len(data) < PCAP_GLOBAL_HEADER_SIZE:
        return []

    # 解析 pcap 全局头
    global_header = struct.unpack(PCAP_GLOBAL_HEADER_FMT, data[:PCAP_GLOBAL_HEADER_SIZE])
    magic = global_header[0]

    # 根据 magic number 确定字节序
    if magic == 0xA1B2C3D4:
        byte_order = "<"
    elif magic == 0xD4C3B2A1:
        byte_order = ">"
    else:
        # 尝试小端
        byte_order = "<"

    packets = []
    offset = PCAP_GLOBAL_HEADER_SIZE

    while offset + PCAP_PKT_HEADER_SIZE <= len(data):
        pkt_header = struct.unpack(
            byte_order + "IIII", data[offset:offset + PCAP_PKT_HEADER_SIZE]
        )
        incl_len = pkt_header[2]
        offset += PCAP_PKT_HEADER_SIZE

        if offset + incl_len > len(data):
            break

        payload = data[offset:offset + incl_len]
        offset += incl_len

        # 在 payload 中搜索 ASFP2 magic
        search_start = 0
        while True:
            idx = payload.find(ASFP2_MAGIC, search_start)
            if idx < 0:
                break

            # 确认有足够字节解析完整 header
            if idx + ASFP2_HEADER_SIZE > len(payload):
                search_start = idx + 1
                continue

            hdr_data = payload[idx:idx + ASFP2_HEADER_SIZE]

            # 解析 ASFP2 header fields
            flag = hdr_data[0:8].decode("ascii", errors="replace")
            raw_len_attr = struct.unpack(">I", hdr_data[8:12])[0]
            asfp2_length = raw_len_attr & 0xFFFF
            attr_high = (raw_len_attr >> 16) & 0xFFFF
            count = struct.unpack(">H", hdr_data[12:14])[0]
            attr_low = struct.unpack(">H", hdr_data[14:16])[0]
            attr_full = (attr_high << 16) | attr_low

            # Length 合理性检查
            remaining = len(payload) - idx
            if asfp2_length <= remaining:
                packets.append({
                    "flag": flag,
                    "length": asfp2_length,
                    "count": count,
                    "attr": attr_full,
                })
            # 继续搜索同一 payload 中可能的后续包
            search_start = idx + 1

    return packets


# ──────────────────────────────────────────────
#  asfp2_server 输出解析
# ──────────────────────────────────────────────


# 匹配接收到的数据行：<ASFP211< key: xxx, timestamp: xxx, data: xxx
_ASFP2_SERVER_OUTPUT_RE = re.compile(
    r"<ASFP211<\s*key:\s*(\d+),\s*timestamp:\s*(\d+),\s*data:\s*(\d+)"
)


def parse_asfp2_server_output(txt_path):
    """解析 asfp2_server stdout 文件，提取所有接收到的数据记录。

    返回 list[dict]:
        [{"key": 1000, "timestamp": 1768848814000, "value": 100}, ...]
    """
    if not os.path.isfile(txt_path):
        return []

    records = []
    with open(txt_path, "r") as f:
        for line in f:
            m = _ASFP2_SERVER_OUTPUT_RE.search(line)
            if m:
                records.append({
                    "key": int(m.group(1)),
                    "timestamp": int(m.group(2)),
                    "value": int(m.group(3)),
                })
    return records


# ──────────────────────────────────────────────
#  Fixtures
# ──────────────────────────────────────────────


@pytest.fixture
def shm_mgr_client():
    """Function 级 fixture — 启动 c4_shm_manager，MCP initialize，yield 客户端，关闭。"""
    binary = _find_shm_manager_binary()
    client = McpClient(binary)
    yield client
    client.close()


@pytest.fixture
def prepare_environment(shm_mgr_client):
    """
    Function 级 fixture — 准备配置文件 + 共享内存。
    返回工厂函数 (config_dict, instance_id) → (config_path, instance_id)。
    内部完成 create_shm + adjust_shm，并在返回前关闭 shm_manager。
    """
    temp_files: list[str] = []

    def _prepare(config_dict: dict, instance_id: str):
        # 步骤 1: 写入配置文件
        fd, config_path = tempfile.mkstemp(suffix=".json", prefix="c4_config_")
        temp_files.append(config_path)
        with os.fdopen(fd, "w") as f:
            json.dump(config_dict, f)

        # 步骤 2: create_shm
        resp = shm_mgr_client.call_tool(
            "create_shm",
            {"instance_id": instance_id},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        if resp["result"].get("isError", False):
            raise RuntimeError(
                f"create_shm failed: {resp['result']['content'][0]['text']}"
            )

        # 步骤 3: adjust_shm
        resp = shm_mgr_client.call_tool(
            "adjust_shm",
            {},
            on_request=_roots_callback([{"uri": f"file://{config_path}"}]),
        )
        if resp["result"].get("isError", False):
            raise RuntimeError(
                f"adjust_shm failed: {resp['result']['content'][0]['text']}"
            )

        # 步骤 4: 关闭 shm_manager
        shm_mgr_client.close()

        return config_path, instance_id

    yield _prepare

    # Teardown: 清理临时配置文件
    for path in temp_files:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.fixture
def isolated_shm():
    """Function 级隔离 fixture。setup 预防性清理，teardown 释放。"""
    registered: list[str] = []

    def register(instance_id: str) -> None:
        registered.append(instance_id)
        try:
            shm_unlink(f"/c4_{instance_id}")
        except OSError:
            pass

    yield register

    for iid in registered:
        try:
            shm_unlink(f"/c4_{iid}")
        except OSError:
            pass


# ──────────────────────────────────────────────
#  端口检测 helper
# ──────────────────────────────────────────────


def _port_is_listening(port, timeout=1.0):
    """检查本地端口是否已被监听。连接成功返回 True。"""
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        s.close()
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False
