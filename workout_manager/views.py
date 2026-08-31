import hashlib
import json
import base64
import binascii
import logging
from datetime import timedelta

import boto3
import h3
from django.core.files.storage import default_storage
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import (
    TrajectorySegment,
    Workout,
    WorkoutDetail,
    WorkoutUploadSession,
)
from .serializers import (
    WorkoutSerializer,
    WorkoutUploadCompleteSerializer,
    WorkoutUploadPrepareSerializer,
    WorkoutUploadSerializer,
)
from .spatial_index import ensure_h3_segments, rebuild_h3_segments


_workout_page_size = 20
_logger = logging.getLogger(__name__)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def prepare_workout_upload(request):
    """운동 메타데이터를 검증하고 상세 JSON의 S3 직접 업로드 폼을 발급한다."""
    body = WorkoutUploadPrepareSerializer(data=request.data)
    if not body.is_valid():
        return _upload_fail(
            request,
            '운동 업로드 준비 정보가 올바르지 않습니다.',
            stage='prepare_metadata',
            metadata=request.data,
            diagnostic=body.errors,
        )
    data = body.validated_data
    if data['fileSize'] > settings.DATA_UPLOAD_MAX_MEMORY_SIZE:
        return _upload_fail(
            request,
            '운동 상세 파일이 허용 크기를 초과했습니다.',
            stage='prepare_file_size',
            code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            metadata=data,
        )

    identity = {
        'user': request.user,
        'source': data['source'],
        'sourceName': data['sourceName'],
        'sourceWorkoutId': data['sourceWorkoutId'],
    }
    object_key = f"workouts/{request.user.id}/{data['contentHash']}.json"
    expires_at = timezone.now() + timedelta(
        seconds=settings.S3_DOWNLOAD_EXPIRES_SECONDS
    )
    metadata = _session_metadata(data)
    session, _ = WorkoutUploadSession.objects.update_or_create(
        **identity,
        contentHash=data['contentHash'],
        defaults={
            'metadata': metadata,
            'objectKey': object_key,
            'fileSize': data['fileSize'],
            'status': WorkoutUploadSession.Status.PREPARED,
            'expiresAt': expires_at,
        },
    )
    try:
        detail_upload_required = not default_storage.exists(object_key)
        upload = (
            _workout_upload_form(object_key, data['fileSize'])
            if detail_upload_required
            else None
        )
    except Exception:
        _logger.exception(
            'workout upload failed · stage=presign · user=%s · workout=%s',
            request.user.id,
            data['sourceWorkoutId'],
        )
        return _fail(
            '운동 상세 파일 업로드 주소를 만들지 못했습니다.',
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return Response({
        's': True,
        'uploadId': session.uploadId,
        'detailUploadRequired': detail_upload_required,
        'uploadUrl': upload['url'] if upload else None,
        'uploadFields': upload['fields'] if upload else {},
        'expiresInSeconds': settings.S3_DOWNLOAD_EXPIRES_SECONDS,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def complete_workout_upload(request):
    """S3 직접 업로드 파일을 검증하고 운동·상세·H3를 확정한다."""
    body = WorkoutUploadCompleteSerializer(data=request.data)
    if not body.is_valid():
        return _upload_fail(
            request,
            '운동 업로드 완료 정보가 올바르지 않습니다.',
            stage='complete_metadata',
            diagnostic=body.errors,
        )
    upload_id = body.validated_data['uploadId']
    try:
        session = WorkoutUploadSession.objects.get(
            uploadId=upload_id,
            user=request.user,
        )
    except WorkoutUploadSession.DoesNotExist:
        return _fail('운동 업로드 정보를 찾을 수 없습니다.', status.HTTP_404_NOT_FOUND)
    if session.status == WorkoutUploadSession.Status.READY:
        return Response({
            's': True,
            'alreadyComplete': True,
            'serverId': session.workout_id,
        })
    if session.expiresAt < timezone.now():
        return _upload_fail(
            request,
            '운동 상세 파일 업로드 시간이 만료됐습니다.',
            stage='expired',
            metadata=session.metadata,
        )
    claimed = WorkoutUploadSession.objects.filter(
        uploadId=upload_id,
        user=request.user,
        status__in=(
            WorkoutUploadSession.Status.PREPARED,
            WorkoutUploadSession.Status.FAILED,
        ),
    ).update(status=WorkoutUploadSession.Status.PROCESSING)
    if not claimed:
        return _fail('운동 업로드를 처리하고 있습니다.', status.HTTP_409_CONFLICT)

    try:
        if not default_storage.exists(session.objectKey):
            raise ValueError('업로드된 운동 상세 파일을 찾을 수 없습니다.')
        with default_storage.open(session.objectKey, 'rb') as source:
            detail_bytes = source.read(settings.DATA_UPLOAD_MAX_MEMORY_SIZE + 1)
        if len(detail_bytes) > settings.DATA_UPLOAD_MAX_MEMORY_SIZE:
            raise ValueError('운동 상세 파일이 허용 크기를 초과했습니다.')
        if len(detail_bytes) != session.fileSize:
            raise ValueError('운동 상세 파일 크기가 일치하지 않습니다.')
        if hashlib.sha256(detail_bytes).hexdigest() != session.contentHash:
            raise ValueError('운동 상세 파일의 해시가 일치하지 않습니다.')
        detail_json = json.loads(detail_bytes)
        metadata_body = WorkoutUploadSerializer(data=session.metadata)
        if not metadata_body.is_valid():
            raise ValueError('저장된 운동 메타데이터가 올바르지 않습니다.')
        data = metadata_body.validated_data
        detail_error = _validate_detail(detail_json, data)
        if detail_error:
            raise ValueError(detail_error)
        result = _persist_direct_upload(
            request,
            data,
            detail_json,
            detail_bytes,
            session.objectKey,
            session.contentHash,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        try:
            default_storage.delete(session.objectKey)
        except Exception:
            _logger.exception(
                'workout upload cleanup failed · user=%s · workout=%s',
                request.user.id,
                session.sourceWorkoutId,
            )
        WorkoutUploadSession.objects.filter(uploadId=upload_id).update(
            status=WorkoutUploadSession.Status.FAILED
        )
        return _upload_fail(
            request,
            str(error),
            stage='complete_validation',
            metadata=session.metadata,
        )
    except Exception:
        WorkoutUploadSession.objects.filter(uploadId=upload_id).update(
            status=WorkoutUploadSession.Status.FAILED
        )
        _logger.exception(
            'workout upload failed · stage=complete_processing · user=%s'
            ' · workout=%s',
            request.user.id,
            session.sourceWorkoutId,
        )
        raise

    WorkoutUploadSession.objects.filter(uploadId=upload_id).update(
        status=WorkoutUploadSession.Status.READY,
        workout_id=result['serverId'],
    )
    return Response({'s': True, **result})


def _session_metadata(data):
    return {
        key: value.isoformat() if hasattr(value, 'isoformat') else value
        for key, value in data.items()
        if key != 'fileSize'
    }


def _workout_upload_form(object_key: str, file_size: int):
    if not settings.S3_BUCKET_NAME or not settings.S3_PUBLIC_ENDPOINT_URL:
        raise ValueError('S3 직접 업로드 설정이 없습니다.')
    client = boto3.client(
        's3',
        endpoint_url=settings.S3_PUBLIC_ENDPOINT_URL,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
    )
    return client.generate_presigned_post(
        Bucket=settings.S3_BUCKET_NAME,
        Key=object_key,
        Fields={'Content-Type': 'application/json'},
        Conditions=[
            {'Content-Type': 'application/json'},
            ['content-length-range', 1, min(file_size, settings.DATA_UPLOAD_MAX_MEMORY_SIZE)],
        ],
        ExpiresIn=settings.S3_DOWNLOAD_EXPIRES_SECONDS,
    )


def _persist_direct_upload(
    request,
    data,
    detail_json,
    detail_bytes,
    object_key,
    content_hash,
):
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
        'kind', 'rawType', 'startAt', 'endAt', 'distanceMeters', 'kcal',
        'steps', 'flightsClimbed',
    )
    identity = {
        'user': request.user,
        'source': data['source'],
        'sourceName': data['sourceName'],
        'sourceWorkoutId': data['sourceWorkoutId'],
    }
    old_key = None
    with transaction.atomic():
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
            segment_count = ensure_h3_segments(workout, detail_json['route'])
            return {
                'serverId': workout.id,
                'created': created,
                'unchanged': not created and not changed,
                'trajectorySegmentCount': segment_count,
            }
        old_key = current.objectKey if current else None
        WorkoutDetail.objects.update_or_create(
            workout=workout,
            defaults={
                'objectKey': object_key,
                'contentHash': content_hash,
                'formatVersion': detail_json['formatVersion'],
                'routePointCount': len(detail_json['route']),
                'heartRateSampleCount': len(heart_rates),
                'fileSize': len(detail_bytes),
            },
        )
        if not created and not changed:
            workout.save(update_fields=['updatedAt'])
        segment_count = rebuild_h3_segments(workout, detail_json['route'])
    if old_key and old_key != object_key:
        try:
            if default_storage.exists(old_key):
                default_storage.delete(old_key)
        except Exception:
            _logger.exception(
                'workout upload old object cleanup failed · user=%s · key=%s',
                request.user.id,
                old_key,
            )
            raise
    return {
        'serverId': workout.id,
        'created': created,
        'unchanged': False,
        'trajectorySegmentCount': segment_count,
    }


def _fail(message: str, code: int = status.HTTP_400_BAD_REQUEST) -> Response:
    return Response({'s': False, 'msg': message}, status=code)


def _upload_fail(
    request,
    message: str,
    *,
    stage: str,
    code: int = status.HTTP_400_BAD_REQUEST,
    metadata=None,
    diagnostic=None,
) -> Response:
    data = metadata if isinstance(metadata, dict) else {}
    _logger.warning(
        'workout upload rejected · stage=%s · user=%s · source=%s'
        ' · workout=%s · reason=%s',
        stage,
        getattr(request.user, 'id', None),
        data.get('source', '-'),
        data.get('sourceWorkoutId', '-'),
        diagnostic or message,
    )
    return _fail(message, code)




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
        previous_timestamp = None
        for point in detail['route']:
            timestamp = parse_datetime(str(point['timestamp']))
            latitude = float(point['latitude'])
            longitude = float(point['longitude'])
            if timestamp is None or timezone.is_naive(timestamp):
                return '경로 좌표 시각 형식이 올바르지 않습니다.'
            if not metadata['startAt'] <= timestamp <= metadata['endAt']:
                return '경로 좌표 시각이 운동 시간 범위를 벗어났습니다.'
            if previous_timestamp is not None and timestamp < previous_timestamp:
                return '경로 좌표 시각은 오름차순이어야 합니다.'
            if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                return '경로 좌표 범위가 올바르지 않습니다.'
            previous_timestamp = timestamp
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
    """내 운동의 경로·심박 원본 파일을 받을 짧은 다운로드 URL을 반환한다."""
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
        download_url = _detail_download_url(request, detail.objectKey)
    except (OSError, TypeError, ValueError):
        return _fail(
            '운동 상세 파일 다운로드 주소를 만들지 못했습니다.',
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return Response({
        's': True,
        'downloadUrl': download_url,
        'expiresInSeconds': settings.S3_DOWNLOAD_EXPIRES_SECONDS,
        'contentHash': detail.contentHash,
        'fileSize': detail.fileSize,
    })


def _detail_download_url(request, object_key: str) -> str:
    public_endpoint = settings.S3_PUBLIC_ENDPOINT_URL
    if not settings.S3_BUCKET_NAME or not public_endpoint:
        return request.build_absolute_uri(default_storage.url(object_key))

    client = boto3.client(
        's3',
        endpoint_url=public_endpoint,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
    )
    return client.generate_presigned_url(
        'get_object',
        Params={'Bucket': settings.S3_BUCKET_NAME, 'Key': object_key},
        ExpiresIn=settings.S3_DOWNLOAD_EXPIRES_SECONDS,
    )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_workout_h3(request, workout_id):
    """내 운동의 현재 H3 체류 구간과 표시용 셀 경계를 반환한다."""
    try:
        workout = Workout.objects.get(
            pk=workout_id,
            user=request.user,
            deletedAt__isnull=True,
        )
    except Workout.DoesNotExist:
        return _fail('운동 기록을 찾을 수 없습니다.', status.HTTP_404_NOT_FOUND)

    segments = list(
        TrajectorySegment.objects.select_related('indexVersion')
        .filter(workout=workout, indexVersion__isActive=True)
        .order_by('sequence')
    )
    version = segments[0].indexVersion if segments else None
    resolution = version.parameters.get('resolution') if version else None
    return Response({
        's': True,
        'indexType': version.indexType if version else 'h3',
        'algorithmVersion': version.algorithmVersion if version else None,
        'resolution': resolution,
        'segments': [
            {
                'sequence': segment.sequence,
                'cellId': segment.cellId,
                'enteredAt': segment.period.lower,
                'exitedAt': segment.period.upper,
                'boundary': [
                    {'latitude': latitude, 'longitude': longitude}
                    for latitude, longitude in h3.cell_to_boundary(segment.cellId)
                ],
            }
            for segment in segments
        ],
    })
