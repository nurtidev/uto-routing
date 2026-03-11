"""
GraphService — high-level wrapper around GraphData (graph_loader).

Provides methods needed by the API layer:
  - snap_to_node(lon, lat) → node_id
  - shortest_path(src, dst) → (nodes, distance_m, coords)
  - batch_shortest_distances(sources, targets) → dict[(src,tgt), float]
  - distances_to_node(sources, target) → dict[src, float]  (N→1 convenience)
  - pairwise_distance_matrix(nodes) → dict[(i,j), float]
  - resolve_well_node(db, uwi) → node_id | None
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.graph_loader import GraphData, load_graph, snap_to_node as _snap
from app.core.shortest_path import (
    DistanceMatrix,
    PathResult,
    batch_distances,
    pairwise_distance_matrix as _pairwise,
    shortest_path as _shortest_path,
)

logger = logging.getLogger(__name__)

# Singleton GraphService instance
_graph_service: Optional["GraphService"] = None


class GraphService:
    """High-level service for graph operations. Wraps GraphData from graph_loader."""

    def __init__(self, data: GraphData) -> None:
        self._data = data
        self._well_node_cache: dict[str, Optional[int]] = {}  # instance-level cache
        self._well_coord_cache: dict[str, Optional[tuple[float, float]]] = {}

    @property
    def node_count(self) -> int:
        return self._data.node_count

    @property
    def edge_count(self) -> int:
        return self._data.edge_count

    # ── Graph bounds ───────────────────────────────────────────────

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """Returns (min_lon, min_lat, max_lon, max_lat) of the road network."""
        coords = self._data.node_coords
        return (
            float(coords[:, 0].min()),
            float(coords[:, 1].min()),
            float(coords[:, 0].max()),
            float(coords[:, 1].max()),
        )

    def node_at_index(self, idx: int) -> int:
        """Return node_id at array index idx (wraps around)."""
        return int(self._data.node_ids[idx % len(self._data.node_ids)])

    # ── Map-matching ───────────────────────────────────────────────

    def snap_to_node(self, lon: float, lat: float) -> Optional[int]:
        """Return the nearest road graph node_id for given coordinates."""
        try:
            return _snap(lon, lat)
        except Exception as exc:
            logger.warning("snap_to_node failed for (%s, %s): %s", lon, lat, exc)
            return None

    # ── Shortest path ──────────────────────────────────────────────

    def shortest_path(
        self,
        source_node: int,
        target_node: int,
    ) -> Optional[PathResult]:
        """Shortest path via Dijkstra (single pass, returns path + geometry).

        Returns:
            (node_list, distance_meters, [[lon, lat], ...]) or None.
        """
        return _shortest_path(self._data.graph, source_node, target_node)

    # ── Batch distance computation ─────────────────────────────────

    def batch_shortest_distances(
        self,
        sources: list[int],
        targets: list[int],
    ) -> DistanceMatrix:
        """Shortest distances for all (source, target) pairs.

        One Dijkstra pass per unique source (O(N × (V+E)logV)).

        Returns:
            {(source_node, target_node): distance_meters}
            math.inf for unreachable pairs.
        """
        return batch_distances(self._data.graph, sources, targets)

    def distances_to_node(
        self,
        sources: list[int],
        target: int,
    ) -> dict[int, float]:
        """Convenience: distances from N sources to a single target node.

        Returns:
            {source_node: distance_meters}  — flat dict (no tuple keys).
        """
        full = batch_distances(self._data.graph, sources, [target])
        return {src: full.get((src, target), float("inf")) for src in sources}

    def pairwise_distance_matrix(
        self,
        nodes: list[Optional[int]],
    ) -> DistanceMatrix:
        """All-pairs shortest distances for a node set (directed, not symmetric).

        Returns:
            {(node_i, node_j): distance_meters} for all ordered pairs.
            Diagonal is 0.0. math.inf for unreachable pairs.
        """
        valid_nodes = [n for n in nodes if n is not None]
        return _pairwise(self._data.graph, valid_nodes)

    # ── Well resolution ────────────────────────────────────────────

    async def resolve_well_node(
        self,
        db: AsyncSession,
        uwi: str,
    ) -> Optional[int]:
        """Resolve well UWI → nearest road graph node_id (cached per service lifetime)."""
        if uwi in self._well_node_cache:
            return self._well_node_cache[uwi]

        result = await db.execute(
            text(
                'SELECT longitude, latitude FROM "references".wells '
                "WHERE uwi = :uwi AND longitude IS NOT NULL AND latitude IS NOT NULL "
                "LIMIT 1"
            ),
            {"uwi": uwi},
        )
        row = result.fetchone()

        if row is None:
            logger.warning("Well UWI '%s' not found or has no coordinates.", uwi)
            self._well_node_cache[uwi] = None
            return None

        lon, lat = float(row[0]), float(row[1])
        self._well_coord_cache[uwi] = (lon, lat)
        node_id = self.snap_to_node(lon, lat)
        self._well_node_cache[uwi] = node_id
        return node_id

    def get_cached_well_coords(self, uwi: str) -> Optional[tuple[float, float]]:
        """Return cached (lon, lat) for a well UWI (populated by resolve_well_node)."""
        return self._well_coord_cache.get(uwi)


# ── Lifecycle helpers ──────────────────────────────────────────────────────

def get_graph_service() -> Optional[GraphService]:
    """Return the cached GraphService or None if not yet initialised."""
    return _graph_service


async def init_graph_service(session: AsyncSession) -> GraphService:
    """Load graph data and initialise the GraphService singleton (idempotent)."""
    global _graph_service
    if _graph_service is not None:
        logger.debug("GraphService already initialised — reusing cached instance.")
        return _graph_service

    data: GraphData = await load_graph(session)
    _graph_service = GraphService(data)
    logger.info(
        "GraphService ready: %d nodes, %d edges.",
        _graph_service.node_count,
        _graph_service.edge_count,
    )
    return _graph_service


def reset_graph_service() -> None:
    """Clear the singleton (for tests)."""
    global _graph_service
    _graph_service = None
