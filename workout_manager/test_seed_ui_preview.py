from unittest.mock import patch
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .encounters import encounter_summaries
from .management.commands.seed_ui_preview import (
    Command,
    WorkoutTemplate,
    _route_for,
    _transform_route,
)
from .models import (
    HighFive,
    TrajectorySegment,
    UserFamiliarity,
    Workout,
    WorkoutDetail,
    WorkoutDistanceRecord,
    WorkoutMetrics,
)
from .workout_metrics import rebuild_workout_metrics
from .workout_statistics import workout_statistics


def _save_detail(command, workout, route, heart_rate):
    rebuild_workout_metrics(workout, route, heart_rate)
    WorkoutDetail.objects.create(
        workout=workout,
        routeObjectKey=f'preview/{workout.id}.json',
        routeContentHash='a' * 64,
        routeFileSize=100,
        contentHash='b' * 64,
        routePointCount=len(route),
        heartRateSampleCount=len(heart_rate),
        fileSize=100,
    )


def _load_template(command, owner, source_workout_id):
    started_at = timezone.now() - timedelta(hours=1)
    ended_at = started_at + timedelta(minutes=10)
    source, _ = Workout.objects.get_or_create(
        user=owner,
        source='appleHealth',
        sourceName='Apple Watch',
        sourceWorkoutId='real-source',
        defaults={
            'kind': 'running',
            'startAt': started_at,
            'endAt': ended_at,
        },
    )
    route = _route_for(1, started_at, ended_at)
    heart_rate = [
        {
            'timestamp': (started_at + timedelta(seconds=index * 30)).isoformat(),
            'bpm': 110 + index,
        }
        for index in range(20)
    ]
    return WorkoutTemplate(source, route, heart_rate)


class SeedUiPreviewCommandTest(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='preview-owner')

    def test_120으로_반복된_케이던스도_시드에서는_변동한다(self):
        started_at = timezone.now().replace(microsecond=0)
        ended_at = started_at + timedelta(minutes=30)
        source = [
            {
                'timestamp': (
                    started_at + timedelta(minutes=index)
                ).isoformat(),
                'latitude': 37.5 + index * 0.0001,
                'longitude': 127.0,
                'cadence': 120,
            }
            for index in range(31)
        ]

        route = _transform_route(
            source,
            started_at,
            ended_at,
            coordinate_variant=0,
        )
        cadence = [point['cadence'] for point in route]

        self.assertGreater(len(set(cadence)), 10)
        self.assertLess(min(cadence), 120)
        self.assertGreater(max(cadence), 120)

    @patch.object(Command, '_guard_local_environment')
    @patch.object(Command, '_delete_object_prefix')
    @patch.object(
        Command,
        '_save_avatar',
        return_value=('profiles/preview/512.webp', 'profiles/preview/128.webp'),
    )
    @patch.object(Command, '_load_template', new=_load_template)
    @patch.object(Command, '_save_detail', new=_save_detail)
    def test_시나리오를_중복없이_만들고_제거한다(
        self,
        save_avatar,
        delete_object_prefix,
        guard_local_environment,
    ):
        call_command('seed_ui_preview', owner_id=self.owner.id)

        workouts = list(
            Workout.objects.filter(user=self.owner, sourceName__contains='UI Preview')
            .order_by('sourceWorkoutId')
        )
        self.assertEqual(len(workouts), 3)
        self.assertTrue(
            all(5_000 <= (workout.distanceMeters or 0) < 6_000 for workout in workouts)
        )
        self.assertEqual(
            WorkoutDistanceRecord.objects.filter(
                workout__in=workouts,
                distanceMeters=5_000,
            ).count(),
            3,
        )
        metrics = WorkoutMetrics.objects.filter(workout__in=workouts)
        self.assertEqual(metrics.count(), 3)
        self.assertTrue(all(len(item.cadenceSeries) == 50 for item in metrics))
        self.assertTrue(
            all(len(set(item.cadenceSeries)) > 1 for item in metrics)
        )
        target_distances = Workout.objects.filter(
            sourceWorkoutId__startswith=f'ui-preview-target-{self.owner.id}-'
        ).values_list('distanceMeters', flat=True)
        self.assertTrue(all(5_000 <= value <= 10_050 for value in target_distances))
        self.assertEqual(HighFive.objects.filter(fromUser=self.owner).count(), 6)
        familiarities = UserFamiliarity.objects.filter(
            firstUser=self.owner,
        ) | UserFamiliarity.objects.filter(secondUser=self.owner)
        self.assertTrue(familiarities.exists())
        self.assertTrue(all(item.metCount > 0 for item in familiarities))
        received = HighFive.objects.filter(toUser=self.owner)
        self.assertEqual(received.count(), 9)
        self.assertEqual(received.values('toWorkout_id').distinct().count(), 2)
        self.assertGreaterEqual(received.values('cellId').distinct().count(), 3)
        summaries = encounter_summaries([workout.id for workout in workouts])
        combinations = sorted(
            (
                summary['totalCount'],
                summary['highFiveCount'],
            )
            for summary in summaries.values()
        )
        self.assertEqual(
            combinations,
            [
                (1, 0),
                (9, 2),
                (14, 4),
            ],
        )

        # 같은 H3 셀에 배치된 샘플 러너들도 서로 다른 시각에 진입한다.
        target_segments = TrajectorySegment.objects.filter(
            workout__sourceWorkoutId__startswith=(
                f'ui-preview-target-{self.owner.id}-'
            )
        )
        entered_by_cell = {}
        for segment in target_segments:
            entered_by_cell.setdefault(segment.cellId, []).append(
                segment.period.lower
            )
        for entered_at in entered_by_cell.values():
            self.assertEqual(len(entered_at), len(set(entered_at)))

        three_cells = next(
            workout
            for workout in workouts
            if '세 셀' in workout.sourceName
        )
        self.assertEqual(
            sorted(
                area['count']
                for area in summaries[three_cells.id]['areas']
            ),
            [2, 3, 4],
        )

        four_cells = next(
            workout
            for workout in workouts
            if '네 셀' in workout.sourceName
        )
        self.assertEqual(
            sorted(area['count'] for area in summaries[four_cells.id]['areas']),
            [2, 3, 4, 5],
        )
        five_km = next(
            record
            for record in workout_statistics(four_cells)['pr']
            if record['distanceMeters'] == 5_000
        )
        self.assertEqual(len(five_km['records']), 2)
        # 통계는 해당 피드보다 과거인 기록만 보지 않는다. 어느 피드를 열어도
        # 현재 피드를 제외한 최근 기록이 같은 한도 안에서 함께 보여야 한다.
        for workout in workouts:
            statistics = workout_statistics(workout)
            five_km = next(
                record
                for record in statistics['pr']
                if record['distanceMeters'] == 5_000
            )
            self.assertEqual(len(five_km['records']), 2)
            self.assertNotIn(
                workout.id,
                {record['workoutId'] for record in five_km['records']},
            )
            # 오래된 피드를 열어도 그 시점 이전 기록으로 자르지 않고, 현재
            # 운동을 포함한 사용자의 최신 기록 분포를 반환한다.
            self.assertEqual(len(statistics['distribution']), 3)
            self.assertEqual(
                sum(
                    point['isCurrent']
                    for point in statistics['distribution']
                ),
                1,
            )

        # 다시 실행해도 먼저 기존 시드를 지우므로 운동 수가 늘어나지 않는다.
        call_command('seed_ui_preview', owner_id=self.owner.id)
        self.assertEqual(
            Workout.objects.filter(user=self.owner, sourceName__contains='UI Preview').count(),
            3,
        )

        call_command('seed_ui_preview', owner_id=self.owner.id, clear=True)
        self.assertFalse(
            Workout.objects.filter(user=self.owner, sourceName__contains='UI Preview').exists()
        )
        self.assertFalse(
            User.objects.filter(username__startswith=f'ui-preview-{self.owner.id}-').exists()
        )
