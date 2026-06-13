"""End-to-end regression tests for note image upload/serve.

Covers the bug where uploaded note images 404'd on every read (and after a
page refresh / reopen), and verifies user isolation so one user can never
fetch another user's images.

These exercise the *real* asset route handlers (`upload_note_image` /
`get_note_image`) with real bearer authentication. The handlers are mounted on
a fresh, self-contained FastAPI app — the same isolation strategy test_apis.py
uses — so the suite does not depend on NiceGUI's global app state (the NiceGUI
`User` test fixture strips routes from the shared app, which would otherwise
make a `TestClient(main.app)` order-dependent).
"""

import uuid
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beaverhabits.app.db import create_db_and_tables, engine
from beaverhabits.app.schemas import UserCreate, UserRead
from beaverhabits.app.users import auth_backend, fastapi_users
from beaverhabits.routes.routes import get_note_image, upload_note_image

PASSWORD = "TestPassword123!"
# Deliberately includes non-UTF8 bytes to ensure the blob round-trips verbatim.
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03\xff\xfe note-image-payload"


def _build_app() -> FastAPI:
    """A minimal app exposing auth + the real asset routes, isolated from NiceGUI."""

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await create_db_and_tables()
        yield

    test_app = FastAPI(lifespan=lifespan)
    test_app.include_router(
        fastapi_users.get_auth_router(auth_backend), prefix="/auth", tags=["auth"]
    )
    test_app.include_router(
        fastapi_users.get_register_router(UserRead, UserCreate),
        prefix="/auth",
        tags=["auth"],
    )
    # Mount the production handlers so we test the actual save/read chain.
    test_app.add_api_route("/assets", upload_note_image, methods=["POST"])
    test_app.add_api_route("/assets/{image_id}", get_note_image, methods=["GET"])
    return test_app


@pytest.fixture(name="client", scope="module")
async def client_fixture():
    with TestClient(_build_app(), raise_server_exceptions=True) as client:
        yield client

    await engine.dispose()


def _register_and_login(client: TestClient, email: str) -> dict:
    """Register a fresh user and return bearer-auth headers."""
    resp = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 201, resp.text

    resp = client.post(
        "/auth/login",
        data={"grant_type": "password", "username": email, "password": PASSWORD},
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "accept": "application/json",
        },
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}", "accept": "application/json"}


def _upload_image(client: TestClient, headers: dict) -> str:
    """Upload PNG_BYTES and return the asset URL embedded into the note."""
    resp = client.post(
        "/assets",
        files={"file": ("note.png", PNG_BYTES, "image/png")},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["url"]


async def test_upload_then_read_and_reopen(client: TestClient):
    """Upload an image, then read it back — including repeated reads that
    simulate refreshing/reopening the note (the original 404 bug)."""
    headers = _register_and_login(client, f"imguser_{uuid.uuid4().hex}@test.com")

    url = _upload_image(client, headers)
    assert url.startswith("/assets/")

    # Read immediately after upload.
    first = client.get(url, headers=headers)
    assert first.status_code == 200, first.text
    assert first.content == PNG_BYTES

    # Reopen the page / refresh: the same URL must keep resolving.
    for _ in range(3):
        again = client.get(url, headers=headers)
        assert again.status_code == 200
        assert again.content == PNG_BYTES


async def test_cross_user_isolation(client: TestClient):
    """A user must never be able to fetch another user's image."""
    owner = _register_and_login(client, f"owner_{uuid.uuid4().hex}@test.com")
    other = _register_and_login(client, f"other_{uuid.uuid4().hex}@test.com")

    url = _upload_image(client, owner)

    # The owner can read their own image.
    assert client.get(url, headers=owner).status_code == 200

    # A different authenticated user is denied (no resource bleed-through).
    assert client.get(url, headers=other).status_code == 404

    # An unauthenticated request is rejected and never returns the image bytes.
    anon = client.get(url)
    assert anon.status_code == 401
    assert anon.content != PNG_BYTES


async def test_missing_image_returns_404(client: TestClient):
    """A well-formed but unknown asset id returns 404, not a server error."""
    headers = _register_and_login(client, f"missing_{uuid.uuid4().hex}@test.com")

    resp = client.get(f"/assets/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404
