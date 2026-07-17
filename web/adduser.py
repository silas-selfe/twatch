"""Create or update a web-UI login (run with an ADMIN dsn, not tw_web):

  TW_ADMIN_DSN=postgres://admin@host/twatch python -m webapp.adduser silas
"""
import getpass
import os
import sys

import psycopg
from argon2 import PasswordHasher


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: python -m webapp.adduser <username>")
    username = sys.argv[1].strip()
    dsn = os.environ.get("TW_ADMIN_DSN") or os.environ.get("TW_CENTRAL_DSN")
    if not dsn:
        sys.exit("set TW_ADMIN_DSN (a role that can write monitor_traffic.users)")
    pw = getpass.getpass(f"password for {username!r}: ")
    if len(pw) < 10:
        sys.exit("use at least 10 characters")
    if pw != getpass.getpass("repeat: "):
        sys.exit("passwords do not match")
    with psycopg.connect(dsn) as con:
        con.execute(
            "INSERT INTO monitor_traffic.users (username, pw_hash)"
            " VALUES (%s, %s)"
            " ON CONFLICT (username) DO UPDATE SET pw_hash = EXCLUDED.pw_hash",
            (username, PasswordHasher().hash(pw)))
    print(f"user {username!r} ready")


if __name__ == "__main__":
    main()
