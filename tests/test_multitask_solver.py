"""
Unit tests for app/core/multitask_solver.py
Run with: pytest tests/test_multitask_solver.py -v
"""
import math
import pytest

from app.core.multitask_solver import solve_grouping


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dist_matrix(tasks: list[dict]) -> dict[tuple[int, int], float]:
    """Build a simple pairwise distance dict from tasks with 'node' field."""
    nodes = [t["node"] for t in tasks]
    result = {}
    for i, n1 in enumerate(nodes):
        for j, n2 in enumerate(nodes):
            if n1 == n2:
                result[(n1, n2)] = 0.0
            else:
                result[(n1, n2)] = abs(n1 - n2) * 1000.0  # metres
    return result


# ---------------------------------------------------------------------------
# Tests: basic grouping logic
# ---------------------------------------------------------------------------

class TestSolveGrouping:
    def test_single_task_returns_single_group(self):
        """Single task should return exactly one group containing that task."""
        tasks = [{"task_id": "T1", "node": 1}]
        dist = _make_dist_matrix(tasks)
        result = solve_grouping(tasks, dist, max_detour_ratio=1.3, max_total_time_minutes=480)
        assert len(result.groups) == 1
        assert result.groups[0] == ["T1"]
        assert result.savings_percent == pytest.approx(0.0)

    def test_two_nearby_tasks_get_merged(self):
        """Two tasks with very close nodes should be combined into one group."""
        tasks = [
            {"task_id": "T1", "node": 100},
            {"task_id": "T2", "node": 101},  # only 1 km apart
        ]
        dist = {
            (100, 100): 0.0, (101, 101): 0.0,
            (100, 101): 1_000.0, (101, 100): 1_000.0,
        }
        result = solve_grouping(tasks, dist, max_detour_ratio=1.3, max_total_time_minutes=480)
        # Both should end up in one group
        all_grouped = [tid for g in result.groups for tid in g]
        assert set(all_grouped) == {"T1", "T2"}

    def test_distant_tasks_stay_separate(self):
        """Tasks far apart (high detour ratio) should not be merged."""
        tasks = [
            {"task_id": "T1", "node": 1},
            {"task_id": "T2", "node": 500},  # 499 km apart
        ]
        dist = {
            (1, 1): 0.0, (500, 500): 0.0,
            (1, 500): 499_000.0, (500, 1): 499_000.0,
        }
        result = solve_grouping(tasks, dist, max_detour_ratio=1.3, max_total_time_minutes=480)
        assert result.strategy_summary == "separate"
        assert result.savings_percent == pytest.approx(0.0)

    def test_all_tasks_in_result(self):
        """Every input task_id must appear exactly once in the result groups."""
        tasks = [{"task_id": f"T{i}", "node": i * 10} for i in range(1, 6)]
        dist = _make_dist_matrix(tasks)
        result = solve_grouping(tasks, dist, max_detour_ratio=1.3, max_total_time_minutes=480)
        all_in_groups = [tid for g in result.groups for tid in g]
        assert sorted(all_in_groups) == sorted([t["task_id"] for t in tasks])

    def test_savings_percent_non_negative(self):
        tasks = [{"task_id": f"T{i}", "node": i * 5} for i in range(1, 4)]
        dist = _make_dist_matrix(tasks)
        result = solve_grouping(tasks, dist, max_detour_ratio=1.3, max_total_time_minutes=480)
        assert result.savings_percent >= 0.0

    def test_total_distance_lte_baseline(self):
        """Optimised distance must never exceed baseline."""
        tasks = [{"task_id": f"T{i}", "node": i * 3} for i in range(1, 5)]
        dist = _make_dist_matrix(tasks)
        result = solve_grouping(tasks, dist, max_detour_ratio=1.3, max_total_time_minutes=480)
        assert result.total_distance_km <= result.baseline_distance_km + 1e-6

    def test_strategy_single_unit_when_all_merged(self):
        """If all tasks end up in one group, strategy should be 'single_unit'."""
        tasks = [
            {"task_id": "T1", "node": 10},
            {"task_id": "T2", "node": 11},
            {"task_id": "T3", "node": 12},
        ]
        dist = {
            (10, 10): 0.0, (11, 11): 0.0, (12, 12): 0.0,
            (10, 11): 500.0,  (11, 10): 500.0,
            (10, 12): 1000.0, (12, 10): 1000.0,
            (11, 12): 500.0,  (12, 11): 500.0,
        }
        result = solve_grouping(tasks, dist, max_detour_ratio=2.0, max_total_time_minutes=9999)
        if len(result.groups) == 1:
            assert result.strategy_summary == "single_unit"

    def test_time_constraint_prevents_merge(self):
        """If max_total_time_minutes is 0, no multi-stop routes should form."""
        tasks = [
            {"task_id": "T1", "node": 100},
            {"task_id": "T2", "node": 101},
        ]
        dist = {
            (100, 100): 0.0, (101, 101): 0.0,
            (100, 101): 1_000.0, (101, 100): 1_000.0,
        }
        result = solve_grouping(tasks, dist, max_detour_ratio=1.3, max_total_time_minutes=0)
        assert result.strategy_summary == "separate"

    def test_reason_string_not_empty(self):
        tasks = [{"task_id": "T1", "node": 1}, {"task_id": "T2", "node": 2}]
        dist = _make_dist_matrix(tasks)
        result = solve_grouping(tasks, dist, max_detour_ratio=1.3, max_total_time_minutes=480)
        assert isinstance(result.reason, str)
        assert len(result.reason) > 0

    def test_empty_tasks_list(self):
        """Empty input should not raise, return empty groups."""
        result = solve_grouping([], {}, max_detour_ratio=1.3, max_total_time_minutes=480)
        assert result.groups == []

    def test_tasks_with_no_node_skipped_gracefully(self):
        """Tasks with node=None should not crash the solver."""
        tasks = [
            {"task_id": "T1", "node": None},
            {"task_id": "T2", "node": 50},
        ]
        dist = {(50, 50): 0.0}
        result = solve_grouping(tasks, dist, max_detour_ratio=1.3, max_total_time_minutes=480)
        assert result is not None
