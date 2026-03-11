# ИС УТО — Intelligent Special Vehicle Routing System

> **Hackathon:** Astana Hub | VRP routing for oilfield special vehicles (Жетыбай field, pilot 126 vehicles)

**Problem:** Manual dispatcher routing → excessive idle mileage, slow response to urgent tasks, no automated ETA/route calculation.

**Solution:** Multi-Depot VRPTW (Vehicle Routing Problem with Time Windows) — each vehicle starts from its current GPS position, serves multiple tasks per shift, routes follow the real road graph.

---

## Quick Start

```bash
# 1. Clone & enter
git clone <repo-url>
cd uto-routing

# 2. Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure DB (hackathon DB is pre-configured in .env)
# DB_HOST=95.47.96.41, DB_NAME=mock_uto — read-only access to real data

# 5. Run
uvicorn app.main:app --reload --port 8000
```

**Swagger UI:** http://localhost:8000/docs
**Health check:** http://localhost:8000/health
**Interactive map:** http://localhost:8000/

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET`  | `/api/orders` | List available order IDs from the real DB |
| `POST` | `/api/recommendations` | Top-3 vehicle candidates for a task |
| `POST` | `/api/route` | Shortest road-graph route between two points |
| `POST` | `/api/multitask` | Optimal multi-stop task grouping with savings % |
| `POST` | `/api/batch` | Full VRPTW batch optimizer — assign all orders to fleet |
| `GET`  | `/health` | Service health + graph stats |

---

## Live Examples (real data from DB)

### 1. List available orders
```bash
curl http://localhost:8000/api/orders
# Returns real orders from dcm.records: G000002, G000004, G000005 ...
```

### 2. Top-3 vehicle recommendations for order G000002 (well G_4416/28)
```bash
curl -X POST http://localhost:8000/api/recommendations \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "G000002",
    "priority": "medium",
    "destination_uwi": "JET_4416",
    "planned_start": "2025-07-30T08:00:00",
    "duration_hours": 12
  }'
```

**Response:**
```json
{
  "task_id": "G000002",
  "units": [
    {
      "wialon_id": 26456213,
      "name": "BPA_Hyundai Universe 012OB12",
      "eta_minutes": 0.2,
      "distance_km": 0.15,
      "score": 1.0,
      "reason": "Совместима по типу работ; свободна прямо сейчас; очень близко (0.2 км); укладывается в SLA с запасом."
    }
  ],
  "baseline": { "approach": "nearest_free", "distance_km": 0.15 }
}
```

### 3. Shortest route between two points
```bash
curl -X POST http://localhost:8000/api/route \
  -H "Content-Type: application/json" \
  -d '{
    "from": {"lon": 56.10, "lat": 46.65},
    "to":   {"lon": 55.82, "lat": 46.70}
  }'
```

### 4. Multi-stop grouping (can these orders share one vehicle?)
```bash
curl -X POST http://localhost:8000/api/multitask \
  -H "Content-Type: application/json" \
  -d '{
    "task_ids": ["G000004", "G000005", "G000006"],
    "constraints": {"max_total_time_minutes": 480, "max_detour_ratio": 1.3}
  }'
```

### 5. Batch VRPTW optimizer (full fleet assignment)
```bash
curl -X POST http://localhost:8000/api/batch \
  -H "Content-Type: application/json" \
  -d '{
    "task_ids": ["G000004", "G000005", "G000006", "G000007", "G000008"],
    "time_limit_seconds": 30,
    "use_greedy_baseline": true
  }'
# Returns per-vehicle routes + savings_percent vs greedy baseline
```

---

## Architecture

```
POST /api/recommendations
POST /api/batch          ──→  app/api/
POST /api/multitask             │
POST /api/route                 │
GET  /api/orders                │
                                ▼
                    ┌─────────────────────────────┐
                    │  app/core/                  │
                    │  graph_loader.py  ← road_nodes, road_edges  │
                    │  graph_service.py            │  (PostgreSQL)
                    │  fleet_state.py   ← wialon_units_snapshot_*│
                    │  orders.py        ← dcm.records (TRS_ORDER) │
                    │  scoring.py                  │
                    │  optimizer.py  (OR-Tools)    │
                    │  multitask_solver.py         │
                    └─────────────────────────────┘
```

**Data sources (PostgreSQL `mock_uto`):**
- `references.road_nodes` — 4,624 road graph vertices
- `references.road_edges` — 19,031 road edges (weights in meters)
- `references.wells` — 3,450 wells with UWI codes and coordinates
- `references.wialon_units_snapshot_1/2/3` — 126 vehicles with GPS positions
- `dcm.records` (document TRS_ORDER) — 120 real work orders with well/work-type info

---

## Algorithm

### Scoring Formula (official, from hackathon TZ slide 7)
```
score = 0.30 × (1 − norm_distance)
      + 0.30 × (1 − norm_eta)
      + 0.15 × (1 − norm_idle)
      + 0.25 × (1 − norm_sla_penalty)
```
Where all components are min-max normalised across candidates:
- `norm_distance` — road-graph distance (Dijkstra) to destination well
- `norm_eta` — travel time + vehicle idle wait
- `norm_idle` — time until vehicle is free
- `norm_sla_penalty` — `max(0, eta − deadline) / deadline` — SLA overshoot

**SLA deadlines:** high priority +2h, medium +5h, low +12h

### Route Finding
1. KD-Tree O(log N) snap of GPS coordinates → nearest road graph node
2. Dijkstra shortest path on directed weighted graph (NetworkX)
3. Open-end routes — vehicles stay at last task location

### Batch Optimization (VRPTW)
- **OR-Tools CP-SAT** with time windows, capacity constraints, multi-depot
- Greedy baseline for comparison → `savings_percent` shows improvement
- Soft penalties for SLA deadline violations (priority-weighted)

---

## Tech Stack

| Layer | Technology |
|---|---|
| API | FastAPI (async) |
| Graph | NetworkX + scipy KD-Tree |
| Optimizer | Google OR-Tools (VRPTW) |
| DB | PostgreSQL + SQLAlchemy async |
| Validation | Pydantic v2 |
| LLM reasons | Anthropic Claude Haiku (optional, fallback to template) |
| Frontend | Leaflet.js interactive map |

---

## Project Structure

```
app/
├── main.py                  # FastAPI app + lifespan startup
├── config.py                # Settings (pydantic-settings + .env)
├── db.py                    # Async SQLAlchemy engine
├── api/
│   ├── recommendations.py   # POST /api/recommendations
│   ├── route.py             # POST /api/route
│   ├── multitask.py         # POST /api/multitask
│   └── batch.py             # POST /api/batch + GET /api/orders
├── core/
│   ├── graph_loader.py      # Road graph + KD-Tree (loads from DB at startup)
│   ├── graph_service.py     # High-level facade (snap, Dijkstra, bbox)
│   ├── fleet_state.py       # Vehicle availability from Wialon snapshots
│   ├── orders.py            # Adapter: dcm.records → task format
│   ├── scoring.py           # Composite scoring formula
│   ├── optimizer.py         # OR-Tools VRPTW batch solver
│   ├── multitask_solver.py  # Greedy multi-stop grouping
│   ├── compatibility.py     # Vehicle-task type matching
│   └── llm_reason.py        # LLM-generated natural language explanation
└── models/
    ├── requests.py          # Pydantic request schemas
    └── responses.py         # Pydantic response schemas
frontend/
└── index.html               # Leaflet.js interactive map
```

---

## Environment Variables

```env
DB_HOST=95.47.96.41          # Hackathon PostgreSQL host
DB_PORT=5432
DB_NAME=mock_uto
DB_USER=readonly_user
DB_PASSWORD=...

DEFAULT_AVG_SPEED_KMH=40     # Fallback vehicle speed

ANTHROPIC_API_KEY=...        # Optional — enables LLM reason generation
```
