#!/bin/bash
# 全量回归 v3（双 Agent 2.1.88 首发回归，2026-10-24）——
# 阶段一：双 Agent 链（run_chain_dual_agent.py）作为门控，失败则不启动全回归；
# 阶段二：v2 全部 21 套件原样（顺序与超时照抄 run_full_regression_v2.sh）。
# 会话内后台执行即可（2026-10-24 用户指令更新：不再要求脱离 zcode），
# 进度续查：tail -f reg-20261024-dual/meta.log
cd /home/wangbo/work/activesys/c4/test/func_case_e2e || exit 1
DIR=/home/wangbo/work/activesys/c4/test/func_case_e2e/reg-20261024-dual
mkdir -p "$DIR"
export C4_E2E_PROVIDER=deepseek C4_E2E_MODEL=deepseek-flash
export DEEPSEEK_API_KEY="$(grep '^DEEPSEEK_API_KEY=' /usr/local/etc/c4/agent.env | head -1 | cut -d= -f2-)"
META=$DIR/meta.log
echo "[$(date '+%F %T')] V3 START (deepseek-flash, 2.1.88 dual-agent first, 失败继续下一套件)" >> "$META"

run() {
  local name="$1"; local tmo="$2"; shift 2
  local rc1
  echo "[$(date '+%F %T')] SUITE BEGIN $name" >> "$META"
  timeout --signal=INT --kill-after=60 "$tmo" python3 "$@" > "$DIR/$name.stdout.log" 2>&1
  rc1=$?
  echo "[$(date '+%F %T')] SUITE END $name rc=$rc1" >> "$META"
}

# 模拟器编队（modbusd/iec104d/influxd/redis——阶段二各协议套件需要；asfp2 二进制按需自起）
echo "[$(date '+%F %T')] SIMS BOOTSTRAP" >> "$META"
bash sims_bootstrap.sh --start > "$DIR/sims.stdout.log" 2>&1
echo "[$(date '+%F %T')] SIMS rc=$?" >> "$META"

# ── 阶段一：双 Agent 门控（失败即止——用户指令：双 Agent 通过后才跑全回归）──
echo "[$(date '+%F %T')] GATE: dual_agent BEGIN" >> "$META"
timeout --signal=INT --kill-after=60 5400 python3 run_chain_dual_agent.py > "$DIR/dual_agent.stdout.log" 2>&1
GATE_RC=$?
echo "[$(date '+%F %T')] GATE: dual_agent END rc=$GATE_RC" >> "$META"
if [ "$GATE_RC" -ne 0 ]; then
  echo "[$(date '+%F %T')] GATE FAILED — 全回归未启动（修复双 Agent 后重跑）" >> "$META"
  echo "SUMMARY: GATE_FAILED dual_agent rc=$GATE_RC" >> "$META"
  exit 1
fi
echo "[$(date '+%F %T')] GATE PASSED — 进入全回归" >> "$META"

# ── 阶段二：全量（v2 清单原样）──
run case15      600   run_case15_float16.py
run case46sc    600   run_case46_selfcheck.py
run reset       600   run_cases.py reset
run cases_all   2400  run_cases.py all
run cases_new   1800  run_cases.py new
run upload      600   run_chain_upload.py
run extra       5400  run_chain_extra.py
run ind1        2700  run_chain_ind1.py
run v21         1800  run_chain_v21.py
run b1          1200  run_chain_b1.py
run b342        1800  run_chain_b342.py
run group       2400  run_chain_group.py
run multi       1800  run_chain_multi.py
run site_change 2400  run_chain_site_change.py
run influx      1800  run_chain_influx.py
run mb104       1800  run_chain_mb104.py
run rp_if       1800  run_chain_real_points_if.py
run rp          2400  run_chain_real_points.py
run rp_mb       2400  run_chain_real_points_mb.py
run user_cases  14400 run_chain_user_cases.py

echo "[$(date '+%F %T')] ALL SUITES DONE" >> "$META"
PASS_N=$(grep -c 'rc=0$' "$META" || true)
FAIL_N=$(grep -cE 'rc=[1-9][0-9]*$' "$META" || true)
echo "SUMMARY: PASS=$PASS_N FAIL=$FAIL_N (GATE dual_agent rc=$GATE_RC)" >> "$META"
