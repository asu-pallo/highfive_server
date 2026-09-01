from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import Q
from psycopg.types.range import Range

from .models import TrajectorySegment


@dataclass(frozen=True)
class _Match:
    source_segment: TrajectorySegment
    matched_segment: TrajectorySegment
    overlap_started_at: datetime
    overlap_ended_at: datetime

    @property
    def duration(self) -> timedelta:
        return self.overlap_ended_at - self.overlap_started_at


def high_five_summaries(workout_ids: list[int]) -> dict[int, dict]:
    """현재 피드 페이지 운동만 H3·시간 겹침으로 실시간 일괄 판정한다."""
    unique_ids = list(dict.fromkeys(workout_ids))
    result = {workout_id: _empty_summary() for workout_id in unique_ids}
    if not unique_ids:
        return result

    source_segments = list(
        TrajectorySegment.objects.select_related('workout')
        .filter(workout_id__in=unique_ids, indexVersion__isActive=True)
        .order_by('workout_id', 'sequence')
    )
    if not source_segments:
        return result

    sources_by_cell: dict[tuple[int, str], list[TrajectorySegment]] = {}
    for source in source_segments:
        sources_by_cell.setdefault(
            (source.indexVersion_id, source.cellId), []
        ).append(source)

    page_periods = {
        source.workout_id: Range(
            source.workout.startAt,
            source.workout.endAt,
            bounds='[)',
        )
        for source in source_segments
    }
    period_filter = Q()
    for period in page_periods.values():
        period_filter |= Q(period__overlap=period)
    candidates = TrajectorySegment.objects.select_related('workout').filter(
        indexVersion_id__in={
            source.indexVersion_id for source in source_segments
        },
        cellId__in={source.cellId for source in source_segments},
        workout__deletedAt__isnull=True,
    ).filter(period_filter)

    best_by_workout_user: dict[tuple[int, int], _Match] = {}
    for candidate in candidates.iterator():
        for source in sources_by_cell.get(
            (candidate.indexVersion_id, candidate.cellId), ()
        ):
            if candidate.workout_id == source.workout_id:
                continue
            if candidate.user_id == source.user_id:
                continue
            overlap_started_at = max(source.period.lower, candidate.period.lower)
            overlap_ended_at = min(source.period.upper, candidate.period.upper)
            if overlap_started_at >= overlap_ended_at:
                continue

            match = _Match(
                source_segment=source,
                matched_segment=candidate,
                overlap_started_at=overlap_started_at,
                overlap_ended_at=overlap_ended_at,
            )
            key = (source.workout_id, candidate.user_id)
            previous = best_by_workout_user.get(key)
            if previous is None or _rank(match) < _rank(previous):
                best_by_workout_user[key] = match

    cells_by_workout: dict[int, dict[str, int]] = {
        workout_id: {} for workout_id in unique_ids
    }
    for (workout_id, _), match in best_by_workout_user.items():
        cells = cells_by_workout[workout_id]
        cell = match.source_segment.cellId
        cells[cell] = cells.get(cell, 0) + 1

    for workout_id, cells in cells_by_workout.items():
        result[workout_id] = {
            'totalCount': sum(cells.values()),
            'areas': [
                {'h3Cell': cell, 'count': count}
                for cell, count in sorted(cells.items())
            ],
        }
    return result


def _empty_summary() -> dict:
    return {'totalCount': 0, 'areas': []}


def _rank(match: _Match) -> tuple[float, int, int]:
    """긴 겹침, 현재 운동의 빠른 구간, 작은 상대 세그먼트 ID 순이다."""
    return (
        -match.duration.total_seconds(),
        match.source_segment.sequence,
        match.matched_segment.id,
    )
