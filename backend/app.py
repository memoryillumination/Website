import os, io, re, cv2, json, sqlite3
import numpy as np
from PIL import Image, ImageOps
import pillow_heif
import pillow_avif  # noqa: F401  (importing registers the AVIF plugin with Pillow)
from rembg import remove as rembg_remove, new_session as rembg_new_session
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

# Loaded once at startup so per-request calls to simplify_for_coloring() don't
# pay session/model init cost on every upload.
REMBG_SESSION = rembg_new_session('u2net')

# APP_ENV selects which GPU backend featureB routes to: "development" (default)
# uses the local worker over loopback HTTP; "production" uses the Modal-hosted
# worker. Set APP_ENV=production in .env to switch.
APP_ENV = os.environ.get('APP_ENV', 'development')

app = Flask(__name__)
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

    conn.commit()
    conn.close()

init_db()


# --- EMAIL CONFIRMATION ---

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

        if should_send and confirmation_quota_remaining(conn, username):
            conn.execute(
                "INSERT INTO confirmation_sends (email, sent_at) VALUES (?, ?)",
                (username, int(time.time()))
            )
            conn.commit()
            send_confirmation_email(username)

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
        user = conn.execute(
            "SELECT is_active FROM users WHERE username = ?", (username,)
        ).fetchone()

        if user is not None and user['is_active'] == 0 and confirmation_quota_remaining(conn, username):
            conn.execute(
                "INSERT INTO confirmation_sends (email, sent_at) VALUES (?, ?)",
                (username, int(time.time()))
            )
            conn.commit()
            send_confirmation_email(username)

        conn.close()
    except Exception as e:
        print(f"RESEND CONFIRMATION ERROR for {username}: {e}")

    return jsonify(REGISTER_OK), 200

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
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if user and user['is_active'] == 1:
        try:
            if ph.verify(user['password_hash'], data.get('password')):
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


def normalize_image(input_data):
    """
    Decode arbitrary upload bytes (JPEG/PNG/WEBP/HEIC/AVIF/...) via Pillow and
    re-encode as PNG, so downstream OpenCV and the diffusion worker only ever
    have to deal with one known-good format.
    """
    try:
        image = Image.open(io.BytesIO(input_data))
        image.load()
    except Exception:
        raise UnsupportedImageError("Unsupported or corrupt image file. Please upload a JPEG, PNG, HEIC, AVIF, or WEBP photo.")

    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def simplify_for_coloring(image_bytes):
    """
    Pre-flatten pass so the diffusion worker traces an already-simplified
    image instead of full photo detail:
      1. Edge-preserving stylization smooths low-contrast texture (fabric,
         wood grain) within the subject while keeping their outline/features.
      2. rembg segmentation + white composite removes the background
         entirely, since stylization alone only smooths texture and still
         preserves background object edges (picture frames, furniture seams).
    """
    img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    stylized = cv2.stylization(img, sigma_s=150, sigma_r=0.5)
    _, stylized_bytes = cv2.imencode(".png", stylized)

    rgba_bytes = rembg_remove(stylized_bytes.tobytes(), session=REMBG_SESSION)
    rgba = Image.open(io.BytesIO(rgba_bytes)).convert("RGBA")
    white_bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    composited = Image.alpha_composite(white_bg, rgba).convert("RGB")

    buf = io.BytesIO()
    composited.save(buf, format="PNG")
    return buf.getvalue()


WORKER_API_URL = "http://127.0.0.1:5001/generate"

def run_local_diffusion_workflow(image_bytes, inference_params=None):
    """
    Ships raw user image bytes directly over local loopback to our continuous
    diffusers worker engine on port 5001 and returns the finished line art bytes.
    inference_params (optional dict) may carry prompt/num_inference_steps/
    guidance_scale overrides for tuning without restarting the worker.
    """
    try:
        # Wrap the raw image into standard multipart form data
        files = {'image': ('input.png', image_bytes, 'image/png')}
        data = {k: v for k, v in (inference_params or {}).items() if v is not None}

        # Dispatch to our persistent background worker daemon
        print("Routing image payload to hot local VRAM engine...")
        response = requests.post(WORKER_API_URL, files=files, data=data, timeout=90)

        if response.status_code != 200:
            print(f"❌ Worker rejected payload: {response.text}")
            raise ValueError(f"Inference Engine Error: {response.text}")

        return response.content
    except Exception as e:
        print(f"❌ Loopback communication failure to model worker: {e}")
        raise e


def run_remote_diffusion_workflow(image_bytes):
    """
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
    if APP_ENV == 'production':
        return run_remote_diffusion_workflow(image_bytes)
    return run_local_diffusion_workflow(image_bytes, inference_params)

@app.route('/upload-endpoint', methods=['POST'])
def upload_file():
    t_start = time.perf_counter()

    file     = request.files['myFile']
    settings = json.loads(request.form.get('settings', '{}'))
    token    = request.cookies.get('session', '')
    input_data = file.read()

    t_read = time.perf_counter()

    try:
        input_data = normalize_image(input_data)
    except UnsupportedImageError as e:
        return jsonify({"error": str(e)}), 400

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

    try:
        # Execution Routing Split
        if settings.get('featureB'):
            # Pass off payload to the local or Modal GPU worker, per APP_ENV
            inference_params = {
                'prompt': settings.get('prompt'),
                'num_inference_steps': settings.get('num_inference_steps'),
                'guidance_scale': settings.get('guidance_scale'),
            }
            pre_simplified = simplify_for_coloring(input_data)
            result_bytes = run_diffusion_workflow(pre_simplified, inference_params)
        else:
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

    #if is_free_tier:
    #    result_bytes = apply_watermark(result_bytes)

    print(
        "⏱️  upload-endpoint timing — "
        f"read: {t_read - t_start:.3f}s, "
        f"normalize: {t_normalize - t_read:.3f}s, "
        f"auth: {t_auth - t_normalize:.3f}s, "
        f"process: {t_process - t_auth:.3f}s, "
        f"total: {t_process - t_start:.3f}s"
    )

    return send_file(io.BytesIO(result_bytes), mimetype='image/png')

if __name__ == '__main__':
    # Listening on all interfaces for network access
    app.run(port=5000, host="0.0.0.0")
