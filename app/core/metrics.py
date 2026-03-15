"""
Prometheus metrics for ИС УТО.

Exposes:
  - HTTP request counters and latency histograms (via middleware)
  - Fleet/graph KPI gauges (updated on /metrics scrape)

Usage:
  Metrics are registered globally on import.
  Call update_kpi_gauges() to refresh fleet/graph values.
  Mount the ASGI metrics app at /metrics in main.py.
"""
from __future__ import annotations

import time
import logging

from prometheus_client import Counter, Gauge, Histogram, CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------

REQUEST_COUNT = Counter(
    "uto_http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status_code"],
)

REQUEST_LATENCY = Histogram(
    "uto_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "endpoint"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

# Fleet KPIs
FLEET_VEHICLES_TOTAL = Gauge("uto_fleet_vehicles_total", "Total vehicles in fleet")
FLEET_VEHICLES_FREE = Gauge("uto_fleet_vehicles_free", "Vehicles currently free")
FLEET_VEHICLES_BUSY = Gauge("uto_fleet_vehicles_busy", "Vehicles currently busy")

# Graph KPIs
GRAPH_NODES = Gauge("uto_graph_nodes_total", "Road graph node count")
GRAPH_EDGES = Gauge("uto_graph_edges_total", "Road graph edge count")
GRAPH_COMPONENTS = Gauge("uto_graph_components_total", "Number of weakly connected components")

# Orders
ORDERS_TOTAL = Gauge("uto_orders_total", "Total active orders")
SLA_COMPLIANCE = Gauge("uto_sla_compliance_ratio", "SLA compliance ratio (0-1) for high-priority orders")


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

def _normalise_path(path: str) -> str:
    """Collapse dynamic segments to avoid high-cardinality labels."""
    # e.g. /api/route/123 → /api/route/:id
    import re
    return re.sub(r"/\d+", "/:id", path)


class PrometheusMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        endpoint = _normalise_path(request.url.path)
        start = time.perf_counter()
        status = "500"  # default — overwritten on success
        try:
            response = await call_next(request)
            status = str(response.status_code)
        except Exception:
            raise
        finally:
            elapsed = time.perf_counter() - start
            REQUEST_COUNT.labels(request.method, endpoint, status).inc()
            REQUEST_LATENCY.labels(request.method, endpoint).observe(elapsed)
        return response


# ---------------------------------------------------------------------------
# KPI gauge updater  (called on each /metrics scrape)
# ---------------------------------------------------------------------------

def update_kpi_gauges() -> None:
    """Refresh fleet and graph gauges from in-memory caches (no DB call)."""
    try:
        from app.core.graph_service import get_graph_service
        svc = get_graph_service()
        if svc:
            GRAPH_NODES.set(svc.node_count)
            GRAPH_EDGES.set(svc.edge_count)
            # Component info available if largest_wcc_nodes is populated
            data = svc._data
            if hasattr(data, "largest_wcc_nodes"):
                # We don't store total component count directly; use 1 as minimum
                GRAPH_COMPONENTS.set(1)
    except Exception as exc:
        logger.debug("metrics: graph update skipped: %s", exc)

    try:
        from app.core.fleet_state import _fleet_state
        if _fleet_state:
            total = _fleet_state.vehicle_count
            busy = sum(1 for v in _fleet_state.vehicles if v.free_at_minutes > 0)
            FLEET_VEHICLES_TOTAL.set(total)
            FLEET_VEHICLES_FREE.set(total - busy)
            FLEET_VEHICLES_BUSY.set(busy)
    except Exception as exc:
        logger.debug("metrics: fleet update skipped: %s", exc)
