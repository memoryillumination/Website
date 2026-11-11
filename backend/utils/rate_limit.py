import time
import random

# Rate limit windows: name -> (max hits, window in seconds)
RATE_LIMITS = {
    'upload':     (20, 900),
    'register':   (3, 900),
    'login':      (20, 900),
    'resend':     (2, 1800),
    'confirm':    (5, 300),
    'tier':       (3, 600),
}

def rate_limit_retry_after(conn, name, identifier, record=True):
    """
    Seconds the caller must wait before retrying `name`, or 0 to proceed.

    Set record=False to test a bucket without consuming quota.
    """
    limit, window = RATE_LIMITS[name]
    bucket = f"{name}:{identifier}"
    now = int(time.time())

    conn.execute("DELETE FROM rate_limits WHERE bucket = ? AND hit_at < ?", (bucket, now - window))

    hits = [r[0] for r in conn.execute(
        "SELECT hit_at FROM rate_limits WHERE bucket = ? ORDER BY hit_at", (bucket,)
    )]
    if len(hits) >= limit:
        return max(1, hits[0] + window - now)

    if record:
        conn.execute("INSERT INTO rate_limits (bucket, hit_at) VALUES (?, ?)", (bucket, now))

    if random.random() < 0.01:
        longest = max(w for _, w in RATE_LIMITS.values())
        conn.execute("DELETE FROM rate_limits WHERE hit_at < ?", (now - longest,))
    return 0


def too_many_requests(retry_after):
    from flask import jsonify
    response = jsonify({"error": "Too many requests. Please try again later."})
    response.headers['Retry-After'] = str(retry_after)
    return response, 429
