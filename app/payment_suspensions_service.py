"""权利人对外付款暂停（财务在收款资料有争议时暂停付款）。

财务可凭"权利人编号 holder_code、调用方唯一暂停号 suspension_ref 与
原因 reason"登记对该权利人的对外付款暂停：

- 登记暂停（active）：返回暂停状态与操作记录；
  同号同内容重试幂等返回原暂停（status=duplicate，HTTP 200，状态不变）；
  同号异内容（权利人或原因不同）409 suspension_ref_conflict；
  权利人未知 404 holder_not_found；
  对同一权利人登记新暂停而其仍有未解除（active）暂停时
  409 payment_suspension_already_active（即使使用新暂停号），拒绝且状态不变；
  解除后可用新暂停号重新登记。
- 解除：只凭暂停号；active -> released，落解除操作记录；
  重复解除幂等（status=duplicate，HTTP 200）；未知暂停号
  404 payment_suspension_not_found。解除后按当时余额与预留规则办理，
  不自动支付。
- 查询：返回权利人、原因、状态、激活/解除时间与操作记录（按发生次序；
  幂等重试与被拒绝请求不留痕）。

暂停生效期间的统一口径（付款侧在权利人行锁下调用 active_suspension 判定）：
- 直接付款（POST /payments）、新建付款预留（POST /payment-reservations）、
  确认既有预留（POST /payment-reservations/{ref}/confirm）一律拒绝
  409 rightsholder_payments_suspended，不产生付款、不创建/不改变预留指令；
- 已完成付款及已确认预留的同内容重试仍按原接口约定幂等返回
  （在暂停判定之前命中原记录）；
- 既有待确认（pending）预留保留额度，仍可取消（取消释放额度，不受暂停限制）；
- 出单、试算、银行退票与余额核对继续按原口径运行（退票照常加回余额）。

并发协议（暂停与付款、预留按同一权利人的提交先后决定结果，无死锁）：
- 登记暂停：暂停号事务咨询锁（同号并发请求串行，幂等/冲突判定确定）
  → 权利人行锁 FOR UPDATE（与付款/预留/退票/出单互斥）→ 查重并写入。
- 解除：暂停号事务咨询锁 → 暂停行锁 → 权利人行锁（与付款/预留的生效判定
  互斥，保证"解除提交后"的请求才看到 released）→ active -> released。
- 付款/预留（创建）/预留确认：业务流水号咨询锁 → 权利人行锁（确认还先取
  指令行锁）→ 行锁下读生效中暂停后再做余额/可用额校验。
  加锁次序统一为"业务流水号咨询锁 → 指令行/付款行 → 权利人行锁"，
  暂停号锁是独立命名空间的首锁且不会与他人的权利人锁交叉持有，无死锁。
  因此先提交者建立的状态对后到者整体可见：暂停先生效则其后付款/预留/确认
  必被拒；付款/确认先提交则暂停登记只阻止其后的新增动作，不撤销历史付款。
"""
from .errors import APIError
from .payments_service import take_ref_advisory_lock

# 暂停号事务咨询锁命名空间
_SUSPENSION_NS = 0x53555331  # "SUS1"

_SUSPENSION_COLS = (
    "id, suspension_ref, holder_code, reason, status, "
    "activated_at, created_at, released_at"
)


def active_suspension(conn, holder_code: str):
    """读取权利人当前生效中（active）的暂停；无则 None。

    付款/预留在持权利人行锁的事务内调用：READ COMMITTED 语句快照 +
    行锁互斥保证"暂停是否已生效"的判定与付款插入/预留状态迁移原子一致。
    """
    return conn.execute(
        f"SELECT {_SUSPENSION_COLS} FROM payment_suspensions "
        "WHERE holder_code = %s AND status = 'active'",
        (holder_code,),
    ).fetchone()


def reject_if_active(conn, holder_code: str) -> None:
    """付款/预留在权利人行锁下调用：暂停生效中即拒绝（不留付款、不改指令）。

    - 直接付款、创建预留、确认预留均在此拒绝：409 rightsholder_payments_suspended；
    - 已完成付款/已确认预留的同内容幂等重试在更早的"原记录命中"分支返回，
      不会走到本判定；取消预留不调用本判定（暂停期间仍可取消、释放额度）。
    """
    susp = active_suspension(conn, holder_code)
    if susp is not None:
        raise APIError(
            409,
            "rightsholder_payments_suspended",
            f"outgoing payments to rightsholder {holder_code} are suspended by "
            f"suspension {susp['suspension_ref']}; operation rejected and no "
            "payment written and no instruction changed",
            detail={
                "holder_code": holder_code,
                "suspension_ref": susp["suspension_ref"],
                "suspension_status": susp["status"],
            },
        )


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
        "reason": row["reason"],
        # 请求级状态：created=本次登记；released=本次解除；
        # duplicate=同号同内容重试或重复解除；查询时与暂停自身状态一致
        "status": request_status,
        # 暂停自身状态：active=暂停生效中 | released=已解除
        "suspension_status": row["status"],
        "activated_at": row["activated_at"],
        "released_at": row["released_at"],
        "operations": _load_events(conn, row["id"]),
    }


def open_suspension(conn, holder_code: str, suspension_ref: str, reason: str) -> dict:
    """登记对权利人的对外付款暂停。

    返回 payload：新登记由路由返回 201，同号同内容重试返回 200。
    """
    with conn.transaction():
        # 1) 暂停号咨询锁：同号并发请求串行化，幂等/冲突判定确定。
        take_ref_advisory_lock(conn, _SUSPENSION_NS, suspension_ref)

        existing = conn.execute(
            f"SELECT {_SUSPENSION_COLS} FROM payment_suspensions "
            "WHERE suspension_ref = %s",
            (suspension_ref,),
        ).fetchone()
        if existing is not None:
            if (
                existing["holder_code"] != holder_code
                or existing["reason"] != reason
            ):
                raise _ref_conflict(suspension_ref, existing)
            # 同号同内容重试：无论 active/released 都幂等返回原记录，不改变状态。
            return _payload(conn, existing, request_status="duplicate")

        # 2) 锁定权利人行：未知权利人 404；与付款/预留创建/确认/退票/出单
        #    对该权利人的处理互斥——本事务提交后取得该行锁的请求必看到暂停。
        holder = conn.execute(
            "SELECT code FROM rightsholders WHERE code = %s FOR UPDATE",
            (holder_code,),
        ).fetchone()
        if holder is None:
            raise _holder_not_found(holder_code)

        # 3) 同一权利人已有未解除暂停：拒绝且不写任何记录（新暂停号也拒绝）。
        active = active_suspension(conn, holder_code)
        if active is not None:
            raise _already_active(holder_code, active)

        row = conn.execute(
            """
            INSERT INTO payment_suspensions(suspension_ref, holder_code, reason)
            VALUES (%s, %s, %s)
            RETURNING id, activated_at, created_at
            """,
            (suspension_ref, holder_code, reason),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO payment_suspension_events(suspension_id, event_type, reason)
            VALUES (%s, 'activated', %s)
            """,
            (row["id"], reason),
        )

        created = conn.execute(
            f"SELECT {_SUSPENSION_COLS} FROM payment_suspensions WHERE id = %s",
            (row["id"],),
        ).fetchone()
        return _payload(conn, created, request_status="created")


def release_suspension(conn, suspension_ref: str) -> dict:
    """凭暂停号解除暂停（幂等：重复解除返回原记录，状态不变）。"""
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

        # 3) 权利人行锁：与付款/预留创建/确认串行——解除提交前它们看到 active
        #    （拒绝），解除提交后看到 released（按当时余额与预留规则办理），
        #    不混合前后状态。解除本身不自动支付、不改变任何指令或付款。
        holder = conn.execute(
            "SELECT code FROM rightsholders WHERE code = %s FOR UPDATE",
            (row["holder_code"],),
        ).fetchone()
        if holder is None:  # 外键已建档，理论不可达
            raise _holder_not_found(row["holder_code"])

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
                f"payment suspension {suspension_ref} is not active; release rejected",
            )
        conn.execute(
            """
            INSERT INTO payment_suspension_events(suspension_id, event_type, reason)
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
        f"rightsholder {holder_code} not found",
        detail={"holder_code": holder_code},
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
        f"{existing['suspension_ref']}; duplicate suspension rejected and state unchanged",
        detail={
            "holder_code": holder_code,
            "existing_suspension_ref": existing["suspension_ref"],
        },
    )
