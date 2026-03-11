"""
Vehicle-task compatibility dictionary.

Maps task_type keywords → vehicle name patterns (case-insensitive substring match).
If a vehicle name matches any pattern for a task_type group, it gains that skill.

If a vehicle matches NO patterns at all → empty skills list → compatible with everything
(open fleet assumption: unrecognised vehicles are treated as general-purpose).
"""
from __future__ import annotations

# task_type (as it appears in tasks.destination_uwi / task_type field) →
# list of substrings to look for in vehicle name (nm field from Wialon)
TASK_TYPE_TO_VEHICLE_PATTERNS: dict[str, list[str]] = {
    # Цементирование / тампонаж
    "цементирование": ["ЦА", "ЦЕМЕНТ", "УНБ"],
    "тампонаж":       ["ЦА", "ЦЕМЕНТ", "УНБ"],
    # Промывка / кислотная обработка
    "промывка":           ["АЦН", "ЦА", "НА-", "НАСОС", "АГРЕГАТ"],
    "кислотная обработка":["АЦН", "АГРЕГАТ", "НА-"],
    # Перфорация / прострелочные работы
    "перфорация":     ["ПАВ", "АГРЕГАТ", "АЦН", "ПОДЪЁМНИК", "ПОДЪЕМНИК"],
    # ГРП
    "грп":            ["ГРП", "НАСОС", "АГРЕГАТ"],
    # Освоение скважины
    "освоение":       ["АЦН", "АГРЕГАТ", "ПОДЪЁМНИК", "ПОДЪЕМНИК"],
    # Капитальный / текущий ремонт
    "крс":            ["ПОДЪЁМНИК", "ПОДЪЕМНИК", "А-50", "А-60", "КМУ"],
    "трс":            ["АЦН", "АГРЕГАТ", "ПОДЪЁМНИК", "ПОДЪЕМНИК"],
    "ремонт":         ["ПОДЪЁМНИК", "ПОДЪЕМНИК", "КМУ", "КРАН", "А-50"],
    # Транспортировка / вспомогательные
    "транспортировка":["ВАХТА", "ВАХТОВК", "ГРУЗОВИК", "КАМАЗ", "УРАЛ", "ГАЗ"],
    "вахта":          ["ВАХТА", "ВАХТОВК", "АВТОБУС"],
    # Геофизика
    "геофизика":      ["ГФ", "КАРОТАЖ", "ПАВ"],
    # Дефектоскопия / диагностика
    "диагностика":    ["ЛАБ", "ДИАГН"],
}

# Normalise to uppercase for faster lookup
_NORMALISED: dict[str, list[str]] = {
    k.upper(): [p.upper() for p in v]
    for k, v in TASK_TYPE_TO_VEHICLE_PATTERNS.items()
}


def get_vehicle_skills(vehicle_name: str) -> list[str]:
    """
    Return list of task_types the vehicle can perform based on its name.
    Empty list → no restriction (compatible with all task types).
    """
    name_upper = (vehicle_name or "").upper()
    skills: list[str] = []

    for task_type, patterns in _NORMALISED.items():
        for pat in patterns:
            if pat in name_upper:
                skills.append(task_type.lower())  # store in lowercase
                break

    return skills


def is_vehicle_compatible(vehicle_name: str, task_type: str | None) -> bool:
    """
    Quick check: can this vehicle (by name) perform the given task_type?
    Returns True if task_type is None, or vehicle has no skill restrictions,
    or the vehicle explicitly matches the task_type.
    """
    if not task_type:
        return True
    skills = get_vehicle_skills(vehicle_name)
    if not skills:
        return True  # unrecognised vehicle → general-purpose
    return task_type.lower() in skills
