"""Structured habit statistics shared by the web UI and the HTTP API.

This module is intentionally free of any web-framework (``nicegui``) imports so
it can be reused both by the frontend stats pages and by ``routes/api.py``. All
figures are derived from the same completion engine the heatmap uses
(:func:`beaverhabits.core.completions.get_habit_date_completion`) and the same
period-window primitives as :func:`beaverhabits.core.completions.period`, so the
API stays consistent with what the pages display.

Every public ``*_summary``/``*_stats`` function takes ``(habit, start, end)``
with an inclusive ``[start, end]`` date range and returns plain
``dict``/``list``/``int`` values (JSON primitives only).
"""

import datetime

from dateutil.relativedelta import relativedelta

from beaverhabits.core.completions import CStatus, get_habit_date_completion
from beaverhabits.storage.storage import EVERY_DAY, Habit
from beaverhabits.utils import date_move, get_period_fist_day


def build_streak_segments(
    dates_desc: list[datetime.date],
) -> list[tuple[datetime.date, int]]:
    """Group descending-sorted completion days into consecutive-day streaks.

    ``dates_desc`` must be unique dates sorted most-recent-first. Returns one
    ``(boundary, length)`` entry per maximal run of consecutive calendar days,
    ordered oldest-run-first; ``boundary`` is the most-recent day of the run.

    This is the single source of truth for streak grouping: the frontend
    ``compose_habit_streaks`` formats its output from this, and the API's
    :func:`streak_summary` reads run lengths from it, so the two never diverge.
    """
    if not dates_desc:
        return []

    segments: list[tuple[datetime.date, int]] = []
    length = 1
    for i in range(1, len(dates_desc)):
        if (dates_desc[i] - dates_desc[i - 1]).days == -1:
            length += 1
        else:
            # Gap: close the run at its more-recent boundary (dates_desc[i - 1]).
            segments.insert(0, (dates_desc[i - 1], length))
            length = 1

    # Final (oldest) run, ending at the oldest completion day.
    segments.insert(0, (dates_desc[-1], length))
    return segments


def streak_summary(
    habit: Habit, start: datetime.date, end: datetime.date
) -> dict:
    """Streak figures over ``[start, end]`` (matches the web streak chart/badge).

    Completion days are the union of ``DONE`` and ``PERIOD_DONE`` days, the same
    set the streak chart is built from. ``current`` is the length of the most
    recent run (equal to the web badge's value), ``longest`` the longest run in
    the range, ``total`` the number of completion days, and ``segments`` the run
    lengths in chronological order.
    """
    status_map = get_habit_date_completion(habit, start, end)
    dates = sorted((d for d in status_map if start <= d <= end), reverse=True)

    segments = build_streak_segments(dates)
    lengths = [length for _, length in segments]
    return {
        "current": lengths[-1] if lengths else 0,
        "longest": max(lengths) if lengths else 0,
        "total": len(dates),
        "segments": lengths,
    }


def period_summary(
    habit: Habit, start: datetime.date, end: datetime.date
) -> dict | None:
    """Period-completion summary, or ``None`` for non-periodic habits.

    Returns ``None`` when the habit has no period or uses the default
    everyday frequency. Otherwise tiles consecutive period windows starting from
    the period boundary of ``start`` (stepping by ``period_count`` units) up to
    ``end``; a window counts as completed when its tick count reaches
    ``target_count``. For the common ``period_count == 1`` case these windows are
    exactly the ones the heatmap marks as ``PERIOD_DONE``. ``current_period_*``
    describe the window containing ``end``.
    """
    p = habit.period
    if not p or p == EVERY_DAY:
        return None

    ticked = habit.ticked_days
    total_periods = completed_periods = 0
    current_done = 0
    current_completed = False

    cursor = get_period_fist_day(start, p.period_type)
    while cursor <= end:
        nxt = date_move(cursor, p.period_count, p.period_type)
        if nxt <= cursor:  # defensive: period_count >= 1 always advances
            break

        done = sum(1 for d in ticked if cursor <= d < nxt)
        is_completed = done >= p.target_count

        total_periods += 1
        if is_completed:
            completed_periods += 1
        if cursor <= end < nxt:
            current_done = done
            current_completed = is_completed

        cursor = nxt

    return {
        "period_type": p.period_type,
        "period_count": p.period_count,
        "target_count": p.target_count,
        "total_periods": total_periods,
        "completed_periods": completed_periods,
        "completion_rate": completed_periods / total_periods if total_periods else 0.0,
        "current_period_done": current_done,
        "current_period_target": p.target_count,
        "current_period_completed": current_completed,
    }


def monthly_trend(
    habit: Habit, start: datetime.date, end: datetime.date
) -> list[dict]:
    """Completion count per calendar month spanned by ``[start, end]``.

    Mirrors the web history chart (``components.habit_history``): one entry per
    month, counting ticked days that fall in that month.
    """
    ticked = habit.ticked_days
    points: list[dict] = []
    cursor = start.replace(day=1)
    last = end.replace(day=1)
    while cursor <= last:
        count = sum(
            1 for d in ticked if d.year == cursor.year and d.month == cursor.month
        )
        points.append({"month": cursor.strftime("%Y-%m"), "count": count})
        cursor = cursor + relativedelta(months=1)
    return points


def heatmap_stats(
    habit: Habit, start: datetime.date, end: datetime.date
) -> dict:
    """Aggregate heatmap counts over ``[start, end]``.

    Built from the same engine as the heatmap, so ``done_days`` /
    ``period_done_days`` match the cells the UI colors. ``active_days`` is the
    number of days with any completion status; ``completion_rate`` is
    ``active_days / total_days``.
    """
    status_map = get_habit_date_completion(habit, start, end)

    done_days = period_done_days = active_days = 0
    for day, statuses in status_map.items():
        if not (start <= day <= end) or not statuses:
            continue
        active_days += 1
        if CStatus.DONE in statuses:
            done_days += 1
        if CStatus.PERIOD_DONE in statuses:
            period_done_days += 1

    total_days = (end - start).days + 1
    return {
        "date_start": start.isoformat(),
        "date_end": end.isoformat(),
        "done_days": done_days,
        "period_done_days": period_done_days,
        "active_days": active_days,
        "total_days": total_days,
        "completion_rate": active_days / total_days if total_days > 0 else 0.0,
    }


def habit_statistics(
    habit: Habit, start: datetime.date, end: datetime.date
) -> dict:
    """Compose the full per-habit statistics payload over ``[start, end]``."""
    return {
        "id": habit.id,
        "name": habit.name,
        "star": habit.star,
        "status": habit.status.value,
        "period": habit.period.to_dict() if habit.period else None,
        "streak": streak_summary(habit, start, end),
        "period_progress": period_summary(habit, start, end),
        "monthly_trend": monthly_trend(habit, start, end),
        "heatmap": heatmap_stats(habit, start, end),
    }
