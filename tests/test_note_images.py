"""
Regression tests for note image upload and retrieval.

Covers:
- Upload then immediate read (same session)
- Upload then read on "page refresh" (new request, same UUID)
- Cross-user isolation (User B cannot access User A's image)
- Non-existent image returns 404
- Unauthenticated access returns 401
"""

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from loguru import logger

from beaverhabits.app.db import User, engine
from beaverhabits.main import app

PASSWORD = "TestPassword123!"


# A minimal valid 1x1 PNG image (67 bytes)
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02"
    b"\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx"
    b"\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00"
    b"\x00IEND\xaeB`\x82"
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(name="client", scope="module")
async def client_fixture():
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client
    await engine.dispose()


def _register_and_login(client: TestClient, label: str) -> dict:
    """Helper: register a new user and return auth headers."""
    email = f"{label}_{datetime.now().timestamp()}@test.com"

    # Register
    resp = client.post(
        "/auth/register",
        json={"email": email, "password": PASSWORD},
    )
    assert resp.status_code == 201, f"Register failed: {resp.text}"

    # Login
    resp = client.post(
        "/auth/login",
        data={
            "grant_type": "password",
            "username": email,
            "password": PASSWORD,
        },
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    token = resp.json()["access_token"]

    return {
        "Authorization": f"Bearer {token}",
        "accept": "application/json",
    }


@pytest.fixture
async def user_a_headers(client: TestClient):
    """User A: registers and logs in."""
    return _register_and_login(client, "img_user_a")


@pytest.fixture
async def user_b_headers(client: TestClient):
    """User B: registers and logs in (separate account)."""
    return _register_and_login(client, "img_user_b")


# ============================================================================
# Tests
# ============================================================================


class TestUploadAndRead:
    """Upload an image, then read it back in the same 'session'."""

    async def test_upload_returns_url_with_uuid(self, client: TestClient, user_a_headers):
        """POST /assets should return a JSON with id (UUID) and url."""
        resp = client.post(
            "/assets",
            files={"file": ("test.png", TINY_PNG, "image/png")},
            headers=user_a_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert "url" in data
        # id should be a valid UUID
        uuid.UUID(data["id"])
        # url should contain the UUID
        assert data["id"] in data["url"]

    async def test_upload_then_read_returns_image(self, client: TestClient, user_a_headers):
        """After upload, GET /assets/{id} should return the image bytes."""
        # Upload
        upload_resp = client.post(
            "/assets",
            files={"file": ("test.png", TINY_PNG, "image/png")},
            headers=user_a_headers,
        )
        assert upload_resp.status_code == 200
        image_id = upload_resp.json()["id"]

        # Read (simulates browser fetching <img src="/assets/{uuid}"/>)
        get_resp = client.get(
            f"/assets/{image_id}",
            headers=user_a_headers,
        )
        assert get_resp.status_code == 200
        assert get_resp.headers["content-type"] == "image/png"
        assert get_resp.content == TINY_PNG


class TestReopenAfterRefresh:
    """Simulate page refresh: upload, then read via a completely new GET request."""

    async def test_image_survives_page_refresh(self, client: TestClient, user_a_headers):
        """
        Simulate: user uploads image, refreshes the page, image should still load.
        The second GET is a separate request — same auth, same UUID.
        """
        # Upload
        upload_resp = client.post(
            "/assets",
            files={"file": ("refresh.png", TINY_PNG, "image/png")},
            headers=user_a_headers,
        )
        assert upload_resp.status_code == 200
        image_id = upload_resp.json()["id"]

        # Simulate "page refresh" — a brand new GET request
        get_resp_1 = client.get(f"/assets/{image_id}", headers=user_a_headers)
        assert get_resp_1.status_code == 200, "First read after upload should succeed"
        assert get_resp_1.content == TINY_PNG

        # Simulate "reopen the note later" — yet another GET request
        get_resp_2 = client.get(f"/assets/{image_id}", headers=user_a_headers)
        assert get_resp_2.status_code == 200, "Second read (reopen) should also succeed"
        assert get_resp_2.content == TINY_PNG

    async def test_multiple_uploads_all_readable(self, client: TestClient, user_a_headers):
        """Upload multiple images, all should remain readable."""
        image_ids = []
        for i in range(3):
            resp = client.post(
                "/assets",
                files={"file": (f"img{i}.png", TINY_PNG, "image/png")},
                headers=user_a_headers,
            )
            assert resp.status_code == 200
            image_ids.append(resp.json()["id"])

        # All should be readable
        for image_id in image_ids:
            resp = client.get(f"/assets/{image_id}", headers=user_a_headers)
            assert resp.status_code == 200, f"Image {image_id} should be readable"


class TestCrossUserIsolation:
    """User B must not be able to access User A's images."""

    async def test_other_user_gets_404(self, client: TestClient, user_a_headers, user_b_headers):
        """User A uploads, User B tries to read → 404 (not 403, to avoid info leakage)."""
        # User A uploads
        upload_resp = client.post(
            "/assets",
            files={"file": ("private.png", TINY_PNG, "image/png")},
            headers=user_a_headers,
        )
        assert upload_resp.status_code == 200
        image_id = upload_resp.json()["id"]

        # User A can read it
        a_resp = client.get(f"/assets/{image_id}", headers=user_a_headers)
        assert a_resp.status_code == 200

        # User B cannot read it — should get 404
        b_resp = client.get(f"/assets/{image_id}", headers=user_b_headers)
        assert b_resp.status_code == 404, "User B should get 404 for User A's image"

    async def test_user_b_cannot_guess_uuid(self, client: TestClient, user_a_headers, user_b_headers):
        """Even knowing the UUID, User B cannot access User A's image."""
        # User A uploads
        upload_resp = client.post(
            "/assets",
            files={"file": ("secret.png", TINY_PNG, "image/png")},
            headers=user_a_headers,
        )
        assert upload_resp.status_code == 200
        image_id = upload_resp.json()["id"]

        # User B tries with the exact UUID
        b_resp = client.get(f"/assets/{image_id}", headers=user_b_headers)
        assert b_resp.status_code == 404


class TestNonExistentImage:
    """GET for a UUID that doesn't exist should return 404."""

    async def test_random_uuid_returns_404(self, client: TestClient, user_a_headers):
        random_uuid = str(uuid.uuid4())
        resp = client.get(f"/assets/{random_uuid}", headers=user_a_headers)
        assert resp.status_code == 404


class TestUnauthenticatedAccess:
    """Requests without auth should be rejected (redirected to /login by middleware)."""

    async def test_upload_without_auth_returns_401(self, client: TestClient):
        # The AuthMiddleware redirects 401 → /login, so we disable redirect following
        resp = client.post(
            "/assets",
            files={"file": ("test.png", TINY_PNG, "image/png")},
            follow_redirects=False,
        )
        # Either 401 directly or 307 redirect to /login
        assert resp.status_code in (401, 307)

    async def test_read_without_auth_returns_401(self, client: TestClient):
        random_uuid = str(uuid.uuid4())
        resp = client.get(f"/assets/{random_uuid}", follow_redirects=False)
        # Either 401 directly or 307 redirect to /login
        assert resp.status_code in (401, 307)
