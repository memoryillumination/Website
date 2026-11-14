"""Database module for MemoryIllumination backend.

Encapsulates database connection management, initialization, and schema management.
"""
import sqlite3
import os

DB_NAME = os.environ.get("DB_NAME", "users.db")
SMOKE_TEST_DB = os.environ.get("SMOKE_TEST_DB", "").strip()
if SMOKE_TEST_DB:
    DB_NAME = SMOKE_TEST_DB


def get_db_connection():
    """Create a new database connection with WAL mode enabled."""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    """Initialize the database schema."""
    conn = get_db_connection()

    conn.execute('''
        CREATE TABLE IF NOT EXISTS subscription_tiers (
            id              INTEGER PRIMARY KEY,
            name            TEXT NOT NULL UNIQUE,
            display_name    TEXT NOT NULL,
            max_conversions INTEGER  -- NULL = unlimited / custom negotiated
        )
    ''')

    conn.executemany('''
        INSERT OR IGNORE INTO subscription_tiers (id, name, display_name, max_conversions)
        VALUES (?, ?, ?, ?)
    ''', [
        (1, 'sample',        'Sample',                                  None),
        (2, 'introductory',  'Introductory',                            None),
        (3, 'complete',      'Complete',                                None),
        (4, 'business',      'Business / Organization / Professional',  None),
    ])

    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            username             TEXT UNIQUE NOT NULL,
            password_hash        TEXT NOT NULL,
            is_active            INTEGER DEFAULT 0,
            subscription_tier_id INTEGER DEFAULT 1
                                 REFERENCES subscription_tiers(id),
            has_completed_tour   INTEGER DEFAULT 0
        )
    ''')

    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if 'subscription_tier_id' not in existing_cols:
        conn.execute('''
            ALTER TABLE users
            ADD COLUMN subscription_tier_id INTEGER DEFAULT 1
                       REFERENCES subscription_tiers(id)
        ''')
    if 'has_completed_tour' not in existing_cols:
        conn.execute('''
            ALTER TABLE users
            ADD COLUMN has_completed_tour INTEGER DEFAULT 0
        ''')

    # Throttle log for confirmation emails. Kept in SQLite rather than process
    # memory so the limit holds across uWSGI workers.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS confirmation_sends (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            email   TEXT NOT NULL,
            sent_at INTEGER NOT NULL
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_confirmation_sends_email
        ON confirmation_sends (email, sent_at)
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS rate_limits (
            bucket TEXT NOT NULL,
            hit_at INTEGER NOT NULL
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_rate_limits_bucket
        ON rate_limits (bucket, hit_at)
    ''')

    # Addresses we must stop mailing: hard bounces (the mailbox does not exist)
    # and spam complaints. Kept separate from users so a suppression survives
    # the account being deleted and re-created.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS email_suppressions (
            email      TEXT PRIMARY KEY,
            reason     TEXT NOT NULL,
            detail     TEXT,
            created_at INTEGER NOT NULL
        )
    ''')

    # Async GPU jobs. Lives in SQLite rather than process memory for the same
    # reason confirmation_sends does: uWSGI runs 4 worker processes, so the
    # request that polls a job is usually not the one that created it.
    #
    # Result bytes are deliberately NOT stored here. Modal retains outputs for
    # up to 7 days, so /jobs/<id>/result re-fetches from the FunctionCall and
    # SQLite only holds the metadata needed to authorize and label that fetch.
    conn.execute('''
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
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_jobs_created
        ON jobs (created_at)
    ''')

    conn.commit()
    conn.close()
