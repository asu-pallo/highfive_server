from datetime import datetime, timedelta, timezone as dt_timezone
import hashlib
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connections
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework.test import APIClient
from django.test import TransactionTestCase, override_settings
from rest_framework_simplejwt.tokens import RefreshToken
from psycopg.types.range import Range

from .high_five import high_five_summaries
from .models import (
    SpatialIndexVersion,
    TrajectorySegment,
    Workout,
    WorkoutDetail,
)
from .views import _workout_page_size


UPLOAD_PREPARE = '/api/workouts/upload/prepare/'
UPLOAD_CREATE = '/api/workouts/upload/create/'
DOWNLOAD = '/api/workouts/'
HIGH_FIVES = '/api/workouts/high-fives/'


def _workout(workout_id='health-1', **overrides):
    start = datetime(2026, 8, 28, 1, tzinfo=dt_timezone.utc)
    data = {
        'source': 'samsungHealth',
        'sourceName': 'com.sec.android.app.shealth',
        'sourceWorkoutId': workout_id,
        'kind': 'running',
        'rawType': 'RUNNING',
        'startAt': start.isoformat(),
        'endAt': (start + timedelta(minutes=30)).isoformat(),
        'distanceMeters': 5000.0,
        'kcal': 300,
        'steps': 6000,
        'flightsClimbed': 2,
    }
    data.update(overrides)
    return data


def _upload_payload(workout_id='health-1', metadata_overrides=None, detail_overrides=None):
    metadata = _workout(workout_id, **(metadata_overrides or {}))
    detail = {
        'formatVersion': 1,
        'source': metadata['source'],
        'sourceName': metadata['sourceName'],
        'sourceWorkoutId': metadata['sourceWorkoutId'],
        'startedAt': metadata['startAt'],
        'endedAt': metadata['endAt'],
        'route': [
            {'timestamp': metadata['startAt'], 'latitude': 37.5, 'longitude': 127.0}
        ],
        'heartRate': [
            {'timestamp': metadata['startAt'], 'bpm': 120, 'source': metadata['sourceName']},
            {'timestamp': metadata['endAt'], 'bpm': 180, 'source': metadata['sourceName']},
        ],
    }
    detail.update(detail_overrides or {})
    content = json.dumps(detail, separators=(',', ':')).encode()
    return {
        'metadata': json.dumps(metadata),
        'detail': SimpleUploadedFile('detail.json', content, content_type='application/json'),
    }


def _direct_upload(client, payload):
    metadata = json.loads(payload['metadata'])
    detail = json.loads(payload['detail'].read())
    identity = {key: detail[key] for key in (
        'formatVersion', 'source', 'sourceName', 'sourceWorkoutId',
        'startedAt', 'endedAt',
    )}
    route_content = _detail_content(identity, 'route', detail['route'])
    heart_content = _detail_content(identity, 'heartRate', detail['heartRate'])
    route_hash = hashlib.sha256(route_content).hexdigest() if route_content else ''
    heart_hash = hashlib.sha256(heart_content).hexdigest() if heart_content else ''
    metadata.update({
        'routeContentHash': route_hash,
        'routeFileSize': len(route_content),
        'heartRateContentHash': heart_hash,
        'heartRateFileSize': len(heart_content),
        'detailContentHash': hashlib.sha256(
            f'{route_hash}:{heart_hash}'.encode()
        ).hexdigest(),
    })
    prepared = client.post(UPLOAD_PREPARE, metadata, format='json')
    if prepared.status_code != 200:
        return prepared
    for response_key, content in (
        ('routeUpload', route_content),
        ('heartRateUpload', heart_content),
    ):
        target = prepared.data[response_key]
        if target['uploadRequired']:
            default_storage.save(target['uploadFields']['key'], ContentFile(content))
    return client.post(UPLOAD_CREATE, metadata, format='json')


def _detail_content(identity, key, samples):
    if not samples:
        return b''
    return json.dumps(
        {**identity, key: samples}, separators=(',', ':')
    ).encode()


@override_settings(
    S3_BUCKET_NAME='test-bucket',
    S3_PUBLIC_ENDPOINT_URL='http://127.0.0.1:9000',
    S3_ACCESS_KEY='test',
    S3_SECRET_KEY='test',
)
class WorkoutApiTest(APITestCase):
    def setUp(self):
        self.media_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.media_directory.cleanup)
        self.storage_settings = self.settings(MEDIA_ROOT=self.media_directory.name)
        self.storage_settings.enable()
        self.addCleanup(self.storage_settings.disable)
        self.user = User.objects.create_user(username='runner')
        access = RefreshToken.for_user(self.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')

    def test_운동을_올리고_받는다(self):
        uploaded = _direct_upload(self.client, _upload_payload())
        downloaded = self.client.get(DOWNLOAD)

        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(uploaded.data['created'], 1)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(len(downloaded.data['workouts']), 1)
        self.assertEqual(downloaded.data['workouts'][0]['id'], 'health-1')
        self.assertEqual(downloaded.data['workouts'][0]['heartRateAvg'], 150)
        self.assertEqual(downloaded.data['workouts'][0]['routePointCount'], 1)
        self.assertRegex(
            downloaded.data['workouts'][0]['detailContentHash'],
            r'^[0-9a-f]{64}$',
        )
        self.assertNotIn('route', downloaded.data['workouts'][0])
        self.assertNotIn('highFives', downloaded.data['workouts'][0])

        server_id = downloaded.data['workouts'][0]['serverId']
        detail = self.client.get(f'/api/workouts/{server_id}/detail/')
        self.assertEqual(detail.status_code, 200)
        self.assertIn('routeDownloadUrl', detail.data)
        self.assertIn('heartRateDownloadUrl', detail.data)
        self.assertNotIn('detail', detail.data)
        self.assertEqual(detail.data['expiresInSeconds'], 300)

    def test_S3_직접_업로드를_준비하고_생성한다(self):
        payload = _upload_payload()
        created = _direct_upload(self.client, payload)

        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.data['trajectorySegmentCount'], 1)
        self.assertEqual(Workout.objects.count(), 1)
        detail = WorkoutDetail.objects.get()
        self.assertIn('/route/', detail.routeObjectKey)
        self.assertIn('/heart-rate/', detail.heartRateObjectKey)
        self.assertNotEqual(detail.routeObjectKey, detail.heartRateObjectKey)

        created_again = _direct_upload(
            self.client,
            _upload_payload(metadata_overrides={'distanceMeters': 5100}),
        )
        self.assertEqual(created_again.status_code, 200)
        self.assertEqual(Workout.objects.get().distanceMeters, 5100)

    def test_원본_경로를_h3_체류_구간으로_저장한다(self):
        metadata = _workout()
        middle = datetime.fromisoformat(metadata['startAt']) + timedelta(minutes=10)
        response = _direct_upload(
            self.client,
            _upload_payload(detail_overrides={'route': [
                {
                    'timestamp': metadata['startAt'],
                    'latitude': 37.5,
                    'longitude': 127.0,
                },
                {
                    'timestamp': middle.isoformat(),
                    'latitude': 37.501,
                    'longitude': 127.001,
                },
            ]}),
        )

        self.assertEqual(response.status_code, 200)
        segments = list(TrajectorySegment.objects.order_by('sequence'))
        self.assertEqual(response.data['trajectorySegmentCount'], len(segments))
        version = SpatialIndexVersion.objects.get()
        self.assertEqual(version.indexType, 'h3')
        self.assertEqual(version.parameters, {'resolution': 11})
        self.assertGreater(len(segments), 1)
        self.assertEqual(
            [segment.sequence for segment in segments],
            list(range(len(segments))),
        )
        self.assertEqual(segments[0].period.lower, datetime.fromisoformat(metadata['startAt']))
        self.assertEqual(segments[-1].period.upper, datetime.fromisoformat(metadata['endAt']))
        self.assertTrue(all(segment.period.bounds == '[)' for segment in segments))

        h3_response = self.client.get(f'/api/workouts/{segments[0].workout_id}/h3/')
        self.assertEqual(h3_response.status_code, 200)
        self.assertEqual(h3_response.data['resolution'], 11)
        self.assertEqual(len(h3_response.data['segments']), len(segments))
        self.assertEqual(len(h3_response.data['segments'][0]['boundary']), 6)

    def test_iOS_경로의_1분_이내_시간_오차를_허용하고_운동_경계로_자른다(self):
        metadata = _workout()
        ended_at = datetime.fromisoformat(metadata['endAt'])
        response = _direct_upload(
            self.client,
            _upload_payload(detail_overrides={'route': [
                {
                    'timestamp': metadata['startAt'],
                    'latitude': 37.5,
                    'longitude': 127.0,
                },
                {
                    'timestamp': (ended_at + timedelta(seconds=59)).isoformat(),
                    'latitude': 37.501,
                    'longitude': 127.001,
                },
            ]}),
        )

        self.assertEqual(response.status_code, 200)
        last_segment = TrajectorySegment.objects.order_by('sequence').last()
        self.assertEqual(last_segment.period.upper, ended_at)

    def test_다른_사용자의_h3_구간은_받지_못한다(self):
        _direct_upload(self.client, _upload_payload())
        workout_id = Workout.objects.get().pk
        other = User.objects.create_user(username='other-h3-user')
        access = RefreshToken.for_user(other).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')

        response = self.client.get(f'/api/workouts/{workout_id}/h3/')

        self.assertEqual(response.status_code, 404)

    def test_상세_경로가_바뀌면_h3_구간을_교체한다(self):
        _direct_upload(self.client, _upload_payload())
        original_ids = set(TrajectorySegment.objects.values_list('id', flat=True))

        response = _direct_upload(
            self.client,
            _upload_payload(detail_overrides={'route': [{
                'timestamp': _workout()['startAt'],
                'latitude': 37.51,
                'longitude': 127.01,
            }]}),
        )

        self.assertEqual(response.status_code, 200)
        replaced_ids = set(TrajectorySegment.objects.values_list('id', flat=True))
        self.assertTrue(replaced_ids)
        self.assertTrue(original_ids.isdisjoint(replaced_ids))

    def test_같은_h3와_겹치는_시간이면_조회할_때_하이파이브를_계산한다(self):
        start = datetime(2026, 8, 28, 1, tzinfo=dt_timezone.utc)
        version = SpatialIndexVersion.objects.create(
            indexType='h3',
            algorithmVersion=1,
            parameters={'resolution': 11},
            isActive=True,
        )
        other = User.objects.create_user(username='high-five-other')
        other_workout = self._create_workout(other, 'other-run', start, 30)
        self._create_segment(
            other_workout,
            version,
            sequence=0,
            cell='test-cell',
            entered_at=start + timedelta(minutes=5),
            exited_at=start + timedelta(minutes=20),
        )
        source_workout = self._create_workout(self.user, 'source-run', start, 30)
        self._create_segment(
            source_workout,
            version,
            sequence=0,
            cell='test-cell',
            entered_at=start,
            exited_at=start + timedelta(minutes=30),
        )

        summary = high_five_summaries([source_workout.id])[source_workout.id]

        self.assertEqual(summary['totalCount'], 1)
        self.assertEqual(summary['areas'], [{'h3Cell': 'test-cell', 'count': 1}])

    def test_운동_업로드는_판정하지_않고_피드에서_실시간_판정한다(self):
        other = User.objects.create_user(username='upload-high-five-other')
        other_access = RefreshToken.for_user(other).access_token
        other_client = APIClient()
        other_client.credentials(HTTP_AUTHORIZATION=f'Bearer {other_access}')
        other_response = _direct_upload(
            other_client,
            _upload_payload('other-upload-run'),
        )

        source_response = _direct_upload(
            self.client,
            _upload_payload('source-upload-run'),
        )

        self.assertEqual(other_response.status_code, 200)
        self.assertEqual(source_response.status_code, 200)
        self.assertNotIn('highFiveCount', other_response.data)
        self.assertNotIn('highFiveCount', source_response.data)

        source_id = Workout.objects.get(user=self.user).id
        other_id = Workout.objects.get(user=other).id
        source_feed = self.client.post(
            HIGH_FIVES, {'workoutIds': [source_id]}, format='json'
        )
        other_feed = other_client.post(
            HIGH_FIVES, {'workoutIds': [other_id]}, format='json'
        )
        self.assertEqual(
            source_feed.data['summaries'][0]['totalCount'], 1
        )
        self.assertEqual(
            other_feed.data['summaries'][0]['totalCount'], 1
        )

    def test_같은_상대의_여러_겹침은_한명으로_계산한다(self):
        start = datetime(2026, 8, 28, 1, tzinfo=dt_timezone.utc)
        version = SpatialIndexVersion.objects.create(
            indexType='h3',
            algorithmVersion=1,
            parameters={'resolution': 11},
            isActive=True,
        )
        other = User.objects.create_user(username='same-high-five-other')
        short_workout = self._create_workout(other, 'short-run', start, 30)
        self._create_segment(
            short_workout,
            version,
            sequence=0,
            cell='test-cell',
            entered_at=start + timedelta(minutes=5),
            exited_at=start + timedelta(minutes=10),
        )
        long_workout = self._create_workout(other, 'long-run', start, 30)
        self._create_segment(
            long_workout,
            version,
            sequence=0,
            cell='test-cell',
            entered_at=start + timedelta(minutes=5),
            exited_at=start + timedelta(minutes=25),
        )
        source_workout = self._create_workout(self.user, 'source-run', start, 30)
        self._create_segment(
            source_workout,
            version,
            sequence=0,
            cell='test-cell',
            entered_at=start,
            exited_at=start + timedelta(minutes=30),
        )

        summary = high_five_summaries([source_workout.id])[source_workout.id]
        self.assertEqual(summary['totalCount'], 1)
        self.assertEqual(summary['areas'], [{'h3Cell': 'test-cell', 'count': 1}])

    def test_종료와_시작만_맞닿으면_하이파이브가_아니다(self):
        start = datetime(2026, 8, 28, 1, tzinfo=dt_timezone.utc)
        boundary = start + timedelta(minutes=10)
        version = SpatialIndexVersion.objects.create(
            indexType='h3',
            algorithmVersion=1,
            parameters={'resolution': 11},
            isActive=True,
        )
        other = User.objects.create_user(username='boundary-other')
        other_workout = self._create_workout(other, 'other-run', start, 10)
        self._create_segment(
            other_workout,
            version,
            sequence=0,
            cell='test-cell',
            entered_at=start,
            exited_at=boundary,
        )
        source_workout = self._create_workout(self.user, 'source-run', boundary, 10)
        self._create_segment(
            source_workout,
            version,
            sequence=0,
            cell='test-cell',
            entered_at=boundary,
            exited_at=boundary + timedelta(minutes=10),
        )

        summary = high_five_summaries([source_workout.id])[source_workout.id]

        self.assertEqual(summary, {'totalCount': 0, 'areas': []})

    def test_같은_사용자의_겹치는_운동은_하이파이브가_아니다(self):
        start = datetime(2026, 8, 28, 1, tzinfo=dt_timezone.utc)
        version = SpatialIndexVersion.objects.create(
            indexType='h3',
            algorithmVersion=1,
            parameters={'resolution': 11},
            isActive=True,
        )
        other_workout = self._create_workout(self.user, 'other-run', start, 30)
        self._create_segment(
            other_workout,
            version,
            sequence=0,
            cell='test-cell',
            entered_at=start,
            exited_at=start + timedelta(minutes=30),
        )
        source_workout = self._create_workout(self.user, 'source-run', start, 30)
        self._create_segment(
            source_workout,
            version,
            sequence=0,
            cell='test-cell',
            entered_at=start,
            exited_at=start + timedelta(minutes=30),
        )
        self.assertEqual(
            high_five_summaries([source_workout.id])[source_workout.id],
            {'totalCount': 0, 'areas': []},
        )

    @staticmethod
    def _create_workout(user, workout_id, start, duration_minutes):
        return Workout.objects.create(
            user=user,
            source='healthConnect',
            sourceName='test.app',
            sourceWorkoutId=workout_id,
            kind='running',
            startAt=start,
            endAt=start + timedelta(minutes=duration_minutes),
        )

    @staticmethod
    def _create_segment(
        workout,
        version,
        *,
        sequence,
        cell,
        entered_at,
        exited_at,
    ):
        return TrajectorySegment.objects.create(
            workout=workout,
            user=workout.user,
            indexVersion=version,
            sequence=sequence,
            cellId=cell,
            period=Range(entered_at, exited_at, bounds='[)'),
        )

    def test_다른_사용자의_상세_파일은_받지_못한다(self):
        _direct_upload(self.client, _upload_payload())
        server_id = Workout.objects.get().pk
        other = User.objects.create_user(username='other-detail-user')
        access = RefreshToken.for_user(other).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')

        response = self.client.get(f'/api/workouts/{server_id}/detail/')

        self.assertEqual(response.status_code, 404)

    def test_같은_운동을_다시_올려도_중복되지_않는다(self):
        _direct_upload(self.client, _upload_payload())
        first_updated_at = Workout.objects.get().updatedAt
        response = _direct_upload(self.client, _upload_payload())

        self.assertEqual(Workout.objects.count(), 1)
        self.assertTrue(response.data['unchanged'])
        self.assertEqual(Workout.objects.get().updatedAt, first_updated_at)

    def test_변경된_운동은_갱신한다(self):
        _direct_upload(self.client, _upload_payload())
        response = _direct_upload(
            self.client,
            _upload_payload(metadata_overrides={'distanceMeters': 5100.0}),
        )

        self.assertFalse(response.data['unchanged'])
        self.assertEqual(Workout.objects.get().distanceMeters, 5100.0)

    def test_상세_파일만_바뀌어도_증분_다운로드에_포함된다(self):
        _direct_upload(self.client, _upload_payload())
        cursor = timezone.now()
        workout = Workout.objects.get()
        Workout.objects.filter(pk=workout.pk).update(
            updatedAt=cursor - timedelta(seconds=1)
        )

        changed_detail = {
            'route': [
                {
                    'timestamp': _workout()['startAt'],
                    'latitude': 37.51,
                    'longitude': 127.01,
                }
            ]
        }
        _direct_upload(
            self.client,
            _upload_payload(detail_overrides=changed_detail),
        )

        response = self.client.get(DOWNLOAD, {'since': cursor.isoformat()})
        self.assertEqual([item['id'] for item in response.data['workouts']], ['health-1'])

    def test_since_이후_변경된_운동만_받는다(self):
        _direct_upload(self.client, _upload_payload('old'))
        cursor = timezone.now()
        old = Workout.objects.get(sourceWorkoutId='old')
        Workout.objects.filter(pk=old.pk).update(updatedAt=cursor - timedelta(seconds=1))
        _direct_upload(self.client, _upload_payload('new'))

        response = self.client.get(DOWNLOAD, {'since': cursor.isoformat()})

        self.assertEqual([item['id'] for item in response.data['workouts']], ['new'])

    def test_운동_목록은_10개씩_커서_페이징한다(self):
        start = timezone.now() - timedelta(days=30)
        Workout.objects.bulk_create([
            Workout(
                user=self.user,
                source='healthConnect',
                sourceName='test.app',
                sourceWorkoutId=f'page-{index:02d}',
                kind='running',
                startAt=start + timedelta(days=index),
                endAt=start + timedelta(days=index, hours=1),
            )
            for index in range(25)
        ])

        response = self.client.get(DOWNLOAD)
        snapshot = response.data['serverTime']
        downloaded_ids = []

        while True:
            page_ids = [item['serverId'] for item in response.data['workouts']]
            self.assertLessEqual(len(page_ids), _workout_page_size)
            self.assertFalse(set(downloaded_ids) & set(page_ids))
            downloaded_ids.extend(page_ids)
            if not response.data['hasMore']:
                self.assertIsNone(response.data['nextCursor'])
                break
            response = self.client.get(DOWNLOAD, {
                'cursor': response.data['nextCursor'],
                'snapshot': snapshot,
            })

        self.assertEqual(len(downloaded_ids), 25)

    def test_다른_사용자_운동은_받지_않는다(self):
        other = User.objects.create_user(username='other')
        Workout.objects.create(
            user=other,
            source='healthConnect',
            sourceName='other.app',
            sourceWorkoutId='other-1',
            kind='running',
            startAt=timezone.now() - timedelta(hours=1),
            endAt=timezone.now(),
        )

        response = self.client.get(DOWNLOAD)

        self.assertEqual(response.data['workouts'], [])

    def test_인증_없이는_업로드와_다운로드를_못한다(self):
        self.client.credentials()
        self.assertEqual(self.client.post(UPLOAD_PREPARE, {}).status_code, 401)
        self.assertEqual(self.client.get(DOWNLOAD).status_code, 401)

    def test_끝이_시작보다_빠르면_거절한다(self):
        start = timezone.now().replace(microsecond=0)
        response = _direct_upload(
            self.client,
            _upload_payload(metadata_overrides={
                'startAt': start.isoformat(),
                'endAt': (start - timedelta(seconds=1)).isoformat(),
            }),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Workout.objects.count(), 0)


@override_settings(
    S3_BUCKET_NAME='test-bucket',
    S3_PUBLIC_ENDPOINT_URL='http://127.0.0.1:9000',
    S3_ACCESS_KEY='test',
    S3_SECRET_KEY='test',
)
class WorkoutParallelUploadTest(TransactionTestCase):
    """SQLite 개발 환경에서도 앱의 최대 3건 병렬 업로드를 받아야 한다."""

    def setUp(self):
        self.media_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.media_directory.cleanup)
        self.storage_settings = self.settings(MEDIA_ROOT=self.media_directory.name)
        self.storage_settings.enable()
        self.addCleanup(self.storage_settings.disable)
        self.user = User.objects.create_user(username='parallel-runner')
        self.access = str(RefreshToken.for_user(self.user).access_token)

    def _upload(self, workout_id):
        try:
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.access}')
            return _direct_upload(client, _upload_payload(workout_id)).status_code
        finally:
            connections.close_all()

    def test_운동_세_건을_병렬로_올려도_잠금_실패가_없다(self):
        with ThreadPoolExecutor(max_workers=3) as executor:
            statuses = list(executor.map(self._upload, ('run-1', 'run-2', 'run-3')))

        self.assertEqual(statuses, [200, 200, 200])
        self.assertEqual(Workout.objects.count(), 3)
