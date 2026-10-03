"""权利人对外付款暂停（财务在收款资料有争议时暂停付款）。

财务在权利人收款资料有争议时，可凭"权利人编号 holder_code、调用方全局
唯一暂停号 suspension_ref 与原因 reason"登记一次暂停，返回暂停状态与
开启/解除操作记录。

- 登记暂停：
  - 同号同内容（同权利人、同原因）重试幂等返回原暂停（status=duplicate，
    HTTP 200），不改变状态（active/released 均如此，已解除的旧号不复活）；
  - 同号异内容（权利人或原因不同）409 suspension_ref_conflict；
  - 权利人未知 404 holder_not_found；
  - 同一权利人已有未解除暂停时再次登记 409 payment_suspension_already_active，
    拒绝且状态不变、不留记录（即使使用新暂停号）；
  - 解除后可用新暂停号重新暂停。
- 暂停效力（见 payments_service / reservations_service 中的门禁）：
  - 直接付款、创建付款预留、确认既有待确认预留一律拒绝
    （409 holder_payments_suspended），不产生付款、不改变任何指令；
  - 既有待确认预留保留额度，仍可取消（取消不涉及对外付款）；
  - 已完成付款及已确认预留的"同内容重试"仍按原接口约定幂等返回原记录；
  - 出单、试算、退票与余额核对继续按原口径运行，不受暂停影响。
- 解除：只凭暂停号；
  - active -> released，落解除操作记录，返回原暂停；
  - 重复解除幂等（status=duplicate，HTTP 200）；
  - 未知暂停号 404 payment_suspension_not_found。
  - 解除不自动支付：此后付款/预留按当时余额与预留规则办理。
- 查询：返回权利人、状态、原因、开启/解除时间与操作记录
  （操作记录按发生次序；幂等重试与被拒绝请求不留痕）。

并发协议（与付款、预留按同一权利人的提交先后决定结果）：
- 登记暂停：暂停号事务咨询锁（同号并发请求串行，幂等/冲突判定确定）
  → 权利人行锁 FOR UPDATE（与直接付款、创建/确认预留、退回、出单持有
  的权利人行锁互斥）→ 活跃暂停检查与插入。
- 直接付款、创建预留、确认预留：各自的业务流水号咨询锁 → 权利人行锁
  → 在同一行锁事务内读取"是否存在活跃暂停"再做校验/写入。
  因而同一权利人上"暂停"与"付款/预留创建/确认"在行锁上完全串行：
  先提交者建立的状态对后到者整体可见——暂停先生效则其后操作必被拒，
  付款/确认先提交则暂停必在其之后生效。出单按编号升序锁多个权利人行，
  本事务只锁单个权利人行，加锁次序统一（业务流水号咨询锁 → 单个
  权利人行锁），无死锁。
- 解除：暂停号事务咨询锁 → 暂停行锁 FOR UPDATE → active -> released。
  解除与付款并发的先后以各自提交次序为准（付款是否看到活跃暂停），
  解除不自动支付。
"""
from .errors import APIError
from .payments_service import take_ref_advisory_lock

# 暂停号事务咨询锁命名空间
_SUSPENSION_NS = 0x53555331  # "SUS1"

_SUSPENSION_COLS = (
    "id, suspension_ref, holder_code, reason, status, "
    "activated_at, created_at, released_at"
)


def active_suspension(conn, holder_code: str) -> dict | None:
    """返回权利人当前未解除（active）暂停；无则 None。

    付款/预留门禁调用：必须在持有该权利人行锁（FOR UPDATE）的事务内调用，
    以与登记暂停事务在行锁上串行（提交先后决定结果，不混合前后状态）。
    """
    return conn.execute(
        f"SELECT {_SUSPENSION_COLS} FROM payment_suspensions "
        "WHERE holder_code = %s AND status = 'active'",
        (holder_code,),
    ).fetchone()


def ensure_not_suspended(conn, holder_code: str) -> None:
    """付款/预留门禁：权利人处于付款暂停中则拒绝（不留任何付款/指令变更）。

    在权利人行锁事务内调用。既有待确认预留的取消不经过此门禁（取消不付款）。
    """
    row = active_suspension(conn, holder_code)
    if row is not None:
        raise _holder_suspended(holder_code, row)


def _load_events(conn, suspension_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT event_type, reason, created_at
        FROM payment_suspension_events
        WHERE suspension_id = %s
        ORDER BY id
        """,
        (suspension_id,),
    ).fetchall()
    return [
        {"operation": r["event_type"], "reason": r["reason"], "at": r["created_at"]}
        for r in rows
    ]


def _payload(conn, row, *, request_status: str) -> dict:
    return {
        "suspension_ref": row["suspension_ref"],
        "holder_code": row["holder_code"],
        # 请求级状态：created=本次开启；released=本次解除；
        # duplicate=同号重试开启或重复解除；查询时与暂停自身状态一致
        "status": request_status,
        "suspension_status": row["status"],
        "reason": row["reason"],
        "activated_at": row["activated_at"],
        "released_at": row["released_at"],
        "operations": _load_events(conn, row["id"]),
    }


def open_suspension(conn, holder_code: str, suspension_ref: str, reason: str) -> dict:
    """对权利人登记对外付款暂停。

    返回 payload：新登记 HTTP 201，同号同内容重试 HTTP 200。
    """
    with conn.transaction():
        # 1) 暂停号咨询锁：同号并发请求串行化。
        take_ref_advisory_lock(conn, _SUSPENSION_NS, suspension_ref)

        existing = conn.execute(
            f"SELECT {_SUSPENSION_COLS} FROM payment_suspensions "
            "WHERE suspension_ref = %s",
            (suspension_ref,),
        ).fetchone()
        if existing is not None:
            # 内容按"权利人 + 原因"判定；同号同内容重试无论 active/released
            # 都幂等返回原结果，不改变状态（已解除的旧号不复活）。
            same_content = (
                existing["holder_code"] == holder_code
                and existing["reason"] == reason
            )
            if not same_content:
                raise _ref_conflict(suspension_ref, existing)
            return _payload(conn, existing, request_status="duplicate")

        # 2) 锁定权利人行：与付款/预留确认/创建、退回、出单互斥；
        #    权利人不存在则 404（整次拒绝，不留暂停）。
        holder = conn.execute(
            "SELECT code FROM rightsholders WHERE code = %s FOR UPDATE",
            (holder_code,),
        ).fetchone()
        if holder is None:
            raise _holder_not_found(holder_code)

        # 3) 同一权利人未解除即重复登记（含使用新暂停号）：拒绝且不留记录。
        active = active_suspension(conn, holder_code)
        if active is not None:
            raise _already_active(holder_code, active)

        row = conn.execute(
            """
            INSERT INTO payment_suspensions (suspension_ref, holder_code, reason)
            VALUES (%s, %s, %s)
            RETURNING id, activated_at, created_at
            """,
            (suspension_ref, holder_code, reason),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO payment_suspension_events (suspension_id, event_type, reason)
            VALUES (%s, 'opened', %s)
            """,
            (row["id"], reason),
        )

        created = conn.execute(
            f"SELECT {_SUSPENSION_COLS} FROM payment_suspensions WHERE id = %s",
            (row["id"],),
        ).fetchone()
        return _payload(conn, created, request_status="created")


def release_suspension(conn, suspension_ref: str) -> dict:
    """凭暂停号解除（幂等：重复解除返回原暂停，状态不变）。"""
    with conn.transaction():
        # 1) 暂停号咨询锁：同号并发解除串行。
        take_ref_advisory_lock(conn, _SUSPENSION_NS, suspension_ref)

        # 2) 锁暂停行：与一切状态判定互斥。
        row = conn.execute(
            f"SELECT {_SUSPENSION_COLS} FROM payment_suspensions "
            "WHERE suspension_ref = %s FOR UPDATE",
            (suspension_ref,),
        ).fetchone()
        if row is None:
            raise _suspension_not_found(suspension_ref)

        if row["status"] == "released":
            return _payload(conn, row, request_status="duplicate")

        updated = conn.execute(
            """
            UPDATE payment_suspensions
            SET status = 'released', released_at = now()
            WHERE id = %s AND status = 'active'
            RETURNING id
            """,
            (row["id"],),
        ).fetchone()
        if updated is None:  # 行锁下理论不可达，防御性兜底
            refreshed = conn.execute(
                f"SELECT {_SUSPENSION_COLS} FROM payment_suspensions WHERE id = %s",
                (row["id"],),
            ).fetchone()
            if refreshed is not None and refreshed["status"] == "released":
                return _payload(conn, refreshed, request_status="duplicate")
            raise APIError(
                409,
                "payment_suspension_not_active",
                f"payment suspension {suspension_ref} is not active; "
                "release rejected",
            )
        conn.execute(
            """
            INSERT INTO payment_suspension_events (suspension_id, event_type, reason)
            VALUES (%s, 'released', %s)
            """,
            (row["id"], row["reason"]),
        )

        released = conn.execute(
            f"SELECT {_SUSPENSION_COLS} FROM payment_suspensions WHERE id = %s",
            (row["id"],),
        ).fetchone()
        return _payload(conn, released, request_status="released")


def get_suspension(conn, suspension_ref: str) -> dict | None:
    row = conn.execute(
        f"SELECT {_SUSPENSION_COLS} FROM payment_suspensions "
        "WHERE suspension_ref = %s",
        (suspension_ref,),
    ).fetchone()
    if row is None:
        return None
    return _payload(conn, row, request_status=row["status"])


def _holder_not_found(holder_code: str):
    return APIError(
        404,
        "holder_not_found",
        f"rightsholder {holder_code} not found; cannot suspend payments",
    )


def _suspension_not_found(suspension_ref: str):
    return APIError(
        404,
        "payment_suspension_not_found",
        f"payment suspension {suspension_ref} not found",
        detail={"suspension_ref": suspension_ref},
    )


def _ref_conflict(suspension_ref: str, existing):
    return APIError(
        409,
        "suspension_ref_conflict",
        f"suspension ref {suspension_ref} already registered with different content",
        detail={
            "suspension_ref": suspension_ref,
            "existing_holder_code": existing["holder_code"],
            "existing_reason": existing["reason"],
            "existing_status": existing["status"],
        },
    )


def _already_active(holder_code: str, existing):
    return APIError(
        409,
        "payment_suspension_already_active",
        f"rightsholder {holder_code} already has active payment suspension "
        f"{existing['suspension_ref']}; duplicate suspension rejected and "
        "state unchanged (release it before registering a new one)",
        detail={
            "holder_code": holder_code,
            "existing_suspension_ref": existing["suspension_ref"],
        },
    )


def _holder_suspended(holder_code: str, active):
    return APIError(
        409,
        "holder_payments_suspended",
        f"outbound payments to rightsholder {holder_code} are suspended under "
        f"{active['suspension_ref']}; direct payment, reservation creation and "
        "reservation confirmation are rejected, no payment is created and no "
        "instruction is changed",
        detail={
            "holder_code": holder_code,
            "suspension_ref": active["suspension_ref"],
            "suspension_status": active["status"],
        },
    )
