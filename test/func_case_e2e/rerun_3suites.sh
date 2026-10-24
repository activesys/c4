#!/bin/bash
# 续跑 3 套件（交接 §4.3）：extra（s45 驱动修复已就位）/ rp_mb（MB-06 抖动确认）/
# user_cases（被 zcode 退出中断，整体重跑）——其余 18 套件第 4 批已 rc=0 无需重跑。
cd /home/wangbo/work/activesys/c4/test/func_case_e2e || exit 1
DIR=/home/wangbo/work/activesys/c4/test/func_case_e2e/reg-20261024-dual
export C4_E2E_PROVIDER=deepseek C4_E2E_MODEL=deepseek-flash
export DEEPSEEK_API_KEY="$(grep '^DEEPSEEK_API_KEY=' /usr/local/etc/c4/agent.env | head -1 | cut -d= -f2-)"
META=$DIR/rerun-meta.log
echo "[$(date '+%F %T')] RERUN START (extra/rp_mb/user_cases, 修复含 s45 新会话)" >> "$META"

echo "[$(date '+%F %T')] SIMS BOOTSTRAP" >> "$META"
bash sims_bootstrap.sh --start > "$DIR/rerun-sims.stdout.log" 2>&1
echo "[$(date '+%F %T')] SIMS rc=$?" >> "$META"

run() {
  local name="$1"; local tmo="$2"; shift 2
  local rc1
  echo "[$(date '+%F %T')] SUITE BEGIN $name" >> "$META"
  timeout --signal=INT --kill-after=60 "$tmo" python3 "$@" > "$DIR/rerun-$name.stdout.log" 2>&1
  rc1=$?
  echo "[$(date '+%F %T')] SUITE END $name rc=$rc1" >> "$META"
}

run extra       5400  run_chain_extra.py
run rp_mb       2400  run_chain_real_points_mb.py
run user_cases  14400 run_chain_user_cases.py

echo "[$(date '+%F %T')] RERUN ALL DONE" >> "$META"
PASS_N=$(grep -c 'rc=0$' "$META" || true)
FAIL_N=$(grep -cE 'rc=[1-9][0-9]*$' "$META" || true)
echo "SUMMARY: PASS=$PASS_N FAIL=$FAIL_N" >> "$META"
