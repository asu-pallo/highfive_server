from datetime import datetime, timedelta, timezone as dt_timezone
import hashlib
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework.test import APIClient
from django.test import TransactionTestCase
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Workout


UPLOAD = '/api/workouts/upload/'
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
        uploaded = self.client.post(UPLOAD, _upload_payload(), format='multipart')
        downloaded = self.client.get(DOWNLOAD)

        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(uploaded.data['created'], 1)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(len(downloaded.data['workouts']), 1)
        self.assertEqual(downloaded.data['workouts'][0]['id'], 'health-1')
        self.assertEqual(downloaded.data['workouts'][0]['heartRateAvg'], 150)
        self.assertEqual(downloaded.data['workouts'][0]['routePointCount'], 1)
        self.assertNotIn('route', downloaded.data['workouts'][0])

        server_id = downloaded.data['workouts'][0]['serverId']
        detail = self.client.get(f'/api/workouts/{server_id}/detail/')
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.data['detail']['route']), 1)

    def test_다른_사용자의_상세_파일은_받지_못한다(self):
        self.client.post(UPLOAD, _upload_payload(), format='multipart')
        server_id = Workout.objects.get().pk
        other = User.objects.create_user(username='other-detail-user')
        access = RefreshToken.for_user(other).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')

        response = self.client.get(f'/api/workouts/{server_id}/detail/')

        self.assertEqual(response.status_code, 404)

    def test_같은_운동을_다시_올려도_중복되지_않는다(self):
        self.client.post(UPLOAD, _upload_payload(), format='multipart')
        first_updated_at = Workout.objects.get().updatedAt
        response = self.client.post(UPLOAD, _upload_payload(), format='multipart')

        self.assertEqual(Workout.objects.count(), 1)
        self.assertTrue(response.data['unchanged'])
        self.assertEqual(Workout.objects.get().updatedAt, first_updated_at)

    def test_변경된_운동은_갱신한다(self):
        self.client.post(UPLOAD, _upload_payload(), format='multipart')
        response = self.client.post(
            UPLOAD,
            _upload_payload(metadata_overrides={'distanceMeters': 5100.0}),
            format='multipart',
        )

        self.assertFalse(response.data['unchanged'])
        self.assertEqual(Workout.objects.get().distanceMeters, 5100.0)

    def test_상세_파일만_바뀌어도_증분_다운로드에_포함된다(self):
        self.client.post(UPLOAD, _upload_payload(), format='multipart')
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
        self.client.post(
            UPLOAD,
            _upload_payload(detail_overrides=changed_detail),
            format='multipart',
        )

        response = self.client.get(DOWNLOAD, {'since': cursor.isoformat()})
        self.assertEqual([item['id'] for item in response.data['workouts']], ['health-1'])

    def test_since_이후_변경된_운동만_받는다(self):
        self.client.post(UPLOAD, _upload_payload('old'), format='multipart')
        cursor = timezone.now()
        old = Workout.objects.get(sourceWorkoutId='old')
        Workout.objects.filter(pk=old.pk).update(updatedAt=cursor - timedelta(seconds=1))
        self.client.post(UPLOAD, _upload_payload('new'), format='multipart')

        response = self.client.get(DOWNLOAD, {'since': cursor.isoformat()})

        self.assertEqual([item['id'] for item in response.data['workouts']], ['new'])

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
        self.assertEqual(self.client.post(UPLOAD, {}).status_code, 401)
        self.assertEqual(self.client.get(DOWNLOAD).status_code, 401)

    def test_끝이_시작보다_빠르면_거절한다(self):
        start = timezone.now().replace(microsecond=0)
        response = self.client.post(
            UPLOAD,
            _upload_payload(metadata_overrides={
                'startAt': start.isoformat(),
                'endAt': (start - timedelta(seconds=1)).isoformat(),
            }),
            format='multipart',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Workout.objects.count(), 0)


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
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.access}')
        return client.post(
            UPLOAD,
            _upload_payload(workout_id),
            format='multipart',
        ).status_code

    def test_운동_세_건을_병렬로_올려도_잠금_실패가_없다(self):
        with ThreadPoolExecutor(max_workers=3) as executor:
            statuses = list(executor.map(self._upload, ('run-1', 'run-2', 'run-3')))

        self.assertEqual(statuses, [200, 200, 200])
        self.assertEqual(Workout.objects.count(), 3)
