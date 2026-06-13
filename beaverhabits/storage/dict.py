import datetime
from dataclasses import dataclass, field

from beaverhabits.logger import logger
from beaverhabits.storage.storage import (
    Backup,
    CheckedRecord,
    Habit,
    HabitFrequency,
    HabitList,
    HabitOrder,
    HabitStatus,
)
from beaverhabits.utils import generate_short_hash

DAY_MASK = "%Y-%m-%d"
MONTH_MASK = "%Y/%m"


@dataclass(init=False)
class DictStorage:
    data: dict = field(default_factory=dict, metadata={"exclude": True})


@dataclass
class DictRecord(CheckedRecord, DictStorage):
    """
    # Read (d1~d3)
    persistent    ->     memory      ->     view
    d0: [x]              d0: [x]
                                            d1: [ ]
    d2: [x]              d2: [x]            d2: [x]
                                            d3: [ ]

    # Update:
    view(update)  ->     memory      ->     persistent
    d1: [ ]
    d2: [ ]              d2: [ ]            d2: [x]
    d3: [x]              d3: [x]            d3: [ ]
    """

    @property
    def day(self) -> datetime.date:
        date = datetime.datetime.strptime(self.data["day"], DAY_MASK)
        return date.date()

    @property
    def done(self) -> bool:
        return self.data.get("done", False)

    @done.setter
    def done(self, value: bool) -> None:
        self.data["done"] = value

    @property
    def text(self) -> str:
        return self.data.get("text", "")

    @text.setter
    def text(self, value: str) -> None:
        self.data["text"] = value


class HabitDataCache:
    def __init__(self, habit: "DictHabit"):
        self.habit = habit
        self.refresh()

    def refresh(self):
        self.ticked_days = [r.day for r in self.habit.records if r.done]
        self.ticked_data = {r.day: r for r in self.habit.records}


@dataclass
class DictHabit(Habit[DictRecord], DictStorage):

    def __init__(self, data: dict, habit_list: HabitList) -> None:
        self.data = data
        self._habit_list = habit_list
        self.cache = HabitDataCache(self)

    @property
    def habit_list(self) -> HabitList:
        return self._habit_list

    @property
    def id(self) -> str:
        if "id" not in self.data:
            self.data["id"] = generate_short_hash(self.name)
        return self.data["id"]

    @id.setter
    def id(self, value: str) -> None:
        self.data["id"] = value

    @property
    def name(self) -> str:
        return self.data["name"]

    @name.setter
    def name(self, value: str) -> None:
        self.data["name"] = value

    @property
    def tags(self) -> list[str]:
        return self.data.get("tags", [])

    @tags.setter
    def tags(self, value: list[str]) -> None:
        self.data["tags"] = list(value)

    @property
    def star(self) -> bool:
        return self.data.get("star", False)

    @star.setter
    def star(self, value: int) -> None:
        self.data["star"] = value

    @property
    def status(self) -> HabitStatus:
        status_value = self.data.get("status")

        if status_value is None:
            return HabitStatus.ACTIVE

        try:
            return HabitStatus(status_value)
        except ValueError:
            logger.error(f"Invalid status value: {status_value}")
            self.data["status"] = None
            return HabitStatus.ACTIVE

    @status.setter
    def status(self, value: HabitStatus) -> None:
        self.data["status"] = value.value

    @property
    def records(self) -> list[DictRecord]:
        return [DictRecord(d) for d in self.data["records"]]

    @property
    def period(self) -> HabitFrequency | None:
        period_value = self.data.get("period")
        if not period_value:
            return None

        try:
            return HabitFrequency.from_dict(period_value)
        except (ValueError, KeyError, TypeError):
            logger.error(f"Invalid period value: {period_value}")
            self.data["period"] = None
            return None

    @period.setter
    def period(self, value: HabitFrequency | None) -> None:
        if value is None:
            self.data["period"] = None
            return

        self.data["period"] = value.to_dict()

    @property
    def chips(self) -> list[str]:
        return self.data.get("chips", [])

    @chips.setter
    def chips(self, value: list[str]) -> None:
        self.data["chips"] = value

    @property
    def ticked_days(self) -> list[datetime.date]:
        return self.cache.ticked_days

    def ticked_count(
        self, start: datetime.date | None = None, end: datetime.date | None = None
    ) -> int:
        if start is None:
            start = datetime.date.min
        if end is None:
            end = datetime.date.max

        return sum(1 for day in self.ticked_days if start <= day <= end)

    @property
    def ticked_data(self) -> dict[datetime.date, DictRecord]:
        return self.cache.ticked_data

    async def tick(
        self, day: datetime.date, done: bool, text: str | None = None
    ) -> CheckedRecord:
        # Find the record in the cache
        record = self.ticked_data.get(day)

        if record is not None:
            # Update only if necessary to avoid unnecessary writes
            new_data = {}
            if record.done != done:
                new_data["done"] = done
            if text is not None and record.text != text:
                new_data["text"] = text
            if new_data:
                record.data.update(new_data)

        else:
            # Update storage once
            data = {"day": day.strftime(DAY_MASK), "done": done}
            if text is not None:
                data["text"] = text
            self.data["records"].append(data)

        # Update the cache
        self.cache.refresh()

        return self.ticked_data[day]

    async def merge(self, other: "DictHabit") -> None:
        """Merge another habit's history and metadata into this one.

        History is unioned by day, preserving record notes and days that are
        only annotated (not done). ``done`` is OR-ed across both copies and the
        note prefers this habit's text, falling back to the other's, so an
        import never silently overwrites existing data. Metadata missing here
        (tags, chips, period, star) is filled in from ``other`` while existing
        non-empty values are kept.
        """
        # Union records by day so notes and not-done-but-noted days survive.
        merged: dict[str, dict] = {}
        for record in (
            *self.data.get("records", []),
            *other.data.get("records", []),
        ):
            day = record.get("day")
            if not day:
                continue
            done = bool(record.get("done", False))
            text = record.get("text") or ""
            existing = merged.get(day)
            if existing is None:
                merged[day] = {"day": day, "done": done, "text": text}
            else:
                existing["done"] = existing["done"] or done
                if not existing["text"]:
                    existing["text"] = text

        # Re-emit every recorded day so export -> import round-trips exactly.
        records = []
        for day in sorted(merged):
            entry = merged[day]
            record = {"day": entry["day"], "done": entry["done"]}
            if entry["text"]:
                record["text"] = entry["text"]
            records.append(record)
        self.data["records"] = records

        # Fill in metadata this habit is missing; never clobber existing values.
        if not self.tags and other.tags:
            self.tags = other.tags
        if not self.chips and other.chips:
            self.chips = other.chips
        if self.period is None and other.period is not None:
            self.period = other.period
        if not self.star and other.star:
            self.star = other.star

        self.cache.refresh()

    def copy(self) -> "Habit":
        new_data = {
            "name": self.name,
            "tags": self.tags,
            "star": self.star,
            "period": self.period.to_dict() if self.period else None,
            "records": [],
        }
        return DictHabit(new_data, self.habit_list)

    def to_dict(self) -> dict:
        return self.data

    def __eq__(self, other: object) -> bool:
        return isinstance(other, DictHabit) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    def __str__(self) -> str:
        return f"[{self.id}]{self.name}<{self.status.value}>"

    __repr__ = __str__


@dataclass
class DictHabitList(HabitList[DictHabit], DictStorage):
    @property
    def habits(self) -> list[DictHabit]:
        return [DictHabit(d, self) for d in self.data["habits"]]

    @property
    def order(self) -> list[str]:
        return self.data.get("order", [])

    @order.setter
    def order(self, value: list[str]) -> None:
        self.data["order"] = value

    @property
    def order_by(self) -> HabitOrder:
        order_value = self.data.get("order_by")
        if order_value is None:
            return HabitOrder.MANUALLY

        try:
            return HabitOrder(order_value)
        except ValueError:
            logger.error(f"Invalid order value: {order_value}")
            self.data["order_by"] = None
            return HabitOrder.MANUALLY

    @order_by.setter
    def order_by(self, value: HabitOrder) -> None:
        self.data["order_by"] = value.value

    @property
    def backup(self) -> Backup:
        backup_value = self.data.get("backup")
        if backup_value is None:
            return Backup()

        try:
            return Backup.from_dict(backup_value)
        except ValueError:
            logger.error(f"Invalid backup value: {backup_value}")
            self.data["backup"] = None
            return Backup()

    @backup.setter
    def backup(self, value: Backup) -> None:
        self.data["backup"] = value.to_dict()

    async def get_habit_by(self, habit_id: str) -> DictHabit | None:
        for habit in self.habits:
            if habit.id == habit_id:
                return habit

    async def add(self, name: str, tags: list | None = None) -> str:
        id = generate_short_hash(name)
        d = {"name": name, "records": [], "id": id, "tags": tags or []}
        self.data["habits"].append(d)
        return id

    async def remove(self, item: DictHabit) -> None:
        self.data["habits"].remove(item.data)

    async def merge(self, other: "DictHabitList") -> None:
        """Merge another habit list into this one (import / restore).

        ``self`` is the destination workspace and ``other`` the imported data.
        Habits are matched by id across *every* status, so a re-imported
        archived (or soft-deleted) habit is updated in place instead of being
        duplicated. Habits not present here are appended verbatim, keeping their
        full history, notes, tags, period and status. List-level ordering, sort
        mode and backup settings are restored from ``other`` where this list
        does not already define them, so a restore onto an empty workspace
        reproduces it exactly while a merge into a live workspace keeps the
        local arrangement.
        """
        self_by_id = {habit.id: habit for habit in self.habits}
        existing_names = {habit.name for habit in self.habits}

        for other_habit in other.habits:
            self_habit = self_by_id.get(other_habit.id)
            if self_habit is not None:
                # Same habit -> merge history and metadata in place.
                await self_habit.merge(other_habit)
                continue

            # New habit -> add it, disambiguating only on a real name clash.
            if other_habit.name in existing_names:
                other_habit.name = f"{other_habit.name} (imported)"
            existing_names.add(other_habit.name)
            self.data["habits"].append(other_habit.data)
            self_by_id[other_habit.id] = other_habit

        self._merge_meta(other)

    def _merge_meta(self, other: "DictHabitList") -> None:
        # Preserve the current arrangement, then slot in anything new from the
        # import followed by any habit still missing from the order list.
        order = list(self.order)
        seen = set(order)
        for habit_id in (*other.order, *(h.id for h in self.habits)):
            if habit_id not in seen:
                order.append(habit_id)
                seen.add(habit_id)
        if order:
            self.order = order

        # Adopt sort mode / backup only when this list has not set its own.
        if "order_by" not in self.data and other.data.get("order_by") is not None:
            self.order_by = other.order_by

        if not self.backup.telegram_bot_token and other.backup.telegram_bot_token:
            self.backup = other.backup
