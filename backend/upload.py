"""Upload endpoint and related image processing functions."""
import io
import json
import time
import uuid
import cv2
import numpy as np
from flask import request, jsonify, send_file

from utils import rate_limit, http


def register_routes(app, get_db_connection, serializer, deploy_env, is_production,
                   cookie_domain, normalize_image, unsupported_image_error,
                   image_too_large_error, is_smoke_test_request,
                   predict_cold_start, spawn_remote_diffusion, job_owner,
                   estimate_seconds, run_diffusion_workflow,
                   apply_watermark, watermark_available, use_modal,
                   cold_estimate_seconds, warm_estimate_seconds, rate_limits=None):
    """Register the upload endpoint."""

    @app.route('/upload-endpoint', methods=['POST'])
    def upload_file():
        t_start = time.perf_counter()

        file     = request.files['myFile']
        settings = json.loads(request.form.get('settings', '{}'))
        token    = request.cookies.get('session', '')

        # Single DB connection for the entire request
        conn = get_db_connection()
        try:
            # Per-IP rate limits. Bypass for the smoke-test user so automated tests
            # can upload repeatedly without hitting the per-IP caps.
            ip = http.client_ip()
            retry_after = rate_limit.rate_limit_retry_after(conn, 'upload', ip, rate_limits=rate_limits)
            if not retry_after and settings.get('featureB') and not is_smoke_test_request():
                retry_after = rate_limit.rate_limit_retry_after(conn, 'upload_gpu', ip, rate_limits=rate_limits)
            conn.commit()
            if retry_after:
                return rate_limit.too_many_requests(retry_after)

            input_data = file.read()

            t_read = time.perf_counter()

            try:
                input_data = normalize_image(input_data)
            except unsupported_image_error as e:
                return jsonify({"error": str(e)}), 400
            except image_too_large_error as e:
                return jsonify({"error": str(e)}), 413

            t_normalize = time.perf_counter()

            # Verify signed token and check subscription status
            is_free_tier = False
            try:
                username = serializer.loads(token, salt='session', max_age=604800)
                user = conn.execute(
                    "SELECT subscription_tier_id FROM users WHERE username = ?", (username,)
                ).fetchone()
                if user and user['subscription_tier_id'] == 1:
                    is_free_tier = True
            except Exception:
                is_free_tier = True

            t_auth = time.perf_counter()

            # Execution Routing Split
            if settings.get('featureB') and use_modal:
                try:
                    cold_start = predict_cold_start(conn)
                    call_id = spawn_remote_diffusion(input_data)
                except Exception as e:
                    print(f"❌ Modal dispatch failure: {e}")
                    return jsonify({"error": "Something went wrong processing that image. Please try again."}), 500

                job_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO jobs (id, call_id, owner, is_free_tier, cold_start, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                    (job_id, call_id, job_owner(token, ip), int(is_free_tier), int(cold_start), int(time.time()))
                )
                conn.commit()

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
                if settings.get('featureB'):
                    inference_params = {
                        'prompt': settings.get('prompt'),
                        'num_inference_steps': settings.get('num_inference_steps'),
                        'guidance_scale': settings.get('guidance_scale'),
                    }
                    result_bytes = run_diffusion_workflow(input_data, inference_params)
                else:
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

            if watermark_available and is_free_tier:
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
        finally:
            conn.close()
