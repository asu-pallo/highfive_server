from rest_framework import serializers

from .models import Workout


class WorkoutUploadItemSerializer(serializers.Serializer):
    source = serializers.ChoiceField(
        choices=('appleHealth', 'healthConnect', 'samsungHealth', 'native')
    )
    sourceName = serializers.CharField(max_length=255)
    sourceWorkoutId = serializers.CharField(max_length=255)
    kind = serializers.ChoiceField(
        choices=('running', 'walking', 'hiking', 'cycling', 'swimming', 'other')
    )
    rawType = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default=''
    )
    startAt = serializers.DateTimeField()
    endAt = serializers.DateTimeField()
    distanceMeters = serializers.FloatField(required=False, allow_null=True, min_value=0)
    kcal = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    heartRateAvg = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    heartRateMin = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    heartRateMax = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    heartRateSampleCount = serializers.IntegerField(
        required=False, allow_null=True, min_value=0
    )
    steps = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    flightsClimbed = serializers.IntegerField(
        required=False, allow_null=True, min_value=0
    )

    def validate(self, attrs):
        if attrs['endAt'] <= attrs['startAt']:
            raise serializers.ValidationError('운동 종료 시각은 시작 시각보다 늦어야 합니다.')

        heart_fields = (
            'heartRateAvg',
            'heartRateMin',
            'heartRateMax',
            'heartRateSampleCount',
        )
        supplied = [attrs.get(field) is not None for field in heart_fields]
        if any(supplied) and not all(supplied):
            raise serializers.ValidationError('심박 요약 값은 모두 함께 보내야 합니다.')
        if all(supplied):
            if not attrs['heartRateMin'] <= attrs['heartRateAvg'] <= attrs['heartRateMax']:
                raise serializers.ValidationError('심박 최솟값·평균·최댓값 순서가 올바르지 않습니다.')
        return attrs


class WorkoutUploadSerializer(serializers.Serializer):
    workouts = WorkoutUploadItemSerializer(many=True, max_length=500)


class WorkoutSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source='sourceWorkoutId')

    class Meta:
        model = Workout
        fields = (
            'id',
            'source',
            'sourceName',
            'kind',
            'rawType',
            'startAt',
            'endAt',
            'distanceMeters',
            'kcal',
            'heartRateAvg',
            'heartRateMin',
            'heartRateMax',
            'heartRateSampleCount',
            'steps',
            'flightsClimbed',
            'updatedAt',
            'deletedAt',
        )
