import hashlib

from rest_framework import serializers

from .models import Workout


class WorkoutUploadSerializer(serializers.Serializer):
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
    detailContentHash = serializers.RegexField(r'^[0-9a-f]{64}$')
    routeContentHash = serializers.RegexField(
        r'^(?:[0-9a-f]{64})?$', required=False, allow_blank=True, default=''
    )
    routeFileSize = serializers.IntegerField(min_value=0, default=0)
    heartRateContentHash = serializers.RegexField(
        r'^(?:[0-9a-f]{64})?$', required=False, allow_blank=True, default=''
    )
    heartRateFileSize = serializers.IntegerField(min_value=0, default=0)
    steps = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    flightsClimbed = serializers.IntegerField(
        required=False, allow_null=True, min_value=0
    )

    def validate(self, attrs):
        if attrs['endAt'] <= attrs['startAt']:
            raise serializers.ValidationError('운동 종료 시각은 시작 시각보다 늦어야 합니다.')

        for prefix in ('route', 'heartRate'):
            has_hash = bool(attrs[f'{prefix}ContentHash'])
            has_file = attrs[f'{prefix}FileSize'] > 0
            if has_hash != has_file:
                raise serializers.ValidationError(
                    f'{prefix} 파일 해시와 크기는 함께 전달해야 합니다.'
                )
        expected_detail_hash = hashlib.sha256(
            f"{attrs['routeContentHash']}:{attrs['heartRateContentHash']}".encode()
        ).hexdigest()
        if attrs['detailContentHash'] != expected_detail_hash:
            raise serializers.ValidationError('운동 상세 묶음 해시가 올바르지 않습니다.')
        return attrs


class WorkoutUploadPrepareSerializer(WorkoutUploadSerializer):
    pass


class WorkoutSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source='sourceWorkoutId')
    serverId = serializers.IntegerField(source='pk')
    routePointCount = serializers.IntegerField(
        source='detail.routePointCount', read_only=True, default=0
    )
    detailContentHash = serializers.CharField(
        source='detail.contentHash', read_only=True, default=''
    )

    class Meta:
        model = Workout
        fields = (
            'id',
            'serverId',
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
            'routePointCount',
            'detailContentHash',
        )
