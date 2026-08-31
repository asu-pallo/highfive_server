from django.contrib.auth.models import User
from django.contrib.postgres.fields import DateTimeRangeField
from django.db import models


class Workout(models.Model):
    """건강 플랫폼에서 정규화되어 올라온 사용자 운동 기록."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='workouts')
    source = models.CharField(max_length=30)
    sourceName = models.CharField(max_length=255)
    sourceWorkoutId = models.CharField(max_length=255)
    kind = models.CharField(max_length=30)
    rawType = models.CharField(max_length=100, blank=True, default='')
    startAt = models.DateTimeField(db_index=True)
    endAt = models.DateTimeField()
    distanceMeters = models.FloatField(null=True, blank=True)
    kcal = models.IntegerField(null=True, blank=True)
    heartRateAvg = models.IntegerField(null=True, blank=True)
    heartRateMin = models.IntegerField(null=True, blank=True)
    heartRateMax = models.IntegerField(null=True, blank=True)
    heartRateSampleCount = models.IntegerField(null=True, blank=True)
    steps = models.IntegerField(null=True, blank=True)
    flightsClimbed = models.IntegerField(null=True, blank=True)
    createdAt = models.DateTimeField(auto_now_add=True)
    updatedAt = models.DateTimeField(auto_now=True, db_index=True)
    deletedAt = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('user', 'source', 'sourceName', 'sourceWorkoutId'),
                name='unique_user_source_workout',
            ),
        ]
        indexes = [
            models.Index(fields=('user', 'updatedAt', 'id')),
            models.Index(fields=('user', 'startAt')),
        ]


class WorkoutDetail(models.Model):
    """지도 경로·심박 원본 JSON 파일의 객체 저장소 메타데이터."""

    workout = models.OneToOneField(
        Workout, on_delete=models.CASCADE, related_name='detail'
    )
    objectKey = models.CharField(max_length=700, unique=True)
    contentHash = models.CharField(max_length=64)
    formatVersion = models.PositiveSmallIntegerField(default=1)
    routePointCount = models.PositiveIntegerField(default=0)
    heartRateSampleCount = models.PositiveIntegerField(default=0)
    fileSize = models.PositiveBigIntegerField(default=0)
    updatedAt = models.DateTimeField(auto_now=True)


class SpatialIndexVersion(models.Model):
    """운동 경로를 공간 셀로 변환하는 서버 정책 버전."""

    indexType = models.CharField(max_length=30)
    algorithmVersion = models.PositiveSmallIntegerField()
    parameters = models.JSONField(default=dict)
    isActive = models.BooleanField(default=False, db_index=True)
    createdAt = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('indexType', 'algorithmVersion'),
                name='unique_spatial_index_algorithm_version',
            ),
            models.UniqueConstraint(
                fields=('isActive',),
                condition=models.Q(isActive=True),
                name='unique_active_spatial_index_version',
            ),
        ]


class TrajectorySegment(models.Model):
    """한 운동이 공간 셀에 연속해서 머문 시간 구간."""

    workout = models.ForeignKey(
        Workout, on_delete=models.CASCADE, related_name='trajectorySegments'
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='trajectorySegments'
    )
    indexVersion = models.ForeignKey(
        SpatialIndexVersion,
        on_delete=models.PROTECT,
        related_name='trajectorySegments',
    )
    sequence = models.PositiveIntegerField()
    cellId = models.CharField(max_length=32)
    period = DateTimeRangeField()
    createdAt = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('workout', 'indexVersion', 'sequence'),
                name='unique_workout_index_segment_sequence',
            ),
        ]
        indexes = [
            models.Index(fields=('workout', 'indexVersion', 'sequence')),
            models.Index(fields=('indexVersion', 'cellId')),
        ]
