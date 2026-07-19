import os, io, cv2, json, sqlite3
import numpy as np
from PIL import Image, ImageOps
import pillow_heif
import pillow_avif  # noqa: F401  (importing registers the AVIF plugin with Pillow)
from flask import Flask, request, jsonify, send_file, make_response
from flask_cors import CORS
from flask_mail import Mail, Message
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import URLSafeTimedSerializer
from dotenv import load_dotenv
import random
import time
import requests

pillow_heif.register_heif_opener()

load_dotenv()

# APP_ENV selects which GPU backend featureB routes to: "development" (default)
# uses the local worker over loopback HTTP; "production" uses the Modal-hosted
# worker. Set APP_ENV=production in .env to switch.
APP_ENV = os.environ.get('APP_ENV', 'development')

app = Flask(__name__)
CORS(app, supports_credentials=True, origins=["https://memoryillumination.com"])

# Config
app.config.update(
    MAIL_SERVER='smtp.gmail.com',
    MAIL_PORT=587,
    MAIL_USE_TLS=True,
    SECRET_KEY=os.environ.get('SECRET_KEY'),
    MAIL_USERNAME=os.environ.get('EMAIL_USER'),
    MAIL_PASSWORD=os.environ.get('EMAIL_PASS'),
    MAIL_DEFAULT_SENDER=os.environ.get('EMAIL_USER')
)

mail = Mail(app)
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

    conn.commit()
    conn.close()

init_db()

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    username, password = data.get('username'), data.get('password')
    try:
        hash_pw = ph.hash(password)
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO users (username, password_hash, is_active, subscription_tier_id) VALUES (?, ?, 1, 1)",
            (username, hash_pw)
        )
        conn.commit()
        conn.close()
        return jsonify({"message": "Registration successful"}), 201
    except Exception as e:
        print(f"REGISTRATION ERROR: {e}")
        return jsonify({"error": str(e)}), 500

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
        email = serializer.loads(token, salt='email-confirm', max_age=3600)
        conn = get_db_connection()
        conn.execute("UPDATE users SET is_active = 1 WHERE username = ?", (email,))
        conn.commit()
        conn.close()
        return "Account activated! You can now log in.", 200
    except:
        return "Expired/Invalid link", 400

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (data.get('username'),)).fetchone()
    conn.close()
    if user and user['is_active'] == 1:
        try:
            if ph.verify(user['password_hash'], data.get('password')):
                token = serializer.dumps(data.get('username'), salt='session')
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


WORKER_API_URL = "http://127.0.0.1:5001/generate"

def run_local_diffusion_workflow(image_bytes):
    """
    Ships raw user image bytes directly over local loopback to our continuous
    diffusers worker engine on port 5001 and returns the finished line art bytes.
    """
    try:
        # Wrap the raw image into standard multipart form data
        files = {'image': ('input.png', image_bytes, 'image/png')}

        # Dispatch to our persistent background worker daemon
        print("Routing image payload to hot local VRAM engine...")
        response = requests.post(WORKER_API_URL, files=files, timeout=90)

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


def run_diffusion_workflow(image_bytes):
    if APP_ENV == 'production':
        return run_remote_diffusion_workflow(image_bytes)
    return run_local_diffusion_workflow(image_bytes)

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
            result_bytes = run_diffusion_workflow(input_data)
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
