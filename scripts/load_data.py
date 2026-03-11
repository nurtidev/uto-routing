#!/usr/bin/env python3
"""
scripts/load_data.py — Bootstrap script: creates the tasks table and loads tasks.csv into DB.

Usage:
    python scripts/load_data.py                      # load data/tasks.csv
    python scripts/load_data.py --csv path/to/file   # custom CSV path
    python scripts/load_data.py --drop-first         # drop & recreate table first

Requires DB connection via .env (DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Ensure project root is on sys.path when run directly
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from sqlalchemy import create_engine, text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DDL — tasks table
# ---------------------------------------------------------------------------

DDL_TASKS = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id                VARCHAR(100) PRIMARY KEY,
    priority               VARCHAR(10)  NOT NULL CHECK (priority IN ('low', 'medium', 'high')),
    planned_start          TIMESTAMP    NOT NULL,
    planned_duration_hours NUMERIC(8,2) NOT NULL,
    destination_uwi        VARCHAR(100) NOT NULL,
    task_type              VARCHAR(100),
    shift                  VARCHAR(10)  CHECK (shift IN ('day', 'night')),
    start_day              DATE
);
"""

# ---------------------------------------------------------------------------
# CSV column → DB column mapping
# Adjust keys if your CSV uses different column names.
# ---------------------------------------------------------------------------

COLUMN_MAP = {
    # CSV name           → DB name
    "task_id":             "task_id",
    "priority":            "priority",
    "planned_start":       "planned_start",
    "planned_duration_hours": "planned_duration_hours",
    "duration_hours":      "planned_duration_hours",  # alternative name
    "destination_uwi":     "destination_uwi",
    "uwi":                 "destination_uwi",
    "task_type":           "task_type",
    "shift":               "shift",
    "start_day":           "start_day",
}

REQUIRED_DB_COLS = {
    "task_id", "priority", "planned_start",
    "planned_duration_hours", "destination_uwi",
}


def _get_db_url() -> str:
    """Build sync database URL from environment / .env file."""
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    import os
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "uto")
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


def load_csv(csv_path: Path) -> pd.DataFrame:
    """Read CSV, rename columns, validate required fields."""
    logger.info("Reading CSV: %s", csv_path)
    df = pd.read_csv(csv_path)
    logger.info("Raw CSV: %d rows, columns: %s", len(df), list(df.columns))

    # Normalise column names (strip spaces, lower-case)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    # Apply column map
    rename = {k: v for k, v in COLUMN_MAP.items() if k in df.columns and k != v}
    if rename:
        df = df.rename(columns=rename)
        logger.info("Renamed columns: %s", rename)

    # Remove duplicate column aliases (keep first)
    df = df.loc[:, ~df.columns.duplicated()]

    # Check required columns
    missing = REQUIRED_DB_COLS - set(df.columns)
    if missing:
        raise ValueError(
            f"CSV is missing required columns: {missing}\n"
            f"Available columns: {list(df.columns)}"
        )

    # Parse dates
    df["planned_start"] = pd.to_datetime(df["planned_start"])
    if "start_day" in df.columns:
        df["start_day"] = pd.to_datetime(df["start_day"]).dt.date
    else:
        df["start_day"] = df["planned_start"].dt.date

    # Infer shift if missing
    if "shift" not in df.columns:
        hour = df["planned_start"].dt.hour
        df["shift"] = hour.apply(lambda h: "day" if 8 <= h < 20 else "night")
        logger.info("Inferred 'shift' column from planned_start hours.")

    # Normalise priority values
    df["priority"] = df["priority"].str.strip().str.lower()
    invalid_priority = df[~df["priority"].isin(["low", "medium", "high"])]
    if not invalid_priority.empty:
        logger.warning(
            "%d rows have invalid priority values: %s — defaulting to 'medium'",
            len(invalid_priority),
            invalid_priority["priority"].unique().tolist(),
        )
        df.loc[~df["priority"].isin(["low", "medium", "high"]), "priority"] = "medium"

    # Keep only DB columns
    db_cols = [
        "task_id", "priority", "planned_start", "planned_duration_hours",
        "destination_uwi", "task_type", "shift", "start_day",
    ]
    df = df[[c for c in db_cols if c in df.columns]]

    logger.info("Prepared %d tasks for import.", len(df))
    return df


def create_table(engine, drop_first: bool = False) -> None:
    with engine.begin() as conn:
        if drop_first:
            logger.warning("Dropping existing tasks table …")
            conn.execute(text("DROP TABLE IF EXISTS tasks CASCADE"))
        conn.execute(text(DDL_TASKS))
    logger.info("Table 'tasks' ready.")


def upsert_tasks(engine, df: pd.DataFrame) -> int:
    """Insert tasks, skipping existing task_ids (on conflict do nothing)."""
    rows = df.to_dict(orient="records")
    inserted = 0

    with engine.begin() as conn:
        for row in rows:
            placeholders = ", ".join(f":{k}" for k in row)
            cols = ", ".join(row.keys())
            sql = text(
                f"INSERT INTO tasks ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT (task_id) DO NOTHING"
            )
            result = conn.execute(sql, row)
            inserted += result.rowcount

    return inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="Load tasks CSV into PostgreSQL")
    parser.add_argument(
        "--csv",
        default=str(PROJECT_ROOT / "data" / "tasks.csv"),
        help="Path to tasks CSV file (default: data/tasks.csv)",
    )
    parser.add_argument(
        "--drop-first",
        action="store_true",
        help="Drop and recreate the tasks table before loading",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        logger.error("CSV file not found: %s", csv_path)
        sys.exit(1)

    try:
        db_url = _get_db_url()
        engine = create_engine(db_url)

        # Test connection
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("DB connection OK.")

        create_table(engine, drop_first=args.drop_first)
        df = load_csv(csv_path)
        inserted = upsert_tasks(engine, df)
        logger.info("Done: %d new tasks inserted (duplicates skipped).", inserted)

    except Exception as exc:
        logger.error("Failed: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
