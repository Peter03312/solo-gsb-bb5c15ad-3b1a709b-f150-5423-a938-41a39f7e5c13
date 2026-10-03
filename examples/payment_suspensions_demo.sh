#!/usr/bin/env bash
# 权利人对外付款暂停（收款资料争议暂停）端到端调用示例。
# 前置：服务已启动（docker compose up -d，或本地 uvicorn）。
# 用法: BASE=http://localhost:8000 bash examples/payment_suspensions_demo.sh
set -u
BASE="${BASE:-http://localhost:8000}"
C="curl -sS"

say() { printf '\n=== %s ===\n' "$1"; }

say "健康检查"
$C "$BASE/health"; echo

say "1) 建档/协议/播放/出单：RS-S 余额 100 分（独立作品 W-SUSP-001）"
$C -X POST "$BASE/works" -H 'Content-Type: application/json' \
  -d '{"code":"W-SUSP-001","title":"暂停演示录音"}' >/dev/null
$C -X POST "$BASE/agreements" -H 'Content-Type: application/json' -d '{
  "work_code":"W-SUSP-001",
  "valid_from":"2026-01-01T00:00:00+08:00",
  "valid_to":"2027-01-01T00:00:00+08:00",
  "shares":[{"holder_code":"RS-S","percent":"100"}]
}' >/dev/null
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-S-1","work_code":"W-SUSP-001",
       "occurred_at":"2026-02-10T20:00:00+08:00","amount":100}' >/dev/null
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}' \
  | python3 -m json.tool

say "2) 暂停前先预留 60 分（待确认，用于演示暂停期间保留额度/可取消/确认被拒）"
$C -X POST "$BASE/payment-reservations" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-S","instruction_ref":"CMD-S-1","amount":60}' \
  | python3 -m json.tool

say "3) 财务发现收款资料有争议：凭权利人 + 唯一暂停号 SUSP-S-1 + 原因登记（201）"
$C -X POST "$BASE/payment-suspensions" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-S","suspension_ref":"SUSP-S-1",
       "reason":"收款账户资料待第三方核账"}' | python3 -m json.tool

say "4) 同号同内容重试 => 200 duplicate（状态不变，不新增操作记录）"
$C -i -X POST "$BASE/payment-suspensions" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-S","suspension_ref":"SUSP-S-1",
       "reason":"收款账户资料待第三方核账"}' | grep -E '^HTTP'

say "5) 异内容复用 => 409 suspension_ref_conflict；同一权利人换新号重复登记 => 409"
$C -i -X POST "$BASE/payment-suspensions" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-S","suspension_ref":"SUSP-S-1","reason":"另一个原因"}' \
  | grep -E '^HTTP'
$C -i -X POST "$BASE/payment-suspensions" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-S","suspension_ref":"SUSP-S-2","reason":"重复登记"}' \
  | grep -E '^HTTP'
say "   未知权利人 => 404 holder_not_found"
$C -i -X POST "$BASE/payment-suspensions" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-GHOST","suspension_ref":"SUSP-X","reason":"x"}' \
  | grep -E '^HTTP'

say "6) 暂停期间：直接付款 409（不留付款）；新建预留 409（不留指令）"
$C -i -X POST "$BASE/payments" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-S","external_ref":"BANK-S-BLOCKED","amount":10}' \
  | grep -E '^HTTP'
$C -i -X POST "$BASE/payment-reservations" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-S","instruction_ref":"CMD-S-BLOCKED","amount":10}' \
  | grep -E '^HTTP'

say "7) 既有待确认预留：确认 409（指令保持 pending、不产生付款），但取消允许（释放额度）"
$C -i -X POST "$BASE/payment-reservations/CMD-S-1/confirm" \
  -H 'Content-Type: application/json' -d '{"external_ref":"BANK-S-CMD-1"}' \
  | grep -E '^HTTP'
$C -X POST "$BASE/payment-reservations/CMD-S-1/cancel" | python3 -m json.tool

say "8) 出单/试算与余额核对照常；对账单带 active_payment_suspension"
$C "$BASE/statements/preview?start=2026-02-01T00:00:00%2B08:00&end=2026-03-01T00:00:00%2B08:00" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("payable:", d["payable"])'
$C "$BASE/rightsholders/RS-S/ledger" | python3 -m json.tool

say "9) 已完成付款的同内容重试在暂停期间仍幂等：先解除 => 付款 => 重新暂停 => 同号重试"
$C -X POST "$BASE/payment-suspensions/SUSP-S-1/release" >/dev/null
$C -X POST "$BASE/payments" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-S","external_ref":"BANK-S-1","amount":30}' >/dev/null
$C -X POST "$BASE/payment-suspensions" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-S","suspension_ref":"SUSP-S-3","reason":"暂停期间验证幂等"}' >/dev/null
$C -i -X POST "$BASE/payments" -H 'Content-Type:application/json' \
  -d '{"holder_code":"RS-S","external_ref":"BANK-S-1","amount":30}' \
  | grep -E '^HTTP'

say "10) 解除 SUSP-S-3：released；再次解除 => duplicate；未知暂停号 => 404"
$C -X POST "$BASE/payment-suspensions/SUSP-S-3/release" | python3 -m json.tool
$C -i -X POST "$BASE/payment-suspensions/SUSP-S-3/release" | grep -E '^HTTP'
$C -i -X POST "$BASE/payment-suspensions/UNKNOWN/release" | grep -E '^HTTP'

say "11) 解除后不自动支付，按当时余额显式发起（余 70，可付 70；超额仍 409）"
$C -i -X POST "$BASE/payments" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-S","external_ref":"BANK-S-2","amount":70}' | grep -E '^HTTP'

say "12) 查询暂停（状态与操作记录）"
$C "$BASE/payment-suspensions/SUSP-S-1" | python3 -m json.tool
