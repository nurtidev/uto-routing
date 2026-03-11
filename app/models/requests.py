"""
Pydantic request schemas for all API endpoints.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────
# POST /api/recommendations
# ─────────────────────────────────────────────

class RecommendationRequest(BaseModel):
    task_id: str = Field(..., example="T-2025-0042", description="Unique task identifier")
    priority: Literal["low", "medium", "high"] = Field(..., example="high")
    destination_uwi: str = Field(..., example="05-1234-567", description="Target well UWI")
    planned_start: datetime = Field(..., example="2025-02-20T08:00:00")
    duration_hours: float = Field(..., gt=0, example=4.5, description="Planned work duration in hours")
    task_type: str | None = Field(None, example="drilling", description="Work type for compatibility check")


# ─────────────────────────────────────────────
# POST /api/route
# ─────────────────────────────────────────────

class RouteFromPoint(BaseModel):
    wialon_id: int | None = Field(None, example=10234, description="Vehicle ID (optional if lon/lat provided)")
    lon: float = Field(..., example=68.12345)
    lat: float = Field(..., example=51.67890)


class RouteToPoint(BaseModel):
    uwi: str | None = Field(None, example="05-1234-567", description="Well UWI (optional if lon/lat provided)")
    lon: float = Field(..., example=68.09100)
    lat: float = Field(..., example=51.70450)


class RouteRequest(BaseModel):
    from_point: RouteFromPoint = Field(..., alias="from")
    to_point: RouteToPoint = Field(..., alias="to")

    model_config = {"populate_by_name": True}


# ─────────────────────────────────────────────
# POST /api/multitask
# ─────────────────────────────────────────────

class MultitaskConstraints(BaseModel):
    max_total_time_minutes: int = Field(480, ge=1, example=480)
    max_detour_ratio: float = Field(1.3, ge=1.0, example=1.3, description="Max allowed route lengthening factor")


class MultitaskRequest(BaseModel):
    task_ids: list[str] = Field(
        ...,
        min_length=2,
        example=["T-2025-0042", "T-2025-0043", "T-2025-0044"],
        description="List of task IDs to evaluate for grouping",
    )
    constraints: MultitaskConstraints = Field(default_factory=MultitaskConstraints)
