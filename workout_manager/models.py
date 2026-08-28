from django.contrib.auth.models import User
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

