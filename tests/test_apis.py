"""
Test suite for Beaver Habits API based on the official API documentation.
Tests cover: authentication, habit CRUD operations, and habit completions.
"""

from datetime import date, datetime

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from loguru import logger
from nicegui import core

from beaverhabits.app.db import User, engine
from beaverhabits.app.dependencies import current_admin_user
from beaverhabits.app.schemas import UserCreate, UserRead
from beaverhabits.app.users import auth_backend, fastapi_users
from beaverhabits.configs import settings
from beaverhabits.main import app
from beaverhabits.routes.api import init_api_routes

PASSWORD = "TestPassword123!"


# ============================================================================
# Test Fixtures and Helpers
# ============================================================================


def create_app_with_admin_registration() -> FastAPI:
    """Create a fresh FastAPI app with REQUIRE_ADMIN_FOR_REGISTRATION=True."""
    test_app = FastAPI()

    # Auth routes with admin-protected registration
    test_app.include_router(
        fastapi_users.get_auth_router(auth_backend), prefix="/auth", tags=["auth"]
    )
    test_app.include_router(
        fastapi_users.get_register_router(UserRead, UserCreate),
        prefix="/auth",
        tags=["auth"],
        dependencies=[Depends(current_admin_user)],  # Always require admin
    )

    init_api_routes(test_app)
    return test_app


@pytest.fixture(name="client", scope="module")
async def client_fixture():
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client

    await engine.dispose()


@pytest.fixture(name="admin_protected_client", scope="module")
async def admin_protected_client_fixture():
    """Client for testing admin-protected registration."""
    test_app = create_app_with_admin_registration()
    with TestClient(test_app, raise_server_exceptions=True) as client:
        yield client


@pytest.fixture
async def test_user(client: TestClient):
    """Set up the database before tests and tear down after."""
    logger.info("Registering test user...")
    email = f"testuser_{datetime.now().timestamp()}@test.com"
    response = client.post(
        "/auth/register",
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 201
    user_data = response.json()
    user = User(**user_data)

    yield user


@pytest.fixture
async def access_token(test_user: User, client: TestClient):
    """Obtain an access token for the test user (Task 1 from docs)."""
    logger.info(f"Obtaining access token for user: {test_user.email}")
    response = client.post(
        "/auth/login",
        data={
            "grant_type": "password",
            "username": test_user.email,
            "password": PASSWORD,
        },
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "accept": "application/json",
        },
    )
    logger.info(f"Access token response: {response.text}")

    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"

    return data["access_token"]


@pytest.fixture
async def auth_headers(access_token):
    """Get authorization headers with access token."""
    return {
        "Authorization": f"Bearer {access_token}",
        "accept": "application/json",
    }


@pytest.fixture
async def sample_habit(auth_headers, client: TestClient):
    """Create a sample habit for testing."""
    response = client.post(
        "/api/v1/habits",
        json={"name": "Test Habit"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    return response.json()


# ============================================================================
# Task 1 - Authentication Tests
# ============================================================================


async def test_create_user(client: TestClient):
    """Test user registration."""
    email = f"newuser_{datetime.now().timestamp()}@test.com"
    data = {"email": email, "password": PASSWORD}
    response = client.post("/auth/register", json=data)

    assert response.status_code == 201
    assert response.json()["email"] == email
    assert response.json()["is_active"] == True


async def test_open_registration(client: TestClient):
    """Test that /auth/register works without auth when REQUIRE_ADMIN_FOR_REGISTRATION=False (default)."""
    email = f"newuser_{datetime.now().timestamp()}@test.com"
    data = {"email": email, "password": PASSWORD}
    response = client.post("/auth/register", json=data)

    assert response.status_code == 201
    assert response.json()["email"] == email


async def test_register_requires_admin_auth_when_enabled(admin_protected_client: TestClient):
    """Test that /auth/register requires admin auth when REQUIRE_ADMIN_FOR_REGISTRATION=True."""
    email = f"newuser_{datetime.now().timestamp()}@test.com"
    data = {"email": email, "password": PASSWORD}
    response = admin_protected_client.post("/auth/register", json=data)
    assert response.status_code == 401


async def test_obtain_access_token(test_user, client: TestClient):
    """
    Task 1 - Obtain an Access Token
    Test authentication with username and password.
    """
    response = client.post(
        "/auth/login",
        data={
            "grant_type": "password",
            "username": test_user.email,
            "password": PASSWORD,
        },
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert len(data["access_token"]) > 0


async def test_authentication_with_invalid_credentials(client: TestClient):
    """Test authentication fails with wrong credentials."""
    response = client.post(
        "/auth/login",
        data={
            "grant_type": "password",
            "username": "nonexistent@test.com",
            "password": "wrongpassword",
        },
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 400


async def test_api_without_token(client: TestClient):
    """Test that API endpoints require authentication."""
    response = client.get("/api/v1/habits")
    assert response.status_code == 401


# ============================================================================
# Task 2 - List Habits Tests
# ============================================================================


async def test_list_all_habits_empty(auth_headers, client: TestClient):
    """
    Task 2 - List All the Habits
    Test listing habits when no habits exist.
    """
    response = client.get(
        "/api/v1/habits",
        headers=auth_headers,
    )

    # New users might have no habits or return 404
    assert response.status_code in [200, 404]
    if response.status_code == 200:
        assert isinstance(response.json(), list)


async def test_list_all_habits(auth_headers, client: TestClient):
    """
    Task 2 - List All the Habits
    Test listing habits with created habits.
    """
    logger.info(
        f"nicegui loop: {core.loop}, is_running: {core.loop and core.loop.is_running}"
    )

    # Create some test habits
    habit1 = client.post(
        "/api/v1/habits",
        json={"name": "Order pizza"},
        headers=auth_headers,
    )
    logger.info(f"Created habit1: {habit1.json()}")
    habit2 = client.post(
        "/api/v1/habits",
        json={"name": "Running"},
        headers=auth_headers,
    )
    logger.info(f"Created habit2: {habit2.json()}")

    # List all habits
    response = client.get(
        "/api/v1/habits",
        headers=auth_headers,
    )

    assert response.status_code == 200
    habits = response.json()
    assert isinstance(habits, list)
    assert len(habits) >= 2

    # Check structure
    for habit in habits:
        assert "id" in habit
        assert "name" in habit


def test_list_habits_filter_by_status(auth_headers, sample_habit, client: TestClient):
    """Test filtering habits by status (active/archived)."""
    # Test with active status
    response = client.get(
        "/api/v1/habits?status=active",
        headers=auth_headers,
    )
    assert response.status_code == 200

    # Archive the habit
    client.put(
        f"/api/v1/habits/{sample_habit['id']}",
        json={"status": "archive"},
        headers=auth_headers,
    )

    # Test with archived status
    response = client.get(
        "/api/v1/habits?status=archive",
        headers=auth_headers,
    )
    assert response.status_code == 200


# ============================================================================
# Habit CRUD Tests
# ============================================================================


def test_create_habit(auth_headers, client: TestClient):
    """Test creating a new habit."""
    response = client.post(
        "/api/v1/habits",
        json={"name": "Morning Exercise"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert "id" in data
    assert data["name"] == "Morning Exercise"


def test_get_habit_detail(auth_headers, sample_habit, client: TestClient):
    """Test getting detailed information about a specific habit."""
    response = client.get(
        f"/api/v1/habits/{sample_habit['id']}",
        headers=auth_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == sample_habit["id"]
    assert data["name"] == sample_habit["name"]
    assert "star" in data
    assert "records" in data
    assert "status" in data
    assert "tags" in data


def test_update_habit(auth_headers, sample_habit, client: TestClient):
    """Test updating habit properties."""
    response = client.put(
        f"/api/v1/habits/{sample_habit['id']}",
        json={
            "name": "Updated Habit Name",
            "star": True,
            "tags": ["health", "fitness"],
        },
        headers=auth_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Updated Habit Name"
    assert data["star"] == True
    assert "health" in data["tags"]


def test_update_habit_period(auth_headers, sample_habit, client: TestClient):
    """Test updating habit frequency/period."""
    response = client.put(
        f"/api/v1/habits/{sample_habit['id']}",
        json={"period": {"period_type": "W", "period_count": 1, "target_count": 3}},
        headers=auth_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["period"] is not None
    assert data["period"]["period_type"] == "W"
    assert data["period"]["target_count"] == 3


def test_delete_habit(auth_headers, sample_habit, client: TestClient):
    """Test deleting a habit."""
    response = client.delete(
        f"/api/v1/habits/{sample_habit['id']}",
        headers=auth_headers,
    )

    assert response.status_code == 200

    # Verify habit is deleted
    get_response = client.get(
        f"/api/v1/habits/{sample_habit['id']}",
        headers=auth_headers,
    )
    assert get_response.status_code == 404


# ============================================================================
# Task 3 - Complete Habit Tests
# ============================================================================


def test_complete_habit(auth_headers, sample_habit, client: TestClient):
    """
    Task 3 - Complete Habit
    Test marking a habit as completed for a specific date.
    """
    today = date.today()
    date_str = today.strftime("%d-%m-%Y")

    response = client.post(
        f"/api/v1/habits/{sample_habit['id']}/completions",
        json={"date_fmt": "%d-%m-%Y", "date": date_str, "done": True},
        headers=auth_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["day"] == date_str
    assert data["done"] == True


def test_uncomplete_habit(auth_headers, sample_habit, client: TestClient):
    """Test marking a habit as not completed (undoing completion)."""
    today = date.today()
    date_str = today.strftime("%d-%m-%Y")

    # First complete it
    client.post(
        f"/api/v1/habits/{sample_habit['id']}/completions",
        json={"date_fmt": "%d-%m-%Y", "date": date_str, "done": True},
        headers=auth_headers,
    )

    # Then uncomplete it
    response = client.post(
        f"/api/v1/habits/{sample_habit['id']}/completions",
        json={"date_fmt": "%d-%m-%Y", "date": date_str, "done": False},
        headers=auth_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["done"] == False


def test_complete_habit_with_note(auth_headers, sample_habit, client: TestClient):
    """Test completing a habit with a text note."""
    today = date.today()
    date_str = today.strftime("%d-%m-%Y")

    response = client.post(
        f"/api/v1/habits/{sample_habit['id']}/completions",
        json={
            "date_fmt": "%d-%m-%Y",
            "date": date_str,
            "done": True,
            "text": "Completed 30 minutes of running",
        },
        headers=auth_headers,
    )

    assert response.status_code == 200


def test_complete_habit_invalid_date_format(
    auth_headers, sample_habit, client: TestClient
):
    """Test that invalid date format returns an error."""
    response = client.post(
        f"/api/v1/habits/{sample_habit['id']}/completions",
        json={
            "date_fmt": "%d-%m-%Y",
            "date": "2024-12-16",  # Wrong format (YYYY-MM-DD instead of DD-MM-YYYY)
            "done": True,
        },
        headers=auth_headers,
    )

    assert response.status_code == 400


# ============================================================================
# Task 4 - Show Recent Completions Tests
# ============================================================================


def test_show_recent_completions(auth_headers, sample_habit, client: TestClient):
    """
    Task 4 - Show Recent Habit Completions
    Test retrieving completion history for a habit.
    """
    # Complete the habit on multiple dates
    dates_to_complete = [
        date(2024, 12, 10),
        date(2024, 12, 12),
        date(2024, 12, 16),
    ]

    for completion_date in dates_to_complete:
        client.post(
            f"/api/v1/habits/{sample_habit['id']}/completions",
            json={
                "date_fmt": "%d-%m-%Y",
                "date": completion_date.strftime("%d-%m-%Y"),
                "done": True,
            },
            headers=auth_headers,
        )

    # Get completions for December 2024
    response = client.get(
        f"/api/v1/habits/{sample_habit['id']}/completions",
        params={
            "date_fmt": "%d-%m-%Y",
            "date_start": "01-12-2024",
            "date_end": "30-12-2024",
            "status": "PERIOD_DONE,DONE",
            "sort": "asc",
        },
        headers=auth_headers,
    )

    assert response.status_code == 200
    completions = response.json()
    assert isinstance(completions, list)
    assert len(completions) >= 3
    assert "10-12-2024" in completions
    assert "12-12-2024" in completions
    assert "16-12-2024" in completions


def test_completions_sorted_descending(auth_headers, sample_habit, client: TestClient):
    """Test that completions can be sorted in descending order."""
    # Complete on two dates
    client.post(
        f"/api/v1/habits/{sample_habit['id']}/completions",
        json={"date_fmt": "%d-%m-%Y", "date": "10-12-2024", "done": True},
        headers=auth_headers,
    )
    client.post(
        f"/api/v1/habits/{sample_habit['id']}/completions",
        json={"date_fmt": "%d-%m-%Y", "date": "20-12-2024", "done": True},
        headers=auth_headers,
    )

    # Get with descending sort
    response = client.get(
        f"/api/v1/habits/{sample_habit['id']}/completions",
        params={
            "date_fmt": "%d-%m-%Y",
            "date_start": "01-12-2024",
            "date_end": "31-12-2024",
            "sort": "desc",
        },
        headers=auth_headers,
    )

    assert response.status_code == 200
    completions = response.json()
    # First item should be more recent than last item
    if len(completions) >= 2:
        first = datetime.strptime(completions[0], "%d-%m-%Y")
        last = datetime.strptime(completions[-1], "%d-%m-%Y")
        assert first >= last


def test_completions_with_limit(auth_headers, sample_habit, client: TestClient):
    """Test limiting the number of returned completions."""
    # Complete on multiple dates
    for day in range(1, 11):  # 10 completions
        client.post(
            f"/api/v1/habits/{sample_habit['id']}/completions",
            json={"date_fmt": "%d-%m-%Y", "date": f"{day:02d}-12-2024", "done": True},
            headers=auth_headers,
        )

    # Request with limit
    response = client.get(
        f"/api/v1/habits/{sample_habit['id']}/completions",
        params={
            "date_fmt": "%d-%m-%Y",
            "date_start": "01-12-2024",
            "date_end": "31-12-2024",
            "limit": 5,
        },
        headers=auth_headers,
    )

    assert response.status_code == 200
    completions = response.json()
    assert len(completions) <= 5


def test_completions_date_range_validation(
    auth_headers, sample_habit, client: TestClient
):
    """Test that date range validation works correctly."""
    # Missing date_end
    response = client.get(
        f"/api/v1/habits/{sample_habit['id']}/completions",
        params={
            "date_fmt": "%d-%m-%Y",
            "date_start": "01-12-2024",
            "date_end": "01-11-2024",
        },
        headers=auth_headers,
    )

    assert response.status_code == 400


# ============================================================================
# Admin API Tests
# ============================================================================


@pytest.fixture
async def admin_user(client: TestClient):
    """Create an admin user using open registration (default client)."""
    email = f"admin_{datetime.now().timestamp()}@test.com"
    original_admin_email = settings.ADMIN_EMAIL

    # Set this user as admin
    settings.ADMIN_EMAIL = email

    # Create the admin user with open registration
    response = client.post(
        "/auth/register",
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 201
    user_data = response.json()
    user = User(**user_data)

    yield user

    # Cleanup
    settings.ADMIN_EMAIL = original_admin_email


@pytest.fixture
async def admin_access_token(admin_user: User, client: TestClient):
    """Get access token for admin user."""
    response = client.post(
        "/auth/login",
        data={
            "grant_type": "password",
            "username": admin_user.email,
            "password": PASSWORD,
        },
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


@pytest.fixture
async def admin_headers(admin_access_token: str):
    """Get authorization headers for admin user."""
    return {
        "Authorization": f"Bearer {admin_access_token}",
        "accept": "application/json",
    }


async def test_admin_can_register_user(admin_headers, admin_protected_client: TestClient):
    """Test that admin can register a new user via /auth/register when admin-protected."""
    new_user_email = f"newuser_{datetime.now().timestamp()}@test.com"
    response = admin_protected_client.post(
        "/auth/register",
        json={"email": new_user_email, "password": PASSWORD},
        headers=admin_headers,
    )

    assert response.status_code == 201
    data = response.json()
    assert data["email"] == new_user_email
    assert data["is_active"]


async def test_admin_register_duplicate_email_fails(admin_headers, admin_protected_client: TestClient):
    """Test that registering a user with existing email fails."""
    email = f"duplicate_{datetime.now().timestamp()}@test.com"

    # Create first user
    response1 = admin_protected_client.post(
        "/auth/register",
        json={"email": email, "password": PASSWORD},
        headers=admin_headers,
    )
    assert response1.status_code == 201

    # Try to create duplicate
    response2 = admin_protected_client.post(
        "/auth/register",
        json={"email": email, "password": PASSWORD},
        headers=admin_headers,
    )
    assert response2.status_code == 400


async def test_non_admin_cannot_register_user(auth_headers, admin_protected_client: TestClient):
    """Test that non-admin users cannot register new users when admin-only mode is enabled."""
    response = admin_protected_client.post(
        "/auth/register",
        json={"email": "should_fail@test.com", "password": PASSWORD},
        headers=auth_headers,
    )
    assert response.status_code == 401


# ============================================================================
# Additional Edge Cases
# ============================================================================


def test_get_nonexistent_habit(auth_headers, client: TestClient):
    """Test accessing a habit that doesn't exist."""
    response = client.get(
        "/api/v1/habits/nonexistent123",
        headers=auth_headers,
    )
    assert response.status_code == 404


def test_complete_nonexistent_habit(auth_headers, client: TestClient):
    """Test completing a habit that doesn't exist."""
    response = client.post(
        "/api/v1/habits/nonexistent123/completions",
        json={"date_fmt": "%d-%m-%Y", "date": "16-12-2024", "done": True},
        headers=auth_headers,
    )
    assert response.status_code == 404


# ============================================================================
# Completion Journal Tests (structured: date / done / notes / completion_type)
# ============================================================================

JOURNAL_FMT = "%d-%m-%Y"


def _tick(client, headers, habit_id, date_str, done, text=None):
    """Create/update a completion record through the public POST endpoint."""
    payload = {"date_fmt": JOURNAL_FMT, "date": date_str, "done": done}
    if text is not None:
        payload["text"] = text
    resp = client.post(
        f"/api/v1/habits/{habit_id}/completions", json=payload, headers=headers
    )
    assert resp.status_code == 200
    return resp


def _journal(client, headers, habit_id, **params):
    """GET the completion journal for a habit with the default date format."""
    params.setdefault("date_fmt", JOURNAL_FMT)
    return client.get(
        f"/api/v1/habits/{habit_id}/completions/journal",
        params=params,
        headers=headers,
    )


def _entry_for(entries, date_str):
    """Return the single journal entry for a date (asserting uniqueness)."""
    matches = [e for e in entries if e["date"] == date_str]
    assert len(matches) == 1, f"expected one entry for {date_str}, got {matches}"
    return matches[0]


def test_journal_normal_check(auth_headers, sample_habit, client: TestClient):
    """普通勾选: a plain done=True check-in appears with done=True and DONE type."""
    _tick(client, auth_headers, sample_habit["id"], "10-12-2024", True)

    resp = _journal(
        client,
        auth_headers,
        sample_habit["id"],
        date_start="01-12-2024",
        date_end="31-12-2024",
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["habit_id"] == sample_habit["id"]
    assert body["total"] == 1
    entry = _entry_for(body["entries"], "10-12-2024")
    assert entry["done"] is True
    assert entry["notes"] == ""
    assert "DONE" in entry["completion_type"]
    assert "PERIOD_DONE" not in entry["completion_type"]


def test_journal_done_with_note(auth_headers, sample_habit, client: TestClient):
    """备注记录: a done check-in carrying a note exposes the note text."""
    _tick(client, auth_headers, sample_habit["id"], "10-12-2024", True, text="ran 5k")

    resp = _journal(
        client,
        auth_headers,
        sample_habit["id"],
        date_start="01-12-2024",
        date_end="31-12-2024",
    )

    assert resp.status_code == 200
    entry = _entry_for(resp.json()["entries"], "10-12-2024")
    assert entry["done"] is True
    assert entry["notes"] == "ran 5k"
    assert "DONE" in entry["completion_type"]


def test_journal_note_only_record(auth_headers, sample_habit, client: TestClient):
    """备注记录: a done=False record that only carries a note is still returned.

    The legacy date-list endpoint filters on completion status, so these
    note-only records were previously invisible to external clients.
    """
    _tick(
        client, auth_headers, sample_habit["id"], "11-12-2024", False, text="rest day"
    )

    resp = _journal(
        client,
        auth_headers,
        sample_habit["id"],
        date_start="01-12-2024",
        date_end="31-12-2024",
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    entry = _entry_for(body["entries"], "11-12-2024")
    assert entry["done"] is False
    assert entry["notes"] == "rest day"
    assert entry["completion_type"] == []


def test_journal_periodic_habit_period_done(
    auth_headers, sample_habit, client: TestClient
):
    """周期习惯: when a weekly target is met, those days are marked PERIOD_DONE."""
    habit_id = sample_habit["id"]
    # Target: 2 completions per 1 week.
    put = client.put(
        f"/api/v1/habits/{habit_id}",
        json={"period": {"period_type": "W", "period_count": 1, "target_count": 2}},
        headers=auth_headers,
    )
    assert put.status_code == 200

    # 2024-12-09 (Mon) and 2024-12-10 (Tue) fall in the same ISO week -> target met.
    _tick(client, auth_headers, habit_id, "09-12-2024", True)
    _tick(client, auth_headers, habit_id, "10-12-2024", True)

    resp = _journal(
        client,
        auth_headers,
        habit_id,
        date_start="09-12-2024",
        date_end="15-12-2024",
    )

    assert resp.status_code == 200
    entries = resp.json()["entries"]
    for d in ("09-12-2024", "10-12-2024"):
        entry = _entry_for(entries, d)
        assert entry["done"] is True
        assert "PERIOD_DONE" in entry["completion_type"]
        assert "DONE" in entry["completion_type"]


def test_journal_periodic_below_target_not_period_done(
    auth_headers, sample_habit, client: TestClient
):
    """周期习惯: a single check-in below the weekly target is DONE but not PERIOD_DONE."""
    habit_id = sample_habit["id"]
    client.put(
        f"/api/v1/habits/{habit_id}",
        json={"period": {"period_type": "W", "period_count": 1, "target_count": 2}},
        headers=auth_headers,
    )
    _tick(client, auth_headers, habit_id, "09-12-2024", True)

    resp = _journal(
        client,
        auth_headers,
        habit_id,
        date_start="09-12-2024",
        date_end="15-12-2024",
    )

    assert resp.status_code == 200
    entry = _entry_for(resp.json()["entries"], "09-12-2024")
    assert entry["completion_type"] == ["DONE"]


def test_journal_time_range_filter(auth_headers, sample_habit, client: TestClient):
    """Records outside the requested range are excluded from the journal."""
    habit_id = sample_habit["id"]
    _tick(client, auth_headers, habit_id, "05-12-2024", True)
    _tick(client, auth_headers, habit_id, "20-12-2024", True)

    resp = _journal(
        client,
        auth_headers,
        habit_id,
        date_start="10-12-2024",
        date_end="31-12-2024",
    )

    assert resp.status_code == 200
    body = resp.json()
    dates = [e["date"] for e in body["entries"]]
    assert "20-12-2024" in dates
    assert "05-12-2024" not in dates
    assert body["total"] == 1


def test_journal_sort_desc(auth_headers, sample_habit, client: TestClient):
    """Entries can be sorted by date in descending order."""
    habit_id = sample_habit["id"]
    for d in ("05-12-2024", "10-12-2024", "20-12-2024"):
        _tick(client, auth_headers, habit_id, d, True)

    resp = _journal(
        client,
        auth_headers,
        habit_id,
        date_start="01-12-2024",
        date_end="31-12-2024",
        sort="desc",
    )

    assert resp.status_code == 200
    dates = [e["date"] for e in resp.json()["entries"]]
    assert dates == ["20-12-2024", "10-12-2024", "05-12-2024"]


def test_journal_pagination(auth_headers, sample_habit, client: TestClient):
    """limit/offset page through entries and total reflects the full match count."""
    habit_id = sample_habit["id"]
    for day in range(1, 8):  # 7 records: 01..07 December
        _tick(client, auth_headers, habit_id, f"{day:02d}-12-2024", True)

    common = dict(date_start="01-12-2024", date_end="31-12-2024", sort="asc", limit=3)

    p1 = _journal(client, auth_headers, habit_id, offset=0, **common).json()
    assert p1["total"] == 7
    assert p1["limit"] == 3
    assert p1["offset"] == 0
    assert [e["date"] for e in p1["entries"]] == [
        "01-12-2024",
        "02-12-2024",
        "03-12-2024",
    ]

    p2 = _journal(client, auth_headers, habit_id, offset=3, **common).json()
    assert p2["total"] == 7
    assert [e["date"] for e in p2["entries"]] == [
        "04-12-2024",
        "05-12-2024",
        "06-12-2024",
    ]

    p3 = _journal(client, auth_headers, habit_id, offset=6, **common).json()
    assert p3["total"] == 7
    assert [e["date"] for e in p3["entries"]] == ["07-12-2024"]


def test_journal_invalid_params(auth_headers, sample_habit, client: TestClient):
    """Bad date/sort -> 400; out-of-range limit -> 422 (FastAPI Query validation)."""
    habit_id = sample_habit["id"]

    # Wrong date format for the given date_fmt.
    assert _journal(client, auth_headers, habit_id, date_start="2024-12-01").status_code == 400
    # date_start after date_end.
    assert (
        _journal(
            client,
            auth_headers,
            habit_id,
            date_start="31-12-2024",
            date_end="01-12-2024",
        ).status_code
        == 400
    )
    # Invalid sort value.
    assert _journal(client, auth_headers, habit_id, sort="sideways").status_code == 400
    # limit below the allowed minimum.
    assert _journal(client, auth_headers, habit_id, limit=0).status_code == 422


def test_journal_requires_auth(sample_habit, client: TestClient):
    """无权限访问: a request without an Authorization header is rejected (401)."""
    resp = client.get(f"/api/v1/habits/{sample_habit['id']}/completions/journal")
    assert resp.status_code == 401


def test_journal_nonexistent_habit(auth_headers, client: TestClient):
    """无权限访问: an unknown habit id returns 404."""
    resp = _journal(client, auth_headers, "nonexistent123")
    assert resp.status_code == 404


def test_journal_cross_user_forbidden(auth_headers, sample_habit, client: TestClient):
    """无权限访问: a different user cannot read someone else's journal (404)."""
    # sample_habit belongs to the first user (auth_headers). Register a second user.
    other_email = f"other_{datetime.now().timestamp()}@test.com"
    reg = client.post(
        "/auth/register", json={"email": other_email, "password": PASSWORD}
    )
    assert reg.status_code == 201

    login = client.post(
        "/auth/login",
        data={
            "grant_type": "password",
            "username": other_email,
            "password": PASSWORD,
        },
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert login.status_code == 200
    other_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = _journal(client, other_headers, sample_habit["id"])
    assert resp.status_code == 404
