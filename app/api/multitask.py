"""
POST /api/multitask

Evaluates a list of tasks and returns the optimal grouping strategy:
- Which tasks can share a single vehicle trip (multi-stop)
- Which tasks should be served separately
- Distance and time savings vs single-task baseline
"""
from __future__ import annotations

import itertools
import logging
import math

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.requests import MultitaskRequest
from app.models.responses import ErrorResponse, MultitaskResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["multitask"])


@router.post(
    "/multitask",
    response_model=MultitaskResponse,
    responses={
        404: {"model": ErrorResponse, "description": "One or more task IDs not found"},
        503: {"model": ErrorResponse, "description": "Graph not loaded yet"},
    },
    summary="Evaluate multi-stop task grouping",
    description=(
        "Given a list of task IDs and routing constraints (max detour ratio, "
        "max total time), determines which tasks are worth combining into a "
        "single multi-stop vehicle trip vs serving separately. "
        "Returns the optimal grouping with distance/time savings vs baseline."
    ),
)
async def multitask(
    body: MultitaskRequest,
    db: AsyncSession = Depends(get_db),
) -> MultitaskResponse:
    from app.core.graph_service import get_graph_service
    from app.core.multitask_solver import solve_grouping

    graph_svc = get_graph_service()
    if graph_svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Graph service not initialised. Try again in a moment.",
        )

    # 1. Load task details from dcm.records (real orders)
    from app.core.orders import get_orders_as_tasks
    task_details = await get_orders_as_tasks(db, body.task_ids)
    missing = set(body.task_ids) - {t["task_id"] for t in task_details}
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Orders not found: {', '.join(sorted(missing))}",
        )

    # 2. Resolve node and coordinates for each task's destination well
    for task in task_details:
        node = await graph_svc.resolve_well_node(db, task["destination_uwi"])
        task["node"] = node
        coords = graph_svc.get_cached_well_coords(task["destination_uwi"])
        if coords:
            task["well_lon"] = coords[0]
            task["well_lat"] = coords[1]

    # 3. Compute pairwise distance matrix between task nodes
    task_nodes = [t["node"] for t in task_details]
    dist_matrix = graph_svc.pairwise_distance_matrix(task_nodes)

    # 4. Run grouping solver
    result = solve_grouping(
        tasks=task_details,
        dist_matrix=dist_matrix,
        max_detour_ratio=body.constraints.max_detour_ratio,
        max_total_time_minutes=body.constraints.max_total_time_minutes,
    )

    # 5. Attach well coordinates for map rendering
    task_coords: dict[str, list[float]] = {}
    for task in task_details:
        lon = task.get("well_lon")
        lat = task.get("well_lat")
        if lon is not None and lat is not None:
            task_coords[task["task_id"]] = [float(lon), float(lat)]

    return result.model_copy(update={"task_coords": task_coords})


async def _load_tasks(db: AsyncSession, task_ids: list[str]) -> list[dict]:
    """Load task records from the DB by task_id list."""
    from sqlalchemy import text

    placeholders = ", ".join(f":id_{i}" for i in range(len(task_ids)))
    params = {f"id_{i}": tid for i, tid in enumerate(task_ids)}

    query = text(f"""
        SELECT
            t.task_id,
            t.priority,
            t.planned_start,
            t.planned_duration_hours,
            t.destination_uwi,
            t.task_type,
            t.shift,
            t.start_day,
            w.latitude  AS well_lat,
            w.longitude AS well_lon
        FROM tasks t
        LEFT JOIN "references".wells w ON w.uwi = t.destination_uwi
        WHERE t.task_id IN ({placeholders})
    """)

    result = await db.execute(query, params)
    rows = result.mappings().all()
    return [dict(r) for r in rows]
