"""
optimizer.py — OR-Tools VRPTW Batch Solver
==========================================

Implements the core optimization engine using Google OR-Tools to solve
the Vehicle Routing Problem with Time Windows (VRPTW).

Modes:
  - solve_batch(vehicles, tasks, dist_matrix, time_matrix) → BatchSolution
      Finds the globally optimal assignment of tasks to vehicles,
      including multi-stop routes.

Problem formulation:
  - Multi-Depot: each vehicle starts from its current position (no shared depot)
  - Open-end routes: vehicles stay at last task location (no return to depot)
  - Time Windows: each task has [tw_start, tw_end] in minutes from horizon start
  - Compatibility: only compatible vehicle types can serve a task (hard constraint)
  - Priority penalties: soft time window violations, weighted by task priority

Reference:
  https://developers.google.com/optimization/routing/vrptw
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# OR-Tools import guard — not available in all environments
try:
    from ortools.constraint_solver import routing_enums_pb2
    from ortools.constraint_solver import pywrapcp
    _ORTOOLS_AVAILABLE = True
except ImportError:
    _ORTOOLS_AVAILABLE = False
    logger.warning("OR-Tools not installed — batch optimizer unavailable. "
                   "Install with: pip install ortools")


# ─────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────

@dataclass
class VehicleInput:
    vehicle_id: int           # wialon_id
    start_node_idx: int       # index in the global location list
    free_at_minutes: float    # minutes from horizon start when vehicle is available
    avg_speed_kmh: float
    skills: list[str]         # compatible task types


@dataclass
class TaskInput:
    task_id: str
    node_idx: int             # index in the global location list
    tw_start: int             # minutes from horizon start (inclusive)
    tw_end: int               # minutes from horizon start (inclusive)
    service_minutes: int      # planned_duration_hours * 60
    priority: str             # low / medium / high
    task_type: str | None
    penalty: int              # soft TW violation penalty (derived from priority)


@dataclass
class RouteStep:
    task_id: str
    node_idx: int
    arrival_minutes: float
    departure_minutes: float


@dataclass
class VehicleRoute:
    vehicle_id: int
    steps: list[RouteStep] = field(default_factory=list)
    total_distance_m: float = 0.0
    total_time_minutes: float = 0.0


@dataclass
class BatchSolution:
    routes: list[VehicleRoute] = field(default_factory=list)
    unassigned_tasks: list[str] = field(default_factory=list)
    total_distance_km: float = 0.0
    solver_status: str = "unknown"   # "optimal", "feasible", "infeasible", "timeout"
    objective_value: float = 0.0


# ─────────────────────────────────────────────
# Priority → penalty mapping
# ─────────────────────────────────────────────

PRIORITY_PENALTY = {
    "high":   100_000,
    "medium":  50_000,
    "low":     10_000,
}

SPEED_M_PER_MIN = {
    # default speeds for travel-time conversion (m/min)
    # actual avg_speed_kmh from VehicleInput overrides this
}


# ─────────────────────────────────────────────
# Main solver function
# ─────────────────────────────────────────────

def solve_batch(
    vehicles: list[VehicleInput],
    tasks: list[TaskInput],
    dist_matrix: list[list[float]],   # metres, shape (N_locations × N_locations)
    time_matrix: list[list[float]],   # minutes, shape (N_locations × N_locations)
    time_limit_seconds: int = 30,
) -> BatchSolution:
    """
    Run OR-Tools VRPTW to find the optimal assignment of tasks to vehicles.

    Args:
        vehicles:          List of vehicle descriptors (start position, speed, etc.)
        tasks:             List of task descriptors (node, time window, service time)
        dist_matrix:       Pairwise distance matrix in metres (indexed by node_idx)
        time_matrix:       Pairwise travel-time matrix in minutes
        time_limit_seconds: OR-Tools search time limit

    Returns:
        BatchSolution with per-vehicle routes and list of unassigned tasks.
    """
    if not _ORTOOLS_AVAILABLE:
        logger.error("OR-Tools not available — returning empty solution.")
        return BatchSolution(
            unassigned_tasks=[t.task_id for t in tasks],
            solver_status="error_no_ortools",
        )

    if not vehicles or not tasks:
        return BatchSolution(solver_status="empty_input")

    n_vehicles = len(vehicles)
    n_tasks = len(tasks)
    n_locations = len(dist_matrix)

    logger.info("OR-Tools VRPTW: %d vehicles, %d tasks, %d locations",
                n_vehicles, n_tasks, n_locations)

    # ── Open-end routing: add a virtual dummy depot ───────────────
    # Vehicles end at the dummy depot (index = n_locations) at zero cost.
    # This removes the mandatory return-to-origin constraint, which would
    # otherwise inflate arc costs beyond the drop penalty for disconnected nodes.
    dummy_depot_idx = n_locations
    n_locations_total = n_locations + 1

    # Extend dist/time matrices with a zero row+column for the dummy depot
    ext_dist = [row + [0] for row in dist_matrix] + [[0] * n_locations_total]
    ext_time = [row + [0] for row in time_matrix] + [[0] * n_locations_total]

    # ── Build OR-Tools data model ──────────────────────────────────
    data = _build_data_model(vehicles, tasks, dist_matrix, time_matrix)
    starts = data["starts"]
    ends   = [dummy_depot_idx] * n_vehicles   # all vehicles end at dummy depot

    # ── Create Routing Index Manager ──────────────────────────────
    manager = pywrapcp.RoutingIndexManager(
        n_locations_total,
        n_vehicles,
        starts,
        ends,
    )
    routing = pywrapcp.RoutingModel(manager)

    # ── Distance callback (uses extended matrix) ───────────────────
    def distance_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return int(ext_dist[from_node][to_node])

    transit_callback_index = routing.RegisterTransitCallback(distance_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    # ── Time callback (uses extended matrix) ───────────────────────
    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        travel = int(ext_time[from_node][to_node])
        # Add service time at the origin node (if it's a task node)
        service = data["service_times"].get(from_node, 0)
        return travel + service

    time_callback_index = routing.RegisterTransitCallback(time_callback)

    # ── Time dimension (for time windows) ─────────────────────────
    max_time = 24 * 60 * 7  # 1 week in minutes
    routing.AddDimension(
        time_callback_index,
        slack_max=max_time,     # waiting time allowed
        capacity=max_time,
        fix_start_cumul_to_zero=False,
        name="Time",
    )
    time_dimension = routing.GetDimensionOrDie("Time")

    # ── Vehicle start time constraints ─────────────────────────────
    for v_idx, vehicle in enumerate(vehicles):
        start_index = routing.Start(v_idx)
        free_at = int(vehicle.free_at_minutes)
        time_dimension.CumulVar(start_index).SetMin(free_at)

    # ── Task time windows (soft constraints with penalties) ────────
    for t_idx, task in enumerate(tasks):
        node_index = manager.NodeToIndex(task.node_idx)
        tw_start = int(task.tw_start)
        tw_end = int(task.tw_end)
        penalty = PRIORITY_PENALTY.get(task.priority, 10_000)

        time_dimension.SetCumulVarSoftUpperBound(node_index, tw_end, penalty)
        time_dimension.CumulVar(node_index).SetMin(tw_start)

    # ── Compatibility: allowed vehicles per task ───────────────────
    for t_idx, task in enumerate(tasks):
        if task.task_type:
            allowed = [
                v_idx for v_idx, v in enumerate(vehicles)
                if not v.skills or task.task_type in v.skills
            ]
            if allowed and len(allowed) < n_vehicles:
                node_index = manager.NodeToIndex(task.node_idx)
                routing.SetAllowedVehiclesForIndex(allowed, node_index)

    # ── Penalty for dropping tasks (soft — prefer to serve all) ───
    for t_idx, task in enumerate(tasks):
        node_index = manager.NodeToIndex(task.node_idx)
        drop_penalty = PRIORITY_PENALTY.get(task.priority, 10_000) * 10
        routing.AddDisjunction([node_index], drop_penalty)

    # ── Search parameters ──────────────────────────────────────────
    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    search_params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    search_params.time_limit.seconds = time_limit_seconds
    search_params.log_search = False

    # ── Solve ──────────────────────────────────────────────────────
    solution = routing.SolveWithParameters(search_params)

    if solution is None:
        logger.warning("OR-Tools returned no solution.")
        return BatchSolution(
            unassigned_tasks=[t.task_id for t in tasks],
            solver_status="infeasible",
        )

    # ── Extract solution ───────────────────────────────────────────
    return _extract_solution(
        solution, routing, manager, vehicles, tasks, dist_matrix, time_dimension
    )


# ─────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────

def _build_data_model(
    vehicles: list[VehicleInput],
    tasks: list[TaskInput],
    dist_matrix: list[list[float]],
    time_matrix: list[list[float]],
) -> dict[str, Any]:
    """Build the OR-Tools data dictionary."""
    n_locations = len(dist_matrix)

    # Start and end nodes per vehicle
    # Open-end: vehicle ends at its last task (same index as start won't force return)
    # We use a virtual depot at the last location slot if needed
    starts = [v.start_node_idx for v in vehicles]
    ends = [v.start_node_idx for v in vehicles]  # open-end: ends = starts (ignored)

    # Service times per location index
    service_times: dict[int, int] = {}
    for task in tasks:
        service_times[task.node_idx] = task.service_minutes

    return {
        "starts": starts,
        "ends": ends,
        "service_times": service_times,
        "n_locations": n_locations,
    }


def _extract_solution(
    solution,
    routing,
    manager,
    vehicles: list[VehicleInput],
    tasks: list[TaskInput],
    dist_matrix: list[list[float]],
    time_dimension,
) -> BatchSolution:
    """Parse OR-Tools solution into BatchSolution."""
    # Build node_idx → task mapping
    node_to_task: dict[int, TaskInput] = {t.node_idx: t for t in tasks}
    served_task_ids: set[str] = set()
    routes: list[VehicleRoute] = []
    total_distance_m = 0.0

    for v_idx, vehicle in enumerate(vehicles):
        index = routing.Start(v_idx)
        steps: list[RouteStep] = []
        route_dist = 0.0
        prev_index = None

        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            task = node_to_task.get(node)

            time_var = time_dimension.CumulVar(index)
            arrival = solution.Min(time_var)
            departure = arrival + (task.service_minutes if task else 0)

            if task:
                steps.append(RouteStep(
                    task_id=task.task_id,
                    node_idx=node,
                    arrival_minutes=arrival,
                    departure_minutes=departure,
                ))
                served_task_ids.add(task.task_id)

            if prev_index is not None:
                prev_node = manager.IndexToNode(prev_index)
                route_dist += dist_matrix[prev_node][node]

            prev_index = index
            index = solution.Value(routing.NextVar(index))

        total_distance_m += route_dist
        if steps:
            routes.append(VehicleRoute(
                vehicle_id=vehicle.vehicle_id,
                steps=steps,
                total_distance_m=route_dist,
                total_time_minutes=(
                    steps[-1].departure_minutes - steps[0].arrival_minutes
                    if steps else 0.0
                ),
            ))

    unassigned = [t.task_id for t in tasks if t.task_id not in served_task_ids]

    from ortools.constraint_solver import routing_enums_pb2 as _enums
    _ss = _enums.RoutingSearchStatus
    status_map = {
        _ss.ROUTING_SUCCESS: "optimal",
        _ss.ROUTING_FAIL: "infeasible",
        _ss.ROUTING_FAIL_TIMEOUT: "timeout",
        _ss.ROUTING_INVALID: "invalid",
        _ss.ROUTING_NOT_SOLVED: "not_solved",
    }
    solver_status = status_map.get(routing.status(), "unknown")

    logger.info(
        "OR-Tools solution: status=%s, routes=%d, unassigned=%d, dist=%.1f km",
        solver_status, len(routes), len(unassigned), total_distance_m / 1000,
    )

    return BatchSolution(
        routes=routes,
        unassigned_tasks=unassigned,
        total_distance_km=round(total_distance_m / 1000, 2),
        solver_status=solver_status,
        objective_value=float(solution.ObjectiveValue()),
    )


# ─────────────────────────────────────────────
# Greedy baseline (for comparison)
# ─────────────────────────────────────────────

def solve_greedy_baseline(
    vehicles: list[VehicleInput],
    tasks: list[TaskInput],
    dist_matrix: list[list[float]],
    time_matrix: list[list[float]],
) -> BatchSolution:
    """
    Naive greedy baseline: assign each task to the nearest available vehicle.
    Used for comparison in demo Scenario 2.
    """
    import copy

    vehicle_free_at = {v.vehicle_id: v.free_at_minutes for v in vehicles}
    vehicle_current_node = {v.vehicle_id: v.start_node_idx for v in vehicles}
    routes_map: dict[int, VehicleRoute] = {
        v.vehicle_id: VehicleRoute(vehicle_id=v.vehicle_id) for v in vehicles
    }

    # Sort tasks by priority then planned start
    priority_order = {"high": 0, "medium": 1, "low": 2}
    sorted_tasks = sorted(tasks, key=lambda t: priority_order.get(t.priority, 1))

    total_dist = 0.0
    unassigned = []

    for task in sorted_tasks:
        best_vehicle = None
        best_eta = math.inf

        for v in vehicles:
            if task.task_type and v.skills and task.task_type not in v.skills:
                continue  # incompatible
            current_node = vehicle_current_node[v.vehicle_id]
            travel_m = dist_matrix[current_node][task.node_idx]
            travel_min = time_matrix[current_node][task.node_idx]
            eta = vehicle_free_at[v.vehicle_id] + travel_min
            if eta < best_eta:
                best_eta = eta
                best_vehicle = v
                best_travel_m = travel_m
                best_travel_min = travel_min

        if best_vehicle is None:
            unassigned.append(task.task_id)
            continue

        arrival = vehicle_free_at[best_vehicle.vehicle_id] + best_travel_min
        departure = arrival + task.service_minutes

        routes_map[best_vehicle.vehicle_id].steps.append(RouteStep(
            task_id=task.task_id,
            node_idx=task.node_idx,
            arrival_minutes=arrival,
            departure_minutes=departure,
        ))
        routes_map[best_vehicle.vehicle_id].total_distance_m += best_travel_m
        total_dist += best_travel_m

        vehicle_free_at[best_vehicle.vehicle_id] = departure
        vehicle_current_node[best_vehicle.vehicle_id] = task.node_idx

    routes = [r for r in routes_map.values() if r.steps]

    return BatchSolution(
        routes=routes,
        unassigned_tasks=unassigned,
        total_distance_km=round(total_dist / 1000, 2),
        solver_status="greedy_baseline",
    )
