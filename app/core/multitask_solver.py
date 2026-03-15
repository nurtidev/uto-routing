"""
Multi-stop task grouping solver.

Algorithm:
1. Compute pairwise distances between all task nodes.
2. Use a greedy cluster-merge approach:
   - Start with each task in its own group.
   - Greedily merge the pair of groups whose combined detour ratio
     is within max_detour_ratio, maximising distance savings.
3. Repeat until no beneficial merges remain.
4. Compute savings vs single-task baseline.
"""
from __future__ import annotations

import itertools
import math

from app.models.responses import MultitaskResponse


def solve_grouping(
    tasks: list[dict],
    dist_matrix: dict[tuple[int, int], float],
    max_detour_ratio: float,
    max_total_time_minutes: float,
) -> MultitaskResponse:
    """
    Returns optimal task grouping and savings metrics.
    """
    from app.config import get_settings
    settings = get_settings()
    speed_kmh = settings.default_avg_speed_kmh

    task_ids = [t["task_id"] for t in tasks]
    node_map = {t["task_id"]: t.get("node") for t in tasks}
    all_nodes = [n for n in node_map.values() if n is not None]

    # ── Baseline: each task served separately ──────────────────────
    baseline_dist_km, baseline_time_min = _baseline_metrics(
        tasks, node_map, dist_matrix, speed_kmh
    )

    # ── Greedy grouping ────────────────────────────────────────────
    # Each group is a list of task_ids
    groups: list[list[str]] = [[tid] for tid in task_ids]

    improved = True
    while improved:
        improved = False
        best_saving = 0.0
        best_merge: tuple[int, int] | None = None

        for i, j in itertools.combinations(range(len(groups)), 2):
            merged = groups[i] + groups[j]
            dist_merged, time_merged = _group_tsp_approx(
                merged, node_map, dist_matrix, speed_kmh, all_nodes
            )
            dist_separate = _sum_single_distances(
                merged, node_map, dist_matrix, speed_kmh
            )[0]

            if dist_separate < 1e-6:
                continue

            detour = dist_merged / dist_separate if dist_separate > 0 else math.inf
            saving = dist_separate - dist_merged

            if (
                detour <= max_detour_ratio
                and time_merged <= max_total_time_minutes
                and saving > best_saving
            ):
                best_saving = saving
                best_merge = (i, j)

        if best_merge:
            i, j = best_merge
            new_group = groups[i] + groups[j]
            # Remove in reverse order to keep indices valid
            for idx in sorted([i, j], reverse=True):
                groups.pop(idx)
            groups.append(new_group)
            improved = True

    # ── Total distance/time for the chosen grouping ────────────────
    all_separate = all(len(g) == 1 for g in groups)
    if all_separate:
        total_dist = baseline_dist_km
        total_time = baseline_time_min
        savings_pct = 0.0
    else:
        total_dist = 0.0
        total_time = 0.0
        for g in groups:
            d, t = _group_tsp_approx(g, node_map, dist_matrix, speed_kmh, all_nodes)
            total_dist += d
            total_time += t
        savings_pct = 0.0
        if baseline_dist_km > 0:
            savings_pct = max(0.0, (baseline_dist_km - total_dist) / baseline_dist_km * 100)

    # ── Strategy summary ───────────────────────────────────────────
    if len(groups) == 1:
        strategy = "single_unit"
    elif any(len(g) > 1 for g in groups):
        strategy = "mixed"
    else:
        strategy = "separate"

    reason = _build_grouping_reason(groups, node_map, dist_matrix, baseline_dist_km, total_dist)

    return MultitaskResponse(
        groups=groups,
        strategy_summary=strategy,
        total_distance_km=round(total_dist, 2),
        total_time_minutes=round(total_time, 1),
        baseline_distance_km=round(baseline_dist_km, 2),
        baseline_time_minutes=round(baseline_time_min, 1),
        savings_percent=round(savings_pct, 1),
        reason=reason,
    )


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _get_dist(n1: int | None, n2: int | None, dist_matrix: dict) -> float:
    if n1 is None or n2 is None or n1 == n2:
        return 0.0
    return dist_matrix.get((n1, n2), dist_matrix.get((n2, n1), math.inf))


def _baseline_metrics(tasks, node_map, dist_matrix, speed_kmh) -> tuple[float, float]:
    """
    Baseline: each task served by a separate vehicle.

    Approximation: for each task, cost = min distance to any other task node
    (represents the vehicle travelling from the nearest neighbour to reach it).
    This gives a lower-bound estimate of separate-trip total distance, which
    is a fair comparison point for the grouping optimiser.
    """
    ids = [t["task_id"] for t in tasks]
    nodes = [node_map.get(tid) for tid in ids if node_map.get(tid) is not None]

    if len(nodes) <= 1:
        # Single task — baseline and grouped cost are both 0 inter-task km
        return 0.0, 0.0

    # Build a nearest-neighbour chain: start from first node, greedily visit all
    # This approximates "one vehicle visiting all separately" as separate round trips
    # by summing direct edges between consecutive nearest neighbours.
    total_m = 0.0
    visited = [nodes[0]]
    remaining = list(nodes[1:])
    while remaining:
        current = visited[-1]
        nearest = min(remaining, key=lambda n: _get_dist(current, n, dist_matrix))
        total_m += _get_dist(current, nearest, dist_matrix)
        visited.append(nearest)
        remaining.remove(nearest)

    # Each task served separately ≈ each leg driven twice (out and back)
    total_d_km = total_m * 2 / 1000
    speed_m_per_min = speed_kmh * 1000 / 60
    total_t = (total_d_km * 1000 / speed_m_per_min) if speed_m_per_min > 0 else 0.0
    return total_d_km, total_t


def _group_tsp_approx(
    group: list[str],
    node_map: dict,
    dist_matrix: dict,
    speed_kmh: float,
    all_nodes: list[int] | None = None,
) -> tuple[float, float]:
    """
    Nearest-neighbour TSP approximation for a group of tasks.
    Returns (distance_km, time_minutes).

    For a single-task group, returns the round-trip distance to the nearest
    neighbour (from all_nodes if provided, else 0) as a proxy for one vehicle trip.
    """
    nodes = [node_map.get(tid) for tid in group]
    nodes = [n for n in nodes if n is not None]

    if len(nodes) == 0:
        return 0.0, 0.0

    if len(nodes) == 1:
        # Single task: estimate trip as distance to nearest other node × 2
        if all_nodes:
            others = [n for n in all_nodes if n != nodes[0]]
            if others:
                min_d = min(_get_dist(nodes[0], n, dist_matrix) for n in others)
                dist_km = min_d * 2 / 1000
                speed_m_per_min = speed_kmh * 1000 / 60
                time_min = (min_d * 2 / speed_m_per_min) if speed_m_per_min > 0 else 0.0
                return dist_km, time_min
        return 0.0, 0.0

    visited = [nodes[0]]
    remaining = list(nodes[1:])
    total_dist_m = 0.0

    while remaining:
        current = visited[-1]
        nearest = min(remaining, key=lambda n: _get_dist(current, n, dist_matrix))
        total_dist_m += _get_dist(current, nearest, dist_matrix)
        visited.append(nearest)
        remaining.remove(nearest)

    dist_km = total_dist_m / 1000
    speed_m_per_min = speed_kmh * 1000 / 60
    time_min = (total_dist_m / speed_m_per_min) if speed_m_per_min > 0 else 0.0
    return dist_km, time_min


def _sum_single_distances(
    group: list[str],
    node_map: dict,
    dist_matrix: dict,
    speed_kmh: float,
) -> tuple[float, float]:
    """Distance if each task in group is served separately (not combined)."""
    if len(group) <= 1:
        return 0.0, 0.0
    nodes = [node_map.get(tid) for tid in group if node_map.get(tid) is not None]
    if len(nodes) <= 1:
        return 0.0, 0.0

    # Approximate: sum of min distances between each consecutive pair when sorted
    total_m = 0.0
    for i in range(len(nodes) - 1):
        total_m += _get_dist(nodes[i], nodes[i + 1], dist_matrix)

    dist_km = total_m / 1000
    speed_m_per_min = speed_kmh * 1000 / 60
    time_min = (total_m / speed_m_per_min) if speed_m_per_min > 0 else 0.0
    return dist_km, time_min


def _build_grouping_reason(
    groups: list[list[str]],
    node_map: dict,
    dist_matrix: dict,
    baseline_km: float,
    total_km: float,
) -> str:
    saving_km = baseline_km - total_km
    multi_groups = [g for g in groups if len(g) > 1]
    single_groups = [g for g in groups if len(g) == 1]

    parts: list[str] = []

    if not multi_groups:
        parts.append(
            "Объединение заявок не даёт выигрыша в рамках заданных ограничений. "
            "Раздельное обслуживание оптимально."
        )
    else:
        for g in multi_groups:
            parts.append(
                f"Заявки {', '.join(g)} объединены в один выезд — "
                f"близкое расположение точек назначения."
            )
        if single_groups:
            flat = [tid for g in single_groups for tid in g]
            parts.append(
                f"Заявки {', '.join(flat)} обслуживаются отдельно — "
                "территориально удалены или нарушают ограничение крюка."
            )
        if saving_km > 0:
            parts.append(f"Итоговая экономия: {saving_km:.1f} км.")

    return " ".join(parts)
