import datetime
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from loguru import logger
from pydantic import BaseModel

from beaverhabits import views
from beaverhabits.app.db import User
from beaverhabits.app.dependencies import current_active_user
from beaverhabits.core.completions import CStatus, get_habit_date_completion
from beaverhabits.core.statistics import habit_statistics
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


def _parse_optional_date(value: str | None, date_fmt: str) -> datetime.date | None:
    """Parse a date string, returning None when empty; 400 on a bad format."""
    if not value:
        return None
    try:
        return datetime.datetime.strptime(value, date_fmt.strip()).date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format")


def _resolve_stats_range(
    date_start: str | None, date_end: str | None, date_fmt: str
) -> tuple[datetime.date, datetime.date]:
    """Resolve the stats date range, defaulting to the last 365 days.

    When ``date_end`` is omitted it defaults to today; when ``date_start`` is
    omitted it defaults to 364 days before the end (a one-year window, matching
    the web streak look-back).
    """
    start = _parse_optional_date(date_start, date_fmt)
    end = _parse_optional_date(date_end, date_fmt)
    if end is None:
        end = datetime.date.today()
    if start is None:
        start = end - datetime.timedelta(days=364)
    if start > end:
        raise HTTPException(
            status_code=400, detail="date_start cannot be after date_end"
        )
    return start, end


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


class StreakSummary(BaseModel):
    current: int
    longest: int
    total: int
    segments: list[int]


class PeriodProgress(BaseModel):
    period_type: str
    period_count: int
    target_count: int
    total_periods: int
    completed_periods: int
    completion_rate: float
    current_period_done: int
    current_period_target: int
    current_period_completed: bool


class MonthlyTrendPoint(BaseModel):
    month: str
    count: int


class HeatmapStats(BaseModel):
    date_start: str
    date_end: str
    done_days: int
    period_done_days: int
    active_days: int
    total_days: int
    completion_rate: float


class HabitPeriod(BaseModel):
    period_type: str
    period_count: int
    target_count: int


class HabitStats(BaseModel):
    id: str | int
    name: str
    star: bool
    status: str
    period: HabitPeriod | None
    streak: StreakSummary
    period_progress: PeriodProgress | None
    monthly_trend: list[MonthlyTrendPoint]
    heatmap: HeatmapStats


_STATS_SORT_KEYS = {
    "name": lambda s: s["name"].lower(),
    "current_streak": lambda s: s["streak"]["current"],
    "longest_streak": lambda s: s["streak"]["longest"],
    "total_completions": lambda s: s["streak"]["total"],
}


@api_router.get("/habits/stats", tags=["habits"], response_model=list[HabitStats])
async def get_habits_stats(
    status: HabitStatus = HabitStatus.ACTIVE,
    date_start: str | None = None,
    date_end: str | None = None,
    date_fmt: str = "%d-%m-%Y",
    sort_by: str = "name",
    sort: str = "asc",
    habit_list: HabitList = Depends(current_habit_list),
):
    start, end = _resolve_stats_range(date_start, date_end, date_fmt)

    if sort_by not in _STATS_SORT_KEYS:
        raise HTTPException(status_code=400, detail=f"Invalid sort_by: {sort_by}")
    if sort not in ("asc", "desc"):
        raise HTTPException(status_code=400, detail="Invalid sort value")

    habits = HabitListBuilder(habit_list).status(status).build()
    stats = [habit_statistics(habit, start, end) for habit in habits]
    stats.sort(key=_STATS_SORT_KEYS[sort_by], reverse=sort == "desc")

    return stats


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
    start = _parse_optional_date(date_start, date_fmt) or datetime.date.min
    end = _parse_optional_date(date_end, date_fmt) or datetime.date.max
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


@api_router.get(
    "/habits/{habit_id}/stats", tags=["habits"], response_model=HabitStats
)
async def get_habit_stats(
    habit_id: str,
    date_start: str | None = None,
    date_end: str | None = None,
    date_fmt: str = "%d-%m-%Y",
    user: User = Depends(current_active_user),
):
    start, end = _resolve_stats_range(date_start, date_end, date_fmt)
    habit = await views.get_user_habit(user, habit_id)
    return habit_statistics(habit, start, end)


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
