"""하이파이브 생성 도메인.

사용자가 손 버튼을 눌렀을 때 방향성 있는 HighFive를 한 번만 저장한다.
실제 마주침 여부는 encounters에 위임하고, 누적 만남 갱신은 familiarity에 위임한다.
"""

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from .encounters import encounter_candidates
from .familiarity import refresh_familiarity
from .models import HighFive, UserFamiliarity, Workout


@dataclass(frozen=True)
class HighFiveCreation:
    high_five: HighFive
    familiarity: UserFamiliarity
    created: bool


def create_high_five_for_encounter(
    *,
    workout: Workout,
    target_user_id: int,
    cell_id: str,
) -> HighFiveCreation | None:
    """검증된 마주침에 하이파이브를 생성하고 최신 만남 횟수를 반환한다."""
    match = next(
        (
            value
            for value in encounter_candidates(workout.id, cell_id)
            if value.matched_segment.user_id == target_user_id
        ),
        None,
    )
    if match is None:
        return None

    other_workout = match.matched_segment.workout
    low_id, high_id = sorted((workout.id, other_workout.id))
    with transaction.atomic():
        high_five, created = HighFive.objects.select_related(
            'toUser__profile'
        ).get_or_create(
            fromWorkout=workout,
            toUser_id=target_user_id,
            defaults={
                'fromUser': workout.user,
                'toWorkout': other_workout,
                'cellId': cell_id,
                'encounteredAt': match.overlap_started_at,
                'encounterKey': f'{low_id}:{high_id}',
            },
        )
        first_user_id, second_user_id = sorted(
            (workout.user_id, target_user_id)
        )
        familiarity, familiarity_created = UserFamiliarity.objects.get_or_create(
            firstUser_id=first_user_id,
            secondUser_id=second_user_id,
        )
        if created and not familiarity_created:
            familiarity.lastHighFiveAt = timezone.now()
            familiarity.save(update_fields=('lastHighFiveAt',))

    return HighFiveCreation(
        high_five=high_five,
        familiarity=refresh_familiarity(familiarity.id),
        created=created,
    )
