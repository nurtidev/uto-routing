"""
Unit tests for app/core/graph_loader.py
Run with: pytest tests/test_graph_loader.py -v
No real DB required — uses a mock async session.
"""

import pytest
import numpy as np
from unittest.mock import AsyncMock, MagicMock

from app.core.graph_loader import (
    load_graph,
    get_graph,
    snap_to_node,
    snap_to_node_batch,
    snap_to_node_with_distance,
    reset_graph,
    GraphData,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_session(nodes: list[tuple], edges: list[tuple]) -> AsyncMock:
    """Build a mock AsyncSession that returns given nodes and edges."""
    session = AsyncMock()

    nodes_result = MagicMock()
    nodes_result.fetchall.return_value = nodes

    edges_result = MagicMock()
    edges_result.fetchall.return_value = edges

    # session.execute returns different results for each call (nodes first, edges second)
    session.execute = AsyncMock(side_effect=[nodes_result, edges_result])
    return session


SAMPLE_NODES = [
    (1, 68.10, 51.60),
    (2, 68.20, 51.60),
    (3, 68.15, 51.70),
    (4, 68.30, 51.80),
]

SAMPLE_EDGES = [
    (1, 2, 10000.0),   # 10 km
    (2, 3,  8000.0),
    (3, 4, 15000.0),
    (2, 1, 10000.0),   # bidirectional represented as two directed edges
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_cache():
    """Ensure each test starts with a clean module cache."""
    reset_graph()
    yield
    reset_graph()


# ---------------------------------------------------------------------------
# Tests: load_graph
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_graph_builds_graph_and_kdtree():
    session = _make_mock_session(SAMPLE_NODES, SAMPLE_EDGES)
    data = await load_graph(session)

    assert isinstance(data, GraphData)
    assert data.node_count == 4
    assert data.edge_count == 4
    assert data.kdtree is not None


@pytest.mark.asyncio
async def test_load_graph_nodes_and_edges_correct():
    session = _make_mock_session(SAMPLE_NODES, SAMPLE_EDGES)
    data = await load_graph(session)

    G = data.graph
    assert set(G.nodes) == {1, 2, 3, 4}
    assert G.has_edge(1, 2)
    assert G.has_edge(2, 1)
    assert not G.has_edge(1, 4)

    # Check edge weight
    assert G[1][2]["weight"] == pytest.approx(10000.0)


@pytest.mark.asyncio
async def test_load_graph_node_attributes():
    session = _make_mock_session(SAMPLE_NODES, SAMPLE_EDGES)
    data = await load_graph(session)

    G = data.graph
    assert G.nodes[1]["lon"] == pytest.approx(68.10)
    assert G.nodes[1]["lat"] == pytest.approx(51.60)


@pytest.mark.asyncio
async def test_load_graph_is_idempotent():
    """Second call returns cached instance, session.execute not called again."""
    session1 = _make_mock_session(SAMPLE_NODES, SAMPLE_EDGES)
    data1 = await load_graph(session1)

    session2 = _make_mock_session(SAMPLE_NODES, SAMPLE_EDGES)
    data2 = await load_graph(session2)

    assert data1 is data2
    session2.execute.assert_not_called()


@pytest.mark.asyncio
async def test_load_graph_raises_on_empty_nodes():
    session = _make_mock_session([], [])
    with pytest.raises(RuntimeError, match="road_nodes is empty"):
        await load_graph(session)


@pytest.mark.asyncio
async def test_load_graph_skips_dangling_edges():
    """Edges referencing unknown nodes should be silently skipped."""
    dangling_edges = SAMPLE_EDGES + [(99, 100, 5000.0)]
    session = _make_mock_session(SAMPLE_NODES, dangling_edges)
    data = await load_graph(session)
    # Dangling edge (99→100) skipped; valid 4 edges remain
    assert data.edge_count == 4


# ---------------------------------------------------------------------------
# Tests: get_graph
# ---------------------------------------------------------------------------

def test_get_graph_raises_before_load():
    with pytest.raises(RuntimeError, match="not loaded"):
        get_graph()


@pytest.mark.asyncio
async def test_get_graph_returns_same_instance_as_load():
    session = _make_mock_session(SAMPLE_NODES, SAMPLE_EDGES)
    loaded = await load_graph(session)
    fetched = get_graph()
    assert loaded is fetched


# ---------------------------------------------------------------------------
# Tests: snap_to_node
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snap_to_node_exact_match():
    session = _make_mock_session(SAMPLE_NODES, SAMPLE_EDGES)
    await load_graph(session)

    # Exact coordinates of node 1
    assert snap_to_node(68.10, 51.60) == 1


@pytest.mark.asyncio
async def test_snap_to_node_nearest():
    session = _make_mock_session(SAMPLE_NODES, SAMPLE_EDGES)
    await load_graph(session)

    # Point slightly offset from node 2 (68.20, 51.60)
    assert snap_to_node(68.201, 51.601) == 2


@pytest.mark.asyncio
async def test_snap_to_node_raises_before_load():
    with pytest.raises(RuntimeError):
        snap_to_node(68.10, 51.60)


# ---------------------------------------------------------------------------
# Tests: snap_to_node_batch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snap_to_node_batch():
    session = _make_mock_session(SAMPLE_NODES, SAMPLE_EDGES)
    await load_graph(session)

    result = snap_to_node_batch([(68.10, 51.60), (68.20, 51.60), (68.15, 51.70)])
    assert result == [1, 2, 3]


@pytest.mark.asyncio
async def test_snap_to_node_batch_empty():
    session = _make_mock_session(SAMPLE_NODES, SAMPLE_EDGES)
    await load_graph(session)
    assert snap_to_node_batch([]) == []


# ---------------------------------------------------------------------------
# Tests: snap_to_node_with_distance
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snap_with_distance_exact():
    session = _make_mock_session(SAMPLE_NODES, SAMPLE_EDGES)
    await load_graph(session)

    node_id, dist = snap_to_node_with_distance(68.10, 51.60)
    assert node_id == 1
    assert dist == pytest.approx(0.0, abs=1e-9)


@pytest.mark.asyncio
async def test_snap_with_distance_positive_for_offset():
    session = _make_mock_session(SAMPLE_NODES, SAMPLE_EDGES)
    await load_graph(session)

    node_id, dist = snap_to_node_with_distance(68.105, 51.605)
    assert node_id == 1
    assert dist > 0


# ---------------------------------------------------------------------------
# Tests: node_index (reverse lookup)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_node_index_complete():
    session = _make_mock_session(SAMPLE_NODES, SAMPLE_EDGES)
    data = await load_graph(session)

    for nid in [1, 2, 3, 4]:
        idx = data.node_index[nid]
        assert data.node_ids[idx] == nid
        # Verify coordinates match
        assert data.node_coords[idx][0] == pytest.approx(data.graph.nodes[nid]["lon"])
        assert data.node_coords[idx][1] == pytest.approx(data.graph.nodes[nid]["lat"])
