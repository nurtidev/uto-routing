"""
Fleet management and system statistics endpoints.

POST /api/fleet/refresh — force-reload vehicle positions from DB
GET  /api/stats         — KPI summary: vehicles, orders, SLA, savings estimate
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["fleet"])


# ── Response models ────────────────────────────────────────────────────────

class FleetRefreshResponse(BaseModel):
    vehicle_count: int
    refreshed_at: str
    message: str


class StatsResponse(BaseModel):
    vehicle_count: int
    free_vehicle_count: int
    busy_vehicle_count: int
    order_count: int
    sla_compliance_pct: float | None   # % high-priority orders within SLA
    # Savings estimate vs nearest-free baseline
    estimated_savings_km: float | None
    estimated_savings_fuel_litres: float | None
    estimated_savings_tenge: float | None
    manual_dispatch_hours_saved: float
    graph_nodes: int
    graph_edges: int
    refreshed_at: str


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post(
    "/fleet/refresh",
    response_model=FleetRefreshResponse,
    summary="Force-reload vehicle GPS positions from Wialon snapshots",
    description=(
        "Re-queries the three Wialon snapshot tables and recomputes vehicle "
        "positions, speeds, and availability. Call this after new snapshots "
        "are loaded into the DB."
    ),
)
async def fleet_refresh(db: AsyncSession = Depends(get_db)) -> FleetRefreshResponse:
    from app.core.fleet_state import get_fleet_state

    fleet = await get_fleet_state(db, force_reload=True)
    now = datetime.now(tz=timezone.utc).isoformat()

    return FleetRefreshResponse(
        vehicle_count=fleet.vehicle_count,
        refreshed_at=now,
        message=f"Fleet reloaded: {fleet.vehicle_count} vehicles available.",
    )


@router.get(
    "/stats",
    response_model=StatsResponse,
    summary="System KPI dashboard statistics",
    description=(
        "Returns key performance indicators: fleet availability, order count, "
        "SLA compliance rate, and estimated km/fuel/cost savings vs manual dispatch."
    ),
)
async def stats(db: AsyncSession = Depends(get_db)) -> StatsResponse:
    from app.core.fleet_state import get_fleet_state
    from app.core.graph_service import get_graph_service
    from app.core.orders import get_orders_as_tasks

    now = datetime.now(tz=timezone.utc).isoformat()

    # ── Fleet ──────────────────────────────────────────────────────
    fleet = await get_fleet_state(db)
    free_count = sum(1 for v in fleet.vehicles if v.free_at_minutes <= 0)
    busy_count = fleet.vehicle_count - free_count

    # ── Graph ──────────────────────────────────────────────────────
    graph_svc = get_graph_service()
    graph_nodes = graph_svc.node_count if graph_svc else 0
    graph_edges = graph_svc.edge_count if graph_svc else 0

    # ── Orders ─────────────────────────────────────────────────────
    try:
        tasks = await get_orders_as_tasks(db)
    except Exception as exc:
        logger.warning("stats: failed to load orders: %s", exc)
        tasks = []

    order_count = len(tasks)

    # ── SLA compliance estimate ────────────────────────────────────
    # For each high-priority order check if any free vehicle can reach it within 2h.
    # We use a lightweight heuristic: assume avg speed 40 km/h, graph diameter ~50km.
    sla_compliance_pct: float | None = None
    if tasks and graph_svc and fleet.vehicle_count > 0:
        from app.core.fleet_state import VehicleInfo
        from app.config import get_settings
        settings = get_settings()

        SLA_MINUTES = {"high": 120, "medium": 300, "low": 720}
        high_tasks = [t for t in tasks if t.get("priority") == "high"]

        if high_tasks:
            compliant = 0
            for task in high_tasks:
                uwi = task.get("destination_uwi")
                if not uwi:
                    continue
                well_node = await graph_svc.resolve_well_node(db, uwi)
                if well_node is None:
                    continue
                # Find best free vehicle ETA
                free_vehicles = [v for v in fleet.vehicles if v.free_at_minutes <= 0]
                if not free_vehicles:
                    free_vehicles = fleet.vehicles  # fallback: use all

                vehicle_nodes = [v.start_node for v in free_vehicles]
                distances = graph_svc.distances_to_node(vehicle_nodes, well_node)

                best_eta = math.inf
                for v in free_vehicles:
                    d = distances.get(v.start_node, math.inf)
                    if math.isinf(d):
                        continue
                    speed = (v.avg_speed_kmh or settings.default_avg_speed_kmh) * 1000 / 60
                    eta = d / speed if speed > 0 else math.inf
                    if eta < best_eta:
                        best_eta = eta

                if best_eta <= SLA_MINUTES["high"]:
                    compliant += 1

            sla_compliance_pct = round(compliant / len(high_tasks) * 100, 1) if high_tasks else None

    # ── Savings estimate ───────────────────────────────────────────
    # Heuristic: OR-Tools typically saves 20-30% vs nearest-free baseline
    # on a mixed fleet with 50+ tasks. We estimate conservatively at 22%.
    # Real number comes from running /api/batch — shown here as projection.
    estimated_savings_km: float | None = None
    estimated_savings_fuel_litres: float | None = None
    estimated_savings_tenge: float | None = None

    if order_count > 0 and graph_svc:
        # Rough average distance per task on this field: ~15 km one-way
        AVG_TASK_DIST_KM = 15.0
        SAVINGS_RATIO = 0.22
        FUEL_LITRES_PER_100KM = 15.0
        FUEL_PRICE_TENGE = 250.0

        baseline_km = order_count * AVG_TASK_DIST_KM
        savings_km = baseline_km * SAVINGS_RATIO
        savings_fuel = savings_km * FUEL_LITRES_PER_100KM / 100
        savings_tenge = savings_fuel * FUEL_PRICE_TENGE

        estimated_savings_km = round(savings_km, 1)
        estimated_savings_fuel_litres = round(savings_fuel, 1)
        estimated_savings_tenge = round(savings_tenge, 0)

    # ── Manual dispatch time saved ─────────────────────────────────
    # Industry benchmark: dispatcher spends ~3 min per manual assignment
    manual_dispatch_hours_saved = round(order_count * 3 / 60, 1)

    return StatsResponse(
        vehicle_count=fleet.vehicle_count,
        free_vehicle_count=free_count,
        busy_vehicle_count=busy_count,
        order_count=order_count,
        sla_compliance_pct=sla_compliance_pct,
        estimated_savings_km=estimated_savings_km,
        estimated_savings_fuel_litres=estimated_savings_fuel_litres,
        estimated_savings_tenge=estimated_savings_tenge,
        manual_dispatch_hours_saved=manual_dispatch_hours_saved,
        graph_nodes=graph_nodes,
        graph_edges=graph_edges,
        refreshed_at=now,
    )
