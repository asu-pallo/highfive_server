import math

from django.contrib.auth.models import User
from django.db import transaction
from django.utils.dateparse import parse_datetime

from .models import Workout, WorkoutMetrics, WorkoutMetricsAggregate


SAMPLE_COUNT = 50
ALGORITHM_VERSION = 1


def rebuild_workout_metrics(
    workout: Workout,
    route: list[dict],
    heart_rate: list[dict],
) -> WorkoutMetrics:
    """원본 경로·심박수를 상세 그래프용 50포인트 시리즈로 축약해 저장한다.

    페이스·심박수·케이던스·고도 시리즈를 만들고 year/all 누적 집계도 갱신한다.
    """
    route_points = _route_points(route)
    heart_points = _heart_points(heart_rate)
    values = {
        'paceSeries': _pace_series(route_points, workout.distanceMeters),
        'heartRateSeries': _time_series(
            heart_points, workout.startAt, workout.endAt
        ),
        'cadenceSeries': _cadence_series(route_points, workout),
        'elevationSeries': _route_value_series(route_points, 'altitude'),
        'splitSeries': _split_series(
            route_points, heart_points, workout.distanceMeters
        ),
        'heartRateZones': _heart_rate_zones(heart_points),
        'sampleCount': SAMPLE_COUNT,
        'algorithmVersion': ALGORITHM_VERSION,
    }
    metrics, created = WorkoutMetrics.objects.update_or_create(
        workout=workout,
        defaults=values,
    )
    update_metrics_aggregates(workout, metrics, rebuild=not created)
    return metrics


@transaction.atomic
def update_metrics_aggregates(
    workout: Workout,
    metrics: WorkoutMetrics,
    *,
    rebuild: bool = False,
) -> None:
    """동일 사용자의 병렬 업로드를 직렬화하고 year/all 집계를 갱신한다."""
    User.objects.select_for_update().get(pk=workout.user_id)
    year = workout.startAt.year
    if rebuild:
        _rebuild_aggregate(workout, WorkoutMetricsAggregate.Scope.ALL, None)
        _rebuild_aggregate(workout, WorkoutMetricsAggregate.Scope.YEAR, year)
        return
    _accumulate_aggregate(
        workout, metrics, WorkoutMetricsAggregate.Scope.ALL, None
    )
    _accumulate_aggregate(
        workout, metrics, WorkoutMetricsAggregate.Scope.YEAR, year
    )


def _aggregate_defaults():
    return {
        'workoutCount': 0,
        'distanceSum': 0,
        'distanceCount': 0,
        'durationSum': 0,
        'durationCount': 0,
        'paceSeriesSum': [],
        'paceSeriesCount': [],
        'heartRateSeriesSum': [],
        'heartRateSeriesCount': [],
        'cadenceSeriesSum': [],
        'cadenceSeriesCount': [],
        'elevationSeriesSum': [],
        'elevationSeriesCount': [],
        'splitSeriesSum': [],
        'splitSeriesCount': [],
        'heartRateZoneSecondsSum': [],
        'heartRateZoneSecondsCount': [],
        'sampleCount': SAMPLE_COUNT,
        'algorithmVersion': ALGORITHM_VERSION,
    }


def _aggregate(workout, scope, year):
    aggregate, _ = WorkoutMetricsAggregate.objects.get_or_create(
        user_id=workout.user_id,
        workoutKind=workout.kind,
        scope=scope,
        scopeYear=year,
        defaults=_aggregate_defaults(),
    )
    return aggregate


def _accumulate_aggregate(workout, metrics, scope, year):
    aggregate = _aggregate(workout, scope, year)
    _add_metrics(aggregate, workout, metrics)
    aggregate.save()


def _rebuild_aggregate(workout, scope, year):
    aggregate = _aggregate(workout, scope, year)
    for field, value in _aggregate_defaults().items():
        setattr(aggregate, field, value)
    queryset = WorkoutMetrics.objects.filter(
        workout__user_id=workout.user_id,
        workout__kind=workout.kind,
        workout__deletedAt__isnull=True,
    ).select_related('workout')
    if year is not None:
        queryset = queryset.filter(workout__startAt__year=year)
    for item in queryset:
        _add_metrics(aggregate, item.workout, item)
    aggregate.save()


def _add_metrics(aggregate, workout, metrics):
    aggregate.workoutCount += 1
    if workout.distanceMeters is not None:
        aggregate.distanceSum += float(workout.distanceMeters)
        aggregate.distanceCount += 1
    duration = max((workout.endAt - workout.startAt).total_seconds(), 0)
    if duration > 0:
        aggregate.durationSum += duration
        aggregate.durationCount += 1
    for name in ('pace', 'heartRate', 'cadence', 'elevation'):
        sums = list(getattr(aggregate, f'{name}SeriesSum'))
        counts = list(getattr(aggregate, f'{name}SeriesCount'))
        _add_series(sums, counts, getattr(metrics, f'{name}Series'))
        setattr(aggregate, f'{name}SeriesSum', sums)
        setattr(aggregate, f'{name}SeriesCount', counts)
    split_sums = list(aggregate.splitSeriesSum)
    split_counts = list(aggregate.splitSeriesCount)
    _add_split_series(split_sums, split_counts, metrics.splitSeries)
    aggregate.splitSeriesSum = split_sums
    aggregate.splitSeriesCount = split_counts
    zone_sums = list(aggregate.heartRateZoneSecondsSum)
    zone_counts = list(aggregate.heartRateZoneSecondsCount)
    _add_zone_series(zone_sums, zone_counts, metrics.heartRateZones)
    aggregate.heartRateZoneSecondsSum = zone_sums
    aggregate.heartRateZoneSecondsCount = zone_counts


def _add_series(sums, counts, values):
    for index, value in enumerate(values or []):
        while len(sums) <= index:
            sums.append(0.0)
            counts.append(0)
        if value is None:
            continue
        number = float(value)
        if not math.isfinite(number):
            continue
        sums[index] += number
        counts[index] += 1


def _add_split_series(sums, counts, values):
    fields = ('paceSecondsPerKm', 'elevationGainMeters', 'averageHeartRate')
    for index, split in enumerate(values or []):
        while len(sums) <= index:
            sums.append({field: 0.0 for field in fields})
            counts.append({field: 0 for field in fields})
        for field in fields:
            value = split.get(field)
            if value is None:
                continue
            number = float(value)
            if not math.isfinite(number):
                continue
            sums[index][field] = float(sums[index].get(field, 0)) + number
            counts[index][field] = int(counts[index].get(field, 0)) + 1


def _add_zone_series(sums, counts, values):
    if not values:
        return
    by_zone = {int(item['zone']): item for item in values}
    for zone in range(1, 6):
        while len(sums) < zone:
            sums.append(0.0)
            counts.append(0)
        item = by_zone.get(zone)
        if item is None or item.get('durationSeconds') is None:
            continue
        sums[zone - 1] += float(item['durationSeconds'])
        counts[zone - 1] += 1


def metrics_json(workout: Workout) -> dict:
    try:
        metrics = workout.metrics
    except WorkoutMetrics.DoesNotExist:
        return {}
    return {
        'sampleCount': metrics.sampleCount,
        'algorithmVersion': metrics.algorithmVersion,
        'paceSeries': metrics.paceSeries,
        'heartRateSeries': metrics.heartRateSeries,
        'cadenceSeries': metrics.cadenceSeries,
        'elevationSeries': metrics.elevationSeries,
        'splitSeries': metrics.splitSeries,
        'heartRateZones': metrics.heartRateZones,
    }


def _route_points(route):
    parsed = []
    for raw in route:
        try:
            timestamp = parse_datetime(str(raw['timestamp']))
            if timestamp is None:
                continue
            parsed.append({
                'timestamp': timestamp,
                'latitude': float(raw['latitude']),
                'longitude': float(raw['longitude']),
                'altitude': _optional_float(raw.get('altitude')),
                'speed': _optional_float(raw.get('speed')),
                'cadence': _optional_float(raw.get('cadence')),
            })
        except (KeyError, TypeError, ValueError):
            continue
    parsed.sort(key=lambda point: point['timestamp'])
    distance = 0.0
    previous = None
    for point in parsed:
        if previous is not None:
            distance += _haversine(previous, point)
        point['distance'] = distance
        previous = point
    return parsed


def _heart_points(points):
    parsed = []
    for raw in points:
        try:
            timestamp = parse_datetime(str(raw['timestamp']))
            bpm = float(raw['bpm'])
            if timestamp is not None and math.isfinite(bpm):
                parsed.append((timestamp, bpm))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(parsed, key=lambda point: point[0])


def _pace_series(points, workout_distance):
    if len(points) < 2 or points[-1]['distance'] <= 0:
        return []
    distance = float(workout_distance or points[-1]['distance'])
    values = []
    for index in range(SAMPLE_COUNT):
        start = _point_at_distance(
            points, points[-1]['distance'] * index / SAMPLE_COUNT
        )
        end = _point_at_distance(
            points, points[-1]['distance'] * (index + 1) / SAMPLE_COUNT
        )
        seconds = (end['timestamp'] - start['timestamp']).total_seconds()
        meters = distance / SAMPLE_COUNT
        values.append(None if seconds <= 0 or meters <= 0 else seconds / (meters / 1000))
    return [_rounded(value) for value in values]


def _time_series(points, started_at, ended_at):
    if not points:
        return []
    buckets = [[] for _ in range(SAMPLE_COUNT)]
    duration = max((ended_at - started_at).total_seconds(), 1)
    for timestamp, value in points:
        position = (timestamp - started_at).total_seconds() / duration
        index = min(max(math.floor(position * SAMPLE_COUNT), 0), SAMPLE_COUNT - 1)
        buckets[index].append(value)
    fallback = sum(value for _, value in points) / len(points)
    previous = fallback
    result = []
    for bucket in buckets:
        if bucket:
            previous = sum(bucket) / len(bucket)
        result.append(_rounded(previous))
    return result


def _cadence_series(points, workout):
    measured = [
        point['cadence']
        for point in points
        if point['cadence'] is not None and point['cadence'] > 0
    ]
    if measured:
        return [_rounded(value) for value in _resample(measured)]

    if not workout.steps or (workout.endAt - workout.startAt).total_seconds() <= 0:
        return []
    speeds = [point['speed'] for point in points if point['speed'] is not None and point['speed'] >= 0]
    if not speeds:
        return []
    average = workout.steps / ((workout.endAt - workout.startAt).total_seconds() / 60)
    mean_speed = sum(speeds) / len(speeds)
    return [
        _rounded(min(max(average * (speed / mean_speed if mean_speed > 0 else 1), 60), 220))
        for speed in _resample(speeds)
    ]


def _route_value_series(points, key):
    values = [point[key] for point in points if point[key] is not None]
    return [_rounded(value) for value in _resample(values)] if values else []


def _split_series(points, heart_points, workout_distance):
    if len(points) < 2 or points[-1]['distance'] <= 0:
        return []
    distance = float(workout_distance or points[-1]['distance'])
    if distance < 100:
        return []
    scale = distance / points[-1]['distance']
    result = []
    for index in range(math.ceil(distance / 1000)):
        start_meters = index * 1000.0
        end_meters = min((index + 1) * 1000.0, distance)
        split_meters = end_meters - start_meters
        if split_meters < 100:
            continue
        start = _point_at_distance(points, start_meters / scale)
        end = _point_at_distance(points, end_meters / scale)
        seconds = (end['timestamp'] - start['timestamp']).total_seconds()
        if seconds <= 0:
            continue
        altitudes = [
            point['altitude']
            for point in points
            if start_meters <= point['distance'] * scale <= end_meters
            and point['altitude'] is not None
        ]
        gain = sum(
            max(current - previous, 0)
            for previous, current in zip(altitudes, altitudes[1:])
        )
        bpms = [
            bpm
            for timestamp, bpm in heart_points
            if start['timestamp'] <= timestamp <= end['timestamp']
        ]
        result.append({
            'distanceMeters': round(split_meters),
            'paceSecondsPerKm': _rounded(seconds / (split_meters / 1000)),
            'elevationGainMeters': _rounded(gain) if altitudes else None,
            'averageHeartRate': _rounded(sum(bpms) / len(bpms)) if bpms else None,
        })
    return result


def _heart_rate_zones(points):
    if not points:
        return []
    maximum = max(value for _, value in points)
    if maximum <= 0:
        return []
    seconds = [0.0] * 5
    for index, (timestamp, bpm) in enumerate(points):
        duration = (
            (points[index + 1][0] - timestamp).total_seconds()
            if index + 1 < len(points)
            else 1.0
        )
        ratio = bpm / maximum
        zone = 4 if ratio >= .9 else 3 if ratio >= .8 else 2 if ratio >= .7 else 1 if ratio >= .6 else 0
        seconds[zone] += min(max(duration, 0), 30)
    total = sum(seconds)
    return [
        {
            'zone': zone + 1,
            'durationSeconds': _rounded(seconds[zone]),
            'ratio': _rounded(seconds[zone] / total) if total > 0 else 0,
        }
        for zone in range(5)
    ]


def _resample(values):
    if not values:
        return []
    if len(values) == 1:
        return [values[0]] * SAMPLE_COUNT
    result = []
    for index in range(SAMPLE_COUNT):
        position = index * (len(values) - 1) / (SAMPLE_COUNT - 1)
        lower, upper = math.floor(position), math.ceil(position)
        if lower == upper:
            result.append(values[lower])
        else:
            ratio = position - lower
            result.append(values[lower] + (values[upper] - values[lower]) * ratio)
    return result


def _point_at_distance(points, target):
    if target <= 0:
        return points[0]
    for index in range(1, len(points)):
        before, after = points[index - 1], points[index]
        if after['distance'] < target:
            continue
        span = after['distance'] - before['distance']
        ratio = 0 if span <= 0 else (target - before['distance']) / span
        return {
            'timestamp': before['timestamp'] + (after['timestamp'] - before['timestamp']) * ratio,
        }
    return points[-1]


def _haversine(a, b):
    radius = 6_371_008.8
    p1, p2 = math.radians(a['latitude']), math.radians(b['latitude'])
    dp = math.radians(b['latitude'] - a['latitude'])
    dl = math.radians(b['longitude'] - a['longitude'])
    value = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    value = min(max(value, 0), 1)
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def _optional_float(value):
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _rounded(value):
    return None if value is None else round(float(value), 3)
