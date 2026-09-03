from datetime import datetime, time, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Count, Sum
from django.db.models.functions import Coalesce

from .models import UserWeeklyWorkoutStat, Workout


def user_timezone(user):
    """프로필의 IANA 시간대를 반환하고 값이 없거나 잘못됐으면 UTC를 사용한다."""
    name = getattr(getattr(user, 'profile', None), 'timezone', '')
    try:
        return ZoneInfo(name) if name else dt_timezone.utc
    except ZoneInfoNotFoundError:
        return dt_timezone.utc


def week_start_for(value, user):
    """운동 시각을 사용자 현지 월요일 날짜로 변환한다."""
    local = value.astimezone(user_timezone(user))
    return local.date() - timedelta(days=local.weekday())


def rebuild_weekly_stat(user, week_start, workout_kind):
    """한 주를 원본 Workout에서 다시 합산해 재업로드에도 중복되지 않게 저장한다."""
    zone = user_timezone(user)
    local_start = datetime.combine(week_start, time.min, tzinfo=zone)
    local_end = local_start + timedelta(days=7)
    totals = Workout.objects.filter(
        user=user,
        kind=workout_kind,
        deletedAt__isnull=True,
        startAt__gte=local_start.astimezone(dt_timezone.utc),
        startAt__lt=local_end.astimezone(dt_timezone.utc),
    ).aggregate(
        distance=Coalesce(Sum('distanceMeters'), 0.0),
        count=Count('id'),
    )
    workouts = Workout.objects.filter(
        user=user,
        kind=workout_kind,
        deletedAt__isnull=True,
        startAt__gte=local_start.astimezone(dt_timezone.utc),
        startAt__lt=local_end.astimezone(dt_timezone.utc),
    ).only('startAt', 'endAt')
    duration = round(sum(
        max((item.endAt - item.startAt).total_seconds(), 0) for item in workouts
    ))
    UserWeeklyWorkoutStat.objects.update_or_create(
        user=user,
        weekStart=week_start,
        workoutKind=workout_kind,
        defaults={
            'distanceMeters': float(totals['distance']),
            'durationSeconds': duration,
            'workoutCount': totals['count'],
        },
    )
