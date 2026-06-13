"""Structured, validated read/write layer for per-user preferences.

This module is the single source of truth for the *shape*, *defaults*, and
*validation* of user preferences, plus the mapping between preference fields and
the keys persisted inside ``UserConfigsModel.config_data`` (see ``app/db.py``).

It is deliberately **session-agnostic**: it only touches the database (via
``app.crud``) and never reads or writes ``nicegui.app.storage.user``. That keeps
it safe to call from the REST API (which runs without a NiceGUI client session)
and trivially unit-testable. The NiceGUI session glue lives in ``views.py``.

Persisted ``config_data`` keys (kept stable for backward compatibility with the
data already stored and with the existing session readers in ``views.py``):

    completion_status_list    -> "default_chips"
    completion_status_mapping -> "default_chips_mapping"
    timezone                  -> "timezone"
    custom_css                -> "css"
"""

import re

import pytz
from pydantic import BaseModel, field_validator

from beaverhabits.app import crud
from beaverhabits.app.db import User
from beaverhabits.configs import settings

# Preference field name -> key stored inside UserConfigsModel.config_data.
_CONFIG_KEYS = {
    "completion_status_list": "default_chips",
    "completion_status_mapping": "default_chips_mapping",
    "timezone": "timezone",
    "custom_css": "css",
}


def sanitize_css(css: str) -> str:
    """Strip any HTML tags from user-supplied CSS to prevent XSS via </style> injection."""
    return re.sub(r"<[^>]*>", "", css)


def _normalize_status_list(value):
    """Strip/dedupe completion-status entries; reject an empty result."""
    if not isinstance(value, (list, tuple)):
        raise ValueError("completion_status_list must be a list of strings")

    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError("completion status entries must be strings")
        stripped = item.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        normalized.append(stripped)

    if not normalized:
        raise ValueError("completion_status_list must contain at least one status")
    return normalized


def _normalize_mapping(value):
    """Validate that mapping keys and values are non-empty strings."""
    if not isinstance(value, dict):
        raise ValueError("completion_status_mapping must be a mapping")

    result: dict[str, str] = {}
    for key, mapped in value.items():
        if not isinstance(key, str) or not isinstance(mapped, str):
            raise ValueError("completion_status_mapping keys and values must be strings")
        clean_key, clean_value = key.strip(), mapped.strip()
        if not clean_key or not clean_value:
            raise ValueError(
                "completion_status_mapping keys and values must be non-empty"
            )
        result[clean_key] = clean_value
    return result


def _normalize_timezone(value):
    """Empty -> None; otherwise require a valid IANA timezone name."""
    if not isinstance(value, str):
        raise ValueError("timezone must be a string")
    stripped = value.strip()
    if not stripped:
        return None
    if stripped not in pytz.all_timezones_set:
        raise ValueError(
            f"Invalid timezone '{stripped}'. Must be a valid IANA timezone name "
            f"(e.g. 'America/New_York', 'Europe/London', 'Asia/Tokyo')."
        )
    return stripped


class UserPreferences(BaseModel):
    """Effective user preferences (defaults already applied).

    Used as the REST API GET response model. Reads are lenient: values stored in
    ``config_data`` were validated on write, so no custom validation runs here.
    """

    completion_status_list: list[str]
    completion_status_mapping: dict[str, str]
    timezone: str | None = None
    custom_css: str | None = None


class UserPreferencesUpdate(BaseModel):
    """Partial preference update / REST API PUT body.

    Every field is optional; only fields explicitly provided are persisted. Each
    provided field is normalized and validated, so constructing this model with
    bad input raises ``pydantic.ValidationError`` (surfaced as HTTP 422 by the
    API, or caught by the UI).
    """

    completion_status_list: list[str] | None = None
    completion_status_mapping: dict[str, str] | None = None
    timezone: str | None = None
    custom_css: str | None = None

    @field_validator("completion_status_list", mode="before")
    @classmethod
    def _validate_completion_status_list(cls, value):
        if value is None:
            return None
        return _normalize_status_list(value)

    @field_validator("completion_status_mapping", mode="before")
    @classmethod
    def _validate_completion_status_mapping(cls, value):
        if value is None:
            return None
        return _normalize_mapping(value)

    @field_validator("timezone", mode="before")
    @classmethod
    def _validate_timezone(cls, value):
        if value is None:
            return None
        return _normalize_timezone(value)

    @field_validator("custom_css", mode="before")
    @classmethod
    def _validate_custom_css(cls, value):
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("custom_css must be a string")
        return sanitize_css(value)


def default_preferences() -> UserPreferences:
    """Effective preferences for a user who has not set anything yet."""
    return UserPreferences(
        completion_status_list=list(settings.DEFAULT_COMPLETION_STATUS_LIST),
        completion_status_mapping={},
        timezone=None,
        custom_css=None,
    )


def preferences_from_config_data(data: dict | None) -> UserPreferences:
    """Build effective preferences from a stored ``config_data`` dict.

    Starts from the defaults and overlays any stored key that is present and
    non-null, so missing or unset preferences fall back to the current defaults
    at read time (rather than freezing a stale default into each user's record).
    """
    data = data or {}
    values = default_preferences().model_dump()

    for field, key in _CONFIG_KEYS.items():
        stored = data.get(key)
        if stored is not None:
            values[field] = stored

    return UserPreferences(**values)


def update_to_config_data(update: UserPreferencesUpdate) -> dict:
    """Map the explicitly-set fields of an update to ``config_data`` keys."""
    set_fields = update.model_dump(exclude_unset=True)
    return {_CONFIG_KEYS[field]: value for field, value in set_fields.items()}


async def get_user_preferences(user: User) -> UserPreferences:
    """Read a user's effective preferences from the database."""
    data = await crud.get_user_configs(user)
    return preferences_from_config_data(data)


async def update_user_preferences(
    user: User, update: UserPreferencesUpdate
) -> UserPreferences:
    """Persist a partial preference update and return the effective preferences.

    Validation has already happened when ``update`` was constructed.
    """
    patch = update_to_config_data(update)
    if patch:
        await crud.update_user_configs(user, patch)
    return await get_user_preferences(user)
