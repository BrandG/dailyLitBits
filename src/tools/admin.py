"""Admin accounts.

Being an admin is the `role` column on an ordinary user row, so there is no
separate admin login and no admin password anywhere. /admin wants a
short-lived token - a reader's dashboard link lives in their inbox forever,
which is fine for their own progress and not fine for everyone's - so this
mints one on demand.

    python tools/admin.py promote you@example.com --create
    python tools/admin.py link you@example.com
    python tools/admin.py list
    python tools/admin.py demote someone@example.com
"""
import argparse
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import db  # noqa: E402
import security  # noqa: E402
from user_manager import UserManager  # noqa: E402

BASE_URL = "https://dailylitbits.com"


def main():
    parser = argparse.ArgumentParser(description="Manage admin access")
    parser.add_argument("action", choices=["promote", "demote", "link", "list"])
    parser.add_argument("email", nargs="?")
    parser.add_argument("--create", action="store_true",
                        help="With promote: create the user if they have never signed up")
    args = parser.parse_args()

    conn = db.get_db()
    try:
        manager = UserManager(conn)

        if args.action == "list":
            rows = conn.execute("SELECT id, role, created_at FROM users ORDER BY id").fetchall()
            if not rows:
                print("No users yet.")
                return
            for row in rows:
                print(f"  {row['id']:4}  {row['role']:6}  {manager.email_for(row['id'])}")
            return

        if not args.email:
            parser.error(f"{args.action} needs an email address")

        user = manager.get_user_by_email(args.email)

        if not user:
            if args.action == "promote" and args.create:
                user_id, _ = manager.get_or_create_user(args.email)
                print(f"Created user {user_id}.")
                user = manager.get_user(user_id)
            else:
                print("No such user. Add --create to promote an address that has "
                      "never signed up.")
                sys.exit(1)

        if args.action == "promote":
            manager.set_role(user["id"], "admin")
            print(f"User {user['id']} is now an admin.")
        elif args.action == "demote":
            manager.set_role(user["id"], "reader")
            print(f"User {user['id']} is now a reader.")
        elif args.action == "link":
            if not manager.is_admin(user["id"]):
                print("That user is not an admin. Promote them first.")
                sys.exit(1)
            token = security.generate_admin_token(user["id"])
            print(f"Valid for {security.ADMIN_TOKEN_MAX_AGE // 60} minutes:")
            print(f"{BASE_URL}/admin?token={token}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
