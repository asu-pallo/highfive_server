from rest_framework import serializers

from .models import DeviceType, Profile


class ClientInfoSerializer(serializers.Serializer):
    """로그인할 때 함께 보내는 기기·앱 정보.

    소셜 로그인과 자동 로그인 양쪽이 이 형태를 받는다. 로그인 시점이 곧 갱신 시점이다.

    전부 선택 항목이다. 없으면 이전 값을 그대로 둘 뿐 요청을 막지 않는다.
    기기 정보를 못 받았다고 로그인을 실패시킬 이유는 없다.
    """

    deviceType = serializers.ChoiceField(choices=DeviceType.choices, required=False)
    deviceModel = serializers.CharField(max_length=100, required=False, allow_blank=True)
    osVersion = serializers.CharField(max_length=20, required=False, allow_blank=True)
    appVersion = serializers.CharField(max_length=20, required=False, allow_blank=True)
    timezone = serializers.CharField(max_length=50, required=False, allow_blank=True)
    fcmToken = serializers.CharField(max_length=255, required=False, allow_blank=True)


class SignInSerializer(ClientInfoSerializer):
    """소셜 로그인 요청. 가입을 겸한다."""

    idToken = serializers.CharField()


class AutoLoginSerializer(ClientInfoSerializer):
    """자동 로그인 요청. 저장해 둔 refresh 토큰으로 들어온다."""

    refresh = serializers.CharField()


class NicknameSerializer(serializers.Serializer):
    """닉네임 설정·변경 요청.

    형식 검사는 nickname.py 가 맡는다. 오류 메시지를 사용자에게 그대로 보여주려면
    한 곳에서 만들어야 해서, 여기서는 값만 받는다.
    """

    nickname = serializers.CharField(max_length=50)


class ProfileSerializer(serializers.ModelSerializer):
    """응답에 실어 보내는 프로필."""

    loginType = serializers.CharField(source='loginProvider', read_only=True)

    class Meta:
        model = Profile
        fields = (
            'nickname',
            'loginType',
            'deviceType',
            'deviceModel',
            'osVersion',
            'appVersion',
            'timezone',
            'createdAt',
        )
