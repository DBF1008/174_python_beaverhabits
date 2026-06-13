"""Regression tests for the habit-order metadata maintenance chain.

These cover the scenarios where manual ordering used to drift out of sync with the
actual habit set: delete, archive, import/restore, API-style order updates, and
corrupted/invalid order data. They operate directly on ``DictHabitList`` (no DB or GUI)
so they are fast and deterministic. ``asyncio_mode = auto`` (pytest.ini) runs the async
cases without extra decorators.
"""

import datetime
from dataclasses import dataclass

from beaverhabits.storage.dict import DictHabitList
from beaverhabits.storage.storage import (
    HabitListBuilder,
    HabitStatus,
    reconcile_habit_order,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_habit_list(specs, order=None) -> DictHabitList:
    """Build a DictHabitList from ``specs`` (an id string or an (id, status) tuple)."""
    habits = []
    for spec in specs:
        if isinstance(spec, tuple):
            habit_id, status = spec
        else:
            habit_id, status = spec, HabitStatus.ACTIVE
        habits.append(
            {"id": habit_id, "name": habit_id, "records": [], "status": status.value}
        )
    data = {"habits": habits}
    if order is not None:
        data["order"] = order
    return DictHabitList(data)


def active_ids(habit_list: DictHabitList) -> list[str]:
    """Ids as the home page builds them."""
    builder = HabitListBuilder(habit_list).status(HabitStatus.ACTIVE)
    return [h.id for h in builder.build()]


def reorder_ids(habit_list: DictHabitList) -> list[str]:
    """Ids as the reorder page builds them (active section + archived section)."""
    builder = HabitListBuilder(habit_list).status(
        HabitStatus.ACTIVE, HabitStatus.ARCHIVED
    )
    return [h.id for h in builder.build()]


@dataclass
class _FakeHabit:
    id: str
    status: HabitStatus


# ---------------------------------------------------------------------------
# Pure helper invariants
# ---------------------------------------------------------------------------


def test_reconcile_drops_stale_dups_and_appends_missing():
    habits = [
        _FakeHabit("a", HabitStatus.ACTIVE),
        _FakeHabit("b", HabitStatus.ARCHIVED),
        _FakeHabit("c", HabitStatus.ACTIVE),
    ]
    # "ghost" no longer exists, "c" is duplicated, "b" is missing from the order.
    result = reconcile_habit_order(habits, ["c", "ghost", "a", "c"])
    # known order preserved (c, a), missing appended (b), active grouped before archived.
    assert result == ["c", "a", "b"]


def test_reconcile_ignores_non_string_entries():
    habits = [_FakeHabit("a", HabitStatus.ACTIVE)]
    assert reconcile_habit_order(habits, ["a", 1, None, {"x": 1}]) == ["a"]


def test_reconcile_empty_order_is_deterministic_and_active_first():
    habits = [
        _FakeHabit("a", HabitStatus.ARCHIVED),
        _FakeHabit("b", HabitStatus.ACTIVE),
    ]
    # No stored order -> habits insertion order, then active-first grouping.
    assert reconcile_habit_order(habits, []) == ["b", "a"]


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def test_delete_prunes_stale_order_entry():
    habit_list = make_habit_list(["a", "b", "c"], order=["a", "b", "c"])

    await habit_list.remove(await habit_list.get_habit_by("b"))

    assert "b" not in habit_list.order
    assert habit_list.order == ["a", "c"]
    assert active_ids(habit_list) == ["a", "c"]


# ---------------------------------------------------------------------------
# Add
# ---------------------------------------------------------------------------


async def test_add_appends_new_id_to_order():
    habit_list = make_habit_list(["a", "b"], order=["a", "b"])

    new_id = await habit_list.add("c")

    # New habit gets a concrete position at the end of the active block (not inf).
    assert new_id in habit_list.order
    assert habit_list.order == ["a", "b", new_id]
    assert active_ids(habit_list)[-1] == new_id


# ---------------------------------------------------------------------------
# Archive boundary
# ---------------------------------------------------------------------------


async def test_archive_groups_active_first_and_fixes_boundary():
    habit_list = make_habit_list(["a", "b", "c"], order=["a", "b", "c"])

    # Archive the middle habit in place (status change only, no list mutation).
    (await habit_list.get_habit_by("b")).status = HabitStatus.ARCHIVED

    # build() self-heals for display: archived habit lands after every active one.
    assert active_ids(habit_list) == ["a", "c"]
    assert reorder_ids(habit_list) == ["a", "c", "b"]

    # Explicit reconcile persists the active-first grouping.
    assert habit_list.reconcile_order() == ["a", "c", "b"]
    assert habit_list.order == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# Import / restore
# ---------------------------------------------------------------------------


async def test_import_merge_reconciles_order_and_records():
    habit_list = make_habit_list(["a", "b"], order=["a", "b"])
    other = DictHabitList(
        {
            "habits": [
                {
                    "id": "a",  # overlaps -> records merged
                    "name": "a",
                    "records": [{"day": "2024-01-01", "done": True}],
                },
                {"id": "x", "name": "x", "records": []},  # new -> appended
            ]
        }
    )

    await habit_list.merge(other)

    # Imported id appended, nothing stale, every id present exactly once.
    assert habit_list.order == ["a", "b", "x"]
    assert active_ids(habit_list) == ["a", "b", "x"]
    # Overlapping habit's records were actually merged.
    merged = await habit_list.get_habit_by("a")
    assert datetime.date(2024, 1, 1) in merged.ticked_days


# ---------------------------------------------------------------------------
# Invalid / corrupted order data
# ---------------------------------------------------------------------------


async def test_invalid_order_data_is_sanitized():
    habit_list = make_habit_list(
        ["a", "b"], order=["a", "ghost", "b", "a", 123, None]
    )

    # Rendering stays deterministic despite the garbage entries.
    assert active_ids(habit_list) == ["a", "b"]

    # Reconcile drops the ghost id, the duplicate, and the non-string entries.
    assert habit_list.reconcile_order() == ["a", "b"]
    assert habit_list.order == ["a", "b"]


async def test_ids_missing_from_order_are_appended_not_clumped():
    habit_list = make_habit_list(["a", "b", "c"], order=["c"])

    # "c" first (from stored order), then a, b appended in habits order.
    assert active_ids(habit_list) == ["c", "a", "b"]
    assert habit_list.reconcile_order() == ["c", "a", "b"]


async def test_soft_deleted_preserved_but_excluded_from_builds():
    habit_list = make_habit_list(
        [
            ("a", HabitStatus.ACTIVE),
            ("b", HabitStatus.SOLF_DELETED),
            ("c", HabitStatus.ACTIVE),
        ],
        order=["a", "b", "c"],
    )

    # Soft-deleted habit never shows up in any visible list.
    assert active_ids(habit_list) == ["a", "c"]
    assert reorder_ids(habit_list) == ["a", "c"]

    # ...but its position is retained (at the tail) rather than dropped.
    assert habit_list.reconcile_order() == ["a", "c", "b"]
    assert "b" in habit_list.order


# ---------------------------------------------------------------------------
# Home page vs reorder page consistency (the core "对不上" bug)
# ---------------------------------------------------------------------------


async def test_home_and_reorder_pages_stay_consistent_after_churn():
    habit_list = make_habit_list(["a", "b", "c", "d"], order=["a", "b", "c", "d"])

    # A realistic mix of mutations through different code paths.
    await habit_list.remove(await habit_list.get_habit_by("b"))  # delete
    (await habit_list.get_habit_by("c")).status = HabitStatus.ARCHIVED  # archive
    new_id = await habit_list.add("e")  # add

    home = active_ids(habit_list)
    reorder_active = [
        h.id
        for h in HabitListBuilder(habit_list)
        .status(HabitStatus.ACTIVE, HabitStatus.ARCHIVED)
        .build()
        if h.status == HabitStatus.ACTIVE
    ]

    # The home page order and the reorder page's active section must match exactly.
    assert home == reorder_active
    assert home == ["a", "d", new_id]
    assert "b" not in habit_list.order  # stale id gone
