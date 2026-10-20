from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings, get_settings
from .contracts import (
    ActionResult, AuthContext, ChatRequest, ChatResponse, ConfirmActionRequest,
    LeaveRequestView,
)
from .api_errors import problem_response, problem_responses, register_error_handlers
from .monitoring import active_alerts
from .runtime import Runtime, assemble_runtime, model_is_ready
from .telemetry import TraceRecord


bearer = HTTPBearer(auto_error=False)


def current_user(request: Request, credentials: Annotated[
    HTTPAuthorizationCredentials | None, Depends(bearer),
]) -> AuthContext:
    if credentials is None:
        raise HTTPException(status_code=401)
    auth = request.app.state.runtime.repository.authenticate(credentials.credentials)
    if auth is None:
        raise HTTPException(status_code=401)
    return auth


def monitoring_admin(auth: Annotated[AuthContext, Depends(current_user)]) -> AuthContext:
    if auth.role != "monitoring_admin":
        raise HTTPException(status_code=403)
    return auth


CurrentUser = Annotated[AuthContext, Depends(current_user)]
MonitoringAdmin = Annotated[AuthContext, Depends(monitoring_admin)]


def build_runtime(settings: Settings) -> Runtime:
    return assemble_runtime(settings)


def create_app(*, settings: Settings | None = None, repository=None, cache=None, agent=None) -> FastAPI:
    """Build the documented API without changing endpoint or response contracts."""
    settings = settings if settings is not None else get_settings()
    if repository is None and cache is None and agent is None:
        runtime = build_runtime(settings)
    else:
        runtime = assemble_runtime(settings, repository, cache, agent)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            runtime.close()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.runtime = runtime
    app.state.settings = settings
    register_error_handlers(app)
    app.include_router(health_routes())
    app.include_router(application_routes())
    return app


def health_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/readyz", response_model=None, responses=problem_responses(503))
    def ready(request: Request) -> dict[str, Any] | JSONResponse:
        runtime = request.app.state.runtime
        settings = request.app.state.settings
        database_ready = runtime.repository.health()
        model_ready = model_is_ready(settings) and runtime.agent is not None
        if not database_ready or not model_ready:
            missing = []
            if not database_ready:
                missing.append("database")
            if not model_ready:
                missing.append("konfigurasi model")
            return problem_response(503, "Belum siap: " + ", ".join(missing) + ".")
        # Redis is optional: its failure degrades caching, not service readiness.
        redis_ready = runtime.cache.health()
        return {"status": "ready" if redis_ready else "degraded", "checks": {
            "database": "ready", "redis": "ready" if redis_ready else "degraded",
            "model": "ready",
        }}

    return router


def application_routes() -> APIRouter:
    error_responses = problem_responses(401, 403, 404, 409, 422, 429, 500, 503)
    router = APIRouter(prefix="/api/v1", responses=error_responses)

    @router.post("/chat", response_model=ChatResponse)
    def chat(body: ChatRequest, request: Request, auth: CurrentUser) -> ChatResponse:
        runtime = request.app.state.runtime
        if runtime.agent is None:
            raise HTTPException(status_code=503)
        settings = request.app.state.settings
        if settings.public_demo:
            try:
                retry = runtime.repository.reserve_demo_chat(
                    settings.demo_daily_chat_limit, settings.demo_minute_chat_limit,
                )
            except Exception:
                # Fail closed, including an absent migration. Never expose SQL/secrets.
                raise HTTPException(status_code=503) from None
            if retry:
                raise HTTPException(status_code=429, headers={"Retry-After": str(retry)})
        return runtime.agent.run(body, auth)

    @router.post("/action-proposals/{proposal_id}/confirm", response_model=ActionResult)
    def confirm(proposal_id: str, body: ConfirmActionRequest,
                request: Request, auth: CurrentUser) -> ActionResult:
        try:
            return request.app.state.runtime.repository.confirm_proposal(
                auth.employee_id, proposal_id, body.idempotency_key,
            )
        except KeyError:
            # Missing and foreign proposals have the same response. Ownership,
            # expiry, replay, and insertion remain inside the repository transaction.
            raise HTTPException(status_code=404) from None

    @router.get("/leave-requests/me", response_model=list[LeaveRequestView])
    def my_requests(request: Request, auth: CurrentUser) -> list[LeaveRequestView]:
        return request.app.state.runtime.repository.list_leave_requests(auth.employee_id)

    @router.get("/traces/{trace_id}", response_model=TraceRecord)
    def trace(trace_id: str, request: Request, auth: CurrentUser) -> dict[str, Any]:
        record = request.app.state.runtime.repository.get_trace(trace_id)
        if record is None:
            raise HTTPException(status_code=404)
        if record["employee_id"] != auth.employee_id and auth.role != "monitoring_admin":
            raise HTTPException(status_code=403)
        return record

    @router.get("/monitoring/summary")
    def summary(request: Request, auth: MonitoringAdmin) -> dict[str, Any]:
        return request.app.state.runtime.repository.monitoring_summary()

    @router.get("/monitoring/alerts")
    def alerts(request: Request, auth: MonitoringAdmin) -> list[dict[str, Any]]:
        metrics = request.app.state.runtime.repository.monitoring_summary()
        return active_alerts(metrics)

    return router

