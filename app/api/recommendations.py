"""
POST /api/recommendations

Returns the top-3 recommended vehicles for a given task, ranked by
composite score (distance, ETA, availability, priority weight).
Also returns a naive baseline (nearest free vehicle) for comparison.
"""
from __future__ import annotations

import logging
import math

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.requests import RecommendationRequest
from app.models.responses import (
    BaselineVehicle,
    ErrorResponse,
    RecommendationResponse,
    VehicleCandidate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["recommendations"])


@router.post(
    "/recommendations",
    response_model=RecommendationResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Well UWI not found"},
        422: {"model": ErrorResponse, "description": "Validation error"},
        503: {"model": ErrorResponse, "description": "Graph not loaded yet"},
    },
    summary="Get top-3 vehicle recommendations for a task",
    description=(
        "Given a task (priority, destination well, planned start time), "
        "returns up to 3 ranked vehicle candidates with ETA, distance, "
        "composite score, and LLM-generated natural-language explanation. "
        "Also includes a naive baseline (nearest free vehicle) for comparison."
    ),
)
async def recommendations(
    body: RecommendationRequest,
    db: AsyncSession = Depends(get_db),
) -> RecommendationResponse:
    from app.core.graph_service import get_graph_service
    from app.core.fleet_state import get_fleet_state
    from app.core.scoring import score_candidates
    from app.core.llm_reason import generate_reason

    # 1. Resolve well coordinates → graph node
    graph_svc = get_graph_service()
    if graph_svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Graph service not initialised. Try again in a moment.",
        )

    well_node = await graph_svc.resolve_well_node(db, body.destination_uwi)
    if well_node is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Well with UWI '{body.destination_uwi}' not found or has no coordinates.",
        )
    well_coords = graph_svc.get_cached_well_coords(body.destination_uwi)
    well_lon = well_coords[0] if well_coords else 0.0
    well_lat = well_coords[1] if well_coords else 0.0

    # 2. Get current fleet state
    fleet = await get_fleet_state(db)

    # 3. Filter compatible vehicles (by task_type if provided)
    candidates = fleet.get_available_vehicles(task_type=body.task_type)
    if not candidates:
        logger.warning("No available vehicles found for task %s", body.task_id)
        return RecommendationResponse(task_id=body.task_id, units=[], well_lon=well_lon, well_lat=well_lat)

    # 4. Compute distances from each vehicle to the destination node
    vehicle_nodes = [v.start_node for v in candidates]
    distances = graph_svc.distances_to_node(vehicle_nodes, well_node)

    # 5. Naive baseline — nearest free vehicle (ignoring score)
    baseline = _compute_baseline(candidates, distances)

    # 6. Score and rank candidates (our optimised algorithm)
    ranked = score_candidates(
        candidates=candidates,
        distances=distances,
        task_priority=body.priority,
        planned_start=body.planned_start,
        task_type=body.task_type,
    )

    # 7. Build response (top-3) with LLM-generated reasons
    units: list[VehicleCandidate] = []
    for vehicle, score_info in ranked[:3]:
        reason = await generate_reason(
            vehicle_name=vehicle.name,
            score=score_info["score"],
            distance_km=score_info["distance_km"],
            eta_minutes=score_info["eta_minutes"],
            free_at_minutes=vehicle.free_at_minutes,
            compatible=score_info["compatible"],
            task_priority=body.priority,
            task_type=body.task_type,
        )
        units.append(
            VehicleCandidate(
                wialon_id=vehicle.wialon_id,
                name=vehicle.name,
                eta_minutes=round(score_info["eta_minutes"], 1),
                distance_km=round(score_info["distance_km"], 2),
                score=round(score_info["score"], 3),
                free_at_minutes=round(vehicle.free_at_minutes, 1),
                compatible=score_info["compatible"],
                reason=reason,
                pos_lon=vehicle.pos_lon,
                pos_lat=vehicle.pos_lat,
            )
        )

    return RecommendationResponse(
        task_id=body.task_id,
        units=units,
        well_lon=well_lon,
        well_lat=well_lat,
        baseline=baseline,
    )


def _compute_baseline(candidates, distances) -> BaselineVehicle | None:
    """
    Naive baseline: pick the free vehicle with the shortest distance.
    Ignores scoring — pure nearest-neighbour greedy assignment.
    """
    from app.config import get_settings
    settings = get_settings()

    free_candidates = [v for v in candidates if v.free_at_minutes <= 0]
    pool = free_candidates if free_candidates else candidates  # fallback to all if none free

    best = None
    best_dist = math.inf
    for v in pool:
        d = distances.get(v.start_node, math.inf)
        if d < best_dist:
            best_dist = d
            best = v

    if best is None:
        return None

    speed_m_per_min = (best.avg_speed_kmh or settings.default_avg_speed_kmh) * 1000 / 60
    eta = best_dist / speed_m_per_min if speed_m_per_min > 0 else 0.0

    return BaselineVehicle(
        wialon_id=best.wialon_id,
        name=best.name,
        distance_km=round(best_dist / 1000, 2),
        eta_minutes=round(eta, 1),
        approach="nearest_free",
    )
