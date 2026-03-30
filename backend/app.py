import os, io, cv2, json, modal, sqlite3
import numpy as np
from PIL import Image
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
    return conn

# --- DATABASE INITIALIZATION ---
def init_db():
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_active INTEGER DEFAULT 0
        )
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
        conn.execute("INSERT INTO users (username, password_hash, is_active) VALUES (?, ?, 1)", (username, hash_pw))
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
                return jsonify({"message": "OK"}), 200
        except VerifyMismatchError: pass
    return jsonify({"error": "Unauthorized"}), 401

@app.route('/upload-endpoint', methods=['POST'])
def upload_file():
    file = request.files['myFile']
    settings = json.loads(request.form.get('settings', '{}'))
    input_data = file.read()

    if settings.get('featureB'):
        res = remote_model().process.remote(input_data)
        return send_file(io.BytesIO(res["flux_sketch"]), mimetype='image/png')
    else:
        img = cv2.imdecode(np.frombuffer(input_data, np.uint8), cv2.IMREAD_GRAYSCALE)
        inv = 255 - img
        blur = cv2.GaussianBlur(inv, (21, 21), 0)
        sketch = cv2.divide(img, 255 - blur, scale=256)
        _, buffer = cv2.imencode(".png", sketch)
        return send_file(io.BytesIO(buffer), mimetype='image/png')

if __name__ == '__main__':
    app.run(port=5000)
