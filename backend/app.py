import os, io, cv2, json, modal, sqlite3
import numpy as np
from PIL import Image, ImageOps
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from flask_mail import Mail, Message
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import URLSafeTimedSerializer
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
CORS(app)

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
remote_model = modal.Cls.from_name("coloring-book-flux", "ColoringModel")

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
                                 REFERENCES subscription_tiers(id)
        )
    ''')

    # Migrate existing databases that predate the tier column
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if 'subscription_tier_id' not in existing_cols:
        conn.execute('''
            ALTER TABLE users
            ADD COLUMN subscription_tier_id INTEGER DEFAULT 1
                       REFERENCES subscription_tiers(id)
        ''')

    conn.commit()
    conn.close()

# Create the table immediately on startup
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

        #token = serializer.dumps(username, salt='email-confirm')
        #confirm_url = f"https://api.memory-illumination.com/confirm/{token}"
        #msg = Message("Activate Account", recipients=[username], body=f"Link: {confirm_url}")
        #mail.send(msg)
        #return jsonify({"message": "Check email"}), 201

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
                return jsonify({"token": token}), 200
        except VerifyMismatchError: pass
    return jsonify({"error": "Unauthorized"}), 401

WATERMARK_PATH = os.path.join(os.path.dirname(__file__), "MI_Watermark.png")

def apply_watermark(image_bytes):
    output = Image.open(io.BytesIO(image_bytes)).convert("RGBA")

    logo = Image.open(WATERMARK_PATH).convert("RGBA")
    logo = logo.resize(output.size, Image.LANCZOS)

    # If PNG has no transparency, derive alpha from inverted luminance so the
    # white background disappears and dark lines stay visible
    logo_alpha = logo.split()[3]
    if logo_alpha.getextrema() == (255, 255):
        alpha = ImageOps.invert(logo.convert("L"))
    else:
        alpha = logo_alpha

    alpha = alpha.point(lambda x: int(x * 0.40))

    watermark = Image.new("RGBA", logo.size, (0, 0, 0, 0))
    watermark.putalpha(alpha)

    output.paste(watermark, (0, 0), watermark)

    buf = io.BytesIO()
    output.save(buf, format="PNG")
    return buf.getvalue()


@app.route('/upload-endpoint', methods=['POST'])
def upload_file():
    file     = request.files['myFile']
    settings = json.loads(request.form.get('settings', '{}'))
    token    = request.form.get('token', '')
    input_data = file.read()

    # Verify the signed session token and look up the user's tier
    is_free_tier = False
    try:
        username = serializer.loads(token, salt='session', max_age=86400)
        conn = get_db_connection()
        user = conn.execute(
            "SELECT subscription_tier_id FROM users WHERE username = ?", (username,)
        ).fetchone()
        conn.close()
        if user and user['subscription_tier_id'] == 1:
            is_free_tier = True
    except Exception:
        # Unsigned, tampered, or expired token — treat as free tier
        is_free_tier = True

    if settings.get('featureB'):
        res = remote_model().process.remote(input_data)
        result_bytes = res["flux_sketch"]
    else:
        img = cv2.imdecode(np.frombuffer(input_data, np.uint8), cv2.IMREAD_GRAYSCALE)
        inv = 255 - img
        blur = cv2.GaussianBlur(inv, (21, 21), 0)
        sketch = cv2.divide(img, 255 - blur, scale=256)
        _, buffer = cv2.imencode(".png", sketch)
        result_bytes = bytes(buffer)

    if is_free_tier:
        result_bytes = apply_watermark(result_bytes)

    return send_file(io.BytesIO(result_bytes), mimetype='image/png')

if __name__ == '__main__':
    app.run(port=5000)
