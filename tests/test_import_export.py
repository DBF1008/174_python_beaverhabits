"""Regression tests for the export -> import -> merge -> backup data round-trip.

These guard the data-integrity contract: notes, history, tags, archived status,
period/frequency and ordering must survive a full export and re-import, conflict
merges must not duplicate or scramble habits, legacy/old-format files must still
import, and a Telegram backup must restore the whole workspace.

They operate directly on the storage layer (no UI / DB), so they are fast and
deterministic.
"""

import json
from datetime import date

from beaverhabits.core.backup import backup_to_telegram
from beaverhabits.frontend.import_page import import_from_csv, import_from_json
from beaverhabits.storage.dict import DictHabit, DictHabitList
from beaverhabits.storage.storage import HabitFrequency, HabitOrder, HabitStatus
from beaverhabits.views import dump_habit_list


def _empty_list() -> DictHabitList:
    return DictHabitList({"habits": []})


def _records(habit: DictHabit) -> dict:
    return {r.day: r for r in habit.records}


def _rich_list() -> DictHabitList:
    """A workspace exercising every metadata field that used to be lost."""
    return DictHabitList(
        {
            "habits": [
                {
                    "id": "aaaaaa",
                    "name": "Running",
                    "tags": ["health"],
                    "star": True,
                    "chips": ["c1"],
                    "status": "active",
                    "period": {
                        "period_type": "W",
                        "period_count": 1,
                        "target_count": 3,
                    },
                    "records": [
                        {"day": "2024-01-01", "done": True, "text": "5km"},
                        # Not done, but carries a note -> must survive.
                        {"day": "2024-01-02", "done": False, "text": "rest note"},
                        {"day": "2024-01-03", "done": True},
                    ],
                },
                {
                    "id": "bbbbbb",
                    "name": "Reading",
                    "status": "archive",
                    "records": [{"day": "2024-02-01", "done": True, "text": "ch1"}],
                },
                {
                    "id": "cccccc",
                    "name": "Deleted",
                    "status": "soft_delete",
                    "records": [{"day": "2024-03-01", "done": True}],
                },
            ],
            "order": ["bbbbbb", "aaaaaa"],
            "order_by": HabitOrder.NAME.value,
        }
    )


# ---------------------------------------------------------------------------
# Full export -> import round-trip
# ---------------------------------------------------------------------------


def test_dump_keeps_archived_and_order_drops_soft_deleted():
    dumped = dump_habit_list(_rich_list())

    ids = {h["id"] for h in dumped["habits"]}
    assert ids == {"aaaaaa", "bbbbbb"}  # archived kept, soft-deleted dropped
    assert dumped["order"] == ["bbbbbb", "aaaaaa"]
    assert dumped["order_by"] == HabitOrder.NAME.value


def test_dump_excludes_backup_secrets():
    hl = _rich_list()
    hl.data["backup"] = {"telegram_bot_token": "secret", "telegram_chat_id": "chat"}

    dumped = dump_habit_list(hl)

    assert "backup" not in dumped
    assert "secret" not in json.dumps(dumped)


async def test_full_export_import_round_trip_preserves_everything():
    # Export (with the real download envelope keys) then re-import into a clean
    # workspace and assert nothing was silently dropped or scrambled.
    dumped = dump_habit_list(_rich_list())
    payload = json.dumps({"user_email": "x", "exported_at": "y", **dumped})

    other = await import_from_json(payload)
    target = _empty_list()
    await target.merge(other)

    run = await target.get_habit_by("aaaaaa")
    assert run is not None
    assert run.name == "Running"
    assert run.tags == ["health"]
    assert run.star is True
    assert run.chips == ["c1"]
    assert run.status == HabitStatus.ACTIVE
    assert run.period == HabitFrequency("W", 1, 3)

    recs = _records(run)
    assert recs[date(2024, 1, 1)].done and recs[date(2024, 1, 1)].text == "5km"
    # The not-done-but-noted day and its note survive.
    assert not recs[date(2024, 1, 2)].done
    assert recs[date(2024, 1, 2)].text == "rest note"
    assert recs[date(2024, 1, 3)].done and recs[date(2024, 1, 3)].text == ""

    read = await target.get_habit_by("bbbbbb")
    assert read is not None
    assert read.status == HabitStatus.ARCHIVED
    assert _records(read)[date(2024, 2, 1)].text == "ch1"

    assert target.order == ["bbbbbb", "aaaaaa"]
    assert target.order_by == HabitOrder.NAME


async def test_import_onto_same_workspace_is_idempotent():
    # Re-importing an export onto the very same workspace must not change it.
    hl = _rich_list()
    before = json.loads(json.dumps(hl.data))

    other = await import_from_json(json.dumps(dump_habit_list(hl)))
    await hl.merge(other)

    run = await hl.get_habit_by("aaaaaa")
    assert run is not None
    assert len(run.records) == 3  # no duplicated records
    assert run.tags == ["health"]
    # Active + archived habit counts unchanged (soft-deleted still present once).
    assert len(hl.data["habits"]) == len(before["habits"])


# ---------------------------------------------------------------------------
# Record-level merge (DictHabit.merge)
# ---------------------------------------------------------------------------


async def test_habit_merge_preserves_notes_and_unions_history():
    parent = _empty_list()
    a = DictHabit(
        {
            "id": "x",
            "name": "A",
            "records": [
                {"day": "2024-03-01", "done": True, "text": "first"},
                {"day": "2024-03-02", "done": False, "text": "note only"},
                {"day": "2024-03-04", "done": False},
            ],
        },
        parent,
    )
    b = DictHabit(
        {
            "id": "x",
            "name": "A",
            "records": [
                {"day": "2024-03-01", "done": True},  # same day, keep A's note
                {"day": "2024-03-02", "done": True},  # upgrade to done, keep note
                {"day": "2024-03-03", "done": True, "text": "third"},  # new
            ],
        },
        parent,
    )

    await a.merge(b)
    recs = _records(a)

    assert sorted(recs) == [
        date(2024, 3, 1),
        date(2024, 3, 2),
        date(2024, 3, 3),
        date(2024, 3, 4),
    ]
    assert recs[date(2024, 3, 1)].text == "first"  # existing note not overwritten
    assert recs[date(2024, 3, 2)].done is True  # done OR-ed
    assert recs[date(2024, 3, 2)].text == "note only"  # note retained
    assert recs[date(2024, 3, 3)].text == "third"  # imported note added
    assert recs[date(2024, 3, 4)].done is False  # not-done day still kept


async def test_habit_merge_fills_missing_metadata_without_clobbering():
    parent = _empty_list()
    a = DictHabit({"id": "x", "name": "A", "tags": ["keep"], "records": []}, parent)
    b = DictHabit(
        {
            "id": "x",
            "name": "A",
            "tags": ["other"],
            "star": True,
            "chips": ["c"],
            "period": {"period_type": "D", "period_count": 1, "target_count": 1},
            "records": [],
        },
        parent,
    )

    await a.merge(b)

    assert a.tags == ["keep"]  # existing non-empty value wins
    assert a.star is True  # filled in from other
    assert a.chips == ["c"]
    assert a.period == HabitFrequency("D", 1, 1)


# ---------------------------------------------------------------------------
# Conflict merge (DictHabitList.merge)
# ---------------------------------------------------------------------------


async def test_list_merge_conflicts_no_duplicate_and_rename_only_on_collision():
    target = DictHabitList(
        {
            "habits": [
                {
                    "id": "a1",
                    "name": "Run",
                    "status": "active",
                    "records": [{"day": "2024-01-01", "done": True, "text": "old"}],
                },
                {
                    "id": "b1",
                    "name": "Read",
                    "status": "archive",
                    "records": [{"day": "2024-01-01", "done": True}],
                },
            ]
        }
    )
    other = DictHabitList(
        {
            "habits": [
                {
                    "id": "a1",
                    "name": "Run",
                    "records": [{"day": "2024-01-02", "done": True, "text": "new"}],
                },
                # Same name as a1 but different id -> added & disambiguated.
                {"id": "c1", "name": "Run", "records": []},
                # Unique name -> added without rename.
                {"id": "d1", "name": "Cook", "records": []},
                # Matches the archived habit by id -> merged in place.
                {
                    "id": "b1",
                    "name": "Read",
                    "records": [{"day": "2024-01-05", "done": True, "text": "ch5"}],
                },
            ],
            "order": ["c1", "d1"],
        }
    )

    await target.merge(other)
    by_id = {h.id: h for h in target.habits}

    # No duplicates: the archived habit was merged, not re-added.
    assert set(by_id) == {"a1", "b1", "c1", "d1"}

    # Conflicting history is unioned with notes intact.
    a = _records(by_id["a1"])
    assert a[date(2024, 1, 1)].text == "old"
    assert a[date(2024, 1, 2)].text == "new"

    # Archived habit stays archived and gains the imported record.
    assert by_id["b1"].status == HabitStatus.ARCHIVED
    assert _records(by_id["b1"])[date(2024, 1, 5)].text == "ch5"

    # Rename only happens on a genuine name collision.
    assert by_id["c1"].name == "Run (imported)"
    assert by_id["d1"].name == "Cook"
    assert by_id["a1"].name == "Run"

    # New habits are reflected in the ordering.
    assert set(target.order) == {"a1", "b1", "c1", "d1"}


# ---------------------------------------------------------------------------
# Legacy / old-format compatibility
# ---------------------------------------------------------------------------


async def test_legacy_minimal_json_imports_without_crash():
    # Old export: no id/tags/status/period/chips, records without `text`,
    # no list-level order/order_by.
    legacy = json.dumps(
        {
            "habits": [
                {
                    "name": "Walk",
                    "records": [
                        {"day": "2023-01-01", "done": True},
                        {"day": "2023-01-02", "done": False},
                    ],
                }
            ]
        }
    )

    other = await import_from_json(legacy)
    target = _empty_list()
    await target.merge(other)

    habits = target.habits
    assert len(habits) == 1
    h = habits[0]
    assert h.name == "Walk"
    assert h.tags == [] and h.chips == [] and h.period is None
    assert h.star is False
    assert h.status == HabitStatus.ACTIVE

    recs = _records(h)
    assert recs[date(2023, 1, 1)].done is True
    assert recs[date(2023, 1, 2)].done is False  # not-done day preserved


async def test_legacy_csv_imports_and_merges():
    csv_text = "Date,Walk,Run\n2023-01-01,1,0\n2023-01-02,2,-1\n"

    other = await import_from_csv(csv_text)
    target = _empty_list()
    await target.merge(other)

    by_name = {h.name: h for h in target.habits}
    assert set(by_name) == {"Walk", "Run"}

    walk = _records(by_name["Walk"])
    assert walk[date(2023, 1, 1)].done is True
    assert walk[date(2023, 1, 2)].done is True  # value 2 > 0

    run = _records(by_name["Run"])
    assert run[date(2023, 1, 1)].done is False  # 0
    assert run[date(2023, 1, 2)].done is False  # -1


async def test_legacy_invalid_period_and_status_do_not_crash_import():
    # Garbage period / status values from a corrupted or future file must
    # degrade gracefully rather than break the whole import.
    legacy = json.dumps(
        {
            "habits": [
                {
                    "id": "z1",
                    "name": "Zen",
                    "status": "totally-unknown",
                    "period": {"oops": True},
                    "records": [{"day": "2023-05-01", "done": True}],
                }
            ]
        }
    )

    other = await import_from_json(legacy)
    target = _empty_list()
    await target.merge(other)

    h = await target.get_habit_by("z1")
    assert h is not None
    assert h.status == HabitStatus.ACTIVE  # falls back to active
    assert h.period is None  # invalid period ignored
    assert _records(h)[date(2023, 5, 1)].done is True


# ---------------------------------------------------------------------------
# Telegram backup -> restore
# ---------------------------------------------------------------------------


async def test_telegram_backup_payload_restores_full_workspace(monkeypatch):
    hl = DictHabitList(
        {
            "habits": [
                {
                    "id": "a1",
                    "name": "Run",
                    "status": "active",
                    "records": [{"day": "2024-01-01", "done": True, "text": "5k"}],
                },
                {
                    "id": "b1",
                    "name": "Read",
                    "status": "archive",
                    "records": [{"day": "2024-01-02", "done": True}],
                },
            ],
            "order": ["b1", "a1"],
            "order_by": HabitOrder.NAME.value,
            "backup": {"telegram_bot_token": "tok", "telegram_chat_id": "chat"},
        }
    )

    captured: dict = {}

    def fake_send(file, chat_id, token):
        captured["payload"] = file.getvalue()
        captured["chat_id"] = chat_id
        captured["token"] = token

    monkeypatch.setattr("beaverhabits.core.backup.send_json_file", fake_send)

    backup_to_telegram("tok", "chat", hl)
    assert captured["token"] == "tok"
    assert captured["chat_id"] == "chat"

    # Restore the captured backup onto a fresh workspace.
    other = await import_from_json(captured["payload"].decode())
    target = _empty_list()
    await target.merge(other)

    run = await target.get_habit_by("a1")
    assert run is not None and _records(run)[date(2024, 1, 1)].text == "5k"

    read = await target.get_habit_by("b1")
    assert read is not None and read.status == HabitStatus.ARCHIVED

    assert target.order == ["b1", "a1"]
    assert target.order_by == HabitOrder.NAME
    assert target.backup.telegram_bot_token == "tok"
    assert target.backup.telegram_chat_id == "chat"
