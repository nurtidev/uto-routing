"""
Fleet State Service — manages current vehicle availability and positions.

Loads data from the three Wialon snapshots and computes:
  - start_node: nearest road graph node to current vehicle position
  - avg_speed_kmh: derived from Δdistance / Δtime across snapshots
  - free_at_minutes: minutes until vehicle is free (0 if available now)
  - skills: compatible task types (from compatibility dictionary)

NOTE: This module is a placeholder with the full interface defined.
      The console Claude agent implements the heavy data-loading logic.
      Only the interface (VehicleInfo, FleetState, get_fleet_state) is
      required by the API layer.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ── Singleton fleet state (refreshed at startup and on demand) ────
_fleet_state: Optional["FleetState"] = None


@dataclass
class VehicleInfo:
    wialon_id: int
    name: str
    registration_plate: str
    start_node: int                          # Current position snapped to graph node
    avg_speed_kmh: float = 40.0             # Derived from snapshots
    free_at_minutes: float = 0.0            # 0 = free now; positive = minutes until free
    skills: list[str] = field(default_factory=list)  # Compatible task_types
    pos_lon: float = 0.0
    pos_lat: float = 0.0

    def is_compatible(self, task_type: str | None) -> bool:
        """True if this vehicle can perform the given task type."""
        if not task_type or not self.skills:
            return True  # No constraint — compatible by default
        return task_type in self.skills


@dataclass
class FleetState:
    vehicles: list[VehicleInfo] = field(default_factory=list)

    @property
    def vehicle_count(self) -> int:
        return len(self.vehicles)

    def get_available_vehicles(
        self,
        task_type: str | None = None,
    ) -> list[VehicleInfo]:
        """
        Return all vehicles compatible with the given task type.
        Vehicles are returned sorted by free_at_minutes ascending
        (most available first).
        """
        result = [
            v for v in self.vehicles
            if v.is_compatible(task_type) and v.start_node is not None
        ]
        result.sort(key=lambda v: v.free_at_minutes)
        return result


async def get_fleet_state(
    db: AsyncSession,
    force_reload: bool = False,
) -> FleetState:
    """
    Returns the cached FleetState, loading from DB if not yet initialised
    or force_reload is True.
    """
    global _fleet_state

    if _fleet_state is not None and not force_reload:
        return _fleet_state

    logger.info("Loading fleet state from Wialon snapshots…")
    _fleet_state = await _load_fleet_from_db(db)
    logger.info("Fleet state loaded: %d vehicles", _fleet_state.vehicle_count)
    return _fleet_state


async def _load_fleet_from_db(db: AsyncSession) -> FleetState:
    """
    Load and compute vehicle state from the three Wialon snapshot tables.

    Steps:
      1. Fetch all rows from wialon_units_snapshot_1/2/3.
      2. For each wialon_id, take pos from the latest snapshot (highest pos_t).
      3. Compute avg_speed from snapshots 1→3 if positions differ.
      4. Snap (pos_x, pos_y) to the nearest road graph node via GraphService.
      5. Load free_at_minutes from the active task plan.
      6. Load skills from the compatibility dictionary.
    """
    from sqlalchemy import text
    from app.core.graph_service import get_graph_service

    graph_svc = get_graph_service()

    # ── 1. Fetch latest snapshot per vehicle ──────────────────────
    query = text("""
        WITH all_snaps AS (
            SELECT wialon_id, nm, registration_plate, pos_t, pos_x, pos_y, 1 AS snap
            FROM references.wialon_units_snapshot_1
            UNION ALL
            SELECT wialon_id, nm, registration_plate, pos_t, pos_x, pos_y, 2
            FROM references.wialon_units_snapshot_2
            UNION ALL
            SELECT wialon_id, nm, registration_plate, pos_t, pos_x, pos_y, 3
            FROM references.wialon_units_snapshot_3
        ),
        latest AS (
            SELECT DISTINCT ON (wialon_id)
                wialon_id, nm, registration_plate, pos_x, pos_y
            FROM all_snaps
            ORDER BY wialon_id, pos_t DESC
        ),
        speed_calc AS (
            -- avg_speed from snapshots 1 and 3 (Δdist / Δtime)
            SELECT
                s1.wialon_id,
                CASE
                    WHEN s3.pos_t > s1.pos_t AND s3.pos_t != s1.pos_t THEN
                        -- Haversine approximation in metres / minutes → km/h
                        (
                            111320.0 * SQRT(
                                POW(s3.pos_y - s1.pos_y, 2) +
                                POW((s3.pos_x - s1.pos_x) * COS(RADIANS((s1.pos_y + s3.pos_y)/2)), 2)
                            )
                        ) / NULLIF(s3.pos_t - s1.pos_t, 0) * 3.6
                    ELSE NULL
                END AS avg_speed_kmh
            FROM references.wialon_units_snapshot_1 s1
            LEFT JOIN references.wialon_units_snapshot_3 s3 USING (wialon_id)
        )
        SELECT
            l.wialon_id,
            l.nm,
            l.registration_plate,
            l.pos_x,
            l.pos_y,
            sc.avg_speed_kmh
        FROM latest l
        LEFT JOIN speed_calc sc USING (wialon_id)
        ORDER BY l.wialon_id
    """)

    result = await db.execute(query)
    rows = result.mappings().all()

    # ── 2. Load active task assignments for free_at_minutes ───────
    busy_map = await _load_busy_map(db)

    # ── 3. Build VehicleInfo objects ──────────────────────────────
    vehicles: list[VehicleInfo] = []
    for row in rows:
        wialon_id = row["wialon_id"]
        pos_lon = float(row["pos_x"] or 0)
        pos_lat = float(row["pos_y"] or 0)

        # Snap to graph node
        start_node = None
        if graph_svc and pos_lon and pos_lat:
            start_node = graph_svc.snap_to_node(pos_lon, pos_lat)

        if start_node is None:
            logger.warning("Vehicle %s could not be snapped to graph, skipping", wialon_id)
            continue

        raw_speed = row.get("avg_speed_kmh")
        avg_speed = float(raw_speed) if raw_speed and not math.isnan(float(raw_speed)) else 40.0
        # Sanity-check: speed must be between 5 and 120 km/h
        avg_speed = max(5.0, min(120.0, avg_speed))

        free_at = busy_map.get(wialon_id, 0.0)

        from app.core.compatibility import get_vehicle_skills
        vehicle_name = row["nm"] or f"Vehicle {wialon_id}"
        vehicles.append(
            VehicleInfo(
                wialon_id=wialon_id,
                name=vehicle_name,
                registration_plate=row.get("registration_plate") or "",
                start_node=start_node,
                avg_speed_kmh=avg_speed,
                free_at_minutes=free_at,
                skills=get_vehicle_skills(vehicle_name),
                pos_lon=pos_lon,
                pos_lat=pos_lat,
            )
        )

    return FleetState(vehicles=vehicles)


async def _load_busy_map(db: AsyncSession) -> dict[int, float]:
    """
    Returns {wialon_id: free_at_minutes} for currently assigned vehicles.
    A vehicle is busy until the end of its active task.
    Placeholder: returns empty dict until task assignment table is populated.
    """
    # TODO: join with task assignment table when available
    return {}
