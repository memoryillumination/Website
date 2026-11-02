"""
Smoke test for MemoryIllumination upload endpoint.

Logs in with the smoke-test user, uploads a test image, and verifies the
response is a valid PNG.  Exits 0 on success, 1 on failure.

Usage:
    python tests/smoke_test.py
    # Must be run from the repo root so it can find the test image.
"""
import os, sys, json, hashlib, io
import requests

BASE_URL = os.environ.get("SMOKE_TEST_API_URL", "http://127.0.0.1:5000")
USERNAME = os.environ.get("SMOKE_TEST_USER", "smoke-test")
PASSWORD = os.environ.get("SMOKE_TEST_PASSWORD", "smoke-test-pass")
TEST_IMAGE = os.path.join(os.path.dirname(__file__), "tests", "test_image.png")


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


def upload_image(session):
    """Upload a test image and return the response (bytes or None)."""
    if not os.path.exists(TEST_IMAGE):
        print(f"❌ Test image not found at {TEST_IMAGE}")
        return None

    with open(TEST_IMAGE, "rb") as f:
        file_data = f.read()

    resp = session.post(
        f"{BASE_URL}/upload-endpoint",
        data={
            "myFile": ("test.png", file_data, "image/png"),
            "settings": json.dumps({"featureA": False, "featureB": False}),
        },
        timeout=30,
    )

    if resp.status_code != 200:
        print(f"❌ Upload failed: {resp.status_code} {resp.text}")
        return None

    # Verify content type is PNG
    content_type = resp.headers.get("Content-Type", "")
    if "image/png" not in content_type:
        print(f"❌ Expected PNG but got Content-Type: {content_type}")
        return None

    # Verify response is valid PNG
    data = resp.content
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

    # Step 2: Upload
    print("Step 2: Upload test image")
    result = upload_image(session)
    if result is None:
        sys.exit(1)
    print()

    print("🎉 All smoke tests passed")
    sys.exit(0)


if __name__ == "__main__":
    main()
