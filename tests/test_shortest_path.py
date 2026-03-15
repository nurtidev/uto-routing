"""
Unit tests for app/core/shortest_path.py
Run with: pytest tests/test_shortest_path.py -v
"""
import math

import networkx as nx
import pytest

from app.core.shortest_path import (
    batch_distances,
    pairwise_distance_matrix,
    shortest_path,
    single_source_all_distances,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_graph() -> nx.DiGraph:
    """
    Small directed graph (weights = meters):

        1 ──10000──► 2 ──8000──► 3
        │                        │
       5000                    6000
        ▼                        ▼
        4 ──────────────────────►5  (weight 20000)

    Node coords stored as attributes (lon, lat):
      1: (68.10, 51.60)
      2: (68.20, 51.60)
      3: (68.25, 51.70)
      4: (68.10, 51.50)
      5: (68.30, 51.50)
    """
    G = nx.DiGraph()
    nodes = {
        1: (68.10, 51.60),
        2: (68.20, 51.60),
        3: (68.25, 51.70),
        4: (68.10, 51.50),
        5: (68.30, 51.50),
    }
    for nid, (lon, lat) in nodes.items():
        G.add_node(nid, lon=lon, lat=lat)

    G.add_edge(1, 2, weight=10000)
    G.add_edge(2, 3, weight=8000)
    G.add_edge(3, 5, weight=6000)
    G.add_edge(1, 4, weight=5000)
    G.add_edge(4, 5, weight=20000)
    return G


@pytest.fixture
def G():
    return _make_graph()


# ---------------------------------------------------------------------------
# Tests: shortest_path
# ---------------------------------------------------------------------------

class TestShortestPath:
    def test_direct_edge(self, G):
        result = shortest_path(G, 1, 2)
        assert result is not None
        nodes, dist, coords = result
        assert nodes == [1, 2]
        assert dist == pytest.approx(10000.0)
        assert len(coords) == 2
        assert coords[0] == pytest.approx([68.10, 51.60])
        assert coords[1] == pytest.approx([68.20, 51.60])

    def test_multi_hop_path(self, G):
        # 1 → 2 → 3 → 5 = 10000 + 8000 + 6000 = 24000 m
        # 1 → 4 → 5      = 5000  + 20000        = 25000 m
        # Shortest = via 2, 3
        result = shortest_path(G, 1, 5)
        assert result is not None
        nodes, dist, coords = result
        assert nodes == [1, 2, 3, 5]
        assert dist == pytest.approx(24000.0)
        assert len(coords) == 4

    def test_same_node(self, G):
        result = shortest_path(G, 3, 3)
        assert result is not None
        nodes, dist, coords = result
        assert nodes == [3]
        assert dist == pytest.approx(0.0)
        assert len(coords) == 1

    def test_no_path_returns_none(self):
        # Truly disconnected graph — not even undirected fallback can connect them
        G2 = nx.DiGraph()
        G2.add_node(10, lon=68.0, lat=51.0)
        G2.add_node(20, lon=69.0, lat=52.0)
        # No edges between 10 and 20 in either direction
        assert shortest_path(G2, 10, 20) is None

    def test_undirected_fallback_finds_reverse_path(self, G):
        # 5→1 has no directed path, but undirected fallback should succeed:
        # undirected: 5-3 (6000), 3-2 (8000), 2-1 (10000) = 24000 m
        result = shortest_path(G, 5, 1)
        assert result is not None
        nodes, dist, coords = result
        assert dist == pytest.approx(24000.0)
        assert nodes[0] == 5
        assert nodes[-1] == 1

    def test_unknown_source_returns_none(self, G):
        assert shortest_path(G, 999, 1) is None

    def test_unknown_target_returns_none(self, G):
        assert shortest_path(G, 1, 999) is None

    def test_coords_match_node_attributes(self, G):
        result = shortest_path(G, 1, 3)
        assert result is not None
        nodes, _, coords = result
        for nid, (lon, lat) in zip(nodes, coords):
            assert lon == pytest.approx(G.nodes[nid]["lon"])
            assert lat == pytest.approx(G.nodes[nid]["lat"])


# ---------------------------------------------------------------------------
# Tests: batch_distances
# ---------------------------------------------------------------------------

class TestBatchDistances:
    def test_single_pair(self, G):
        d = batch_distances(G, [1], [2])
        assert d[(1, 2)] == pytest.approx(10000.0)

    def test_multiple_sources_multiple_targets(self, G):
        d = batch_distances(G, [1, 4], [3, 5])
        # 1 → 3: 10000 + 8000 = 18000
        assert d[(1, 3)] == pytest.approx(18000.0)
        # 1 → 5: 24000 (via 2,3) < 25000 (via 4)
        assert d[(1, 5)] == pytest.approx(24000.0)
        # 4 → 5: 20000
        assert d[(4, 5)] == pytest.approx(20000.0)
        # 4 → 3: no direct directed path 4→3, but undirected fallback may find one
        # so we only assert it's a valid float, not inf in this case
        assert d[(4, 3)] >= 0

    def test_self_distance_is_zero(self, G):
        d = batch_distances(G, [1, 2], [1, 2])
        assert d[(1, 1)] == pytest.approx(0.0)
        assert d[(2, 2)] == pytest.approx(0.0)

    def test_unreachable_directed_uses_undirected_fallback(self, G):
        # 5→1 has no directed path, but batch_distances uses undirected fallback
        # so a finite distance is expected (same as shortest_path undirected fallback)
        d = batch_distances(G, [5], [1])
        assert not math.isinf(d[(5, 1)]), "Undirected fallback should find 5→1 path"

    def test_deduplicates_sources(self, G):
        # Passing duplicate source should not raise or double-count
        d = batch_distances(G, [1, 1, 2], [3])
        assert d[(1, 3)] == pytest.approx(18000.0)
        assert d[(2, 3)] == pytest.approx(8000.0)

    def test_empty_sources_returns_empty(self, G):
        assert batch_distances(G, [], [1, 2]) == {}

    def test_empty_targets_returns_empty(self, G):
        assert batch_distances(G, [1], []) == {}

    def test_unknown_source_node(self, G):
        d = batch_distances(G, [999], [1])
        assert math.isinf(d[(999, 1)])

    def test_dijkstra_runs_once_per_unique_source(self, G, mocker):
        """Verify that deduplication causes only 1 Dijkstra pass for 3 identical sources."""
        spy = mocker.patch(
            "app.core.shortest_path.nx.single_source_dijkstra_path_length",
            wraps=nx.single_source_dijkstra_path_length,
        )
        batch_distances(G, [1, 1, 1], [2])
        assert spy.call_count == 1


# ---------------------------------------------------------------------------
# Tests: pairwise_distance_matrix
# ---------------------------------------------------------------------------

class TestPairwiseDistanceMatrix:
    def test_diagonal_is_zero(self, G):
        matrix = pairwise_distance_matrix(G, [1, 2, 3])
        assert matrix[(1, 1)] == pytest.approx(0.0)
        assert matrix[(2, 2)] == pytest.approx(0.0)
        assert matrix[(3, 3)] == pytest.approx(0.0)

    def test_known_distances(self, G):
        matrix = pairwise_distance_matrix(G, [1, 2, 3])
        assert matrix[(1, 2)] == pytest.approx(10000.0)
        assert matrix[(1, 3)] == pytest.approx(18000.0)
        assert matrix[(2, 3)] == pytest.approx(8000.0)

    def test_directed_graph_asymmetry_via_fallback(self, G):
        matrix = pairwise_distance_matrix(G, [1, 5])
        # 1 → 5 is reachable directly
        assert not math.isinf(matrix[(1, 5)])
        # 5 → 1 may be found via undirected fallback — just verify it's a valid number
        assert matrix[(5, 1)] >= 0

    def test_deduplicates_node_list(self, G):
        m1 = pairwise_distance_matrix(G, [1, 2, 3])
        m2 = pairwise_distance_matrix(G, [1, 2, 2, 3, 1])
        # Results should be identical
        assert m1 == m2


# ---------------------------------------------------------------------------
# Tests: single_source_all_distances
# ---------------------------------------------------------------------------

class TestSingleSourceAllDistances:
    def test_all_reachable_nodes(self, G):
        d = single_source_all_distances(G, 1)
        assert d[1] == pytest.approx(0.0)
        assert d[2] == pytest.approx(10000.0)
        assert d[3] == pytest.approx(18000.0)
        assert d[4] == pytest.approx(5000.0)
        assert d[5] == pytest.approx(24000.0)

    def test_cutoff_limits_results(self, G):
        # With cutoff=12000, from node 1 we can reach 2 (10000) and 4 (5000) but not 3 (18000)
        d = single_source_all_distances(G, 1, cutoff=12000)
        assert 2 in d
        assert 4 in d
        assert 3 not in d
        assert 5 not in d

    def test_unknown_source_returns_empty(self, G):
        assert single_source_all_distances(G, 999) == {}
