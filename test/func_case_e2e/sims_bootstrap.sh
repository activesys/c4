#!/bin/bash
# e2e 模拟器环境一键重建（2026-10-20 建：机器重启 /tmp 清空后手工重建易错，
# 依 模拟器使用.md §1~§4 固化）。可重复执行（幂等：目录/配置重写，进程已跑则跳过）。
# 用法：bash sims_bootstrap.sh [--start]   # 不带 --start 只写配置与值，不拉进程
set -e
START=0
[ "${1:-}" = "--start" ] && START=1
export ACQUISITION=/var/acquisition

# ── modbusd .51（5020，用例30/42：10 点保持/输入寄存器混排）────────
mkdir -p /tmp/c4_env
cat > /tmp/c4_env/modbusd.json <<'EOF'
{
    "engine": {"pwd": "/tmp/c4_env", "stop_check": 100},
    "log": {"dir": "log", "file": "log.log", "level": 1, "debug_time": 300, "size": 128},
    "modbus": {"ip": "0.0.0.0", "port": 5020, "hton_register": 1, "hton_total": 0,
               "swap": 2, "timer": 1000},
    "redis": {"ip": "127.0.0.1", "port": 6379, "dbid": 0, "auth": "",
              "with_timestamp": 0, "precision": 6},
    "points": [
        {"key": "SIM_MB_3000", "modbusaddr": 3000, "funcode": 2, "type": 10},
        {"key": "SIM_MB_3002", "modbusaddr": 3002, "funcode": 2, "type": 10},
        {"key": "SIM_MB_3004", "modbusaddr": 3004, "funcode": 2, "type": 10},
        {"key": "SIM_MB_3006", "modbusaddr": 3006, "funcode": 2, "type": 10},
        {"key": "SIM_MB_3008", "modbusaddr": 3008, "funcode": 2, "type": 10},
        {"key": "SIM_MB_3010", "modbusaddr": 3010, "funcode": 2, "type": 10},
        {"key": "SIM_MB_3012", "modbusaddr": 3012, "funcode": 3, "type": 10},
        {"key": "SIM_MB_3014", "modbusaddr": 3014, "funcode": 3, "type": 10},
        {"key": "SIM_MB_3016", "modbusaddr": 3016, "funcode": 3, "type": 10},
        {"key": "SIM_MB_3018", "modbusaddr": 3018, "funcode": 2, "type": 10}
    ]
}
EOF

# ── modbusd .52（5021，用例31：4000 线圈 + 4100~4112 保持寄存器）────
mkdir -p /tmp/c4_env_52
cat > /tmp/c4_env_52/modbusd_52.json <<'EOF'
{
    "engine": {"pwd": "/tmp/c4_env_52", "stop_check": 100},
    "log": {"dir": "log", "file": "log.log", "level": 1, "debug_time": 300, "size": 128},
    "modbus": {"ip": "0.0.0.0", "port": 5021, "hton_register": 1, "hton_total": 0,
               "swap": 2, "timer": 1000},
    "redis": {"ip": "127.0.0.1", "port": 6379, "dbid": 0, "auth": "",
              "with_timestamp": 0, "precision": 6},
    "points": [
        {"key": "SIM_MB2_4000", "modbusaddr": 4000, "funcode": 0, "type": 15},
        {"key": "SIM_MB2_4100", "modbusaddr": 4100, "funcode": 2, "type": 10},
        {"key": "SIM_MB2_4102", "modbusaddr": 4102, "funcode": 2, "type": 10},
        {"key": "SIM_MB2_4104", "modbusaddr": 4104, "funcode": 2, "type": 10},
        {"key": "SIM_MB2_4106", "modbusaddr": 4106, "funcode": 2, "type": 10},
        {"key": "SIM_MB2_4108", "modbusaddr": 4108, "funcode": 2, "type": 10},
        {"key": "SIM_MB2_4110", "modbusaddr": 4110, "funcode": 2, "type": 10},
        {"key": "SIM_MB2_4112", "modbusaddr": 4112, "funcode": 2, "type": 10}
    ]
}
EOF

# ── iec104d A/B/C（2404 CA=1 / 2405 CA=1 / 2406 CA=2，六点同构）─────
mk_104() {
    local dir="$1" port="$2" ca="$3" tag="$4"
    mkdir -p "$dir"
    cat > "$dir/iec104d_${port}.json" <<EOF
{
    "engine": {"pwd": "$dir", "stop_check": 100},
    "log": {"dir": "log", "file": "log.log", "level": 1, "debug_time": 300, "size": 128},
    "iec104": {"ip": "0.0.0.0", "port": $port, "k": 12, "w": 8, "t0": 30, "t1": 15,
               "t2": 10, "t3": 20, "modules": 32768, "common_address": $ca,
               "with_cp56time2a": 0, "acquisition_of_events_timer": 1000,
               "cyclic_data_transmission_timer": 0, "timer": 1000},
    "redis": {"ip": "127.0.0.1", "port": 6379, "dbid": 0, "auth": "",
              "with_timestamp": 1, "precision": 6},
    "points": [
        {"key": "SIM_${tag}_16385", "104addr": 16385},
        {"key": "SIM_${tag}_16386", "104addr": 16386},
        {"key": "SIM_${tag}_16387", "104addr": 16387},
        {"key": "SIM_${tag}_1", "104addr": 1},
        {"key": "SIM_${tag}_2", "104addr": 2},
        {"key": "SIM_${tag}_25601", "104addr": 25601}
    ]
}
EOF
}
mk_104 /tmp/c4_env_104A 2404 1 104A
mk_104 /tmp/c4_env_104B 2405 1 104B
mk_104 /tmp/c4_env_104C 2406 2 104C

# ── redis_tool 值文件（iec104d with_timestamp=1，须 redis_tool -w 写）──
mkdir -p /tmp/c4_env_104rt
cat > /tmp/c4_env_104rt/v220.txt <<'EOF'
SIM_104A_16385
SIM_104A_16387
SIM_104B_16385
SIM_104C_16385
EOF
cat > /tmp/c4_env_104rt/v50.txt <<'EOF'
SIM_104A_16386
SIM_104B_16386
SIM_104C_16386
EOF
cat > /tmp/c4_env_104rt/v1.txt <<'EOF'
SIM_104A_1
SIM_104B_1
SIM_104C_1
EOF
cat > /tmp/c4_env_104rt/v0.txt <<'EOF'
SIM_104A_2
EOF
cat > /tmp/c4_env_104rt/v1024.txt <<'EOF'
SIM_104A_25601
SIM_104B_25601
SIM_104C_25601
EOF
cat > /tmp/c4_env_104rt/v1024A.txt <<'EOF'
SIM_104A_25601
EOF

# ── influxd（run_cases 用例38~40 的 /tmp/c4_influxdb 实例；influx 套件的
#    /tmp/influxdb_test 由其 Influxd.up 自举，无需此处管）──────────────
mkdir -p /tmp/c4_influxdb
cat > /tmp/c4_influxdb/influxdb.conf <<'EOF'
reporting-disabled = true
[meta]
  dir = "/tmp/c4_influxdb/meta"
[data]
  dir = "/tmp/c4_influxdb/data"
  wal-dir = "/tmp/c4_influxdb/wal"
[http]
  enabled = true
  bind-address = "127.0.0.1:18086"
EOF

echo "配置与值文件就绪。"
if [ "$START" = "1" ]; then
    # modbusd 值口径（with_timestamp=0 → redis-cli 文本直写；手册 §1 值表）
    redis-cli -p 6379 MSET SIM_MB_3000 12.5 SIM_MB_3002 15.5 SIM_MB_3004 45.5 \
        SIM_MB_3006 25.5 SIM_MB_3008 220.5 SIM_MB_3010 28.5 SIM_MB_3012 30.5 \
        SIM_MB_3014 30.0 SIM_MB_3016 35.0 SIM_MB_3018 40.0 \
        SIM_MB2_4000 1 SIM_MB2_4100 41.5 SIM_MB2_4102 2.5 SIM_MB2_4104 60.0 \
        SIM_MB2_4106 3.2 SIM_MB2_4108 55.5 SIM_MB2_4110 0.8 SIM_MB2_4112 12.3 > /dev/null
    # iec104d 初值（启动前写 = 初始值，无突发是正常行为；电度变化事件由
    # case34 建链后补写触发——模拟器手册 §2/§3）
    RT=/usr/local/bin/redis_tool
    $RT -s 127.0.0.1 -p 6379 -d 0 -F /tmp/c4_env_104rt/v220.txt  -t 1 -V 220.5 -w
    $RT -s 127.0.0.1 -p 6379 -d 0 -F /tmp/c4_env_104rt/v50.txt   -t 1 -V 50.2  -w
    $RT -s 127.0.0.1 -p 6379 -d 0 -F /tmp/c4_env_104rt/v1.txt    -t 1 -V 1     -w
    $RT -s 127.0.0.1 -p 6379 -d 0 -F /tmp/c4_env_104rt/v0.txt    -t 1 -V 0     -w
    $RT -s 127.0.0.1 -p 6379 -d 0 -F /tmp/c4_env_104rt/v1024.txt -t 1 -V 1024  -w
    # 拉进程（log/ 目录必须预建——log_init 不自建目录，缺目录 = 静默退出 rc=255，
    # 2026-10-20 实测；重启禁令：用 pkill -x 精确进程名——手册 §2 警告）
    mkdir -p /tmp/c4_env/log /tmp/c4_env_52/log \
             /tmp/c4_env_104A/log /tmp/c4_env_104B/log /tmp/c4_env_104C/log
    pgrep -x modbusd > /dev/null || {
        nohup /usr/local/bin/modbusd -c /tmp/c4_env/modbusd.json     > /tmp/c4_env/modbusd.out 2>&1 &
        nohup /usr/local/bin/modbusd -c /tmp/c4_env_52/modbusd_52.json > /tmp/c4_env_52/modbusd.out 2>&1 &
    }
    pgrep -x iec104d > /dev/null || {
        nohup /usr/local/bin/iec104d -c /tmp/c4_env_104A/iec104d_2404.json > /tmp/c4_env_104A/iec104d.out 2>&1 &
        nohup /usr/local/bin/iec104d -c /tmp/c4_env_104B/iec104d_2405.json > /tmp/c4_env_104B/iec104d.out 2>&1 &
        nohup /usr/local/bin/iec104d -c /tmp/c4_env_104C/iec104d_2406.json > /tmp/c4_env_104C/iec104d.out 2>&1 &
    }
    pgrep -x influxd > /dev/null || {
        nohup /home/wangbo/backup/influxdb/influxdb-1.8.10-1/usr/bin/influxd \
            -config /tmp/c4_influxdb/influxdb.conf > /tmp/c4_influxdb/influxd.out 2>&1 &
    }
    sleep 3
    echo "== 端口核验 =="
    ss -ltn | grep -E "5020|5021|2404|2405|2406|18086" || echo "（有端口未监听，查各 .out 日志）"
fi
