"""
Scoring module — computes composite score for vehicle candidates.

Score formula (weights add up to 1.0):
  score = 0.35 * (1 - norm_distance)
        + 0.30 * (1 - norm_eta)
        + 0.20 * availability_bonus
        + 0.15 * priority_factor

Where:
  norm_distance   — distance normalised to [0, 1] across all candidates
  norm_eta        — ETA normalised to [0, 1] across all candidates
  availability_bonus — 1.0 if free now, 0 < x < 1 if waiting, decays with wait time
  priority_factor — based on task priority: high=1.0, medium=0.64, low=0.18
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.fleet_state import VehicleInfo

# Score weights — must sum to 1.0
W_DISTANCE = 0.35
W_ETA = 0.30
W_AVAILABILITY = 0.20
W_PRIORITY = 0.15

PRIORITY_FACTOR: dict[str, float] = {
    "high": 1.0,
    "medium": 0.64,
    "low": 0.18,
}

# SLA deadline offsets (hours)
SLA_DEADLINE_HOURS: dict[str, float] = {
    "high": 2.0,
    "medium": 5.0,
    "low": 12.0,
}


def score_candidates(
    candidates: list["VehicleInfo"],
    distances: dict[int, float],          # node_id → distance_m (float('inf') if unreachable)
    task_priority: str,
    planned_start: datetime,
    task_type: str | None = None,
) -> list[tuple["VehicleInfo", dict]]:
    """
    Rank candidates by composite score (descending).

    Returns list of (vehicle, score_info_dict) tuples.
    """
    from app.config import get_settings
    settings = get_settings()

    scored = []
    for vehicle in candidates:
        dist_m = distances.get(vehicle.start_node, math.inf)
        if math.isinf(dist_m):
            # Unreachable — still include with worst score
            dist_m = 1_000_000.0

        # Convert distance → time using vehicle avg speed (m/min)
        speed_m_per_min = (vehicle.avg_speed_kmh or settings.default_avg_speed_kmh) * 1000 / 60
        travel_minutes = dist_m / speed_m_per_min if speed_m_per_min > 0 else math.inf

        # ETA = travel time + wait if vehicle is currently busy
        eta_minutes = travel_minutes + max(0.0, vehicle.free_at_minutes)

        # Availability bonus: 1.0 if available now, decays exponentially with wait
        wait = max(0.0, vehicle.free_at_minutes)
        availability_bonus = math.exp(-wait / 120.0)  # half-value at 120 min wait

        # Compatibility check (task_type, not priority)
        compatible = vehicle.is_compatible(task_type)

        scored.append(
            (
                vehicle,
                {
                    "distance_km": dist_m / 1000.0,
                    "eta_minutes": eta_minutes,
                    "availability_bonus": availability_bonus,
                    "compatible": compatible,
                    "score": 0.0,   # filled below after normalisation
                },
            )
        )

    if not scored:
        return []

    # Normalise distance and ETA across candidates (min-max)
    all_distances = [s["distance_km"] for _, s in scored]
    all_etas = [s["eta_minutes"] for _, s in scored]

    min_d, max_d = min(all_distances), max(all_distances)
    min_e, max_e = min(all_etas), max(all_etas)
    pf = PRIORITY_FACTOR.get(task_priority, 0.5)

    for _, info in scored:
        norm_d = _safe_norm(info["distance_km"], min_d, max_d)
        norm_e = _safe_norm(info["eta_minutes"], min_e, max_e)

        info["score"] = (
            W_DISTANCE * (1.0 - norm_d)
            + W_ETA * (1.0 - norm_e)
            + W_AVAILABILITY * info["availability_bonus"]
            + W_PRIORITY * pf
        )

    # Sort descending by score
    scored.sort(key=lambda x: x[1]["score"], reverse=True)
    return scored


def build_reason(vehicle: "VehicleInfo", score_info: dict, priority: str) -> str:
    """Generate a concise human-readable explanation for the recommendation."""
    parts: list[str] = []

    if score_info["compatible"]:
        parts.append("совместима по типу работ")

    wait = vehicle.free_at_minutes
    if wait <= 0:
        parts.append("свободна прямо сейчас")
    elif wait < 60:
        parts.append(f"занята, освободится через {int(wait)} мин")
    else:
        parts.append(f"занята, освободится через {wait/60:.1f} ч")

    dist = score_info["distance_km"]
    if dist < 5:
        parts.append(f"очень близко ({dist:.1f} км)")
    elif dist < 20:
        parts.append(f"расстояние {dist:.1f} км по дорогам")
    else:
        parts.append(f"расстояние {dist:.1f} км (отдалённая)")

    eta = score_info["eta_minutes"]
    deadline_h = SLA_DEADLINE_HOURS.get(priority, 12.0)
    deadline_min = deadline_h * 60
    if eta <= deadline_min * 0.5:
        parts.append(f"укладывается в SLA с запасом (ETA {eta:.0f} мин)")
    elif eta <= deadline_min:
        parts.append(f"укладывается в SLA (ETA {eta:.0f} мин)")
    else:
        parts.append(f"⚠️ превышает SLA {priority} приоритета (ETA {eta:.0f} мин)")

    return "; ".join(parts).capitalize() + "."


def _safe_norm(val: float, min_val: float, max_val: float) -> float:
    """Normalise value to [0, 1]. Returns 0.5 if all values are equal."""
    spread = max_val - min_val
    if spread < 1e-9:
        return 0.0
    return (val - min_val) / spread
