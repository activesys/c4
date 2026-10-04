#!/usr/bin/env python3
# 用例 15 A 级脚本（func_test_case.md §用例 15；c4_architecture.md §2.2.3 FLOAT16 特例）。
# 协议级回归，不经对话驱动（附录 B A 级——纯脚本，可进 CI）：
#   create_shm → start(c4_asfp2_server) → asfp2_client 注入 type=9 value=f16 0x3E00(=1.5)
#   → mmap 直读 /dev/shm/<inst> 块1：state=1、type=9、value 低 4 字节=0x3FC00000
#   （float32 位模式，按 float32 解释=1.5——修复前只拷贝线缆尺寸 2 字节导致归零）
#   → c4_shm_manager read_points 交叉验证（下游按 float32 位模式还原=1.5）
import json
import mmap
import os
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_cases as rc  # noqa: E402

INSTANCE = "c4_case15"      # ^c4_[a-zA-Z0-9]+$；同时是 POSIX shm 名（/dev/shm/c4_case15）
PORT = 19077                # e2e 19xxx 池内未占用端口
CFG = "/tmp/c4_case15_config.json"
ADDR = 3000
SHM_ID = 1
F16_WIRE = 0x3E00           # half(1.5) 线缆位型 = 15936
F32_EXPECT = 0x3FC00000     # float32(1.5) 位模式

# shm 块布局（c4/mcp/internal/shm/shm.go）：BlockSize=32、BlkOffState=4、
# BlkOffType=7、BlkOffWriteSeq=8、BlkOffTimestamp=16、BlkOffValue=24（本机序，
# 低位有效高位补零——FLOAT16 特例：低 4 字节为 float32 位模式）
BLOCK = 32
OFF_STATE, OFF_TYPE, OFF_SEQ, OFF_VALUE = 4, 7, 8, 24


def call(svc, tool, args):
    text, is_err = rc.SockClient(svc, timeout=10.0).call_tool_text(tool, args)
    if is_err:
        raise rc.Fail(f"{svc}.{tool} 失败: {text[:300]}")
    return text


def main():
    rc.log("════ 用例 15 FLOAT16 shm 写入协议级回归（A 级）开始 ════")
    t0 = time.time()
    rc.MCP_STACK.up()

    with open(CFG, "w", encoding="utf-8") as f:
        json.dump({
            "c4_asfp2_server": [{
                "id": "channel1", "name": "f16测试", "port": PORT,
                "points": [{"addr": ADDR, "id": "f16_point", "shm_id": SHM_ID}],
            }],
            "c4_shm_manager": {"writer": ["c4_asfp2_server"], "reader": []},
        }, f, ensure_ascii=False)

    # 幂等起手：清残留实例（shm 段复用——create_shm 幂等附加）
    try:
        call("c4_asfp2_server", "stop", {})
    except rc.Fail:
        pass

    call("c4_shm_manager", "create_shm", {"instance_id": INSTANCE})
    call("c4_asfp2_server", "start", {"instance_id": INSTANCE, "config_path": CFG})
    rc.wait_port(PORT, True)

    # 注入：ASFPV211 旗标（--protocol 8）、type=9(FLOAT16)、addr 3000、value=f16 0x3E00
    #（长选项形态——已装 asfp2_client 的短选项串不含 L/I/P；ke 为开区间端点 count=ke-kb，
    #  单点注入须 ke=kb+1；数据值起点是 --db（--i0 是发送间隔），db 需小于 de 否则回绕清零）
    r = subprocess.run(
        [rc.ASFP2_CLIENT, "--server", "127.0.0.1", "--port", str(PORT),
         "--kb", str(ADDR), "--ke", str(ADDR + 1),
         "--protocol", "8", "--type", "9",
         "--db", str(F16_WIRE), "--de", str(F16_WIRE + 1), "--times", "3"],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise rc.Fail(f"asfp2_client 注入失败: {r.stderr[:200]}")

    fd = os.open(f"/dev/shm/{INSTANCE}", os.O_RDWR)
    try:
        mm = mmap.mmap(fd, 0)
        deadline = time.time() + 10
        state = 0
        while time.time() < deadline:
            state = mm[SHM_ID * BLOCK + OFF_STATE]
            if state == 1:
                break
            time.sleep(0.2)
        blk = SHM_ID * BLOCK
        if state != 1:
            raise rc.Fail(f"15: 注入后 10s 块{SHM_ID} state 仍为 {state}（未写入）")
        btype = mm[blk + OFF_TYPE]
        seq = int.from_bytes(mm[blk + OFF_SEQ:blk + OFF_SEQ + 8], sys.byteorder)
        value = int.from_bytes(mm[blk + OFF_VALUE:blk + OFF_VALUE + 4], sys.byteorder)
        f32 = struct.unpack("<f" if sys.byteorder == "little" else ">f",
                            mm[blk + OFF_VALUE:blk + OFF_VALUE + 4])[0]
        rc.log(f"  直读 shm: state={state} type={btype} write_seq={seq} "
               f"value=0x{value:08X} (float32={f32})")
        if btype != 9:
            raise rc.Fail(f"15: 块 type={btype} ≠ 9(FLOAT16)")
        # 修复前：只拷贝线缆尺寸 2 字节 → 0x3FC00000 高 16 位被丢弃 → 0x00000000（归零）
        if value != F32_EXPECT:
            raise rc.Fail(f"15: value=0x{value:08X} ≠ 0x{F32_EXPECT:08X}"
                          f"（FLOAT16 未按 float32 位模式落盘）")
        if abs(f32 - 1.5) > 1e-6:
            raise rc.Fail(f"15: float32 解释 = {f32} ≠ 1.5")
        if seq % 2 != 0:
            raise rc.Fail(f"15: write_seq={seq} 为奇数（seqlock 悬在写中）")
    finally:
        os.close(fd)

    # 交叉验证（下游还原面）：c4_shm_manager read_points 按 FLOAT16 特例解 float32
    c = rc.SockClient("c4_shm_manager", timeout=10.0)
    entries = c.read_points([SHM_ID])
    c.close()
    reads = entries.get("reads") if isinstance(entries, dict) else entries
    e0 = (reads or [{}])[0]
    if e0.get("status") != "ok":
        raise rc.Fail(f"15: read_points 未读到块{SHM_ID}: {str(entries)[:300]}")
    if e0.get("data_type") != 9:
        raise rc.Fail(f"15: read_points data_type={e0.get('data_type')} ≠ 9: {e0}")
    if abs(float(e0.get("value", 0)) - 1.5) > 1e-6:
        raise rc.Fail(f"15: read_points 还原值 {e0.get('value')} ≠ 1.5: {e0}")
    if e0.get("value_raw") != str(F32_EXPECT):
        raise rc.Fail(f"15: value_raw={e0.get('value_raw')} ≠ {F32_EXPECT}"
                      f"（位模式非 float32 1.5）")
    rc.log(f"  read_points 交叉验证: data_type=9 value=1.5 raw={e0.get('value_raw')} ✓")

    call("c4_asfp2_server", "stop", {})
    rc.log(f"════ 用例 15 PASS（{time.time()-t0:.0f}s）════")


if __name__ == "__main__":
    try:
        main()
    except rc.Fail as e:
        rc.log(f"════ 用例 15 FAIL: {e} ════")
        sys.exit(1)
