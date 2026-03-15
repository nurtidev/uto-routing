"""
Module 1: Road Graph Loader & Spatial Index
============================================
Responsibilities:
  - Load road_nodes and road_edges from PostgreSQL at service startup.
  - Build an in-memory directed weighted NetworkX graph.
  - Build a scipy KD-Tree for fast map-matching (coordinate → nearest node).
  - Expose snap_to_node(lon, lat) → node_id globally after startup.

Lifecycle:
  Call `load_graph(session)` once inside the FastAPI lifespan.
  All other modules use `get_graph()` to access the cached GraphData.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import networkx as nx
import numpy as np
from scipy.spatial import KDTree
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

@dataclass
class GraphData:
    """Immutable snapshot of the road network, kept in memory for the service lifetime."""

    # NetworkX directed graph: nodes carry lon/lat attributes, edges carry weight (meters).
    graph: nx.DiGraph

    # KD-Tree built over (lon, lat) coordinates for O(log N) nearest-node lookup.
    kdtree: KDTree

    # Parallel arrays: node_ids[i] ↔ node_coords[i] ↔ kdtree leaf i
    node_ids: np.ndarray    # shape (N,), dtype int64 — logical node_id values
    node_coords: np.ndarray  # shape (N, 2), dtype float64 — [[lon, lat], ...]

    # Fast reverse lookup: node_id (int) → index in node_ids / node_coords
    node_index: dict[int, int] = field(default_factory=dict)

    # Nodes belonging to the largest weakly connected component.
    # Pairs where both nodes are outside this set will never have a directed path.
    largest_wcc_nodes: frozenset = field(default_factory=frozenset)

    @property
    def node_count(self) -> int:
        return len(self.node_ids)

    @property
    def edge_count(self) -> int:
        return self.graph.number_of_edges()

    def in_main_component(self, node_id: int) -> bool:
        """True if node_id is part of the largest weakly connected component."""
        return node_id in self.largest_wcc_nodes


# ---------------------------------------------------------------------------
# Module-level singleton cache
# ---------------------------------------------------------------------------

_graph_data: Optional[GraphData] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def load_graph(session: AsyncSession) -> GraphData:
    """Load and cache the road graph from the database.

    Idempotent: if the graph is already loaded, returns the cached instance
    immediately without hitting the database again.

    Args:
        session: An active async SQLAlchemy session.

    Returns:
        The populated GraphData singleton.
    """
    global _graph_data
    if _graph_data is not None:
        logger.debug("Graph already cached — skipping DB load.")
        return _graph_data

    logger.info("Loading road graph from database …")

    # ------------------------------------------------------------------
    # 1. Fetch nodes
    # ------------------------------------------------------------------
    nodes_result = await session.execute(
        text('SELECT node_id, lon, lat FROM "references".road_nodes ORDER BY node_id')
    )
    node_rows = nodes_result.fetchall()

    if not node_rows:
        raise RuntimeError(
            "references.road_nodes is empty — cannot build graph. "
            "Make sure the database is populated before starting the service."
        )

    # ------------------------------------------------------------------
    # 2. Fetch edges
    # ------------------------------------------------------------------
    edges_result = await session.execute(
        text('SELECT source, target, weight FROM "references".road_edges')
    )
    edge_rows = edges_result.fetchall()

    logger.info(
        "Fetched %d nodes and %d edges from DB.", len(node_rows), len(edge_rows)
    )

    # ------------------------------------------------------------------
    # 3. Build NetworkX directed graph
    # ------------------------------------------------------------------
    G = nx.DiGraph()

    node_ids_list: list[int] = []
    coords_list: list[list[float]] = []

    for node_id, lon, lat in node_rows:
        nid = int(node_id)
        flon, flat = float(lon), float(lat)
        G.add_node(nid, lon=flon, lat=flat)
        node_ids_list.append(nid)
        coords_list.append([flon, flat])

    skipped_edges = 0
    for source, target, weight in edge_rows:
        src, tgt = int(source), int(target)
        w = float(weight)
        if src not in G or tgt not in G:
            # Guard against dangling edges referencing unknown nodes
            skipped_edges += 1
            continue
        G.add_edge(src, tgt, weight=w)

    if skipped_edges:
        logger.warning(
            "Skipped %d edges with unknown source/target nodes.", skipped_edges
        )

    # ------------------------------------------------------------------
    # 4. Analyse weakly connected components
    # ------------------------------------------------------------------
    wccs = sorted(nx.weakly_connected_components(G), key=len, reverse=True)
    largest_wcc = frozenset(wccs[0]) if wccs else frozenset()

    if len(wccs) == 1:
        logger.info("Graph is fully connected (1 weakly connected component).")
    else:
        isolated = sum(1 for c in wccs if len(c) == 1)
        small = [c for c in wccs[1:] if len(c) > 1]
        logger.warning(
            "Graph has %d weakly connected components. "
            "Largest: %d nodes. Small components (size ≥ 2): %d. Isolated nodes: %d. "
            "Routing across disconnected components will use undirected fallback.",
            len(wccs), len(largest_wcc), len(small), isolated,
        )
        if small:
            sizes = sorted((len(c) for c in small), reverse=True)
            logger.warning("Small component sizes: %s", sizes[:10])

    # ------------------------------------------------------------------
    # 5. Build numpy arrays + KD-Tree
    # ------------------------------------------------------------------
    node_ids_arr = np.array(node_ids_list, dtype=np.int64)
    coords_arr = np.array(coords_list, dtype=np.float64)  # shape (N, 2): [lon, lat]

    kdtree = KDTree(coords_arr)

    # Reverse index for O(1) node_id → array-index lookups
    node_index: dict[int, int] = {
        int(nid): idx for idx, nid in enumerate(node_ids_arr)
    }

    # ------------------------------------------------------------------
    # 6. Cache & return
    # ------------------------------------------------------------------
    _graph_data = GraphData(
        graph=G,
        kdtree=kdtree,
        node_ids=node_ids_arr,
        node_coords=coords_arr,
        node_index=node_index,
        largest_wcc_nodes=largest_wcc,
    )

    logger.info(
        "Graph ready: %d nodes, %d edges.",
        _graph_data.node_count,
        _graph_data.edge_count,
    )
    return _graph_data


def get_graph() -> GraphData:
    """Return the cached GraphData.

    Raises:
        RuntimeError: If load_graph() has not been called yet.
    """
    if _graph_data is None:
        raise RuntimeError(
            "Road graph is not loaded. "
            "Call `await load_graph(session)` inside the FastAPI lifespan startup."
        )
    return _graph_data


def snap_to_node(lon: float, lat: float) -> int:
    """Map-match arbitrary coordinates to the nearest road-graph node.

    Uses the pre-built KD-Tree for O(log N) lookup.

    Args:
        lon: Longitude (pos_x in Wialon, longitude in wells).
        lat: Latitude  (pos_y in Wialon, latitude  in wells).

    Returns:
        node_id (int) of the nearest road node.
    """
    data = get_graph()
    _, idx = data.kdtree.query([lon, lat])
    return int(data.node_ids[idx])


def snap_to_node_batch(coords: list[tuple[float, float]]) -> list[int]:
    """Vectorised snap_to_node for multiple points at once.

    Args:
        coords: List of (lon, lat) tuples.

    Returns:
        List of node_ids in the same order as the input.
    """
    if not coords:
        return []
    data = get_graph()
    pts = np.array(coords, dtype=np.float64)   # shape (M, 2)
    _, indices = data.kdtree.query(pts)
    return [int(data.node_ids[i]) for i in indices]


def snap_to_node_with_distance(lon: float, lat: float) -> tuple[int, float]:
    """Like snap_to_node but also returns the Euclidean distance to the matched node.

    The distance is in the same unit as the coordinate space (degrees), useful
    for quick sanity-checks (e.g. detecting points very far from the road network).

    Returns:
        (node_id, euclidean_distance)
    """
    data = get_graph()
    dist, idx = data.kdtree.query([lon, lat])
    return int(data.node_ids[idx]), float(dist)


def reset_graph() -> None:
    """Clear the cached graph (mainly for testing purposes)."""
    global _graph_data
    _graph_data = None
