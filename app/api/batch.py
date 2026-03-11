"""
POST /api/batch

Runs the OR-Tools VRPTW batch optimizer over a set of tasks for a planning horizon.
Returns per-vehicle routes with task assignments, distances, and timing.
Also returns a greedy-baseline solution for comparison (Scenario 2 demo).
"""
from __future__ import annotations

import logging
import math
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.responses import ErrorResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["batch"])


# ── Request / Response schemas ─────────────────────────────────────────────

class BatchRequest(BaseModel):
    task_ids: list[str] = Field(
        ..., min_length=1,
        description="List of task IDs to plan. Must exist in the tasks table.",
        example=["T-2025-0042", "T-2025-0043", "T-2025-0044"],
    )
    horizon_start: datetime = Field(
        default_factory=lambda: datetime.now().replace(hour=0, minute=0, second=0, microsecond=0),
        description="Start of planning horizon (minutes are counted from this moment).",
        example="2025-02-20T00:00:00",
    )
    time_limit_seconds: int = Field(
        default=30, ge=5, le=300,
        description="OR-Tools solver time limit.",
    )
    use_greedy_baseline: bool = Field(
        default=True,
        description="Also compute greedy baseline for comparison.",
    )


class RouteStepOut(BaseModel):
    task_id: str
    arrival_minutes: float
    departure_minutes: float


class VehicleRouteOut(BaseModel):
    wialon_id: int
    vehicle_name: str
    steps: list[RouteStepOut]
    total_distance_km: float
    total_time_minutes: float


class BatchResponse(BaseModel):
    solver_status: str
    objective_value: float
    total_distance_km: float
    routes: list[VehicleRouteOut]
    unassigned_tasks: list[str]
    # Greedy baseline for comparison
    baseline_status: str | None = None
    baseline_distance_km: float | None = None
    baseline_routes: list[VehicleRouteOut] | None = None
    savings_percent: float | None = None


# ── Endpoint ───────────────────────────────────────────────────────────────

@router.post(
    "/batch",
    response_model=BatchResponse,
    responses={
        404: {"model": ErrorResponse, "description": "One or more tasks not found"},
        503: {"model": ErrorResponse, "description": "Graph or fleet not loaded"},
    },
    summary="Batch VRPTW optimizer — assign tasks to vehicles",
    description=(
        "Runs OR-Tools VRPTW to find the globally optimal assignment of a set of "
        "tasks to the available vehicle fleet. Returns per-vehicle multi-stop routes "
        "with arrival/departure times. Optionally returns a greedy baseline solution "
        "for comparison (useful for Scenario 2 demo)."
    ),
)
async def batch_optimize(
    body: BatchRequest,
    db: AsyncSession = Depends(get_db),
) -> BatchResponse:
    from app.core.graph_service import get_graph_service
    from app.core.fleet_state import get_fleet_state
    from app.core.optimizer import (
        VehicleInput, TaskInput, solve_batch, solve_greedy_baseline,
    )

    # ── 1. Check services ──────────────────────────────────────────
    graph_svc = get_graph_service()
    if graph_svc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Graph service not ready.")

    fleet = await get_fleet_state(db)
    if not fleet.vehicles:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Fleet state is empty.")

    # ── 2. Load task rows from DB ──────────────────────────────────
    from app.core.orders import get_orders_as_tasks
    task_rows = await get_orders_as_tasks(db, body.task_ids)
    found_ids = {r["task_id"] for r in task_rows}
    missing = set(body.task_ids) - found_ids
    if missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail=f"Orders not found: {sorted(missing)}")

    # ── 3. Build location index ─────────────────────────────────────
    # All unique nodes = vehicle start nodes + task destination nodes
    vehicle_nodes: list[int] = [v.start_node for v in fleet.vehicles]

    task_nodes: list[int | None] = []
    skipped_task_ids: set[str] = set()
    for row in task_rows:
        node = await graph_svc.resolve_well_node(db, row["destination_uwi"])
        if node is None:
            logger.warning("Task %s: well %s has no coordinates — skipping",
                           row["task_id"], row["destination_uwi"])
            skipped_task_ids.add(row["task_id"])
        task_nodes.append(node)

    # Filter out tasks with unresolvable wells
    task_rows = [r for r, n in zip(task_rows, task_nodes) if n is not None]
    task_nodes = [n for n in task_nodes if n is not None]

    all_nodes = _dedup_ordered(vehicle_nodes + task_nodes)
    node_to_idx = {n: i for i, n in enumerate(all_nodes)}
    n_locs = len(all_nodes)

    # ── 4. Distance & time matrices ─────────────────────────────────
    dist_dict = graph_svc.batch_shortest_distances(all_nodes, all_nodes)
    default_speed = 40.0  # km/h fallback

    dist_matrix = _make_matrix(all_nodes, dist_dict)
    time_matrix = [
        [dist_matrix[i][j] / ((default_speed * 1000 / 60) or 1)
         for j in range(n_locs)]
        for i in range(n_locs)
    ]

    # ── 5. Prepare VehicleInput list ────────────────────────────────
    vehicles_in: list[VehicleInput] = [
        VehicleInput(
            vehicle_id=v.wialon_id,
            start_node_idx=node_to_idx[v.start_node],
            free_at_minutes=v.free_at_minutes,
            avg_speed_kmh=v.avg_speed_kmh,
            skills=list(v.skills),
        )
        for v in fleet.vehicles
    ]

    # ── 6. Prepare TaskInput list ───────────────────────────────────
    horizon_start = body.horizon_start
    tasks_in: list[TaskInput] = []
    for row, node in zip(task_rows, task_nodes):
        tw_start, tw_end = _time_window(row, horizon_start)
        tasks_in.append(TaskInput(
            task_id=row["task_id"],
            node_idx=node_to_idx[node],
            tw_start=tw_start,
            tw_end=tw_end,
            service_minutes=int(float(row["planned_duration_hours"]) * 60),
            priority=row["priority"],
            task_type=row.get("task_type"),
            penalty=_priority_penalty(row["priority"]),
        ))

    # ── 7. Run optimizer ───────────────────────────────────────────
    solution = solve_batch(
        vehicles_in, tasks_in, dist_matrix, time_matrix,
        time_limit_seconds=body.time_limit_seconds,
    )

    vehicle_name_map = {v.wialon_id: v.name for v in fleet.vehicles}
    response = _build_response(solution, vehicle_name_map)
    # Prepend skipped (no-coord) tasks to unassigned list
    if skipped_task_ids:
        response.unassigned_tasks = sorted(skipped_task_ids) + response.unassigned_tasks

    # ── 8. Greedy baseline for comparison ──────────────────────────
    if body.use_greedy_baseline:
        baseline = solve_greedy_baseline(vehicles_in, tasks_in, dist_matrix, time_matrix)
        baseline_routes = _build_routes_out(baseline, vehicle_name_map)
        response.baseline_status = baseline.solver_status
        response.baseline_distance_km = baseline.total_distance_km
        response.baseline_routes = baseline_routes
        opt_km = solution.total_distance_km
        base_km = baseline.total_distance_km
        if base_km > 0:
            response.savings_percent = round((base_km - opt_km) / base_km * 100, 1)

    return response


# ── GET /api/orders — list available order IDs ─────────────────────────────

@router.get(
    "/orders",
    summary="List available order IDs from dcm.records",
    description="Returns all active order numbers (e.g. 'G000002') that have a resolvable well and can be used in /api/batch or /api/multitask.",
)
async def list_orders(db: AsyncSession = Depends(get_db)) -> list[dict]:
    from app.core.orders import get_orders_as_tasks
    tasks = await get_orders_as_tasks(db, order_ids=None)
    return [
        {
            "task_id": t["task_id"],
            "destination_uwi": t["destination_uwi"],
            "priority": t["priority"],
            "task_type": t["task_type"],
            "planned_start": t["planned_start"].isoformat() if t.get("planned_start") else None,
            "planned_duration_hours": t["planned_duration_hours"],
        }
        for t in tasks
    ]


# ── Legacy helpers ──────────────────────────────────────────────────────────

async def _fetch_tasks(db: AsyncSession, task_ids: list[str]) -> list[dict]:
    placeholders = ", ".join(f":id_{i}" for i in range(len(task_ids)))
    params = {f"id_{i}": tid for i, tid in enumerate(task_ids)}
    result = await db.execute(
        text(f"""
            SELECT task_id, priority, planned_start, planned_duration_hours,
                   destination_uwi, task_type, shift, start_day
            FROM tasks WHERE task_id IN ({placeholders})
        """),
        params,
    )
    return [dict(r) for r in result.mappings().all()]


def _time_window(row: dict, horizon_start: datetime) -> tuple[int, int]:
    """Convert task shift + start_day to (tw_start_min, tw_end_min) from horizon_start."""
    from datetime import date, timedelta

    start_day = row.get("start_day")
    if isinstance(start_day, str):
        start_day = date.fromisoformat(start_day)

    shift = (row.get("shift") or "day").lower()
    if shift == "day":
        shift_start_h, shift_end_h = 8, 20
    else:
        shift_start_h, shift_end_h = 20, 32   # 20:00 – 08:00 next day

    if start_day is None:
        # Fall back to planned_start
        planned = row.get("planned_start")
        if planned and isinstance(planned, datetime):
            start_day = planned.date()
        else:
            start_day = horizon_start.date()

    # If the task date is in the past, treat it as today (demo / re-planning scenario)
    if start_day < horizon_start.date():
        start_day = horizon_start.date()

    base = datetime(start_day.year, start_day.month, start_day.day)
    tw_start_abs = base.replace(hour=shift_start_h % 24)
    tw_end_abs   = base.replace(hour=0) + timedelta(hours=shift_end_h)

    delta_start = int((tw_start_abs - horizon_start).total_seconds() / 60)
    delta_end   = int((tw_end_abs   - horizon_start).total_seconds() / 60)

    return max(0, delta_start), max(0, delta_end)


def _priority_penalty(priority: str) -> int:
    return {"high": 100_000, "medium": 50_000, "low": 10_000}.get(priority, 10_000)


def _dedup_ordered(lst: list[int]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for x in lst:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _make_matrix(
    nodes: list[int],
    dist_dict: dict[tuple[int, int], float],
) -> list[list[float]]:
    n = len(nodes)
    m = [[0.0] * n for _ in range(n)]
    for i, ni in enumerate(nodes):
        for j, nj in enumerate(nodes):
            v = dist_dict.get((ni, nj), math.inf)
            m[i][j] = v if not math.isinf(v) else 9_999_999.0
    return m


def _build_response(solution, vehicle_name_map: dict[int, str]) -> BatchResponse:
    return BatchResponse(
        solver_status=solution.solver_status,
        objective_value=solution.objective_value,
        total_distance_km=solution.total_distance_km,
        routes=_build_routes_out(solution, vehicle_name_map),
        unassigned_tasks=solution.unassigned_tasks,
    )


def _build_routes_out(solution, vehicle_name_map: dict[int, str]) -> list[VehicleRouteOut]:
    out = []
    for r in solution.routes:
        out.append(VehicleRouteOut(
            wialon_id=r.vehicle_id,
            vehicle_name=vehicle_name_map.get(r.vehicle_id, f"#{r.vehicle_id}"),
            steps=[RouteStepOut(
                task_id=s.task_id,
                arrival_minutes=s.arrival_minutes,
                departure_minutes=s.departure_minutes,
            ) for s in r.steps],
            total_distance_km=round(r.total_distance_m / 1000, 2),
            total_time_minutes=round(r.total_time_minutes, 1),
        ))
    return out
