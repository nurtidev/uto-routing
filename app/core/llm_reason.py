"""
LLM-based natural language reason generation for vehicle recommendations.

Uses Anthropic Claude API (claude-haiku — fast and cheap).
Falls back to template-based reason if ANTHROPIC_API_KEY is not set or API fails.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Module-level client (lazy init)
_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    # Prefer settings (reads .env via Pydantic), fall back to raw os.getenv
    try:
        from app.config import get_settings
        api_key = get_settings().anthropic_api_key or os.getenv("ANTHROPIC_API_KEY")
    except Exception:
        api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
        _client = anthropic.AsyncAnthropic(api_key=api_key)
        return _client
    except ImportError:
        logger.warning("anthropic package not installed — LLM reason disabled")
        return None


async def generate_reason(
    vehicle_name: str,
    score: float,
    distance_km: float,
    eta_minutes: float,
    free_at_minutes: float,
    compatible: bool,
    task_priority: str,
    task_type: str | None = None,
) -> str:
    """
    Generate a natural language explanation for why this vehicle was recommended.
    Returns LLM-generated text or a template fallback.
    """
    client = _get_client()
    if client is None:
        return _template_reason(
            vehicle_name, score, distance_km, eta_minutes,
            free_at_minutes, compatible, task_priority,
        )

    try:
        status = (
            "свободна прямо сейчас"
            if free_at_minutes <= 0
            else f"занята, освободится через {round(free_at_minutes)} мин"
        )
        compat_str = (
            f"совместима с типом работ «{task_type}»"
            if compatible and task_type
            else ("совместима (тип работ не задан)" if compatible else "тип работ не совпадает (назначена по умолчанию)")
        )
        sla_map = {"high": 2, "medium": 5, "low": 12}
        sla_h = sla_map.get(task_priority, 12)

        prompt = (
            f"Ты диспетчер спецтехники на нефтяном месторождении. "
            f"Объясни в 1–2 предложениях, почему выбрана именно эта единица техники.\n\n"
            f"Техника: {vehicle_name}\n"
            f"Расстояние до скважины: {distance_km:.1f} км\n"
            f"Расчётное время прибытия (ETA): {eta_minutes:.0f} мин\n"
            f"Статус: {status}\n"
            f"Совместимость: {compat_str}\n"
            f"Приоритет заявки: {task_priority} (SLA: +{sla_h} ч)\n"
            f"Итоговый балл: {score:.0%}\n\n"
            f"Ответ на русском языке, кратко и по существу, без вводных слов."
        )

        import anthropic
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
            timeout=5.0,
        )
        if not message.content:
            logger.warning("LLM returned empty content, falling back to template")
            return _template_reason(
                vehicle_name, score, distance_km, eta_minutes,
                free_at_minutes, compatible, task_priority,
            )
        return message.content[0].text.strip()

    except Exception as exc:
        logger.warning("LLM reason generation failed: %s", exc)
        return _template_reason(
            vehicle_name, score, distance_km, eta_minutes,
            free_at_minutes, compatible, task_priority,
        )


def _template_reason(
    vehicle_name: str,
    score: float,
    distance_km: float,
    eta_minutes: float,
    free_at_minutes: float,
    compatible: bool,
    task_priority: str,
) -> str:
    """Fallback template-based reason (no LLM required)."""
    parts: list[str] = []

    if compatible:
        parts.append("совместима по типу работ")

    if free_at_minutes <= 0:
        parts.append("свободна прямо сейчас")
    elif free_at_minutes < 60:
        parts.append(f"занята, освободится через {int(free_at_minutes)} мин")
    else:
        parts.append(f"занята, освободится через {free_at_minutes / 60:.1f} ч")

    if distance_km < 5:
        parts.append(f"очень близко ({distance_km:.1f} км)")
    elif distance_km < 20:
        parts.append(f"расстояние {distance_km:.1f} км по дорогам")
    else:
        parts.append(f"расстояние {distance_km:.1f} км")

    sla_map = {"high": 120, "medium": 300, "low": 720}
    deadline_min = sla_map.get(task_priority, 720)
    if eta_minutes <= deadline_min * 0.5:
        parts.append(f"укладывается в SLA с запасом (ETA {eta_minutes:.0f} мин)")
    elif eta_minutes <= deadline_min:
        parts.append(f"укладывается в SLA (ETA {eta_minutes:.0f} мин)")
    else:
        parts.append(f"⚠ превышает SLA {task_priority}-приоритета (ETA {eta_minutes:.0f} мин)")

    return "; ".join(parts).capitalize() + "."
