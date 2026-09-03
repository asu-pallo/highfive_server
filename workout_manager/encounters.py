"""마주침 조회 도메인.

이미 운동 업로드 때 저장된 H3 셀과 시간 구간을 비교해 마주친 사용자를 찾는다.
하이파이브를 새로 생성하거나 누적 만남 횟수를 계산하는 책임은 갖지 않는다.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from collections.abc import Callable

from django.db import connection
from django.core.exceptions import ObjectDoesNotExist
from psycopg.types.range import Range

from .familiarity import find_familiarities_by_other_user
from .models import (
    HighFive,
    SpatialIndexVersion,
    TrajectorySegment,
    UserFamiliarity,
    Workout,
)


@dataclass(frozen=True)
class _Match:
    source_segment: TrajectorySegment
    matched_segment: TrajectorySegment
    overlap_started_at: datetime
    overlap_ended_at: datetime

    @property
    def duration(self) -> timedelta:
        return self.overlap_ended_at - self.overlap_started_at


@dataclass(frozen=True)
class EncounterRelations:
    """마주침 응답에 공통으로 필요한 하이파이브·친밀도 조회 결과."""

    sent_by_user: dict[int, HighFive]
    received_user_ids: set[int]
    familiarity_by_user: dict[int, UserFamiliarity]


def load_encounter_relations(
    *,
    viewer_id: int,
    workout: Workout,
    candidate_ids: list[int],
) -> EncounterRelations:
    """후보들의 하이파이브 상태와 저장된 친밀도를 조회만 한다."""
    unique_candidate_ids = list(dict.fromkeys(candidate_ids))
    sent_by_user = {
        item.toUser_id: item
        for item in HighFive.objects.select_related('toUser__profile').filter(
            fromWorkout=workout,
            toUser_id__in=unique_candidate_ids,
        )
    }
    received_user_ids = set(
        HighFive.objects.filter(
            toUser_id=viewer_id,
            fromUser_id__in=unique_candidate_ids,
        ).values_list('fromUser_id', flat=True)
    )
    familiarity_by_user = find_familiarities_by_other_user(
        user_id=viewer_id,
        other_user_ids=unique_candidate_ids,
    )

    return EncounterRelations(
        sent_by_user=sent_by_user,
        received_user_ids=received_user_ids,
        familiarity_by_user=familiarity_by_user,
    )


def encounter_summaries(
    workout_ids: list[int],
    *,
    profile_image_url_for: Callable[[str], str] | None = None,
) -> dict[int, dict]:
    """피드 운동별 마주친 사용자를 최초로 겹친 H3 셀에 한 번만 배치한다."""
    unique_ids = list(dict.fromkeys(workout_ids))
    result = {workout_id: _empty_summary() for workout_id in unique_ids}
    if not unique_ids:
        return result

    users_by_workout_cell = _encountered_users_by_cell(unique_ids)
    users_by_workout: dict[int, set[int]] = {
        workout_id: set() for workout_id in unique_ids
    }
    cells_by_workout: dict[int, dict[str, set[int]]] = {
        workout_id: {} for workout_id in unique_ids
    }
    for (workout_id, cell), user_ids in users_by_workout_cell.items():
        cells_by_workout[workout_id][cell] = user_ids
        users_by_workout[workout_id].update(user_ids)

    sent_by_workout = {workout_id: [] for workout_id in unique_ids}
    sent = (
        HighFive.objects.select_related('toUser__profile')
        .filter(fromWorkout_id__in=unique_ids)
        .order_by('-createdAt', '-id')
    )
    for high_five in sent:
        sent_by_workout[high_five.fromWorkout_id].append(high_five)

    for workout_id, cells in cells_by_workout.items():
        sent_high_fives = sent_by_workout[workout_id]
        result[workout_id] = {
            'totalCount': len(users_by_workout[workout_id]),
            'highFiveCount': len(sent_high_fives),
            'areas': [
                _area_summary(
                    cell=cell,
                    user_ids=user_ids,
                    sent=[
                        high_five
                        for high_five in sent_high_fives
                        if high_five.toUser_id in user_ids
                    ],
                    profile_image_url_for=profile_image_url_for,
                )
                for cell, user_ids in sorted(cells.items())
            ],
            'previewUsers': [
                profile_user(item.toUser, profile_image_url_for)
                for item in sent_high_fives[:2]
            ],
        }
    return result


def _encountered_users_by_cell(
    workout_ids: list[int],
) -> dict[tuple[int, str], set[int]]:
    """각 상대를 시간상 최초로 겹친 H3 셀 하나에만 배치해 조회한다."""
    quote = connection.ops.quote_name
    segment_table = quote(TrajectorySegment._meta.db_table)
    workout_table = quote(Workout._meta.db_table)
    version_table = quote(SpatialIndexVersion._meta.db_table)
    index_version = quote('indexVersion_id')
    cell_id = quote('cellId')
    deleted_at = quote('deletedAt')
    is_active = quote('isActive')

    sql = f'''\
        SELECT DISTINCT ON (source.workout_id, candidate.user_id)
            source.workout_id,
            source.{cell_id},
            candidate.user_id
        FROM {segment_table} AS source
        JOIN {version_table} AS version
          ON version.id = source.{index_version}
         AND version.{is_active} = TRUE
        JOIN {segment_table} AS candidate
          ON candidate.{index_version} = source.{index_version}
         AND candidate.{cell_id} = source.{cell_id}
         AND candidate.period && source.period
         AND candidate.workout_id <> source.workout_id
         AND candidate.user_id <> source.user_id
        JOIN {workout_table} AS candidate_workout
          ON candidate_workout.id = candidate.workout_id
         AND candidate_workout.{deleted_at} IS NULL
        WHERE source.workout_id = ANY(%s)
        ORDER BY
            source.workout_id,
            candidate.user_id,
            GREATEST(lower(source.period), lower(candidate.period)),
            source.id,
            candidate.id
    '''
    grouped: dict[tuple[int, str], set[int]] = {}
    with connection.cursor() as cursor:
        cursor.execute(sql, [workout_ids])
        for workout_id, cell, user_id in cursor.fetchall():
            grouped.setdefault((workout_id, cell), set()).add(user_id)
    return grouped


def _area_summary(
    *,
    cell: str,
    user_ids: set[int],
    sent: list[HighFive],
    profile_image_url_for: Callable[[str], str] | None,
) -> dict:
    return {
        'h3Cell': cell,
        'count': len(user_ids),
        'highFiveCount': len(sent),
        'previewUsers': [
            profile_user(item.toUser, profile_image_url_for)
            for item in sent[:2]
        ],
    }


def encounter_candidates(workout_id: int, cell_id: str) -> list[_Match]:
    """대표 H3 영역에서 마주친 상대를 사용자별 하나로 반환한다."""
    # 기존 요약과 동일한 판정 경로를 사용하되 상세 API에서 필요한 Match를 반환한다.
    source_segments = list(
        TrajectorySegment.objects.select_related('workout')
        .filter(
            workout_id=workout_id,
            indexVersion__isActive=True,
            cellId=cell_id,
        )
        .order_by('sequence')
    )
    if not source_segments:
        return []

    period = Range(
        source_segments[0].workout.startAt,
        source_segments[0].workout.endAt,
        bounds='[)',
    )
    candidates = TrajectorySegment.objects.select_related(
        'workout', 'user', 'user__profile'
    ).filter(
        indexVersion_id__in={item.indexVersion_id for item in source_segments},
        cellId=cell_id,
        period__overlap=period,
        workout__deletedAt__isnull=True,
    )
    best_by_user: dict[int, _Match] = {}
    for candidate in candidates.iterator():
        for source in source_segments:
            if candidate.indexVersion_id != source.indexVersion_id:
                continue
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
            previous = best_by_user.get(candidate.user_id)
            if previous is None or _rank(match) < _rank(previous):
                best_by_user[candidate.user_id] = match
    return sorted(
        best_by_user.values(),
        key=_encounter_order,
    )


def workout_encounter_candidates(workout_id: int) -> list[_Match]:
    """운동 전체 H3에서 마주친 상대를 사용자별 대표 1건으로 반환한다."""
    source_segments = list(
        TrajectorySegment.objects.select_related('workout')
        .filter(workout_id=workout_id, indexVersion__isActive=True)
        .order_by('sequence')
    )
    if not source_segments:
        return []

    sources_by_cell: dict[tuple[int, str], list[TrajectorySegment]] = {}
    for source in source_segments:
        sources_by_cell.setdefault(
            (source.indexVersion_id, source.cellId), []
        ).append(source)

    workout = source_segments[0].workout
    period = Range(workout.startAt, workout.endAt, bounds='[)')
    candidates = (
        TrajectorySegment.objects.select_related('workout', 'user', 'user__profile')
        .filter(
            indexVersion_id__in={item.indexVersion_id for item in source_segments},
            cellId__in={item.cellId for item in source_segments},
            period__overlap=period,
            workout__deletedAt__isnull=True,
        )
    )
    best_by_user: dict[int, _Match] = {}
    for candidate in candidates.iterator():
        if candidate.workout_id == workout_id or candidate.user_id == workout.user_id:
            continue
        for source in sources_by_cell.get(
            (candidate.indexVersion_id, candidate.cellId), []
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
            previous = best_by_user.get(candidate.user_id)
            if previous is None or _rank(match) < _rank(previous):
                best_by_user[candidate.user_id] = match

    return sorted(
        best_by_user.values(),
        key=_encounter_order,
    )


def _encounter_order(match: _Match) -> tuple:
    """마주친 시각 우선, 같은 시각은 프로필 닉네임 사전순으로 정렬한다."""
    try:
        name = match.matched_segment.user.profile.nicknameKey
    except ObjectDoesNotExist:
        name = match.matched_segment.user.username
    return (
        match.overlap_started_at,
        (name or '').casefold(),
        match.matched_segment.user_id,
    )


def _empty_summary() -> dict:
    return {
        'totalCount': 0,
        'highFiveCount': 0,
        'areas': [],
        'previewUsers': [],
    }


def profile_user(
    user,
    profile_image_url_for: Callable[[str], str] | None,
) -> dict:
    try:
        profile = user.profile
    except ObjectDoesNotExist:
        profile = None
    image_key = profile.imageThumbKey if profile is not None else ''
    return {
        'userId': user.id,
        'nickname': (
            profile.nickname if profile is not None and profile.nickname
            else user.get_username()
        ),
        'profileImageUrl': (
            profile_image_url_for(image_key)
            if image_key and profile_image_url_for is not None
            else None
        ),
    }


def _rank(match: _Match) -> tuple[float, int, int]:
    """긴 겹침, 현재 운동의 빠른 구간, 작은 상대 세그먼트 ID 순이다."""
    return (
        -match.duration.total_seconds(),
        match.source_segment.sequence,
        match.matched_segment.id,
    )
