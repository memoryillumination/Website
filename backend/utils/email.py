import time

def is_email_suppressed(conn, email):
    """True if this address hard-bounced or filed a spam complaint."""
    return conn.execute(
        "SELECT 1 FROM email_suppressions WHERE email = ?", (email,)
    ).fetchone() is not None


def suppress_email(conn, email, reason, detail=None):
    """Record an address as undeliverable. Idempotent — webhooks get retried."""
    conn.execute(
        """INSERT INTO email_suppressions (email, reason, detail, created_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(email) DO UPDATE SET
               reason = excluded.reason,
               detail = excluded.detail,
               created_at = excluded.created_at""",
        (email, reason, detail, int(time.time()))
    )
