"""
Create the smoke test database with a smoke-test user.

Usage:
    cd /tmp/website-repo
    python scripts/create_smoke_test_db.py

This creates `backend/smoke_test_users.db` with a single user:
    username: smoke-test
    password: smoke-test-pass
"""
import os
import sys
import sqlite3

# Ensure argon2-cffi is available for password hashing
try:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError
except ImportError:
    print("❌ argon2-cffi is not installed. Install it with:")
    print("   pip install argon2-cffi")
    sys.exit(1)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "backend", "smoke_test_users.db")
USERNAME = "smoke-test@example.com"
PASSWORD = "smoke-test-pass"


def create_db():
    """Create the smoke test database with schema and user."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Create subscription_tiers table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscription_tiers (
            id              INTEGER PRIMARY KEY,
            name            TEXT NOT NULL UNIQUE,
            display_name    TEXT NOT NULL,
            max_conversions INTEGER
        )
    """)

    conn.executemany("""
        INSERT OR IGNORE INTO subscription_tiers (id, name, display_name, max_conversions)
        VALUES (?, ?, ?, ?)
    """, [
        (1, 'sample', 'Sample', None),
        (2, 'introductory', 'Introductory', None),
        (3, 'complete', 'Complete', None),
        (4, 'business', 'Business / Organization / Professional', None),
    ])

    # Create users table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            username             TEXT UNIQUE NOT NULL,
            password_hash        TEXT NOT NULL,
            is_active            INTEGER DEFAULT 0,
            subscription_tier_id INTEGER DEFAULT 1 REFERENCES subscription_tiers(id),
            has_completed_tour   INTEGER DEFAULT 0
        )
    """)

    # Create confirmation_sends table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS confirmation_sends (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            email   TEXT NOT NULL,
            sent_at INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_confirmation_sends_email
        ON confirmation_sends (email, sent_at)
    """)

    # Create rate_limits table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rate_limits (
            bucket TEXT NOT NULL,
            hit_at INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_rate_limits_bucket
        ON rate_limits (bucket, hit_at)
    """)

    # Create email_suppressions table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_suppressions (
            email      TEXT PRIMARY KEY,
            reason     TEXT NOT NULL,
            detail     TEXT,
            created_at INTEGER NOT NULL
        )
    """)

    # Create jobs table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id           TEXT PRIMARY KEY,
            call_id      TEXT NOT NULL,
            owner        TEXT NOT NULL,
            is_free_tier INTEGER NOT NULL,
            cold_start   INTEGER NOT NULL,
            status       TEXT NOT NULL,
            error        TEXT,
            created_at   INTEGER NOT NULL,
            finished_at  INTEGER
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_jobs_created
        ON jobs (created_at)
    """)

    # Hash password and insert smoke-test user
    ph = PasswordHasher()
    password_hash = ph.hash(PASSWORD)

    # Check if user already exists
    existing = conn.execute(
        "SELECT id FROM users WHERE username = ?", (USERNAME,)
    ).fetchone()

    if existing:
        print(f"ℹ️  User '{USERNAME}' already exists (id={existing['id']}). Skipping insert.")
    else:
        conn.execute(
            """INSERT INTO users (username, password_hash, is_active, subscription_tier_id)
               VALUES (?, ?, 1, 1)""",
            (USERNAME, password_hash)
        )
        print(f"✅ Created user '{USERNAME}' (active, sample tier)")

    conn.commit()
    conn.close()

    print(f"\n📁 Database created at: {DB_PATH}")


if __name__ == "__main__":
    create_db()
