"""Change a user's role after they've already signed up.

Role is normally decided once, on first login (see supabase_auth.role_for_email
+ db.get_or_create_user) - adding an email to KAULI_STAFF_EMAILS afterward
does NOT retroactively upgrade an existing account. Use this for that case,
or to onboard a contractor/QA person after they've already signed up once.

Usage:
    python -m webapp.promote someone@example.com staff
    python -m webapp.promote someone@example.com client
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from webapp import db  # noqa: E402


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[2] not in ("staff", "client"):
        print(__doc__)
        sys.exit(1)
    email, role = sys.argv[1].strip().lower(), sys.argv[2]

    user = db.get_user_by_email(email)
    if not user:
        print(f"No local account for {email} yet - they need to sign up at "
              f"least once (via /login -> Sign up) before this can promote them.")
        sys.exit(1)

    conn = db.get_conn()
    conn.execute("UPDATE users SET role = ? WHERE email = ?", (role, email))
    conn.commit()
    conn.close()
    print(f"{email}: {user['role']} -> {role}")


if __name__ == "__main__":
    main()
