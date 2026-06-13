"""Pure computation functions for habit statistics.

These functions extract the statistical logic that was previously coupled
to the NiceGUI UI layer (frontend/components.py) into reusable, testable
units. All functions reuse ``get_habit_date_completion`` from
``core/completions.py`` to guarantee API results match the web UI.
"""

import calendar
import datetime
from dataclasses import dataclass, field

from dateutil.relativedelta import relativedelta

from beaverhabits.core.completions import get_habit_date_completion
from beaverhabits.storage.storage import EVERY_DAY, Habit, HabitFrequency
from beaverhabits.utils import date_move, get_period_fist_day


# ---------------------------------------------------------------------------
# Streak summary
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StreakInfo:
    """A single consecutive-day streak."""

    start: datetime.date
    end: datetime.date
    length: int


@dataclass(frozen=True)
class StreakSummary:
    """Aggregated streak information for a habit."""

    current_streak: int
    longest_streak: int
    recent_streaks: list[StreakInfo] = field(default_factory=list)


def _find_streak_runs(dates_desc: list[datetime.date]) -> list[StreakInfo]:
    """Split a descending-sorted list of dates into consecutive streak runs.

    Returns streaks ordered *newest first*.
    """
    if not dates_desc:
        return []

    runs: list[StreakInfo] = []
    streak_end = dates_desc[0]
    streak_start = dates_desc[0]
    count = 1

    for i in range(1, len(dates_desc)):
        if (dates_desc[i] - dates_desc[i - 1]).days == -1:
            # Still consecutive
            streak_start = dates_desc[i]
            count += 1
        else:
            runs.append(StreakInfo(start=streak_start, end=streak_end, length=count))
            streak_end = dates_desc[i]
            streak_start = dates_desc[i]
            count = 1

    # Flush last run
    runs.append(StreakInfo(start=streak_start, end=streak_end, length=count))
    return runs


def compute_streak_summary(
    habit: Habit, today: datetime.date
) -> StreakSummary:
    """Compute current streak, longest streak, and up to 5 recent streaks.

    Looks back exactly 1 year from *today*, mirroring the web UI behaviour
    in ``compose_habit_streaks``.
    """
    start = today.replace(year=today.year - 1)
    status = get_habit_date_completion(habit, start, today)
    dates_desc = sorted(status.keys(), reverse=True)

    if len(dates_desc) <= 1:
        if len(dates_desc) == 1:
            d = dates_desc[0]
            single = StreakInfo(start=d, end=d, length=1)
            # Current streak is 1 only if that day is today or yesterday
            days_ago = (today - d).days
            current = 1 if days_ago <= 1 else 0
            return StreakSummary(
                current_streak=current,
                longest_streak=1,
                recent_streaks=[single],
            )
        return StreakSummary(current_streak=0, longest_streak=0)

    runs = _find_streak_runs(dates_desc)

    # Current streak: the most recent run must end today or yesterday
    current = 0
    if runs:
        latest = runs[0]
        gap = (today - latest.end).days
        if gap <= 1:
            current = latest.length

    longest = max(r.length for r in runs)
    recent = runs[:5]

    return StreakSummary(
        current_streak=current,
        longest_streak=longest,
        recent_streaks=recent,
    )


# ---------------------------------------------------------------------------
# Period summary
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PeriodProgress:
    """Progress within a single period window."""

    period_start: datetime.date
    period_end: datetime.date
    done_count: int
    target_count: int
    completed: bool


@dataclass(frozen=True)
class PeriodSummary:
    """Aggregated period-goal progress across a date range."""

    period_type: str
    period_count: int
    target_count: int
    periods: list[PeriodProgress] = field(default_factory=list)
    overall_completion_rate: float = 0.0


def _enumerate_period_windows(
    p: HabitFrequency,
    start: datetime.date,
    end: datetime.date,
) -> list[tuple[datetime.date, datetime.date]]:
    """Generate all ``[window_start, window_end)`` pairs covering [start, end]."""
    # Align to the period boundary that contains (or precedes) *start*
    cursor = get_period_fist_day(start, p.period_type)

    windows: list[tuple[datetime.date, datetime.date]] = []
    while cursor <= end:
        window_end = date_move(cursor, p.period_count, p.period_type)
        # Only include windows that overlap with [start, end]
        if window_end > start:
            windows.append((cursor, window_end))
        cursor = window_end

    return windows


def compute_period_summary(
    habit: Habit,
    start: datetime.date,
    end: datetime.date,
) -> PeriodSummary | None:
    """Compute per-period-window progress for the given date range.

    Returns ``None`` when the habit has no configured period or the period
    is the default ``EVERY_DAY``.
    """
    p = habit.period
    if not p or p == EVERY_DAY:
        return None

    ticked = set(habit.ticked_days)
    windows = _enumerate_period_windows(p, start, end)

    periods: list[PeriodProgress] = []
    completed_count = 0
    for w_start, w_end in windows:
        done = sum(1 for d in ticked if w_start <= d < w_end)
        is_completed = done >= p.target_count
        if is_completed:
            completed_count += 1
        periods.append(
            PeriodProgress(
                period_start=w_start,
                period_end=w_end,
                done_count=done,
                target_count=p.target_count,
                completed=is_completed,
            )
        )

    rate = completed_count / len(periods) if periods else 0.0

    return PeriodSummary(
        period_type=p.period_type,
        period_count=p.period_count,
        target_count=p.target_count,
        periods=periods,
        overall_completion_rate=round(rate, 4),
    )


# ---------------------------------------------------------------------------
# Monthly trend
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MonthlyTrendEntry:
    """Tick count for a single calendar month."""

    year: int
    month: int
    label: str
    count: int


def compute_monthly_trend(
    habit: Habit,
    today: datetime.date,
    total_months: int = 13,
) -> list[MonthlyTrendEntry]:
    """Return per-month tick counts for the last *total_months* months.

    Mirrors ``habit_history`` in the frontend layer.
    """
    ticked = habit.ticked_days
    entries: list[MonthlyTrendEntry] = []

    for i in range(total_months, 0, -1):
        offset = today - relativedelta(months=i)
        count = sum(
            1
            for d in ticked
            if d.month == offset.month and d.year == offset.year
        )
        entries.append(
            MonthlyTrendEntry(
                year=offset.year,
                month=offset.month,
                label=calendar.month_abbr[offset.month],
                count=count,
            )
        )

    return entries


# ---------------------------------------------------------------------------
# Heatmap interval stats
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HeatmapStats:
    """Completion statistics for an arbitrary date range."""

    start: datetime.date
    end: datetime.date
    total_days: int
    completed_days: int
    completion_rate: float


def compute_heatmap_stats(
    habit: Habit,
    start: datetime.date,
    end: datetime.date,
) -> HeatmapStats:
    """Compute total/completed/rate for the inclusive date range."""
    status = get_habit_date_completion(habit, start, end)
    completed_days = sum(1 for day in status if start <= day <= end)
    total_days = (end - start).days + 1
    rate = completed_days / total_days if total_days > 0 else 0.0

    return HeatmapStats(
        start=start,
        end=end,
        total_days=total_days,
        completed_days=completed_days,
        completion_rate=round(rate, 4),
    )


# ---------------------------------------------------------------------------
# Bundle (all stats for one habit)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HabitStatsBundle:
    """Complete statistics bundle for a single habit."""

    habit_id: str
    habit_name: str
    streak: StreakSummary
    period: PeriodSummary | None
    monthly_trend: list[MonthlyTrendEntry]
    heatmap: HeatmapStats


def compute_habit_stats_bundle(
    habit: Habit,
    today: datetime.date,
    start: datetime.date,
    end: datetime.date,
    total_months: int = 13,
) -> HabitStatsBundle:
    """Compose all four statistics for a single habit."""
    return HabitStatsBundle(
        habit_id=str(habit.id),
        habit_name=habit.name,
        streak=compute_streak_summary(habit, today),
        period=compute_period_summary(habit, start, end),
        monthly_trend=compute_monthly_trend(habit, today, total_months),
        heatmap=compute_heatmap_stats(habit, start, end),
    )
