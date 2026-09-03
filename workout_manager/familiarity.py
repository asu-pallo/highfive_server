"""사용자 친밀도 도메인.

하이파이브로 관계가 공개된 두 사용자의 누적 마주침 횟수를 관리한다.
H3 후보 탐색과 하이파이브 생성은 각각 encounters, high_fives가 담당한다.
"""

from datetime import datetime

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from .models import SpatialIndexVersion, TrajectorySegment, UserFamiliarity, Workout


def find_familiarities_by_other_user(
    *,
    user_id: int,
    other_user_ids: list[int],
) -> dict[int, UserFamiliarity]:
    """현재 사용자와 상대 사용자들의 무방향 관계를 상대 ID 기준으로 반환한다."""
    unique_other_ids = sorted(set(other_user_ids) - {user_id})
    if not unique_other_ids:
        return {}

    rows = UserFamiliarity.objects.filter(
        Q(firstUser_id=user_id, secondUser_id__in=unique_other_ids)
        | Q(secondUser_id=user_id, firstUser_id__in=unique_other_ids)
    )
    return {
        (
            item.secondUser_id
            if item.firstUser_id == user_id
            else item.firstUser_id
        ): item
        for item in rows
    }


def refresh_user_familiarity(
    *,
    user_id: int,
    other_user_id: int,
) -> UserFamiliarity:
    """이미 성립한 두 사용자 관계를 찾아 최신 만남 횟수로 갱신한다."""
    first_user_id, second_user_id = sorted((user_id, other_user_id))
    familiarity = UserFamiliarity.objects.get(
        firstUser_id=first_user_id,
        secondUser_id=second_user_id,
    )
    return refresh_familiarity(familiarity.id)


def refresh_familiarity(familiarity_id: int) -> UserFamiliarity:
    """한 사용자 쌍의 마지막 검사 이후 신규 만남을 갱신한다."""
    return refresh_familiarities([familiarity_id])[familiarity_id]


def refresh_familiarities(
    familiarity_ids: list[int],
) -> dict[int, UserFamiliarity]:
    """여러 사용자 쌍의 신규 만남을 SQL 한 번으로 집계해 일괄 갱신한다."""
    unique_ids = sorted(set(familiarity_ids))
    if not unique_ids:
        return {}

    with transaction.atomic():
        familiarities = list(
            UserFamiliarity.objects.select_for_update()
            .filter(pk__in=unique_ids)
            .order_by('pk')
        )
        if len(familiarities) != len(unique_ids):
            raise UserFamiliarity.DoesNotExist

        cutoff = timezone.now()
        counts = _new_encounter_counts(
            familiarities=familiarities,
            cutoff=cutoff,
        )
        for familiarity in familiarities:
            familiarity.metCount += counts.get(familiarity.id, 0)
            familiarity.metCheckedAt = cutoff
        UserFamiliarity.objects.bulk_update(
            familiarities,
            fields=('metCount', 'metCheckedAt'),
        )
        return {item.id: item for item in familiarities}


def _new_encounter_counts(
    *,
    familiarities: list[UserFamiliarity],
    cutoff: datetime,
) -> dict[int, int]:
    """여러 관계의 업로드 커서 이후 운동 만남 수를 SQL 한 번으로 센다."""
    if not familiarities:
        return {}

    quote = connection.ops.quote_name
    segment_table = quote(TrajectorySegment._meta.db_table)
    workout_table = quote(Workout._meta.db_table)
    version_table = quote(SpatialIndexVersion._meta.db_table)
    index_version = quote('indexVersion_id')
    cell_id = quote('cellId')
    created_at = quote('createdAt')
    deleted_at = quote('deletedAt')
    is_active = quote('isActive')

    pair_rows = ', '.join(
        '(%s::bigint, %s::bigint, %s::bigint, %s::timestamptz)'
        for _ in familiarities
    )
    sql = f'''\
        WITH pairs(familiarity_id, first_user_id, second_user_id, checked_at) AS (
            VALUES {pair_rows}
        )
        SELECT
            pairs.familiarity_id,
            COUNT(DISTINCT (
                first_segment.workout_id,
                second_segment.workout_id
            ))
        FROM pairs
        JOIN {segment_table} AS first_segment
          ON first_segment.user_id = pairs.first_user_id
        JOIN {version_table} AS version
          ON version.id = first_segment.{index_version}
         AND version.{is_active} = TRUE
        JOIN {segment_table} AS second_segment
          ON second_segment.{index_version} = first_segment.{index_version}
         AND second_segment.{cell_id} = first_segment.{cell_id}
         AND second_segment.period && first_segment.period
         AND second_segment.user_id = pairs.second_user_id
        JOIN {workout_table} AS first_workout
          ON first_workout.id = first_segment.workout_id
         AND first_workout.{deleted_at} IS NULL
         AND first_workout.{created_at} <= %s
        JOIN {workout_table} AS second_workout
          ON second_workout.id = second_segment.workout_id
         AND second_workout.{deleted_at} IS NULL
         AND second_workout.{created_at} <= %s
        WHERE pairs.checked_at IS NULL
           OR first_workout.{created_at} > pairs.checked_at
           OR second_workout.{created_at} > pairs.checked_at
        GROUP BY pairs.familiarity_id
    '''
    params = []
    for familiarity in familiarities:
        params.extend((
            familiarity.id,
            familiarity.firstUser_id,
            familiarity.secondUser_id,
            familiarity.metCheckedAt,
        ))
    params.extend((cutoff, cutoff))
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return {
            familiarity_id: count
            for familiarity_id, count in cursor.fetchall()
        }
