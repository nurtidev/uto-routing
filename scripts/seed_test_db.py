#!/usr/bin/env python3
"""
scripts/seed_test_db.py — Generate and load synthetic test data into a
PostgreSQL database for local/dev testing.

Creates:
  - references.road_nodes   (~300 nodes, grid + jitter)
  - references.road_edges   (~600 directed edges)
  - references.wells        (30 wells snapped to random nodes)
  - references.wialon_units_snapshot_1/2/3  (20 vehicles × 3 snapshots)
  - public.tasks            (40 tasks of varying priority)

Usage:
    python scripts/seed_test_db.py
    python scripts/seed_test_db.py --drop-first   # recreate from scratch
"""
from __future__ import annotations

import argparse
import logging
import math
import random
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import psycopg2
from app.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Coordinate origin — fictitious oilfield, metrically consistent ──────────
LON0, LAT0 = 68.12000, 51.67000   # bottom-left corner
LON_SPAN   = 0.60                  # ~45 km east–west
LAT_SPAN   = 0.45                  # ~50 km north–south

GRID_COLS  = 20
GRID_ROWS  = 15
JITTER     = 0.008   # degrees ~600 m

VEHICLE_NAMES = [
    "ЦА-320 А001КМ", "ЦА-320 В002ОР", "ЦА-400 К003МН",
    "АЦН-10 А004КМ", "АЦН-10 В005ОР", "АЦН-12 К006МН",
    "А-50 А007КМ",   "А-50 В008ОР",   "А-50У К009МН",
    "Подъёмник А010КМ", "Подъёмник В011ОР", "КМУ К012МН",
    "Вахтовка А013КМ",  "Вахтовка В014ОР",  "КамАЗ К015МН",
    "УАЗ А016КМ",    "УАЗ В017ОР",    "Урал К018МН",
    "ГАЗ-66 А019КМ", "КамАЗ В020ОР",
]

TASK_TYPES = [
    "цементирование", "промывка", "кислотная обработка",
    "перфорация", "освоение", "крс", "трс",
    "транспортировка", "геофизика",
]

WELL_NAMES = [
    f"Скв. {i:03d}" for i in range(1, 31)
]


# ── DDL ─────────────────────────────────────────────────────────────────────

DDL = """
-- Schema
CREATE SCHEMA IF NOT EXISTS "references";

-- Road nodes
CREATE TABLE IF NOT EXISTS "references".road_nodes (
    id      SERIAL PRIMARY KEY,
    node_id INTEGER UNIQUE NOT NULL,
    lon     NUMERIC(12,8) NOT NULL,
    lat     NUMERIC(12,8) NOT NULL
);

-- Road edges
CREATE TABLE IF NOT EXISTS "references".road_edges (
    id     SERIAL PRIMARY KEY,
    source INTEGER NOT NULL,
    target INTEGER NOT NULL,
    weight NUMERIC(12,6) NOT NULL
);

-- Wells
CREATE TABLE IF NOT EXISTS "references".wells (
    id        SERIAL PRIMARY KEY,
    uwi       VARCHAR(50) UNIQUE NOT NULL,
    latitude  NUMERIC(12,8),
    longitude NUMERIC(12,8),
    well_name VARCHAR(255)
);

-- Wialon snapshots
CREATE TABLE IF NOT EXISTS "references".wialon_units_snapshot_1 (
    wialon_id          BIGINT PRIMARY KEY,
    nm                 TEXT,
    cls                INTEGER,
    mu                 INTEGER,
    pos_t              BIGINT,
    pos_y              DOUBLE PRECISION,
    pos_x              DOUBLE PRECISION,
    registration_plate TEXT,
    payload_json       JSONB
);
CREATE TABLE IF NOT EXISTS "references".wialon_units_snapshot_2 (LIKE "references".wialon_units_snapshot_1 INCLUDING ALL);
CREATE TABLE IF NOT EXISTS "references".wialon_units_snapshot_3 (LIKE "references".wialon_units_snapshot_1 INCLUDING ALL);

-- Tasks
CREATE TABLE IF NOT EXISTS public.tasks (
    task_id                VARCHAR(100) PRIMARY KEY,
    priority               VARCHAR(10)  NOT NULL,
    planned_start          TIMESTAMP    NOT NULL,
    planned_duration_hours FLOAT        NOT NULL,
    destination_uwi        VARCHAR(50)  NOT NULL,
    task_type              VARCHAR(100),
    shift                  VARCHAR(10),
    start_day              DATE
);
"""

DROP_DDL = """
DROP TABLE IF EXISTS public.tasks CASCADE;
DROP TABLE IF EXISTS "references".wialon_units_snapshot_3 CASCADE;
DROP TABLE IF EXISTS "references".wialon_units_snapshot_2 CASCADE;
DROP TABLE IF EXISTS "references".wialon_units_snapshot_1 CASCADE;
DROP TABLE IF EXISTS "references".wells CASCADE;
DROP TABLE IF EXISTS "references".road_edges CASCADE;
DROP TABLE IF EXISTS "references".road_nodes CASCADE;
DROP SCHEMA IF EXISTS "references" CASCADE;
"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def haversine_m(lon1, lat1, lon2, lat2) -> float:
    R = 6_371_000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def gen_nodes(rng: random.Random):
    """Generate grid nodes with jitter."""
    nodes = []
    node_id = 1
    for row in range(GRID_ROWS):
        for col in range(GRID_COLS):
            lon = LON0 + (col / (GRID_COLS - 1)) * LON_SPAN + rng.uniform(-JITTER, JITTER)
            lat = LAT0 + (row / (GRID_ROWS - 1)) * LAT_SPAN + rng.uniform(-JITTER, JITTER)
            nodes.append({"node_id": node_id, "lon": round(lon, 8), "lat": round(lat, 8)})
            node_id += 1
    return nodes


def gen_edges(nodes, rng: random.Random):
    """Connect each node to neighbours (grid + a few random shortcuts)."""
    id_to_pos = {n["node_id"]: (n["lon"], n["lat"]) for n in nodes}
    node_ids = [n["node_id"] for n in nodes]
    edges = []
    added = set()

    def add_edge(src, tgt):
        if src == tgt or (src, tgt) in added:
            return
        lo, la = id_to_pos[src]
        lo2, la2 = id_to_pos[tgt]
        w = round(haversine_m(lo, la, lo2, la2), 2)
        edges.append({"source": src, "target": tgt, "weight": w})
        edges.append({"source": tgt, "target": src, "weight": w})
        added.add((src, tgt))
        added.add((tgt, src))

    # Grid neighbours
    for row in range(GRID_ROWS):
        for col in range(GRID_COLS):
            idx = row * GRID_COLS + col
            nid = node_ids[idx]
            if col + 1 < GRID_COLS:
                add_edge(nid, node_ids[idx + 1])
            if row + 1 < GRID_ROWS:
                add_edge(nid, node_ids[idx + GRID_COLS])
            # Diagonal connections (field roads)
            if col + 1 < GRID_COLS and row + 1 < GRID_ROWS:
                if rng.random() < 0.3:
                    add_edge(nid, node_ids[idx + GRID_COLS + 1])

    # Random shortcuts (cross-field tracks)
    for _ in range(60):
        a, b = rng.sample(node_ids, 2)
        lo, la = id_to_pos[a]
        lo2, la2 = id_to_pos[b]
        if haversine_m(lo, la, lo2, la2) < 8000:
            add_edge(a, b)

    return edges


def gen_wells(nodes, rng: random.Random):
    chosen = rng.sample(nodes, 30)
    wells = []
    for i, n in enumerate(chosen):
        uwi = f"KZ-{i+1:04d}-W{rng.randint(100,999)}"
        wells.append({
            "uwi": uwi,
            "latitude": n["lat"],
            "longitude": n["lon"],
            "well_name": WELL_NAMES[i],
        })
    return wells


def gen_vehicles(nodes, rng: random.Random):
    """Generate 20 vehicles with positions in 3 snapshots."""
    base_t = int(time.time()) - 7200  # 2 hours ago
    snaps = [[], [], []]
    for i, name in enumerate(VEHICLE_NAMES):
        wid = 10000 + i
        reg = f"A{100+i:03d}KZ"
        # Vehicle moves between snapshots (simulates travel)
        pos0 = rng.choice(nodes)
        pos1 = rng.choice(nodes)
        pos2 = rng.choice(nodes)
        speed_kmh = rng.uniform(20, 60)
        for snap_idx, pos in enumerate([pos0, pos1, pos2]):
            t = base_t + snap_idx * 3600  # 1-hour intervals
            snaps[snap_idx].append({
                "wialon_id": wid,
                "nm": name,
                "cls": 2, "mu": 0,
                "pos_t": t,
                "pos_y": pos["lat"],
                "pos_x": pos["lon"],
                "registration_plate": reg,
                "payload_json": f'{{"speed": {speed_kmh:.1f}}}',
            })
    return snaps


def gen_tasks(wells, rng: random.Random):
    tasks = []
    priorities = ["high", "medium", "medium", "low", "low"]
    shifts = ["day", "night"]
    for i in range(40):
        well = rng.choice(wells)
        priority = rng.choice(priorities)
        task_type = rng.choice(TASK_TYPES)
        duration = round(rng.uniform(2, 12), 1)
        # planned_start: next few days
        days_ahead = rng.randint(0, 6)
        hour = 8 if rng.random() < 0.7 else 20
        planned_start = f"2025-02-{20 + days_ahead:02d}T{hour:02d}:00:00"
        shift = "day" if hour == 8 else "night"
        tasks.append({
            "task_id": f"T-2025-{i+1:04d}",
            "priority": priority,
            "planned_start": planned_start,
            "planned_duration_hours": duration,
            "destination_uwi": well["uwi"],
            "task_type": task_type,
            "shift": shift,
            "start_day": f"2025-02-{20 + days_ahead:02d}",
        })
    return tasks


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drop-first", action="store_true", help="Drop and recreate all tables")
    ap.add_argument("--seed", type=int, default=42, help="Random seed")
    args = ap.parse_args()

    s = get_settings()
    log.info("Connecting to %s:%s/%s …", s.db_host, s.db_port, s.db_name)

    conn = psycopg2.connect(
        host=s.db_host, port=s.db_port,
        dbname=s.db_name, user=s.db_user, password=s.db_password,
    )
    conn.autocommit = False
    cur = conn.cursor()

    if args.drop_first:
        log.info("Dropping existing tables …")
        cur.execute(DROP_DDL)
        conn.commit()

    log.info("Creating schema and tables …")
    cur.execute(DDL)
    conn.commit()

    rng = random.Random(args.seed)

    # ── Nodes ──────────────────────────────────────────────────────────
    log.info("Generating road nodes …")
    nodes = gen_nodes(rng)
    cur.executemany(
        'INSERT INTO "references".road_nodes (node_id, lon, lat) VALUES (%(node_id)s, %(lon)s, %(lat)s) ON CONFLICT DO NOTHING',
        nodes,
    )
    log.info("  Inserted %d nodes", len(nodes))

    # ── Edges ──────────────────────────────────────────────────────────
    log.info("Generating road edges …")
    edges = gen_edges(nodes, rng)
    cur.executemany(
        'INSERT INTO "references".road_edges (source, target, weight) VALUES (%(source)s, %(target)s, %(weight)s)',
        edges,
    )
    log.info("  Inserted %d edges", len(edges))

    # ── Wells ──────────────────────────────────────────────────────────
    log.info("Generating wells …")
    wells = gen_wells(nodes, rng)
    cur.executemany(
        'INSERT INTO "references".wells (uwi, latitude, longitude, well_name) VALUES (%(uwi)s, %(latitude)s, %(longitude)s, %(well_name)s) ON CONFLICT DO NOTHING',
        wells,
    )
    log.info("  Inserted %d wells", len(wells))

    # ── Vehicles ───────────────────────────────────────────────────────
    log.info("Generating vehicle snapshots …")
    snaps = gen_vehicles(nodes, rng)
    for i, snap in enumerate(snaps, 1):
        cur.executemany(
            f'INSERT INTO "references".wialon_units_snapshot_{i} '
            "(wialon_id, nm, cls, mu, pos_t, pos_y, pos_x, registration_plate, payload_json) "
            "VALUES (%(wialon_id)s, %(nm)s, %(cls)s, %(mu)s, %(pos_t)s, %(pos_y)s, %(pos_x)s, %(registration_plate)s, %(payload_json)s) "
            "ON CONFLICT DO NOTHING",
            snap,
        )
    log.info("  Inserted %d vehicles × 3 snapshots", len(snaps[0]))

    # ── Tasks ──────────────────────────────────────────────────────────
    log.info("Generating tasks …")
    tasks = gen_tasks(wells, rng)
    cur.executemany(
        "INSERT INTO public.tasks (task_id, priority, planned_start, planned_duration_hours, destination_uwi, task_type, shift, start_day) "
        "VALUES (%(task_id)s, %(priority)s, %(planned_start)s, %(planned_duration_hours)s, %(destination_uwi)s, %(task_type)s, %(shift)s, %(start_day)s) "
        "ON CONFLICT DO NOTHING",
        tasks,
    )
    log.info("  Inserted %d tasks", len(tasks))

    conn.commit()
    cur.close()
    conn.close()
    log.info("Done! DB ready for testing.")

    # Print a few sample task IDs for convenience
    log.info("Sample task IDs: %s", ", ".join(t["task_id"] for t in tasks[:5]))
    log.info("Sample well UWIs: %s", ", ".join(w["uwi"] for w in wells[:5]))


if __name__ == "__main__":
    main()
