# ИС УТО — Intelligent Special Vehicle Routing System

> **Hackathon:** Astana Hub | Vehicle Routing Problem (VRPTW) for oilfield special vehicles

---

## Quick Start

```bash
# 1. Clone & enter
git clone <repo-url>
cd uto-routing

# 2. Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure DB connection
cp .env.example .env
# Edit .env — fill in DB_HOST, DB_NAME, DB_USER, DB_PASSWORD

# 5. Run
uvicorn app.main:app --reload --port 8000
```

**Swagger UI:** http://localhost:8000/docs  
**Health check:** http://localhost:8000/health

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/recommendations` | Top-3 vehicle candidates for a task |
| `POST` | `/api/route` | Shortest road-graph route between two points |
| `POST` | `/api/multitask` | Optimal multi-stop task grouping |
| `GET`  | `/health` | Service health + graph stats |

### Example: Recommendations
```bash
curl -X POST http://localhost:8000/api/recommendations \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "T-2025-0042",
    "priority": "high",
    "destination_uwi": "05-1234-567",
    "planned_start": "2025-02-20T08:00:00",
    "duration_hours": 4.5
  }'
```

### Example: Route
```bash
curl -X POST http://localhost:8000/api/route \
  -H "Content-Type: application/json" \
  -d '{
    "from": {"wialon_id": 10234, "lon": 68.12345, "lat": 51.67890},
    "to":   {"uwi": "05-1234-567", "lon": 68.09100, "lat": 51.70450}
  }'
```

### Example: Multitask grouping
```bash
curl -X POST http://localhost:8000/api/multitask \
  -H "Content-Type: application/json" \
  -d '{
    "task_ids": ["T-2025-0042", "T-2025-0043", "T-2025-0044"],
    "constraints": {"max_total_time_minutes": 480, "max_detour_ratio": 1.3}
  }'
```

---

## Project Structure

```
app/
├── main.py              # FastAPI app + lifespan startup
├── config.py            # Settings (pydantic-settings + .env)
├── db.py                # Async SQLAlchemy engine
├── api/                 # Route handlers
│   ├── recommendations.py
│   ├── route.py
│   └── multitask.py
├── core/                # Business logic
│   ├── graph_loader.py  # Graph + KD-Tree (loads from DB)
│   ├── graph_service.py # High-level graph operations
│   ├── fleet_state.py   # Vehicle availability from Wialon snapshots
│   ├── optimizer.py     # OR-Tools VRPTW batch solver
│   ├── scoring.py       # Composite scoring formula
│   └── multitask_solver.py  # Greedy task grouping
└── models/
    ├── requests.py      # Pydantic request schemas
    └── responses.py     # Pydantic response schemas
```

---

## Tech Stack

- **FastAPI** — async REST API
- **NetworkX** — road graph + Dijkstra shortest paths
- **scipy KD-Tree** — O(log N) coordinate → node map-matching
- **Google OR-Tools** — VRPTW batch optimization
- **SQLAlchemy (async)** — PostgreSQL access
- **Pydantic v2** — request/response validation

---

## Docs

See [`docs/`](docs/) for:
- [`hackathon_analysis.md`](docs/hackathon_analysis.md) — full task analysis, algorithm choices, scoring formula
