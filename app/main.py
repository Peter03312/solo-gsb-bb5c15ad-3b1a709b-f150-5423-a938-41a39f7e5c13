from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .db import close_pool, init_pool
from .errors import APIError
from .routers import (
    balance_snapshots,
    catalog,
    payment_suspensions,
    payments,
    period_closings,
    play_disputes,
    plays,
    receipts,
    refunds,
    reservations,
    statements,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    yield
    await close_pool()


app = FastAPI(
    title="演出录音播放版税结算服务",
    version="1.8.0",
    description=(
        "登记作品与分成协议、导入播放记录（支持幂等与更正链）、"
        "按播放发生时间匹配协议并逐笔按比例分摊，按期间出具应付/追补单据，"
        "按权利人核对此前已出具单据与实际付款（正余额付款、流水号幂等、负额抵扣），"
        "付款额度预留（凭权利人/唯一指令号/正整数分额预留，取消释放、"
        "提交外部流水号原子确认付款，负额追补下更早预留 FIFO 优先），"
        "登记银行退票（凭原付款流水号与退回流水号、累计不超额、退回加回余额），"
        "按批次导入银行回单核验已登记付款（冻结逐行结论与累计退回额、"
        "迟到付款/退回复核版本化），"
        "并按唯一快照号一次性冻结权利人余额核对底稿（单据净额/付款/退回/"
        "待确认预留/余额/可用额，同集合幂等、异集合拒绝、分页读取、历史不改写），"
        "并对播放更正链登记计费争议暂缓（凭链内任一播放编号/唯一争议号/原因，"
        "作用于整条链、后续更正自动继承，试算与出单把链尾列为争议待处理而不计应付；"
        "凭争议号解除，幂等重试、同链重复开启拒绝，解除后按最新链尾与协议恢复），"
        "并按唯一关账号封存已出单期间（冻结最新单号/各权利人累计应付/待处理数量，"
        "关账后拒绝该期间出单、历史单据与归因不变，试算展示实时应付与关账差额；"
        "同号同期间重试幂等、异期间复用与同期间重复关账拒绝；凭关账号解除后"
        "可按既有规则出具追补，重复解除幂等；关账、解除与出单按期间串行），"
        "并在权利人收款资料有争议时凭权利人编号/全局唯一暂停号/原因暂停其对外"
        "付款（同号同内容重试幂等、异内容复用与未解除重复登记拒绝；暂停期间"
        "直接付款、创建预留、确认既有预留一律拒绝且不产生付款或改变指令，"
        "既有待确认预保留额可取消，已完成付款及确认的同内容重试仍幂等返回，"
        "出单/试算/退票/余额核对照常；凭暂停号解除、重复解除幂等，解除后"
        "按当时余额与预留规则办理且不自动支付；暂停与付款/预留按同一权利人"
        "提交先后串行）。"
    ),
    lifespan=lifespan,
)

app.include_router(catalog.router)
app.include_router(plays.router)
app.include_router(play_disputes.router)
app.include_router(statements.router)
app.include_router(payments.router)
app.include_router(reservations.router)
app.include_router(refunds.router)
app.include_router(receipts.router)
app.include_router(balance_snapshots.router)
app.include_router(period_closings.router)
app.include_router(payment_suspensions.router)


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}


@app.exception_handler(APIError)
async def api_error_handler(request, exc: APIError):
    return JSONResponse(status_code=exc.status_code, content=exc.detail)


@app.exception_handler(psycopg.Error)
async def psycopg_error_handler(request, exc: psycopg.Error):
    """把并发竞态下数据库直接抛出的约束错误映射为 409/422。"""
    if isinstance(exc, psycopg.errors.ExclusionViolation):
        code, message = "agreement_overlap", str(exc.diag.message_primary or exc)
        status = 409
    elif isinstance(exc, psycopg.errors.UniqueViolation):
        code, message = "duplicate", str(exc.diag.message_primary or exc)
        status = 409
    elif isinstance(exc, psycopg.errors.CheckViolation):
        code, message = "constraint_violation", str(exc.diag.message_primary or exc)
        status = 422
    elif isinstance(exc, psycopg.errors.ForeignKeyViolation):
        code, message = "foreign_key_violation", str(exc.diag.message_primary or exc)
        status = 422
    else:
        code, message = "database_error", str(exc)
        status = 500
    return JSONResponse(
        status_code=status,
        content={"error": code, "message": message, "detail": None},
    )
