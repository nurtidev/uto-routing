"""
Orders adapter — reads task orders from dcm.records and maps to internal task format.

dcm.records with document_id=2 (TRS_ORDER) contains real work orders.
Key indicators:
  id=129  TRS_ORDER_WELL1C   — well description JSON  {'Description': 'G_4416/28'}
  id=128  TRS_ORDER_WKIND1C  — work type JSON         {'Description': 'Геофизические работы'}
  id=29   TRS_ORDER_HOURS    — planned duration (int, hours)
  id=14   TRS_ORDER_DATE     — work date
  id=13   TRS_ORDER_PRY      — priority list value (mostly NULL → default 'medium')
  id=11   TRS_ORDER_SHIFT    — shift (mostly NULL → default 'day')
"""
from __future__ import annotations

import difflib
import logging
import re
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Indicator IDs in dcm schema
_IND_WELL1C = 129
_IND_WKIND1C = 128
_IND_HOURS = 29
_IND_DATE = 14
_IND_PRY = 13
_IND_SHIFT = 11

# Priority mapping: dcm list codes → internal
_PRIORITY_MAP = {
    "high": "high",
    "average": "medium",
    "low": "low",
}

# Work type description → task_type (substring matching, lowercase)
_WKIND_MAP = {
    "цемент": "цементирование",
    "тампон": "тампонаж",
    "промывк": "промывка",
    "кислотн": "кислотная обработка",
    "перфора": "перфорация",
    "грп": "грп",
    "освоен": "освоение",
    "крс": "крс",
    "трс": "трс",
    "ремонт": "ремонт",
    "транспорт": "транспортировка",
    "вахт": "вахта",
    "геофиз": "геофизика",
    "каротаж": "геофизика",
    "диагн": "диагностика",
    "дефектоск": "диагностика",
}

# Module-level cache: well description → UWI
_well_uwi_cache: dict[str, Optional[str]] = {}


def _parse_well_desc(value_json_text: str) -> Optional[str]:
    """Extract well Description from the oddly-encoded JSON text."""
    m = re.search(r"Description['\"\s:]+([^'\"\\}]+)", str(value_json_text))
    return m.group(1).strip() if m else None


def _parse_wkind_desc(value_json_text: str) -> Optional[str]:
    """Extract work type Description from JSON text."""
    m = re.search(r"Description['\"\s:]+([^'\"\\}]+)", str(value_json_text))
    return m.group(1).strip() if m else None


def _normalize_task_type(wkind_desc: Optional[str]) -> Optional[str]:
    """Map work type description to internal task_type slug."""
    if not wkind_desc:
        return None
    lower = wkind_desc.lower()
    for key, task_type in _WKIND_MAP.items():
        if key in lower:
            return task_type
    return None


async def resolve_well_uwi(db: AsyncSession, well_desc: str) -> Optional[str]:
    """
    Map well description (e.g. 'G_4416/28 доб.') to references.wells.uwi.

    Strategy:
      1. Exact match on well_name.
      2. Prefix match: G_XXXX or XXXX (numeric part before '/').
      3. Fuzzy match via difflib.SequenceMatcher across candidate wells (ratio ≥ 0.60).
      4. Returns None if all strategies fail.
    """
    if well_desc in _well_uwi_cache:
        return _well_uwi_cache[well_desc]

    # Strategy 1: exact
    row = await db.execute(
        text('SELECT uwi FROM "references".wells WHERE well_name = :n AND longitude IS NOT NULL LIMIT 1'),
        {"n": well_desc},
    )
    r = row.fetchone()
    if r:
        _well_uwi_cache[well_desc] = r[0]
        return r[0]

    # Strategy 2: numeric prefix match
    num = re.sub(r"^G_", "", well_desc).split("/")[0].strip()
    if num and re.search(r"\d", num):
        row = await db.execute(
            text(
                'SELECT uwi FROM "references".wells '
                "WHERE well_name ILIKE :pat AND longitude IS NOT NULL LIMIT 1"
            ),
            {"pat": f"%{num}%"},
        )
        r = row.fetchone()
        if r:
            _well_uwi_cache[well_desc] = r[0]
            return r[0]

    # Strategy 3: fuzzy match using difflib (stdlib, no extra deps)
    # Extract alphanumeric tokens (length >= 2) and query candidate wells
    tokens = [t for t in re.findall(r"[A-Za-z0-9_/]+", well_desc) if len(t) >= 2]
    if tokens:
        conditions = " OR ".join(f"well_name ILIKE :tok_{i}" for i in range(len(tokens)))
        tok_params: dict = {f"tok_{i}": f"%{t}%" for i, t in enumerate(tokens)}
        tok_params["lim"] = 50
        cand_rows = await db.execute(
            text(
                f'SELECT uwi, well_name FROM "references".wells '
                f"WHERE ({conditions}) AND longitude IS NOT NULL LIMIT :lim"
            ),
            tok_params,
        )
        candidates = cand_rows.fetchall()

        if candidates:
            norm_desc = well_desc.lower().strip()
            best_uwi, best_ratio = None, 0.0
            for uwi, well_name in candidates:
                ratio = difflib.SequenceMatcher(
                    None, norm_desc, (well_name or "").lower().strip()
                ).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_uwi = uwi

            if best_ratio >= 0.60 and best_uwi:
                logger.info(
                    "Well '%s' fuzzy-matched → uwi=%s (ratio=%.2f)",
                    well_desc, best_uwi, best_ratio,
                )
                _well_uwi_cache[well_desc] = best_uwi
                return best_uwi

    logger.warning("Could not resolve well '%s' to UWI (all strategies failed)", well_desc)
    _well_uwi_cache[well_desc] = None
    return None


async def get_orders_as_tasks(
    db: AsyncSession,
    order_ids: list[str] | None = None,
) -> list[dict]:
    """
    Read orders from dcm.records and return them in task-row format:
      {task_id, priority, planned_start, planned_duration_hours,
       destination_uwi, task_type, shift, start_day}

    order_ids: list of r.number values (e.g. ['G000088', 'G000002']).
                If None — return all active orders that have a resolvable well.
    """
    # Build WHERE clause
    if order_ids:
        placeholders = ", ".join(f":id_{i}" for i in range(len(order_ids)))
        id_filter = f"AND r.number IN ({placeholders})"
        params: dict = {f"id_{i}": oid for i, oid in enumerate(order_ids)}
    else:
        id_filter = ""
        params = {}

    query = text(f"""
        SELECT
            r.id,
            r.number                                                   AS order_number,
            r.date                                                     AS created_at,
            MAX(CASE WHEN v.indicator_id = {_IND_WELL1C}  THEN v.value_json::text END)  AS well_json,
            MAX(CASE WHEN v.indicator_id = {_IND_WKIND1C} THEN v.value_json::text END)  AS wkind_json,
            MAX(CASE WHEN v.indicator_id = {_IND_HOURS}   THEN v.value_int END)         AS planned_hours,
            MAX(CASE WHEN v.indicator_id = {_IND_DATE}    THEN v.value_datetime END)    AS work_date,
            MAX(CASE WHEN v.indicator_id = {_IND_PRY}     THEN v.value_str END)         AS priority_code,
            MAX(CASE WHEN v.indicator_id = {_IND_SHIFT}   THEN v.value_str END)         AS shift_code
        FROM dcm.records r
        JOIN dcm.record_indicator_values v ON v.record_id = r.id
        WHERE r.document_id = 2 AND r.is_deleted = FALSE
          {id_filter}
        GROUP BY r.id, r.number, r.date
        HAVING MAX(CASE WHEN v.indicator_id = {_IND_WELL1C} THEN v.value_json::text END) IS NOT NULL
        ORDER BY r.id
    """)

    result = await db.execute(query, params)
    rows = result.mappings().all()

    tasks: list[dict] = []
    for row in rows:
        well_desc = _parse_well_desc(row["well_json"] or "")
        if not well_desc:
            continue

        uwi = await resolve_well_uwi(db, well_desc)
        if uwi is None:
            logger.debug("Order %s: no UWI for well '%s', skipping", row["order_number"], well_desc)
            continue

        priority_code = row.get("priority_code") or "average"
        priority = _PRIORITY_MAP.get(priority_code, "medium")

        wkind_desc = _parse_wkind_desc(row["wkind_json"] or "")
        task_type = _normalize_task_type(wkind_desc)

        work_dt = row.get("work_date")
        if work_dt:
            start_day = work_dt.date() if hasattr(work_dt, "date") else date.today()
            planned_start = datetime(start_day.year, start_day.month, start_day.day,
                                     8, 0, 0, tzinfo=timezone.utc)
        else:
            start_day = date.today()
            planned_start = datetime.now(tz=timezone.utc).replace(hour=8, minute=0,
                                                                   second=0, microsecond=0)

        shift_code = row.get("shift_code") or "change_2"
        shift = "night" if shift_code == "change_1" else "day"

        planned_hours = float(row.get("planned_hours") or 4)

        tasks.append({
            "task_id": row["order_number"],
            "priority": priority,
            "planned_start": planned_start,
            "planned_duration_hours": planned_hours,
            "destination_uwi": uwi,
            "task_type": task_type,
            "shift": shift,
            "start_day": start_day,
        })

    return tasks
