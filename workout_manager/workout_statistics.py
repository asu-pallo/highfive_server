import math

from django.db.models import Count, F, IntegerField, OuterRef, Subquery, Window
from django.db.models.functions import Coalesce, RowNumber
from django.utils.dateparse import parse_datetime

from .models import (
    Workout,
    WorkoutDistanceRecord,
    WorkoutMetricsAggregate,
)


DISTANCE_TARGETS_METERS = (1_000, 5_000, 10_000, 20_000, 40_000)
ALGORITHM_VERSION = 1
PR_HISTORY_LIMIT = 30
DISTRIBUTION_LIMIT = 30


def rebuild_distance_records(workout: Workout, route: list[dict]) -> int:
    """원본 경로에서 1·5·10·20·40km별 가장 빠른 연속 구간을 다시 만든다."""
    WorkoutDistanceRecord.objects.filter(workout=workout).delete()
    if workout.kind != 'running' or len(route) < 2:
        return 0

    points = _cumulative_points(route)
    if len(points) < 2:
        return 0

    records = []
    for target in DISTANCE_TARGETS_METERS:
        if points[-1][0] < target:
            continue
        effort = _fastest_effort(points, target)
        if effort is None:
            continue
        duration_ms, started_at, ended_at = effort
        records.append(WorkoutDistanceRecord(
            workout=workout,
            user=workout.user,
            distanceMeters=target,
            durationMilliseconds=duration_ms,
            startedAt=started_at,
            endedAt=ended_at,
            algorithmVersion=ALGORITHM_VERSION,
        ))
    WorkoutDistanceRecord.objects.bulk_create(records)
    return len(records)


def workout_statistics(workout: Workout) -> dict:
    """거리별 최근 PR 이력·전체 순위와 최근 30개 기록 분포를 반환한다."""
    better_records = (
        WorkoutDistanceRecord.objects.filter(
            user_id=OuterRef('user_id'),
            distanceMeters=OuterRef('distanceMeters'),
            workout__kind='running',
            workout__deletedAt__isnull=True,
            durationMilliseconds__lt=OuterRef('durationMilliseconds'),
        )
        .values('user_id', 'distanceMeters')
        .annotate(total=Count('id'))
        .values('total')
    )
    rows = list(
        workout.distanceRecords.annotate(
            betterRecordCount=Coalesce(
                Subquery(better_records, output_field=IntegerField()),
                0,
            ),
        ).order_by('distanceMeters')
    )

    target_distances = [row.distanceMeters for row in rows]
    history_by_distance = {distance: [] for distance in target_distances}
    if target_distances:
        history_rows = (
            WorkoutDistanceRecord.objects.filter(
                user=workout.user,
                distanceMeters__in=target_distances,
                workout__kind='running',
                workout__deletedAt__isnull=True,
            )
            .exclude(workout=workout)
            .annotate(
                recentRank=Window(
                    expression=RowNumber(),
                    partition_by=[F('distanceMeters')],
                    order_by=[
                        F('workout__startAt').desc(),
                        F('workout_id').desc(),
                    ],
                ),
            )
            .filter(recentRank__lte=PR_HISTORY_LIMIT - 1)
            .order_by('distanceMeters', '-workout__startAt', '-workout_id')
        )
        for item in history_rows:
            history_by_distance[item.distanceMeters].append(item)

    pr = []
    for current in rows:
        history = history_by_distance[current.distanceMeters]
        pr.append({
            'distanceMeters': current.distanceMeters,
            'durationMilliseconds': current.durationMilliseconds,
            'paceSecondsPerKm': current.durationMilliseconds / 1000 / (current.distanceMeters / 1000),
            'rank': current.betterRecordCount + 1,
            'records': [
                {
                    'workoutId': item.workout_id,
                    'durationMilliseconds': item.durationMilliseconds,
                }
                for item in history
            ],
        })

    distribution_rows = list(
        Workout.objects.filter(
            user=workout.user,
            kind='running',
            deletedAt__isnull=True,
            distanceMeters__gt=0,
        ).order_by('-startAt', '-id')[:DISTRIBUTION_LIMIT]
    )
    distribution = []
    for item in distribution_rows:
        seconds = max((item.endAt - item.startAt).total_seconds(), 0)
        if seconds <= 0 or not item.distanceMeters:
            continue
        distribution.append({
            'workoutId': item.id,
            'distanceMeters': item.distanceMeters,
            'paceSecondsPerKm': seconds / (item.distanceMeters / 1000),
            'isCurrent': item.id == workout.id,
        })
    return {'pr': pr, 'distribution': distribution}


def workout_comparison(
    workout: Workout,
    preset: str,
    comparison_workout_id: int | None = None,
    comparison_year: int | None = None,
) -> dict | None:
    """현재 운동보다 앞선 동일 종목 기록을 비교 그래프용으로 반환한다."""
    if preset in ('year', 'all'):
        return _aggregate_comparison(workout, preset, comparison_year)

    candidates = (
        Workout.objects.filter(
            user=workout.user,
            kind=workout.kind,
            deletedAt__isnull=True,
            startAt__lt=workout.startAt,
            metrics__isnull=False,
        )
        .exclude(pk=workout.pk)
        .select_related('metrics')
        .order_by('-startAt', '-id')
    )

    if preset == 'workout':
        if comparison_workout_id is None:
            return None
        candidates = candidates.filter(pk=comparison_workout_id)[:1]
        title = None
    elif preset == 'previous':
        candidates = candidates[:1]
        title = '직전 기록'
    elif preset == 'recent5':
        candidates = candidates[:5]
        title = '최근 5회 평균'
    elif preset == 'recent30':
        candidates = candidates[:30]
        title = '최근 30회 평균'
    else:
        return None

    workouts = list(candidates)
    if not workouts:
        return None
    if title is None:
        selected = workouts[0]
        local = selected.startAt
        title = f'{local.month}월 {local.day}일 {selected.kind}'

    return {
        'id': (
            f'workout:{workouts[0].id}'
            if preset == 'workout'
            else f'{preset}:{workout.id}'
        ),
        'title': title,
        'subtitle': f'{len(workouts)}개 기록',
        'occurredAt': workouts[0].startAt,
        'workoutCount': len(workouts),
        'distanceMeters': _average_number(
            [item.distanceMeters for item in workouts]
        ),
        'durationSeconds': _average_number([
            max((item.endAt - item.startAt).total_seconds(), 0)
            for item in workouts
        ]),
        'metrics': {
            'paceSeries': _average_series(
                [item.metrics.paceSeries for item in workouts]
            ),
            'heartRateSeries': _average_series(
                [item.metrics.heartRateSeries for item in workouts]
            ),
            'cadenceSeries': _average_series(
                [item.metrics.cadenceSeries for item in workouts]
            ),
            'elevationSeries': _average_series(
                [item.metrics.elevationSeries for item in workouts]
            ),
            'splitSeries': _average_splits(
                [item.metrics.splitSeries for item in workouts]
            ),
            'heartRateZones': _average_zones(
                [item.metrics.heartRateZones for item in workouts]
            ),
        },
    }


def _aggregate_comparison(workout, preset, comparison_year):
    filters = {
        'user_id': workout.user_id,
        'workoutKind': workout.kind,
        'scope': preset,
    }
    if preset == 'year':
        if comparison_year is None:
            return None
        filters['scopeYear'] = comparison_year
        title = f'{comparison_year}년 평균'
    else:
        filters['scopeYear__isnull'] = True
        title = '전체 평균'
    aggregate = WorkoutMetricsAggregate.objects.filter(**filters).first()
    if aggregate is None or aggregate.workoutCount == 0:
        return None
    return {
        'id': f'{preset}:{comparison_year or "all"}',
        'title': title,
        'subtitle': f'{aggregate.workoutCount}개 기록',
        'occurredAt': aggregate.updatedAt,
        'workoutCount': aggregate.workoutCount,
        'distanceMeters': _aggregate_number(
            aggregate.distanceSum, aggregate.distanceCount
        ),
        'durationSeconds': _aggregate_number(
            aggregate.durationSum, aggregate.durationCount
        ),
        'metrics': {
            'paceSeries': _aggregate_series(
                aggregate.paceSeriesSum, aggregate.paceSeriesCount
            ),
            'heartRateSeries': _aggregate_series(
                aggregate.heartRateSeriesSum,
                aggregate.heartRateSeriesCount,
            ),
            'cadenceSeries': _aggregate_series(
                aggregate.cadenceSeriesSum, aggregate.cadenceSeriesCount
            ),
            'elevationSeries': _aggregate_series(
                aggregate.elevationSeriesSum,
                aggregate.elevationSeriesCount,
            ),
            'splitSeries': _aggregate_splits(
                aggregate.splitSeriesSum,
                aggregate.splitSeriesCount,
            ),
            'heartRateZones': _aggregate_zones(
                aggregate.heartRateZoneSecondsSum,
                aggregate.heartRateZoneSecondsCount,
            ),
        },
    }


def _aggregate_number(total, count):
    return round(float(total) / count, 3) if count else None


def _aggregate_series(sums, counts):
    length = max(len(sums), len(counts))
    return [
        round(float(sums[index]) / counts[index], 3)
        if index < len(sums) and index < len(counts) and counts[index]
        else None
        for index in range(length)
    ]


def _average_number(values):
    usable = [float(value) for value in values if value is not None]
    return round(sum(usable) / len(usable), 3) if usable else None


def _average_series(series_list):
    usable = [series for series in series_list if series]
    if not usable:
        return []
    length = min(len(series) for series in usable)
    result = []
    for index in range(length):
        values = [
            float(series[index])
            for series in usable
            if series[index] is not None
        ]
        result.append(
            round(sum(values) / len(values), 3) if values else None
        )
    return result


def _average_splits(series_list):
    fields = ('paceSecondsPerKm', 'elevationGainMeters', 'averageHeartRate')
    usable = [series for series in series_list if series]
    if not usable:
        return []
    result = []
    for index in range(max(len(series) for series in usable)):
        rows = [series[index] for series in usable if index < len(series)]
        distance_values = [
            float(row['distanceMeters'])
            for row in rows
            if row.get('distanceMeters') is not None
        ]
        item = {
            'distanceMeters': (
                round(sum(distance_values) / len(distance_values))
                if distance_values else min((index + 1) * 1000, 1000)
            ),
        }
        for field in fields:
            values = [
                float(row[field])
                for row in rows
                if row.get(field) is not None
            ]
            item[field] = round(sum(values) / len(values), 3) if values else None
        result.append(item)
    return result


def _average_zones(series_list):
    usable = [series for series in series_list if series]
    if not usable:
        return []
    duration_by_zone = []
    for zone in range(1, 6):
        values = [
            float(item.get('durationSeconds', 0))
            for series in usable
            for item in series
            if int(item.get('zone', 0)) == zone
        ]
        duration_by_zone.append(sum(values) / len(values) if values else 0)
    return _zones_from_durations(duration_by_zone)


def _aggregate_splits(sums, counts):
    result = []
    for index in range(max(len(sums), len(counts))):
        sum_item = sums[index] if index < len(sums) else {}
        count_item = counts[index] if index < len(counts) else {}
        item = {'distanceMeters': 1000}
        for field in ('paceSecondsPerKm', 'elevationGainMeters', 'averageHeartRate'):
            count = int(count_item.get(field, 0))
            item[field] = (
                round(float(sum_item.get(field, 0)) / count, 3)
                if count else None
            )
        result.append(item)
    return result


def _aggregate_zones(sums, counts):
    durations = [
        float(sums[index]) / counts[index]
        if index < len(sums) and index < len(counts) and counts[index]
        else 0
        for index in range(max(len(sums), len(counts)))
    ]
    return _zones_from_durations(durations)


def _zones_from_durations(durations):
    total = sum(durations)
    return [
        {
            'zone': index + 1,
            'durationSeconds': round(duration, 3),
            'ratio': round(duration / total, 3) if total > 0 else 0,
        }
        for index, duration in enumerate(durations)
    ]


def _cumulative_points(route: list[dict]):
    parsed = []
    for raw in route:
        try:
            timestamp = parse_datetime(str(raw['timestamp']))
            if timestamp is None:
                continue
            parsed.append((
                float(raw['latitude']),
                float(raw['longitude']),
                timestamp,
            ))
        except (KeyError, TypeError, ValueError):
            continue
    parsed.sort(key=lambda point: point[2])
    if not parsed:
        return []
    result = [(0.0, parsed[0][2])]
    total = 0.0
    previous = parsed[0]
    for point in parsed[1:]:
        distance = _haversine(previous[0], previous[1], point[0], point[1])
        if math.isfinite(distance) and distance >= 0:
            total += distance
            result.append((total, point[2]))
        previous = point
    return result


def _fastest_effort(points, target):
    best = None
    end_index = 1
    for start_index, (start_distance, started_at) in enumerate(points[:-1]):
        wanted = start_distance + target
        end_index = max(end_index, start_index + 1)
        while end_index < len(points) and points[end_index][0] < wanted:
            end_index += 1
        if end_index >= len(points):
            break
        before_distance, before_at = points[end_index - 1]
        after_distance, after_at = points[end_index]
        span = after_distance - before_distance
        ratio = 0.0 if span <= 0 else (wanted - before_distance) / span
        ended_at = before_at + (after_at - before_at) * ratio
        duration_ms = round((ended_at - started_at).total_seconds() * 1000)
        if duration_ms <= 0:
            continue
        if best is None or duration_ms < best[0]:
            best = (duration_ms, started_at, ended_at)
    return best


def _haversine(lat1, lon1, lat2, lon2):
    radius = 6_371_008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    value = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    value = min(max(value, 0.0), 1.0)
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))
