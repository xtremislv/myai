"""Bootstrap: create tables, a first admin user, and baseline ACL policies.

Usage (inside the gateway container or a venv with deps):
    python -m scripts.init_db --admin-user admin --admin-pass secret
"""
from __future__ import annotations

import argparse
import uuid

from app.db.session import ACLPolicy, SessionLocal, User, init_db
from app.auth.security import hash_password


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin-user", default="admin")
    ap.add_argument("--admin-pass", required=True)
    args = ap.parse_args()

    init_db()
    with SessionLocal() as s:
        if not s.query(User).filter(User.username == args.admin_user).first():
            s.add(
                User(
                    id=uuid.uuid4().hex,
                    username=args.admin_user,
                    password_hash=hash_password(args.admin_pass),
                    roles=["admin"],
                )
            )
        # Baseline: admins can do everything; deny-by-default for everyone else.
        baseline = [
            ("admin", "allow", "*", "*"),
            ("admin", "allow", "admin", "system:*"),
        ]
        for subject, effect, action, resource in baseline:
            exists = (
                s.query(ACLPolicy)
                .filter_by(subject=subject, effect=effect, action=action, resource=resource)
                .first()
            )
            if not exists:
                s.add(
                    ACLPolicy(
                        id=uuid.uuid4().hex,
                        subject=subject,
                        effect=effect,
                        action=action,
                        resource=resource,
                    )
                )
        s.commit()
    print("init complete: admin user + baseline policies ready")


if __name__ == "__main__":
    main()
