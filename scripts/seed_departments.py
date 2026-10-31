"""Seed the default department packs + their ACL policies.

Usage:
    python -m scripts.seed_departments
"""
from __future__ import annotations

from app.db.session import SessionLocal, init_db
from app.departments.registry import seed_departments


def main() -> None:
    init_db()
    with SessionLocal() as s:
        summary = seed_departments(s)
    print(f"seeded departments: {summary}")


if __name__ == "__main__":
    main()
