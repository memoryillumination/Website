"""Job management routes for async image processing."""
import io
import time
from flask import request, jsonify, send_file

from utils import http


def register_routes(app, get_db_connection, job_owner, load_job,
                   fetch_job_result, estimate_seconds, job_phase, job_max_age_seconds,
                   watermark_available, apply_watermark):
    """Register the job management routes."""

    @app.route('/jobs/<job_id>', methods=['GET'])
    def job_status(job_id):
        ip = http.client_ip()
        owner = job_owner(request.cookies.get('session', ''), ip)

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

            if row['status'] != 'pending':
                if row['status'] == 'error':
                    payload['error'] = row['error']
                return jsonify(payload), 200

            status, result = fetch_job_result(row['call_id'])

            if status == 'transient':
                if elapsed <= job_max_age_seconds:
                    status = 'pending'
                else:
                    status = 'error'
                    result = "Lost contact with the image worker. Please try again."

            if status == 'pending':
                payload['phase'] = job_phase(row, elapsed)
                return jsonify(payload), 200

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
            response = jsonify({"error": "Couldn't reach the image worker. Please try again in a moment."})
            response.headers['Retry-After'] = '5'
            return response, 503
        if status == 'error':
            return jsonify({"error": result}), 500

        result_bytes = result
        if watermark_available and row['is_free_tier']:
            try:
                result_bytes = apply_watermark(result_bytes)
            except Exception as e:
                print(f"⚠️  Watermarking failed, returning unwatermarked image: {e}")

        return send_file(io.BytesIO(result_bytes), mimetype='image/png')
