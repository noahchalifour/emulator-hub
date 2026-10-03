"""Two ASGI apps sharing one LeaseEngine, served by two uvicorn servers in one
process. See auth.py for why they are separate listeners."""

import asyncio
import contextlib
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from emulator_hub import metrics
from emulator_hub.api import build_api
from emulator_hub.auth import bearer_ok, ui_user
from emulator_hub.emulator_grpc import GrpcBootProbe, GrpcScreen
from emulator_hub.leases import EngineConfig, LeaseEngine
from emulator_hub.liveview import build_liveview
from emulator_hub.mcp_server import build_mcp
from emulator_hub.models import SLOT_FREE
from emulator_hub.pods import KubePods
from emulator_hub.settings import Settings
from emulator_hub.store import Store

log = logging.getLogger("emulator_hub")
UI_DIR = Path(__file__).parent / "ui"


def create_ui_app(engine: LeaseEngine, screen_factory=GrpcScreen) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def require_authentik(request: Request, call_next):
        if request.url.path != "/healthz" and ui_user(request.headers) is None:
            return JSONResponse({"detail": "sign in through authentik"}, status_code=401)
        return await call_next(request)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/me")
    async def me(request: Request):
        return {"user": ui_user(request.headers)}

    app.include_router(build_api(engine))
    app.include_router(build_liveview(engine, screen_factory))
    app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
    return app


def create_machine_app(engine: LeaseEngine, api_token: str, reap_interval_s: float = 15) -> FastAPI:
    mcp = build_mcp(engine)
    mcp_app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        # Host checks are the Ingress's job; this port is otherwise reachable
        # only through NetworkPolicy-restricted in-cluster Service names.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def reaper():
        while True:
            try:
                for _, reason in await engine.reap():
                    metrics.LEASES_ENDED.labels(reason).inc()
            except Exception:
                log.exception("reap failed")
            await asyncio.sleep(reap_interval_s)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await engine.reconcile()
        task = asyncio.create_task(reaper())
        async with mcp.session_manager.run():
            yield
        task.cancel()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def require_bearer(request: Request, call_next):
        if request.url.path not in ("/healthz", "/metrics") and not bearer_ok(request.headers, api_token):
            return JSONResponse({"detail": "missing or invalid bearer token"}, status_code=401)
        return await call_next(request)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/metrics")
    async def prom():
        metrics.SLOTS_IN_USE.set(sum(1 for s in engine.store.list_slots() if s.state != SLOT_FREE))
        metrics.QUEUE_DEPTH.set(engine.queue_depth())
        while engine.boot_seconds:
            metrics.BOOT_SECONDS.observe(engine.boot_seconds.pop())
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # mcp_app routes its own /mcp path; appending its routes keeps it at /mcp.
    app.router.routes.extend(mcp_app.routes)
    return app


async def serve(settings: Settings) -> None:
    engine = LeaseEngine(
        Store(settings.db_path, slot_count=len(settings.slot_ip_list)),
        KubePods(settings.namespace),
        GrpcBootProbe(),
        EngineConfig(
            namespace=settings.namespace,
            emulator_image=settings.emulator_image,
            slot_ips=settings.slot_ip_list,
            boot_timeout_s=settings.boot_timeout_s,
            max_age_s=settings.max_age_s,
        ),
    )
    common = dict(host="0.0.0.0", proxy_headers=True, forwarded_allow_ips="*", log_level="info")
    ui = uvicorn.Server(uvicorn.Config(create_ui_app(engine), port=settings.ui_port, **common))
    machine = uvicorn.Server(
        uvicorn.Config(
            create_machine_app(engine, settings.api_token, settings.reap_interval_s),
            port=settings.machine_port,
            **common,
        )
    )
    # The machine app's lifespan runs reconcile() before either port answers
    # traffic that depends on it: the UI server starts second.
    machine_task = asyncio.create_task(machine.serve())
    while not machine.started:
        if machine_task.done():
            await machine_task  # surface a startup failure
        await asyncio.sleep(0.05)
    await asyncio.gather(machine_task, ui.serve())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(serve(Settings()))
