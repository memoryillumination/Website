import os
import io
import cv2
import json
import modal
import sqlite3
import numpy as np
from PIL import Image
from rembg import remove
from dotenv import load_dotenv
from argon2 import PasswordHasher
from flask_mail import Mail, Message
from PIL import Image, ImageOps, ImageEnhance
from itsdangerous import URLSafeTimedSerializer
from flask import Flask, request, jsonify, send_file
from argon2.exceptions import VerifyMismatchError

load_dotenv()

app = Flask(__name__)

# --- CONFIGURATION ---
app.config['MAIL_SERVER'] = 'smtp.gmail.com' # Or smtp.sendgrid.net
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')
app.config['MAIL_USERNAME'] = os.environ.get('EMAIL_USER')
app.config['MAIL_PASSWORD'] = os.environ.get('EMAIL_PASS')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('EMAIL_USER')

mail = Mail(app)
serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])

ph = PasswordHasher()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "users.db")

UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

remote_model = modal.Cls.from_name("coloring-book-flux", "ColoringModel")

def process_image_bundle(input_bytes, settings):
    try:
        # 1. Initialize variables to None so they exist even if the 'if' block is skipped
        normalized_np = None
        coloring_page_bytes = None

        if settings['featureA']:
            input_image = Image.open(io.BytesIO(input_bytes)).convert("RGB")
            target_size = 1024
            width, height = input_image.size
            scale = target_size / max(width, height)
            new_size = (int(width * scale), int(height * scale))
            input_image = input_image.resize(new_size, Image.LANCZOS)

            gray_np_uint8 = np.array(input_image.convert("L"))

            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
            gray_np_uint8 = clahe.apply(gray_np_uint8)
            gray_np = gray_np_uint8.astype(np.float32)
            blur_np = cv2.GaussianBlur(gray_np, (15, 15), 0)
            
            normalized_np = 255 * (gray_np / (blur_np + 1.0))
            normalized_np = np.clip(normalized_np, 0, 255).astype(np.uint8)

        if settings['featureB']:
            images_dict = remote_model().process.remote(input_bytes)
            coloring_page_bytes = images_dict.get("flux_sketch")

        if normalized_np is not None:
            is_success, buffer = cv2.imencode(".png", normalized_np)
            if is_success:
                return io.BytesIO(buffer.tobytes())

        if coloring_page_bytes:
            return io.BytesIO(coloring_page_bytes)

        return None

    except Exception as e:
        print(f"Error: {e}")
        return None

@app.route('/upload-endpoint', methods=['POST'])
def upload_file():
    if 'myFile' not in request.files:
        return jsonify({"error": "No file part"}), 400
    
    file = request.files['myFile']
    
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    settings_str = request.form.get('settings') 
    if settings_str:
        settings = json.loads(settings_str)

    if file:
        input_data = file.read()
        output_stream = process_image_bundle(input_data, settings)

        if output_stream:
            output_stream.seek(0)
            return send_file(
                output_stream, 
                mimetype='image/png', 
                as_attachment=True, 
                download_name="illuminated.png"
            )
        else:
            return jsonify({"error": "Failed to process image"}), 500

# --- DATABASE LOGIC (Unchanged) ---
def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row 
    return conn

def init_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_active INTEGER DEFAULT 0  -- 0 = inactive, 1 = active
            )
        ''')
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Database error: {e}")

init_db()

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400

    try:
        hashed_password = ph.hash(password)
        conn = get_db_connection()
        cursor = conn.cursor()

        # Insert user as inactive
        cursor.execute("INSERT INTO users (username, password_hash, is_active) VALUES (?, ?, 0)", 
                       (username, hashed_password))
        conn.commit()
        conn.close()

        # Generate verification token
        token = serializer.dumps(username, salt='email-confirm')
        confirm_url = f"http://memory-illumination.com/confirm/{token}"

        # Send Email
        msg = Message("Confirm Your Account", recipients=[username])
        msg.body = f"Click the link to activate your account: {confirm_url}"
        mail.send(msg)

        return jsonify({"message": f"Check your email to activate your account"}), 201

    except sqlite3.IntegrityError:
        return jsonify({"error": "Username already taken"}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/confirm/<token>')
def confirm_email(token):
    try:
        email = serializer.loads(token, salt='email-confirm', max_age=3600)
    except:
        return "The confirmation link is invalid or has expired.", 400

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_active = 1 WHERE username = ?", (email,))
    conn.commit()
    conn.close()
    
    return "Account activated! You can now log in.", 200

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT password_hash, is_active FROM users WHERE username = ?", (username,))
    user_row = cursor.fetchone()
    conn.close()

    if user_row:
        if user_row['is_active'] == 0:
            return jsonify({"error": "Please verify your email before logging in."}), 403
        stored_hash = user_row['password_hash']
        try:
            if ph.verify(stored_hash, password):
                return jsonify({"message": "Login successful"}), 200
        except VerifyMismatchError:
            pass

    return jsonify({"error": "Invalid credentials"}), 401

if __name__ == '__main__':
    app.run(debug=True, port=5000)