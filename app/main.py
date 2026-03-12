import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.graph_service import init_graph_service, get_graph_service
from app.db import AsyncSessionLocal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: pre-load road graph and fleet state into memory."""
    # ── Load road graph ────────────────────────────────────────────
    logger.info("🚀 Service starting — loading road graph …")
    try:
        async with AsyncSessionLocal() as session:
            svc = await init_graph_service(session)
        logger.info(
            "✅ Road graph loaded: %d nodes, %d edges",
            svc.node_count,
            svc.edge_count,
        )
    except Exception as exc:
        logger.error("❌ Failed to load graph: %s", exc, exc_info=True)

    # ── Pre-load fleet state ───────────────────────────────────────
    try:
        from app.core.fleet_state import get_fleet_state
        async with AsyncSessionLocal() as session:
            fleet = await get_fleet_state(session, force_reload=True)
        logger.info("✅ Fleet state loaded: %d vehicles", len(fleet.vehicles))
    except Exception as exc:
        logger.error("❌ Failed to load fleet state: %s", exc, exc_info=True)

    logger.info("🟢 Service ready.")
    yield
    logger.info("🛑 Service shutting down.")


app = FastAPI(
    title="ИС УТО — Интеллектуальная система маршрутизации спецтехники",
    description=(
        "Backend service for optimal routing of special-purpose vehicles "
        "across oilfield road networks. Implements VRP with time windows (VRPTW).\n\n"
        "**Endpoints:**\n"
        "- `POST /api/recommendations` — Top-3 vehicle candidates for a task\n"
        "- `POST /api/route` — Shortest road-graph route between two points\n"
        "- `POST /api/multitask` — Optimal multi-stop task grouping\n"
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# ── CORS ───────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ────────────────────────────────────────────────────────
from app.api import recommendations, route, multitask, batch, fleet  # noqa: E402

app.include_router(recommendations.router)
app.include_router(route.router)
app.include_router(multitask.router)
app.include_router(batch.router)
app.include_router(fleet.router)

# ── Frontend (static) ──────────────────────────────────────────────
app.mount("/frontend", StaticFiles(directory="frontend"), name="frontend")


# ── Health check ───────────────────────────────────────────────────
@app.get("/health", tags=["system"], summary="Service health check")
async def health():
    graph_svc = get_graph_service()
    graph_ok = graph_svc is not None
    return JSONResponse(
        status_code=200 if graph_ok else 503,
        content={
            "status": "ok" if graph_ok else "degraded",
            "graph_loaded": graph_ok,
            "graph_nodes": graph_svc.node_count if graph_ok else 0,
            "graph_edges": graph_svc.edge_count if graph_ok else 0,
        },
    )


@app.get("/", tags=["system"], include_in_schema=False)
async def root():
    return FileResponse("frontend/index.html")
