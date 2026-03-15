# ИС УТО — Интеллектуальная система маршрутизации спецтехники

> **Хакатон:** Astana Hub | Месторождение Жетыбай (Мангистауская обл.) | Пилот: 126 машин, 55 заявок

## Живое демо

| Ссылка | Описание |
|---|---|
| **[uto-routing-production.up.railway.app](https://uto-routing-production.up.railway.app)** | Интерактивная карта |
| **[uto-routing-production.up.railway.app/docs](https://uto-routing-production.up.railway.app/docs)** | Swagger UI — все API |
| **[uto-routing-production.up.railway.app/health](https://uto-routing-production.up.railway.app/health)** | Health check |

> Данные — реальная БД организаторов (`mock_uto` @ 95.47.96.41), read-only доступ.

---

## Что делает система

**Проблема:** Диспетчер вручную назначает машины на заявки — это медленно, дорого и неточно. Машины гоняют вхолостую, опаздывают на скважины.

**Решение:** Система автоматически подбирает лучшую машину за секунды, умеет объединять несколько задач в один рейс и оптимально распределяет все заявки смены по всему парку.

### Оценочный экономический эффект (55 заявок/смена)

| Метрика | Baseline (ручной/greedy) | ИС УТО | Экономия |
|---|---|---|---|
| Пробег | ~825 км/смена | ~644 км/смена | **−181 км (22%)** |
| Топливо | ~124 л | ~97 л | **−27 л** |
| Стоимость | ~31 000 тг | ~24 000 тг | **~7 000 тг/смена** |
| Время диспетчера | ~165 мин (3 мин × 55) | **< 1 мин** | **−164 мин** |

---

## 4 сценария работы

| Сценарий | Endpoint | Что происходит |
|---|---|---|
| Срочная заявка | `POST /api/recommendations` | Система смотрит GPS 126 машин, считает кратчайший путь по графу дорог и выдаёт **топ-3 лучших машины** с обоснованием (ETA, SLA, тип работ) |
| Несколько заявок рядом | `POST /api/multitask` | Проверяет — стоит ли объединить их в один рейс (крюк ≤1.3×, ограничение по времени смены) |
| Планирование смены | `POST /api/batch` | **OR-Tools VRPTW** назначает все заявки оптимально — с временными окнами, приоритетами и расстояниями |
| KPI дашборд | `GET /api/stats` | Текущие показатели: флот, заявки, SLA compliance, оценка экономии |

---

## Быстрый старт (локально)

```bash
git clone <repo>
cd uto-routing

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload --port 8000
```

При старте система автоматически загружает граф дорог (4 624 узла, 19 031 рёбер) и GPS-позиции машин из БД.

> `.env` с реквизитами БД уже преднастроен — подключается к БД организаторов автоматически.

---

## API — живые примеры

### 1. KPI дашборд

```bash
curl https://uto-routing-production.up.railway.app/api/stats
```

```json
{
  "vehicle_count": 116,
  "free_vehicle_count": 114,
  "order_count": 55,
  "sla_compliance_pct": 94.1,
  "estimated_savings_km": 181.5,
  "estimated_savings_tenge": 6806.0,
  "manual_dispatch_hours_saved": 2.8
}
```

---

### 2. Список заявок из БД

```bash
curl https://uto-routing-production.up.railway.app/api/orders
```

Возвращает реальные заявки из `dcm.records` (TRS_ORDER):
```json
[
  {"task_id": "G000002", "destination_uwi": "JET_4416", "priority": "medium", "task_type": "геофизика"},
  {"task_id": "G000033", "destination_uwi": "JET_4555", "priority": "medium", "task_type": null}
]
```

---

### 3. Топ-3 машины для заявки

```bash
curl -X POST https://uto-routing-production.up.railway.app/api/recommendations \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "G000002",
    "priority": "medium",
    "destination_uwi": "JET_4416",
    "planned_start": "2025-07-30T08:00:00",
    "duration_hours": 4
  }'
```

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
      "reason": "Свободна прямо сейчас; очень близко (0.2 км); укладывается в SLA с запасом (ETA 0 мин)."
    }
  ],
  "baseline": {"approach": "nearest_free", "distance_km": 0.15}
}
```

Система сравнивает с **baseline** (ближайшая свободная) — наш алгоритм учитывает SLA, тип работ и занятость.

---

### 4. Кратчайший маршрут по графу дорог

```bash
curl -X POST https://uto-routing-production.up.railway.app/api/route \
  -H "Content-Type: application/json" \
  -d '{
    "from": {"lon": 56.10, "lat": 46.65},
    "to":   {"lon": 55.82, "lat": 46.70}
  }'
```

Возвращает геометрию маршрута (список координат) + расстояние в км + время в минутах.

---

### 5. Объединение заявок в один рейс

```bash
curl -X POST https://uto-routing-production.up.railway.app/api/multitask \
  -H "Content-Type: application/json" \
  -d '{
    "task_ids": ["G000004", "G000005", "G000006"],
    "constraints": {"max_total_time_minutes": 480, "max_detour_ratio": 1.3}
  }'
```

Пример ответа (заявки рядом — выгодно объединить):
```json
{
  "groups": [["G000004", "G000006"], ["G000005"]],
  "strategy_summary": "mixed",
  "total_distance_km": 18.4,
  "total_time_minutes": 138.0,
  "baseline_distance_km": 31.2,
  "baseline_time_minutes": 234.0,
  "savings_percent": 41.0,
  "reason": "Заявки G000004, G000006 объединены в один выезд — близкое расположение точек назначения. Заявки G000005 обслуживаются отдельно — территориально удалены или нарушают ограничение крюка. Итоговая экономия: 12.8 км."
}
```

Поля `baseline_distance_km` / `baseline_time_minutes` — суммарные показатели при раздельном обслуживании. `savings_percent` — процент экономии предложенной группировки.

---

### 6. Пакетное планирование смены (VRPTW)

```bash
curl -X POST https://uto-routing-production.up.railway.app/api/batch \
  -H "Content-Type: application/json" \
  -d '{
    "task_ids": ["G000004", "G000005", "G000006", "G000007", "G000008"],
    "time_limit_seconds": 30,
    "use_greedy_baseline": true
  }'
```

OR-Tools назначает каждую заявку на конкретную машину с учётом временных окон смены (08:00–20:00). Возвращает `savings_percent` — экономия vs жадный baseline.

---

### 7. Обновление GPS-позиций флота

```bash
curl -X POST https://uto-routing-production.up.railway.app/api/fleet/refresh
```

```json
{"vehicle_count": 116, "message": "Fleet reloaded: 116 vehicles available."}
```

---

## Демонстрация 3 сценариев (ТЗ п. 10.2)

### Сценарий 1 — Срочная заявка (high priority)

```bash
curl -X POST https://uto-routing-production.up.railway.app/api/recommendations \
  -H "Content-Type: application/json" \
  -d '{"task_id":"G000002","priority":"high","destination_uwi":"JET_4416","planned_start":"2025-07-30T08:00:00","duration_hours":4}'
```

Система выбирает машину с SLA-штрафом 0 (ETA < 2ч), показывает маршрут по графу. Baseline (ближайшая свободная) — для сравнения.

### Сценарий 2 — Сравнение baseline vs оптимизированный (medium priority)

| Подход | ETA, мин | Расстояние, км | SLA |
|---|---|---|---|
| Baseline (nearest_free) | — | ближайшая | не учитывает занятость |
| ИС УТО | учтена занятость | по графу дорог | штраф при превышении +5ч |

Разница видна в поле `baseline` в ответе `/api/recommendations` — топ-1 нашего алгоритма часто не совпадает с baseline-машиной из-за учёта SLA и занятости.

### Сценарий 3 — Многозадачность: 3 заявки в одном районе

```bash
curl -X POST https://uto-routing-production.up.railway.app/api/multitask \
  -H "Content-Type: application/json" \
  -d '{"task_ids":["G000004","G000005","G000006"],"constraints":{"max_total_time_minutes":480,"max_detour_ratio":1.3}}'
```

Если заявки территориально близки — система возвращает `strategy_summary: "single_unit"` или `"mixed"` с `savings_percent > 0`. Если разнесены — `"separate"`.

---

## Архитектура

```
HTTP запрос
    │
    ▼
app/api/
  ├── recommendations.py  — POST /api/recommendations
  ├── batch.py            — POST /api/batch, GET /api/vehicles, /api/orders, /api/wells
  ├── fleet.py            — POST /api/fleet/refresh, GET /api/stats
  ├── route.py            — POST /api/route
  └── multitask.py        — POST /api/multitask
    │
    ▼
app/core/
  ├── graph_loader.py     — граф дорог + KD-Tree (грузится из БД при старте)
  ├── graph_service.py    — Dijkstra, snap координат → ближайший узел
  ├── fleet_state.py      — GPS позиции 126 машин + занятость из dcm.records
  ├── orders.py           — реальные заявки из dcm.records (ЭДО)
  ├── scoring.py          — формула оценки кандидатов (официальная из ТЗ)
  ├── optimizer.py        — OR-Tools VRPTW (пакетный оптимизатор)
  └── multitask_solver.py — жадная группировка задач
    │
    ▼
PostgreSQL — mock_uto @ 95.47.96.41 (БД организаторов)
  references.road_nodes          — 4 624 узла графа дорог
  references.road_edges          — 19 031 рёбер (вес = метры)
  references.wells               — 3 450 скважин с координатами
  references.wialon_units_snapshot_1/2/3  — 126 машин с GPS позициями
  dcm.records (TRS_ORDER)        — 120 рабочих заявок
```

---

## Алгоритм скоринга (формула из ТЗ, слайд 7)

```
score = 0.30 × (1 − norm_distance)
      + 0.30 × (1 − norm_eta)
      + 0.15 × (1 − norm_idle)
      + 0.25 × (1 − norm_sla_penalty)
```

| Компонент | Описание |
|---|---|
| `norm_distance` | Расстояние по графу дорог (Dijkstra), нормированное min-max |
| `norm_eta` | ETA = время в пути + ожидание освобождения машины |
| `norm_idle` | Время до освобождения (0 если машина свободна прямо сейчас) |
| `norm_sla_penalty` | `max(0, eta − deadline) / deadline` · SLA: high +2ч, medium +5ч, low +12ч |

### Поиск маршрута

1. KD-Tree snap GPS → ближайший узел графа за O(log N)
2. Dijkstra по взвешенному ориентированному графу (NetworkX)
3. Open-end маршруты — машина остаётся у последней задачи (не возвращается на базу)

### Пакетная оптимизация (VRPTW)

- **Google OR-Tools** с временными окнами, multi-depot, мягкими штрафами по приоритетам
- Жадный baseline для сравнения → `savings_percent`
- Стратегия поиска: PATH_CHEAPEST_ARC + Guided Local Search

---

## Стек технологий

| Слой | Технология |
|---|---|
| API | FastAPI (async) |
| Граф | NetworkX + scipy KD-Tree |
| Оптимизатор | Google OR-Tools (VRPTW) |
| БД | PostgreSQL + SQLAlchemy async (asyncpg) |
| Валидация | Pydantic v2 |
| LLM объяснения | Anthropic Claude Haiku (fallback → шаблон) |
| Карта | Leaflet.js |
| Deploy | Railway |

---

## Переменные окружения

```env
DB_HOST=95.47.96.41
DB_PORT=5432
DB_NAME=mock_uto
DB_USER=readonly_user
DB_PASSWORD=...

DEFAULT_AVG_SPEED_KMH=40
ANTHROPIC_API_KEY=...     # опционально — включает LLM-объяснения
```

---

## Методология расчёта экономии

Оценка сделана на 55 заявках из `dcm.records` (TRS_ORDER) для одной дневной смены (08:00–20:00).

**Baseline:** жадный алгоритм — каждая заявка назначается на ближайшую свободную совместимую машину по прямой. Реализован в `optimizer.py → solve_greedy_baseline()`.

**ИС УТО:** OR-Tools VRPTW с временными окнами, soft-штрафами по приоритету и учётом занятости.

| Параметр | Значение |
|---|---|
| Заявок в смене | 55 |
| Машин в парке | 116 |
| Пробег baseline (greedy) | ~825 км |
| Пробег ИС УТО (VRPTW) | ~644 км |
| Экономия пробега | −181 км (22%) |
| Расход топлива (12 л/100 км) | −27 л |
| Стоимость топлива (~260 тг/л) | ~7 000 тг/смена |
| Время диспетчера (3 мин × 55) | −164 мин/смену |

Цифры получены через `GET /api/stats` на живых данных БД организаторов. Поле `estimated_savings_km` в ответе вычисляется динамически при каждом запросе сравнением VRPTW-плана с жадным baseline.

---

## Ограничения текущего прототипа и план развития

### GPS-позиции машин

Снапшоты Wialon в предоставленной БД (`wialon_units_snapshot_1/2/3`) содержат обезличенные координаты, которые не совпадают с bbox графа дорог месторождения. Система детектирует это автоматически и распределяет машины по узлам графа детерминированно (`node_at_index(wialon_id × 7919)`), что позволяет корректно работать алгоритму при демонстрации.

**В production-интеграции** с реальным Wialon GPS-координаты машин совпадают с координатами графа — `snap_to_node(lon, lat)` работает через KD-Tree без fallback.

### Обновление GPS в реальном времени

Сейчас `POST /api/fleet/refresh` обновляет снапшот вручную. В production возможны два варианта:
- **Polling:** cron-задача каждые 5–10 минут вызывает `/api/fleet/refresh`
- **Push:** Wialon поддерживает webhook-уведомления о смене позиции — один endpoint принимает событие и вызывает `get_fleet_state(force_reload=True)`

Оба варианта не требуют изменений архитектуры — только добавление планировщика или webhook-handler.

### Тесты

```bash
pytest tests/ -v          # 83 теста: scoring, shortest_path, graph_loader, compatibility, multitask_solver
pytest tests/ --cov=app   # с покрытием
```
