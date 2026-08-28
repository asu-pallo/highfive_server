import hashlib
import json
import threading
import base64
import binascii
import time
from contextlib import nullcontext

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Workout, WorkoutDetail
from .serializers import WorkoutSerializer, WorkoutUploadSerializer


# SQLite는 동시 쓰기를 지원하지 않는다. 로컬 개발 서버에서 파일 업로드 자체는 병렬로
# 진행하되, 마지막 DB 반영 몇 ms만 한 요청씩 처리한다. PostgreSQL에서는 사용하지 않는다.
_sqlite_write_lock = threading.Lock()
_workout_page_size = 20


def _fail(message: str, code: int = status.HTTP_400_BAD_REQUEST) -> Response:
    return Response({'s': False, 'msg': message}, status=code)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def upload_workouts(request):
    """운동 한 건과 경로·심박 원본 JSON 파일을 멱등하게 저장한다."""
    metadata = request.data.get('metadata')
    detail_file = request.FILES.get('detail')
    if not isinstance(metadata, str) or detail_file is None:
        return _fail('운동 메타데이터와 상세 파일이 필요합니다.')
    if detail_file.size > settings.DATA_UPLOAD_MAX_MEMORY_SIZE:
        return _fail('운동 상세 파일이 허용 크기를 초과했습니다.', status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    try:
        metadata_json = json.loads(metadata)
        detail_bytes = detail_file.read()
        detail_json = json.loads(detail_bytes)
    except (TypeError, ValueError, UnicodeDecodeError):
        return _fail('운동 상세 파일 형식이 올바르지 않습니다.')

    body = WorkoutUploadSerializer(data=metadata_json)
    if not body.is_valid():
        return _fail('운동 기록 형식이 올바르지 않습니다.')
    data = body.validated_data
    detail_error = _validate_detail(detail_json, data)
    if detail_error:
        return _fail(detail_error)
    content_hash = hashlib.sha256(detail_bytes).hexdigest()
    if content_hash != data['contentHash']:
        return _fail('운동 상세 파일의 해시가 일치하지 않습니다.')

    heart_rates = detail_json['heartRate']
    heart_values = [round(float(sample['bpm'])) for sample in heart_rates]
    heart_summary = {
        'heartRateAvg': round(sum(heart_values) / len(heart_values)),
        'heartRateMin': min(heart_values),
        'heartRateMax': max(heart_values),
        'heartRateSampleCount': len(heart_values),
    } if heart_values else {
        'heartRateAvg': None,
        'heartRateMin': None,
        'heartRateMax': None,
        'heartRateSampleCount': None,
    }
    mutable_fields = (
        'kind',
        'rawType',
        'startAt',
        'endAt',
        'distanceMeters',
        'kcal',
        'steps',
        'flightsClimbed',
    )

    identity = {
        'user': request.user,
        'source': data['source'],
        'sourceName': data['sourceName'],
        'sourceWorkoutId': data['sourceWorkoutId'],
    }
    # 객체 저장은 네트워크 I/O라 DB 트랜잭션 안에서 수행하면 SQLite 쓰기 잠금을
    # 오래 잡는다. 내용 해시 기반 키로 먼저 저장하고 DB 쓰기는 짧게 끝낸다.
    new_key = f'workouts/{request.user.id}/{content_hash}.json'
    object_created = False
    if not default_storage.exists(new_key):
        new_key = default_storage.save(new_key, ContentFile(detail_bytes))
        object_created = True

    old_key = None
    try:
        write_lock = _sqlite_write_lock if connection.vendor == 'sqlite' else nullcontext()
        with write_lock, transaction.atomic():
            defaults = {field: data.get(field) for field in mutable_fields}
            defaults.update(heart_summary)
            workout, created = Workout.objects.get_or_create(**identity, defaults=defaults)
            changed = []
            if not created:
                for field, value in defaults.items():
                    if getattr(workout, field) != value:
                        setattr(workout, field, value)
                        changed.append(field)
                if workout.deletedAt is not None:
                    workout.deletedAt = None
                    changed.append('deletedAt')
                if changed:
                    workout.save(update_fields=[*changed, 'updatedAt'])

            current = WorkoutDetail.objects.filter(workout=workout).first()
            if current and current.contentHash == content_hash:
                return Response({
                    's': True,
                    'created': created,
                    'unchanged': not created and not changed,
                })

            old_key = current.objectKey if current else None
            WorkoutDetail.objects.update_or_create(
                workout=workout,
                defaults={
                    'objectKey': new_key,
                    'contentHash': content_hash,
                    'formatVersion': detail_json['formatVersion'],
                    'routePointCount': len(detail_json['route']),
                    'heartRateSampleCount': len(heart_rates),
                    'fileSize': len(detail_bytes),
                },
            )
            # 메타데이터가 같고 경로·심박 파일만 달라져도 증분 다운로드에 포함돼야 한다.
            if not created and not changed:
                workout.save(update_fields=['updatedAt'])
    except Exception:
        if object_created and new_key != old_key and default_storage.exists(new_key):
            default_storage.delete(new_key)
        raise

    if old_key and old_key != new_key and default_storage.exists(old_key):
        default_storage.delete(old_key)
    return Response({'s': True, 'created': created, 'unchanged': False})


def _validate_detail(detail, metadata):
    if not isinstance(detail, dict) or detail.get('formatVersion') != 1:
        return '지원하지 않는 운동 상세 파일 버전입니다.'
    for key in ('route', 'heartRate'):
        if not isinstance(detail.get(key), list):
            return f'{key}는 배열이어야 합니다.'
        if len(detail[key]) > 100_000:
            return f'{key} 샘플이 너무 많습니다.'
    identity_fields = {
        'source': 'source',
        'sourceName': 'sourceName',
        'sourceWorkoutId': 'sourceWorkoutId',
    }
    for detail_key, metadata_key in identity_fields.items():
        expected = metadata[metadata_key]
        actual = detail.get(detail_key)
        if actual != expected:
            return f'상세 파일의 {detail_key} 값이 메타데이터와 다릅니다.'
    for detail_key, metadata_key in (('startedAt', 'startAt'), ('endedAt', 'endAt')):
        actual = parse_datetime(str(detail.get(detail_key)))
        if actual is None or actual != metadata[metadata_key]:
            return f'상세 파일의 {detail_key} 값이 메타데이터와 다릅니다.'
    try:
        for point in detail['route']:
            latitude = float(point['latitude'])
            longitude = float(point['longitude'])
            if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                return '경로 좌표 범위가 올바르지 않습니다.'
        for sample in detail['heartRate']:
            bpm = float(sample['bpm'])
            if not 1 <= bpm <= 300:
                return '심박수 범위가 올바르지 않습니다.'
    except (KeyError, TypeError, ValueError):
        return '운동 상세 샘플 형식이 올바르지 않습니다.'
    return None


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_workouts(request):
    """서버 스냅샷 시각까지 변경된 내 운동을 20건씩 반환한다."""
    snapshot_value = request.query_params.get('snapshot')
    snapshot_at = parse_datetime(snapshot_value) if snapshot_value else timezone.now()
    if snapshot_at is None or timezone.is_naive(snapshot_at):
        return _fail('snapshot은 시간대가 포함된 ISO 8601 시각이어야 합니다.')
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

    # 개발 중 전역·하단 페이징 로더를 눈으로 확인하기 위한 지연이다.
    # 운영 환경(DEBUG=False)에는 적용하지 않는다.
    # if settings.DEBUG:
    #     time.sleep(1)

    cursor_value = request.query_params.get('cursor')
    if cursor_value:
        try:
            padding = '=' * (-len(cursor_value) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(cursor_value + padding))
            cursor_at = parse_datetime(decoded['updatedAt'])
            cursor_id = int(decoded['id'])
            if cursor_at is None or timezone.is_naive(cursor_at):
                raise ValueError
        except (
            ValueError,
            TypeError,
            KeyError,
            UnicodeDecodeError,
            binascii.Error,
            json.JSONDecodeError,
        ):
            return _fail('cursor 형식이 올바르지 않습니다.')
        workouts = workouts.filter(
            Q(updatedAt__lt=cursor_at) |
            Q(updatedAt=cursor_at, id__lt=cursor_id)
        )

    page = list(workouts.order_by('-updatedAt', '-id')[:_workout_page_size + 1])
    has_more = len(page) > _workout_page_size
    page = page[:_workout_page_size]
    next_cursor = None
    if has_more:
        last = page[-1]
        payload = json.dumps({
            'updatedAt': last.updatedAt.isoformat(),
            'id': last.id,
        }, separators=(',', ':')).encode()
        next_cursor = base64.urlsafe_b64encode(payload).decode().rstrip('=')

    return Response({
        's': True,
        'serverTime': snapshot_at,
        'workouts': WorkoutSerializer(page, many=True).data,
        'nextCursor': next_cursor,
        'hasMore': has_more,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_workout_detail(request, workout_id):
    """내 운동의 경로·심박 원본 파일을 반환한다."""
    try:
        workout = Workout.objects.select_related('detail').get(
            pk=workout_id,
            user=request.user,
            deletedAt__isnull=True,
        )
        detail = workout.detail
    except (Workout.DoesNotExist, WorkoutDetail.DoesNotExist):
        return _fail('운동 상세 정보를 찾을 수 없습니다.', status.HTTP_404_NOT_FOUND)

    try:
        with default_storage.open(detail.objectKey, 'rb') as stored:
            payload = json.load(stored)
    except (OSError, TypeError, ValueError, UnicodeDecodeError):
        return _fail(
            '운동 상세 파일을 읽지 못했습니다.',
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return Response({'s': True, 'detail': payload})
