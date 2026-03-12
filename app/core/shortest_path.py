"""
Module 2: Shortest Path & Distance Matrix
==========================================
Provides:
  - Single-pair Dijkstra with full route geometry.
  - Batch Dijkstra for N sources × M targets (one pass per unique source).
  - Pairwise all-to-all distance matrix for a node set.

Algorithm choice: nx.single_source_dijkstra_path_length for distance-only
queries (faster), and nx.single_source_dijkstra for queries that also need
the node path. Both are O((V + E) log V) per source call.

Design principle: accept a plain nx.DiGraph so this module has no dependency
on graph_loader — it is pure algorithm logic, easily unit-testable.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import networkx as nx

logger = logging.getLogger(__name__)

# Cache for undirected graph (expensive to build, reused across calls)
_undirected_cache: dict[int, nx.Graph] = {}


def _get_undirected(graph: nx.DiGraph) -> nx.Graph:
    gid = id(graph)
    if gid not in _undirected_cache:
        _undirected_cache[gid] = graph.to_undirected()
    return _undirected_cache[gid]


# Type aliases
NodeId = int
PathResult = tuple[list[NodeId], float, list[list[float]]]
# PathResult = (ordered_node_ids, total_distance_meters, [[lon, lat], ...])

DistanceMatrix = dict[tuple[NodeId, NodeId], float]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def shortest_path(
    graph: nx.DiGraph,
    source: NodeId,
    target: NodeId,
) -> Optional[PathResult]:
    """Find the shortest path between two nodes, returning full geometry.

    Coordinates are read from node attributes 'lon' and 'lat', which are
    set by graph_loader when the graph is built from road_nodes.

    Args:
        graph:  Road network (directed, weighted, 'weight' = meters).
        source: Starting node ID.
        target: Destination node ID.

    Returns:
        (node_ids, distance_m, [[lon, lat], ...]) or None if no path exists.
    """
    if source not in graph:
        logger.debug("Source node %d not in graph.", source)
        return None
    if target not in graph:
        logger.debug("Target node %d not in graph.", target)
        return None

    # Trivial case: same node
    if source == target:
        lon = graph.nodes[source].get("lon", 0.0)
        lat = graph.nodes[source].get("lat", 0.0)
        return [source], 0.0, [[lon, lat]]

    try:
        distance, path_nodes = nx.single_source_dijkstra(
            graph, source, target=target, weight="weight"
        )
    except nx.NetworkXNoPath:
        # Fallback: try undirected graph (road data may have one-way gaps)
        logger.debug("No directed path %d→%d, trying undirected fallback.", source, target)
        ugraph = _get_undirected(graph)
        try:
            distance, path_nodes = nx.single_source_dijkstra(
                ugraph, source, target=target, weight="weight"
            )
            logger.info("Undirected fallback path %d→%d: %.0f m", source, target, distance)
        except nx.NetworkXNoPath:
            logger.debug("No path from node %d to node %d.", source, target)
            return None
    except nx.NodeNotFound as exc:
        logger.warning("NodeNotFound during Dijkstra: %s", exc)
        return None

    coords = _extract_coords(graph, path_nodes)
    return path_nodes, float(distance), coords


def batch_distances(
    graph: nx.DiGraph,
    sources: list[NodeId],
    targets: list[NodeId],
) -> DistanceMatrix:
    """Compute shortest distances from each source to each target.

    Runs one Dijkstra pass per **unique** source node, then picks out
    distances to all requested targets from the result. This is optimal
    when N_sources << N_nodes (typical in VRP: ~tens of vehicles).

    Args:
        graph:   Road network graph.
        sources: Source node IDs (e.g., current vehicle positions).
                 Duplicates are collapsed — each unique source runs once.
        targets: Target node IDs (e.g., task destination nodes).

    Returns:
        Dict {(source_node, target_node): distance_meters}.
        Value is math.inf for unreachable pairs.
        Value is 0.0 when source_node == target_node.
    """
    if not sources or not targets:
        return {}

    # Deduplicate sources while preserving the original list for result mapping
    unique_sources: list[NodeId] = list(dict.fromkeys(sources))
    target_set: set[NodeId] = set(targets)

    logger.debug(
        "batch_distances: %d unique sources × %d targets → %d Dijkstra passes",
        len(unique_sources),
        len(targets),
        len(unique_sources),
    )

    # Map: unique_source → {target_node: distance_m}
    source_lengths: dict[NodeId, dict[NodeId, float]] = {}

    for src in unique_sources:
        if src not in graph:
            logger.warning("Source node %d not in graph — all targets set to inf.", src)
            source_lengths[src] = {}
            continue

        try:
            # Single Dijkstra pass from src → distances to all reachable nodes
            raw: dict[NodeId, float] = dict(
                nx.single_source_dijkstra_path_length(graph, src, weight="weight")
            )
        except nx.NetworkXError as exc:
            logger.warning("Dijkstra error from node %d: %s", src, exc)
            raw = {}

        # Keep only the distances we actually need
        source_lengths[src] = {t: raw[t] for t in target_set if t in raw}

    # Build the flat result dict for the original (possibly duplicate) sources
    result: DistanceMatrix = {}
    for src in sources:
        lengths = source_lengths.get(src, {})
        for tgt in targets:
            if src == tgt:
                result[(src, tgt)] = 0.0
            else:
                result[(src, tgt)] = lengths.get(tgt, math.inf)

    _log_unreachable(result, sources, targets)
    return result


def pairwise_distance_matrix(
    graph: nx.DiGraph,
    nodes: list[NodeId],
) -> DistanceMatrix:
    """All-pairs shortest distances for a set of nodes.

    Equivalent to batch_distances(graph, nodes, nodes) with diagonal forced to 0.
    Used by the multitask grouping solver to evaluate detour ratios.

    Args:
        graph: Road network graph.
        nodes: Node IDs to include (duplicates are ignored).

    Returns:
        Dict {(i, j): distance_meters} for all ordered pairs.
    """
    unique_nodes = list(dict.fromkeys(nodes))  # dedup, preserve order
    matrix = batch_distances(graph, unique_nodes, unique_nodes)

    # Guarantee diagonal is exactly 0.0 (Dijkstra may never visit self)
    for n in unique_nodes:
        matrix[(n, n)] = 0.0

    return matrix


def single_source_all_distances(
    graph: nx.DiGraph,
    source: NodeId,
    cutoff: Optional[float] = None,
) -> dict[NodeId, float]:
    """Full Dijkstra from one source to all reachable nodes.

    Useful for pre-computing a vehicle's reach across the entire graph.

    Args:
        graph:  Road network graph.
        source: Starting node.
        cutoff: Optional maximum distance in meters — nodes beyond this are omitted.

    Returns:
        {node_id: distance_m} for all reachable nodes within cutoff.
    """
    if source not in graph:
        return {}
    try:
        return dict(
            nx.single_source_dijkstra_path_length(
                graph, source, cutoff=cutoff, weight="weight"
            )
        )
    except nx.NetworkXError as exc:
        logger.warning("Dijkstra error from source %d: %s", source, exc)
        return {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_coords(graph: nx.DiGraph, node_ids: list[NodeId]) -> list[list[float]]:
    """Read [lon, lat] from node attributes for each node in the path."""
    coords: list[list[float]] = []
    for nid in node_ids:
        attrs = graph.nodes.get(nid, {})
        coords.append([attrs.get("lon", 0.0), attrs.get("lat", 0.0)])
    return coords


def _log_unreachable(
    result: DistanceMatrix,
    sources: list[NodeId],
    targets: list[NodeId],
) -> None:
    """Emit a single warning if any (source, target) pair is unreachable."""
    inf_pairs = [
        (s, t)
        for s in set(sources)
        for t in set(targets)
        if s != t and math.isinf(result.get((s, t), math.inf))
    ]
    if inf_pairs:
        logger.warning(
            "%d unreachable source→target pairs (graph may be disconnected). "
            "First few: %s",
            len(inf_pairs),
            inf_pairs[:5],
        )
