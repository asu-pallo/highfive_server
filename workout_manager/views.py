from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Workout
from .serializers import WorkoutSerializer, WorkoutUploadSerializer


def _fail(message: str, code: int = status.HTTP_400_BAD_REQUEST) -> Response:
    return Response({'s': False, 'msg': message}, status=code)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def upload_workouts(request):
    """클라이언트 운동을 멱등하게 저장한다."""
    body = WorkoutUploadSerializer(data=request.data)
    if not body.is_valid():
        return _fail('운동 기록 형식이 올바르지 않습니다.')

    # return _fail('운동 기록 형식이 올바르지 않습니다.')

    created = 0
    updated = 0
    unchanged = 0
    mutable_fields = (
        'kind',
        'rawType',
        'startAt',
        'endAt',
        'distanceMeters',
        'kcal',
        'heartRateAvg',
        'heartRateMin',
        'heartRateMax',
        'heartRateSampleCount',
        'steps',
        'flightsClimbed',
    )

    with transaction.atomic():
        for data in body.validated_data['workouts']:
            identity = {
                'user': request.user,
                'source': data['source'],
                'sourceName': data['sourceName'],
                'sourceWorkoutId': data['sourceWorkoutId'],
            }
            workout, was_created = Workout.objects.get_or_create(
                **identity,
                defaults={field: data.get(field) for field in mutable_fields},
            )
            if was_created:
                created += 1
                continue

            changed = []
            for field in mutable_fields:
                value = data.get(field)
                if getattr(workout, field) != value:
                    setattr(workout, field, value)
                    changed.append(field)
            if workout.deletedAt is not None:
                workout.deletedAt = None
                changed.append('deletedAt')

            if changed:
                workout.save(update_fields=[*changed, 'updatedAt'])
                updated += 1
            else:
                unchanged += 1

    return Response({
        's': True,
        'created': created,
        'updated': updated,
        'unchanged': unchanged,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_workouts(request):
    """서버 스냅샷 시각까지 변경된 내 운동을 반환한다."""
    snapshot_at = timezone.now()
    workouts = Workout.objects.filter(
        user=request.user,
        updatedAt__lte=snapshot_at,
    )

    since_value = request.query_params.get('since')
    if since_value:
        since = parse_datetime(since_value)
        if since is None or timezone.is_naive(since):
            return _fail('since는 시간대가 포함된 ISO 8601 시각이어야 합니다.')
        workouts = workouts.filter(updatedAt__gt=since)

    workouts = workouts.order_by('updatedAt', 'id')
    return Response({
        's': True,
        'serverTime': snapshot_at,
        'workouts': WorkoutSerializer(workouts, many=True).data,
    })

