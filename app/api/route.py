"""
POST /api/route

Builds a shortest-path route between a vehicle's current location and a
destination well, returning the full node list, coordinates (polyline),
distance in km, and estimated travel time.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from app.models.requests import RouteRequest
from app.models.responses import ErrorResponse, RouteResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["route"])


@router.post(
    "/route",
    response_model=RouteResponse,
    responses={
        404: {"model": ErrorResponse, "description": "No path found between given points"},
        503: {"model": ErrorResponse, "description": "Graph not loaded yet"},
    },
    summary="Build road-graph route between two points",
    description=(
        "Computes the shortest path through the oilfield road graph between "
        "a source point (vehicle position) and destination (well coordinates). "
        "Returns the ordered node list, polyline coordinates, total distance in km, "
        "and estimated travel time in minutes based on vehicle avg speed."
    ),
)
async def build_route(body: RouteRequest) -> RouteResponse:
    from app.core.graph_service import get_graph_service

    graph_svc = get_graph_service()
    if graph_svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Graph service not initialised. Try again in a moment.",
        )

    # 1. Snap source and destination coordinates to nearest graph nodes
    from_lon, from_lat = body.from_point.lon, body.from_point.lat
    to_lon, to_lat = body.to_point.lon, body.to_point.lat

    source_node = graph_svc.snap_to_node(from_lon, from_lat)
    target_node = graph_svc.snap_to_node(to_lon, to_lat)

    if source_node is None or target_node is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Could not snap one or both coordinates to the road graph.",
        )

    # 2. Find shortest path
    path_info = graph_svc.shortest_path(source_node, target_node)
    if path_info is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No path found between node {source_node} and node {target_node}.",
        )

    nodes, distance_m, coords = path_info

    # 3. Estimate travel time using default speed (vehicle-specific speed TBD)
    from app.config import get_settings
    settings = get_settings()
    avg_speed_ms = settings.default_avg_speed_kmh * 1000 / 60  # km/h → m/min
    time_minutes = distance_m / avg_speed_ms if avg_speed_ms > 0 else 0.0

    return RouteResponse(
        distance_km=round(distance_m / 1000, 3),
        time_minutes=round(time_minutes, 1),
        nodes=nodes,
        coords=coords,
    )
