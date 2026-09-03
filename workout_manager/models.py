from django.contrib.auth.models import User
from django.contrib.postgres.fields import DateTimeRangeField
from django.db import models
from django.db.models import F, Q


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
    """단순화된 지도 경로와 업로드 원본 해시를 관리하는 메타데이터."""

    workout = models.OneToOneField(
        Workout, on_delete=models.CASCADE, related_name='detail'
    )
    routeObjectKey = models.CharField(max_length=700, null=True, blank=True, unique=True)
    routeContentHash = models.CharField(max_length=64, blank=True, default='')
    routeFileSize = models.PositiveBigIntegerField(default=0)
    # 두 파일 해시를 합친 앱 캐시 비교용 해시다.
    contentHash = models.CharField(max_length=64)
    formatVersion = models.PositiveSmallIntegerField(default=1)
    routePointCount = models.PositiveIntegerField(default=0)
    heartRateSampleCount = models.PositiveIntegerField(default=0)
    fileSize = models.PositiveBigIntegerField(default=0)
    updatedAt = models.DateTimeField(auto_now=True)


class WorkoutDistanceRecord(models.Model):
    """한 운동에서 산출한 거리별 가장 빠른 연속 구간 기록."""

    workout = models.ForeignKey(
        Workout, on_delete=models.CASCADE, related_name='distanceRecords'
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='workoutDistanceRecords'
    )
    distanceMeters = models.PositiveIntegerField()
    durationMilliseconds = models.PositiveIntegerField()
    startedAt = models.DateTimeField()
    endedAt = models.DateTimeField()
    algorithmVersion = models.PositiveSmallIntegerField(default=1)
    createdAt = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('workout', 'distanceMeters'),
                name='unique_workout_distance_record',
            ),
        ]
        indexes = [
            models.Index(
                fields=('user', 'distanceMeters', 'durationMilliseconds'),
                name='workout_user_dist_duration_idx',
            ),
            models.Index(
                fields=('user', 'distanceMeters', 'createdAt'),
                name='workout_user_dist_created_idx',
            ),
        ]


class WorkoutMetrics(models.Model):
    """상세 화면 그래프용으로 원본을 고정 개수로 축약한 운동 통계."""

    workout = models.OneToOneField(
        Workout, on_delete=models.CASCADE, related_name='metrics'
    )
    paceSeries = models.JSONField(default=list)
    heartRateSeries = models.JSONField(default=list)
    cadenceSeries = models.JSONField(default=list)
    elevationSeries = models.JSONField(default=list)
    splitSeries = models.JSONField(default=list)
    heartRateZones = models.JSONField(default=list)
    sampleCount = models.PositiveSmallIntegerField(default=50)
    algorithmVersion = models.PositiveSmallIntegerField(default=1)
    updatedAt = models.DateTimeField(auto_now=True)


class WorkoutMetricsAggregate(models.Model):
    """사용자·운동 종류별 연도/전체 그래프 통계의 누적 합계."""

    class Scope(models.TextChoices):
        YEAR = 'year', 'Year'
        ALL = 'all', 'All'

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='workoutMetricsAggregates'
    )
    workoutKind = models.CharField(max_length=30)
    scope = models.CharField(max_length=10, choices=Scope.choices)
    scopeYear = models.PositiveSmallIntegerField(null=True, blank=True)
    workoutCount = models.PositiveIntegerField(default=0)
    distanceSum = models.FloatField(default=0)
    distanceCount = models.PositiveIntegerField(default=0)
    durationSum = models.FloatField(default=0)
    durationCount = models.PositiveIntegerField(default=0)
    paceSeriesSum = models.JSONField(default=list)
    paceSeriesCount = models.JSONField(default=list)
    heartRateSeriesSum = models.JSONField(default=list)
    heartRateSeriesCount = models.JSONField(default=list)
    cadenceSeriesSum = models.JSONField(default=list)
    cadenceSeriesCount = models.JSONField(default=list)
    elevationSeriesSum = models.JSONField(default=list)
    elevationSeriesCount = models.JSONField(default=list)
    splitSeriesSum = models.JSONField(default=list)
    splitSeriesCount = models.JSONField(default=list)
    heartRateZoneSecondsSum = models.JSONField(default=list)
    heartRateZoneSecondsCount = models.JSONField(default=list)
    sampleCount = models.PositiveSmallIntegerField(default=50)
    algorithmVersion = models.PositiveSmallIntegerField(default=1)
    updatedAt = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(scope='all', scopeYear__isnull=True)
                    | Q(scope='year', scopeYear__isnull=False)
                ),
                name='valid_metrics_aggregate_scope',
            ),
            models.UniqueConstraint(
                fields=('user', 'workoutKind'),
                condition=Q(scope='all'),
                name='unique_metrics_aggregate_all',
            ),
            models.UniqueConstraint(
                fields=('user', 'workoutKind', 'scopeYear'),
                condition=Q(scope='year'),
                name='unique_metrics_aggregate_year',
            ),
        ]
        indexes = [
            models.Index(
                fields=('user', 'workoutKind', 'scope', 'scopeYear'),
                name='metrics_aggregate_lookup_idx',
            ),
        ]


class UserWeeklyWorkoutStat(models.Model):
    """사용자별 한 주 운동 합계. 최근 1년 프로필 그래프의 조회 원본이다."""

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='weeklyWorkoutStats'
    )
    weekStart = models.DateField()
    workoutKind = models.CharField(max_length=30)
    distanceMeters = models.FloatField(default=0)
    durationSeconds = models.PositiveBigIntegerField(default=0)
    workoutCount = models.PositiveIntegerField(default=0)
    updatedAt = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('user', 'weekStart', 'workoutKind'),
                name='unique_user_weekly_workout_stat',
            ),
        ]
        indexes = [
            models.Index(
                fields=('user', 'workoutKind', 'weekStart'),
                name='weekly_workout_stat_lookup_idx',
            ),
        ]


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


class HighFive(models.Model):
    """한 사용자가 같은 운동에서 마주친 상대에게 남긴 일방향 기록."""

    fromUser = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='sentHighFives'
    )
    toUser = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='receivedHighFives'
    )
    fromWorkout = models.ForeignKey(
        Workout, on_delete=models.CASCADE, related_name='sentHighFives'
    )
    toWorkout = models.ForeignKey(
        Workout, on_delete=models.CASCADE, related_name='receivedHighFives'
    )
    cellId = models.CharField(max_length=32)
    encounteredAt = models.DateTimeField()
    # 두 운동 ID를 정렬한 값이다. 서로 각각 눌러도 누적 만남은 한 번으로 센다.
    encounterKey = models.CharField(max_length=50, db_index=True)
    createdAt = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('fromWorkout', 'toUser'),
                name='unique_workout_high_five_target',
            ),
        ]
        indexes = [
            models.Index(fields=('fromUser', 'toUser', 'createdAt')),
            models.Index(fields=('toUser', 'fromUser', 'createdAt')),
        ]


class UserFamiliarity(models.Model):
    """최초 하이파이브 뒤 두 사용자의 누적 만남을 관리하는 무방향 관계."""

    firstUser = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='familiaritiesAsFirst'
    )
    secondUser = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='familiaritiesAsSecond'
    )
    firstHighFiveAt = models.DateTimeField(auto_now_add=True)
    lastHighFiveAt = models.DateTimeField(auto_now_add=True)
    # 두 사용자가 H3·시간 기준으로 마주친 운동 쌍의 누적 개수다.
    metCount = models.PositiveIntegerField(default=0)
    # 이 시각까지 서버에 업로드된 두 사용자의 운동을 집계했다.
    metCheckedAt = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('firstUser', 'secondUser'),
                name='unique_user_familiarity_pair',
            ),
            models.CheckConstraint(
                condition=Q(firstUser__lt=F('secondUser')),
                name='ordered_user_familiarity_pair',
            ),
        ]
