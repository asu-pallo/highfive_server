from dataclasses import dataclass
from datetime import datetime

import h3
from django.db import transaction
from django.utils.dateparse import parse_datetime
from psycopg.types.range import Range

from .models import SpatialIndexVersion, TrajectorySegment, Workout


H3_RESOLUTION = 11
H3_ALGORITHM_VERSION = 1


@dataclass(frozen=True)
class _CellVisit:
    cell_id: str
    entered_at: datetime
    exited_at: datetime


def ensure_h3_segments(workout: Workout, route: list[dict]) -> int:
    """기존 원본이 동일해도 아직 인덱싱되지 않은 운동은 한 번 생성한다."""
    if not route:
        return 0
    index_version = _active_h3_version()
    existing = TrajectorySegment.objects.filter(
        workout=workout,
        indexVersion=index_version,
    ).count()
    return existing or rebuild_h3_segments(workout, route, index_version=index_version)


def rebuild_h3_segments(
    workout: Workout,
    route: list[dict],
    *,
    index_version: SpatialIndexVersion | None = None,
) -> int:
    """원본 경로를 H3 연속 체류 구간으로 바꾸고 해당 운동의 인덱스를 교체한다."""
    index_version = index_version or _active_h3_version()
    visits = _build_cell_visits(route, workout.endAt)
    visits = [visit for visit in visits if visit.entered_at < visit.exited_at]
    segments = [
        TrajectorySegment(
            workout=workout,
            user=workout.user,
            indexVersion=index_version,
            sequence=sequence,
            cellId=visit.cell_id,
            period=Range(visit.entered_at, visit.exited_at, bounds='[)'),
        )
        for sequence, visit in enumerate(visits)
    ]

    with transaction.atomic():
        TrajectorySegment.objects.filter(
            workout=workout,
            indexVersion=index_version,
        ).delete()
        TrajectorySegment.objects.bulk_create(segments)
    return len(segments)


def _active_h3_version() -> SpatialIndexVersion:
    version, _ = SpatialIndexVersion.objects.get_or_create(
        indexType='h3',
        algorithmVersion=H3_ALGORITHM_VERSION,
        defaults={
            'parameters': {'resolution': H3_RESOLUTION},
            'isActive': True,
        },
    )
    return version


def _build_cell_visits(route: list[dict], workout_end: datetime) -> list[_CellVisit]:
    if not route:
        return []

    points = sorted(
        (
            parse_datetime(str(point['timestamp'])),
            float(point['latitude']),
            float(point['longitude']),
        )
        for point in route
    )
    timed_cells: list[tuple[str, datetime]] = []
    for index, (recorded_at, latitude, longitude) in enumerate(points):
        cell = h3.latlng_to_cell(latitude, longitude, H3_RESOLUTION)
        if index == 0:
            timed_cells.append((cell, recorded_at))
            continue

        previous_at, previous_latitude, previous_longitude = points[index - 1]
        previous_cell = h3.latlng_to_cell(
            previous_latitude, previous_longitude, H3_RESOLUTION
        )
        path = _grid_path(previous_cell, cell)
        duration = recorded_at - previous_at
        for path_index, path_cell in enumerate(path[1:], start=1):
            entered_at = previous_at + duration * (path_index / (len(path) - 1))
            if timed_cells[-1][0] != path_cell:
                timed_cells.append((path_cell, entered_at))

    visits = []
    final_exit = max(points[-1][0], workout_end)
    for index, (cell, entered_at) in enumerate(timed_cells):
        exited_at = (
            timed_cells[index + 1][1]
            if index + 1 < len(timed_cells)
            else final_exit
        )
        visits.append(_CellVisit(cell, entered_at, exited_at))
    return visits


def _grid_path(start_cell: str, end_cell: str) -> list[str]:
    if start_cell == end_cell:
        return [start_cell]
    try:
        return h3.grid_path_cells(start_cell, end_cell)
    except h3.H3BaseException:
        # 매우 긴 샘플 간격이나 H3 왜곡 구간에서도 업로드 자체는 실패시키지 않는다.
        return [start_cell, end_cell]
