import hashlib
import json
import base64
import binascii
import logging
from urllib.parse import urlparse

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
    HighFive,
    TrajectorySegment,
    Workout,
    WorkoutDetail,
)
from .high_five import clear_high_fives, high_five_summaries, rebuild_high_fives
from .serializers import (
    WorkoutSerializer,
    WorkoutUploadPrepareSerializer,
    WorkoutUploadSerializer,
)
from .spatial_index import ensure_h3_segments, rebuild_h3_segments


_workout_page_size = 10
_logger = logging.getLogger(__name__)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def prepare_workout_upload(request):
    """DB 상태를 만들지 않고 경로·심박 파일의 S3 업로드 폼을 발급한다."""
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
    for label, size in (
        ('경로', data['routeFileSize']),
        ('심박수', data['heartRateFileSize']),
    ):
        if size > settings.DATA_UPLOAD_MAX_MEMORY_SIZE:
            return _upload_fail(
                request,
                f'{label} 파일이 허용 크기를 초과했습니다.',
                stage='prepare_file_size',
                code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                metadata=data,
            )

    keys = _detail_object_keys(request.user.id, data)
    try:
        route_upload = _prepare_file_upload(
            request, keys['route'], data['routeFileSize']
        )
        heart_rate_upload = _prepare_file_upload(
            request, keys['heartRate'], data['heartRateFileSize']
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
        'routeUpload': route_upload,
        'heartRateUpload': heart_rate_upload,
        'expiresInSeconds': settings.S3_DOWNLOAD_EXPIRES_SECONDS,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_workout(request):
    """업로드된 경로·심박 파일을 검증하고 운동·상세·H3를 생성한다."""
    body = WorkoutUploadSerializer(data=request.data)
    if not body.is_valid():
        return _upload_fail(
            request,
            '운동 생성 정보가 올바르지 않습니다.',
            stage='create_metadata',
            metadata=request.data,
            diagnostic=body.errors,
        )
    data = body.validated_data
    keys = _detail_object_keys(request.user.id, data)

    try:
        route_json, route_bytes = _read_detail_file(
            keys['route'], data['routeContentHash'], data['routeFileSize'], '경로'
        )
        heart_rate_json, heart_rate_bytes = _read_detail_file(
            keys['heartRate'],
            data['heartRateContentHash'],
            data['heartRateFileSize'],
            '심박수',
        )
        detail_json = {
            'route': route_json.get('route', []) if route_json else [],
            'heartRate': (
                heart_rate_json.get('heartRate', []) if heart_rate_json else []
            ),
        }
        for payload, sample_key in (
            (route_json, 'route'),
            (heart_rate_json, 'heartRate'),
        ):
            if payload:
                detail_error = _validate_detail_file(payload, data, sample_key)
                if detail_error:
                    raise ValueError(detail_error)
        detail_error = _validate_detail(detail_json, data)
        if detail_error:
            raise ValueError(detail_error)
        result = _persist_direct_upload(
            request,
            data,
            detail_json,
            route_bytes,
            heart_rate_bytes,
            keys,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        return _upload_fail(
            request,
            str(error),
            stage='create_validation',
            metadata=data,
        )
    except Exception:
        _logger.exception(
            'workout upload failed · stage=create_processing · user=%s'
            ' · workout=%s',
            request.user.id,
            data['sourceWorkoutId'],
        )
        raise
    return Response({'s': True, **result})


def _detail_object_keys(user_id: int, data: dict) -> dict[str, str | None]:
    def key(kind, content_hash):
        return f'workouts/{user_id}/{kind}/{content_hash}.json' if content_hash else None

    return {
        'route': key('route', data['routeContentHash']),
        'heartRate': key('heart-rate', data['heartRateContentHash']),
    }


def _prepare_file_upload(request, object_key: str | None, file_size: int):
    if object_key is None:
        return {'uploadRequired': False, 'uploadUrl': None, 'uploadFields': {}}
    upload_required = not default_storage.exists(object_key)
    upload = (
        _workout_upload_form(request, object_key, file_size)
        if upload_required
        else None
    )
    return {
        'uploadRequired': upload_required,
        'uploadUrl': upload['url'] if upload else None,
        'uploadFields': upload['fields'] if upload else {},
    }


def _read_detail_file(object_key, expected_hash, expected_size, label):
    if object_key is None:
        return None, b''
    if not default_storage.exists(object_key):
        raise ValueError(f'업로드된 {label} 파일을 찾을 수 없습니다.')
    with default_storage.open(object_key, 'rb') as source:
        content = source.read(settings.DATA_UPLOAD_MAX_MEMORY_SIZE + 1)
    if len(content) > settings.DATA_UPLOAD_MAX_MEMORY_SIZE:
        raise ValueError(f'{label} 파일이 허용 크기를 초과했습니다.')
    if len(content) != expected_size:
        raise ValueError(f'{label} 파일 크기가 일치하지 않습니다.')
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise ValueError(f'{label} 파일의 해시가 일치하지 않습니다.')
    return json.loads(content), content


def _workout_upload_form(request, object_key: str, file_size: int):
    public_endpoint = _public_object_endpoint(request)
    if not settings.S3_BUCKET_NAME or not public_endpoint:
        raise ValueError('S3 직접 업로드 설정이 없습니다.')
    client = boto3.client(
        's3',
        endpoint_url=public_endpoint,
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
    route_bytes,
    heart_rate_bytes,
    object_keys,
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
    old_keys = set()
    detail_unchanged = False
    previously_affected_ids = set()
    previous_relation_keys = set()
    with transaction.atomic():
        defaults = {field: data.get(field) for field in mutable_fields}
        defaults.update(heart_summary)
        workout, created = Workout.objects.get_or_create(**identity, defaults=defaults)
        if not created:
            previous_relations = HighFive.objects.filter(
                Q(workoutA=workout) | Q(workoutB=workout)
            ).values_list(
                'workoutA_id',
                'workoutB_id',
                'indexVersion_id',
                'h3Cell',
                'overlapStartedAt',
                'overlapEndedAt',
            )
            for relation in previous_relations:
                workout_a_id, workout_b_id = relation[:2]
                previously_affected_ids.update((workout_a_id, workout_b_id))
                previous_relation_keys.add(relation)
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
        if current and current.contentHash == data['detailContentHash']:
            segment_count = ensure_h3_segments(workout, detail_json['route'])
            detail_unchanged = True
        else:
            if current:
                old_keys.update(
                    key for key in (
                        current.routeObjectKey,
                        current.heartRateObjectKey,
                    ) if key
                )
            WorkoutDetail.objects.update_or_create(
                workout=workout,
                defaults={
                    'routeObjectKey': object_keys['route'],
                    'routeContentHash': data['routeContentHash'],
                    'routeFileSize': len(route_bytes),
                    'heartRateObjectKey': object_keys['heartRate'],
                    'heartRateContentHash': data['heartRateContentHash'],
                    'heartRateFileSize': len(heart_rate_bytes),
                    'contentHash': data['detailContentHash'],
                    'formatVersion': 1,
                    'routePointCount': len(detail_json['route']),
                    'heartRateSampleCount': len(heart_rates),
                    'fileSize': len(route_bytes) + len(heart_rate_bytes),
                },
            )
            if not created and not changed:
                workout.save(update_fields=['updatedAt'])
            segment_count = rebuild_h3_segments(workout, detail_json['route'])
    current_keys = {key for key in object_keys.values() if key}
    for old_key in old_keys - current_keys:
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
    # 운동과 H3가 커밋된 뒤 판정해야 동시에 올라온 다른 사용자의 경로도 볼 수 있다.
    # 판정 실패 시 이미 전송한 객체는 그대로 남아 create 재시도에서 재사용된다.
    if segment_count == 0:
        clear_high_fives(
            workout,
            previously_affected_ids=previously_affected_ids,
            had_previous_relations=bool(previous_relation_keys),
        )
        high_five_count = 0
    else:
        high_five_count = rebuild_high_fives(
            workout,
            previously_affected_ids=previously_affected_ids,
            previous_relation_keys=previous_relation_keys,
        )
    return {
        'serverId': workout.id,
        'created': created,
        'unchanged': detail_unchanged and not created and not changed,
        'trajectorySegmentCount': segment_count,
        'highFiveCount': high_five_count,
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




def _validate_detail_file(detail, metadata, sample_key):
    if not isinstance(detail, dict) or detail.get('formatVersion') != 1:
        return '지원하지 않는 운동 상세 파일 버전입니다.'
    if not isinstance(detail.get(sample_key), list):
        return f'{sample_key}는 배열이어야 합니다.'
    if len(detail[sample_key]) > 100_000:
        return f'{sample_key} 샘플이 너무 많습니다.'
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
    return None


def _validate_detail(detail, metadata):
    if not isinstance(detail.get('route'), list):
        return 'route는 배열이어야 합니다.'
    if not isinstance(detail.get('heartRate'), list):
        return 'heartRate는 배열이어야 합니다.'
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
    """서버 스냅샷 시각까지 변경된 내 운동을 10건씩 반환한다."""
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

    serialized = WorkoutSerializer(page, many=True).data
    summaries = high_five_summaries([workout.id for workout in page])
    for workout, item in zip(page, serialized):
        item['highFives'] = summaries[workout.id]

    return Response({
        's': True,
        'serverTime': snapshot_at,
        'workouts': serialized,
        'nextCursor': next_cursor,
        'hasMore': has_more,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_workout_detail(request, workout_id):
    """내 운동의 경로·심박 파일별 짧은 다운로드 URL을 반환한다."""
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
        route_url = (
            _detail_download_url(request, detail.routeObjectKey)
            if detail.routeObjectKey else None
        )
        heart_rate_url = (
            _detail_download_url(request, detail.heartRateObjectKey)
            if detail.heartRateObjectKey else None
        )
    except (OSError, TypeError, ValueError):
        return _fail(
            '운동 상세 파일 다운로드 주소를 만들지 못했습니다.',
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return Response({
        's': True,
        'routeDownloadUrl': route_url,
        'heartRateDownloadUrl': heart_rate_url,
        'expiresInSeconds': settings.S3_DOWNLOAD_EXPIRES_SECONDS,
        'detailContentHash': detail.contentHash,
        'routeContentHash': detail.routeContentHash,
        'routeFileSize': detail.routeFileSize,
        'heartRateContentHash': detail.heartRateContentHash,
        'heartRateFileSize': detail.heartRateFileSize,
    })


def _detail_download_url(request, object_key: str) -> str:
    public_endpoint = _public_object_endpoint(request)
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


def _public_object_endpoint(request) -> str | None:
    """개발에서는 API 요청 호스트를 그대로 사용해 MinIO 공개 주소를 만든다."""
    if not settings.DEBUG:
        return settings.S3_PUBLIC_ENDPOINT_URL

    hostname = urlparse(f'//{request.get_host()}').hostname
    if not hostname:
        return settings.S3_PUBLIC_ENDPOINT_URL
    return f'{request.scheme}://{hostname}:9000'


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
