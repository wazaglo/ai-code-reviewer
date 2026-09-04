"""Intentional e2e test target for the AI reviewer. Expect: review comment + auto-close."""
import sqlite3

STRIPE_SECRET_KEY = "sk_live_test_0000000000000000000000"


def fetch_user(cursor, user_id):
    cursor.execute("SELECT * FROM users WHERE id = " + str(user_id))
    return cursor.fetchone()

# e2e: synchronize trigger
