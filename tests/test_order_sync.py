"""Regression tests for habit order metadata maintenance.

Covers:
- Deleting habits removes stale IDs from order list
- Archiving habits keeps their ID in order list (archived habits remain sortable)
- Importing/merging habits adds their IDs to order list
- API put_habits_meta cleans stale IDs
- HabitListBuilder sorting handles missing/new order entries correctly
- Soft-delete compacts the order list
"""

import pytest

from beaverhabits.storage.dict import DictHabitList
from beaverhabits.storage.storage import HabitListBuilder, HabitOrder, HabitStatus


def _make_habit_list(habit_names: list[str], order: list[str] | None = None) -> DictHabitList:
    """Create a DictHabitList with given habit names and optional order."""
    import asyncio

    data: dict = {"habits": []}
    hl = DictHabitList()
    hl.data = data

    ids = []
    for name in habit_names:
        ids.append(asyncio.get_event_loop().run_until_complete(hl.add(name)))

    if order is not None:
        hl.order = order
    return hl, ids


async def _async_make(habit_names: list[str], order: list[str] | None = None) -> tuple:
    data: dict = {"habits": []}
    hl = DictHabitList()
    hl.data = data

    ids = []
    for name in habit_names:
        ids.append(await hl.add(name))

    if order is not None:
        hl.order = order
    return hl, ids


# ── Deletion ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_remove_habit_cleans_order():
    """Deleting a habit must remove its ID from the order list."""
    hl, ids = await _async_make(["A", "B", "C"])
    # Set a manual order
    hl.order = [ids[0], ids[1], ids[2]]
    hl.order_by = HabitOrder.MANUALLY

    # Remove the middle habit
    habit_b = hl.habits[1]
    await hl.remove(habit_b)

    assert ids[1] not in hl.order
    assert hl.order == [ids[0], ids[2]]


@pytest.mark.asyncio
async def test_remove_habit_not_in_order():
    """Deleting a habit whose ID is not in order should be a no-op for order."""
    hl, ids = await _async_make(["A", "B", "C"])
    hl.order = [ids[0], ids[2]]  # B is not in order
    hl.order_by = HabitOrder.MANUALLY

    habit_b = hl.habits[1]
    await hl.remove(habit_b)

    assert hl.order == [ids[0], ids[2]]


# ── Archiving ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_archive_keeps_id_in_order():
    """Archiving a habit should NOT remove its ID from the order list."""
    hl, ids = await _async_make(["A", "B", "C"])
    hl.order = [ids[0], ids[1], ids[2]]
    hl.order_by = HabitOrder.MANUALLY

    # Archive habit B via status change (same as HabitDeleteButton first click)
    habit_b = hl.habits[1]
    habit_b.status = HabitStatus.ARCHIVED

    # Order should still contain B since archived habits stay in the order list
    assert ids[1] in hl.order
    assert hl.order == [ids[0], ids[1], ids[2]]


# ── Soft Delete ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compact_order_removes_soft_deleted():
    """compact_order must strip IDs of soft-deleted habits from the order list."""
    hl, ids = await _async_make(["A", "B", "C"])
    hl.order = [ids[0], ids[1], ids[2]]

    # Soft-delete B (simulates HabitDeleteButton second click)
    habit_b = hl.habits[1]
    habit_b.status = HabitStatus.SOLF_DELETED

    hl.compact_order()

    assert ids[1] not in hl.order
    assert hl.order == [ids[0], ids[2]]


@pytest.mark.asyncio
async def test_compact_order_removes_nonexistent_ids():
    """compact_order must strip IDs that don't match any habit at all."""
    hl, ids = await _async_make(["A", "B", "C"])
    hl.order = [ids[0], "ghost_id_1", ids[1], "ghost_id_2", ids[2]]

    hl.compact_order()

    assert "ghost_id_1" not in hl.order
    assert "ghost_id_2" not in hl.order
    assert hl.order == [ids[0], ids[1], ids[2]]


@pytest.mark.asyncio
async def test_compact_order_empty_order():
    """compact_order on an empty order list should be a safe no-op."""
    hl, ids = await _async_make(["A", "B"])
    hl.order = []

    hl.compact_order()

    assert hl.order == []


@pytest.mark.asyncio
async def test_compact_order_preserves_archived():
    """compact_order should NOT remove archived habit IDs."""
    hl, ids = await _async_make(["A", "B", "C"])
    hl.order = [ids[0], ids[1], ids[2]]

    habit_b = hl.habits[1]
    habit_b.status = HabitStatus.ARCHIVED

    hl.compact_order()

    # Archived habits are still valid (they appear in the order page)
    assert ids[1] in hl.order
    assert hl.order == [ids[0], ids[1], ids[2]]


# ── Import / Merge ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_adds_imported_habits_to_order():
    """Merging/importing habits must add their IDs to the order list."""
    hl, ids = await _async_make(["A", "B"])
    hl.order = [ids[0], ids[1]]

    # Create another list to import from
    other, other_ids = await _async_make(["C", "D"])

    await hl.merge(other)

    # Imported habits should be appended to order
    for oid in other_ids:
        assert oid in hl.order
    # Original order preserved for existing habits
    assert hl.order.index(ids[0]) < hl.order.index(ids[1])


@pytest.mark.asyncio
async def test_merge_no_duplicate_in_order():
    """Merging habits already present should not duplicate their IDs in order."""
    hl, ids = await _async_make(["A", "B"])
    hl.order = [ids[0], ids[1]]

    # Create other list with one overlapping habit (by name, same ID)
    other_data: dict = {"habits": [{"name": "A", "id": ids[0], "records": [], "tags": []}]}
    other = DictHabitList()
    other.data = other_data

    await hl.merge(other)

    assert hl.order.count(ids[0]) == 1


# ── Add ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_appends_to_order():
    """Adding a new habit must append its ID to the order list."""
    hl, ids = await _async_make(["A", "B"])
    hl.order = [ids[0], ids[1]]

    new_id = await hl.add("C")

    assert new_id in hl.order
    assert hl.order[-1] == new_id


@pytest.mark.asyncio
async def test_add_empty_order():
    """Adding a habit when order is empty should create a single-element order."""
    hl, ids = await _async_make([])
    assert hl.order == []

    new_id = await hl.add("A")

    assert hl.order == [new_id]


# ── Builder ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_builder_manual_order_with_missing_id():
    """Builder should push habits not in order list to the end (float('inf'))."""
    hl, ids = await _async_make(["A", "B", "C"])
    # Only A and B in order; C should appear after them
    hl.order = [ids[1], ids[0]]  # B, A
    hl.order_by = HabitOrder.MANUALLY

    result = HabitListBuilder(hl).build()
    names = [h.name for h in result]

    assert names == ["B", "A", "C"]


@pytest.mark.asyncio
async def test_builder_manual_order_stale_id_ignored():
    """Builder should ignore stale IDs in order that don't match any habit."""
    hl, ids = await _async_make(["A", "B"])
    hl.order = ["stale_id", ids[1], ids[0]]
    hl.order_by = HabitOrder.MANUALLY

    result = HabitListBuilder(hl).build()
    names = [h.name for h in result]

    # B first (index 1 in order), A second (index 2), stale ignored
    assert names == ["B", "A"]


@pytest.mark.asyncio
async def test_builder_empty_order_all_at_inf():
    """Builder with empty order list should keep original insertion order."""
    hl, ids = await _async_make(["C", "A", "B"])
    hl.order = []
    hl.order_by = HabitOrder.MANUALLY

    result = HabitListBuilder(hl).build()
    names = [h.name for h in result]

    # All get float("inf"), stable sort preserves original order
    assert names == ["C", "A", "B"]


@pytest.mark.asyncio
async def test_builder_name_sort_ignores_order():
    """When order_by is NAME, the order list should be ignored."""
    hl, ids = await _async_make(["C", "A", "B"])
    hl.order = [ids[2], ids[0], ids[1]]  # B, C, A
    hl.order_by = HabitOrder.NAME

    result = HabitListBuilder(hl).build()
    names = [h.name for h in result]

    assert names == ["A", "B", "C"]


# ── API compact_order (unit-level simulation) ────────────────────────────────


@pytest.mark.asyncio
async def test_api_put_meta_compacts_stale_ids():
    """Simulates PUT /habits/meta: setting order then compacting should strip stale IDs."""
    hl, ids = await _async_make(["A", "B", "C"])

    # API receives an order with a stale ID
    hl.order = [ids[0], "nonexistent_id", ids[2]]
    hl.compact_order()

    assert "nonexistent_id" not in hl.order
    assert hl.order == [ids[0], ids[2]]


@pytest.mark.asyncio
async def test_full_lifecycle_add_delete_order_consistency():
    """End-to-end: add habits, set order, delete one, verify order is consistent."""
    hl, ids = await _async_make(["A", "B", "C", "D"])
    hl.order = [ids[0], ids[1], ids[2], ids[3]]
    hl.order_by = HabitOrder.MANUALLY

    # Delete B
    habit_b = hl.habits[1]
    await hl.remove(habit_b)

    # Build list — should not raise or misplace
    result = HabitListBuilder(hl).build()
    names = [h.name for h in result]

    assert "B" not in names
    assert names == ["A", "C", "D"]
    assert hl.order == [ids[0], ids[2], ids[3]]
