from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Workout


UPLOAD = '/api/workouts/upload/'
DOWNLOAD = '/api/workouts/'


def _workout(workout_id='health-1', **overrides):
    start = timezone.now().replace(microsecond=0) - timedelta(hours=1)
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
        'heartRateAvg': 150,
        'heartRateMin': 110,
        'heartRateMax': 180,
        'heartRateSampleCount': 120,
        'steps': 6000,
        'flightsClimbed': 2,
    }
    data.update(overrides)
    return data


class WorkoutApiTest(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='runner')
        access = RefreshToken.for_user(self.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')

    def test_운동을_올리고_받는다(self):
        uploaded = self.client.post(UPLOAD, {'workouts': [_workout()]})
        downloaded = self.client.get(DOWNLOAD)

        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(uploaded.data['created'], 1)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(len(downloaded.data['workouts']), 1)
        self.assertEqual(downloaded.data['workouts'][0]['id'], 'health-1')
        self.assertNotIn('route', downloaded.data['workouts'][0])

    def test_같은_운동을_다시_올려도_중복되지_않는다(self):
        self.client.post(UPLOAD, {'workouts': [_workout()]})
        first_updated_at = Workout.objects.get().updatedAt
        response = self.client.post(UPLOAD, {'workouts': [_workout()]})

        self.assertEqual(Workout.objects.count(), 1)
        self.assertEqual(response.data['unchanged'], 1)
        self.assertEqual(Workout.objects.get().updatedAt, first_updated_at)

    def test_변경된_운동은_갱신한다(self):
        self.client.post(UPLOAD, {'workouts': [_workout()]})
        response = self.client.post(
            UPLOAD,
            {'workouts': [_workout(distanceMeters=5100.0)]},
        )

        self.assertEqual(response.data['updated'], 1)
        self.assertEqual(Workout.objects.get().distanceMeters, 5100.0)

    def test_since_이후_변경된_운동만_받는다(self):
        self.client.post(UPLOAD, {'workouts': [_workout('old')]})
        cursor = timezone.now()
        old = Workout.objects.get(sourceWorkoutId='old')
        Workout.objects.filter(pk=old.pk).update(updatedAt=cursor - timedelta(seconds=1))
        self.client.post(UPLOAD, {'workouts': [_workout('new')]})

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
        self.assertEqual(self.client.post(UPLOAD, {'workouts': []}).status_code, 401)
        self.assertEqual(self.client.get(DOWNLOAD).status_code, 401)

    def test_끝이_시작보다_빠르면_거절한다(self):
        start = timezone.now().replace(microsecond=0)
        response = self.client.post(
            UPLOAD,
            {'workouts': [_workout(
                startAt=start.isoformat(),
                endAt=(start - timedelta(seconds=1)).isoformat(),
            )]},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Workout.objects.count(), 0)
