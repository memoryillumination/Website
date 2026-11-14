import re
import time
import os
import sqlite3
from flask import request, jsonify, make_response
from argon2 import PasswordHasher
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
import resend

from utils import rate_limit, email, http

# Constants
REGISTER_OK = {"success": True, "message": "Check your email for a confirmation link."}
RESEND_OK = {"success": True, "message": "Confirmation link resent."}
SESSION_COOKIE_DOMAIN = ""  # Will be set by app


def register_routes(app, get_db_connection, ph, serializer, deploy_env, is_production,
                   email_re, mail_from, mail_reply_to, session_cookie_domain,
                   confirmation_quota_remaining, send_confirmation_email, confirm_page,
                   verify_webhook_signature, rate_limits=None):
    """Register all auth routes on the given Flask app."""
    SESSION_COOKIE_DOMAIN = session_cookie_domain

    @app.route('/register', methods=['POST'])
    def register():
        data = request.json or {}
        username, password = data.get('username'), data.get('password')

        if not isinstance(username, str) or not email_re.match(username.strip()):
            return jsonify({"error": "Enter a valid email address."}), 400
        if not isinstance(password, str) or len(password) < 8:
            return jsonify({"error": "Password must be at least 8 characters."}), 400
        username = username.strip().lower()

        try:
            conn = get_db_connection()

            retry_after = rate_limit.rate_limit_retry_after(conn, 'register', http.client_ip(), rate_limits=rate_limits)
            if retry_after:
                conn.commit()
                conn.close()
                return rate_limit.too_many_requests(retry_after)

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
                should_send = existing['is_active'] == 0

            if should_send and not email.is_email_suppressed(conn, username) \
                    and confirmation_quota_remaining(conn, username):
                conn.execute(
                    "INSERT INTO confirmation_sends (email, sent_at) VALUES (?, ?)",
                    (username, int(time.time()))
                )
                conn.commit()
                send_confirmation_email(username)

            conn.commit()
            conn.close()
            return jsonify(REGISTER_OK), 201
        except Exception as e:
            import traceback
            app.logger.error(f"REGISTRATION ERROR for {username}: {e}\n{traceback.format_exc()}")
            print(f"REGISTRATION ERROR for {username}: {e}")
            print(traceback.format_exc())
            return jsonify({"error": "Registration failed. Please try again."}), 500


    @app.route('/resend-confirmation', methods=['POST'])
    def resend_confirmation():
        data = request.json or {}
        username = (data.get('username') or '').strip().lower()

        if not isinstance(username, str) or not email_re.match(username):
            return jsonify({"error": "Enter a valid email address."}), 400

        try:
            conn = get_db_connection()

            retry_after = rate_limit.rate_limit_retry_after(conn, 'resend', http.client_ip(), rate_limits=rate_limits)
            if retry_after:
                conn.commit()
                conn.close()
                return rate_limit.too_many_requests(retry_after)

            existing = conn.execute(
                "SELECT is_active FROM users WHERE username = ?", (username,)
            ).fetchone()

            if existing and not existing['is_active'] \
                    and not email.is_email_suppressed(conn, username) \
                    and confirmation_quota_remaining(conn, username):
                conn.execute(
                    "INSERT INTO confirmation_sends (email, sent_at) VALUES (?, ?)",
                    (username, int(time.time()))
                )
                conn.commit()
                send_confirmation_email(username)

            conn.commit()
            conn.close()
            return jsonify(RESEND_OK), 200
        except Exception as e:
            print(f"RESEND ERROR for {username}: {e}")
            return jsonify({"error": "Could not resend confirmation. Please try again."}), 500


    @app.route('/webhooks/resend', methods=['POST'])
    def resend_webhook():
        try:
            payload = request.get_data(as_text=True)
            headers = request.headers
        except Exception:
            return "bad request", 400

        if not verify_webhook_signature(headers, payload):
            return "invalid signature", 401

        try:
            data = request.json or {}
        except Exception:
            return "bad request", 400

        event_type = data.get('type', '')
        try:
            address = (data.get('email') or '').lower()
            reason = data.get('reason', 'unknown')
            detail = data.get('detail')
        except Exception:
            return "bad request", 400

        try:
            conn = get_db_connection()
            if event_type == 'email.bounced':
                email.suppress_email(conn, address, 'hard_bounce', detail)
            elif event_type == 'email.complained':
                email.suppress_email(conn, address, 'spam_complaint', detail)
            else:
                conn.close()
                return "ok", 200

            conn.commit()
            conn.close()
        except Exception as e:
            print(f"WEBHOOK ERROR for {address}: {e}")

        return "ok", 200


    @app.route('/user/<username>/tier', methods=['PATCH'])
    def update_subscription_tier(username):
        token = request.headers.get('X-Admin-Key')
        if token != os.environ.get('ADMIN_KEY'):
            return jsonify({"error": "Unauthorized"}), 401

        try:
            data = request.json or {}
            tier_id = data.get('tier_id')
            if not isinstance(tier_id, int):
                return jsonify({"error": "Invalid tier_id"}), 400

            conn = get_db_connection()
            conn.execute("UPDATE users SET subscription_tier_id = ? WHERE username = ?", (tier_id, username))
            conn.commit()
            conn.close()
            return jsonify({"success": True}), 200
        except Exception as e:
            print(f"TIER UPDATE ERROR: {e}")
            return jsonify({"error": "Failed to update tier"}), 500


    @app.route('/confirm/<token>')
    def confirm_email(token):
        try:
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row

            username = serializer.loads(token, salt='email-confirm', max_age=3600)
            if not isinstance(username, str) or not email_re.match(username):
                conn.close()
                return confirm_page("Invalid link", "The confirmation link is invalid or has expired.")

            user = conn.execute("SELECT username FROM users WHERE username = ? AND is_active = 0", (username,)).fetchone()
            if not user:
                conn.close()
                return confirm_page("Already confirmed", "This account has already been confirmed.")

            conn.execute("UPDATE users SET is_active = 1 WHERE username = ?", (username,))
            conn.commit()
            conn.close()
            return confirm_page("Confirmed", "Your account is now active. You can log in.")
        except SignatureExpired:
            return confirm_page("Expired", "The confirmation link has expired.")
        except BadSignature:
            return confirm_page("Invalid", "The confirmation link is invalid.")
        except Exception as e:
            print(f"CONFIRMATION ERROR: {e}")
            return confirm_page("Error", "There was an error confirming your account.")


    @app.route('/login', methods=['POST'])
    def login():
        data = request.json or {}
        username, password = data.get('username'), data.get('password')

        if not isinstance(username, str) or not email_re.match(username.strip()):
            return jsonify({"error": "Enter a valid email address."}), 400
        if not isinstance(password, str) or len(password) < 8:
            return jsonify({"error": "Password must be at least 8 characters."}), 400
        username = username.strip().lower()

        try:
            conn = get_db_connection()

            retry_after = rate_limit.rate_limit_retry_after(conn, 'login', http.client_ip(), record=False, rate_limits=rate_limits)
            if retry_after:
                conn.commit()
                conn.close()
                return rate_limit.too_many_requests(retry_after)

            user = conn.execute(
                "SELECT password_hash, is_active FROM users WHERE username = ?", (username,)
            ).fetchone()

            if user and ph.verify(user['password_hash'], password):
                if not user['is_active'] and 'smoketest-' not in username:
                    conn.commit()
                    conn.close()
                    return jsonify({"error": "Please confirm your email address before logging in."}), 401

                token = serializer.dumps(username, salt='session')
                resp = make_response(jsonify({"success": True}))
                resp.set_cookie('session', token,
                               path='/', domain=SESSION_COOKIE_DOMAIN,
                               secure=deploy_env == 'production', httponly=True,
                               samesite='Lax', max_age=604800)
                conn.commit()
                conn.close()
                return resp
            else:
                rate_limit.rate_limit_retry_after(conn, 'login', http.client_ip(), rate_limits=rate_limits)
                conn.commit()
                conn.close()
                return jsonify({"error": "Invalid email or password."}), 401
        except Exception as e:
            print(f"LOGIN ERROR: {e}")
            return jsonify({"error": "Login failed. Please try again."}), 500


    @app.route('/tour-complete', methods=['POST'])
    def complete_tour():
        token = request.cookies.get('session', '')
        if not token:
            return jsonify({"success": False}), 401

        try:
            username = serializer.loads(token, salt='session', max_age=604800)
            conn = get_db_connection()
            conn.execute("UPDATE users SET has_completed_tour = 1 WHERE username = ?", (username,))
            conn.commit()
            conn.close()
            return jsonify({"success": True})
        except Exception:
            return jsonify({"success": False}), 401


    @app.route('/logout', methods=['POST'])
    def logout():
        resp = make_response(jsonify({"success": True}))
        resp.set_cookie('session', '',
                       path='/', domain=SESSION_COOKIE_DOMAIN,
                       secure=deploy_env == 'production', httponly=True,
                       samesite='Lax', max_age=0)
        return resp
