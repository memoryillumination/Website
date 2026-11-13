"""
Smoke test for MemoryIllumination upload endpoint.

Logs in with the smoke-test user, uploads a test image, and verifies the
response is a valid PNG.  Exits 0 on success, 1 on failure.

Usage:
    python tests/smoke_test.py
    # Must be run from the repo root so it can find the test image.
"""
import os, sys, json, hashlib, io, time
import requests
from PIL import Image

BASE_URL = os.environ.get("SMOKE_TEST_API_URL", "http://127.0.0.1:5000")
USERNAME = os.environ.get("SMOKE_TEST_USER", "smoke-test@example.com")
PASSWORD = os.environ.get("SMOKE_TEST_PASSWORD", "smoke-test-pass")
TEST_IMAGE = os.path.join(os.path.dirname(__file__), "test_image.png")


def login(session, username, password):
    """Log in and return a session with a valid cookie."""
    resp = session.post(
        f"{BASE_URL}/login",
        json={"username": username, "password": password},
        timeout=10,
    )
    if resp.status_code != 200:
        print(f"❌ Login failed: {resp.status_code} {resp.text}")
        return False
    data = resp.json()
    if "error" in data:
        print(f"❌ Login error: {data['error']}")
        return False
    print(f"✅ Logged in as {username}")
    return True


def upload_image(session, use_gpu=False):
    """Upload a test image and return the response (bytes or None)."""
    if not os.path.exists(TEST_IMAGE):
        print(f"❌ Test image not found at {TEST_IMAGE}")
        return None

    settings = {"featureA": False, "featureB": use_gpu}

    with open(TEST_IMAGE, "rb") as f:
        resp = session.post(
            f"{BASE_URL}/upload-endpoint",
            files={"myFile": ("test.png", f, "image/png")},
            data={"settings": json.dumps(settings)},
            timeout=30,
        )

    # GPU path returns 202 with job ID
    if resp.status_code == 202:
        job_info = resp.json()
        job_id = job_info["job_id"]
        print(f"   Job created: {job_id} (cold_start={job_info.get('cold_start')})")

        # Poll for completion
        for i in range(60):
            time.sleep(2)
            resp = session.get(f"{BASE_URL}/jobs/{job_id}", timeout=10)
            if resp.status_code == 200:
                job = resp.json()
                if job["status"] in ("done", "completed"):
                    # Get result
                    resp = session.get(f"{BASE_URL}/jobs/{job_id}/result", timeout=30)
                    if resp.status_code != 200 or "image/png" not in resp.headers.get("Content-Type", ""):
                        print(f"❌ Job result error: {resp.status_code}")
                        return None
                    data = resp.content
                    break
                elif job["status"] in ("failed", "error"):
                    print(f"❌ Job failed: {job.get('error')}")
                    return None
            else:
                print(f"❌ Job status error: {resp.status_code}")
                return None
        else:
            print("❌ Job timed out")
            return None

    # CPU path returns 200 with image directly
    elif resp.status_code != 200:
        print(f"❌ Upload failed: {resp.status_code} {resp.text}")
        return None
    else:
        data = resp.content

    # Verify content type is PNG
    content_type = resp.headers.get("Content-Type", "")
    if "image/png" not in content_type:
        print(f"❌ Expected PNG but got Content-Type: {content_type}")
        return None

    # Verify response is valid PNG
    if len(data) < 8:
        print(f"❌ Response too short ({len(data)} bytes)")
        return None
    if data[:4] != b"\x89PNG":
        print(f"❌ Not a valid PNG (magic bytes: {data[:4]!r})")
        return None

    file_hash = hashlib.sha256(data).hexdigest()[:16]
    print(f"✅ Upload succeeded — {len(data)} bytes, SHA-256: {file_hash}")
    return data


def main():
    print(f"🧪 MemoryIllumination smoke test")
    print(f"   API: {BASE_URL}")
    print(f"   User: {USERNAME}")
    print()

    session = requests.Session()

    # Step 1: Login
    print("Step 1: Login")
    if not login(session, USERNAME, PASSWORD):
        sys.exit(1)
    print()

    # Step 2: Upload (CPU path) - Free tier (expects watermark)
    print("Step 2: Upload test image (CPU path) - Free tier")
    result = upload_image(session, use_gpu=False)
    if result is None:
        sys.exit(1)
    
    # Verify watermark is present by checking pixel differences
    original = Image.open(io.BytesIO(open(TEST_IMAGE, 'rb').read())).convert("RGBA")
    watermarked = Image.open(io.BytesIO(result)).convert("RGBA")
    
    # Check that images are different (watermark added)
    if original.size != watermarked.size:
        print("✅ Watermark detected (free tier) - different sizes")
    else:
        # Compare pixel data by converting to bytes
        orig_data = original.tobytes()
        wm_data = watermarked.tobytes()
        if orig_data == wm_data:
            print("❌ Watermark not detected - images are identical")
            sys.exit(1)
        print("✅ Watermark detected (free tier)")
    print()
    
    # Step 3: Upload (GPU path) - Free tier (expects watermark)
    print("Step 3: Upload test image (GPU path) - Free tier")
    result = upload_image(session, use_gpu=True)
    if result is None:
        sys.exit(1)
    
    # Verify watermark is present
    watermarked = Image.open(io.BytesIO(result)).convert("RGBA")
    if original.size != watermarked.size:
        print("✅ Watermark detected (GPU path, free tier) - different sizes")
    else:
        orig_data = original.tobytes()
        wm_data = watermarked.tobytes()
        if orig_data == wm_data:
            print("❌ Watermark not detected (GPU path) - images are identical")
            sys.exit(1)
        print("✅ Watermark detected (GPU path, free tier)")

    print("🎉 All smoke tests passed")
    sys.exit(0)


if __name__ == "__main__":
    main()
