"""로컬 앱에서 피드·하이파이브 조합을 확인할 실제 서버 데이터를 만든다."""

from __future__ import annotations

import hashlib
import io
import json
import math
from dataclasses import dataclass
from datetime import timedelta

import h3
from PIL import Image, ImageDraw
from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from psycopg.types.range import Range

from config.object_storage import build_client
from user_manager.models import LoginProvider, Profile
from workout_manager.models import (
    HighFive,
    TrajectorySegment,
    UserFamiliarity,
    Workout,
    WorkoutDetail,
    WorkoutMetrics,
)
from workout_manager.familiarity import refresh_familiarities
from workout_manager.route_simplification import simplify_route
from workout_manager.spatial_index import H3_RESOLUTION, rebuild_h3_segments
from workout_manager.workout_metrics import rebuild_workout_metrics
from workout_manager.workout_statistics import rebuild_distance_records
from workout_manager.weekly_stats import rebuild_weekly_stat, week_start_for


SOURCE = 'native'
SOURCE_NAME = 'HighFive UI Preview'
SOURCE_ID_PREFIX = 'ui-preview'


@dataclass(frozen=True)
class PreviewScenario:
    label: str
    # 경로 위에서 선택한 H3 셀마다 배치할 마주친 사용자 수다.
    cell_counts: tuple[int, ...]
    # 같은 위치의 사용자 중 내가 하이파이브한 수다.
    sent_high_five_counts: tuple[int, ...]
    # 같은 위치의 사용자 중 나에게 먼저 하이파이브한 수다.
    received_high_five_counts: tuple[int, ...]

    def __post_init__(self):
        count = len(self.cell_counts)
        if count != len(self.sent_high_five_counts) or count != len(
            self.received_high_five_counts
        ):
            raise ValueError('셀별 인원과 하이파이브 수의 길이가 달라요.')
        for runners, sent, received in zip(
            self.cell_counts,
            self.sent_high_five_counts,
            self.received_high_five_counts,
            strict=True,
        ):
            if not 0 <= sent <= runners or not 0 <= received <= runners:
                raise ValueError(
                    '보내거나 받은 하이파이브 수는 마주친 인원을 넘을 수 없어요.'
                )


SCENARIOS = (
    # 한 명을 눌렀을 때 즉시 하이파이브하는 흐름을 확인한다.
    PreviewScenario('한 명 만남', (1,), (0,), (0,)),
    # 여러 H3 셀, 보낸 하이파이브, 받은 하이파이브가 섞인 목록이다.
    PreviewScenario('여러 명 만남 · 세 셀', (2, 3, 4), (0, 1, 1), (1, 1, 2)),
    PreviewScenario(
        '여러 명 만남 · 네 셀',
        (3, 5, 2, 4),
        (1, 2, 0, 1),
        (1, 1, 1, 2),
    ),
)


@dataclass(frozen=True)
class WorkoutTemplate:
    workout: Workout
    route: list[dict]
    heart_rate: list[dict]


class Command(BaseCommand):
    help = '로컬 UI 확인용 운동·H3·하이파이브 데이터를 생성하거나 제거한다.'

    def add_arguments(self, parser):
        parser.add_argument('--owner-id', type=int, required=True)
        parser.add_argument(
            '--clear',
            action='store_true',
            help='지정한 사용자의 UI Preview 데이터만 제거한다.',
        )
        parser.add_argument(
            '--source-workout-id',
            type=int,
            help='경로·심박 원본으로 사용할 오너의 서버 운동 ID',
        )

    def handle(self, *args, **options):
        self._guard_local_environment()
        owner = self._owner(options['owner_id'])
        self._clear(owner)
        if options['clear']:
            self.stdout.write(self.style.SUCCESS('UI Preview 데이터를 제거했습니다.'))
            return

        template = self._load_template(owner, options.get('source_workout_id'))
        self._seed(owner, template)
        self.stdout.write(
            self.style.SUCCESS(
                f'UI Preview 운동 {len(SCENARIOS)}건을 생성했습니다. '
                '앱에서 풀다운 새로고침하세요.'
            )
        )

    def _guard_local_environment(self):
        if not settings.DEBUG:
            raise CommandError('DEBUG 환경에서만 실행할 수 있습니다.')
        database = settings.DATABASES['default']
        if database.get('HOST') not in {'localhost', '127.0.0.1'}:
            raise CommandError('로컬 PostgreSQL에서만 실행할 수 있습니다.')
        if not settings.S3_BUCKET_NAME or not settings.S3_PUBLIC_BUCKET_NAME:
            raise CommandError('로컬 MinIO 버킷 설정이 필요합니다.')

    @staticmethod
    def _owner(owner_id: int) -> User:
        try:
            return User.objects.get(pk=owner_id)
        except User.DoesNotExist as error:
            raise CommandError(f'사용자 {owner_id}를 찾을 수 없습니다.') from error

    def _load_template(
        self,
        owner: User,
        source_workout_id: int | None,
    ) -> WorkoutTemplate:
        candidates = Workout.objects.filter(
            user=owner,
            deletedAt__isnull=True,
            detail__routeObjectKey__isnull=False,
        ).exclude(sourceWorkoutId__startswith=SOURCE_ID_PREFIX)
        if source_workout_id is not None:
            candidates = candidates.filter(pk=source_workout_id)
        workout = candidates.select_related('detail', 'metrics').order_by(
            '-startAt', '-id'
        ).first()
        if workout is None:
            suffix = f' ID {source_workout_id}' if source_workout_id else ''
            raise CommandError(
                f'사용자 {owner.id}의 경로·심박이 모두 있는 원본 운동{suffix}을 찾지 못했습니다.'
            )
        try:
            route_payload = _read_json_object(workout.detail.routeObjectKey)
            route = route_payload.get('route', [])
            heart_rate = _template_heart_rate(workout)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise CommandError('원본 운동의 경로·심박 파일을 읽지 못했습니다.') from error
        if len(route) < 2:
            raise CommandError('원본 운동의 경로 샘플이 비어 있습니다.')
        self.stdout.write(
            f'원본 운동 #{workout.id} 사용 · 경로 {len(route)}개 · 심박 {len(heart_rate)}개'
        )
        return WorkoutTemplate(workout, route, heart_rate)

    def _seed(self, owner: User, template: WorkoutTemplate):
        now = timezone.now().replace(microsecond=0)
        familiarity_ids = set()
        with transaction.atomic():
            for scenario_index, scenario in enumerate(
                SCENARIOS,
                start=1,
            ):
                # 피드에서 가장 복잡한 시나리오가 위에 오고, PR에는 앞서 생성된
                # 단순 시나리오 2건이 과거 기록으로 나타나게 한다.
                hours_ago = (len(SCENARIOS) - scenario_index + 1) * 2
                started_at = now - timedelta(hours=hours_ago)
                requested_distance = 5_000 + scenario_index * 250
                duration = timedelta(minutes=26 + scenario_index * 3)
                ended_at = started_at + duration
                source_route = _scale_route_to_distance(
                    template.route,
                    requested_distance,
                )
                route = _transform_route(
                    source_route,
                    started_at,
                    ended_at,
                    coordinate_variant=scenario_index - 1,
                )
                heart_rate = _transform_heart_rate(
                    template.heart_rate,
                    started_at,
                    ended_at,
                    bpm_delta=(scenario_index - 2) * 5,
                )
                distance = _route_distance(route)
                source_workout = Workout.objects.create(
                    user=owner,
                    source=SOURCE,
                    sourceName=f'{SOURCE_NAME} · {scenario.label}',
                    sourceWorkoutId=(
                        f'{SOURCE_ID_PREFIX}-{owner.id}-{scenario_index}'
                    ),
                    kind='running',
                    rawType='UI_PREVIEW',
                    startAt=started_at,
                    endAt=ended_at,
                    distanceMeters=distance,
                    kcal=max(20, round(distance / 1000 * (55 + scenario_index * 4))),
                    steps=max(1, round(distance / (0.72 + scenario_index * 0.02))),
                    heartRateAvg=round(sum(item['bpm'] for item in heart_rate) / len(heart_rate)),
                    heartRateMin=round(min(item['bpm'] for item in heart_rate)),
                    heartRateMax=round(max(item['bpm'] for item in heart_rate)),
                    heartRateSampleCount=len(heart_rate),
                )
                self._save_detail(source_workout, route, heart_rate)
                rebuild_h3_segments(source_workout, route)
                rebuild_distance_records(source_workout, route)
                source_segments = list(
                    source_workout.trajectorySegments.order_by('sequence')
                )
                selected_segments = _spread_segments(
                    source_segments,
                    len(scenario.cell_counts),
                )
                if len(selected_segments) != len(scenario.cell_counts):
                    raise CommandError('UI Preview H3 구간을 충분히 생성하지 못했습니다.')

                runner_number = 0
                for cell_index, (
                    source_segment,
                    met_count,
                    sent_count,
                    received_count,
                ) in enumerate(
                    zip(
                        selected_segments,
                        scenario.cell_counts,
                        scenario.sent_high_five_counts,
                        scenario.received_high_five_counts,
                        strict=True,
                    ),
                    start=1,
                ):
                    for cell_runner_index in range(1, met_count + 1):
                        runner_number += 1
                        target = self._target_user(
                            owner,
                            scenario_index,
                            runner_number,
                        )
                        target_distance = 5_000 + (
                            (runner_number * 683 + scenario_index * 317) % 5_001
                        )
                        target_pace_seconds = 270 + (
                            (runner_number * 29 + scenario_index * 17) % 151
                        )
                        target_end_at = started_at + timedelta(
                            seconds=(target_distance / 1000) * target_pace_seconds
                        )
                        target_source_route = _scale_route_to_distance(
                            template.route,
                            target_distance,
                        )
                        target_route = _transform_route(
                            target_source_route,
                            started_at,
                            target_end_at,
                            coordinate_variant=scenario_index * 20 + runner_number,
                        )
                        actual_target_distance = _route_distance(target_route)
                        target_workout = Workout.objects.create(
                            user=target,
                            source=SOURCE,
                            sourceName=SOURCE_NAME,
                            sourceWorkoutId=(
                                f'{SOURCE_ID_PREFIX}-target-{owner.id}-'
                                f'{scenario_index}-{runner_number}'
                            ),
                            kind='running',
                            rawType='UI_PREVIEW_TARGET',
                            startAt=started_at,
                            # 분포도에서 러너별 점이 실제로 갈라져 보이도록 거리와
                            # 전체 운동 시간을 서로 다르게 만든다. H3 마주침 시간은
                            # 아래 target_segment가 source_segment와 동일하게 유지한다.
                            endAt=target_end_at,
                            distanceMeters=actual_target_distance,
                            kcal=max(15, (source_workout.kcal or 20) + runner_number * 2),
                        )
                        target_heart_rate = _transform_heart_rate(
                            template.heart_rate,
                            target_workout.startAt,
                            target_workout.endAt,
                            bpm_delta=-9 + (runner_number % 7) * 4,
                        )
                        target_workout.heartRateAvg = round(
                            sum(item['bpm'] for item in target_heart_rate)
                            / len(target_heart_rate)
                        )
                        target_workout.heartRateMin = round(
                            min(item['bpm'] for item in target_heart_rate)
                        )
                        target_workout.heartRateMax = round(
                            max(item['bpm'] for item in target_heart_rate)
                        )
                        target_workout.heartRateSampleCount = len(target_heart_rate)
                        target_workout.save(update_fields=(
                            'heartRateAvg', 'heartRateMin', 'heartRateMax',
                            'heartRateSampleCount',
                        ))
                        self._save_detail(target_workout, target_route, target_heart_rate)
                        rebuild_distance_records(target_workout, target_route)
                        # 같은 셀의 러너도 진입 시각을 조금씩 달리해 상세 화면에서
                        # 마주친 순서가 현실적으로 구분되게 한다.
                        source_duration = (
                            source_segment.period.upper
                            - source_segment.period.lower
                        )
                        encounter_offset = source_duration * (
                            cell_runner_index / (met_count + 1)
                        )
                        target_segment = TrajectorySegment.objects.create(
                            workout=target_workout,
                            user=target,
                            indexVersion=source_segment.indexVersion,
                            sequence=0,
                            cellId=source_segment.cellId,
                            period=Range(
                                source_segment.period.lower + encounter_offset,
                                source_segment.period.upper,
                                bounds='[)',
                            ),
                        )
                        if cell_runner_index <= sent_count:
                            familiarity_ids.add(self._create_high_five(
                                from_user=owner,
                                to_user=target,
                                from_workout=source_workout,
                                to_workout=target_workout,
                                segment=target_segment,
                            ))
                        if cell_runner_index > met_count - received_count:
                            familiarity_ids.add(self._create_high_five(
                                from_user=target,
                                to_user=owner,
                                from_workout=target_workout,
                                to_workout=source_workout,
                                segment=target_segment,
                            ))

            # 실제 하이파이브 API와 동일하게 생성된 관계의 만남 횟수를 확정한다.
            refresh_familiarities(list(familiarity_ids))

            # Preview 운동은 업로드 API를 거치지 않으므로 주간 통계도 직접 갱신한다.
            preview_workouts = Workout.objects.filter(
                source=SOURCE,
                sourceWorkoutId__contains=f'-{owner.id}-',
            ).select_related('user')
            weekly_keys = {
                (
                    workout.user_id,
                    week_start_for(workout.startAt, workout.user),
                    workout.kind,
                )
                for workout in preview_workouts
            }
            users = User.objects.in_bulk({item[0] for item in weekly_keys})
            for user_id, week_start, workout_kind in weekly_keys:
                rebuild_weekly_stat(users[user_id], week_start, workout_kind)

    @staticmethod
    def _create_high_five(
        *,
        from_user: User,
        to_user: User,
        from_workout: Workout,
        to_workout: Workout,
        segment: TrajectorySegment,
    ):
        HighFive.objects.create(
            fromUser=from_user,
            toUser=to_user,
            fromWorkout=from_workout,
            toWorkout=to_workout,
            cellId=segment.cellId,
            encounteredAt=segment.period.lower,
            encounterKey=_encounter_key(from_workout.id, to_workout.id),
        )
        first_user_id, second_user_id = sorted((from_user.id, to_user.id))
        familiarity, _ = UserFamiliarity.objects.get_or_create(
            firstUser_id=first_user_id,
            secondUser_id=second_user_id,
        )
        return familiarity.id

    def _target_user(
        self,
        owner: User,
        scenario_index: int,
        runner_index: int,
    ) -> User:
        runner_key = f'{scenario_index}-{runner_index}'
        username = f'{SOURCE_ID_PREFIX}-{owner.id}-{runner_key}'
        existing = User.objects.filter(username=username).first()
        if existing is not None:
            return existing

        user = User.objects.create_user(username=username)
        color = _avatar_color(scenario_index, runner_index)
        image_key, thumb_key = self._save_avatar(username, color)
        Profile.objects.create(
            user=user,
            nickname=f'러너 {runner_key}',
            nicknameKey=f'러너 {runner_key}',
            loginProvider=LoginProvider.GOOGLE,
            imageKey=image_key,
            imageThumbKey=thumb_key,
            imageUpdatedAt=timezone.now(),
        )
        return user

    def _save_detail(
        self,
        workout: Workout,
        route: list[dict],
        heart_rate: list[dict],
    ):
        identity = {
            'formatVersion': 1,
            'source': workout.source,
            'sourceName': workout.sourceName,
            'sourceWorkoutId': workout.sourceWorkoutId,
            'startedAt': workout.startAt.isoformat(),
            'endedAt': workout.endAt.isoformat(),
        }
        rebuild_workout_metrics(workout, route, heart_rate)
        stored_route = simplify_route(route)
        route_payload = {
            **identity,
            'route': stored_route,
        }
        route_content = json.dumps(route_payload, separators=(',', ':')).encode()
        route_hash = hashlib.sha256(route_content).hexdigest()
        heart_hash = hashlib.sha256(
            json.dumps(heart_rate, separators=(',', ':')).encode()
        ).hexdigest() if heart_rate else None
        content_hash = hashlib.sha256(f'{route_hash}:{heart_hash}'.encode()).hexdigest()
        preview_owner_id = _preview_owner_id(workout.sourceWorkoutId)
        route_key = (
            f'{SOURCE_ID_PREFIX}/{preview_owner_id}/users/{workout.user_id}/route/'
            f'{workout.sourceWorkoutId}.json'
        )
        if default_storage.exists(route_key):
            default_storage.delete(route_key)
        default_storage.save(route_key, ContentFile(route_content))
        WorkoutDetail.objects.create(
            workout=workout,
            routeObjectKey=route_key,
            routeContentHash=route_hash,
            routeFileSize=len(route_content),
            contentHash=content_hash,
            routePointCount=len(stored_route),
            heartRateSampleCount=len(heart_rate),
            fileSize=len(route_content),
        )

    @staticmethod
    def _save_avatar(username: str, color: tuple[int, int, int]):
        client = build_client(settings.S3_ENDPOINT_URL)
        keys = []
        for size in (512, 128):
            image = Image.new('RGB', (size, size), color)
            draw = ImageDraw.Draw(image)
            radius = size // 5
            center = size // 2
            draw.ellipse(
                (
                    center - radius,
                    center - radius,
                    center + radius,
                    center + radius,
                ),
                fill=(255, 255, 255),
            )
            output = io.BytesIO()
            image.save(output, format='WEBP', quality=88)
            key = f'profiles/{SOURCE_ID_PREFIX}/{username}/{size}.webp'
            client.put_object(
                Bucket=settings.S3_PUBLIC_BUCKET_NAME,
                Key=key,
                Body=output.getvalue(),
                ContentType='image/webp',
            )
            keys.append(key)
        return keys[0], keys[1]

    def _clear(self, owner: User):
        Workout.objects.filter(
            user=owner,
            source=SOURCE,
            sourceWorkoutId__startswith=f'{SOURCE_ID_PREFIX}-{owner.id}-',
        ).delete()
        User.objects.filter(
            username__startswith=f'{SOURCE_ID_PREFIX}-{owner.id}-'
        ).delete()
        self._delete_object_prefix(
            settings.S3_BUCKET_NAME,
            f'{SOURCE_ID_PREFIX}/{owner.id}/',
        )
        self._delete_object_prefix(
            settings.S3_PUBLIC_BUCKET_NAME,
            f'profiles/{SOURCE_ID_PREFIX}/{SOURCE_ID_PREFIX}-{owner.id}-',
        )

    @staticmethod
    def _delete_object_prefix(bucket: str, prefix: str):
        client = build_client(settings.S3_ENDPOINT_URL)
        continuation_token = None
        while True:
            arguments = {'Bucket': bucket, 'Prefix': prefix}
            if continuation_token:
                arguments['ContinuationToken'] = continuation_token
            response = client.list_objects_v2(**arguments)
            objects = [{'Key': item['Key']} for item in response.get('Contents', [])]
            if objects:
                client.delete_objects(Bucket=bucket, Delete={'Objects': objects})
            if not response.get('IsTruncated'):
                return
            continuation_token = response['NextContinuationToken']


def _route_for(
    scenario_index: int,
    started_at,
    ended_at,
    *,
    repeat_route: bool = False,
) -> list[dict]:
    base_latitude = 37.5000 + scenario_index * 0.0015
    base_longitude = 127.0300 + scenario_index * 0.0015
    origin = h3.latlng_to_cell(base_latitude, base_longitude, H3_RESOLUTION)
    ring = sorted(h3.grid_ring(origin, 5))
    target = ring[scenario_index % len(ring)]
    outward_cells = h3.grid_path_cells(origin, target)
    cells = (
        [*outward_cells, *reversed(outward_cells[:-1])]
        if repeat_route
        else outward_cells
    )
    # 재방문 경로는 마지막 원점에도 실제 체류 구간이 남아야 한다. 마지막 좌표가
    # 종료 시각과 같으면 공간 인덱서가 0초 구간으로 제거하므로 한 간격을 남긴다.
    interval_divisor = len(cells) if repeat_route else len(cells) - 1
    interval = (ended_at - started_at) / interval_divisor
    return [
        {
            'timestamp': (started_at + interval * index).isoformat(),
            'latitude': h3.cell_to_latlng(cell)[0],
            'longitude': h3.cell_to_latlng(cell)[1],
            'altitude': 25 + index,
            'accuracy': 3.0,
            'verticalAccuracy': 4.0,
            'speed': 2.5,
            'course': 0.0,
        }
        for index, cell in enumerate(cells)
    ]


def _read_json_object(object_key: str) -> dict:
    with default_storage.open(object_key, 'rb') as stream:
        value = json.loads(stream.read())
    if not isinstance(value, dict):
        raise ValueError('JSON 최상위 값이 객체가 아닙니다.')
    return value


def _template_heart_rate(workout: Workout) -> list[dict]:
    """현재 구조의 50포인트 통계를 더미 운동용 시간 샘플로 복원한다."""
    try:
        series = workout.metrics.heartRateSeries
    except WorkoutMetrics.DoesNotExist:
        series = []
    values = [float(value) for value in series if value is not None]
    if not values and workout.heartRateAvg is not None:
        values = [float(workout.heartRateAvg)] * 50
    if not values:
        # 경로만 있는 실제 운동도 UI Preview 원본으로 사용할 수 있게 한다.
        values = [125 + math.sin(index / 6) * 12 for index in range(50)]
    denominator = max(len(values) - 1, 1)
    return [
        {
            'timestamp': (
                workout.startAt
                + (workout.endAt - workout.startAt) * (index / denominator)
            ).isoformat(),
            'bpm': round(value, 1),
            'source': workout.sourceName,
        }
        for index, value in enumerate(values)
    ]


def _scale_route_to_distance(source: list[dict], target_meters: float) -> list[dict]:
    """원본 경로의 모양과 샘플 수를 유지한 채 목표 누적거리로 확대한다."""
    current_distance = _route_distance(source)
    if current_distance <= 0:
        raise CommandError('원본 경로의 이동거리를 계산할 수 없습니다.')
    origin_latitude = float(source[0]['latitude'])
    origin_longitude = float(source[0]['longitude'])
    scale = target_meters / current_distance
    return [
        {
            **point,
            'latitude': origin_latitude
            + (float(point['latitude']) - origin_latitude) * scale,
            'longitude': origin_longitude
            + (float(point['longitude']) - origin_longitude) * scale,
        }
        for point in source
    ]


def _transform_route(
    source: list[dict],
    started_at,
    ended_at,
    *,
    coordinate_variant: int,
) -> list[dict]:
    """실제 경로 모양을 유지하며 시각·좌표·고도를 재현 가능한 값으로 변형한다."""
    result = []
    denominator = max(len(source) - 1, 1)
    # 약 0~7m 범위의 작은 변화다. 같은 원본이어도 더미 러너 경로가 완전히
    # 포개지지 않으면서 실제 장소를 벗어나지 않는다.
    phase = coordinate_variant * 0.73
    for index, raw in enumerate(source):
        ratio = index / denominator
        latitude = float(raw['latitude']) + math.sin(index * 0.37 + phase) * 0.000025
        longitude = float(raw['longitude']) + math.cos(index * 0.31 + phase) * 0.000025
        point = dict(raw)
        point.update({
            'timestamp': (started_at + (ended_at - started_at) * ratio).isoformat(),
            'latitude': latitude,
            'longitude': longitude,
        })
        if raw.get('altitude') is not None:
            point['altitude'] = float(raw['altitude']) + math.sin(phase) * 2.5
        base_cadence = float(raw.get('cadence') or 168)
        cadence_offset = ((coordinate_variant % 7) - 3) * 1.5
        # 삼성 헬스 원본이 120spm 근처여도 하한을 120으로 묶으면 모든 값이
        # 120으로 평평해진다. 워밍업·감속 구간까지 보이도록 현실적인 범위에서
        # 흔들림을 보존한다.
        point['cadence'] = round(
            min(
                210.0,
                max(
                    90.0,
                    base_cadence
                    + cadence_offset
                    + math.sin(index / 5 + phase) * 5,
                ),
            ),
            1,
        )
        result.append(point)
    return result


def _transform_heart_rate(
    source: list[dict],
    started_at,
    ended_at,
    *,
    bpm_delta: int,
) -> list[dict]:
    result = []
    denominator = max(len(source) - 1, 1)
    for index, raw in enumerate(source):
        bpm = min(195.0, max(45.0, float(raw['bpm']) + bpm_delta + math.sin(index / 9) * 3))
        sample = dict(raw)
        sample.update({
            'timestamp': (
                started_at + (ended_at - started_at) * (index / denominator)
            ).isoformat(),
            'bpm': round(bpm, 1),
        })
        result.append(sample)
    return result


def _route_distance(route: list[dict]) -> float:
    total = 0.0
    for previous, current in zip(route, route[1:]):
        latitude_1 = math.radians(float(previous['latitude']))
        latitude_2 = math.radians(float(current['latitude']))
        latitude_delta = latitude_2 - latitude_1
        longitude_delta = math.radians(
            float(current['longitude']) - float(previous['longitude'])
        )
        value = (
            math.sin(latitude_delta / 2) ** 2
            + math.cos(latitude_1)
            * math.cos(latitude_2)
            * math.sin(longitude_delta / 2) ** 2
        )
        total += 6_371_008.8 * 2 * math.atan2(
            math.sqrt(value), math.sqrt(max(1 - value, 0))
        )
    return total


def _spread_segments(segments: list, count: int) -> list:
    """경로의 처음부터 끝까지 H3 셀을 균등하게 고른다."""
    if count <= 0 or not segments:
        return []
    if count == 1:
        return [segments[len(segments) // 2]]
    if count > len(segments):
        return []
    last_index = len(segments) - 1
    return [
        segments[round(index * last_index / (count - 1))]
        for index in range(count)
    ]


def _avatar_color(scenario_index: int, runner_index: int) -> tuple[int, int, int]:
    colors = (
        (62, 112, 214),
        (231, 95, 68),
        (55, 157, 116),
        (141, 93, 190),
        (220, 151, 43),
    )
    return colors[(scenario_index + runner_index - 2) % len(colors)]


def _encounter_key(first_workout_id: int, second_workout_id: int) -> str:
    lower, upper = sorted((first_workout_id, second_workout_id))
    return f'{lower}:{upper}'


def _preview_owner_id(source_workout_id: str) -> str:
    target_prefix = f'{SOURCE_ID_PREFIX}-target-'
    owner_prefix = f'{SOURCE_ID_PREFIX}-'
    remainder = (
        source_workout_id[len(target_prefix):]
        if source_workout_id.startswith(target_prefix)
        else source_workout_id[len(owner_prefix):]
    )
    return remainder.split('-', 1)[0]
