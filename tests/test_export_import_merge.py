"""
Regression tests for the export / import / merge pipeline.

Covers:
  - Full export→import roundtrip (all fields preserved)
  - Record merge (text, non-done records, done union)
  - Metadata merge (tags, star, period, chips, status, name)
  - New-habit naming (no "(imported)" suffix)
  - Import envelope parsing (new + legacy formats)
  - Legacy format compatibility
"""

import copy
import json

import pytest

from beaverhabits.storage.dict import (
    DAY_MASK,
    DictHabit,
    DictHabitList,
    SCHEMA_VERSION,
)
from beaverhabits.storage.storage import (
    HabitFrequency,
    HabitOrder,
    HabitStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _habit_list(data: dict) -> DictHabitList:
    """Wrap a raw dict into a DictHabitList."""
    return DictHabitList(data)


def _make_habit(
    name: str,
    *,
    habit_id: str = "abc123",
    tags: list[str] | None = None,
    star: bool = False,
    status: HabitStatus = HabitStatus.ACTIVE,
    period: HabitFrequency | None = None,
    chips: list[str] | None = None,
    records: list[dict] | None = None,
) -> dict:
    """Build a raw habit dict."""
    h: dict = {
        "id": habit_id,
        "name": name,
        "tags": tags or [],
        "star": star,
        "status": status.value,
        "records": records or [],
    }
    if period:
        h["period"] = period.to_dict()
    if chips:
        h["chips"] = chips
    return h


def _make_record(day: str, done: bool = True, text: str = "") -> dict:
    r: dict = {"day": day, "done": done}
    if text:
        r["text"] = text
    return r


def _full_habit_list_data() -> dict:
    """Build a complete habit list data dict with rich content."""
    return {
        "habits": [
            _make_habit(
                "Running",
                habit_id="run001",
                tags=["fitness", "outdoor"],
                star=True,
                period=HabitFrequency("W", 1, 3),
                chips=["yes", "no", "skip"],
                records=[
                    _make_record("2024-01-15", True, "Ran 5km"),
                    _make_record("2024-01-16", False, "Rest day"),
                    _make_record("2024-01-17", True, "Ran 3km #skip"),
                ],
            ),
            _make_habit(
                "Reading",
                habit_id="read01",
                tags=["learning"],
                star=False,
                status=HabitStatus.ARCHIVED,
                period=HabitFrequency("D", 1, 1),
                records=[
                    _make_record("2024-02-01", True, "Read 30 pages"),
                    _make_record("2024-02-02", True),
                ],
            ),
        ],
        "order": ["run001", "read01"],
        "order_by": "MANUALLY",
    }


# ---------------------------------------------------------------------------
# TestRecordMerge — Bug 3: merge preserves text and non-done records
# ---------------------------------------------------------------------------

class TestRecordMerge:

    async def test_text_preserved_from_both_sides(self):
        """Records with text from both sides should be preserved."""
        self_list = _habit_list({
            "habits": [
                _make_habit(
                    "Habit",
                    habit_id="h1",
                    records=[
                        _make_record("2024-01-01", True, "self note"),
                        _make_record("2024-01-03", True, "only self"),
                    ],
                )
            ]
        })
        other_list = _habit_list({
            "habits": [
                _make_habit(
                    "Habit",
                    habit_id="h1",
                    records=[
                        _make_record("2024-01-01", True, "other note"),
                        _make_record("2024-01-02", True, "only other"),
                    ],
                )
            ]
        })

        await self_list.merge(other_list)
        habit = self_list.habits[0]
        records_by_day = {str(r.day): r for r in habit.records}

        # Day 1: both have text — incoming wins (richer data)
        assert records_by_day["2024-01-01"].text == "other note"
        assert records_by_day["2024-01-01"].done is True

        # Day 2: only other has it — should be present
        assert "2024-01-02" in records_by_day
        assert records_by_day["2024-01-02"].text == "only other"

        # Day 3: only self has it — should be present
        assert "2024-01-03" in records_by_day
        assert records_by_day["2024-01-03"].text == "only self"

    async def test_non_done_records_preserved(self):
        """Non-done records with text should survive merge."""
        self_list = _habit_list({
            "habits": [
                _make_habit(
                    "Habit",
                    habit_id="h1",
                    records=[
                        _make_record("2024-01-01", False, "sick today"),
                    ],
                )
            ]
        })
        other_list = _habit_list({
            "habits": [
                _make_habit(
                    "Habit",
                    habit_id="h1",
                    records=[
                        _make_record("2024-01-02", True, "did it"),
                    ],
                )
            ]
        })

        await self_list.merge(other_list)
        habit = self_list.habits[0]
        records_by_day = {str(r.day): r for r in habit.records}

        # Non-done record from self should be preserved
        assert "2024-01-01" in records_by_day
        assert records_by_day["2024-01-01"].text == "sick today"
        assert records_by_day["2024-01-01"].done is False

        # Done record from other should be present
        assert "2024-01-02" in records_by_day
        assert records_by_day["2024-01-02"].text == "did it"
        assert records_by_day["2024-01-02"].done is True

    async def test_done_union_promotes_to_true(self):
        """If either side has done=True, the merged result should be done=True."""
        self_list = _habit_list({
            "habits": [
                _make_habit(
                    "Habit",
                    habit_id="h1",
                    records=[
                        _make_record("2024-01-01", False),
                    ],
                )
            ]
        })
        other_list = _habit_list({
            "habits": [
                _make_habit(
                    "Habit",
                    habit_id="h1",
                    records=[
                        _make_record("2024-01-01", True),
                    ],
                )
            ]
        })

        await self_list.merge(other_list)
        habit = self_list.habits[0]
        record = habit.records[0]

        assert record.done is True

    async def test_empty_records_preserved(self):
        """Merging with empty records should not destroy existing records."""
        self_list = _habit_list({
            "habits": [
                _make_habit(
                    "Habit",
                    habit_id="h1",
                    records=[
                        _make_record("2024-01-01", True, "important note"),
                    ],
                )
            ]
        })
        other_list = _habit_list({
            "habits": [
                _make_habit("Habit", habit_id="h1", records=[])
            ]
        })

        await self_list.merge(other_list)
        habit = self_list.habits[0]

        assert len(habit.records) == 1
        assert habit.records[0].text == "important note"


# ---------------------------------------------------------------------------
# TestMetadataMerge — Bug 4: merge updates all metadata from imported side
# ---------------------------------------------------------------------------

class TestMetadataMerge:

    async def test_tags_updated(self):
        self_list = _habit_list({
            "habits": [_make_habit("H", habit_id="h1", tags=["old"])]
        })
        other_list = _habit_list({
            "habits": [_make_habit("H", habit_id="h1", tags=["new", "extra"])]
        })

        await self_list.merge(other_list)
        assert self_list.habits[0].tags == ["new", "extra"]

    async def test_star_updated(self):
        self_list = _habit_list({
            "habits": [_make_habit("H", habit_id="h1", star=False)]
        })
        other_list = _habit_list({
            "habits": [_make_habit("H", habit_id="h1", star=True)]
        })

        await self_list.merge(other_list)
        assert self_list.habits[0].star is True

    async def test_period_updated(self):
        self_list = _habit_list({
            "habits": [_make_habit("H", habit_id="h1")]
        })
        other_list = _habit_list({
            "habits": [
                _make_habit(
                    "H",
                    habit_id="h1",
                    period=HabitFrequency("W", 2, 5),
                )
            ]
        })

        await self_list.merge(other_list)
        period = self_list.habits[0].period
        assert period is not None
        assert period.period_type == "W"
        assert period.period_count == 2
        assert period.target_count == 5

    async def test_chips_updated(self):
        self_list = _habit_list({
            "habits": [_make_habit("H", habit_id="h1", chips=["yes"])]
        })
        other_list = _habit_list({
            "habits": [_make_habit("H", habit_id="h1", chips=["yes", "no", "skip"])]
        })

        await self_list.merge(other_list)
        assert self_list.habits[0].chips == ["yes", "no", "skip"]

    async def test_status_updated(self):
        self_list = _habit_list({
            "habits": [
                _make_habit("H", habit_id="h1", status=HabitStatus.ACTIVE)
            ]
        })
        other_list = _habit_list({
            "habits": [
                _make_habit("H", habit_id="h1", status=HabitStatus.ARCHIVED)
            ]
        })

        await self_list.merge(other_list)
        assert self_list.habits[0].status == HabitStatus.ARCHIVED

    async def test_name_updated(self):
        self_list = _habit_list({
            "habits": [_make_habit("Old Name", habit_id="h1")]
        })
        other_list = _habit_list({
            "habits": [_make_habit("New Name", habit_id="h1")]
        })

        await self_list.merge(other_list)
        assert self_list.habits[0].name == "New Name"


# ---------------------------------------------------------------------------
# TestNewHabitNaming — Bug 5: no "(imported)" suffix
# ---------------------------------------------------------------------------

class TestNewHabitNaming:

    async def test_new_habit_keeps_original_name(self):
        self_list = _habit_list({"habits": []})
        other_list = _habit_list({
            "habits": [_make_habit("My Habit", habit_id="new1")]
        })

        await self_list.merge(other_list)
        assert self_list.habits[0].name == "My Habit"

    async def test_new_habit_not_renamed_with_suffix(self):
        self_list = _habit_list({
            "habits": [_make_habit("Existing", habit_id="ex1")]
        })
        other_list = _habit_list({
            "habits": [
                _make_habit("Existing", habit_id="ex1"),
                _make_habit("Brand New", habit_id="new1"),
            ]
        })

        await self_list.merge(other_list)
        names = [h.name for h in self_list.habits]
        assert "Brand New" in names
        assert "Brand New (imported)" not in names


# ---------------------------------------------------------------------------
# TestExportFormat — Bugs 1, 2: export includes archived + order/order_by
# ---------------------------------------------------------------------------

class TestExportFormat:

    def test_archived_habits_included_in_export(self):
        """Archived habits should appear in the exported habits list."""
        data = _full_habit_list_data()
        habit_list = _habit_list(data)

        # Simulate export: filter ACTIVE + ARCHIVED
        from beaverhabits.storage.storage import HabitListBuilder
        habits = HabitListBuilder(habit_list).status(
            HabitStatus.ACTIVE, HabitStatus.ARCHIVED
        ).build()

        names = [h.name for h in habits]
        assert "Running" in names
        assert "Reading" in names  # archived

    def test_soft_deleted_habits_excluded(self):
        """Soft-deleted habits should NOT appear in export."""
        data = {
            "habits": [
                _make_habit("Active", habit_id="a1"),
                _make_habit(
                    "Deleted",
                    habit_id="d1",
                    status=HabitStatus.SOLF_DELETED,
                ),
            ]
        }
        habit_list = _habit_list(data)

        from beaverhabits.storage.storage import HabitListBuilder
        habits = HabitListBuilder(habit_list).status(
            HabitStatus.ACTIVE, HabitStatus.ARCHIVED
        ).build()

        names = [h.name for h in habits]
        assert "Active" in names
        assert "Deleted" not in names

    def test_export_envelope_has_order(self):
        """Export envelope should contain order and order_by."""
        data = _full_habit_list_data()
        habit_list = _habit_list(data)

        # Simulate export envelope construction
        export_d = {
            "version": SCHEMA_VERSION,
            "order": habit_list.order,
            "order_by": habit_list.order_by.value,
            "habits": [h.to_dict() for h in habit_list.habits],
        }

        assert export_d["order"] == ["run001", "read01"]
        assert export_d["order_by"] == "MANUALLY"
        assert export_d["version"] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# TestImportEnvelope — Bug 6: import parses envelope fields correctly
# ---------------------------------------------------------------------------

class TestImportEnvelope:

    def test_import_new_envelope_format(self):
        """Import should parse version, order, order_by from envelope."""
        export_json = json.dumps({
            "version": 1,
            "user_email": "user@example.com",
            "exported_at": "2024-01-01 12:00:00",
            "order": ["h1", "h2"],
            "order_by": "MANUALLY",
            "habits": [
                _make_habit("A", habit_id="h1"),
                _make_habit("B", habit_id="h2"),
            ],
        })

        # Simulate import_from_json logic
        parsed = json.loads(export_json)
        order = parsed.pop("order", None)
        order_by = parsed.pop("order_by", None)
        parsed.pop("version", None)
        parsed.pop("user_email", None)
        parsed.pop("exported_at", None)

        habit_list = DictHabitList(parsed)
        if order is not None:
            habit_list.order = order
        if order_by is not None:
            habit_list.order_by = HabitOrder(order_by)

        assert habit_list.order == ["h1", "h2"]
        assert habit_list.order_by == HabitOrder.MANUALLY
        assert len(habit_list.habits) == 2
        # Envelope fields should not leak into data
        assert "user_email" not in habit_list.data
        assert "exported_at" not in habit_list.data
        assert "version" not in habit_list.data

    def test_import_legacy_telegram_backup(self):
        """Legacy Telegram backup (raw dict with order/order_by/backup) should work."""
        backup_json = json.dumps({
            "habits": [
                _make_habit("X", habit_id="x1"),
            ],
            "order": ["x1"],
            "order_by": "MANUALLY",
            "backup": {
                "telegram_bot_token": "bot123",
                "telegram_chat_id": "chat456",
            },
        })

        parsed = json.loads(backup_json)
        order = parsed.pop("order", None)
        order_by = parsed.pop("order_by", None)
        parsed.pop("version", None)
        parsed.pop("user_email", None)
        parsed.pop("exported_at", None)

        habit_list = DictHabitList(parsed)
        if order is not None:
            habit_list.order = order
        if order_by is not None:
            habit_list.order_by = HabitOrder(order_by)

        assert habit_list.order == ["x1"]
        assert habit_list.order_by == HabitOrder.MANUALLY
        assert len(habit_list.habits) == 1

    def test_import_old_format_no_order(self):
        """Old export without order/order_by should still work gracefully."""
        old_json = json.dumps({
            "user_email": "user@example.com",
            "exported_at": "2024-01-01 12:00:00",
            "habits": [
                _make_habit("OldHabit", habit_id="old1"),
            ],
        })

        parsed = json.loads(old_json)
        order = parsed.pop("order", None)
        order_by = parsed.pop("order_by", None)
        parsed.pop("version", None)
        parsed.pop("user_email", None)
        parsed.pop("exported_at", None)

        habit_list = DictHabitList(parsed)
        if order is not None:
            habit_list.order = order
        if order_by is not None:
            habit_list.order_by = HabitOrder(order_by)

        # Should work with defaults
        assert habit_list.order == []
        assert habit_list.order_by == HabitOrder.MANUALLY
        assert len(habit_list.habits) == 1


# ---------------------------------------------------------------------------
# TestLegacyFormat — backward compatibility with old exports
# ---------------------------------------------------------------------------

class TestLegacyFormat:

    async def test_old_export_imports_successfully(self):
        """An export without version/order/order_by should import without error."""
        old_export = {
            "user_email": "old@user.com",
            "exported_at": "2023-06-15 10:30:00",
            "habits": [
                _make_habit(
                    "Legacy Habit",
                    habit_id="leg1",
                    tags=["old"],
                    records=[
                        _make_record("2023-06-01", True, "legacy note"),
                    ],
                )
            ],
        }

        # Simulate import
        parsed = copy.deepcopy(old_export)
        parsed.pop("order", None)
        parsed.pop("order_by", None)
        parsed.pop("version", None)
        parsed.pop("user_email", None)
        parsed.pop("exported_at", None)

        imported = DictHabitList(parsed)
        assert len(imported.habits) == 1
        assert imported.habits[0].name == "Legacy Habit"
        assert imported.habits[0].tags == ["old"]

        # Merge into empty list
        target = DictHabitList({"habits": []})
        await target.merge(imported)

        assert len(target.habits) == 1
        assert target.habits[0].name == "Legacy Habit"
        assert target.habits[0].tags == ["old"]
        assert target.habits[0].records[0].text == "legacy note"

    async def test_old_format_without_id(self):
        """Habits without an 'id' field should get auto-generated IDs."""
        old_data = {
            "habits": [
                {
                    "name": "No ID Habit",
                    "records": [
                        {"day": "2023-01-01", "done": True},
                    ],
                }
            ]
        }

        imported = DictHabitList(old_data)
        habit = imported.habits[0]
        # Should auto-generate an ID
        assert habit.id is not None
        assert len(habit.id) == 6


# ---------------------------------------------------------------------------
# TestFullRoundtrip — export → import → merge preserves everything
# ---------------------------------------------------------------------------

class TestFullRoundtrip:

    async def test_full_roundtrip_preserves_all_fields(self):
        """Export then import should preserve all habit data end-to-end."""
        # 1. Build source data
        original_data = _full_habit_list_data()
        source = _habit_list(copy.deepcopy(original_data))

        # 2. Simulate export (same logic as export_user_habit_list)
        from beaverhabits.storage.storage import HabitListBuilder
        exported_habits = HabitListBuilder(source).status(
            HabitStatus.ACTIVE, HabitStatus.ARCHIVED
        ).build()

        export_envelope = {
            "version": SCHEMA_VERSION,
            "user_email": "test@example.com",
            "exported_at": "2024-06-01 12:00:00",
            "order": source.order,
            "order_by": source.order_by.value,
            "habits": [h.to_dict() for h in exported_habits],
        }
        export_json = json.dumps(export_envelope)

        # 3. Simulate import (same logic as import_from_json)
        parsed = json.loads(export_json)
        order = parsed.pop("order", None)
        order_by = parsed.pop("order_by", None)
        parsed.pop("version", None)
        parsed.pop("user_email", None)
        parsed.pop("exported_at", None)

        imported = DictHabitList(parsed)
        if order is not None:
            imported.order = order
        if order_by is not None:
            imported.order_by = HabitOrder(order_by)

        # 4. Merge into an empty target (simulating fresh restore)
        target = DictHabitList({"habits": []})
        await target.merge(imported)

        # 5. Verify everything survived
        assert target.order == ["run001", "read01"]
        assert target.order_by == HabitOrder.MANUALLY
        assert len(target.habits) == 2

        # Check Running habit
        running = next(h for h in target.habits if h.id == "run001")
        assert running.name == "Running"
        assert running.tags == ["fitness", "outdoor"]
        assert running.star is True
        assert running.status == HabitStatus.ACTIVE
        assert running.chips == ["yes", "no", "skip"]
        assert running.period is not None
        assert running.period.period_type == "W"
        assert running.period.period_count == 1
        assert running.period.target_count == 3

        # Check records with text
        run_records = {str(r.day): r for r in running.records}
        assert "2024-01-15" in run_records
        assert run_records["2024-01-15"].done is True
        assert run_records["2024-01-15"].text == "Ran 5km"
        assert "2024-01-16" in run_records
        assert run_records["2024-01-16"].done is False
        assert run_records["2024-01-16"].text == "Rest day"
        assert "2024-01-17" in run_records
        assert run_records["2024-01-17"].done is True
        assert run_records["2024-01-17"].text == "Ran 3km #skip"

        # Check Reading habit (archived)
        reading = next(h for h in target.habits if h.id == "read01")
        assert reading.name == "Reading"
        assert reading.status == HabitStatus.ARCHIVED
        assert reading.tags == ["learning"]
        assert reading.star is False

    async def test_merge_conflict_both_sides_have_data(self):
        """When both sides have the same habit, merge should combine correctly."""
        # Existing data
        existing = _habit_list({
            "habits": [
                _make_habit(
                    "Running",
                    habit_id="run001",
                    tags=["fitness"],
                    star=False,
                    records=[
                        _make_record("2024-01-15", True, "self note"),
                        _make_record("2024-01-16", False, "rest"),
                    ],
                )
            ],
            "order": ["run001"],
            "order_by": "MANUALLY",
        })

        # Imported data (same habit, different metadata + overlapping records)
        imported = _habit_list({
            "habits": [
                _make_habit(
                    "Running v2",
                    habit_id="run001",
                    tags=["fitness", "outdoor"],
                    star=True,
                    period=HabitFrequency("W", 1, 3),
                    chips=["yes", "no"],
                    records=[
                        _make_record("2024-01-15", True, "imported note"),
                        _make_record("2024-01-17", True, "new day"),
                    ],
                )
            ],
            "order": ["run001"],
            "order_by": "MANUALLY",
        })

        await existing.merge(imported)

        habit = existing.habits[0]

        # Metadata should come from imported side
        assert habit.name == "Running v2"
        assert habit.tags == ["fitness", "outdoor"]
        assert habit.star is True
        assert habit.period is not None
        assert habit.period.target_count == 3
        assert habit.chips == ["yes", "no"]

        # Records: union of all days with text preserved
        records_by_day = {str(r.day): r for r in habit.records}
        assert len(records_by_day) == 3

        # Day 15: both had text — imported wins
        assert records_by_day["2024-01-15"].text == "imported note"
        assert records_by_day["2024-01-15"].done is True

        # Day 16: only existing — preserved
        assert records_by_day["2024-01-16"].text == "rest"
        assert records_by_day["2024-01-16"].done is False

        # Day 17: only imported — preserved
        assert records_by_day["2024-01-17"].text == "new day"
        assert records_by_day["2024-01-17"].done is True

    async def test_telegram_backup_roundtrip(self):
        """Telegram backup (full raw dict) should restore perfectly."""
        # 1. Build source with all statuses
        source_data = {
            "habits": [
                _make_habit("Active", habit_id="a1", tags=["t1"]),
                _make_habit(
                    "Archived",
                    habit_id="ar1",
                    status=HabitStatus.ARCHIVED,
                    records=[_make_record("2024-03-01", True, "archived note")],
                ),
                _make_habit(
                    "Deleted",
                    habit_id="d1",
                    status=HabitStatus.SOLF_DELETED,
                ),
            ],
            "order": ["a1", "ar1", "d1"],
            "order_by": "MANUALLY",
            "backup": {
                "telegram_bot_token": "bot_token",
                "telegram_chat_id": "chat_id",
            },
        }

        # 2. Telegram backup sends the raw dict
        backup_json = json.dumps(source_data)

        # 3. Restore via import
        parsed = json.loads(backup_json)
        order = parsed.pop("order", None)
        order_by = parsed.pop("order_by", None)
        parsed.pop("version", None)
        parsed.pop("user_email", None)
        parsed.pop("exported_at", None)

        imported = DictHabitList(parsed)
        if order is not None:
            imported.order = order
        if order_by is not None:
            imported.order_by = HabitOrder(order_by)

        # 4. Merge into fresh target
        target = DictHabitList({"habits": []})
        await target.merge(imported)

        # 5. All habits (including deleted) should be restored
        assert len(target.habits) == 3
        ids = {h.id for h in target.habits}
        assert ids == {"a1", "ar1", "d1"}

        # Status preserved
        archived = next(h for h in target.habits if h.id == "ar1")
        assert archived.status == HabitStatus.ARCHIVED
        assert archived.records[0].text == "archived note"

        deleted = next(h for h in target.habits if h.id == "d1")
        assert deleted.status == HabitStatus.SOLF_DELETED

        # Order preserved
        assert target.order == ["a1", "ar1", "d1"]
        assert target.order_by == HabitOrder.MANUALLY

    async def test_roundtrip_order_name_sorting(self):
        """Export with NAME ordering should preserve order_by on roundtrip."""
        source = _habit_list({
            "habits": [
                _make_habit("Banana", habit_id="b1"),
                _make_habit("Apple", habit_id="a1"),
            ],
            "order": [],
            "order_by": "NAME",
        })

        # Simulate export
        export_envelope = {
            "version": SCHEMA_VERSION,
            "order": source.order,
            "order_by": source.order_by.value,
            "habits": [h.to_dict() for h in source.habits],
        }
        export_json = json.dumps(export_envelope)

        # Simulate import
        parsed = json.loads(export_json)
        order = parsed.pop("order", None)
        order_by = parsed.pop("order_by", None)
        parsed.pop("version", None)
        parsed.pop("user_email", None)
        parsed.pop("exported_at", None)

        imported = DictHabitList(parsed)
        if order is not None:
            imported.order = order
        if order_by is not None:
            imported.order_by = HabitOrder(order_by)

        target = DictHabitList({"habits": []})
        await target.merge(imported)

        assert target.order_by == HabitOrder.NAME
