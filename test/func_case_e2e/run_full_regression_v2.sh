#!/bin/bash
# 全量干净回归 v2（v2.1.87 修复后连续验证）——
# 相对 reg-20261019 初版修正：① rc 捕获（$(date) 命令替换吃掉 $?，恒 0）；
# ② user_cases 超时 5400→14400（UC 三用例实测 6097s，90 分钟上限会硬切）。
cd /home/wangbo/work/activesys/c4/test/func_case_e2e || exit 1
DIR=/home/wangbo/work/activesys/c4/test/func_case_e2e/reg-20261020-full
mkdir -p "$DIR"
export C4_E2E_PROVIDER=deepseek C4_E2E_MODEL=deepseek-flash
export DEEPSEEK_API_KEY="$(grep '^DEEPSEEK_API_KEY=' /usr/local/etc/c4/agent.env | head -1 | cut -d= -f2-)"
META=$DIR/meta.log
echo "[$(date '+%F %T')] FULL REGRESSION START (deepseek-flash, v2.1.87, 失败继续下一套件)" >> "$META"
run() {
  local name="$1"; local tmo="$2"; shift 2
  local rc1
  echo "[$(date '+%F %T')] SUITE BEGIN $name" >> "$META"
  timeout --signal=INT --kill-after=60 "$tmo" python3 "$@" > "$DIR/$name.stdout.log" 2>&1
  rc1=$?
  echo "[$(date '+%F %T')] SUITE END $name rc=$rc1" >> "$META"
}
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
