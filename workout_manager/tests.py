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

from .models import (
    SpatialIndexVersion,
    TrajectorySegment,
    Workout,
    WorkoutUploadSession,
)
from .views import _workout_page_size


UPLOAD_PREPARE = '/api/workouts/upload/prepare/'
UPLOAD_COMPLETE = '/api/workouts/upload/complete/'
DOWNLOAD = '/api/workouts/'


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
    metadata['contentHash'] = hashlib.sha256(content).hexdigest()
    return {
        'metadata': json.dumps(metadata),
        'detail': SimpleUploadedFile('detail.json', content, content_type='application/json'),
    }


def _direct_upload(client, payload):
    metadata = json.loads(payload['metadata'])
    content = payload['detail'].read()
    metadata['fileSize'] = len(content)
    prepared = client.post(UPLOAD_PREPARE, metadata, format='json')
    if prepared.status_code != 200:
        return prepared
    session = WorkoutUploadSession.objects.get(uploadId=prepared.data['uploadId'])
    if prepared.data['detailUploadRequired']:
        default_storage.save(session.objectKey, ContentFile(content))
    return client.post(
        UPLOAD_COMPLETE,
        {'uploadId': str(session.uploadId)},
        format='json',
    )


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

        server_id = downloaded.data['workouts'][0]['serverId']
        detail = self.client.get(f'/api/workouts/{server_id}/detail/')
        self.assertEqual(detail.status_code, 200)
        self.assertIn('downloadUrl', detail.data)
        self.assertNotIn('detail', detail.data)
        self.assertEqual(detail.data['expiresInSeconds'], 300)

    def test_S3_직접_업로드를_준비하고_완료한다(self):
        payload = _upload_payload()
        metadata = json.loads(payload['metadata'])
        content = payload['detail'].read()
        metadata['fileSize'] = len(content)
        with self.settings(
            S3_BUCKET_NAME='test-bucket',
            S3_PUBLIC_ENDPOINT_URL='http://127.0.0.1:9000',
            S3_ACCESS_KEY='test',
            S3_SECRET_KEY='test',
        ):
            prepared = self.client.post(UPLOAD_PREPARE, metadata, format='json')

        self.assertEqual(prepared.status_code, 200)
        self.assertTrue(prepared.data['detailUploadRequired'])
        self.assertIn('uploadUrl', prepared.data)
        session = WorkoutUploadSession.objects.get(
            uploadId=prepared.data['uploadId']
        )
        default_storage.save(session.objectKey, ContentFile(content))

        completed = self.client.post(
            UPLOAD_COMPLETE,
            {'uploadId': str(session.uploadId)},
            format='json',
        )

        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.data['trajectorySegmentCount'], 1)
        self.assertEqual(Workout.objects.count(), 1)
        session.refresh_from_db()
        self.assertEqual(session.status, WorkoutUploadSession.Status.READY)

        metadata['distanceMeters'] = 5100
        prepared_again = self.client.post(
            UPLOAD_PREPARE,
            metadata,
            format='json',
        )
        self.assertEqual(prepared_again.status_code, 200)
        self.assertFalse(prepared_again.data['detailUploadRequired'])

        completed_again = self.client.post(
            UPLOAD_COMPLETE,
            {'uploadId': prepared_again.data['uploadId']},
            format='json',
        )
        self.assertEqual(completed_again.status_code, 200)
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

    def test_운동_목록은_20개씩_커서_페이징한다(self):
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
