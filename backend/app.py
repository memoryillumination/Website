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
from utils import rate_limit, email, http

pillow_heif.register_heif_opener()

# .env discovery has to satisfy two different layouts:
#
#   repo    <root>/.env with app.py in <root>/backend/, so one file serves the
#           backend and the frontend config generator.
#   deployed backend/ is flattened -- backend/uwsgi sets chdir=/srv/memory-
#           illumination with module=app, so app.py IS the root and .env sits
#           beside it. Assuming the repo layout here resolved the root to /srv
#           and silently loaded nothing.
#
# Nearest first, matching normal dotenv precedence.
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_CANDIDATES = (
    os.path.join(BACKEND_DIR, '.env'),
    os.path.join(os.path.dirname(BACKEND_DIR), '.env'),
)
ENV_PATH = next((p for p in ENV_CANDIDATES if os.path.exists(p)), None)
if ENV_PATH:
    load_dotenv(ENV_PATH)
else:
    load_dotenv()  # fall back to python-dotenv's own upward search

# Was loaded once at startup so per-request calls to simplify_for_coloring()
# didn't pay session/model init cost on every upload.
# REMBG_SESSION = rembg_new_session('u2net')

# Two orthogonal switches, replacing the old single APP_ENV:
#
# DEPLOY_ENV -- where the frontend is served relative to this API. In
#   development both run on this machine (frontend :8000, API :5000) so the
#   browser talks to localhost and CORS must allow it; in production they are
#   hosted separately. Defaults to production so a missing value fails closed
#   on CORS rather than quietly allowing localhost in a deployed environment.
#
# INFERENCE_BACKEND -- where diffusion runs, which is a genuinely separate
#   question: you can point a local frontend at the Modal GPU, or run the local
#   worker behind a production frontend.
def _require_choice(name, default, choices):
    """
    Read a setting and fail loudly on anything unrecognised.

    An unvalidated DEPLOY_ENV fails *open*: 'prod' or 'Production' is simply
    not equal to 'production', so the app would quietly drop into development
    mode on a live host -- session cookies losing Secure and their domain, and
    CORS accepting localhost and RFC1918 origins with credentials. Refusing to
    start is the safer failure.
    """
    value = os.environ.get(name, default).strip().lower()
    if value not in choices:
        raise RuntimeError(
            f"{name} must be one of {sorted(choices)}, got {value!r}. "
            f"Checked .env at: {ENV_PATH or 'not found'}"
        )
    return value


DEPLOY_ENV = _require_choice('DEPLOY_ENV', 'production', {'development', 'production'})
IS_PRODUCTION = DEPLOY_ENV == 'production'

INFERENCE_BACKEND = _require_choice('INFERENCE_BACKEND', 'modal', {'local', 'modal'})
USE_MODAL = INFERENCE_BACKEND == 'modal'

# The local worker is flux_1_kontext.py, a separate Flask process holding the
# quantised model resident on the Intel XPU. Started independently of this app.
LOCAL_WORKER_URL = os.environ.get('LOCAL_WORKER_URL', 'http://127.0.0.1:5001/generate')

# When True, output for free-tier (and unauthenticated) callers is watermarked.
# Off by default because MI_LOGO_wordmark_transparent.png is not checked into the repo — see the
# availability check next to WATERMARK_PATH below. If this is turned on while
# the asset is missing, startup logs a warning and uploads continue
# unwatermarked rather than failing.
WATERMARK_FREE_TIER = False

app = Flask(__name__)

# Defence in depth behind Nginx's `client_max_body_size 50M` — Flask rejects
# oversized bodies with 413 even if the Nginx config drifts or another
# front-end is put in place.
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

# Origins are derived from DEPLOY_ENV but can be overridden explicitly. The
# localhost entries are added only outside production: allowing them there
# would let a page on any visitor's own machine call this API with credentials.
SITE_BASE_URL = os.environ.get(
    'SITE_BASE_URL',
    'https://memoryillumination.com' if IS_PRODUCTION else 'http://localhost:8000',
)
API_BASE_URL = os.environ.get(
    'API_BASE_URL',
    'https://api.memoryillumination.com' if IS_PRODUCTION else 'http://localhost:5000',
)

# Port the frontend dev server listens on, used to build the permitted origin.
FRONTEND_PORT = os.environ.get('FRONTEND_PORT', '8000')

if IS_PRODUCTION:
    CORS_ORIGINS = [SITE_BASE_URL]
else:
    # In development the frontend is reachable over the LAN as well as on
    # loopback -- browsing from a phone or another machine at
    # http://192.168.1.116:8000 sends that as the Origin -- so match loopback
    # plus the RFC1918 private ranges on the frontend port.
    #
    # A literal '*' is not an option: supports_credentials requires a concrete
    # origin to echo back, and browsers reject a wildcard with credentials.
    # Restricting to private ranges keeps this from being an open door if
    # DEPLOY_ENV is ever wrong on a public host.
    #
    # A compiled pattern is passed rather than a regex-looking string because
    # flask-cors treats re.Pattern explicitly instead of guessing.
    CORS_ORIGINS = [re.compile(
        r'^http://('
        r'localhost'
        r'|127\.\d{1,3}\.\d{1,3}\.\d{1,3}'
        r'|\[::1\]'
        r'|10\.\d{1,3}\.\d{1,3}\.\d{1,3}'
        r'|192\.168\.\d{1,3}\.\d{1,3}'
        r'|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}'
        r'):' + re.escape(FRONTEND_PORT) + r'$'
    )]

CORS(app, supports_credentials=True, origins=CORS_ORIGINS)

# The session cookie is scoped to the real domain in production. Locally that
# domain cannot be set by a localhost origin at all, and Secure would be
# rejected over plain http, so both are relaxed outside production.
COOKIE_DOMAIN = '.memoryillumination.com' if IS_PRODUCTION else None
COOKIE_SECURE = IS_PRODUCTION

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

if not app.config['SECRET_KEY']:
    # Without this URLSafeTimedSerializer(None) raises an opaque TypeError at
    # import and every uWSGI worker dies with no indication why.
    raise RuntimeError(
        "SECRET_KEY is not set. Checked .env at: "
        + (ENV_PATH or f"none found; looked in {', '.join(ENV_CANDIDATES)}")
    )

serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
ph = PasswordHasher()

# Support a separate database for smoke testing so test runs never corrupt
# or collide with the production / dev users.db.
_SMOKE_TEST_DB = os.environ.get("SMOKE_TEST_DB", "").strip()
DB_NAME = os.path.join(os.path.dirname(__file__), _SMOKE_TEST_DB if _SMOKE_TEST_DB else "users.db")

# When set, requests authenticated as this user bypass all rate limits.
SMOKE_TEST_USER = os.environ.get("SMOKE_TEST_USER", "").strip()


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
# Functions moved to utils/rate_limit.py

def _is_smoke_test_request():
    """Check if the current request is authenticated as the smoke-test user.

    Used to bypass rate limits for automated smoke tests that need to upload
    repeatedly without hitting the per-IP caps.
    """
    if not SMOKE_TEST_USER:
        return False
    token = request.cookies.get('session', '')
    if not token:
        return False
    try:
        username = serializer.loads(token, salt='session', max_age=604800)
        return username == SMOKE_TEST_USER
    except Exception:
        return False


# --- EMAIL CONFIRMATION ---

# Email suppression functions moved to utils/email.py

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

# Auth routes defined in auth/routes.py
def _register_auth_routes():
    from auth.routes import register_routes
    register_routes(app, get_db_connection, ph, serializer, DEPLOY_ENV, IS_PRODUCTION,
                   EMAIL_RE, MAIL_FROM, MAIL_REPLY_TO, COOKIE_DOMAIN,
                   confirmation_quota_remaining, send_confirmation_email, _confirm_page,
                   verify_webhook_signature)
_register_auth_routes()



def apply_watermark(image_bytes):
    output = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    logo = Image.open(WATERMARK_PATH).convert("RGBA")

    # Scale logo to 1/10 of image width
    logo_w = max(output.width // 10, 50)
    aspect = logo.height / max(logo.width, 1)
    logo = logo.resize((logo_w, int(logo_w * aspect)), Image.LANCZOS)

    # Diagonal tiling parameters
    gap = logo_w // 3
    diag = logo_w  # per-row diagonal offset
    cols = (output.width // (logo_w + gap)) + 4
    rows = (output.height // (logo_w + gap)) + 4

    # Build an oversized tile canvas
    canvas_w = cols * (logo_w + gap) + diag + logo_w
    canvas_h = rows * (logo_w + gap) + diag + logo_w
    tile_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    for r in range(rows):
        for c in range(cols):
            x = c * (logo_w + gap) + r * diag
            y = r * (logo_w + gap)
            tile_layer.paste(logo, (x, y), logo)

    # Apply 12% opacity via alpha channel
    alpha = tile_layer.split()[3]
    alpha = alpha.point(lambda a: int(a * 0.12))
    tile_layer.putalpha(alpha)

    # Center the tile layer over the output image
    ox = (canvas_w - output.width) // 2
    oy = (canvas_h - output.height) // 2
    output.paste(tile_layer, (ox, oy), tile_layer)

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
def run_local_diffusion_workflow(image_bytes, inference_params=None):
    """
    Ships raw user image bytes over loopback to the resident diffusers worker
    (flux_1_kontext.py, port 5001) and returns the finished line art bytes.

    Synchronous by nature: the worker answers with the PNG on the same request,
    so this path does NOT create a job row and the polling machinery is not
    exercised. Set INFERENCE_BACKEND=modal to test that.

    inference_params (optional dict) may carry prompt/num_inference_steps/
    guidance_scale overrides for tuning without restarting the worker.
    """
    try:
        files = {'image': ('input.png', image_bytes, 'image/png')}
        data = {k: v for k, v in (inference_params or {}).items() if v is not None}

        print(f"Routing image payload to local worker at {LOCAL_WORKER_URL}...")
        response = requests.post(LOCAL_WORKER_URL, files=files, data=data, timeout=180)

        if response.status_code != 200:
            print(f"❌ Worker rejected payload: {response.text}")
            raise ValueError(f"Inference Engine Error: {response.text}")

        return response.content
    except Exception as e:
        print(f"❌ Loopback communication failure to model worker: {e}")
        raise e
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
    """
    Synchronous diffusion, used when INFERENCE_BACKEND=local. The Modal path
    does not come through here: it is dispatched asynchronously straight from
    /upload-endpoint via spawn_remote_diffusion().
    """
    if not USE_MODAL:
        return run_local_diffusion_workflow(image_bytes, inference_params)
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

# Ceiling on retrying a job we cannot reach Modal about. flux_1_kontext_modal.py
# sets the function timeout to 600s, so past this the worker cannot still be
# running and the job is genuinely lost rather than merely unreachable.
JOB_MAX_AGE_SECONDS = 660


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


# Modal failures that say nothing about the job itself, only about our ability
# to ask after it right now. Resolved by name and cached on first use, so a
# modal version that renames or drops one of these degrades to "not transient"
# rather than breaking the import.
_MODAL_TRANSIENT_ERRORS = None

def modal_transient_errors():
    global _MODAL_TRANSIENT_ERRORS
    if _MODAL_TRANSIENT_ERRORS is None:
        import modal
        names = (
            'ConnectionError',        # could not reach the Modal servers
            'ServiceError',           # basic client/server communication failed
            'InternalError',          # internal error on Modal's side
            'InternalFailure',        # documented as explicitly retriable
            'ResourceExhaustedError', # server-side resource exhausted
            'ClientClosed',
        )
        _MODAL_TRANSIENT_ERRORS = tuple(
            getattr(modal.exception, n) for n in names if hasattr(modal.exception, n)
        )
    return _MODAL_TRANSIENT_ERRORS


def fetch_job_result(call_id):
    """
    ('pending', None) | ('transient', message) | ('done', png_bytes) | ('error', message)

    The 'transient' state is the important distinction: a failure to reach
    Modal is not a failed job, and callers must not record it as terminal —
    the GPU work may well have succeeded. Only 'error' is a verdict on the job.
    """
    import modal
    try:
        result = modal.FunctionCall.from_id(call_id).get(timeout=0)
    except TimeoutError:
        # The *builtin* TimeoutError: the worker is still going. Modal's own
        # exception.TimeoutError is a separate class deriving from modal.Error,
        # so this clause does not shadow the two modal timeouts caught below.
        return 'pending', None
    except modal.exception.OutputExpiredError:
        return 'error', "That result expired before it was downloaded. Please try again."
    except modal.exception.FunctionTimeoutError:
        return 'error', "That image took too long to generate. Please try again."
    except modal_transient_errors() as e:
        print(f"⚠️  Modal unreachable for job {call_id} ({type(e).__name__}: {e})")
        return 'transient', "Could not reach the image worker."
    except Exception as e:
        # Anything else is the job itself failing, including whatever the
        # remote function raised, which arrives here as its own type.
        print(f"❌ Modal job {call_id} failed ({type(e).__name__}): {e}")
        return 'error', "Something went wrong processing that image. Please try again."

    try:
        return 'done', result["flux_sketch"]
    except (KeyError, TypeError) as e:
        # Kept inside a guard so a payload-shape change cannot escape as a 500
        # and strand the job row as pending.
        print(f"❌ Modal job {call_id} returned an unexpected payload: {e!r}")
        return 'error', "Something went wrong processing that image. Please try again."


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


# Upload endpoint defined in upload.py
def _register_upload_routes():
    from upload import register_routes
    register_routes(app, get_db_connection, serializer, DEPLOY_ENV, IS_PRODUCTION,
                   COOKIE_DOMAIN, normalize_image, UnsupportedImageError,
                   ImageTooLargeError, _is_smoke_test_request,
                   predict_cold_start, spawn_remote_diffusion, job_owner,
                   sweep_old_jobs, estimate_seconds, run_diffusion_workflow,
                   apply_watermark, WATERMARK_AVAILABLE, USE_MODAL,
                   COLD_ESTIMATE_SECONDS, WARM_ESTIMATE_SECONDS)
_register_upload_routes()


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
    ip = http.client_ip()
    owner = job_owner(request.cookies.get('session', ''), ip)

    # try/finally rather than close-per-branch: an unexpected raise between here
    # and the end used to leak the connection for the life of the worker.
    conn = get_db_connection()
    try:
        row = load_job(conn, job_id, owner)
        if row is None:
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
            if row['status'] == 'error':
                payload['error'] = row['error']
            return jsonify(payload), 200

        status, result = fetch_job_result(row['call_id'])

        if status == 'transient':
            # We could not reach Modal, which is not a verdict on the job — the
            # GPU work may have succeeded. Recording it as terminal here would
            # permanently kill a job over one blip, so report it as still
            # pending and let the next poll ask again. Give up only once the
            # worker could not possibly still be running.
            if elapsed <= JOB_MAX_AGE_SECONDS:
                status = 'pending'
            else:
                status = 'error'
                result = "Lost contact with the image worker. Please try again."

        if status == 'pending':
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

        payload['status'] = status
        if status == 'error':
            payload['error'] = error
        return jsonify(payload), 200
    finally:
        conn.close()


@app.route('/jobs/<job_id>/result', methods=['GET'])
def job_result(job_id):
    ip = http.client_ip()
    owner = job_owner(request.cookies.get('session', ''), ip)

    conn = get_db_connection()
    row = load_job(conn, job_id, owner)
    conn.close()
    if row is None:
        return jsonify({"error": "Job not found."}), 404

    status, result = fetch_job_result(row['call_id'])
    if status == 'pending':
        return jsonify({"error": "That image isn't ready yet."}), 409
    if status == 'transient':
        # 503, not 500: the image is probably fine and retrying is the right move.
        response = jsonify({"error": "Couldn't reach the image worker. Please try again in a moment."})
        response.headers['Retry-After'] = '5'
        return response, 503
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
