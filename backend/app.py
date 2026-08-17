import os, io, re, cv2, json, hmac, uuid, base64, hashlib, sqlite3
import numpy as np
from PIL import Image, ImageOps
import pillow_heif
import pillow_avif  # noqa: F401  (importing registers the AVIF plugin with Pillow)
# rembg is only used by simplify_for_coloring(), which is commented out below.
# It is also absent from both requirements files, so importing it here crashes
# any host built from requirements_prod.txt.
# from rembg import remove as rembg_remove, new_session as rembg_new_session
from flask import Flask, request, jsonify, send_file, make_response
from flask_cors import CORS
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
import resend
from dotenv import load_dotenv
import random
import time
import requests

pillow_heif.register_heif_opener()

load_dotenv()

# Was loaded once at startup so per-request calls to simplify_for_coloring()
# didn't pay session/model init cost on every upload.
# REMBG_SESSION = rembg_new_session('u2net')

# APP_ENV selects which GPU backend featureB routes to: "development" (default)
# uses the local worker over loopback HTTP; "production" uses the Modal-hosted
# worker. Set APP_ENV=production in .env to switch.
APP_ENV = os.environ.get('APP_ENV', 'development')

# When True, output for free-tier (and unauthenticated) callers is watermarked.
# Off by default because MI_Watermark.png is not checked into the repo — see the
# availability check next to WATERMARK_PATH below. If this is turned on while
# the asset is missing, startup logs a warning and uploads continue
# unwatermarked rather than failing.
WATERMARK_FREE_TIER = False

app = Flask(__name__)

# Defence in depth behind Nginx's `client_max_body_size 50M` — Flask rejects
# oversized bodies with 413 even if the Nginx config drifts or another
# front-end is put in place.
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

CORS(app, supports_credentials=True, origins=["https://memoryillumination.com"])

# Config
app.config.update(
    SECRET_KEY=os.environ.get('SECRET_KEY'),
)

# Transactional email goes through Resend, sending from the verified
# mail.memoryillumination.com subdomain. REPLY_TO is a real inbox (Cloudflare
# Email Routing) so replies to a noreply@ sender don't vanish.
resend.api_key = os.environ.get('EMAIL_API_KEY')
MAIL_FROM = os.environ.get('MAIL_FROM', 'Memory Illumination <noreply@mail.memoryillumination.com>')
MAIL_REPLY_TO = os.environ.get('MAIL_REPLY_TO', 'support@memoryillumination.com')
API_BASE_URL = os.environ.get('API_BASE_URL', 'https://api.memoryillumination.com')
SITE_BASE_URL = os.environ.get('SITE_BASE_URL', 'https://memoryillumination.com')

# Signing secret for the Resend webhook (Svix format, "whsec_..."). Without it
# the webhook endpoint rejects everything — an unauthenticated suppression
# endpoint would let anyone block delivery to any address.
RESEND_WEBHOOK_SECRET = os.environ.get('RESEND_WEBHOOK_SECRET')
WEBHOOK_TOLERANCE = 300           # max age of a webhook timestamp, in seconds

# Per-IP rate limits as {name: (max_requests, window_seconds)}. Enforced in
# SQLite rather than process memory so the counts hold across uWSGI workers.
# 'login' counts only failed attempts, so a legitimate user is never locked out.
# 'upload_gpu' is deliberately the tightest: featureB dispatches to Modal, and
# that is the only path here that costs money per request.
RATE_LIMITS = {
    'register':   (5, 3600),
    'resend':     (10, 3600),
    'login':      (20, 900),
    'upload':     (30, 3600),
    'upload_gpu': (10, 3600),
}

CONFIRM_TOKEN_MAX_AGE = 3600      # 1 hour, matches the copy in the email body
CONFIRM_RESEND_WINDOW = 3600      # throttle window for confirmation sends
CONFIRM_RESEND_LIMIT = 3          # max confirmation emails per address per window

# Deliberately permissive: real validation is "did the confirmation email
# arrive". This only rejects input that can't be an address at all, so we
# don't hand obvious garbage to Resend and rack up bounces.
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$')

serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
ph = PasswordHasher()
DB_NAME = os.path.join(os.path.dirname(__file__), "users.db")


def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

# --- DATABASE INITIALIZATION ---
def init_db():
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

init_db()


# --- RATE LIMITING ---

def client_ip():
    """
    Caller's address.

    Nginx reaches uWSGI over the uwsgi protocol (uwsgi_pass), and its
    uwsgi_params sets REMOTE_ADDR to $remote_addr — the real client address,
    since api.memoryillumination.com is a DNS-only record pointing straight at
    the droplet with nothing proxying in front of it.

    Deliberately does NOT consult X-Forwarded-For. Nginx never sets that header
    here, so it would carry only whatever the client chose to send: anyone could
    rotate it per request and walk straight through every rate limit. If a proxy
    (e.g. Cloudflare) is ever put in front of this API, trust its specific
    header instead of re-adding a blanket ProxyFix.
    """
    return request.remote_addr or 'unknown'


def rate_limit_retry_after(conn, name, identifier, record=True):
    """
    Seconds the caller must wait before retrying `name`, or 0 to proceed.

    Set record=False to test a bucket without consuming quota — used by
    /login so only failed attempts count against the limit.
    """
    limit, window = RATE_LIMITS[name]
    bucket = f"{name}:{identifier}"
    now = int(time.time())

    # Prune this bucket only. A global sweep here would use one endpoint's
    # window to delete rows another endpoint still needs.
    conn.execute("DELETE FROM rate_limits WHERE bucket = ? AND hit_at < ?", (bucket, now - window))

    hits = [r[0] for r in conn.execute(
        "SELECT hit_at FROM rate_limits WHERE bucket = ? ORDER BY hit_at", (bucket,)
    )]
    if len(hits) >= limit:
        # Quota frees up when the oldest hit falls out of the window.
        return max(1, hits[0] + window - now)

    if record:
        conn.execute("INSERT INTO rate_limits (bucket, hit_at) VALUES (?, ?)", (bucket, now))

    # Idle buckets are never revisited, so sweep them occasionally rather than
    # letting the table grow without bound.
    if random.random() < 0.01:
        longest = max(w for _, w in RATE_LIMITS.values())
        conn.execute("DELETE FROM rate_limits WHERE hit_at < ?", (now - longest,))
    return 0


def too_many_requests(retry_after):
    response = jsonify({"error": "Too many requests. Please try again later."})
    response.headers['Retry-After'] = str(retry_after)
    return response, 429


# --- EMAIL CONFIRMATION ---

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


def verify_webhook_signature(headers, payload):
    """
    Verify a Resend (Svix) webhook signature.

    Svix signs "{id}.{timestamp}.{raw body}" with HMAC-SHA256 under the
    base64 secret that follows the "whsec_" prefix, and sends one or more
    space-separated "v1,<sig>" candidates so keys can be rotated.
    """
    if not RESEND_WEBHOOK_SECRET:
        print("⚠️  Resend webhook received but RESEND_WEBHOOK_SECRET is unset — rejecting.")
        return False

    msg_id = headers.get('svix-id')
    timestamp = headers.get('svix-timestamp')
    signatures = headers.get('svix-signature')
    if not (msg_id and timestamp and signatures):
        return False

    # Reject stale payloads so a captured request can't be replayed later.
    try:
        if abs(time.time() - int(timestamp)) > WEBHOOK_TOLERANCE:
            return False
    except (TypeError, ValueError):
        return False

    secret = RESEND_WEBHOOK_SECRET.split('_', 1)[1] if RESEND_WEBHOOK_SECRET.startswith('whsec_') else RESEND_WEBHOOK_SECRET
    try:
        key = base64.b64decode(secret)
    except Exception:
        return False

    signed = f"{msg_id}.{timestamp}.".encode() + payload
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()

    for candidate in signatures.split():
        version, _, value = candidate.partition(',')
        if version == 'v1' and hmac.compare_digest(value, expected):
            return True
    return False


def confirmation_quota_remaining(conn, email):
    """True if this address is still under its send limit for the window."""
    cutoff = int(time.time()) - CONFIRM_RESEND_WINDOW
    conn.execute("DELETE FROM confirmation_sends WHERE sent_at < ?", (cutoff,))
    recent = conn.execute(
        "SELECT COUNT(*) FROM confirmation_sends WHERE email = ? AND sent_at >= ?",
        (email, cutoff)
    ).fetchone()[0]
    return recent < CONFIRM_RESEND_LIMIT


def send_confirmation_email(email):
    """
    Send an account confirmation link. Returns True on success.

    Never raises: registration must not fail just because the mail provider is
    having a bad day. A caller that gets False has already created the account,
    and the user can recover through /resend-confirmation.
    """
    try:
        token = serializer.dumps(email, salt='email-confirm')
        link = f"{API_BASE_URL}/confirm/{token}"
        resend.Emails.send({
            "from": MAIL_FROM,
            "to": [email],
            "reply_to": MAIL_REPLY_TO,
            "subject": "Confirm your Memory Illumination account",
            "html": (
                "<p>Thanks for signing up. Confirm your account to get started:</p>"
                f'<p><a href="{link}">Confirm my account</a></p>'
                "<p>Or paste this link into your browser:<br>"
                f'<span style="word-break:break-all">{link}</span></p>'
                "<p>This link expires in 1 hour. "
                "If you didn't create this account, you can ignore this email.</p>"
            ),
            "text": (
                "Thanks for signing up. Confirm your account by opening this link:\n\n"
                f"{link}\n\n"
                "This link expires in 1 hour. "
                "If you didn't create this account, you can ignore this email."
            ),
        })
        return True
    except Exception as e:
        print(f"CONFIRMATION EMAIL ERROR for {email}: {e}")
        return False


def _confirm_page(heading, body, ok=True):
    color = "#396cd8" if ok else "#c0392b"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{heading}</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family:system-ui,sans-serif;max-width:480px;margin:4rem auto;padding:0 1rem;text-align:center">
<h1 style="color:{color};font-size:1.4rem">{heading}</h1>
<p style="color:#444">{body}</p>
<p><a href="{SITE_BASE_URL}/login.html" style="color:#396cd8">Go to login</a></p>
</body></html>"""

# Identical response whether or not the address is already registered, so this
# endpoint can't be used to enumerate accounts.
REGISTER_OK = {"message": "Check your email for a confirmation link."}


@app.route('/register', methods=['POST'])
def register():
    data = request.json or {}
    username, password = data.get('username'), data.get('password')

    if not isinstance(username, str) or not EMAIL_RE.match(username.strip()):
        return jsonify({"error": "Enter a valid email address."}), 400
    if not isinstance(password, str) or len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    username = username.strip().lower()

    try:
        conn = get_db_connection()

        retry_after = rate_limit_retry_after(conn, 'register', client_ip())
        if retry_after:
            conn.commit()
            conn.close()
            return too_many_requests(retry_after)

        existing = conn.execute(
            "SELECT is_active FROM users WHERE username = ?", (username,)
        ).fetchone()

        if existing is None:
            conn.execute(
                "INSERT INTO users (username, password_hash, is_active, subscription_tier_id) VALUES (?, ?, 0, 1)",
                (username, ph.hash(password))
            )
            conn.commit()
            should_send = True
        else:
            # Already registered. Re-send the link if they never confirmed;
            # stay silent if the account is live, so an attacker can't tell
            # the two cases apart and we don't email an existing user on demand.
            should_send = existing['is_active'] == 0

        if should_send and not is_email_suppressed(conn, username) \
                and confirmation_quota_remaining(conn, username):
            conn.execute(
                "INSERT INTO confirmation_sends (email, sent_at) VALUES (?, ?)",
                (username, int(time.time()))
            )
            conn.commit()
            send_confirmation_email(username)

        # Commit unconditionally: the rate-limit hit recorded above is rolled
        # back on paths that send no mail, which would make the limit unenforceable.
        conn.commit()
        conn.close()
        return jsonify(REGISTER_OK), 201
    except Exception as e:
        print(f"REGISTRATION ERROR for {username}: {e}")
        return jsonify({"error": "Registration failed. Please try again."}), 500


@app.route('/resend-confirmation', methods=['POST'])
def resend_confirmation():
    data = request.json or {}
    username = data.get('username')
    if not isinstance(username, str) or not EMAIL_RE.match(username.strip()):
        return jsonify({"error": "Enter a valid email address."}), 400
    username = username.strip().lower()

    try:
        conn = get_db_connection()

        retry_after = rate_limit_retry_after(conn, 'resend', client_ip())
        if retry_after:
            conn.commit()
            conn.close()
            return too_many_requests(retry_after)

        user = conn.execute(
            "SELECT is_active FROM users WHERE username = ?", (username,)
        ).fetchone()

        if user is not None and user['is_active'] == 0 \
                and not is_email_suppressed(conn, username) \
                and confirmation_quota_remaining(conn, username):
            conn.execute(
                "INSERT INTO confirmation_sends (email, sent_at) VALUES (?, ?)",
                (username, int(time.time()))
            )
            conn.commit()
            send_confirmation_email(username)

        # See /register: the rate-limit hit must survive paths that send nothing.
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"RESEND CONFIRMATION ERROR for {username}: {e}")

    return jsonify(REGISTER_OK), 200

@app.route('/webhooks/resend', methods=['POST'])
def resend_webhook():
    """
    Receives Resend delivery events. Hard bounces and spam complaints add the
    address to email_suppressions so we stop mailing it — continuing to send to
    dead mailboxes is what erodes domain reputation.
    """
    if not verify_webhook_signature(request.headers, request.get_data()):
        return jsonify({"error": "Invalid signature"}), 401

    event = request.get_json(silent=True) or {}
    event_type = event.get('type')
    data = event.get('data') or {}
    recipients = data.get('to') or []
    if isinstance(recipients, str):
        recipients = [recipients]

    if event_type == 'email.bounced':
        bounce = data.get('bounce') or {}
        # Only permanent failures are suppressed. A transient bounce (full
        # mailbox, greylisting) resolves on its own and must stay deliverable.
        if str(bounce.get('type', '')).lower() != 'permanent':
            print(f"Resend soft bounce for {recipients}: {bounce.get('subType')}")
            return jsonify({"status": "ignored"}), 200
        reason, detail = 'hard_bounce', bounce.get('subType') or bounce.get('message')
    elif event_type == 'email.complained':
        reason, detail = 'complaint', 'marked as spam'
    else:
        # Delivered, opened, clicked, delayed: acknowledged so Resend stops
        # retrying, but nothing to record.
        return jsonify({"status": "ignored"}), 200

    try:
        conn = get_db_connection()
        for address in recipients:
            address = address.strip().lower()
            if not address:
                continue
            suppress_email(conn, address, reason, detail)
            print(f"Suppressed {address}: {reason} ({detail})")
        conn.commit()
        conn.close()
    except Exception as e:
        # 500 tells Svix to retry, which is what we want for a transient DB error.
        print(f"RESEND WEBHOOK ERROR ({event_type}): {e}")
        return jsonify({"error": "Could not record event"}), 500

    return jsonify({"status": "recorded"}), 200


@app.route('/user/<username>/tier', methods=['PATCH'])
def update_subscription_tier(username):
    if request.headers.get('X-Admin-Key') != os.environ.get('ADMIN_KEY'):
        return jsonify({"error": "Forbidden"}), 403

    tier_id = request.json.get('tier_id')
    if not isinstance(tier_id, int):
        return jsonify({"error": "tier_id must be an integer"}), 400

    conn = get_db_connection()
    tier = conn.execute("SELECT * FROM subscription_tiers WHERE id = ?", (tier_id,)).fetchone()
    if not tier:
        conn.close()
        return jsonify({"error": f"No tier with id {tier_id}"}), 404

    result = conn.execute(
        "UPDATE users SET subscription_tier_id = ? WHERE username = ?",
        (tier_id, username)
    )
    conn.commit()
    conn.close()

    if result.rowcount == 0:
        return jsonify({"error": "User not found"}), 404

    return jsonify({
        "message": "Tier updated",
        "username": username,
        "tier_id": tier["id"],
        "tier_name": tier["display_name"]
    }), 200

@app.route('/confirm/<token>')
def confirm_email(token):
    try:
        email = serializer.loads(token, salt='email-confirm', max_age=CONFIRM_TOKEN_MAX_AGE)
    except SignatureExpired:
        return _confirm_page(
            "This link has expired",
            "Confirmation links are valid for one hour. Request a new one from the login page.",
            ok=False
        ), 400
    except BadSignature:
        return _confirm_page(
            "This link isn't valid",
            "Check that you copied the whole link from the email.",
            ok=False
        ), 400

    conn = get_db_connection()
    result = conn.execute(
        "UPDATE users SET is_active = 1 WHERE username = ?", (email,)
    )
    conn.commit()
    conn.close()

    # rowcount is 0 only if the account was deleted after the token was issued —
    # a valid signature is no guarantee the user still exists.
    if result.rowcount == 0:
        return _confirm_page(
            "Account not found",
            "This account no longer exists. You can register again.",
            ok=False
        ), 404

    return _confirm_page(
        "Account confirmed",
        "You're all set — you can now log in."
    ), 200

@app.route('/login', methods=['POST'])
def login():
    data = request.json or {}
    # Must match the normalisation /register applies, or mixed-case input
    # silently fails to find the row.
    username = (data.get('username') or '').strip().lower()
    conn = get_db_connection()

    # Probe without recording: only failures below consume quota, so a user
    # signing in normally is never locked out of their own account.
    retry_after = rate_limit_retry_after(conn, 'login', client_ip(), record=False)
    if retry_after:
        conn.close()
        return too_many_requests(retry_after)

    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if user and user['is_active'] == 1:
        try:
            if ph.verify(user['password_hash'], data.get('password')):
                conn.close()
                token = serializer.dumps(username, salt='session')
                response = make_response(jsonify({"success": True, "newUser": user['has_completed_tour'] == 0}), 200)
                response.set_cookie(
                    "session",
                    value=token,
                    max_age=604800,
                    secure=True,
                    httponly=True,
                    samesite="Lax",
                    domain=".memoryillumination.com"
                )
                return response
        except VerifyMismatchError: pass

    # Reached only on failure: unknown user, unconfirmed account, or bad
    # password. Record the attempt so repeated guessing hits the limit.
    rate_limit_retry_after(conn, 'login', client_ip())
    conn.commit()
    conn.close()
    return jsonify({"error": "Unauthorized"}), 401

@app.route('/tour-complete', methods=['POST'])
def complete_tour():
    token = request.cookies.get('session', '')
    try:
        username = serializer.loads(token, salt='session', max_age=604800)
    except Exception:
        return jsonify({"error": "Unauthorized"}), 401

    conn = get_db_connection()
    conn.execute("UPDATE users SET has_completed_tour = 1 WHERE username = ?", (username,))
    conn.commit()
    conn.close()
    return jsonify({"success": True}), 200

@app.route('/logout', methods=['POST'])
def logout():
    response = make_response(jsonify({"success": True}), 200)
    response.set_cookie(
        "session",
        value="",
        max_age=0,
        secure=True,
        httponly=True,
        samesite="Lax",
        domain=".memoryillumination.com"
    )
    return response

WATERMARK_PATH = os.path.join(os.path.dirname(__file__), "MI_Watermark.png")

# Resolved once at startup so a missing asset costs one warning instead of an
# exception per upload. Watermarking is best-effort: if the file isn't there,
# free-tier output simply goes out unwatermarked.
WATERMARK_AVAILABLE = WATERMARK_FREE_TIER and os.path.exists(WATERMARK_PATH)
if WATERMARK_FREE_TIER and not WATERMARK_AVAILABLE:
    print(
        f"⚠️  WATERMARK_FREE_TIER is enabled but {WATERMARK_PATH} is missing — "
        "free-tier output will NOT be watermarked."
    )

def apply_watermark(image_bytes):
    output = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    logo = Image.open(WATERMARK_PATH).convert("RGBA")
    logo = logo.resize(output.size, Image.LANCZOS)

    logo_alpha = logo.split()[3]
    if logo_alpha.getextrema() == (255, 255):
        alpha = ImageOps.invert(logo.convert("L"))
    else:
        alpha = logo_alpha

    alpha = alpha.point(lambda x: int(x * 0.12))
    watermark = Image.new("RGBA", logo.size, (0, 0, 0, 0))
    watermark.putalpha(alpha)
    output.paste(watermark, (0, 0), watermark)

    buf = io.BytesIO()
    output.save(buf, format="PNG")
    return buf.getvalue()


class UnsupportedImageError(Exception):
    pass


class ImageTooLargeError(Exception):
    pass


# Only the formats we actually advertise. Pillow otherwise registers 43 decoders
# against untrusted bytes, including EPS — which it renders by shelling out to
# Ghostscript. Passing this to Image.open() cuts the native parser attack
# surface to the five we need.
ALLOWED_IMAGE_FORMATS = ['JPEG', 'PNG', 'WEBP', 'HEIF', 'AVIF']

# Pixel ceiling, checked from the header before any pixel buffer is allocated.
# Pillow's own bomb guard only *warns* at 89 MP and raises above 179 MP, by
# which point the memory is already committed: a 189 KB upload declaring
# 13000x13000 peaked at ~900 MB RSS.
#
# 64 MP clears every current phone camera — 50 MP sensors (Pixel 8, Galaxy S23)
# output 8192x6144 = 50.3 MP, so a 50 MP cap would reject them by a hair. Peak
# decode at this ceiling is still ~400 MB, so this bounds the blast radius
# rather than making it cheap; the per-IP upload limit is the other half.
MAX_UPLOAD_PIXELS = 64_000_000


def normalize_image(input_data):
    """
    Decode arbitrary upload bytes (JPEG/PNG/WEBP/HEIC/AVIF) via Pillow and
    re-encode as PNG, so downstream OpenCV and the diffusion worker only ever
    have to deal with one known-good format.

    Re-encoding also strips everything that is not pixels — appended payloads,
    EXIF, colour profiles — so nothing from the original file survives.
    """
    try:
        image = Image.open(io.BytesIO(input_data), formats=ALLOWED_IMAGE_FORMATS)
    except Exception:
        raise UnsupportedImageError(
            "Unsupported or corrupt image file. Please upload a JPEG, PNG, HEIC, AVIF, or WEBP photo."
        )

    # Dimensions come from the header, so this rejects a decompression bomb
    # before load() allocates anything.
    width, height = image.size
    if width * height > MAX_UPLOAD_PIXELS:
        raise ImageTooLargeError(
            f"That image is too large ({width}x{height}). "
            f"Please upload a photo under {MAX_UPLOAD_PIXELS // 1_000_000} megapixels."
        )

    try:
        image.load()
    except Exception:
        raise UnsupportedImageError(
            "Unsupported or corrupt image file. Please upload a JPEG, PNG, HEIC, AVIF, or WEBP photo."
        )

    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


# Dead code: the only call site (featureB in upload_file) is commented out,
# and this is the sole consumer of rembg.
# def simplify_for_coloring(image_bytes):
#     """
#     Pre-flatten pass so the diffusion worker traces an already-simplified
#     image instead of full photo detail:
#       1. Edge-preserving stylization smooths low-contrast texture (fabric,
#          wood grain) within the subject while keeping their outline/features.
#       2. rembg segmentation + white composite removes the background
#          entirely, since stylization alone only smooths texture and still
#          preserves background object edges (picture frames, furniture seams).
#     """
#     img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
#     stylized = cv2.stylization(img, sigma_s=150, sigma_r=0.5)
#     _, stylized_bytes = cv2.imencode(".png", stylized)
#
#     rgba_bytes = rembg_remove(stylized_bytes.tobytes(), session=REMBG_SESSION)
#     rgba = Image.open(io.BytesIO(rgba_bytes)).convert("RGBA")
#     white_bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
#     composited = Image.alpha_composite(white_bg, rgba).convert("RGB")
#
#     buf = io.BytesIO()
#     composited.save(buf, format="PNG")
#     return buf.getvalue()


# --- DEVELOPMENT-ONLY PATH (commented out) ---------------------------------
# The local loopback worker is reachable only when APP_ENV != 'production'.
# It is also the sole consumer of inference_params — the Modal worker takes
# image bytes alone. Uncomment this block and the branch in
# run_diffusion_workflow() together to restore local inference.
#
# WORKER_API_URL = "http://127.0.0.1:5001/generate"
#
# def run_local_diffusion_workflow(image_bytes, inference_params=None):
#     """
#     Ships raw user image bytes directly over local loopback to our continuous
#     diffusers worker engine on port 5001 and returns the finished line art bytes.
#     inference_params (optional dict) may carry prompt/num_inference_steps/
#     guidance_scale overrides for tuning without restarting the worker.
#     """
#     try:
#         # Wrap the raw image into standard multipart form data
#         files = {'image': ('input.png', image_bytes, 'image/png')}
#         data = {k: v for k, v in (inference_params or {}).items() if v is not None}
#
#         # Dispatch to our persistent background worker daemon
#         print("Routing image payload to hot local VRAM engine...")
#         response = requests.post(WORKER_API_URL, files=files, data=data, timeout=90)
#
#         if response.status_code != 200:
#             print(f"❌ Worker rejected payload: {response.text}")
#             raise ValueError(f"Inference Engine Error: {response.text}")
#
#         return response.content
#     except Exception as e:
#         print(f"❌ Loopback communication failure to model worker: {e}")
#         raise e
# ---------------------------------------------------------------------------


def run_remote_diffusion_workflow(image_bytes):
    """
    SUPERSEDED for the request path by spawn_remote_diffusion(): /upload-endpoint
    no longer blocks on the GPU. Kept because the commented-out local-inference
    restoration above is written in terms of run_diffusion_workflow(); delete
    both once that path is either restored or abandoned.

    Dispatches to the Modal-hosted GPU worker (backend/flux_1_kontext_modal.py).
    Only reachable when APP_ENV=production; `modal` is imported lazily so it
    isn't a hard dependency for local development.
    """
    import modal
    try:
        remote_model = modal.Cls.from_name("coloring-book-flux", "ColoringModel")
        result = remote_model().process.remote(image_bytes)
        return result["flux_sketch"]
    except Exception as e:
        print(f"❌ Modal remote inference failure: {e}")
        raise e


def run_diffusion_workflow(image_bytes, inference_params=None):
    # Development branch commented out along with run_local_diffusion_workflow.
    # Every caller now reaches Modal regardless of APP_ENV.
    # if APP_ENV != 'production':
    #     return run_local_diffusion_workflow(image_bytes, inference_params)
    return run_remote_diffusion_workflow(image_bytes)

# --- ASYNC GPU JOBS ---

# Wall-clock profiles that drive the client's progress bar. These are estimates,
# not measurements: the client anchors them to real events (upload completion,
# poll-confirmed elapsed time, actual completion) and eases between those
# anchors. Tune from the "⏱️  upload-endpoint timing" and "⏱️  modal worker
# timing" lines in the logs.
WARM_ESTIMATE_SECONDS = 20   # container already up: inference + transfer only
COLD_ESTIMATE_SECONDS = 80   # adds @modal.enter() loading FLUX bf16 onto the A100

# flux_1_kontext_modal.py sets scaledown_window=300, so a container idle longer
# than that is gone and the next call pays a cold start. Held slightly under the
# real window: predicting cold and finishing early reads better than the reverse.
CONTAINER_IDLE_TIMEOUT = 270

# Job rows are metadata only. Keep a day for debugging, then sweep.
JOB_RETENTION_SECONDS = 86400


def predict_cold_start(conn):
    """
    True if the next Modal call will likely wait on a container boot.

    Modal's API cannot tell us this directly: InputStatus.PENDING covers both
    "queued, no container yet" and "actively running", and get_call_graph() is
    documented as best-effort and not real-time. So we infer it from our own
    dispatch history, which tracks container warmth closely enough to pick a
    duration profile.
    """
    row = conn.execute(
        "SELECT MAX(COALESCE(finished_at, created_at)) AS last_seen FROM jobs"
    ).fetchone()
    last_seen = row['last_seen'] if row else None
    if last_seen is None:
        return True
    return (int(time.time()) - last_seen) > CONTAINER_IDLE_TIMEOUT


def estimate_seconds(cold_start):
    return COLD_ESTIMATE_SECONDS if cold_start else WARM_ESTIMATE_SECONDS


def job_owner(token, ip):
    """
    Identity a job is bound to, so one caller cannot poll another's job.
    Falls back to IP for the anonymous uploads this endpoint still allows.
    """
    try:
        return f"user:{serializer.loads(token, salt='session', max_age=604800)}"
    except Exception:
        return f"ip:{ip}"


def spawn_remote_diffusion(image_bytes):
    """Dispatch to the Modal GPU worker without waiting; returns the call id."""
    import modal
    remote_model = modal.Cls.from_name("coloring-book-flux", "ColoringModel")
    return remote_model().process.spawn(image_bytes).object_id


def fetch_job_result(call_id):
    """
    ('pending', None) | ('done', png_bytes) | ('error', message)

    get(timeout=0) polls without blocking: TimeoutError means the worker is
    still going, OutputExpiredError means Modal has aged the result out (it
    retains outputs for ~7 days).
    """
    import modal
    try:
        result = modal.FunctionCall.from_id(call_id).get(timeout=0)
    except TimeoutError:
        return 'pending', None
    except modal.exception.OutputExpiredError:
        return 'error', "That result expired before it was downloaded. Please try again."
    except Exception as e:
        print(f"❌ Modal job {call_id} failed: {e}")
        return 'error', "Something went wrong processing that image. Please try again."
    return 'done', result["flux_sketch"]


def load_job(conn, job_id, owner):
    """The job row, but only for the caller that created it."""
    return conn.execute(
        "SELECT * FROM jobs WHERE id = ? AND owner = ?", (job_id, owner)
    ).fetchone()


def sweep_old_jobs(conn):
    if random.random() < 0.05:
        conn.execute(
            "DELETE FROM jobs WHERE created_at < ?",
            (int(time.time()) - JOB_RETENTION_SECONDS,)
        )


@app.route('/upload-endpoint', methods=['POST'])
def upload_file():
    t_start = time.perf_counter()

    file     = request.files['myFile']
    settings = json.loads(request.form.get('settings', '{}'))
    token    = request.cookies.get('session', '')

    # Checked before reading the upload body: this endpoint needs no session, so
    # the per-IP limit is the only thing standing between an anonymous caller
    # and unbounded Modal GPU spend.
    ip = client_ip()
    conn = get_db_connection()
    retry_after = rate_limit_retry_after(conn, 'upload', ip)
    if not retry_after and settings.get('featureB'):
        retry_after = rate_limit_retry_after(conn, 'upload_gpu', ip)
    conn.commit()
    conn.close()
    if retry_after:
        return too_many_requests(retry_after)

    input_data = file.read()

    t_read = time.perf_counter()

    try:
        input_data = normalize_image(input_data)
    except UnsupportedImageError as e:
        return jsonify({"error": str(e)}), 400
    except ImageTooLargeError as e:
        return jsonify({"error": str(e)}), 413

    t_normalize = time.perf_counter()

    # Verify signed token and check subscription status (Keep unchanged)
    is_free_tier = False
    try:
        username = serializer.loads(token, salt='session', max_age=604800)
        conn = get_db_connection()
        user = conn.execute(
            "SELECT subscription_tier_id FROM users WHERE username = ?", (username,)
        ).fetchone()
        conn.close()
        if user and user['subscription_tier_id'] == 1:
            is_free_tier = True
    except Exception:
        is_free_tier = True

    t_auth = time.perf_counter()

    # Execution Routing Split.
    #
    # The GPU path is dispatched asynchronously. It can run for a minute or
    # more, and pinning one of only 4 uWSGI workers for that long is what
    # breaks first behind a proxy timeout. The caller gets a job id and polls
    # /jobs/<id>. The OpenCV path is sub-second, so it stays synchronous and
    # returns the PNG directly — the client branches on the response type.
    if settings.get('featureB'):
        # Everything else that used to live in this branch is commented out:
        #
        # inference_params was only ever read by the local loopback worker:
        # inference_params = {
        #     'prompt': settings.get('prompt'),
        #     'num_inference_steps': settings.get('num_inference_steps'),
        #     'guidance_scale': settings.get('guidance_scale'),
        # }
        #
        # rembg/cv2 pre-flatten pass, now skipped entirely:
        # pre_simplified = simplify_for_coloring(input_data)
        conn = get_db_connection()
        try:
            cold_start = predict_cold_start(conn)
            call_id = spawn_remote_diffusion(input_data)
        except Exception as e:
            conn.close()
            print(f"❌ Modal dispatch failure: {e}")
            return jsonify({"error": "Something went wrong processing that image. Please try again."}), 500

        job_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO jobs (id, call_id, owner, is_free_tier, cold_start, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (job_id, call_id, job_owner(token, ip), int(is_free_tier), int(cold_start), int(time.time()))
        )
        sweep_old_jobs(conn)
        conn.commit()
        conn.close()

        print(
            "⏱️  upload-endpoint dispatch — "
            f"read: {t_read - t_start:.3f}s, "
            f"normalize: {t_normalize - t_read:.3f}s, "
            f"auth: {t_auth - t_normalize:.3f}s, "
            f"job: {job_id}, cold_start: {cold_start}"
        )
        return jsonify({
            "job_id": job_id,
            "cold_start": cold_start,
            "estimate_seconds": estimate_seconds(cold_start),
        }), 202

    try:
        # High speed OpenCV classic fallback path
        img = cv2.imdecode(np.frombuffer(input_data, np.uint8), cv2.IMREAD_GRAYSCALE)
        inv = 255 - img
        blur = cv2.GaussianBlur(inv, (21, 21), 0)
        sketch = cv2.divide(img, 255 - blur, scale=256)
        _, buffer = cv2.imencode(".png", sketch)
        result_bytes = bytes(buffer)
    except Exception as e:
        print(f"❌ Image processing failure: {e}")
        return jsonify({"error": "Something went wrong processing that image. Please try again."}), 500

    t_process = time.perf_counter()

    if WATERMARK_AVAILABLE and is_free_tier:
        # Best-effort: a watermarking failure must not cost the user the image
        # they already paid GPU time for, so fall back to the unmarked result.
        try:
            result_bytes = apply_watermark(result_bytes)
        except Exception as e:
            print(f"⚠️  Watermarking failed, returning unwatermarked image: {e}")

    print(
        "⏱️  upload-endpoint timing — "
        f"read: {t_read - t_start:.3f}s, "
        f"normalize: {t_normalize - t_read:.3f}s, "
        f"auth: {t_auth - t_normalize:.3f}s, "
        f"process: {t_process - t_auth:.3f}s, "
        f"total: {t_process - t_start:.3f}s"
    )

    return send_file(io.BytesIO(result_bytes), mimetype='image/png')


def job_phase(row, elapsed):
    """
    Coarse label for what the worker is most likely doing right now.

    A cold start spends its first stretch in @modal.enter() loading FLUX onto
    the GPU before denoising begins, so the difference between the two duration
    profiles is roughly the model-load window. Predicted, not observed — see
    predict_cold_start().
    """
    if row['cold_start'] and elapsed < (COLD_ESTIMATE_SECONDS - WARM_ESTIMATE_SECONDS):
        return 'warming'
    return 'generating'


@app.route('/jobs/<job_id>', methods=['GET'])
def job_status(job_id):
    ip = client_ip()
    owner = job_owner(request.cookies.get('session', ''), ip)

    conn = get_db_connection()
    row = load_job(conn, job_id, owner)
    if row is None:
        conn.close()
        return jsonify({"error": "Job not found."}), 404

    elapsed = int(time.time()) - row['created_at']
    payload = {
        "status": row['status'],
        "elapsed": elapsed,
        "cold_start": bool(row['cold_start']),
        "estimate_seconds": estimate_seconds(row['cold_start']),
    }

    # Terminal states are recorded, so don't call Modal again for them.
    if row['status'] != 'pending':
        conn.close()
        if row['status'] == 'error':
            payload['error'] = row['error']
        return jsonify(payload), 200

    status, result = fetch_job_result(row['call_id'])

    if status == 'pending':
        conn.close()
        payload['phase'] = job_phase(row, elapsed)
        return jsonify(payload), 200

    # Record the terminal state so later polls (and a reload) are answered from
    # SQLite. The bytes stay with Modal; /jobs/<id>/result re-fetches them.
    error = result if status == 'error' else None
    conn.execute(
        "UPDATE jobs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
        (status, error, int(time.time()), job_id)
    )
    conn.commit()
    conn.close()

    payload['status'] = status
    if status == 'error':
        payload['error'] = error
    return jsonify(payload), 200


@app.route('/jobs/<job_id>/result', methods=['GET'])
def job_result(job_id):
    ip = client_ip()
    owner = job_owner(request.cookies.get('session', ''), ip)

    conn = get_db_connection()
    row = load_job(conn, job_id, owner)
    conn.close()
    if row is None:
        return jsonify({"error": "Job not found."}), 404

    status, result = fetch_job_result(row['call_id'])
    if status == 'pending':
        return jsonify({"error": "That image isn't ready yet."}), 409
    if status == 'error':
        return jsonify({"error": result}), 500

    result_bytes = result
    # Tier was captured when the job was submitted, so a mid-job upgrade or
    # session expiry can't change the terms the image was generated under.
    if WATERMARK_AVAILABLE and row['is_free_tier']:
        # Best-effort, matching /upload-endpoint: a watermarking failure must
        # not cost the user the image they already paid GPU time for.
        try:
            result_bytes = apply_watermark(result_bytes)
        except Exception as e:
            print(f"⚠️  Watermarking failed, returning unwatermarked image: {e}")

    return send_file(io.BytesIO(result_bytes), mimetype='image/png')


if __name__ == '__main__':
    # Listening on all interfaces for network access
    app.run(port=5000, host="0.0.0.0")
