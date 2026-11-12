"""Helper functions used across multiple route modules."""
import random
import time
from flask import request

# These will be set by app.py after import
_serializer = None
_smoke_test_user = None
_job_retention_seconds = None


def set_globals(serializer, smoke_test_user, job_retention_seconds):
    """Set global configuration values for helper functions."""
    global _serializer, _smoke_test_user, _job_retention_seconds
    _serializer = serializer
    _smoke_test_user = smoke_test_user
    _job_retention_seconds = job_retention_seconds


def _is_smoke_test_request():
    """Check if the current request is authenticated as the smoke-test user.

    Used to bypass rate limits for automated smoke tests that need to upload
    repeatedly without hitting the per-IP caps.
    """
    if not _smoke_test_user:
        return False
    token = request.cookies.get('session', '')
    if not token:
        return False
    try:
        username = _serializer.loads(token, salt='session', max_age=604800)
        return username == _smoke_test_user
    except Exception:
        return False


def job_owner(token, ip):
    """Identity a job is bound to, so one caller cannot poll another's job.

    Falls back to IP for the anonymous uploads this endpoint still allows.
    """
    try:
        return f"user:{_serializer.loads(token, salt='session', max_age=604800)}"
    except Exception:
        return f"ip:{ip}"


def load_job(conn, job_id, owner):
    """The job row, but only for the caller that created it."""
    return conn.execute(
        "SELECT * FROM jobs WHERE id = ? AND owner = ?", (job_id, owner)
    ).fetchone()


def sweep_old_jobs(conn):
    if random.random() < 0.05:
        conn.execute(
            "DELETE FROM jobs WHERE created_at < ?",
            (int(time.time()) - _job_retention_seconds,)
        )
