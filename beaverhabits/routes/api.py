import datetime
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from loguru import logger
from pydantic import BaseModel

from beaverhabits import views
from beaverhabits.app.db import User
from beaverhabits.app.dependencies import current_active_user
from beaverhabits.core.completions import CStatus, get_habit_date_completion
from beaverhabits.core.stats import (
    HabitStatsBundle,
    HeatmapStats,
    MonthlyTrendEntry,
    PeriodProgress,
    PeriodSummary,
    StreakInfo,
    StreakSummary,
    compute_habit_stats_bundle,
    compute_heatmap_stats,
    compute_monthly_trend,
    compute_period_summary,
    compute_streak_summary,
)
from beaverhabits.storage.storage import (
    Habit,
    HabitFrequency,
    HabitList,
    HabitListBuilder,
    HabitStatus,
)

api_router = APIRouter()


async def current_habit_list(user: User = Depends(current_active_user)) -> HabitList:
    habit_list = await views.get_user_habit_list(user)
    if not habit_list:
        raise HTTPException(status_code=404, detail="No habits found")
    return habit_list


class HabitListMeta(BaseModel):
    order: list[str] | None = None


@api_router.get("/habits/meta", tags=["habits"])
async def get_habits_meta(
    habit_list: HabitList = Depends(current_habit_list),
):
    return HabitListMeta(order=habit_list.order)


@api_router.put("/habits/meta", tags=["habits"])
async def put_habits_meta(
    meta: HabitListMeta,
    habit_list: HabitList = Depends(current_habit_list),
):
    if meta.order is not None:
        habit_list.order = meta.order
    return {"order": habit_list.order}


@api_router.get("/habits", tags=["habits"])
async def get_habits(
    status: HabitStatus = HabitStatus.ACTIVE,
    habit_list: HabitList = Depends(current_habit_list),
):
    habits = HabitListBuilder(habit_list).status(status).build()
    return [{"id": x.id, "name": x.name} for x in habits]


class CreateHabit(BaseModel):
    name: str


@api_router.post("/habits", tags=["habits"])
async def post_habits(
    habit: CreateHabit,
    user: User = Depends(current_active_user),
):
    habit_list = await views.get_or_create_user_habit_list(
        user, views.dummy_empty_habit_list()
    )

    id = await habit_list.add(habit.name)
    logger.info(f"Created new habit {id} for user {user.email}")

    return {"id": id, "name": habit.name}


@api_router.get("/habits/{habit_id}", tags=["habits"])
async def get_habit_detail(
    habit_id: str,
    user: User = Depends(current_active_user),
):
    habit = await views.get_user_habit(user, habit_id)
    return format_json_response(habit)


class UpdateHabit(BaseModel):
    class UpdateHabitPeriod(BaseModel):
        period_type: Literal["D", "W", "M", "Y"]
        period_count: int
        target_count: int

    name: str | None = None
    star: bool | None = None
    status: HabitStatus | None = None
    period: UpdateHabitPeriod | None = None
    tags: list[str] | None = None


@api_router.put("/habits/{habit_id}", tags=["habits"])
async def put_habit(
    habit_id: str,
    habit: UpdateHabit,
    user: User = Depends(current_active_user),
):
    existing_habit = await views.get_user_habit(user, habit_id)
    if habit.name is not None:
        existing_habit.name = habit.name
    if habit.star is not None:
        existing_habit.star = habit.star
    if habit.status is not None:
        existing_habit.status = habit.status
    if habit.period is not None:
        existing_habit.period = HabitFrequency(
            target_count=habit.period.target_count,
            period_count=habit.period.period_count,
            period_type=habit.period.period_type,
        )
    if habit.tags is not None:
        existing_habit.tags = habit.tags

    return format_json_response(existing_habit)


@api_router.delete("/habits/{habit_id}", tags=["habits"])
async def delete_habit(
    habit_id: str,
    user: User = Depends(current_active_user),
):
    habit = await views.get_user_habit(user, habit_id)
    await views.remove_user_habit(user, habit)
    return format_json_response(habit)


@api_router.get("/habits/{habit_id}/completions", tags=["habits"])
async def get_habit_completions(
    habit_id: str,
    status: str | None = None,
    date_fmt: str = "%d-%m-%Y",
    date_start: str | None = None,
    date_end: str | None = None,
    limit: int | None = 10,
    sort="asc",
    user: User = Depends(current_active_user),
):
    # Parse date range
    start, end = datetime.date.min, datetime.date.max
    if date_start:
        try:
            start = datetime.datetime.strptime(date_start, date_fmt.strip()).date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format")
    if date_end:
        try:
            end = datetime.datetime.strptime(date_end, date_fmt.strip()).date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format")
    if start > end:
        raise HTTPException(
            status_code=400, detail="date_start cannot be after date_end"
        )

    # Parse status filter
    cstatus_list = [CStatus.DONE]
    if status:
        cstatus_list = []
        for s in status.split(","):
            try:
                cstatus_list.append(CStatus[s.strip().upper()])
            except KeyError:
                raise HTTPException(status_code=400, detail=f"Invalid status: {s}")

    habit = await views.get_user_habit(user, habit_id)
    status_map = get_habit_date_completion(habit, start, end)
    ticked_days = [
        day
        for day, stat in status_map.items()
        if any(s in stat for s in cstatus_list) and start <= day <= end
    ]

    if sort not in ("asc", "desc"):
        raise HTTPException(status_code=400, detail="Invalid sort value")
    ticked_days = sorted(ticked_days, reverse=sort == "desc")

    if limit:
        ticked_days = ticked_days[:limit]

    return [x.strftime(date_fmt) for x in ticked_days]


class Tick(BaseModel):
    done: bool
    date: str
    text: str | None = None
    date_fmt: str = "%d-%m-%Y"


@api_router.post("/habits/{habit_id}/completions", tags=["habits"])
async def put_habit_completions(
    habit_id: str,
    tick: Tick,
    user: User = Depends(current_active_user),
):
    try:
        day = datetime.datetime.strptime(tick.date, tick.date_fmt.strip()).date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format")

    habit = await views.get_user_habit(user, habit_id)
    await habit.tick(day, tick.done, tick.text)
    return {"day": day.strftime(tick.date_fmt), "done": tick.done}


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

DATE_FMT_STATS = "%Y-%m-%d"


def _parse_stats_date(value: str | None, default: datetime.date) -> datetime.date:
    if not value:
        return default
    try:
        return datetime.datetime.strptime(value, DATE_FMT_STATS).date()
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date format: {value!r}, expected YYYY-MM-DD",
        )


def _validate_date_range(start: datetime.date, end: datetime.date) -> None:
    if start > end:
        raise HTTPException(
            status_code=400, detail="date_start cannot be after date_end"
        )


def _serialize_streak(s: StreakSummary) -> dict:
    return {
        "current_streak": s.current_streak,
        "longest_streak": s.longest_streak,
        "recent_streaks": [
            {
                "start": r.start.isoformat(),
                "end": r.end.isoformat(),
                "length": r.length,
            }
            for r in s.recent_streaks
        ],
    }


def _serialize_period(p: PeriodSummary) -> dict:
    return {
        "period_type": p.period_type,
        "period_count": p.period_count,
        "target_count": p.target_count,
        "periods": [
            {
                "period_start": pp.period_start.isoformat(),
                "period_end": pp.period_end.isoformat(),
                "done_count": pp.done_count,
                "target_count": pp.target_count,
                "completed": pp.completed,
            }
            for pp in p.periods
        ],
        "overall_completion_rate": p.overall_completion_rate,
    }


def _serialize_monthly_trend(entries: list[MonthlyTrendEntry]) -> dict:
    return {
        "months": [
            {
                "year": e.year,
                "month": e.month,
                "label": e.label,
                "count": e.count,
            }
            for e in entries
        ]
    }


def _serialize_heatmap(h: HeatmapStats) -> dict:
    return {
        "start": h.start.isoformat(),
        "end": h.end.isoformat(),
        "total_days": h.total_days,
        "completed_days": h.completed_days,
        "completion_rate": h.completion_rate,
    }


def _serialize_bundle(b: HabitStatsBundle) -> dict:
    return {
        "habit_id": b.habit_id,
        "habit_name": b.habit_name,
        "streak": _serialize_streak(b.streak),
        "period": _serialize_period(b.period) if b.period else None,
        "monthly_trend": _serialize_monthly_trend(b.monthly_trend),
        "heatmap": _serialize_heatmap(b.heatmap),
    }


# ---------------------------------------------------------------------------
# Statistics endpoints — single habit
# ---------------------------------------------------------------------------


@api_router.get("/habits/{habit_id}/stats/streaks", tags=["statistics"])
async def get_habit_streak_stats(
    habit_id: str,
    user: User = Depends(current_active_user),
):
    habit = await views.get_user_habit(user, habit_id)
    today = datetime.date.today()
    summary = compute_streak_summary(habit, today)
    return _serialize_streak(summary)


@api_router.get("/habits/{habit_id}/stats/periods", tags=["statistics"])
async def get_habit_period_stats(
    habit_id: str,
    date_start: str | None = None,
    date_end: str | None = None,
    user: User = Depends(current_active_user),
):
    habit = await views.get_user_habit(user, habit_id)
    today = datetime.date.today()
    start = _parse_stats_date(date_start, today - datetime.timedelta(days=90))
    end = _parse_stats_date(date_end, today)
    _validate_date_range(start, end)

    summary = compute_period_summary(habit, start, end)
    if summary is None:
        raise HTTPException(
            status_code=404, detail="No period configured for this habit"
        )
    return _serialize_period(summary)


@api_router.get("/habits/{habit_id}/stats/monthly-trend", tags=["statistics"])
async def get_habit_monthly_trend(
    habit_id: str,
    total_months: int = Query(default=13, ge=1, le=120),
    user: User = Depends(current_active_user),
):
    habit = await views.get_user_habit(user, habit_id)
    today = datetime.date.today()
    entries = compute_monthly_trend(habit, today, total_months)
    return _serialize_monthly_trend(entries)


@api_router.get("/habits/{habit_id}/stats/heatmap", tags=["statistics"])
async def get_habit_heatmap_stats(
    habit_id: str,
    date_start: str | None = None,
    date_end: str | None = None,
    user: User = Depends(current_active_user),
):
    habit = await views.get_user_habit(user, habit_id)
    today = datetime.date.today()
    start = _parse_stats_date(date_start, today - datetime.timedelta(days=90))
    end = _parse_stats_date(date_end, today)
    _validate_date_range(start, end)

    stats = compute_heatmap_stats(habit, start, end)
    return _serialize_heatmap(stats)


# ---------------------------------------------------------------------------
# Statistics endpoint — batch (all habits)
# ---------------------------------------------------------------------------


@api_router.get("/habits/stats/batch", tags=["statistics"])
async def get_batch_habit_stats(
    status: HabitStatus = HabitStatus.ACTIVE,
    date_start: str | None = None,
    date_end: str | None = None,
    total_months: int = Query(default=13, ge=1, le=120),
    sort_by: Literal["name", "completion_rate", "streak_length", "longest_streak"] = (
        "name"
    ),
    sort_order: Literal["asc", "desc"] = "asc",
    habit_list: HabitList = Depends(current_habit_list),
):
    today = datetime.date.today()
    start = _parse_stats_date(date_start, today - datetime.timedelta(days=90))
    end = _parse_stats_date(date_end, today)
    _validate_date_range(start, end)

    habits = HabitListBuilder(habit_list).status(status).build()

    bundles: list[HabitStatsBundle] = []
    for habit in habits:
        bundle = compute_habit_stats_bundle(habit, today, start, end, total_months)
        bundles.append(bundle)

    # Sort
    reverse = sort_order == "desc"
    if sort_by == "name":
        bundles.sort(key=lambda b: b.habit_name.lower(), reverse=reverse)
    elif sort_by == "completion_rate":
        bundles.sort(key=lambda b: b.heatmap.completion_rate, reverse=reverse)
    elif sort_by == "streak_length":
        bundles.sort(key=lambda b: b.streak.current_streak, reverse=reverse)
    elif sort_by == "longest_streak":
        bundles.sort(key=lambda b: b.streak.longest_streak, reverse=reverse)

    return {"habits": [_serialize_bundle(b) for b in bundles]}


def format_json_response(habit: Habit) -> dict:
    return {
        "id": habit.id,
        "name": habit.name,
        "star": habit.star,
        "records": habit.records,
        "status": habit.status,
        "period": habit.period,
        "tags": habit.tags,
    }


def init_api_routes(app: FastAPI) -> None:
    app.include_router(api_router, prefix="/api/v1")
