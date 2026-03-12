"""
Unit tests for app/core/scoring.py
Run with: pytest tests/test_scoring.py -v
"""
import math
from dataclasses import dataclass, field
from datetime import datetime

import pytest

from app.core.scoring import (
    W_DISTANCE,
    W_ETA,
    W_IDLE,
    W_SLA,
    SLA_DEADLINE_HOURS,
    _safe_norm,
    score_candidates,
)


# ---------------------------------------------------------------------------
# Minimal VehicleInfo stub (avoids DB dependency)
# ---------------------------------------------------------------------------

@dataclass
class FakeVehicle:
    wialon_id: int
    name: str
    start_node: int
    avg_speed_kmh: float = 40.0
    free_at_minutes: float = 0.0
    skills: list = field(default_factory=list)
    pos_lon: float = 0.0
    pos_lat: float = 0.0
    registration_plate: str = ""

    def is_compatible(self, task_type):
        if not task_type or not self.skills:
            return True
        return task_type in self.skills


PLANNED = datetime(2025, 2, 20, 8, 0, 0)


# ---------------------------------------------------------------------------
# Tests: _safe_norm
# ---------------------------------------------------------------------------

class TestSafeNorm:
    def test_zero_to_one_range(self):
        assert _safe_norm(0.0, 0.0, 10.0) == pytest.approx(0.0)
        assert _safe_norm(10.0, 0.0, 10.0) == pytest.approx(1.0)
        assert _safe_norm(5.0, 0.0, 10.0) == pytest.approx(0.5)

    def test_all_equal_returns_zero(self):
        # When min == max, there is no spread — all values normalise to 0
        assert _safe_norm(7.0, 7.0, 7.0) == pytest.approx(0.0)

    def test_negative_spread(self):
        # Values can be negative (e.g. minutes already elapsed)
        assert _safe_norm(-5.0, -10.0, 0.0) == pytest.approx(0.5)

    def test_weights_sum_to_one(self):
        total = W_DISTANCE + W_ETA + W_IDLE + W_SLA
        assert total == pytest.approx(1.0), f"Weights sum to {total}, expected 1.0"


# ---------------------------------------------------------------------------
# Tests: score_candidates — correctness
# ---------------------------------------------------------------------------

class TestScoreCandidates:
    def test_empty_candidates_returns_empty(self):
        result = score_candidates([], {}, "high", PLANNED)
        assert result == []

    def test_single_candidate_gets_max_score(self):
        """With one candidate, all norm values are 0, so score = W_sum = 1.0."""
        v = FakeVehicle(wialon_id=1, name="V1", start_node=100, avg_speed_kmh=60.0, free_at_minutes=0)
        distances = {100: 10_000}  # 10 km

        result = score_candidates([v], distances, "high", PLANNED)
        assert len(result) == 1
        _, info = result[0]
        # All norms are 0 (only one candidate, spread=0), so score = 1.0
        assert info["score"] == pytest.approx(1.0)

    def test_closer_vehicle_ranked_higher(self):
        """Closer vehicle should score higher (less distance, less ETA)."""
        near = FakeVehicle(wialon_id=1, name="Near", start_node=10, avg_speed_kmh=60.0, free_at_minutes=0)
        far  = FakeVehicle(wialon_id=2, name="Far",  start_node=20, avg_speed_kmh=60.0, free_at_minutes=0)
        distances = {10: 5_000, 20: 50_000}   # 5 km vs 50 km

        result = score_candidates([near, far], distances, "medium", PLANNED)
        assert result[0][0].wialon_id == 1  # near vehicle ranked first

    def test_free_vehicle_beats_busy_vehicle(self):
        """A free vehicle should score higher than a busy vehicle at same distance."""
        free_v = FakeVehicle(wialon_id=1, name="Free", start_node=10, avg_speed_kmh=40.0, free_at_minutes=0)
        busy_v = FakeVehicle(wialon_id=2, name="Busy", start_node=10, avg_speed_kmh=40.0, free_at_minutes=120)
        distances = {10: 10_000}

        result = score_candidates([free_v, busy_v], distances, "high", PLANNED)
        assert result[0][0].wialon_id == 1  # free vehicle ranked first

    def test_score_bounded_zero_to_one(self):
        """Score must always be in [0.0, 1.0]."""
        vehicles = [
            FakeVehicle(wialon_id=i, name=f"V{i}", start_node=i * 10,
                        avg_speed_kmh=40.0, free_at_minutes=float(i * 30))
            for i in range(1, 6)
        ]
        distances = {v.start_node: (v.wialon_id * 15_000) for v in vehicles}

        result = score_candidates(vehicles, distances, "low", PLANNED)
        for _, info in result:
            assert 0.0 <= info["score"] <= 1.0, f"Score out of range: {info['score']}"

    def test_sorted_descending_by_score(self):
        """Result must be sorted highest score first."""
        vehicles = [
            FakeVehicle(wialon_id=i, name=f"V{i}", start_node=i * 10, free_at_minutes=float(i * 60))
            for i in range(1, 5)
        ]
        distances = {v.start_node: v.wialon_id * 20_000 for v in vehicles}

        result = score_candidates(vehicles, distances, "medium", PLANNED)
        scores = [info["score"] for _, info in result]
        assert scores == sorted(scores, reverse=True), "Scores not sorted descending"

    def test_sla_penalty_zero_when_within_deadline(self):
        """If ETA < deadline, sla_penalty must be 0."""
        # high priority deadline = 2h = 120 min
        # ETA = 10 km / 60 km/h = 10 min → well within 120 min
        v = FakeVehicle(wialon_id=1, name="V1", start_node=10, avg_speed_kmh=60.0, free_at_minutes=0)
        distances = {10: 10_000}

        result = score_candidates([v], distances, "high", PLANNED)
        _, info = result[0]
        assert info["sla_penalty"] == pytest.approx(0.0)

    def test_sla_penalty_nonzero_when_over_deadline(self):
        """If ETA > deadline, sla_penalty must be > 0."""
        # high priority deadline = 120 min
        # ETA = 200 km / 40 km/h = 300 min → over 120 min
        v = FakeVehicle(wialon_id=1, name="V1", start_node=10, avg_speed_kmh=40.0, free_at_minutes=0)
        distances = {10: 200_000}

        result = score_candidates([v], distances, "high", PLANNED)
        _, info = result[0]
        assert info["sla_penalty"] > 0.0

    def test_unreachable_vehicle_gets_worst_distance(self):
        """Vehicle with inf distance should get distance_km = 1000 and still appear in results."""
        v = FakeVehicle(wialon_id=1, name="V1", start_node=10)
        distances = {10: math.inf}

        result = score_candidates([v], distances, "medium", PLANNED)
        assert len(result) == 1
        _, info = result[0]
        assert info["distance_km"] == pytest.approx(1000.0)   # 1_000_000 m / 1000

    def test_eta_includes_idle_time(self):
        """ETA = travel_time + free_at_minutes (idle)."""
        v = FakeVehicle(wialon_id=1, name="V1", start_node=10, avg_speed_kmh=60.0, free_at_minutes=30.0)
        distances = {10: 60_000}  # 60 km / 60 km/h = 60 min travel

        result = score_candidates([v], distances, "low", PLANNED)
        _, info = result[0]
        # travel = 60 min, idle = 30 min → eta = 90 min
        assert info["eta_minutes"] == pytest.approx(90.0)
        assert info["idle_minutes"] == pytest.approx(30.0)

    def test_compatible_flag_set_correctly(self):
        """is_compatible is forwarded into score_info correctly."""
        v_compatible   = FakeVehicle(wialon_id=1, name="V1", start_node=10, skills=["drilling"])
        v_incompatible = FakeVehicle(wialon_id=2, name="V2", start_node=20, skills=["cementing"])
        distances = {10: 10_000, 20: 10_000}

        result = score_candidates([v_compatible, v_incompatible], distances, "medium", PLANNED, task_type="drilling")
        info_map = {v.wialon_id: info["compatible"] for v, info in result}
        assert info_map[1] is True
        assert info_map[2] is False

    def test_high_priority_tighter_sla_than_low(self):
        """High priority deadline (2h) is much tighter than low (12h)."""
        deadline_high = SLA_DEADLINE_HOURS["high"] * 60   # 120 min
        deadline_low  = SLA_DEADLINE_HOURS["low"] * 60    # 720 min
        assert deadline_high < deadline_low

        # ETA = 200 min → over high, within low
        v = FakeVehicle(wialon_id=1, name="V1", start_node=10, avg_speed_kmh=60.0)
        distances = {10: 200_000}  # 200 km / 60 km/h = 200 min

        result_high = score_candidates([v], distances, "high", PLANNED)
        result_low  = score_candidates([v], distances, "low",  PLANNED)
        _, info_high = result_high[0]
        _, info_low  = result_low[0]
        assert info_high["sla_penalty"] > info_low["sla_penalty"]

    def test_distance_km_conversion(self):
        """distance_km in score_info = dist_m / 1000."""
        v = FakeVehicle(wialon_id=1, name="V1", start_node=10)
        distances = {10: 37_500}   # 37.5 km

        result = score_candidates([v], distances, "medium", PLANNED)
        _, info = result[0]
        assert info["distance_km"] == pytest.approx(37.5)
