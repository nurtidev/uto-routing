# ИС УТО — Интеллектуальная система маршрутизации спецтехники

> **Хакатон:** Astana Hub | Месторождение Жетыбай (Мангистауская обл.) | Пилот: 126 машин, 120 заявок

---

## Что делает система

**Проблема:** Диспетчер вручную назначает машины на заявки — это медленно, дорого и неточно. Машины гоняют вхолостую, опаздывают на скважины.

**Решение:** Система автоматически подбирает лучшую машину за секунды, умеет объединять несколько задач в один рейс и оптимально распределяет все заявки смены по всему парку.

### 3 сценария работы

| Сценарий | Что происходит |
|---|---|
| Пришла срочная заявка | Система смотрит GPS всех 126 машин, считает кратчайший путь по графу дорог и выдаёт **топ-3 лучших машины** с обоснованием (расстояние, ETA, тип работ, SLA) |
| Несколько заявок рядом | Система проверяет — **стоит ли объединить** их в один рейс с учётом крюка и ограничений по времени |
| Планирование смены | **OR-Tools (Google)** назначает все заявки дня на весь парк оптимально — с учётом временных окон смен, приоритетов и расстояний |

---

## Быстрый старт

```bash
git clone <repo>
cd uto-routing

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload --port 8000
```

При старте система автоматически загружает граф дорог и GPS-позиции машин из БД.

- **Интерактивная карта:** http://localhost:8000/
- **Swagger UI:** http://localhost:8000/docs
- **Health check:** http://localhost:8000/health

> `.env` с реквизитами БД уже преднастроен (PostgreSQL БД организаторов `mock_uto`, read-only доступ).

---

## Живые примеры (реальные данные)

### 1. Список доступных заявок из БД

```bash
curl http://localhost:8000/api/orders
```

Возвращает 55 реальных заявок из `dcm.records` (документ TRS_ORDER):
```json
[
  {"task_id": "G000002", "destination_uwi": "JET_4416", "priority": "medium", "task_type": "геофизика"},
  {"task_id": "G000033", "destination_uwi": "JET_4555", "priority": "medium", "task_type": null}
]
```

---

### 2. Топ-3 машины для заявки G000002 (скважина JET_4416)

```bash
curl -X POST http://localhost:8000/api/recommendations \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "G000002",
    "priority": "medium",
    "destination_uwi": "JET_4416",
    "planned_start": "2025-07-30T08:00:00",
    "duration_hours": 4
  }'
```

**Ответ:**
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
      "free_at_minutes": 0.0,
      "compatible": true,
      "reason": "Совместима по типу работ; свободна прямо сейчас; очень близко (0.2 км); укладывается в SLA с запасом."
    },
    {
      "wialon_id": 26456872,
      "name": "BPA_Toyota Coaster 790BU12",
      "eta_minutes": 5.1,
      "distance_km": 3.39,
      "score": 0.998
    }
  ],
  "baseline": {"approach": "nearest_free", "distance_km": 0.15}
}
```

Система сравнивает с **baseline** (ближайшая свободная) — наш алгоритм дополнительно учитывает SLA, тип работ и занятость.

---

### 3. Кратчайший маршрут по графу дорог

```bash
curl -X POST http://localhost:8000/api/route \
  -H "Content-Type: application/json" \
  -d '{
    "from": {"lon": 56.10, "lat": 46.65},
    "to":   {"lon": 55.82, "lat": 46.70}
  }'
```

Возвращает геометрию маршрута (список координат) + расстояние в км + время в минутах.

---

### 4. Можно ли объединить заявки в один рейс?

```bash
curl -X POST http://localhost:8000/api/multitask \
  -H "Content-Type: application/json" \
  -d '{
    "task_ids": ["G000004", "G000005", "G000006"],
    "constraints": {"max_total_time_minutes": 480, "max_detour_ratio": 1.3}
  }'
```

Система считает пары расстояний и решает: стоит ли объединять с учётом крюка (≤1.3×) и ограничения по времени смены.

---

### 5. Оптимальное назначение всех заявок смены (VRPTW)

```bash
curl -X POST http://localhost:8000/api/batch \
  -H "Content-Type: application/json" \
  -d '{
    "task_ids": ["G000004", "G000005", "G000006", "G000007", "G000008"],
    "time_limit_seconds": 30,
    "use_greedy_baseline": true
  }'
```

OR-Tools назначает каждую заявку на конкретную машину с учётом временных окон смены (08:00–20:00). Возвращает `savings_percent` — экономия vs жадный baseline.

```json
{
  "solver_status": "optimal",
  "total_distance_km": 25.56,
  "routes": [
    {
      "vehicle_name": "BPA_Hyundai Universe 012OB12",
      "steps": [{"task_id": "G000004", "arrival_minutes": 480.0, "departure_minutes": 1200.0}],
      "total_distance_km": 0.15
    }
  ],
  "unassigned_tasks": [],
  "savings_percent": 0.0
}
```

---

## Архитектура

```
HTTP запрос
    │
    ▼
app/api/                   ← FastAPI роутеры
    │
    ▼
app/core/
  ├── graph_loader.py      — граф дорог + KD-Tree (грузится из БД при старте)
  ├── graph_service.py     — Dijkstra, snap координат → ближайший узел
  ├── fleet_state.py       — GPS позиции 126 машин из Wialon snapshots
  ├── orders.py            — реальные заявки из dcm.records (ЭДО)
  ├── scoring.py           — формула оценки кандидатов (официальная из ТЗ)
  ├── optimizer.py         — OR-Tools VRPTW (пакетный оптимизатор)
  └── multitask_solver.py  — жадная группировка задач

    │
    ▼
PostgreSQL — mock_uto @ 95.47.96.41 (БД организаторов)
  references.road_nodes          — 4 624 узла графа дорог
  references.road_edges          — 19 031 рёбер (вес = метры)
  references.wells               — 3 450 скважин с координатами
  references.wialon_units_snapshot_1/2/3  — 126 машин с GPS позициями
  dcm.records (TRS_ORDER)        — 120 реальных рабочих заявок
```

---

## Алгоритм скоринга (формула из ТЗ, слайд 7)

```
score = 0.30 × (1 − norm_distance)
      + 0.30 × (1 − norm_eta)
      + 0.15 × (1 − norm_idle)
      + 0.25 × (1 − norm_sla_penalty)
```

- `norm_distance` — расстояние по графу дорог (Dijkstra), нормированное min-max
- `norm_eta` — ETA = время в пути + ожидание освобождения
- `norm_idle` — время до освобождения машины (0 если свободна)
- `norm_sla_penalty` — `max(0, eta − deadline) / deadline` | SLA: high +2ч, medium +5ч, low +12ч

### Поиск маршрута

1. KD-Tree snap GPS → ближайший узел графа за O(log N)
2. Dijkstra по взвешенному ориентированному графу (NetworkX)
3. Open-end маршруты — машина остаётся у последней задачи

### Пакетная оптимизация (VRPTW)

- **Google OR-Tools CP-SAT** с временными окнами, multi-depot, мягкими штрафами
- Жадный baseline для сравнения → `savings_percent`

---

## Стек технологий

| Слой | Технология |
|---|---|
| API | FastAPI (async) |
| Граф | NetworkX + scipy KD-Tree |
| Оптимизатор | Google OR-Tools (VRPTW) |
| БД | PostgreSQL + SQLAlchemy async |
| Валидация | Pydantic v2 |
| LLM объяснения | Anthropic Claude Haiku (fallback → шаблон) |
| Карта | Leaflet.js |

---

## Переменные окружения

```env
DB_HOST=95.47.96.41       # PostgreSQL БД организаторов
DB_PORT=5432
DB_NAME=mock_uto
DB_USER=readonly_user
DB_PASSWORD=...

DEFAULT_AVG_SPEED_KMH=40
ANTHROPIC_API_KEY=...     # опционально — включает LLM-объяснения
```
