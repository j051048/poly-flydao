from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from polybot.config import TradingMode, get_settings
from polybot.models import utc_now
from polybot.runtime import Runtime, build_runtime


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.runtime = build_runtime()
    try:
        yield
    finally:
        await app.state.runtime.close()


app = FastAPI(
    title="Polybot Control API",
    version="0.1.0",
    description="Evidence-driven Polymarket automation; paper mode by default.",
    lifespan=lifespan,
)

_api_settings = get_settings()
if _api_settings.allowed_dashboard_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_api_settings.allowed_dashboard_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )


def runtime(request: Request) -> Runtime:
    return request.app.state.runtime


RuntimeDep = Annotated[Runtime, Depends(runtime)]


def require_admin(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    settings = runtime(request).settings
    if settings.admin_token is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "admin operations are disabled")
    expected = f"Bearer {settings.admin_token.get_secret_value()}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid admin token")


class ArmRequest(BaseModel):
    mode: TradingMode
    minutes: int = Field(default=5, ge=1, le=15)


@app.get("/health")
async def health(rt: RuntimeDep) -> JSONResponse:
    store_ok = await rt.store.health()
    return JSONResponse(
        status_code=200 if store_ok else 503,
        content={
            "ok": store_ok,
            "mode": rt.settings.mode.value,
            "store": "healthy" if store_ok else "unhealthy",
            "real_money": rt.settings.mode in {TradingMode.CANARY, TradingMode.LIVE},
        },
    )


@app.get("/livez")
async def liveness() -> dict[str, bool]:
    return {"ok": True}


@app.get("/v1/status", dependencies=[Depends(require_admin)])
async def get_status(rt: RuntimeDep) -> dict[str, object]:
    control = await rt.store.get_runtime_control(rt.settings.account_id)
    return {
        "mode": rt.settings.mode,
        "ai_provider": rt.settings.ai_provider,
        "forecast_model": rt.settings.forecast_model,
        "control": control.model_dump(mode="json"),
        "risk_limits": {
            "min_edge": str(rt.settings.min_edge),
            "max_order_usd": str(rt.settings.max_order_usd),
            "max_trade_risk_pct": str(rt.settings.max_trade_risk_pct),
            "max_event_exposure_pct": str(rt.settings.max_event_exposure_pct),
            "max_gross_exposure_pct": str(rt.settings.max_gross_exposure_pct),
            "daily_loss_limit_pct": str(rt.settings.daily_loss_limit_pct),
            "max_drawdown_pct": str(rt.settings.max_drawdown_pct),
        },
    }


@app.post("/v1/cycles/run", dependencies=[Depends(require_admin)])
async def run_cycle(rt: RuntimeDep) -> dict[str, object]:
    if rt.settings.mode in {TradingMode.CANARY, TradingMode.LIVE}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "real-money cycles run only in the leased Zeabur worker",
        )
    report = await rt.engine.run_cycle()
    return report.model_dump(mode="json")


@app.post("/v1/control/arm", dependencies=[Depends(require_admin)])
async def arm(body: ArmRequest, rt: RuntimeDep) -> dict[str, object]:
    if rt.settings.mode not in {TradingMode.CANARY, TradingMode.LIVE}:
        raise HTTPException(status.HTTP_409_CONFLICT, "paper/shadow runtimes cannot be live-armed")
    if body.mode is not rt.settings.mode:
        raise HTTPException(status.HTTP_409_CONFLICT, "requested mode does not match executor mode")
    if not rt.settings.uses_supabase:
        raise HTTPException(status.HTTP_409_CONFLICT, "durable Supabase control is required")
    previous = await rt.store.get_runtime_control(rt.settings.account_id)
    if previous.cancellation_pending:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "cannot arm until the signer worker verifies zero open orders",
        )
    saved = await rt.store.arm_runtime_control(
        rt.settings.account_id,
        body.mode,
        utc_now() + timedelta(minutes=body.minutes),
        previous.version,
    )
    if saved is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "runtime control changed concurrently; refresh status before arming",
        )
    return saved.model_dump(mode="json")


@app.post("/v1/control/disarm", dependencies=[Depends(require_admin)])
async def disarm(rt: RuntimeDep) -> dict[str, object]:
    saved = await rt.store.disarm_runtime_control(
        rt.settings.account_id,
        rt.settings.mode,
    )
    try:
        cancellation_verified = await rt.broker.cancel_all("manual disarm")
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "kill switch is set, but exchange cancellation could not be verified; "
            f"operator action required ({type(exc).__name__})",
        ) from exc
    if cancellation_verified and saved.cancellation_pending:
        try:
            unresolved = await rt.store.has_unresolved_live_orders(rt.settings.account_id)
            acknowledged = None
            if not unresolved:
                acknowledged = await rt.store.acknowledge_runtime_cancellation(
                    rt.settings.account_id, saved.version
                )
        except Exception as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "orders are cancelled, but the durable cancellation acknowledgement "
                "failed; worker retry is required",
            ) from exc
        if acknowledged is not None:
            saved = acknowledged
    return {
        **saved.model_dump(mode="json"),
        "cancellation_verified": cancellation_verified,
        "cancellation_pending_worker": saved.cancellation_pending,
    }
