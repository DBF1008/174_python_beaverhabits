"""
Test suite for the structured statistics API endpoints.
Covers: regular habits, periodic habits, empty-data habits,
        date-range validation, sorting, and authentication.
"""

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger

from beaverhabits.app.app import init_auth_routes
from beaverhabits.app.db import User, create_db_and_tables, engine
from beaverhabits.routes.api import init_api_routes

PASSWORD = "TestPassword123!"


def _create_test_app() -> FastAPI:
    """Create a minimal FastAPI app with auth + API routes only (no NiceGUI)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await create_db_and_tables()
        yield

    test_app = FastAPI(lifespan=lifespan)
    init_auth_routes(test_app)
    init_api_routes(test_app)
    return test_app


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(name="client", scope="module")
async def client_fixture():
    test_app = _create_test_app()
    with TestClient(test_app, raise_server_exceptions=True) as client:
        yield client
    await engine.dispose()


@pytest.fixture
async def test_user(client: TestClient):
    email = f"statsuser_{datetime.now().timestamp()}@test.com"
    response = client.post(
        "/auth/register",
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 201
    return User(**response.json())


@pytest.fixture
async def access_token(test_user: User, client: TestClient):
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
    return response.json()["access_token"]


@pytest.fixture
async def auth_headers(access_token):
    return {"Authorization": f"Bearer {access_token}"}


def _tick(client: TestClient, headers: dict, habit_id: str, day: date, done=True):
    """Helper: tick a habit on a given date."""
    resp = client.post(
        f"/api/v1/habits/{habit_id}/completions",
        json={
            "date_fmt": "%Y-%m-%d",
            "date": day.strftime("%Y-%m-%d"),
            "done": done,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


def _create_habit(client: TestClient, headers: dict, name: str) -> dict:
    """Helper: create a habit and return its JSON."""
    resp = client.post(
        "/api/v1/habits",
        json={"name": name},
        headers=headers,
    )
    assert resp.status_code == 200
    return resp.json()


def _set_period(client: TestClient, headers: dict, habit_id: str,
                period_type: str, period_count: int, target_count: int):
    """Helper: set a period on a habit."""
    resp = client.put(
        f"/api/v1/habits/{habit_id}",
        json={"period": {
            "period_type": period_type,
            "period_count": period_count,
            "target_count": target_count,
        }},
        headers=headers,
    )
    assert resp.status_code == 200


@pytest.fixture
async def regular_habit(auth_headers, client: TestClient):
    """A regular (non-periodic) habit with two streaks:
    - 5 consecutive days ending today (current streak)
    - 3 consecutive days ending 10 days ago
    """
    habit = _create_habit(client, auth_headers, "Morning Run")
    today = date.today()

    # 5-day current streak: today, today-1, ..., today-4
    for i in range(5):
        _tick(client, auth_headers, habit["id"], today - timedelta(days=i))

    # 3-day older streak: today-10, today-11, today-12
    for i in range(10, 13):
        _tick(client, auth_headers, habit["id"], today - timedelta(days=i))

    return habit


@pytest.fixture
async def periodic_habit(auth_headers, client: TestClient):
    """A weekly periodic habit (target: 3 times/week)
    - Current week: 3 ticks (completed)
    - Previous week: 2 ticks (not completed)
    """
    habit = _create_habit(client, auth_headers, "Gym Sessions")
    _set_period(client, auth_headers, habit["id"], "W", 1, 3)

    today = date.today()
    # Current week (Mon-Sun): tick Mon, Tue, Wed
    weekday = today.weekday()  # 0=Mon
    monday = today - timedelta(days=weekday)

    _tick(client, auth_headers, habit["id"], monday)
    _tick(client, auth_headers, habit["id"], monday + timedelta(days=1))
    _tick(client, auth_headers, habit["id"], monday + timedelta(days=2))

    # Previous week: tick only Mon, Tue (2 out of 3 — not completed)
    prev_monday = monday - timedelta(days=7)
    _tick(client, auth_headers, habit["id"], prev_monday)
    _tick(client, auth_headers, habit["id"], prev_monday + timedelta(days=1))

    return habit


@pytest.fixture
async def empty_habit(auth_headers, client: TestClient):
    """A habit with zero records."""
    return _create_habit(client, auth_headers, "Reading (empty)")


# ============================================================================
# Streak endpoint tests
# ============================================================================


def test_streak_regular_habit(auth_headers, regular_habit, client: TestClient):
    """Regular habit should show current streak, longest streak, and recent streaks."""
    resp = client.get(
        f"/api/v1/habits/{regular_habit['id']}/stats/streaks",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["current_streak"] == 5
    assert data["longest_streak"] == 5
    assert isinstance(data["recent_streaks"], list)
    assert len(data["recent_streaks"]) == 2

    # First streak (most recent) should be the 5-day run
    recent = data["recent_streaks"][0]
    assert recent["length"] == 5
    assert "start" in recent
    assert "end" in recent

    # Second streak should be the 3-day run
    older = data["recent_streaks"][1]
    assert older["length"] == 3


def test_streak_periodic_habit(auth_headers, periodic_habit, client: TestClient):
    """Periodic habit streaks should include PERIOD_DONE days."""
    resp = client.get(
        f"/api/v1/habits/{periodic_habit['id']}/stats/streaks",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    # The habit has ticked days, streaks should be computed
    assert data["longest_streak"] >= 1
    assert isinstance(data["recent_streaks"], list)


def test_streak_empty_habit(auth_headers, empty_habit, client: TestClient):
    """Empty habit should return zero streaks."""
    resp = client.get(
        f"/api/v1/habits/{empty_habit['id']}/stats/streaks",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["current_streak"] == 0
    assert data["longest_streak"] == 0
    assert data["recent_streaks"] == []


def test_streak_requires_auth(regular_habit, client: TestClient):
    """Streak endpoint should require authentication."""
    resp = client.get(
        f"/api/v1/habits/{regular_habit['id']}/stats/streaks",
    )
    assert resp.status_code == 401


def test_streak_nonexistent_habit(auth_headers, client: TestClient):
    """Requesting streaks for a nonexistent habit should return 404."""
    resp = client.get(
        "/api/v1/habits/nonexistent999/stats/streaks",
        headers=auth_headers,
    )
    assert resp.status_code == 404


# ============================================================================
# Period endpoint tests
# ============================================================================


def test_period_summary_weekly(auth_headers, periodic_habit, client: TestClient):
    """Weekly periodic habit should return per-week progress."""
    today = date.today()
    start = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    resp = client.get(
        f"/api/v1/habits/{periodic_habit['id']}/stats/periods",
        params={"date_start": start, "date_end": end},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["period_type"] == "W"
    assert data["period_count"] == 1
    assert data["target_count"] == 3
    assert isinstance(data["periods"], list)
    assert len(data["periods"]) > 0
    assert 0.0 <= data["overall_completion_rate"] <= 1.0

    # Check that at least one period is completed (current week with 3 ticks)
    completed_periods = [p for p in data["periods"] if p["completed"]]
    assert len(completed_periods) >= 1

    # Check structure of individual period entries
    for period in data["periods"]:
        assert "period_start" in period
        assert "period_end" in period
        assert "done_count" in period
        assert "target_count" in period
        assert period["target_count"] == 3
        assert "completed" in period


def test_period_summary_custom_date_range(auth_headers, periodic_habit, client: TestClient):
    """Period summary should respect custom date ranges."""
    # Use a very narrow range — just the current week
    today = date.today()
    weekday = today.weekday()
    monday = today - timedelta(days=weekday)
    sunday = monday + timedelta(days=6)

    resp = client.get(
        f"/api/v1/habits/{periodic_habit['id']}/stats/periods",
        params={
            "date_start": monday.strftime("%Y-%m-%d"),
            "date_end": sunday.strftime("%Y-%m-%d"),
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    # Should only contain the current week's period
    assert len(data["periods"]) == 1
    assert data["periods"][0]["done_count"] == 3
    assert data["periods"][0]["completed"] is True


def test_period_no_period_returns_404(auth_headers, regular_habit, client: TestClient):
    """Non-periodic habit should return 404 for period stats."""
    resp = client.get(
        f"/api/v1/habits/{regular_habit['id']}/stats/periods",
        headers=auth_headers,
    )
    assert resp.status_code == 404
    assert "No period configured" in resp.json()["detail"]


def test_period_invalid_dates(auth_headers, periodic_habit, client: TestClient):
    """date_start > date_end should return 400."""
    resp = client.get(
        f"/api/v1/habits/{periodic_habit['id']}/stats/periods",
        params={
            "date_start": "2026-12-01",
            "date_end": "2026-01-01",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 400


def test_period_invalid_date_format(auth_headers, periodic_habit, client: TestClient):
    """Invalid date format should return 400."""
    resp = client.get(
        f"/api/v1/habits/{periodic_habit['id']}/stats/periods",
        params={
            "date_start": "01-13-2026",  # Wrong format
        },
        headers=auth_headers,
    )
    assert resp.status_code == 400


# ============================================================================
# Monthly trend endpoint tests
# ============================================================================


def test_monthly_trend_default_13_months(auth_headers, regular_habit, client: TestClient):
    """Default monthly trend should return 13 months of data."""
    resp = client.get(
        f"/api/v1/habits/{regular_habit['id']}/stats/monthly-trend",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    assert "months" in data
    assert len(data["months"]) == 13

    for entry in data["months"]:
        assert "year" in entry
        assert "month" in entry
        assert "label" in entry
        assert "count" in entry
        assert isinstance(entry["count"], int)
        assert entry["count"] >= 0


def test_monthly_trend_custom_months(auth_headers, regular_habit, client: TestClient):
    """Custom total_months should return the right number of entries."""
    resp = client.get(
        f"/api/v1/habits/{regular_habit['id']}/stats/monthly-trend",
        params={"total_months": 6},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["months"]) == 6


def test_monthly_trend_empty_habit(auth_headers, empty_habit, client: TestClient):
    """Empty habit should return all-zero monthly counts."""
    resp = client.get(
        f"/api/v1/habits/{empty_habit['id']}/stats/monthly-trend",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    assert len(data["months"]) == 13
    for entry in data["months"]:
        assert entry["count"] == 0


def test_monthly_trend_has_ticks(auth_headers, client: TestClient):
    """Monthly trend should show non-zero counts for months with ticks.

    Note: The monthly trend function counts *past* months (excludes the
    current month), matching the web UI behaviour.
    """
    habit = _create_habit(client, auth_headers, "Trend Test Habit")

    # Tick some days in the previous month so they show up in the trend
    today = date.today()
    prev_month_day = today - timedelta(days=35)  # ~5 weeks ago → previous month
    _tick(client, auth_headers, habit["id"], prev_month_day)
    _tick(client, auth_headers, habit["id"], prev_month_day + timedelta(days=1))
    _tick(client, auth_headers, habit["id"], prev_month_day + timedelta(days=2))

    resp = client.get(
        f"/api/v1/habits/{habit['id']}/stats/monthly-trend",
        params={"total_months": 3},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    # At least one of the 3 months should have ticks
    total_ticks = sum(entry["count"] for entry in data["months"])
    assert total_ticks >= 3


# ============================================================================
# Heatmap endpoint tests
# ============================================================================


def test_heatmap_regular_habit(auth_headers, regular_habit, client: TestClient):
    """Heatmap stats should reflect completed days for a regular habit."""
    today = date.today()
    start = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    resp = client.get(
        f"/api/v1/habits/{regular_habit['id']}/stats/heatmap",
        params={"date_start": start, "date_end": end},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["start"] == start
    assert data["end"] == end
    assert data["total_days"] == 31
    assert data["completed_days"] == 8  # 5 + 3 ticks
    assert 0.0 < data["completion_rate"] < 1.0


def test_heatmap_periodic_habit(auth_headers, periodic_habit, client: TestClient):
    """Heatmap should include PERIOD_DONE days for periodic habits."""
    today = date.today()
    start = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    resp = client.get(
        f"/api/v1/habits/{periodic_habit['id']}/stats/heatmap",
        params={"date_start": start, "date_end": end},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["completed_days"] >= 5  # At least the 5 raw ticks
    assert data["total_days"] == 31


def test_heatmap_empty_habit(auth_headers, empty_habit, client: TestClient):
    """Empty habit heatmap should show zero completions."""
    today = date.today()
    start = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    resp = client.get(
        f"/api/v1/habits/{empty_habit['id']}/stats/heatmap",
        params={"date_start": start, "date_end": end},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["completed_days"] == 0
    assert data["completion_rate"] == 0.0


def test_heatmap_custom_range(auth_headers, regular_habit, client: TestClient):
    """Heatmap should respect custom date ranges."""
    today = date.today()
    # Only look at the last 5 days
    start = (today - timedelta(days=4)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    resp = client.get(
        f"/api/v1/habits/{regular_habit['id']}/stats/heatmap",
        params={"date_start": start, "date_end": end},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_days"] == 5
    assert data["completed_days"] == 5  # All 5 consecutive days


def test_heatmap_invalid_range(auth_headers, regular_habit, client: TestClient):
    """date_start after date_end should return 400."""
    resp = client.get(
        f"/api/v1/habits/{regular_habit['id']}/stats/heatmap",
        params={
            "date_start": "2026-12-01",
            "date_end": "2026-01-01",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 400


# ============================================================================
# Batch endpoint tests
# ============================================================================


def test_batch_returns_all_active_habits(
    auth_headers, regular_habit, periodic_habit, empty_habit, client: TestClient
):
    """Batch endpoint should return stats for all active habits."""
    resp = client.get(
        "/api/v1/habits/stats/batch",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    assert "habits" in data
    habit_ids = {h["habit_id"] for h in data["habits"]}
    assert regular_habit["id"] in habit_ids
    assert periodic_habit["id"] in habit_ids
    assert empty_habit["id"] in habit_ids


def test_batch_habit_structure(
    auth_headers, regular_habit, client: TestClient
):
    """Each habit in batch response should have all stats sections."""
    resp = client.get(
        "/api/v1/habits/stats/batch",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    for habit_stats in data["habits"]:
        assert "habit_id" in habit_stats
        assert "habit_name" in habit_stats
        assert "streak" in habit_stats
        assert "period" in habit_stats  # None for non-periodic
        assert "monthly_trend" in habit_stats
        assert "heatmap" in habit_stats

        # Streak structure
        streak = habit_stats["streak"]
        assert "current_streak" in streak
        assert "longest_streak" in streak
        assert "recent_streaks" in streak

        # Heatmap structure
        heatmap = habit_stats["heatmap"]
        assert "total_days" in heatmap
        assert "completed_days" in heatmap
        assert "completion_rate" in heatmap


def test_batch_sort_by_name(
    auth_headers, regular_habit, periodic_habit, empty_habit, client: TestClient
):
    """Sorting by name should order alphabetically."""
    resp = client.get(
        "/api/v1/habits/stats/batch",
        params={"sort_by": "name", "sort_order": "asc"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    names = [h["habit_name"] for h in resp.json()["habits"]]
    assert names == sorted(names, key=str.lower)


def test_batch_sort_by_name_desc(
    auth_headers, regular_habit, periodic_habit, empty_habit, client: TestClient
):
    """Sorting by name descending should reverse alphabetical order."""
    resp = client.get(
        "/api/v1/habits/stats/batch",
        params={"sort_by": "name", "sort_order": "desc"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    names = [h["habit_name"] for h in resp.json()["habits"]]
    assert names == sorted(names, key=str.lower, reverse=True)


def test_batch_sort_by_completion_rate(
    auth_headers, regular_habit, empty_habit, client: TestClient
):
    """Sorting by completion_rate descending should put highest rate first."""
    today = date.today()
    start = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    resp = client.get(
        "/api/v1/habits/stats/batch",
        params={
            "sort_by": "completion_rate",
            "sort_order": "desc",
            "date_start": start,
            "date_end": end,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    rates = [h["heatmap"]["completion_rate"] for h in resp.json()["habits"]]
    assert rates == sorted(rates, reverse=True)


def test_batch_sort_by_streak_length(
    auth_headers, regular_habit, empty_habit, client: TestClient
):
    """Sorting by streak_length descending should put longest current streak first."""
    resp = client.get(
        "/api/v1/habits/stats/batch",
        params={"sort_by": "streak_length", "sort_order": "desc"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    streaks = [h["streak"]["current_streak"] for h in resp.json()["habits"]]
    assert streaks == sorted(streaks, reverse=True)


def test_batch_empty_habits(auth_headers, empty_habit, client: TestClient):
    """Empty habits should return valid zero-valued stats in batch."""
    resp = client.get(
        "/api/v1/habits/stats/batch",
        headers=auth_headers,
    )
    assert resp.status_code == 200

    # Find the empty habit
    data = resp.json()
    empty_stats = next(
        h for h in data["habits"] if h["habit_id"] == empty_habit["id"]
    )

    assert empty_stats["streak"]["current_streak"] == 0
    assert empty_stats["streak"]["longest_streak"] == 0
    assert empty_stats["period"] is None
    assert empty_stats["heatmap"]["completed_days"] == 0
    assert empty_stats["heatmap"]["completion_rate"] == 0.0
    for entry in empty_stats["monthly_trend"]["months"]:
        assert entry["count"] == 0


def test_batch_periodic_habit_has_period_data(
    auth_headers, periodic_habit, client: TestClient
):
    """Periodic habits in batch should have non-null period data."""
    resp = client.get(
        "/api/v1/habits/stats/batch",
        headers=auth_headers,
    )
    assert resp.status_code == 200

    data = resp.json()
    periodic_stats = next(
        h for h in data["habits"] if h["habit_id"] == periodic_habit["id"]
    )

    assert periodic_stats["period"] is not None
    assert periodic_stats["period"]["period_type"] == "W"
    assert periodic_stats["period"]["target_count"] == 3


def test_batch_requires_auth(client: TestClient):
    """Batch endpoint should require authentication."""
    resp = client.get("/api/v1/habits/stats/batch")
    assert resp.status_code == 401


def test_batch_custom_date_range(
    auth_headers, regular_habit, client: TestClient
):
    """Batch endpoint should respect custom date ranges."""
    today = date.today()
    start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    resp = client.get(
        "/api/v1/habits/stats/batch",
        params={
            "date_start": start,
            "date_end": end,
            "total_months": 3,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    for habit_stats in data["habits"]:
        # Heatmap should use the custom range
        assert habit_stats["heatmap"]["start"] == start
        assert habit_stats["heatmap"]["end"] == end
        assert habit_stats["heatmap"]["total_days"] == 8

        # Monthly trend should have 3 entries
        assert len(habit_stats["monthly_trend"]["months"]) == 3


def test_batch_invalid_date_range(
    auth_headers, empty_habit, client: TestClient
):
    """Batch endpoint should reject invalid date ranges."""
    resp = client.get(
        "/api/v1/habits/stats/batch",
        params={
            "date_start": "2026-12-01",
            "date_end": "2026-01-01",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 400
