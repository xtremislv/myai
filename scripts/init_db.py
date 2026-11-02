"""Bootstrap: create tables, a first admin user, and baseline ACL policies.

Usage (inside the gateway container or a venv with deps):
    python -m scripts.init_db --admin-user admin --admin-pass secret
"""
from __future__ import annotations

import argparse
import uuid

from app.auth.security import hash_password
from app.db.session import SessionLocal, User, init_db
from app.departments.registry import seed_departments
from app.vault import user_keys


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin-user", default="admin")
    ap.add_argument("--admin-pass", required=True)
    args = ap.parse_args()

    init_db()
    with SessionLocal() as s:
        if not s.query(User).filter(User.username == args.admin_user).first():
            admin = User(
                id=uuid.uuid4().hex,
                username=args.admin_user,
                password_hash=hash_password(args.admin_pass),
                roles=["admin"],
            )
            # The admin chose this password, so it can hold their chat key right away.
            chat_key = user_keys.provision(admin, args.admin_pass)
            recovery_code = user_keys.add_recovery_code(admin, chat_key)
            s.add(admin)
            s.commit()
            print(f"admin recovery code (save it now, it is not shown again): {recovery_code}")
        # All access policies come from the department packs (incl. admin's
        # full-access grant). Deny-by-default applies to everything else.
        summary = seed_departments(s)
    print(f"init complete: admin user ready; departments seeded: {summary}")


if __name__ == "__main__":
    main()
