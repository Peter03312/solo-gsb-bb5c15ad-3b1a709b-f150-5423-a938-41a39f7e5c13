#!/usr/bin/env bash
# 权利人对外付款暂停（收款资料有争议）端到端调用示例。
# 前置：服务已启动（docker compose up -d，或本地 uvicorn）。
# 用法: BASE=http://localhost:8000 bash examples/payment_suspensions_demo.sh
set -u
BASE="${BASE:-http://localhost:8000}"
C="curl -sS"

say() { printf '\n=== %s ===\n' "$1"; }

say "健康检查"
$C "$BASE/health"; echo

say "1) 登记作品/协议（RS-SUS 100%）、播放 PLAY-SUS-1（100 分）并出具单据"
$C -X POST "$BASE/works" -H 'Content-Type: application/json' \
  -d '{"code":"W-SUS-001","title":"暂停演示录音"}'; echo
$C -X POST "$BASE/agreements" -H 'Content-Type: application/json' -d '{
  "work_code":"W-SUS-001",
  "valid_from":"2026-01-01T00:00:00+08:00",
  "valid_to":"2027-01-01T00:00:00+08:00",
  "shares":[{"holder_code":"RS-SUS","percent":"100"}]
}'; echo
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-SUS-1","work_code":"W-SUS-001",
       "occurred_at":"2026-02-10T20:00:00+08:00","amount":100}'; echo
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}' \
  | python3 -m json.tool

say "2) 先预留一笔 60 分（待确认，暂停前创建；暂停后将保留额度、仍可取消）"
$C -X POST "$BASE/payment-reservations" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-SUS","instruction_ref":"CMD-SUS-1","amount":60}' \
  | python3 -m json.tool

say "3) 凭权利人编号 + 全局唯一暂停号 + 原因登记暂停（201，返回状态与操作记录）"
$C -X POST "$BASE/payment-suspensions" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-SUS","suspension_ref":"SUSP-20261003-0001",
       "reason":"收款账户资料待银行核验，暂停对外付款"}' | python3 -m json.tool

say "4) 同号同内容重试 => 200 status=duplicate，返回原暂停、状态不变"
$C -i -X POST "$BASE/payment-suspensions" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-SUS","suspension_ref":"SUSP-20261003-0001",
       "reason":"收款账户资料待银行核验，暂停对外付款"}' | grep -E '^HTTP'

say "5) 拒绝：同号异原因/异权利人 409 suspension_ref_conflict；换号重复登记 409 payment_suspension_already_active"
$C -i -X POST "$BASE/payment-suspensions" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-SUS","suspension_ref":"SUSP-20261003-0001","reason":"别的原因"}' \
  | grep -E '^HTTP|suspension_ref_conflict'
$C -i -X POST "$BASE/payment-suspensions" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-SUS","suspension_ref":"SUSP-OTHER-0002","reason":"又一争议"}' \
  | grep -E '^HTTP|payment_suspension_already_active'
$C -i -X POST "$BASE/payment-suspensions" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-NOBODY","suspension_ref":"SUSP-X","reason":"未知权利人"}' \
  | grep -E '^HTTP|holder_not_found'

say "6) 暂停期间：直接付款 409、新建预留 409（均 holder_payments_suspended，不留痕）"
$C -i -X POST "$BASE/payments" -H 'Content-Type:application/json' \
  -d '{"holder_code":"RS-SUS","external_ref":"BANK-SUS-DIRECT","amount":10}' \
  | grep -E '^HTTP|holder_payments_suspended'
$C -i -X POST "$BASE/payment-reservations" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-SUS","instruction_ref":"CMD-SUS-NEW","amount":10}' \
  | grep -E '^HTTP|holder_payments_suspended'

say "7) 暂停期间：确认既有待确认预留同样 409（指令保持 pending、不产生付款）"
$C -i -X POST "$BASE/payment-reservations/CMD-SUS-1/confirm" \
  -H 'Content-Type: application/json' -d '{"external_ref":"BANK-SUS-CONF"}' \
  | grep -E '^HTTP|holder_payments_suspended'
$C "$BASE/payment-reservations/CMD-SUS-1" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("instruction:", d["instruction_status"], "reserved:", d["reserved_total"], "available:", d["available_balance"])'

say "8) 既有待确认预保留额并可取消（取消不涉及对外付款，暂停期间照常）"
$C -X POST "$BASE/payment-reservations/CMD-SUS-1/cancel" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["instruction_status"], "reserved:", d["reserved_total"], "available:", d["available_balance"])'

say "9) 出单/试算/退票/余额核对继续按原口径运行（对账单 payment_suspension 显示活跃暂停）"
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-SUS-1-R1","work_code":"W-SUS-001",
       "occurred_at":"2026-02-10T20:00:00+08:00","amount":80,
       "corrects_id":"PLAY-SUS-1"}' >/dev/null
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("supplement issued:", d.get("batch_id"), [(l["holder_code"], l["amount"]) for l in d.get("payable", [])])'
$C "$BASE/rightsholders/RS-SUS/ledger" | python3 -m json.tool

say "10) 凭暂停号解除（200 released；重复解除幂等 200 duplicate；未知号 404）"
$C -X POST "$BASE/payment-suspensions/SUSP-20261003-0001/release" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["suspension_status"], [op["operation"] for op in d["operations"]])'
$C -X POST "$BASE/payment-suspensions/SUSP-20261003-0001/release" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
$C -i -X POST "$BASE/payment-suspensions/UNKNOWN-SUSP/release" | grep -E '^HTTP'

say "11) 解除不自动支付：按当时余额（80）与预留规则办理——预留 30 后确认，确认后余额 50"
$C -X POST "$BASE/payment-reservations" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-SUS","instruction_ref":"CMD-SUS-2","amount":30}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], "balance:", d["balance"], "available:", d["available_balance"])'
$C -X POST "$BASE/payment-reservations/CMD-SUS-2/confirm" \
  -H 'Content-Type: application/json' -d '{"external_ref":"BANK-SUS-CONF2"}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], "paid:", d["amount"], "balance_after:", d["balance_after"])'

say "12) 解除后可用新暂停号重新暂停；旧号同内容重试仍返回已解除结果（不复活）"
$C -X POST "$BASE/payment-suspensions" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-SUS","suspension_ref":"SUSP-20261003-0003","reason":"新争议"}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["suspension_status"])'
$C -X POST "$BASE/payment-suspensions" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-SUS","suspension_ref":"SUSP-20261003-0001",
       "reason":"收款账户资料待银行核验，暂停对外付款"}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["suspension_status"])'

say "清理：解除演示暂停"
$C -X POST "$BASE/payment-suspensions/SUSP-20261003-0003/release" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["suspension_status"])'
say "完成"
