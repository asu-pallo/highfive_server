import hashlib
import json
import base64
import binascii
import logging
from datetime import timedelta
import boto3
import h3
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.conf import settings
from django.contrib.auth.models import User
from config.object_storage import public_object_endpoint
from user_manager.profile_storage import profile_image_url
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
    UserFamiliarity,
    UserWeeklyWorkoutStat,
    Workout,
    WorkoutDetail,
)
from .encounters import (
    encounter_candidates,
    encounter_summaries,
    load_encounter_relations,
    profile_user,
    workout_encounter_candidates,
)
from .familiarity import refresh_user_familiarity
from .high_fives import create_high_five_for_encounter
from .serializers import WorkoutSerializer, WorkoutUploadSerializer
from .spatial_index import ensure_h3_segments, rebuild_h3_segments
from .workout_statistics import (
    rebuild_distance_records,
    workout_comparison,
    workout_statistics,
)
from .workout_metrics import metrics_json, rebuild_workout_metrics
from .route_simplification import simplify_route
from .weekly_stats import rebuild_weekly_stat, week_start_for


_workout_page_size = 10
_logger = logging.getLogger(__name__)
_route_time_tolerance = timedelta(minutes=1)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def upload_workout(request):
    """multipart 운동 데이터를 검증하고 파생 데이터까지 생성한다.

    메타데이터·경로·심박수를 검증한 뒤 지도용 경로는 단순화해 객체 저장소에
    보관하고, 원본 경로로 H3·PR·상세 그래프 통계를 생성한다. 심박수 원본은
    통계 생성에만 사용하며 객체 저장소에는 남기지 않는다.
    """
    body = WorkoutUploadSerializer(data=request.data)
    if not body.is_valid():
        return _upload_fail(
            request,
            '운동 업로드 정보가 올바르지 않습니다.',
            stage='upload_metadata',
            metadata=request.data,
            diagnostic=body.errors,
        )
    data = body.validated_data
    keys = _detail_object_keys(request.user.id, data)
    saved_keys = []
    try:
        route_json, route_bytes = _read_uploaded_detail(
            request.FILES.get('routeFile'),
            data['routeContentHash'],
            data['routeFileSize'],
            '경로',
        )
        heart_rate_json, _ = _read_uploaded_detail(
            request.FILES.get('heartRateFile'),
            data['heartRateContentHash'],
            data['heartRateFileSize'],
            '심박수',
        )
        detail_json = {
            'route': route_json.get('route', []) if route_json else [],
            'heartRate': heart_rate_json.get('heartRate', []) if heart_rate_json else [],
        }
        for payload, sample_key in (
            (route_json, 'route'),
            (heart_rate_json, 'heartRate'),
        ):
            if payload:
                error = _validate_detail_file(payload, data, sample_key)
                if error:
                    raise ValueError(error)
        error = _validate_detail(detail_json, data)
        if error:
            raise ValueError(error)
        _clamp_route_to_workout(detail_json['route'], data['startAt'], data['endAt'])

        simplified_route = simplify_route(detail_json['route'])
        stored_route_json = dict(route_json) if route_json else None
        if stored_route_json is not None:
            stored_route_json['route'] = simplified_route
            route_bytes = json.dumps(
                stored_route_json, ensure_ascii=False, separators=(',', ':'),
            ).encode()
        # 심박 원본은 통계 생성에만 사용하고 객체 저장소에는 남기지 않는다.
        for key, content in ((keys['route'], route_bytes),):
            if key and not default_storage.exists(key):
                default_storage.save(key, ContentFile(content))
                saved_keys.append(key)

        result = _persist_workout_upload(
            request,
            data,
            detail_json,
            route_bytes,
            keys,
            simplified_route,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        _delete_objects(saved_keys)
        return _upload_fail(
            request,
            str(error),
            stage='upload_validation',
            metadata=data,
        )
    except Exception:
        _delete_objects(saved_keys)
        _logger.exception(
            'workout upload failed · stage=upload_processing · user=%s · workout=%s',
            request.user.id,
            data['sourceWorkoutId'],
        )
        raise
    return Response({'s': True, **result})


def _read_uploaded_detail(upload, expected_hash, expected_size, label):
    """업로드된 JSON의 용량·SHA-256을 검증하고 파싱 결과와 원본 bytes를 반환한다."""
    if not expected_hash:
        if upload is not None or expected_size != 0:
            raise ValueError(f'{label} 파일 정보가 일치하지 않습니다.')
        return None, b''
    if upload is None:
        raise ValueError(f'{label} 파일이 필요합니다.')
    content = upload.read(settings.DATA_UPLOAD_MAX_MEMORY_SIZE + 1)
    if len(content) > settings.DATA_UPLOAD_MAX_MEMORY_SIZE:
        raise ValueError(f'{label} 파일이 허용 크기를 초과했습니다.')
    if len(content) != expected_size:
        raise ValueError(f'{label} 파일 크기가 일치하지 않습니다.')
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise ValueError(f'{label} 파일의 해시가 일치하지 않습니다.')
    return json.loads(content), content


def _delete_objects(keys):
    """업로드 처리 실패 시 이번 요청에서 새로 저장한 객체들을 정리한다."""
    for key in keys:
        try:
            default_storage.delete(key)
        except Exception:
            _logger.exception('workout upload object cleanup failed · key=%s', key)


def _detail_object_keys(user_id: int, data: dict) -> dict[str, str | None]:
    """사용자와 콘텐츠 해시를 기준으로 중복 저장을 피하는 객체 키를 만든다."""
    def key(kind, content_hash):
        return f'workouts/{user_id}/{kind}/{content_hash}.json' if content_hash else None

    return {'route': key('route', data['routeContentHash'])}


def _persist_workout_upload(
    request,
    data,
    detail_json,
    route_bytes,
    object_keys,
    stored_route,
):
    """검증된 운동과 파생 데이터를 하나의 DB 트랜잭션으로 반영한다.

    Workout·WorkoutDetail을 저장한 뒤 원본 경로를 이용해 H3와 거리별 PR을
    만들고, 경로·심박수 원본으로 50포인트 그래프 및 year/all 집계를 갱신한다.
    """
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
    with transaction.atomic():
        defaults = {field: data.get(field) for field in mutable_fields}
        defaults.update(heart_summary)
        workout, created = Workout.objects.get_or_create(**identity, defaults=defaults)
        previous_week = None if created else week_start_for(workout.startAt, request.user)
        previous_kind = None if created else workout.kind
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
            if not workout.distanceRecords.exists():
                rebuild_distance_records(workout, detail_json['route'])
            if not hasattr(workout, 'metrics'):
                rebuild_workout_metrics(
                    workout, detail_json['route'], detail_json['heartRate']
                )
            detail_unchanged = True
        else:
            if current:
                if current.routeObjectKey:
                    old_keys.add(current.routeObjectKey)
            WorkoutDetail.objects.update_or_create(
                workout=workout,
                defaults={
                    'routeObjectKey': object_keys['route'],
                    'routeContentHash': data['routeContentHash'],
                    'routeFileSize': len(route_bytes),
                    'contentHash': data['detailContentHash'],
                    'formatVersion': 1,
                    'routePointCount': len(stored_route),
                    'heartRateSampleCount': len(heart_rates),
                    'fileSize': len(route_bytes),
                },
            )
            if not created and not changed:
                workout.save(update_fields=['updatedAt'])
            segment_count = rebuild_h3_segments(workout, detail_json['route'])
            rebuild_distance_records(workout, detail_json['route'])
            rebuild_workout_metrics(
                workout, detail_json['route'], detail_json['heartRate']
            )
        current_week = week_start_for(workout.startAt, request.user)
        rebuild_weekly_stat(request.user, current_week, workout.kind)
        if previous_week is not None and (
            previous_week != current_week or previous_kind != workout.kind
        ):
            rebuild_weekly_stat(request.user, previous_week, previous_kind)
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
    return {
        'serverId': workout.id,
        'created': created,
        'unchanged': detail_unchanged and not created and not changed,
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




def _validate_detail_file(detail, metadata, sample_key):
    """상세 JSON의 버전·개수·운동 식별정보가 메타데이터와 같은지 검사한다."""
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
    """경로 좌표·시각과 심박수 샘플 값이 허용 범위인지 검사한다."""
    if not isinstance(detail.get('route'), list):
        return 'route는 배열이어야 합니다.'
    if not isinstance(detail.get('heartRate'), list):
        return 'heartRate는 배열이어야 합니다.'
    try:
        previous_timestamp = None
        allowed_start = metadata['startAt'] - _route_time_tolerance
        allowed_end = metadata['endAt'] + _route_time_tolerance
        for point in detail['route']:
            timestamp = parse_datetime(str(point['timestamp']))
            latitude = float(point['latitude'])
            longitude = float(point['longitude'])
            if timestamp is None or timezone.is_naive(timestamp):
                return '경로 좌표 시각 형식이 올바르지 않습니다.'
            if not allowed_start <= timestamp <= allowed_end:
                return '경로 좌표 시각이 운동 시간 허용 범위를 벗어났습니다.'
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


def _clamp_route_to_workout(route, started_at, ended_at):
    """허용한 HealthKit 경계 오차가 H3 체류 시간으로 번지지 않게 잘라낸다."""
    for point in route:
        timestamp = parse_datetime(str(point['timestamp']))
        if timestamp < started_at:
            point['timestamp'] = started_at.isoformat()
        elif timestamp > ended_at:
            point['timestamp'] = ended_at.isoformat()


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_workouts(request):
    """서버 스냅샷 시각까지 변경된 내 운동을 10건씩 반환한다."""
    snapshot_value = request.query_params.get('snapshot')
    snapshot_at = parse_datetime(snapshot_value) if snapshot_value else timezone.now()
    if snapshot_at is None or timezone.is_naive(snapshot_at):
        return _fail('snapshot은 시간대가 포함된 ISO 8601 시각이어야 합니다.')
    workouts = Workout.objects.select_related('detail').filter(
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


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def download_workout_encounters(request):
    """현재 피드 페이지 운동의 마주침·하이파이브 요약을 실시간 판정한다."""
    raw_ids = request.data.get('workoutIds')
    if not isinstance(raw_ids, list):
        return _fail('workoutIds는 운동 ID 목록이어야 합니다.')
    try:
        workout_ids = list(dict.fromkeys(int(value) for value in raw_ids))
    except (TypeError, ValueError):
        return _fail('workoutIds 형식이 올바르지 않습니다.')
    if not workout_ids or len(workout_ids) > _workout_page_size:
        return _fail(f'운동은 1~{_workout_page_size}건까지 조회할 수 있습니다.')

    owned_ids = list(
        Workout.objects.filter(
            id__in=workout_ids,
            user=request.user,
            deletedAt__isnull=True,
        ).values_list('id', flat=True)
    )
    if len(owned_ids) != len(workout_ids):
        return _fail('조회할 수 없는 운동이 포함되어 있습니다.', status.HTTP_404_NOT_FOUND)

    summaries = encounter_summaries(
        owned_ids,
        profile_image_url_for=lambda object_key: profile_image_url(
            request, object_key
        ),
    )
    return Response({
        's': True,
        'summaries': [
            {'serverId': workout_id, **summaries[workout_id]}
            for workout_id in workout_ids
        ],
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_encounter_candidates(request, workout_id):
    """한 H3 영역 후보를 반환하고 현재 운동에서 누른 상대만 공개한다."""
    cell_id = request.query_params.get('h3Cell', '').strip()
    if not cell_id:
        return _fail('h3Cell이 필요합니다.')
    try:
        workout = Workout.objects.get(
            pk=workout_id,
            user=request.user,
            deletedAt__isnull=True,
        )
    except Workout.DoesNotExist:
        return _fail('운동을 찾을 수 없습니다.', status.HTTP_404_NOT_FOUND)

    matches = encounter_candidates(workout.id, cell_id)
    candidate_ids = [match.matched_segment.user_id for match in matches]
    relations = load_encounter_relations(
        viewer_id=request.user.id,
        workout=workout,
        candidate_ids=candidate_ids,
    )
    sent = relations.sent_by_user
    received_user_ids = relations.received_user_ids
    familiarity_by_user = relations.familiarity_by_user
    return Response({
        's': True,
        'candidates': [
            {
                'candidateId': match.matched_segment.user_id,
                'encounteredAt': match.overlap_started_at,
                'alreadyHighFived': match.matched_segment.user_id in sent,
                'receivedHighFive': (
                    match.matched_segment.user_id in received_user_ids
                ),
                'metCount': (
                    familiarity_by_user[match.matched_segment.user_id].metCount
                    if match.matched_segment.user_id in sent
                    else 0
                ),
                **(
                    {
                        'familiarSince': familiarity_by_user[
                            match.matched_segment.user_id
                        ].firstHighFiveAt
                    }
                    if match.matched_segment.user_id in sent
                    and match.matched_segment.user_id in familiarity_by_user
                    else {}
                ),
                **(
                    {
                        'profile': profile_user(
                            sent[match.matched_segment.user_id].toUser,
                            lambda key: profile_image_url(request, key),
                        )
                    }
                    if match.matched_segment.user_id in sent
                    else {}
                ),
            }
            for match in matches
        ],
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_encounter_distribution(request, workout_id):
    """상세 화면 분포도에 필요한 운동 전체의 마주침 데이터를 반환한다."""
    try:
        workout = Workout.objects.get(
            pk=workout_id,
            user=request.user,
            deletedAt__isnull=True,
        )
    except Workout.DoesNotExist:
        return _fail('운동을 찾을 수 없습니다.', status.HTTP_404_NOT_FOUND)

    matches = workout_encounter_candidates(workout.id)
    candidate_ids = [match.matched_segment.user_id for match in matches]
    relations = load_encounter_relations(
        viewer_id=request.user.id,
        workout=workout,
        candidate_ids=candidate_ids,
    )
    sent = relations.sent_by_user
    received_user_ids = relations.received_user_ids
    familiarity_by_user = relations.familiarity_by_user

    return Response({
        's': True,
        'me': _distribution_workout(workout),
        'people': [
            {
                'candidateId': match.matched_segment.user_id,
                'encounterOrder': encounter_order,
                'h3Cell': match.source_segment.cellId,
                'encounteredAt': match.overlap_started_at,
                'alreadyHighFived': match.matched_segment.user_id in sent,
                'receivedHighFive': (
                    match.matched_segment.user_id in received_user_ids
                ),
                'metCount': (
                    familiarity_by_user[match.matched_segment.user_id].metCount
                    if match.matched_segment.user_id in sent
                    and match.matched_segment.user_id in familiarity_by_user
                    else 0
                ),
                **_distribution_workout(match.matched_segment.workout),
                **(
                    {
                        'profile': profile_user(
                            sent[match.matched_segment.user_id].toUser,
                            lambda key: profile_image_url(request, key),
                        )
                    }
                    if match.matched_segment.user_id in sent
                    else {}
                ),
            }
            for encounter_order, match in enumerate(matches, start=1)
        ],
    })


def _distribution_workout(workout):
    distance = workout.distanceMeters
    elapsed_seconds = max((workout.endAt - workout.startAt).total_seconds(), 0)
    pace = (
        elapsed_seconds / (distance / 1000)
        if distance is not None and distance > 0
        else None
    )
    return {
        'distanceMeters': distance,
        'paceSecondsPerKm': pace,
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def refresh_encounter_familiarity(request, user_id):
    """상대 피드·프로필 진입 시 해당 사용자 쌍의 만남 횟수만 최신화한다."""
    try:
        familiarity = refresh_user_familiarity(
            user_id=request.user.id,
            other_user_id=user_id,
        )
    except UserFamiliarity.DoesNotExist:
        return _fail(
            '하이파이브로 공개된 사용자 관계를 찾을 수 없습니다.',
            status.HTTP_404_NOT_FOUND,
        )
    return Response({
        's': True,
        'userId': user_id,
        'metCount': familiarity.metCount,
        'familiarSince': familiarity.firstHighFiveAt,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_user_weekly_workout_stats(request, user_id):
    """공개된 상대의 최근 53주 러닝 합계를 오래된 주부터 반환한다."""
    if user_id != request.user.id:
        first_id, second_id = sorted((request.user.id, user_id))
        if not UserFamiliarity.objects.filter(
            firstUser_id=first_id,
            secondUser_id=second_id,
        ).exists():
            return _fail(
                '하이파이브로 공개된 사용자 관계를 찾을 수 없습니다.',
                status.HTTP_404_NOT_FOUND,
            )

    try:
        user = User.objects.select_related('profile').get(pk=user_id)
    except User.DoesNotExist:
        return _fail('사용자를 찾을 수 없습니다.', status.HTTP_404_NOT_FOUND)

    current_week = week_start_for(timezone.now(), user)
    first_week = current_week - timedelta(weeks=52)
    stored = {
        item.weekStart: item
        for item in UserWeeklyWorkoutStat.objects.filter(
            user=user,
            workoutKind='running',
            weekStart__gte=first_week,
            weekStart__lte=current_week,
        )
    }
    weeks = []
    for offset in range(53):
        week_start = first_week + timedelta(weeks=offset)
        item = stored.get(week_start)
        weeks.append({
            'weekStart': week_start,
            'distanceMeters': item.distanceMeters if item else 0,
            'workoutCount': item.workoutCount if item else 0,
        })
    return Response({'s': True, 'weeks': weeks})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_high_five(request, workout_id):
    """현재 운동에서 마주친 한 사용자에게 일방향 하이파이브를 남긴다."""
    try:
        target_user_id = int(request.data.get('candidateId'))
    except (TypeError, ValueError):
        return _fail('candidateId 형식이 올바르지 않습니다.')
    cell_id = str(request.data.get('h3Cell', '')).strip()
    if not cell_id:
        return _fail('h3Cell이 필요합니다.')
    try:
        workout = Workout.objects.get(
            pk=workout_id,
            user=request.user,
            deletedAt__isnull=True,
        )
    except Workout.DoesNotExist:
        return _fail('운동을 찾을 수 없습니다.', status.HTTP_404_NOT_FOUND)

    creation = create_high_five_for_encounter(
        workout=workout,
        target_user_id=target_user_id,
        cell_id=cell_id,
    )
    if creation is None:
        return _fail(
            '이 운동에서 하이파이브할 수 없는 사용자입니다.',
            status.HTTP_404_NOT_FOUND,
        )
    return Response({
        's': True,
        'created': creation.created,
        'profile': profile_user(
            creation.high_five.toUser,
            lambda key: profile_image_url(request, key),
        ),
        'metCount': creation.familiarity.metCount,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_workout_detail(request, workout_id):
    """내 운동의 경로 다운로드 URL과 가공 통계를 반환한다."""
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
    except (OSError, TypeError, ValueError):
        return _fail(
            '운동 상세 파일 다운로드 주소를 만들지 못했습니다.',
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return Response({
        's': True,
        'routeDownloadUrl': route_url,
        'expiresInSeconds': settings.S3_DOWNLOAD_EXPIRES_SECONDS,
        'detailContentHash': detail.contentHash,
        'routeContentHash': detail.routeContentHash,
        'routeFileSize': detail.routeFileSize,
        'statistics': workout_statistics(workout),
        'metrics': metrics_json(workout),
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_workout_statistics(request, workout_id):
    """현재 운동의 거리별 기록과 작성자의 최근 러닝 분포를 반환한다."""
    try:
        workout = Workout.objects.get(
            pk=workout_id,
            user=request.user,
            deletedAt__isnull=True,
        )
    except Workout.DoesNotExist:
        return _fail('운동 기록을 찾을 수 없습니다.', status.HTTP_404_NOT_FOUND)
    return Response({'s': True, **workout_statistics(workout)})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_workout_comparison(request, workout_id):
    """버튼으로 선택한 비교 기준의 50포인트 통계를 반환한다."""
    try:
        workout = Workout.objects.get(
            pk=workout_id,
            user=request.user,
            deletedAt__isnull=True,
        )
    except Workout.DoesNotExist:
        return _fail('운동 기록을 찾을 수 없습니다.', status.HTTP_404_NOT_FOUND)

    preset = request.query_params.get('preset', '')
    comparison_id = request.query_params.get('comparisonWorkoutId')
    comparison_year = request.query_params.get('year')
    try:
        comparison_id = int(comparison_id) if comparison_id else None
        comparison_year = int(comparison_year) if comparison_year else None
    except ValueError:
        return _fail('비교 조건이 올바르지 않습니다.', status.HTTP_400_BAD_REQUEST)

    comparison = workout_comparison(
        workout,
        preset,
        comparison_id,
        comparison_year,
    )
    if comparison is None:
        return _fail('비교할 운동 기록이 없습니다.', status.HTTP_404_NOT_FOUND)
    return Response({'s': True, 'comparison': comparison})


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
    """개발에서는 API 요청 호스트를 그대로 사용해 MinIO 공개 주소를 만든다.

    프로필 이미지도 같은 규칙이 필요해 `config.object_storage` 로 옮겼다.
    """
    return public_object_endpoint(request)


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
