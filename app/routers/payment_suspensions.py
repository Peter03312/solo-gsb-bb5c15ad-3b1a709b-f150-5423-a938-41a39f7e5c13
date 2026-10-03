from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .. import suspensions_service as svc
from ..db import get_conn
from ..errors import APIError
from ..schemas import PaymentSuspensionIn, PaymentSuspensionOut

router = APIRouter(tags=["payment-suspensions"])


@router.post("/payment-suspensions")
def open_suspension(body: PaymentSuspensionIn, conn=Depends(get_conn)):
    """在权利人收款资料有争议时暂停其对外付款。

    - 凭权利人编号 holder_code、全局唯一暂停号 suspension_ref 与原因 reason
      登记，返回暂停状态与开启/解除操作记录；
    - 暂停期间直接付款（POST /payments）、创建付款预留
      （POST /payment-reservations）、确认既有待确认预留
      （.../confirm）一律 409 holder_payments_suspended 拒绝，不产生付款、
      不改变任何指令；既有待确认预留保留额度，仍可取消（.../cancel）；
      已完成付款/已确认预留的同内容重试仍按原接口约定幂等返回；
      出单、试算、退票与余额核对继续按原口径运行；
    - 同号同内容（同权利人、同原因）重试幂等返回原暂停
      （status=duplicate，HTTP 200，不改变状态，已解除旧号不复活）；
      同号异内容 409 suspension_ref_conflict；未知权利人 404
      holder_not_found；对同一权利人未解除即重复登记（含新暂停号）
      409 payment_suspension_already_active，拒绝且不留记录。
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


@router.post("/payment-suspensions/{suspension_ref}/release",
             response_model=PaymentSuspensionOut)
def release_suspension(suspension_ref: str, conn=Depends(get_conn)):
    """凭暂停号解除对外付款暂停。

    - active -> released：解除后按当时余额与预留规则办理付款/预留，
      不自动支付；重复解除幂等（status=duplicate，HTTP 200）；
    - 未知暂停号 404 payment_suspension_not_found。
    """
    result = svc.release_suspension(conn, suspension_ref)
    return PaymentSuspensionOut(**result).model_dump(mode="json")


@router.get("/payment-suspensions/{suspension_ref}",
            response_model=PaymentSuspensionOut)
def get_suspension(suspension_ref: str, conn=Depends(get_conn)):
    """查询暂停：权利人、状态（active/released）、原因与开启/解除操作记录。

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
