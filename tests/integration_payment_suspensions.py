"""权利人对外付款暂停（收款资料有争议时暂停付款）：端到端集成测试。

pgserver 便携 PostgreSQL，无需 Docker。
运行: PYTHONPATH=. python3 tests/integration_payment_suspensions.py

场景按权利人/作品隔离：
- 权利人 S1：登记/幂等/异内容冲突/未知权利人/重复登记拒绝；
  暂停期间直接付款、创建预留、确认既有预留全部拒绝且不留痕；
  既有待确认预保留额并可取消；退票/出单/试算/余额核对照常；
  已完成付款与已确认预留的同内容重试仍按原约定返回；
  解除幂等/未知暂停号 404/解除后按余额与预留规则恢复且不自动支付；解除后新号重开。
- 权利人 S2：暂停前先预留，暂停后确认被拒、取消成功（释放额度）。
- 权利人 S3：暂停与直接付款/创建预留并发，按提交先后决定，不超付、无死锁。
"""
import os
import pathlib
import tempfile
import threading

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_sus_")) / "pgdata"
server = pgserver.get_server(_pgdata, cleanup_mode="delete")
try:
    os.environ["DATABASE_URL"] = server.get_uri()
    from fastapi.testclient import TestClient
    import psycopg
    from app import db
    from app.main import app

    failures = []

    def check(name, cond, extra=""):
        print(("PASS" if cond else "FAIL"), name, extra)
        if not cond:
            failures.append(name)

    period = {"start": "2026-02-01T00:00:00+08:00",
              "end": "2026-03-01T00:00:00+08:00"}

    def setup_work(code, shares):
        c.post("/works", json={"code": code, "title": code})
        for h in shares:
            c.post("/rightsholders", json={"code": h, "name": h})
        c.post("/agreements", json={
            "work_code": code,
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": h, "percent": str(p)}
                       for h, p in shares.items()]})

    def play_and_issue(import_id, work, amount, corrects=None):
        r = c.post("/plays/import", json={
            "import_id": import_id, "work_code": work,
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": amount,
            "corrects_id": corrects})
        assert r.status_code in (200, 201), r.text
        r = c.post("/statements/issue", json=period)
        assert r.status_code in (200, 201), r.text
        return r.json()

    with TestClient(app) as c:
        check("health", c.get("/health").json()["status"] == "ok")

        setup_work("WS1", {"S1": "100"})
        setup_work("WS2", {"S2": "100"})
        setup_work("WS3", {"S3": "100"})
        play_and_issue("PS1", "WS1", 200)   # S1 200（后两次出具为 S2/S3 追补）
        play_and_issue("PS2", "WS2", 200)   # S2 200
        play_and_issue("PS3", "WS3", 200)   # S3 200

        # =================================================================
        # 一、S1：登记、幂等、冲突、门禁、解除、恢复
        # =================================================================
        # 1. 登记暂停 201：返回状态与开启操作记录
        r = c.post("/payment-suspensions", json={
            "holder_code": "S1", "suspension_ref": "SUS-1",
            "reason": "收款账户待核验"})
        b = r.json()
        check("suspend created 201",
              r.status_code == 201 and b["status"] == "created"
              and b["suspension_status"] == "active"
              and b["holder_code"] == "S1" and b["reason"] == "收款账户待核验"
              and b["released_at"] is None
              and [op["operation"] for op in b["operations"]] == ["opened"]
              and b["operations"][0]["reason"] == "收款账户待核验", r.text)

        # GET 查询 / 未知暂停号 404
        r = c.get("/payment-suspensions/SUS-1")
        check("get suspension", r.status_code == 200
              and r.json()["suspension_status"] == "active"
              and len(r.json()["operations"]) == 1, r.text)
        r = c.get("/payment-suspensions/NOPE")
        check("get missing 404",
              r.status_code == 404
              and r.json()["error"] == "payment_suspension_not_found", r.text)

        # 对账单可见当前未解除暂停
        led = c.get("/rightsholders/S1/ledger").json()
        check("ledger shows active suspension",
              led["payment_suspension"] is not None
              and led["payment_suspension"]["suspension_ref"] == "SUS-1"
              and led["payment_suspension"]["suspension_status"] == "active"
              and led["balance"] == 200, str(led.get("payment_suspension")))

        # 2. 同号同内容重试 => 200 duplicate，不新增操作记录
        r = c.post("/payment-suspensions", json={
            "holder_code": "S1", "suspension_ref": "SUS-1",
            "reason": "收款账户待核验"})
        check("suspend idempotent retry",
              r.status_code == 200 and r.json()["status"] == "duplicate"
              and r.json()["suspension_status"] == "active"
              and len(r.json()["operations"]) == 1, r.text)

        # 同号异内容（异原因/异权利人）=> 409
        r = c.post("/payment-suspensions", json={
            "holder_code": "S1", "suspension_ref": "SUS-1",
            "reason": "别的原因"})
        check("suspend conflict reason",
              r.status_code == 409
              and r.json()["error"] == "suspension_ref_conflict"
              and r.json()["detail"]["existing_holder_code"] == "S1"
              and r.json()["detail"]["existing_reason"] == "收款账户待核验", r.text)
        r = c.post("/payment-suspensions", json={
            "holder_code": "S2", "suspension_ref": "SUS-1",
            "reason": "收款账户待核验"})
        check("suspend conflict holder",
              r.status_code == 409
              and r.json()["detail"]["existing_holder_code"] == "S1", r.text)

        # 未知权利人 => 404；空原因/空暂停号 => 422
        r = c.post("/payment-suspensions", json={
            "holder_code": "NOBODY", "suspension_ref": "SUS-X", "reason": "r"})
        check("suspend holder missing 404",
              r.status_code == 404
              and r.json()["error"] == "holder_not_found", r.text)
        for bad in ({}, {"holder_code": "S1", "suspension_ref": "SUS-X", "reason": ""},
                    {"holder_code": "S1", "suspension_ref": "", "reason": "r"}):
            r = c.post("/payment-suspensions", json=bad)
            check(f"suspend bad payload {bad!r} 422", r.status_code == 422, r.text)

        # 3. 对同一权利人未解除即重复登记（换新暂停号）=> 409，不留记录
        r = c.post("/payment-suspensions", json={
            "holder_code": "S1", "suspension_ref": "SUS-1B",
            "reason": "又一个争议"})
        check("duplicate active rejected",
              r.status_code == 409
              and r.json()["error"] == "payment_suspension_already_active"
              and r.json()["detail"]["existing_suspension_ref"] == "SUS-1", r.text)
        check("rejected duplicate leaves no record",
              c.get("/payment-suspensions/SUS-1B").status_code == 404)

        # 4. 暂停期间：直接付款、创建预留均拒绝（409 holder_payments_suspended）
        r = c.post("/payments", json={
            "holder_code": "S1", "external_ref": "PAY-S1-DIRECT", "amount": 10})
        check("direct pay suspended",
              r.status_code == 409
              and r.json()["error"] == "holder_payments_suspended"
              and r.json()["detail"]["suspension_ref"] == "SUS-1", r.text)
        check("direct pay rejected leaves no payment",
              c.get("/rightsholders/S1/ledger").json()["paid_total"] == 0)
        r = c.post("/payment-reservations", json={
            "holder_code": "S1", "instruction_ref": "INS-S1-NEW", "amount": 10})
        check("create reservation suspended",
              r.status_code == 409
              and r.json()["error"] == "holder_payments_suspended", r.text)
        check("create rejected leaves no instruction",
              "INS-S1-NEW" not in [
                  i["instruction_ref"]
                  for i in c.get("/rightsholders/S1/ledger").json()["instructions"]])

        # 5. 暂停不影响：出单（追补）、试算、退票、余额核对
        play_and_issue("PS1R1", "WS1", 100, corrects="PS1")   # S1 余额 100
        led = c.get("/rightsholders/S1/ledger").json()
        check("statements continue while suspended",
              led["billed_total"] == 100 and led["paid_total"] == 0
              and led["balance"] == 100, str(led))
        pv = c.get(
            "/statements/preview?start=2026-02-01T00:00:00%2B08:00"
            "&end=2026-03-01T00:00:00%2B08:00")
        check("preview continues while suspended", pv.status_code == 200, pv.text)
        # 退票口径照常：需先有一笔付款——S1 无法付款；改用 S2 的付款制造退票
        r = c.post("/payments", json={
            "holder_code": "S2", "external_ref": "PAY-S2-1", "amount": 50})
        check("S2 pay while S1 suspended", r.status_code == 201, r.text)
        r = c.post("/refunds", json={
            "payment_ref": "PAY-S2-1", "refund_ref": "REF-S2-1", "amount": 20})
        check("refund continues while S1 suspended",
              r.status_code == 201 and r.json()["balance_after"] == 170, r.text)
        # 余额快照（底稿冻结）口径照常
        r = c.post("/balance-snapshots", json={
            "snapshot_no": "SNAP-SUS", "holder_codes": ["S1"]})
        check("balance snapshot continues while suspended",
              r.status_code == 201
              and r.json()["lines"][0]["balance"] == 100, r.text)

        # 6. 已完成付款的同内容重试仍按原约定返回（幂等，不受 S2 是否暂停影响之外）
        r = c.post("/payments", json={
            "holder_code": "S2", "external_ref": "PAY-S2-1", "amount": 50})
        check("completed payment retry still idempotent",
              r.status_code == 200 and r.json()["status"] == "duplicate"
              and c.get("/rightsholders/S2/ledger").json()["paid_total"] == 50,
              r.text)

        # 7. 解除：released；重复解除 duplicate；未知号 404
        r = c.post("/payment-suspensions/SUS-1/release")
        b = r.json()
        check("release 200",
              r.status_code == 200 and b["status"] == "released"
              and b["suspension_status"] == "released"
              and b["released_at"] is not None
              and [op["operation"] for op in b["operations"]] == ["opened", "released"],
              r.text)
        r = c.post("/payment-suspensions/SUS-1/release")
        check("release idempotent",
              r.status_code == 200 and r.json()["status"] == "duplicate"
              and r.json()["suspension_status"] == "released"
              and len(r.json()["operations"]) == 2, r.text)
        r = c.post("/payment-suspensions/NOPE/release")
        check("release missing 404",
              r.status_code == 404
              and r.json()["error"] == "payment_suspension_not_found", r.text)

        # 解除后对账单不再显示活跃暂停
        check("ledger suspension cleared after release",
              c.get("/rightsholders/S1/ledger").json()["payment_suspension"] is None)

        # 8. 解除不自动支付：无新付款；按当时余额与预留规则恢复办理
        led = c.get("/rightsholders/S1/ledger").json()
        check("release does not auto-pay", led["paid_total"] == 0
              and led["balance"] == 100, str(led))
        r = c.post("/payment-reservations", json={
            "holder_code": "S1", "instruction_ref": "INS-S1-A", "amount": 60})
        check("reserve after release ok", r.status_code == 201
              and r.json()["available_balance"] == 40, r.text)
        r = c.post("/payment-reservations/INS-S1-A/confirm",
                   json={"external_ref": "PAY-S1-A"})
        check("confirm after release ok",
              r.status_code == 201 and r.json()["balance_after"] == 40, r.text)
        r = c.post("/payments", json={
            "holder_code": "S1", "external_ref": "PAY-S1-B", "amount": 40})
        check("direct pay after release ok",
              r.status_code == 201 and r.json()["balance_after"] == 0, r.text)

        # 9. 解除后用新暂停号可重新暂停；旧号同内容重试仍返回已解除结果（不复活）
        r = c.post("/payment-suspensions", json={
            "holder_code": "S1", "suspension_ref": "SUS-2",
            "reason": "新争议"})
        check("re-suspend with new ref after release",
              r.status_code == 201 and r.json()["suspension_status"] == "active",
              r.text)
        r = c.post("/payment-suspensions", json={
            "holder_code": "S1", "suspension_ref": "SUS-1",
            "reason": "收款账户待核验"})
        check("old released ref retry stays released",
              r.status_code == 200 and r.json()["status"] == "duplicate"
              and r.json()["suspension_status"] == "released", r.text)
        check("S1 still suspended via SUS-2",
              c.get("/rightsholders/S1/ledger").json()["payment_suspension"]
              ["suspension_ref"] == "SUS-2")
        c.post("/payment-suspensions/SUS-2/release")

        # =================================================================
        # 二、S2：暂停前先预留；暂停后确认被拒、取消成功（额度保留可取消）
        # =================================================================
        # S2 当前余额 170（200 - 50 + 20），无 pending 预留
        r = c.post("/payment-reservations", json={
            "holder_code": "S2", "instruction_ref": "INS-S2-KEEP", "amount": 30})
        check("S2 reserve before suspend", r.status_code == 201
              and r.json()["available_balance"] == 140, r.text)

        r = c.post("/payment-suspensions", json={
            "holder_code": "S2", "suspension_ref": "SUS-S2", "reason": "资料争议"})
        check("S2 suspend 201", r.status_code == 201, r.text)

        # 确认既有待确认预留 => 拒绝，指令保持 pending、不产生付款
        r = c.post("/payment-reservations/INS-S2-KEEP/confirm",
                   json={"external_ref": "PAY-S2-KEEP"})
        check("confirm existing reservation suspended",
              r.status_code == 409
              and r.json()["error"] == "holder_payments_suspended", r.text)
        led = c.get("/rightsholders/S2/ledger").json()
        keep = next(i for i in led["instructions"]
                    if i["instruction_ref"] == "INS-S2-KEEP")
        check("instruction stays pending, reservation retained",
              keep["status"] == "pending" and led["reserved_total"] == 30
              and "PAY-S2-KEEP" not in [p["external_ref"] for p in led["payments"]]
              and led["balance"] == 170, str(keep))

        # 仍可取消：pending -> cancelled，释放额度，不涉及对外付款
        r = c.post("/payment-reservations/INS-S2-KEEP/cancel")
        check("cancel pending reservation while suspended",
              r.status_code == 200 and r.json()["status"] == "cancelled"
              and r.json()["reserved_total"] == 0
              and r.json()["available_balance"] == 170, r.text)
        # 已确认预留的同内容重试不受影响：S2 无已确认预留，此条在 S1 已验证
        # 直接付款在暂停期间仍被拒
        r = c.post("/payments", json={
            "holder_code": "S2", "external_ref": "PAY-S2-X", "amount": 1})
        check("S2 direct pay suspended", r.status_code == 409
              and r.json()["error"] == "holder_payments_suspended", r.text)
        c.post("/payment-suspensions/SUS-S2/release")

        # =================================================================
        # 三、S3：暂停与直接付款、创建预留并发，按提交先后决定
        # =================================================================
        # S3 余额 200。用 barrier 让"暂停"与多笔付款/预留同时发起；
        # 行锁串行后：恰一笔暂停生效；成功的付款 + 成功预留占用 ≤ 200，
        # 暂停生效后的请求全部 409 holder_payments_suspended，无死锁、不超付。
        outcomes = {"pay_created": 0, "pay_suspended": 0, "pay_other_rej": 0,
                    "res_created": 0, "res_suspended": 0, "res_other_rej": 0,
                    "suspensions": [], "errors": []}
        barrier = threading.Barrier(9)

        def fire_pay(i):
            barrier.wait()
            try:
                rr = c.post("/payments", json={
                    "holder_code": "S3", "external_ref": f"PAY-S3-{i}",
                    "amount": 60})
                if rr.status_code == 201:
                    outcomes["pay_created"] += 1
                elif rr.status_code == 409 and \
                        rr.json()["error"] == "holder_payments_suspended":
                    outcomes["pay_suspended"] += 1
                else:
                    outcomes["pay_other_rej"] += 1
            except Exception as e:  # noqa
                outcomes["errors"].append(repr(e))

        def fire_res(i):
            barrier.wait()
            try:
                rr = c.post("/payment-reservations", json={
                    "holder_code": "S3", "instruction_ref": f"INS-S3-{i}",
                    "amount": 60})
                if rr.status_code == 201:
                    outcomes["res_created"] += 1
                elif rr.status_code == 409 and \
                        rr.json()["error"] == "holder_payments_suspended":
                    outcomes["res_suspended"] += 1
                else:
                    outcomes["res_other_rej"] += 1
            except Exception as e:  # noqa
                outcomes["errors"].append(repr(e))

        def fire_suspend():
            barrier.wait()
            try:
                rr = c.post("/payment-suspensions", json={
                    "holder_code": "S3", "suspension_ref": "SUS-S3",
                    "reason": "并发放款争议"})
                outcomes["suspensions"].append(rr.status_code)
            except Exception as e:  # noqa
                outcomes["errors"].append(repr(e))

        threads = [threading.Thread(target=fire_pay, args=(i,)) for i in range(4)]
        threads += [threading.Thread(target=fire_res, args=(i,)) for i in range(4)]
        threads += [threading.Thread(target=fire_suspend)]
        [t.start() for t in threads]
        [t.join() for t in threads]

        led = c.get("/rightsholders/S3/ledger").json()
        committed_out = led["paid_total"] + led["reserved_total"]
        # 行锁串行后的合法结局只有两类：201 成功、409（暂停生效 或 余额/可用额
        # 不足——暂停提交前最后到达者可能因额度已满被拒）。两种 409 都不产生
        # 付款/预留；关键不变量：恰一笔暂停生效、无异常无死锁、占用绝不超余额。
        check("concurrent suspension vs payments serialized",
              not outcomes["errors"]
              and outcomes["suspensions"] == [201]
              and outcomes["pay_suspended"] + outcomes["res_suspended"] >= 1
              and (outcomes["pay_created"] + outcomes["pay_suspended"]
                   + outcomes["pay_other_rej"]) == 4
              and (outcomes["res_created"] + outcomes["res_suspended"]
                   + outcomes["res_other_rej"]) == 4
              and committed_out <= 200
              and led["balance"] - led["reserved_total"] >= 0
              and led["payment_suspension"] is not None
              and led["payment_suspension"]["suspension_ref"] == "SUS-S3",
              f"{outcomes} {led}")

        # 暂停生效后再发起，确定性全部被拒（不产生付款、不改变指令）
        paid_before = led["paid_total"]
        reserved_before = led["reserved_total"]
        r1 = c.post("/payments", json={
            "holder_code": "S3", "external_ref": "PAY-S3-AFTER", "amount": 1})
        r2 = c.post("/payment-reservations", json={
            "holder_code": "S3", "instruction_ref": "INS-S3-AFTER", "amount": 1})
        led2 = c.get("/rightsholders/S3/ledger").json()
        check("after suspension effective all rejected",
              r1.status_code == 409 and r2.status_code == 409
              and led2["paid_total"] == paid_before
              and led2["reserved_total"] == reserved_before, f"{r1.text} {r2.text}")

        # 同暂停号 8 并发登记（同内容）=> 恰一笔 created，其余 duplicate
        c.post("/payment-suspensions/SUS-S3/release")
        same_results = []
        b3 = threading.Barrier(8)

        def fire_same():
            b3.wait()
            rr = c.post("/payment-suspensions", json={
                "holder_code": "S3", "suspension_ref": "SUS-S3-DUP",
                "reason": "同号并发"})
            same_results.append((rr.status_code, rr.json().get("status")))

        ts = [threading.Thread(target=fire_same) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        created = sum(1 for s, st in same_results if st == "created")
        dup = sum(1 for s, st in same_results if st == "duplicate")
        check("concurrent same-ref suspension one created",
              created == 1 and dup == 7, str(same_results))

        # =================================================================
        # 四、建表脚本可重复执行（幂等迁移），历史数据不改写
        # =================================================================
        with db.pool.connection() as db_conn:
            db_conn.execute(db.SCHEMA_SQL)
        check("schema idempotent re-run",
              c.get("/health").json()["status"] == "ok"
              and c.get("/payment-suspensions/SUS-1").json()["suspension_status"]
              == "released"
              and c.get("/rightsholders/S1/ledger").json()["paid_total"] == 100)

        # 触发器：暂停头/操作记录不可改删，终态不可迁移
        with db.pool.connection() as db_conn:
            sus_id = db_conn.execute(
                "SELECT id FROM payment_suspensions WHERE suspension_ref='SUS-1'"
            ).fetchone()["id"]
            denied = []
            for sql, args in (
                ("UPDATE payment_suspensions SET reason='x' WHERE id=%s", (sus_id,)),
                ("UPDATE payment_suspensions SET status='active' WHERE id=%s",
                 (sus_id,)),
                ("DELETE FROM payment_suspensions WHERE id=%s", (sus_id,)),
            ):
                try:
                    db_conn.execute(sql, args)
                    denied.append(False)
                except psycopg.Error:
                    denied.append(True)
            ev_id = db_conn.execute(
                "SELECT id FROM payment_suspension_events WHERE suspension_id=%s "
                "LIMIT 1", (sus_id,)).fetchone()["id"]
            try:
                db_conn.execute(
                    "UPDATE payment_suspension_events SET reason='x' WHERE id=%s",
                    (ev_id,))
                ev_frozen = False
            except psycopg.Error:
                ev_frozen = True
        check("suspension frozen by trigger", all(denied) and ev_frozen,
              f"{denied} {ev_frozen}")

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL PAYMENT-SUSPENSION INTEGRATION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
