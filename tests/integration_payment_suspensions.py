"""权利人对外付款暂停（收款资料争议暂停）端到端集成测试
（pgserver 便携 PostgreSQL，无需 Docker）。

运行: PYTHONPATH=. python3 tests/integration_payment_suspensions.py
"""
import os
import pathlib
import tempfile
import threading

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_susp_pg_")) / "pgdata"
server = pgserver.get_server(_pgdata, cleanup_mode="delete")
try:
    os.environ["DATABASE_URL"] = server.get_uri()
    from fastapi.testclient import TestClient
    from app.main import app

    failures = []

    def check(name, cond, extra=""):
        print(("PASS" if cond else "FAIL"), name, extra)
        if not cond:
            failures.append(name)

    def agree(c, work, frm, to, shares):
        r = c.post("/agreements", json={
            "work_code": work, "valid_from": frm, "valid_to": to,
            "shares": [{"holder_code": h, "percent": p} for h, p in shares]})
        assert r.status_code == 201, r.text

    def play(c, pid, work, amount, when="2026-02-10T20:00:00+08:00"):
        r = c.post("/plays/import", json={
            "import_id": pid, "work_code": work,
            "occurred_at": when, "amount": amount})
        assert r.status_code == 201, r.text

    def open_susp(c, holder, ref, reason, code=201):
        r = c.post("/payment-suspensions", json={
            "holder_code": holder, "suspension_ref": ref, "reason": reason})
        assert r.status_code == code, r.text
        return r

    def pay(c, holder, ref, amount, code=201):
        r = c.post("/payments", json={
            "holder_code": holder, "external_ref": ref, "amount": amount})
        assert r.status_code == code, r.text
        return r

    def reserve(c, holder, ref, amount, code=201):
        r = c.post("/payment-reservations", json={
            "holder_code": holder, "instruction_ref": ref, "amount": amount})
        assert r.status_code == code, r.text
        return r

    FEB = {"start": "2026-02-01T00:00:00+08:00",
           "end": "2026-03-01T00:00:00+08:00"}

    with TestClient(app) as c:
        check("health", c.get("/health").json()["status"] == "ok")

        # S1/S2 各 100 分应付；S3 建档但不出单（余额 0）
        c.post("/works", json={"code": "W1", "title": "作品一"})
        c.post("/works", json={"code": "W2", "title": "作品二"})
        c.post("/works", json={"code": "W3", "title": "作品三"})
        agree(c, "W1", "2026-01-01T00:00:00+08:00", "2027-01-01T00:00+08:00",
              [("S1", "100")])
        agree(c, "W2", "2026-01-01T00:00:00+08:00", "2027-01-01T00:00+08:00",
              [("S2", "100")])
        agree(c, "W3", "2026-01-01T00:00:00+08:00", "2027-01-01T00:00+08:00",
              [("S3", "100")])
        play(c, "P1", "W1", 100)
        play(c, "P2", "W2", 100)
        r = c.post("/statements/issue", json=FEB)
        check("issue", r.status_code == 200, r.text)
        c.post("/rightsholders", json={"code": "S4", "name": "无余额无单据"})

        # ---------- 1) 登记暂停 ----------
        r = open_susp(c, "S1", "SUSP-1", "收款账户资料待核实")
        b = r.json()
        check("open 201", r.status_code == 201 and b["status"] == "created")
        check("open fields",
              b["suspension_status"] == "active" and b["holder_code"] == "S1"
              and b["reason"] == "收款账户资料待核实" and b["released_at"] is None)
        check("open operation",
              [(o["operation"], o["reason"]) for o in b["operations"]]
              == [("activated", "收款账户资料待核实")], str(b["operations"]))

        # ---------- 2) 同号同内容重试幂等（200 duplicate，不加操作记录）----------
        r = open_susp(c, "S1", "SUSP-1", "收款账户资料待核实", code=200)
        b2 = r.json()
        check("retry duplicate",
              b2["status"] == "duplicate" and b2["suspension_status"] == "active"
              and b2["activated_at"] == b["activated_at"])
        check("retry no new op", len(b2["operations"]) == 1)

        # ---------- 3) 同号异内容复用：异权利人/异原因均 409 ----------
        r = c.post("/payment-suspensions", json={
            "holder_code": "S2", "suspension_ref": "SUSP-1",
            "reason": "收款账户资料待核实"})
        check("diff holder 409", r.status_code == 409
              and r.json()["error"] == "suspension_ref_conflict", r.text)
        r = c.post("/payment-suspensions", json={
            "holder_code": "S1", "suspension_ref": "SUSP-1", "reason": "别的原因"})
        check("diff reason 409", r.status_code == 409
              and r.json()["error"] == "suspension_ref_conflict", r.text)

        # ---------- 4) 未知权利人 404；字段校验 422 ----------
        r = c.post("/payment-suspensions", json={
            "holder_code": "GHOST", "suspension_ref": "SUSP-X", "reason": "x"})
        check("unknown holder 404", r.status_code == 404
              and r.json()["error"] == "holder_not_found", r.text)
        r = c.post("/payment-suspensions", json={
            "holder_code": "S1", "suspension_ref": "", "reason": "x"})
        check("empty ref 422", r.status_code == 422)
        r = c.post("/payment-suspensions", json={
            "holder_code": "S1", "suspension_ref": "SUSP-Z", "reason": ""})
        check("empty reason 422", r.status_code == 422)

        # ---------- 5) 同一权利人未解除重复登记（换新号）409，不留痕 ----------
        r = open_susp(c, "S1", "SUSP-2", "再次暂停", code=409)
        check("already active 409",
              r.json()["error"] == "payment_suspension_already_active", r.text)
        check("rejected absent",
              c.get("/payment-suspensions/SUSP-2").status_code == 404)
        check("state unchanged",
              c.get("/payment-suspensions/SUSP-1").json()["suspension_status"]
              == "active")

        # ---------- 6) 暂停期间：直接付款拒绝，不产生付款 ----------
        r = pay(c, "S1", "BANK-1", 10, code=409)
        check("direct pay rejected",
              r.json()["error"] == "rightsholder_payments_suspended"
              and r.json()["detail"]["suspension_ref"] == "SUSP-1", r.text)
        led = c.get("/rightsholders/S1/ledger").json()
        check("no payment written", led["payments"] == [])
        check("ledger active suspension",
              led["active_payment_suspension"] is not None
              and led["active_payment_suspension"]["suspension_ref"] == "SUSP-1")

        # ---------- 7) 暂停期间：创建预留拒绝（不留指令），余额 0 的 S3 亦可暂停 ----------
        r = reserve(c, "S1", "CMD-X", 10, code=409)
        check("create reservation rejected",
              r.json()["error"] == "rightsholder_payments_suspended", r.text)
        check("no instruction written",
              len(c.get("/rightsholders/S1/ledger").json()["instructions"]) == 0)
        r = open_susp(c, "S3", "SUSP-3", "零余额权利人也可暂停")
        check("zero-balance suspension 201", r.status_code == 201)
        check("zero-balance ledger suspension",
              c.get("/rightsholders/S3/ledger").json()
              ["active_payment_suspension"]["suspension_ref"] == "SUSP-3")

        # ---------- 8) 出单、试算、余额核对口径不受暂停影响 ----------
        r = c.get("/statements/preview", params=FEB)
        got = {x["holder_code"]: x["amount"] for x in r.json()["payable"]}
        check("preview unchanged", got == {"S1": 100, "S2": 100}, str(got))
        # 无变化再出单：不产生新单
        before_n = len(c.get("/statements", params=FEB).json())
        c.post("/statements/issue", json=FEB)
        check("issue unchanged count",
              len(c.get("/statements", params=FEB).json()) == before_n)

        # ---------- 9) 暂停前的待确认预留：保留额度、可取消、确认被拒 ----------
        # S2 先预留 60（余额 100，可用额 40），再暂停
        reserve(c, "S2", "CMD-1", 60)
        open_susp(c, "S2", "SUSP-4", "预留后暂停")
        led = c.get("/rightsholders/S2/ledger").json()
        check("pending reservation kept",
              led["reserved_total"] == 60 and led["available_balance"] == 40
              and led["instructions"][0]["status"] == "pending")
        # 直接付款即使在可用额内也拒绝（暂停优先于余额规则）
        pay(c, "S2", "BANK-2", 40, code=409)
        # 确认既有预留被拒：指令保持 pending、不产生付款
        r = c.post("/payment-reservations/CMD-1/confirm",
                   json={"external_ref": "BANK-CMD-1"})
        check("confirm rejected", r.status_code == 409
              and r.json()["error"] == "rightsholder_payments_suspended", r.text)
        led = c.get("/rightsholders/S2/ledger").json()
        check("confirm no payment no change",
              led["payments"] == []
              and led["instructions"][0]["status"] == "pending")
        # 取消待确认预留：允许，额度释放
        r = c.post("/payment-reservations/CMD-1/cancel")
        check("cancel allowed under suspension",
              r.status_code == 200 and r.json()["instruction_status"] == "cancelled",
              r.text)
        led = c.get("/rightsholders/S2/ledger").json()
        check("cancel released quota",
              led["reserved_total"] == 0 and led["available_balance"] == 100)
        # 重复取消幂等（原约定不变）
        r = c.post("/payment-reservations/CMD-1/cancel")
        check("cancel idempotent",
              r.status_code == 200 and r.json()["status"] == "duplicate")
        # 取消后再创建预留仍被拒（暂停未解除）
        reserve(c, "S2", "CMD-1B", 10, code=409)
        check("reserve still rejected after cancel",
              c.get("/rightsholders/S2/ledger").json()["instructions"][-1]
              ["instruction_ref"] == "CMD-1")

        # ---------- 10) 退票在暂停期间照常（先解除 S2、付款、再暂停、退票）----------
        c.post("/payment-suspensions/SUSP-4/release")
        pay(c, "S2", "BANK-R", 10)
        open_susp(c, "S2", "SUSP-5", "付款后退票期间暂停")
        r = c.post("/refunds", json={
            "payment_ref": "BANK-R", "refund_ref": "REF-1", "amount": 4})
        check("refund allowed during suspension",
              r.status_code == 201 and r.json()["balance_after"] == 94, r.text)
        # 退票同号同内容重试仍幂等
        r = c.post("/refunds", json={
            "payment_ref": "BANK-R", "refund_ref": "REF-1", "amount": 4})
        check("refund retry idempotent",
              r.status_code == 200 and r.json()["status"] == "duplicate")
        c.post("/payment-suspensions/SUSP-5/release")

        # ---------- 11) 已完成付款的同内容重试在暂停期间仍幂等返回（S1）----------
        # S1 此前付款均被拒；改用：先解除 -> 付款 BANK-3 -> 重新暂停 -> 同号重试
        c.post("/payment-suspensions/SUSP-1/release")
        pay(c, "S1", "BANK-3", 30)
        open_susp(c, "S1", "SUSP-6", "暂停后验证幂等")
        r = pay(c, "S1", "BANK-3", 30, code=200)
        check("payment idempotent under suspension",
              r.json()["status"] == "duplicate" and r.json()["payment_id"]
              and len(c.get("/rightsholders/S1/ledger").json()["payments"]) == 1,
              r.text)
        # 新流水号付款仍拒绝
        pay(c, "S1", "BANK-4", 5, code=409)

        # ---------- 12) 已确认预留的同流水号确认重试在暂停期间幂等 ----------
        c.post("/payment-suspensions/SUSP-6/release")
        reserve(c, "S1", "CMD-2", 20)
        r = c.post("/payment-reservations/CMD-2/confirm",
                   json={"external_ref": "BANK-CMD-2"})
        check("confirm before suspension", r.status_code == 201, r.text)
        payments_before = len(c.get("/rightsholders/S1/ledger").json()["payments"])
        open_susp(c, "S1", "SUSP-7", "确认后暂停")
        r = c.post("/payment-reservations/CMD-2/confirm",
                   json={"external_ref": "BANK-CMD-2"})
        check("confirmed reservation retry idempotent",
              r.status_code == 200 and r.json()["status"] == "duplicate", r.text)
        check("confirm retry no extra payment",
              len(c.get("/rightsholders/S1/ledger").json()["payments"])
              == payments_before)
        # 已确认指令不可取消（原约定不变）
        r = c.post("/payment-reservations/CMD-2/cancel")
        check("confirmed cannot cancel",
              r.status_code == 409
              and r.json()["error"] == "reservation_not_pending", r.text)

        # ---------- 13) 解除：幂等、未知号 404；解除后按当时规则办理，不自动支付 ----------
        r = c.post("/payment-suspensions/GONE/release")
        check("release unknown 404", r.status_code == 404
              and r.json()["error"] == "payment_suspension_not_found", r.text)
        r = c.post("/payment-suspensions/SUSP-7/release")
        b = r.json()
        check("release 200",
              r.status_code == 200 and b["status"] == "released"
              and b["suspension_status"] == "released"
              and b["released_at"] is not None)
        check("release ops",
              [o["operation"] for o in b["operations"]] == ["activated", "released"])
        r = c.post("/payment-suspensions/SUSP-7/release")
        b2 = r.json()
        check("release idempotent",
              r.status_code == 200 and b2["status"] == "duplicate"
              and b2["suspension_status"] == "released")
        check("release idempotent no extra op", len(b2["operations"]) == 2)
        led = c.get("/rightsholders/S1/ledger").json()
        check("ledger suspension cleared",
              led["active_payment_suspension"] is None)
        # 解除不自动支付：付款数不变；此时可按当时余额/预留规则新付款
        paid_n = len(led["payments"])
        # S1 已付 30+20=50（余额 100），还可付不超过 50
        r = pay(c, "S1", "BANK-5", 50)
        check("manual payment after release",
              r.status_code == 201 and r.json()["balance_after"] == 0, r.text)
        check("no auto payment on release", paid_n == 2)

        # ---------- 14) 解除后可用新暂停号重新暂停；旧号同内容仍幂等不复活 ----------
        r = open_susp(c, "S1", "SUSP-7", "暂停后验证幂等等等", code=409)
        check("old ref different content conflicts",
              r.json()["error"] == "suspension_ref_conflict", r.text)
        # 旧号原内容重试仍返回 released 原记录
        r = c.post("/payment-suspensions", json={
            "holder_code": "S1", "suspension_ref": "SUSP-7",
            "reason": "确认后暂停"})
        check("old ref same content idempotent released",
              r.status_code == 200 and r.json()["status"] == "duplicate"
              and r.json()["suspension_status"] == "released")
        r = open_susp(c, "S1", "SUSP-8", "解除后重新暂停")
        check("reopen with new ref",
              r.status_code == 201 and r.json()["suspension_status"] == "active")
        pay(c, "S1", "BANK-6", 1, code=409)
        c.post("/payment-suspensions/SUSP-8/release")

        # 查询未知号 404；无暂停权利人对账单字段为 None
        check("get unknown 404",
              c.get("/payment-suspensions/NOPE").status_code == 404)
        check("S4 ledger no suspension",
              c.get("/rightsholders/S4/ledger").json()
              ["active_payment_suspension"] is None)

        # ---------- 15) 暂停与付款并发：按同一权利人提交先后决定，不变量成立 ----------
        # 全新权利人 C1 余额 100（独立期间数据，避免干扰）
        c.post("/works", json={"code": "W9", "title": "并发作品"})
        agree(c, "W9", "2031-01-01T00:00:00+08:00", "2033-01-01T00:00+08:00",
              [("C1", "100")])
        play(c, "CC-PAY", "W9", 100, when="2031-02-10T20:00:00+08:00")
        c.post("/statements/issue", json={
            "start": "2031-02-01T00:00:00+08:00",
            "end": "2031-03-01T00:00:00+08:00"})

        def race_once(i):
            """一对并发：暂停 SUSP-Ri 与付款 BANK-Ri（各 10，余额容许多次）。"""
            barrier = threading.Barrier(2)
            results = {}

            def do_susp():
                barrier.wait()
                r = c.post("/payment-suspensions", json={
                    "holder_code": "C1", "suspension_ref": f"SUSP-R{i}",
                    "reason": "并发暂停"})
                results["susp"] = r.status_code

            def do_pay():
                barrier.wait()
                r = c.post("/payments", json={
                    "holder_code": "C1", "external_ref": f"BANK-R{i}",
                    "amount": 10})
                results["pay"] = r.status_code
                if r.status_code == 201:
                    results["paid"] = r.json()["balance_after"]

            t1 = threading.Thread(target=do_susp)
            t2 = threading.Thread(target=do_pay)
            t1.start(); t2.start(); t1.join(); t2.join()

            # 解除暂停以便下一轮（解除与下一轮的付款/暂停不构成同对竞态）
            rel = c.post(f"/payment-suspensions/SUSP-R{i}/release")
            return results, rel.status_code

        observed_orders = set()
        for i in range(8):
            res, rel_code = race_once(i)
            check(f"race {i} valid codes",
                  res["susp"] == 201 and res["pay"] in (201, 409)
                  and rel_code == 200, str(res))
            if res["pay"] == 201:
                observed_orders.add("payment_first")
            else:
                observed_orders.add("suspension_first")
            # 不变量：无论谁先提交，对账单始终自洽；
            # 且若暂停先生效，该笔付款必不存在（拒绝不留痕）。
            led = c.get("/rightsholders/C1/ledger").json()
            paid_total = led["paid_total"]
            check(f"race {i} ledger consistent",
                  led["balance"] == 100 - paid_total
                  and led["active_payment_suspension"] is None, str(led))
            if res["pay"] == 409:
                check(f"race {i} rejected payment absent",
                      all(p["external_ref"] != f"BANK-R{i}"
                          for p in led["payments"]))
        check("both orders observed", observed_orders == {"payment_first",
              "suspension_first"}, str(observed_orders))

        # 终态：最后一轮解除后付款恢复正常；重新暂停后新付款必被拒
        r = pay(c, "C1", "BANK-AFTER", 5)
        check("payment after all races", r.status_code == 201, r.text)
        open_susp(c, "C1", "SUSP-FINAL", "终态暂停")
        r = pay(c, "C1", "BANK-FINAL", 1, code=409)
        check("blocked after final suspension",
              r.json()["error"] == "rightsholder_payments_suspended", r.text)
        # 暂停与"创建预留"并发：同暂停号/同指令号并发各自恰一次成功
        open_susp(c, "C2", "SUSP-C2-SEED", "seed") if False else None
        c.post("/works", json={"code": "W8", "title": "并发作品二"})
        agree(c, "W8", "2031-01-01T00:00:00+08:00", "2033-01-01T00:00+08:00",
              [("C2", "100")])
        play(c, "CC-RES", "W8", 100, when="2031-02-11T20:00:00+08:00")
        c.post("/statements/issue", json={
            "start": "2031-02-01T00:00:00+08:00",
            "end": "2031-03-01T00:00:00+08:00"})

        codes = []
        barrier = threading.Barrier(6)

        def fire_susp_same_ref():
            barrier.wait()
            r = c.post("/payment-suspensions", json={
                "holder_code": "C2", "suspension_ref": "SUSP-C2", "reason": "r"})
            codes.append(("susp", r.status_code))

        def fire_resv():
            barrier.wait()
            r = c.post("/payment-reservations", json={
                "holder_code": "C2", "instruction_ref": "CMD-C2", "amount": 10})
            codes.append(("resv", r.status_code))

        threads = [threading.Thread(target=fire_susp_same_ref) for _ in range(5)]
        threads.append(threading.Thread(target=fire_resv))
        [t.start() for t in threads]
        [t.join() for t in threads]
        susp_codes = [x[1] for x in codes if x[0] == "susp"]
        resv_codes = [x[1] for x in codes if x[0] == "resv"]
        # 同暂停号并发：恰一笔 201，其余 200 幂等
        check("same suspension ref concurrent: one 201 rest 200",
              susp_codes.count(201) == 1 and susp_codes.count(200) == 4,
              str(susp_codes))
        # 预留与暂停并发：要么 201（先提交），要么 409 suspended（暂停先生效）
        check("reservation vs suspension serialized",
              resv_codes == [201] or resv_codes == [409], str(resv_codes))
        led = c.get("/rightsholders/C2/ledger").json()
        if resv_codes == [409]:
            check("no reservation when suspended",
                  led["instructions"] == [] and led["reserved_total"] == 0)
            # 解除后预留恢复
            c.post("/payment-suspensions/SUSP-C2/release")
            r = reserve(c, "C2", "CMD-C2", 10)
            check("reserve after release", r.status_code == 201)
        else:
            check("reservation committed first",
                  len(led["instructions"]) == 1
                  and led["instructions"][0]["status"] == "pending")

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL PAYMENT SUSPENSION INTEGRATION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
