"""Deterministically build the seeded ``orders``/``customers`` DuckDB fixture (spec §4, §9.5).

The generated ``fixtures/orders.duckdb`` is checked in so tests open it directly with
zero setup, but it is fully reproducible: run ``python fixtures/build_orders_duckdb.py``
and you get a byte-stable schema with the same seeded data. Data is "realistic
enough" that column profiling produces meaningful ranges, distincts, and top-k.

Run from the repo root:  uv run python fixtures/build_orders_duckdb.py
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

DB_PATH = Path(__file__).resolve().parent / "orders.duckdb"

REGIONS = ["us", "emea", "apac", "latam"]
PLANS = ["free", "pro", "enterprise"]
# Most orders confirm; a minority are refunded / partially refunded (drives net-revenue math).
STATUS_WEIGHTS = [("confirmed", 0.84), ("refunded", 0.08), ("partial_refund", 0.08)]

N_CUSTOMERS = 200
N_ORDERS = 2000
SEED = 1729

# The data's real coverage window — Claude reads min/max of created_at to infer this.
START = datetime(2019, 2, 1)
END = datetime(2026, 6, 29)


def _weighted_status(rng: random.Random) -> str:
    r = rng.random()
    cumulative = 0.0
    for status, weight in STATUS_WEIGHTS:
        cumulative += weight
        if r <= cumulative:
            return status
    return STATUS_WEIGHTS[-1][0]


def build(db_path: Path = DB_PATH) -> Path:
    rng = random.Random(SEED)
    if db_path.exists():
        db_path.unlink()

    con = duckdb.connect(str(db_path))
    con.execute("CREATE SCHEMA IF NOT EXISTS analytics;")

    con.execute(
        """
        CREATE TABLE analytics.customers (
            customer_id BIGINT PRIMARY KEY,
            name        TEXT NOT NULL,
            email       TEXT,
            region      TEXT NOT NULL,
            plan        TEXT NOT NULL,
            signup_date DATE NOT NULL
        );
        """
    )
    con.execute(
        """
        CREATE TABLE analytics.orders (
            order_id     BIGINT PRIMARY KEY,
            customer_id  BIGINT NOT NULL REFERENCES analytics.customers(customer_id),
            amount_cents BIGINT NOT NULL,
            status       TEXT NOT NULL,
            created_at   TIMESTAMP NOT NULL
        );
        """
    )

    customers = []
    base_signup = date(2018, 6, 1)
    for cid in range(1, N_CUSTOMERS + 1):
        region = rng.choice(REGIONS)
        plan = rng.choices(PLANS, weights=[0.5, 0.35, 0.15])[0]
        signup = base_signup + timedelta(days=rng.randint(0, 365 * 7))
        customers.append(
            (cid, f"Customer {cid:03d}", f"user{cid:03d}@example.com", region, plan, signup)
        )
    con.executemany(
        "INSERT INTO analytics.customers VALUES (?, ?, ?, ?, ?, ?);",
        customers,
    )

    span_seconds = int((END - START).total_seconds())
    orders = []
    for oid in range(1, N_ORDERS + 1):
        cid = rng.randint(1, N_CUSTOMERS)
        # Skewed amounts: typical order a few thousand cents, a long tail up to ~$42k.
        amount = int(min(4_200_000, max(0, rng.lognormvariate(8.5, 1.1))))
        status = _weighted_status(rng)
        created = START + timedelta(seconds=rng.randint(0, span_seconds))
        orders.append((oid, cid, amount, status, created))
    con.executemany(
        "INSERT INTO analytics.orders VALUES (?, ?, ?, ?, ?);",
        orders,
    )

    con.close()
    return db_path


if __name__ == "__main__":
    path = build()
    print(f"built {path}")
