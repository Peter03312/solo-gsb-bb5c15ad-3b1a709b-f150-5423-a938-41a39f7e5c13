from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .. import payment_suspensions_service as svc
from ..db import get_conn
from ..errors import APIError
from ..schemas import PaymentSuspensionIn, PaymentSuspensionOut

router = APIRouter(tags=["payment-suspensions"])


@router.post("/payment-suspensions")
def open_suspension(body: PaymentSuspensionIn, conn=Depends(get_conn)):
    """登记权利人对外付款暂停（收款资料有争议时暂停其一切对外付款）。

    - 凭权利人编号 holder_code、全局唯一暂停号 suspension_ref 与原因 reason
      登记，返回暂停状态与激活/解除操作记录；
    - 同号同内容重试幂等返回原暂停（status=duplicate，HTTP 200，状态不变）；
      同号异内容（权利人或原因不同）409 suspension_ref_conflict；
    - 权利人未知 404 holder_not_found；同一权利人已有未解除暂停（含使用新
      暂停号）409 payment_suspension_already_active，拒绝且状态不变；
      解除后可用新暂停号重新登记；
    - 暂停生效期间，直接付款、创建付款预留、确认既有预留一律拒绝
      409 rightsholder_payments_suspended，不产生付款也不改变指令；
      既有待确认预留保留额度并仍可取消；出单、试算、退票与余额核对照常。
    """
    result = svc.open_suspension(
        conn, body.holder_code, body.suspension_ref, body.reason
    )
    # 新登记 201；同号同内容重试 200 且返回原暂停
    status_code = 201 if result["status"] == "created" else 200
    return JSONResponse(
        status_code=status_code,
        content=PaymentSuspensionOut(**result).model_dump(mode="json"),
    )


@router.post(
    "/payment-suspensions/{suspension_ref}/release",
    response_model=PaymentSuspensionOut,
)
def release_suspension(suspension_ref: str, conn=Depends(get_conn)):
    """凭暂停号解除对外付款暂停。

    - active -> released：解除后按当时余额与预留规则办理，不自动支付；
    - 重复解除幂等（status=duplicate，HTTP 200，原暂停状态不变）；
    - 未知暂停号 404 payment_suspension_not_found。
    """
    result = svc.release_suspension(conn, suspension_ref)
    return PaymentSuspensionOut(**result).model_dump(mode="json")


@router.get(
    "/payment-suspensions/{suspension_ref}",
    response_model=PaymentSuspensionOut,
)
def get_suspension(suspension_ref: str, conn=Depends(get_conn)):
    """查询暂停：权利人、原因、状态与激活/解除操作记录。

    未知暂停号返回 404 payment_suspension_not_found。
    """
    result = svc.get_suspension(conn, suspension_ref)
    if result is None:
        raise APIError(
            404,
            "payment_suspension_not_found",
            f"payment suspension {suspension_ref} not found",
        )
    return result
