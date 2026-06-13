"""Regression tests for the structured user-preferences read/write layer.

Covers the four required scenarios — default values, persistence updates,
cross-session recovery, and invalid input — at three levels:

* pure model / validation (``UserPreferences`` / ``UserPreferencesUpdate``),
* DB-backed core functions (``get_user_preferences`` / ``update_user_preferences``),
* the REST API (``GET`` / ``PUT /api/v1/user/preferences``) used by external clients.

The DB/API tests mirror ``tests/test_api_tokens.py``: a module-scoped ``TestClient``
boots the app (creating the database), a user is registered + logged in through it,
and the core functions are then awaited directly with that user.
"""

import os
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from nicegui.testing.general import prepare_simulation
from pydantic import ValidationError

from beaverhabits.app import preferences
from beaverhabits.app.crud import get_user_configs
from beaverhabits.app.db import User, engine
from beaverhabits.app.preferences import UserPreferences, UserPreferencesUpdate
from beaverhabits.configs import USER_DATA_FOLDER, settings
from beaverhabits.main import app

PASSWORD = "TestPassword123!"
PREFS_URL = "/api/v1/user/preferences"


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(name="client", scope="module")
async def client_fixture():
    # The sqlite DB lives under the (gitignored) user-data folder; ensure it exists.
    os.makedirs(USER_DATA_FOLDER, exist_ok=True)
    # Re-arm nicegui's run config: a prior nicegui `user`-fixture test (test_gui /
    # test_storage) resets core.app via nicegui_reset_globals(), which otherwise makes
    # the real TestClient lifespan fail with "You must call ui.run()". Keeps this suite
    # order-independent.
    prepare_simulation()
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client
    await engine.dispose()


@pytest.fixture
async def pref_user(client: TestClient):
    """Register + log in a fresh user; return the User and bearer headers."""
    email = f"prefs_{datetime.now().timestamp()}@test.com"
    resp = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 201
    user = User(**resp.json())

    resp = client.post(
        "/auth/login",
        data={"grant_type": "password", "username": email, "password": PASSWORD},
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]

    yield {"user": user, "headers": {"Authorization": f"Bearer {token}"}}


# ============================================================================
# Pure model / validation — defaults & invalid input (no DB)
# ============================================================================


def test_default_preferences_match_settings():
    prefs = preferences.default_preferences()
    assert prefs.completion_status_list == settings.DEFAULT_COMPLETION_STATUS_LIST
    assert prefs.completion_status_mapping == {}
    assert prefs.timezone is None
    assert prefs.custom_css is None


def test_default_preferences_list_is_a_copy():
    """Mutating the returned default list must not corrupt the global setting."""
    prefs = preferences.default_preferences()
    prefs.completion_status_list.append("mutated")
    assert "mutated" not in settings.DEFAULT_COMPLETION_STATUS_LIST


def test_from_config_data_empty_returns_defaults():
    assert preferences.preferences_from_config_data(None).completion_status_list == [
        "yes",
        "no",
    ]
    assert preferences.preferences_from_config_data({}).timezone is None


def test_from_config_data_overlays_stored_keys():
    eff = preferences.preferences_from_config_data(
        {
            "default_chips": ["a", "b"],
            "default_chips_mapping": {"a": "#red"},
            "timezone": "Asia/Tokyo",
            "css": "body{color:red}",
        }
    )
    assert eff.completion_status_list == ["a", "b"]
    assert eff.completion_status_mapping == {"a": "#red"}
    assert eff.timezone == "Asia/Tokyo"
    assert eff.custom_css == "body{color:red}"


def test_update_to_config_data_maps_only_set_fields():
    update = UserPreferencesUpdate(custom_css="body{}")
    assert preferences.update_to_config_data(update) == {"css": "body{}"}

    empty = UserPreferencesUpdate()
    assert preferences.update_to_config_data(empty) == {}


def test_completion_status_list_is_normalized():
    update = UserPreferencesUpdate(
        completion_status_list=[" yes ", "no", "yes", "  ", "skip"]
    )
    assert update.completion_status_list == ["yes", "no", "skip"]


def test_custom_css_is_sanitized():
    update = UserPreferencesUpdate(custom_css="body{}</style><script>evil()</script>")
    assert "<" not in update.custom_css and ">" not in update.custom_css
    assert "evil()" in update.custom_css  # text kept, tags stripped


def test_blank_timezone_becomes_none():
    assert UserPreferencesUpdate(timezone="   ").timezone is None
    # ...and is still recorded as an explicit (no-op) reset, not dropped.
    assert preferences.update_to_config_data(
        UserPreferencesUpdate(timezone="")
    ) == {"timezone": None}


@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"timezone": "Not/AZone"},
        {"timezone": "America/Nowhere"},
        {"completion_status_list": []},
        {"completion_status_list": ["  ", ""]},
        {"completion_status_mapping": {"": "x"}},
        {"completion_status_mapping": {"k": ""}},
        {"completion_status_mapping": {"k": 5}},
    ],
)
def test_invalid_input_rejected(bad_kwargs):
    with pytest.raises(ValidationError):
        UserPreferencesUpdate(**bad_kwargs)


# ============================================================================
# DB-backed core functions
# ============================================================================


async def test_db_defaults_for_new_user(pref_user):
    user = pref_user["user"]
    # Nothing persisted yet.
    assert await get_user_configs(user) is None

    prefs = await preferences.get_user_preferences(user)
    assert prefs.completion_status_list == ["yes", "no"]
    assert prefs.completion_status_mapping == {}
    assert prefs.timezone is None
    assert prefs.custom_css is None


async def test_db_persist_and_read_back(pref_user):
    user = pref_user["user"]
    await preferences.update_user_preferences(
        user,
        UserPreferencesUpdate(
            completion_status_list=["done", "skip"],
            completion_status_mapping={"skip": "#amber #skip"},
            timezone="Europe/London",
            custom_css="body{margin:0}",
        ),
    )

    prefs = await preferences.get_user_preferences(user)
    assert prefs.completion_status_list == ["done", "skip"]
    assert prefs.completion_status_mapping == {"skip": "#amber #skip"}
    assert prefs.timezone == "Europe/London"
    assert prefs.custom_css == "body{margin:0}"

    # Stored under the stable config_data keys (backward compatible).
    stored = await get_user_configs(user)
    assert stored["default_chips"] == ["done", "skip"]
    assert stored["default_chips_mapping"] == {"skip": "#amber #skip"}
    assert stored["timezone"] == "Europe/London"
    assert stored["css"] == "body{margin:0}"


async def test_db_partial_update_preserves_other_fields(pref_user):
    user = pref_user["user"]
    await preferences.update_user_preferences(
        user, UserPreferencesUpdate(completion_status_list=["a", "b"])
    )
    # A second, disjoint update must not wipe the first.
    await preferences.update_user_preferences(
        user, UserPreferencesUpdate(timezone="Asia/Tokyo")
    )

    prefs = await preferences.get_user_preferences(user)
    assert prefs.completion_status_list == ["a", "b"]
    assert prefs.timezone == "Asia/Tokyo"


async def test_db_cross_session_recovery(pref_user):
    """Persisted prefs (esp. timezone) survive being read back in a fresh session.

    Each crud call opens and closes its own DB session, so re-reading genuinely
    round-trips through the database rather than a cached session object.
    """
    user = pref_user["user"]
    await preferences.update_user_preferences(
        user,
        UserPreferencesUpdate(timezone="America/New_York", custom_css="body{}"),
    )

    # Fresh read via the high-level function...
    recovered = await preferences.get_user_preferences(user)
    assert recovered.timezone == "America/New_York"
    assert recovered.custom_css == "body{}"

    # ...and rebuilding straight from the raw stored row.
    rebuilt = preferences.preferences_from_config_data(await get_user_configs(user))
    assert rebuilt.timezone == "America/New_York"


async def test_db_invalid_update_does_not_persist(pref_user):
    user = pref_user["user"]
    baseline = await preferences.get_user_preferences(user)

    with pytest.raises(ValidationError):
        # Fails at construction, before any DB write happens.
        await preferences.update_user_preferences(
            user, UserPreferencesUpdate(timezone="Mars/Phobos")
        )

    after = await preferences.get_user_preferences(user)
    assert after == baseline
    assert await get_user_configs(user) is None


# ============================================================================
# REST API — external-client visibility
# ============================================================================


async def test_api_get_requires_auth(client: TestClient):
    assert client.get(PREFS_URL).status_code == 401


async def test_api_get_defaults(pref_user, client: TestClient):
    resp = client.get(PREFS_URL, headers=pref_user["headers"])
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "completion_status_list": ["yes", "no"],
        "completion_status_mapping": {},
        "timezone": None,
        "custom_css": None,
    }


async def test_api_put_persists(pref_user, client: TestClient):
    headers = pref_user["headers"]
    resp = client.put(
        PREFS_URL,
        json={
            "completion_status_list": ["yes", "no", "skip"],
            "completion_status_mapping": {"skip": "#amber"},
            "timezone": "Europe/Berlin",
        },
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["timezone"] == "Europe/Berlin"

    # A subsequent request reflects the persisted values.
    body = client.get(PREFS_URL, headers=headers).json()
    assert body["completion_status_list"] == ["yes", "no", "skip"]
    assert body["completion_status_mapping"] == {"skip": "#amber"}
    assert body["timezone"] == "Europe/Berlin"


async def test_api_put_partial_only_changes_provided(pref_user, client: TestClient):
    headers = pref_user["headers"]
    resp = client.put(PREFS_URL, json={"custom_css": "body{}"}, headers=headers)
    assert resp.status_code == 200

    body = client.get(PREFS_URL, headers=headers).json()
    assert body["custom_css"] == "body{}"
    # Untouched field keeps its default.
    assert body["completion_status_list"] == ["yes", "no"]


async def test_api_cross_session_recovery(pref_user, client: TestClient):
    """A value set in one request is visible in a brand-new (separate) request."""
    headers = pref_user["headers"]
    client.put(
        PREFS_URL,
        json={"timezone": "Asia/Shanghai", "custom_css": "body{color:#000}"},
        headers=headers,
    )

    body = client.get(PREFS_URL, headers=headers).json()
    assert body["timezone"] == "Asia/Shanghai"
    assert body["custom_css"] == "body{color:#000}"


async def test_api_put_normalizes_and_sanitizes(pref_user, client: TestClient):
    headers = pref_user["headers"]
    resp = client.put(
        PREFS_URL,
        json={
            "completion_status_list": [" yes ", "yes", "no"],
            "custom_css": "x</style><script>1</script>",
        },
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["completion_status_list"] == ["yes", "no"]
    assert "<" not in body["custom_css"] and ">" not in body["custom_css"]


@pytest.mark.parametrize(
    "bad_body",
    [
        {"timezone": "Not/AZone"},
        {"completion_status_list": []},
        {"completion_status_mapping": {"": "x"}},
    ],
)
async def test_api_put_invalid_input_422(pref_user, client: TestClient, bad_body):
    resp = client.put(PREFS_URL, json=bad_body, headers=pref_user["headers"])
    assert resp.status_code == 422

    # Nothing was persisted by the rejected request.
    body = client.get(PREFS_URL, headers=pref_user["headers"]).json()
    assert body["completion_status_list"] == ["yes", "no"]
    assert body["timezone"] is None
