"""
Pydantic response schemas for all API endpoints.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────
# POST /api/recommendations  →  RecommendationResponse
# ─────────────────────────────────────────────

class VehicleCandidate(BaseModel):
    wialon_id: int = Field(..., example=10234)
    name: str = Field(..., example="АЦН-12 А045КМ")
    eta_minutes: float = Field(..., example=38.0, description="Estimated time of arrival in minutes")
    distance_km: float = Field(..., example=12.4, description="Route distance in km")
    score: float = Field(..., ge=0.0, le=1.0, example=0.92, description="Composite score (higher = better)")
    free_at_minutes: float = Field(..., example=0.0, description="Minutes until vehicle is free (0 = already free)")
    compatible: bool = Field(..., example=True, description="Is the vehicle compatible with the task type")
    reason: str | None = Field(None, example="Ближайшая свободная, совместима по типу работ")
    pos_lon: float = Field(0.0, example=68.12345, description="Vehicle current longitude")
    pos_lat: float = Field(0.0, example=51.67890, description="Vehicle current latitude")


class BaselineVehicle(BaseModel):
    """Naive nearest-free-vehicle baseline for comparison with optimised result."""
    wialon_id: int = Field(..., example=10234)
    name: str = Field(..., example="АЦН-12 А045КМ")
    distance_km: float = Field(..., example=8.1, description="Straight nearest distance")
    eta_minutes: float = Field(..., example=29.0)
    approach: str = Field("nearest_free", description="Baseline strategy label")


class RecommendationResponse(BaseModel):
    task_id: str
    units: list[VehicleCandidate]
    well_lon: float = Field(0.0, description="Destination well longitude")
    well_lat: float = Field(0.0, description="Destination well latitude")
    baseline: BaselineVehicle | None = Field(
        None,
        description="Naive nearest-free-vehicle result for baseline comparison",
    )


# ─────────────────────────────────────────────
# POST /api/route  →  RouteResponse
# ─────────────────────────────────────────────

class RouteResponse(BaseModel):
    distance_km: float = Field(..., example=12.4)
    time_minutes: float = Field(..., example=38.0)
    nodes: list[int] = Field(..., description="Ordered list of road graph node IDs")
    coords: list[list[float]] = Field(
        ...,
        description="Ordered list of [lon, lat] coordinate pairs for polyline rendering",
        example=[[68.12345, 51.67890], [68.11450, 51.68500], [68.09100, 51.70450]],
    )


# ─────────────────────────────────────────────
# POST /api/multitask  →  MultitaskResponse
# ─────────────────────────────────────────────

class MultitaskResponse(BaseModel):
    groups: list[list[str]] = Field(
        ...,
        description="Task IDs grouped into optimal trips",
        example=[["T-2025-0042", "T-2025-0044"], ["T-2025-0043"]],
    )
    strategy_summary: Literal["single_unit", "mixed", "separate"] = Field(
        ...,
        example="mixed",
        description=(
            "single_unit – all tasks in one trip; "
            "mixed – some grouped, some separate; "
            "separate – no grouping beneficial"
        ),
    )
    total_distance_km: float = Field(..., example=41.2)
    total_time_minutes: float = Field(..., example=195.0)
    baseline_distance_km: float = Field(..., example=56.8, description="Distance if each task served separately")
    baseline_time_minutes: float = Field(..., example=244.0)
    savings_percent: float = Field(..., example=27.5, description="Distance savings vs baseline")
    reason: str | None = Field(None, example="Заявки T-0042 и T-0044 в радиусе 3 км, объединение экономит 15.6 км")
    task_coords: dict[str, list[float]] = Field(
        default_factory=dict,
        description="Task ID → [lon, lat] of destination well",
        example={"T-2025-0042": [68.12345, 51.67890]},
    )


# ─────────────────────────────────────────────
# Generic error response
# ─────────────────────────────────────────────

class ErrorResponse(BaseModel):
    detail: str
    code: str | None = None
