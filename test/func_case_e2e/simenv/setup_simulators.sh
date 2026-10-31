#!/usr/bin/env bash
# setup_simulators.sh — 用例 30~43 本机模拟环境（2026-10-31）
#
# 数据流：redis_tool 写 Redis（浮点值）→ modbusd/iec104d MGET 读 Redis →
# 对外提供 Modbus TCP(5020/5021) 与 IEC104(2404/2405/2406) 服务 →
# c4_modbus_client / c4_iec104_client 采集 → C4 接入链路。
# influxd(18086) 入库链路数据由 runner inject() 注入 shm，不依赖 redis。
#
# 端序定版（c4_asset_learning 会话源码推演 + case30 对点实证）：
#   modbusd  { swap: 1, hton_register: 1 }  ↔  agent 实例 { hton_register: 1(默认), swap: 2 }
#   （swap=1/hton=1 即 modbusd 官方示例端序；实测 18 点全对，2026-10-31）
#
# 用法: sudo bash setup_simulators.sh
set -euo pipefail

ENV=/tmp/c4_env
KICK=/tmp/c4_env_104rt
RT=/usr/local/bin/redis_tool

mkdir -p "$ENV" "$KICK" /tmp/c4_influxdb \
         "$ENV/mb5020/log" "$ENV/mb5021/log" "$ENV/iec2404/log" \
         "$ENV/iec2405/log" "$ENV/iec2406/log"

# ── modbusd #1：变桨控制器 127.0.0.1:5020（用例 30/42，10 点浮点）──
cat > "$ENV/modbusd.json" << 'EOF'
{
    "engine": { "pwd": "/tmp/c4_env/mb5020", "stop_check": 100 },
    "log": { "dir": "log", "file": "log.log", "level": 1, "debug_time": 300, "size": 128 },
    "modbus": { "ip": "127.0.0.1", "port": 5020, "hton_register": 1, "hton_total": 0, "swap": 1, "timer": 1000 },
    "redis": { "ip": "127.0.0.1", "port": 6379, "dbid": 0, "auth": "", "with_timestamp": 1, "precision": 6 },
    "points": [
        {"key": "c4e2e_mb30_3000", "modbusaddr": 3000, "funcode": 2, "type": 10},
        {"key": "c4e2e_mb30_3002", "modbusaddr": 3002, "funcode": 2, "type": 10},
        {"key": "c4e2e_mb30_3004", "modbusaddr": 3004, "funcode": 2, "type": 10},
        {"key": "c4e2e_mb30_3006", "modbusaddr": 3006, "funcode": 2, "type": 10},
        {"key": "c4e2e_mb30_3008", "modbusaddr": 3008, "funcode": 2, "type": 10},
        {"key": "c4e2e_mb30_3010", "modbusaddr": 3010, "funcode": 2, "type": 10},
        {"key": "c4e2e_mb30_3018", "modbusaddr": 3018, "funcode": 2, "type": 10},
        {"key": "c4e2e_mb30_3012", "modbusaddr": 3012, "funcode": 3, "type": 10},
        {"key": "c4e2e_mb30_3014", "modbusaddr": 3014, "funcode": 3, "type": 10},
        {"key": "c4e2e_mb30_3016", "modbusaddr": 3016, "funcode": 3, "type": 10}
    ]
}
EOF

# ── modbusd #2：油泵控制器 127.0.0.1:5021（用例 31，线圈 + 7 浮点）──
cat > "$ENV/modbusd_52.json" << 'EOF'
{
    "engine": { "pwd": "/tmp/c4_env/mb5021", "stop_check": 100 },
    "log": { "dir": "log", "file": "log.log", "level": 1, "debug_time": 300, "size": 128 },
    "modbus": { "ip": "127.0.0.1", "port": 5021, "hton_register": 1, "hton_total": 0, "swap": 1, "timer": 1000 },
    "redis": { "ip": "127.0.0.1", "port": 6379, "dbid": 0, "auth": "", "with_timestamp": 1, "precision": 6 },
    "points": [
        {"key": "c4e2e_mb31_4000", "modbusaddr": 4000, "funcode": 0, "type": 15},
        {"key": "c4e2e_mb31_4100", "modbusaddr": 4100, "funcode": 2, "type": 10},
        {"key": "c4e2e_mb31_4102", "modbusaddr": 4102, "funcode": 2, "type": 10},
        {"key": "c4e2e_mb31_4104", "modbusaddr": 4104, "funcode": 2, "type": 10},
        {"key": "c4e2e_mb31_4106", "modbusaddr": 4106, "funcode": 2, "type": 10},
        {"key": "c4e2e_mb31_4108", "modbusaddr": 4108, "funcode": 2, "type": 10},
        {"key": "c4e2e_mb31_4110", "modbusaddr": 4110, "funcode": 2, "type": 10},
        {"key": "c4e2e_mb31_4112", "modbusaddr": 4112, "funcode": 2, "type": 10}
    ]
}
EOF

# ── iec104d ×3：1号/2号主变 CA=1（2404/2405）+ 3号主变 CA=2（2406）──
make_iec() {  # $1=port $2=ca $3=dir $4=outfile $5=keyprefix
    cat > "$ENV/$4" << EOF
{
    "engine": { "pwd": "$ENV/$3", "stop_check": 100 },
    "log": { "dir": "log", "file": "log.log", "level": 1, "debug_time": 300, "size": 128 },
    "iec104": { "ip": "127.0.0.1", "port": $1, "k": 12, "w": 8, "t0": 30, "t1": 15, "t2": 10,
                "t3": 20, "modules": 32768, "common_address": $2, "with_cp56time2a": 0,
                "acquisition_of_events_timer": 1000, "cyclic_data_transmission_timer": 0, "timer": 1000 },
    "redis": { "ip": "127.0.0.1", "port": 6379, "dbid": 0, "auth": "", "with_timestamp": 1, "precision": 6 },
    "points": [
        {"key": "${5}_1",      "104addr": 1},
        {"key": "${5}_16385",  "104addr": 16385},
        {"key": "${5}_16386",  "104addr": 16386},
        {"key": "${5}_25601",  "104addr": 25601}
    ]
}
EOF
}
make_iec 2404 1 iec2404 iec104d.json       c4e2e_10434   # 用例 34：6 点（含 2:装置异常、16387:UAC）
python3 - << 'PYEOF'
import json
# 2404 需 6 点（case34 断言 IOA {1,2,16385,16386,16387,25601}）——补 2/16387 两点
p = '/tmp/c4_env/iec104d.json'
d = json.load(open(p))
pts = d['points']
pts.insert(1, {"key": "c4e2e_10434_2", "104addr": 2})
pts.insert(4, {"key": "c4e2e_10434_16387", "104addr": 16387})
json.dump(d, open(p, 'w'), ensure_ascii=False, indent=4)
print('2404 points:', [(x['104addr']) for x in pts])
PYEOF
make_iec 2405 1 iec2405 iec104d_52.json c4e2e_10435   # 用例 35：2号主变 4 点
make_iec 2406 2 iec2406 iec104d_102.json c4e2e_10437  # 用例 37：3号主变 CA=2 4 点

# ── influxd 1.8 配置（v1 /query 端点供 case38 对点；/api/v2/write 入库）──
cat > /tmp/c4_influxdb/influxdb.conf << 'EOF'
[meta]
  dir = "/tmp/c4_influxdb/meta"
[data]
  dir = "/tmp/c4_influxdb/data"
  wal-dir = "/tmp/c4_influxdb/wal"
[http]
  bind-address = "127.0.0.1:18086"
EOF

# ── case34 电度二段踢的键名文件（0 → 间隔2s → 1024）──
echo "c4e2e_10434_25601" > "$KICK/v1024A.txt"

# ── redis 喂数（redis_tool 写模式，带时间戳；值=用例期望值集）──
seed() { "$RT" -s 127.0.0.1 -p 6379 -d 0 -P "$1" -w -V "$2" -t 1 > /dev/null; }
# mb30（对点集 MB30_VALS）
seed c4e2e_mb30_3000 12.5;  seed c4e2e_mb30_3002 15.5;  seed c4e2e_mb30_3004 45.5
seed c4e2e_mb30_3006 25.5;  seed c4e2e_mb30_3008 220.5; seed c4e2e_mb30_3010 28.5
seed c4e2e_mb30_3012 30.5;  seed c4e2e_mb30_3014 30.0;  seed c4e2e_mb30_3016 35.0
seed c4e2e_mb30_3018 40.0
# mb31（对点集 MB31_VALS）
seed c4e2e_mb31_4000 1.0
seed c4e2e_mb31_4100 41.5;  seed c4e2e_mb31_4102 2.5;   seed c4e2e_mb31_4104 60.0
seed c4e2e_mb31_4106 3.2;   seed c4e2e_mb31_4108 55.5;  seed c4e2e_mb31_4110 0.8
seed c4e2e_mb31_4112 12.3
# 104（遥信 1.0；遥测/电度入对点集）
seed c4e2e_10434_1 1.0;     seed c4e2e_10434_2 1.0
seed c4e2e_10434_16385 220.5; seed c4e2e_10434_16386 50.2; seed c4e2e_10434_16387 220.5
seed c4e2e_10434_25601 1024.0
seed c4e2e_10435_1 1.0;     seed c4e2e_10435_16385 220.5; seed c4e2e_10435_16386 50.2
seed c4e2e_10435_25601 50.2
seed c4e2e_10437_1 1.0;     seed c4e2e_10437_16385 220.5; seed c4e2e_10437_16386 50.2
seed c4e2e_10437_25601 50.2

# ── 启动模拟器（root 常驻；重复启动前先清旧进程）──
pkill -f 'modbusd -c /tmp/c4_env' 2>/dev/null || true
pkill -f 'iec104d -c /tmp/c4_env' 2>/dev/null || true
sleep 1
export ACQUISITION=/var/acquisition
start() {  # $1=binary $2=config $3=log
    nohup "$1" -c "$2" > "$ENV/$3" 2>&1 &
}
start /usr/local/bin/modbusd "$ENV/modbusd.json"    mb5020.out
start /usr/local/bin/modbusd "$ENV/modbusd_52.json" mb5021.out
start /usr/local/bin/iec104d "$ENV/iec104d.json"       iec2404.out
start /usr/local/bin/iec104d "$ENV/iec104d_52.json"    iec2405.out
start /usr/local/bin/iec104d "$ENV/iec104d_102.json"   iec2406.out

sleep 2
# ── 自检：端口监听 + redis 回读 ──
for port in 5020 5021 2404 2405 2406; do
    ss -tln | grep -q ":$port " && echo "port $port OK" || echo "port $port MISSING"
done
redis_tool_read=$("$RT" -s 127.0.0.1 -p 6379 -d 0 -P c4e2e_mb30_3000 -t 1 2>/dev/null | head -1)
echo "redis readback c4e2e_mb30_3000 = $redis_tool_read"
influxd_running=$(pgrep -fc 'influxd -config' || true)
echo "influxd processes: ${influxd_running:-0}（case40 的 start_influxd 助手会按需拉起）"
echo "模拟器环境就绪"
