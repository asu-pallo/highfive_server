from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import InvalidToken as JwtInvalidToken
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings as jwt_settings
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from . import nickname as nickname_rules
from .firebase import InvalidToken, NotConfigured, verify_id_token
from .models import Profile
from .serializers import (
    AutoLoginSerializer,
    NicknameSerializer,
    ProfileSerializer,
    SignInSerializer,
)

# 응답은 열품타와 같이 `{'s': bool, ...}` 형태로 통일한다.


def _fail(message: str, code: int = status.HTTP_400_BAD_REQUEST) -> Response:
    return Response({'s': False, 'msg': message}, status=code)


def _tokens_for(user: User) -> dict:
    refresh = RefreshToken.for_user(user)
    return {'access': str(refresh.access_token), 'refresh': str(refresh)}


CLIENT_FIELDS = ('deviceType', 'deviceModel', 'osVersion', 'appVersion',
                 'timezone', 'fcmToken')


def _apply_client_info(profile: Profile, data: dict) -> list[str]:
    """받은 기기·앱 정보만 반영한다. 안 보낸 항목은 이전 값을 유지한다.

    소셜 로그인과 자동 로그인 양쪽에서 부른다 — 갱신 시점은 '로그인할 때' 하나다.
    """
    changed = []
    for field in CLIENT_FIELDS:
        if field in data and getattr(profile, field) != data[field]:
            setattr(profile, field, data[field])
            changed.append(field)
    return changed


@api_view(['POST'])
@permission_classes([AllowAny])
def sign_in(request):
    """소셜 로그인. 가입을 겸한다.

    소셜 로그인은 클라이언트가 "처음인지"를 알 수 없다. 서버가 uid 로 판단해
    없으면 만들고, 있으면 그대로 쓴다.

    가입 직후에는 닉네임이 없다. 앱은 프로필의 nickname 으로 설정 필요 여부를 판단한다.

    기기 정보와 FCM 토큰을 함께 받아 갱신한다. 두 번째부터는 [auto_login] 이 같은 일을
    한다 — 로그인하는 시점이 곧 갱신 시점이다.
    """
    body = SignInSerializer(data=request.data)
    if not body.is_valid():
        return _fail('요청 형식이 올바르지 않습니다.')
    data = body.validated_data

    try:
        account = verify_id_token(data['idToken'])
    except NotConfigured as error:
        # 클라이언트 잘못이 아니라 서버 설정 문제다. 500 으로 알린다.
        return _fail(str(error), status.HTTP_500_INTERNAL_SERVER_ERROR)
    except InvalidToken:
        return _fail('로그인 정보가 유효하지 않습니다. 다시 로그인해 주세요.',
                     status.HTTP_401_UNAUTHORIZED)

    with transaction.atomic():
        # username 에 Firebase uid 를 쓴다. 프로젝트 안에서 유일하고 바뀌지 않는다.
        user, _ = User.objects.get_or_create(
            username=account.uid,
            defaults={'email': account.email or ''},
        )
        profile, _ = Profile.objects.get_or_create(
            user=user,
            defaults={'loginProvider': account.provider},
        )

        # 같은 계정이 다른 제공자로 들어오는 경우는 없지만(uid 가 제공자별로 다르다),
        # 값이 비어 있던 예전 행을 메우는 용도로 둔다.
        if profile.loginProvider != account.provider:
            profile.loginProvider = account.provider

        _apply_client_info(profile, data)
        profile.save()  # auto_now 라 lastLoginAt 은 여기서 갱신된다.

    return Response({
        's': True,
        'profile': ProfileSerializer(profile).data,
        **_tokens_for(user),
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def auto_login(request):
    """자동 로그인. 저장해 둔 refresh 토큰으로 세션을 잇는다.

    앱을 켤 때 refresh 토큰이 있으면 소셜 로그인 화면을 건너뛰고 이걸 부른다.
    access 가 쓰는 도중 만료됐을 때도 같은 API 로 다시 받는다.

    **만료된 access 토큰으로는 부를 수 없으므로 AllowAny 다.** 인증은 헤더가 아니라
    본문에 담긴 refresh 토큰 자체가 한다.

    소셜 로그인과 마찬가지로 기기 정보와 FCM 토큰을 함께 받아 갱신한다.
    로그인하는 시점이 곧 갱신 시점이라, 별도의 기기 정보 API 를 두지 않는다.

    설정이 `ROTATE_REFRESH_TOKENS` 라 **refresh 토큰도 새것으로 바뀐다.**
    앱은 응답의 refresh 로 저장해 둔 값을 반드시 덮어써야 한다. 예전 것을 계속 쓰면
    블랙리스트에 걸려 다음부터 막힌다.
    """
    body = AutoLoginSerializer(data=request.data)
    if not body.is_valid():
        # refresh 가 없는 것과 기기 정보 형식이 틀린 것은 앱이 할 일이 다르다.
        # 하나로 뭉뚱그리면 어디를 고쳐야 할지 알 수 없다.
        if 'refresh' in body.errors:
            return _fail('refresh 토큰이 필요합니다.')
        return _fail('기기 정보 형식이 올바르지 않습니다.')
    data = body.validated_data

    tokenBody = TokenRefreshSerializer(data={'refresh': data['refresh']})
    try:
        tokenBody.is_valid(raise_exception=True)
    except (TokenError, JwtInvalidToken):
        # 만료됐거나, 위조됐거나, 이미 한 번 써서 블랙리스트에 올라간 토큰이다.
        # 어느 쪽이든 앱이 할 일은 같다 — 소셜 로그인부터 다시.
        return _fail('세션이 만료되었습니다. 다시 로그인해 주세요.',
                     status.HTTP_401_UNAUTHORIZED)

    tokens = tokenBody.validated_data

    # 새로 발급된 access 토큰에서 사용자를 꺼낸다. 들어온 refresh 토큰은 이미
    # 블랙리스트에 올라가 있어서 다시 열어 보면 실패한다.
    userId = AccessToken(tokens['access'])[jwt_settings.USER_ID_CLAIM]
    profile = Profile.objects.select_related('user').get(user_id=userId)

    changed = _apply_client_info(profile, data)
    if changed:
        profile.save(update_fields=[*changed, 'lastLoginAt'])
    else:
        # 바뀐 게 없어도 접속 시각은 남긴다.
        profile.save(update_fields=['lastLoginAt'])

    return Response({
        's': True,
        'profile': ProfileSerializer(profile).data,
        **tokens,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def check_nickname(request):
    """쓸 수 있는 닉네임인지 미리 확인한다.

    **여기서 통과해도 등록 시점에 실패할 수 있다.** 그 사이 다른 사람이 같은 이름을
    가져갈 수 있기 때문이다. 최종 판정은 [set_nickname] 의 DB 제약이 한다.
    """
    try:
        _, key = nickname_rules.validate(request.query_params.get('nickname', ''))
    except nickname_rules.NicknameError as error:
        return Response({'s': True, 'available': False, 'msg': str(error)})

    taken = (
        Profile.objects.filter(nicknameKey=key)
        .exclude(user=request.user)
        .exists()
    )
    return Response({
        's': True,
        'available': not taken,
        'msg': '이미 사용 중인 닉네임이에요.' if taken else '',
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def set_nickname(request):
    """닉네임을 정하거나 바꾼다."""
    body = NicknameSerializer(data=request.data)
    if not body.is_valid():
        return _fail('닉네임을 입력해 주세요.')

    try:
        value, key = nickname_rules.validate(body.validated_data['nickname'])
    except nickname_rules.NicknameError as error:
        return _fail(str(error))

    profile = request.user.profile
    profile.nickname = value
    profile.nicknameKey = key

    try:
        # 중복을 미리 조회해서 막지 않는다. 조회와 저장 사이에 다른 요청이 끼어들면
        # 뚫리기 때문이다(check-then-act). unique 제약이 걸리는 걸 잡아서 처리한다.
        with transaction.atomic():
            profile.save(update_fields=['nickname', 'nicknameKey', 'lastLoginAt'])
    except IntegrityError:
        return _fail('이미 사용 중인 닉네임이에요.', status.HTTP_409_CONFLICT)

    return Response({'s': True, 'profile': ProfileSerializer(profile).data})
