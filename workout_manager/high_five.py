from dataclasses import dataclass
from datetime import datetime, timedelta

from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from psycopg.types.range import Range

from .models import HighFive, TrajectorySegment, Workout


@dataclass(frozen=True)
class _Match:
    source_segment: TrajectorySegment
    matched_segment: TrajectorySegment
    overlap_started_at: datetime
    overlap_ended_at: datetime

    @property
    def duration(self) -> timedelta:
        return self.overlap_ended_at - self.overlap_started_at


def rebuild_high_fives(
    workout: Workout,
    *,
    previously_affected_ids=(),
    previous_relation_keys=(),
) -> int:
    """운동과 겹치는 운동 쌍을 대칭 HighFive 관계로 교체한다."""
    source_segments = list(
        TrajectorySegment.objects.select_related('indexVersion')
        .filter(workout=workout, indexVersion__isActive=True)
        .order_by('sequence')
    )
    best_by_workout: dict[int, _Match] = {}
    sources_by_cell: dict[tuple[int, str], list[TrajectorySegment]] = {}
    for source in source_segments:
        sources_by_cell.setdefault(
            (source.indexVersion_id, source.cellId), []
        ).append(source)

    if source_segments:
        candidates = (
            TrajectorySegment.objects.select_related('workout')
            .filter(
                indexVersion_id__in={
                    source.indexVersion_id for source in source_segments
                },
                cellId__in={source.cellId for source in source_segments},
                period__overlap=Range(workout.startAt, workout.endAt, bounds='[)'),
                workout__deletedAt__isnull=True,
            )
            .exclude(user=workout.user)
        )
    else:
        candidates = ()

    for candidate in candidates:
        for source in sources_by_cell.get(
            (candidate.indexVersion_id, candidate.cellId), ()
        ):
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
            previous = best_by_workout.get(candidate.workout_id)
            if previous is None or _rank(match) < _rank(previous):
                best_by_workout[candidate.workout_id] = match

    previous_pairs = set(
        HighFive.objects.filter(
            Q(workoutA=workout) | Q(workoutB=workout)
        ).values_list('workoutA_id', 'workoutB_id')
    )
    affected_ids = {workout.id, *previously_affected_ids}
    for workout_a_id, workout_b_id in previous_pairs:
        affected_ids.update((workout_a_id, workout_b_id))

    relations = []
    for match in best_by_workout.values():
        matched_workout = match.matched_segment.workout
        if workout.id < matched_workout.id:
            workout_a, workout_b = workout, matched_workout
            segment_a, segment_b = match.source_segment, match.matched_segment
        else:
            workout_a, workout_b = matched_workout, workout
            segment_a, segment_b = match.matched_segment, match.source_segment
        affected_ids.add(matched_workout.id)
        relations.append(HighFive(
            workoutA=workout_a,
            workoutB=workout_b,
            userA_id=workout_a.user_id,
            userB_id=workout_b.user_id,
            indexVersion=match.source_segment.indexVersion,
            h3Cell=match.source_segment.cellId,
            overlapStartedAt=match.overlap_started_at,
            overlapEndedAt=match.overlap_ended_at,
            segmentA=segment_a,
            segmentB=segment_b,
        ))

    new_relation_keys = {
        (
            relation.workoutA_id,
            relation.workoutB_id,
            relation.indexVersion_id,
            relation.h3Cell,
            relation.overlapStartedAt,
            relation.overlapEndedAt,
        )
        for relation in relations
    }
    if set(previous_relation_keys) == new_relation_keys:
        return len({
            relation.userB_id
            if relation.workoutA_id == workout.id
            else relation.userA_id
            for relation in relations
        })

    with transaction.atomic():
        HighFive.objects.filter(
            Q(workoutA=workout) | Q(workoutB=workout)
        ).delete()
        HighFive.objects.bulk_create(relations)
        # 다른 사용자의 새 운동 때문에 기존 운동의 하이파이브 요약만 달라져도
        # 증분 다운로드(since)에 포함되도록 관련 운동 변경 시각을 함께 올린다.
        Workout.objects.filter(id__in=affected_ids).update(updatedAt=timezone.now())
        transaction.on_commit(
            lambda: invalidate_high_five_summaries(affected_ids)
        )
    return len({
        relation.userB_id if relation.workoutA_id == workout.id else relation.userA_id
        for relation in relations
    })


def clear_high_fives(
    workout: Workout,
    *,
    previously_affected_ids=(),
    had_previous_relations: bool = False,
) -> None:
    """유효한 H3 경로가 없는 운동의 기존 관계와 캐시를 정리한다."""
    current_pairs = set(
        HighFive.objects.filter(
            Q(workoutA=workout) | Q(workoutB=workout)
        ).values_list('workoutA_id', 'workoutB_id')
    )
    if not current_pairs and not had_previous_relations:
        return

    affected_ids = {workout.id, *previously_affected_ids}
    for workout_a_id, workout_b_id in current_pairs:
        affected_ids.update((workout_a_id, workout_b_id))

    with transaction.atomic():
        HighFive.objects.filter(
            Q(workoutA=workout) | Q(workoutB=workout)
        ).delete()
        Workout.objects.filter(id__in=affected_ids).update(updatedAt=timezone.now())
        transaction.on_commit(
            lambda: invalidate_high_five_summaries(affected_ids)
        )


def high_five_summaries(workout_ids: list[int]) -> dict[int, dict]:
    """피드 페이지의 운동별 H3 인원수를 캐시와 DB에서 일괄 조회한다."""
    unique_ids = list(dict.fromkeys(workout_ids))
    keys = {workout_id: _cache_key(workout_id) for workout_id in unique_ids}
    cached = cache.get_many(keys.values())
    result = {
        workout_id: cached[key]
        for workout_id, key in keys.items()
        if key in cached
    }
    missing = [workout_id for workout_id in unique_ids if workout_id not in result]
    if not missing:
        return result

    rows = HighFive.objects.filter(
        Q(workoutA_id__in=missing) | Q(workoutB_id__in=missing)
    ).order_by('id')
    best_by_workout_user: dict[tuple[int, int], HighFive] = {}
    for row in rows:
        if row.workoutA_id in missing:
            _keep_best(best_by_workout_user, row.workoutA_id, row.userB_id, row)
        if row.workoutB_id in missing:
            _keep_best(best_by_workout_user, row.workoutB_id, row.userA_id, row)

    for workout_id in missing:
        cells: dict[str, int] = {}
        for (owner_id, _), row in best_by_workout_user.items():
            if owner_id == workout_id:
                cells[row.h3Cell] = cells.get(row.h3Cell, 0) + 1
        result[workout_id] = {
            'totalCount': sum(cells.values()),
            'areas': [
                {'h3Cell': cell, 'count': count}
                for cell, count in sorted(cells.items())
            ],
        }

    cache.set_many(
        {keys[workout_id]: result[workout_id] for workout_id in missing},
        timeout=300,
    )
    return result


def invalidate_high_five_summaries(workout_ids) -> None:
    cache.delete_many([_cache_key(workout_id) for workout_id in set(workout_ids)])


def _keep_best(store, workout_id: int, other_user_id: int, row: HighFive) -> None:
    key = (workout_id, other_user_id)
    previous = store.get(key)
    if previous is None or _relation_rank(row) < _relation_rank(previous):
        store[key] = row


def _cache_key(workout_id: int) -> str:
    return f'highfive:summary:v1:workout:{workout_id}'


def _rank(match: _Match) -> tuple[float, int, int]:
    """긴 겹침, 현재 운동의 빠른 구간, 작은 상대 세그먼트 ID 순이다."""
    return (
        -match.duration.total_seconds(),
        match.source_segment.sequence,
        match.matched_segment.id,
    )


def _relation_rank(row: HighFive) -> tuple[float, datetime, int]:
    duration = row.overlapEndedAt - row.overlapStartedAt
    return (-duration.total_seconds(), row.overlapStartedAt, row.id)
