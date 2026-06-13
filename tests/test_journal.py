"""
Regression tests for the GET /api/v1/habits/{habit_id}/journal endpoint.

Covers:
  - Normal tick (DONE, no note)
  - Tick with note text
  - Period habit producing PERIOD_DONE entries
  - Response structure & pagination metadata
  - Sorting (asc / desc)
  - Offset + limit pagination
  - Date-range filtering
  - Input validation (bad dates, bad sort, negative offset)
  - Cross-user unauthorized access (404)
"""

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from loguru import logger

from beaverhabits.app.db import User, engine
from beaverhabits.main import app

PASSWORD = "TestPassword123!"
DATE_FMT = "%d-%m-%Y"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(name="client", scope="module")
async def client_fixture():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    await engine.dispose()


def _register_and_login(client: TestClient, email: str) -> dict:
    """Register a new user and return auth headers."""
    resp = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 201, f"register failed: {resp.text}"

    resp = client.post(
        "/auth/login",
        data={
            "grant_type": "password",
            "username": email,
            "password": PASSWORD,
        },
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}", "accept": "application/json"}


@pytest.fixture
async def user_a_headers(client: TestClient):
    email = f"journal_a_{datetime.now().timestamp()}@test.com"
    return _register_and_login(client, email)


@pytest.fixture
async def user_b_headers(client: TestClient):
    email = f"journal_b_{datetime.now().timestamp()}@test.com"
    return _register_and_login(client, email)


def _create_habit(client: TestClient, headers: dict, name: str) -> dict:
    resp = client.post("/api/v1/habits", json={"name": name}, headers=headers)
    assert resp.status_code == 200
    return resp.json()


def _tick(
    client: TestClient,
    headers: dict,
    habit_id: str,
    day: date,
    done: bool = True,
    text: str | None = None,
):
    payload: dict = {
        "date_fmt": DATE_FMT,
        "date": day.strftime(DATE_FMT),
        "done": done,
    }
    if text is not None:
        payload["text"] = text
    resp = client.post(
        f"/api/v1/habits/{habit_id}/completions", json=payload, headers=headers
    )
    assert resp.status_code == 200
    return resp.json()


def _set_period(
    client: TestClient,
    headers: dict,
    habit_id: str,
    period_type: str,
    period_count: int,
    target_count: int,
):
    resp = client.put(
        f"/api/v1/habits/{habit_id}",
        json={
            "period": {
                "period_type": period_type,
                "period_count": period_count,
                "target_count": target_count,
            }
        },
        headers=headers,
    )
    assert resp.status_code == 200


def _get_journal(client: TestClient, headers: dict, habit_id: str, **params):
    return client.get(
        f"/api/v1/habits/{habit_id}/journal",
        params=params,
        headers=headers,
    )


# ---------------------------------------------------------------------------
# 1. Normal tick (DONE, no note)
# ---------------------------------------------------------------------------


async def test_journal_normal_tick(client: TestClient, user_a_headers):
    """A simple tick without text produces a DONE journal entry with empty text."""
    habit = _create_habit(client, user_a_headers, "Journal Normal")
    _tick(client, user_a_headers, habit["id"], date(2025, 3, 10))

    resp = _get_journal(
        client,
        user_a_headers,
        habit["id"],
        date_fmt=DATE_FMT,
        date_start="01-03-2025",
        date_end="31-03-2025",
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["total"] >= 1
    assert body["offset"] == 0
    assert body["limit"] is None

    entries = body["entries"]
    done_entries = [e for e in entries if e["date"] == "10-03-2025"]
    assert len(done_entries) == 1

    entry = done_entries[0]
    assert entry["done"] is True
    assert entry["text"] == ""
    assert entry["completion_type"] == "DONE"


# ---------------------------------------------------------------------------
# 2. Tick with note text
# ---------------------------------------------------------------------------


async def test_journal_with_note(client: TestClient, user_a_headers):
    """A tick with text produces a DONE journal entry carrying the note."""
    habit = _create_habit(client, user_a_headers, "Journal Note")
    _tick(
        client,
        user_a_headers,
        habit["id"],
        date(2025, 4, 5),
        text="Ran 5 km in the park",
    )

    resp = _get_journal(
        client,
        user_a_headers,
        habit["id"],
        date_fmt=DATE_FMT,
        date_start="01-04-2025",
        date_end="30-04-2025",
    )
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    match = [e for e in entries if e["date"] == "05-04-2025"]
    assert len(match) == 1
    assert match[0]["completion_type"] == "DONE"
    assert match[0]["done"] is True
    assert match[0]["text"] == "Ran 5 km in the park"


# ---------------------------------------------------------------------------
# 3. Period habit producing PERIOD_DONE entries
# ---------------------------------------------------------------------------


async def test_journal_period_habit(client: TestClient, user_a_headers):
    """
    Weekly habit with target 2.  Ticking twice in the same ISO week
    (Mon 2024-12-02 & Tue 2024-12-03) should:
      - mark those 2 days as DONE (with their notes)
      - mark the remaining 5 days of the week as PERIOD_DONE
    """
    habit = _create_habit(client, user_a_headers, "Journal Period")
    _set_period(client, user_a_headers, habit["id"], "W", 1, 2)

    _tick(client, user_a_headers, habit["id"], date(2024, 12, 2), text="Monday run")
    _tick(client, user_a_headers, habit["id"], date(2024, 12, 3), text="Tuesday gym")

    resp = _get_journal(
        client,
        user_a_headers,
        habit["id"],
        date_fmt=DATE_FMT,
        date_start="02-12-2024",
        date_end="08-12-2024",
    )
    assert resp.status_code == 200
    body = resp.json()
    entries = body["entries"]

    # Should cover the full week Mon–Sun
    assert body["total"] == 7
    assert len(entries) == 7

    # First two days: explicit DONE with notes
    done_entries = [e for e in entries if e["completion_type"] == "DONE"]
    assert len(done_entries) == 2
    for e in done_entries:
        assert e["done"] is True
        assert e["text"] != ""

    # Remaining days: PERIOD_DONE
    period_entries = [e for e in entries if e["completion_type"] == "PERIOD_DONE"]
    assert len(period_entries) == 5
    for e in period_entries:
        assert e["done"] is True
        assert e["text"] == ""


# ---------------------------------------------------------------------------
# 4. Cross-user unauthorized access → 404
# ---------------------------------------------------------------------------


async def test_journal_unauthorized_access(client: TestClient, user_a_headers, user_b_headers):
    """User B cannot read User A's journal — the API returns 404."""
    habit = _create_habit(client, user_a_headers, "Journal Private")
    _tick(client, user_a_headers, habit["id"], date(2025, 5, 1))

    resp = _get_journal(
        client,
        user_b_headers,
        habit["id"],
        date_fmt=DATE_FMT,
        date_start="01-05-2025",
        date_end="31-05-2025",
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 5. Sorting
# ---------------------------------------------------------------------------


async def test_journal_sort_asc(client: TestClient, user_a_headers):
    """Ascending sort returns earlier dates first."""
    habit = _create_habit(client, user_a_headers, "Journal Sort Asc")
    for d in [date(2025, 6, 3), date(2025, 6, 1), date(2025, 6, 5)]:
        _tick(client, user_a_headers, habit["id"], d)

    resp = _get_journal(
        client,
        user_a_headers,
        habit["id"],
        date_fmt=DATE_FMT,
        date_start="01-06-2025",
        date_end="30-06-2025",
        sort="asc",
    )
    assert resp.status_code == 200
    dates = [e["date"] for e in resp.json()["entries"]]
    assert dates == sorted(dates, key=lambda s: datetime.strptime(s, DATE_FMT))


async def test_journal_sort_desc(client: TestClient, user_a_headers):
    """Descending sort returns later dates first."""
    habit = _create_habit(client, user_a_headers, "Journal Sort Desc")
    for d in [date(2025, 6, 3), date(2025, 6, 1), date(2025, 6, 5)]:
        _tick(client, user_a_headers, habit["id"], d)

    resp = _get_journal(
        client,
        user_a_headers,
        habit["id"],
        date_fmt=DATE_FMT,
        date_start="01-06-2025",
        date_end="30-06-2025",
        sort="desc",
    )
    assert resp.status_code == 200
    dates = [e["date"] for e in resp.json()["entries"]]
    assert dates == sorted(dates, key=lambda s: datetime.strptime(s, DATE_FMT), reverse=True)


# ---------------------------------------------------------------------------
# 6. Pagination (offset + limit)
# ---------------------------------------------------------------------------


async def test_journal_pagination(client: TestClient, user_a_headers):
    """offset + limit slice the journal correctly; total reflects the full set."""
    habit = _create_habit(client, user_a_headers, "Journal Paging")
    for d in range(1, 11):  # 10 ticks
        _tick(client, user_a_headers, habit["id"], date(2025, 7, d))

    # Page 1: offset=0, limit=3
    resp = _get_journal(
        client,
        user_a_headers,
        habit["id"],
        date_fmt=DATE_FMT,
        date_start="01-07-2025",
        date_end="31-07-2025",
        sort="asc",
        offset=0,
        limit=3,
    )
    body = resp.json()
    assert body["total"] == 10
    assert body["offset"] == 0
    assert body["limit"] == 3
    assert len(body["entries"]) == 3
    assert body["entries"][0]["date"] == "01-07-2025"

    # Page 2: offset=3, limit=3
    resp2 = _get_journal(
        client,
        user_a_headers,
        habit["id"],
        date_fmt=DATE_FMT,
        date_start="01-07-2025",
        date_end="31-07-2025",
        sort="asc",
        offset=3,
        limit=3,
    )
    body2 = resp2.json()
    assert body2["total"] == 10
    assert body2["offset"] == 3
    assert len(body2["entries"]) == 3
    assert body2["entries"][0]["date"] == "04-07-2025"

    # Last page (partial): offset=8, limit=5
    resp3 = _get_journal(
        client,
        user_a_headers,
        habit["id"],
        date_fmt=DATE_FMT,
        date_start="01-07-2025",
        date_end="31-07-2025",
        sort="asc",
        offset=8,
        limit=5,
    )
    body3 = resp3.json()
    assert body3["total"] == 10
    assert len(body3["entries"]) == 2


# ---------------------------------------------------------------------------
# 7. Date-range filtering
# ---------------------------------------------------------------------------


async def test_journal_date_range_filter(client: TestClient, user_a_headers):
    """Only entries within the requested range are returned."""
    habit = _create_habit(client, user_a_headers, "Journal Range")
    _tick(client, user_a_headers, habit["id"], date(2025, 8, 1))
    _tick(client, user_a_headers, habit["id"], date(2025, 8, 15))
    _tick(client, user_a_headers, habit["id"], date(2025, 8, 30))

    # Narrow range: only Aug 10-20
    resp = _get_journal(
        client,
        user_a_headers,
        habit["id"],
        date_fmt=DATE_FMT,
        date_start="10-08-2025",
        date_end="20-08-2025",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["entries"][0]["date"] == "15-08-2025"


# ---------------------------------------------------------------------------
# 8. Input validation
# ---------------------------------------------------------------------------


async def test_journal_invalid_date_format(client: TestClient, user_a_headers):
    """Invalid date strings return 400."""
    habit = _create_habit(client, user_a_headers, "Journal Bad Date")

    resp = _get_journal(
        client,
        user_a_headers,
        habit["id"],
        date_start="not-a-date",
        date_end="31-12-2025",
    )
    assert resp.status_code == 400


async def test_journal_date_start_after_end(client: TestClient, user_a_headers):
    """date_start after date_end returns 400."""
    habit = _create_habit(client, user_a_headers, "Journal Range Err")

    resp = _get_journal(
        client,
        user_a_headers,
        habit["id"],
        date_fmt=DATE_FMT,
        date_start="31-12-2025",
        date_end="01-01-2025",
    )
    assert resp.status_code == 400


async def test_journal_invalid_sort(client: TestClient, user_a_headers):
    """An invalid sort value returns 400."""
    habit = _create_habit(client, user_a_headers, "Journal Bad Sort")

    resp = _get_journal(
        client,
        user_a_headers,
        habit["id"],
        sort="random",
    )
    assert resp.status_code == 400


async def test_journal_negative_offset(client: TestClient, user_a_headers):
    """A negative offset returns 400."""
    habit = _create_habit(client, user_a_headers, "Journal Neg Offset")

    resp = _get_journal(
        client,
        user_a_headers,
        habit["id"],
        offset=-1,
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 9. Nonexistent habit → 404
# ---------------------------------------------------------------------------


async def test_journal_nonexistent_habit(client: TestClient, user_a_headers):
    """Accessing a journal for a habit that doesn't exist returns 404."""
    resp = _get_journal(
        client,
        user_a_headers,
        "does_not_exist",
        date_fmt=DATE_FMT,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 10. Empty journal
# ---------------------------------------------------------------------------


async def test_journal_empty(client: TestClient, user_a_headers):
    """A habit with no ticks returns an empty journal."""
    habit = _create_habit(client, user_a_headers, "Journal Empty")

    resp = _get_journal(
        client,
        user_a_headers,
        habit["id"],
        date_fmt=DATE_FMT,
        date_start="01-01-2025",
        date_end="31-12-2025",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["entries"] == []
