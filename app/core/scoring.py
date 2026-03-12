"""
Scoring module — computes composite score for vehicle candidates.

Score formula (weights add up to 1.0) — official formula from organiser PPTX slide 7:
  score = 0.30 * (1 - norm_distance)
        + 0.30 * (1 - norm_eta)
        + 0.15 * (1 - norm_idle)
        + 0.25 * (1 - norm_sla_penalty)

Where:
  norm_distance   — distance normalised to [0, 1] across all candidates
  norm_eta        — ETA (travel + wait) normalised to [0, 1] across all candidates
  norm_idle       — vehicle idle wait time normalised to [0, 1] (0 = free now)
  norm_sla_penalty — normalised deadline violation: max(0, eta - deadline) / deadline
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.fleet_state import VehicleInfo

# Score weights — must sum to 1.0
W_DISTANCE = 0.30
W_ETA = 0.30
W_IDLE = 0.15
W_SLA = 0.25

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

    # SLA deadline in minutes for this task priority
    deadline_minutes = SLA_DEADLINE_HOURS.get(task_priority, 12.0) * 60

    scored = []
    for vehicle in candidates:
        dist_m = distances.get(vehicle.start_node, math.inf)
        if math.isinf(dist_m):
            # Unreachable — still include with worst score
            dist_m = 1_000_000.0

        # Convert distance → time using vehicle avg speed (m/min)
        speed_m_per_min = (vehicle.avg_speed_kmh or settings.default_avg_speed_kmh) * 1000 / 60
        travel_minutes = dist_m / speed_m_per_min if speed_m_per_min > 0 else math.inf

        # Idle = time vehicle must wait before it can start (if currently busy)
        idle_minutes = max(0.0, vehicle.free_at_minutes)

        # ETA = travel time + idle wait
        eta_minutes = travel_minutes + idle_minutes

        # SLA penalty: how far ETA overshoots the deadline (0 if on time)
        sla_penalty = max(0.0, eta_minutes - deadline_minutes) / max(deadline_minutes, 1.0)
        sla_penalty = min(1.0, sla_penalty)  # cap at 1.0

        # Compatibility check (task_type, not priority)
        compatible = vehicle.is_compatible(task_type)

        scored.append(
            (
                vehicle,
                {
                    "distance_km": dist_m / 1000.0,
                    "eta_minutes": eta_minutes,
                    "idle_minutes": idle_minutes,
                    "sla_penalty": sla_penalty,
                    "compatible": compatible,
                    "score": 0.0,   # filled below after normalisation
                },
            )
        )

    if not scored:
        return []

    # Normalise all components across candidates (min-max)
    all_distances = [s["distance_km"] for _, s in scored]
    all_etas = [s["eta_minutes"] for _, s in scored]
    all_idles = [s["idle_minutes"] for _, s in scored]
    all_sla = [s["sla_penalty"] for _, s in scored]

    min_d, max_d = min(all_distances), max(all_distances)
    min_e, max_e = min(all_etas), max(all_etas)
    min_i, max_i = min(all_idles), max(all_idles)
    min_s, max_s = min(all_sla), max(all_sla)

    for _, info in scored:
        norm_d = _safe_norm(info["distance_km"], min_d, max_d)
        norm_e = _safe_norm(info["eta_minutes"], min_e, max_e)
        norm_i = _safe_norm(info["idle_minutes"], min_i, max_i)
        norm_s = _safe_norm(info["sla_penalty"], min_s, max_s)

        info["score"] = (
            W_DISTANCE * (1.0 - norm_d)
            + W_ETA * (1.0 - norm_e)
            + W_IDLE * (1.0 - norm_i)
            + W_SLA * (1.0 - norm_s)
        )

    # Sort descending by score
    scored.sort(key=lambda x: x[1]["score"], reverse=True)
    return scored


def build_reason(vehicle: "VehicleInfo", score_info: dict, priority: str) -> str:
    """Generate a concise human-readable explanation for the recommendation."""
    parts: list[str] = []

    if score_info["compatible"]:
        parts.append("совместима по типу работ")

    idle = score_info.get("idle_minutes", vehicle.free_at_minutes)
    if idle <= 0:
        parts.append("свободна прямо сейчас")
    elif idle < 60:
        parts.append(f"занята, освободится через {int(idle)} мин")
    else:
        parts.append(f"занята, освободится через {idle/60:.1f} ч")

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
    """Normalise value to [0, 1]. Returns 0.0 if all values are equal (no spread)."""
    spread = max_val - min_val
    if spread < 1e-9:
        return 0.0
    return (val - min_val) / spread
