from django.contrib.auth.models import User
from django.db import models


class LoginProvider(models.TextChoices):
    GOOGLE = 'google', 'Google'
    APPLE = 'apple', 'Apple'


class DeviceType(models.TextChoices):
    IOS = 'ios', 'iOS'
    ANDROID = 'android', 'Android'


class Profile(models.Model):
    """사용자 부가 정보.

    테이블 이름은 Django 기본 규칙에 따라 `user_manager_profile` 이 된다.
    열품타와 같은 이름이라 `db_table` 을 따로 지정하지 않는다.

    필드 이름은 열품타와 맞춰 camelCase 로 쓴다. Django 관례(snake_case)와는
    다르지만, 두 서버를 오갈 때 헷갈리지 않는 쪽을 택했다. API 로 나가는 JSON 도
    camelCase 라 변환 계층이 필요 없다.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')

    # ── 닉네임 ──
    #
    # **중복을 허용한다.** 사람 이름처럼 겹쳐도 되는 표시용 이름이라, 먼저 온 사람이
    # 흔한 이름을 선점하는 걸 막지 않는다. 사용자 식별은 `user_id` 가 한다.
    nickname = models.CharField(max_length=12, null=True, blank=True, default=None)

    # 검색·정렬용 정규화 값(NFKC + 소문자). 중복 판정에는 쓰지 않는다.
    # 나중에 닉네임 검색이나 멘션을 붙일 때 대소문자·전각을 신경 쓰지 않으려고 남겨 둔다.
    nicknameKey = models.CharField(
        max_length=12, null=True, blank=True, default=None, db_index=True
    )

    loginProvider = models.CharField(
        max_length=10, choices=LoginProvider.choices, db_index=True
    )

    # ── 기기 정보 ──
    #
    # 로그인할 때(소셜·자동 모두) 갱신된다. 전부 OS 권한 없이 읽히는 값만 받는다.
    # 광고 ID(IDFA/GAID)와 설치 식별자(IDFV/ANDROID_ID)는 받지 않는다. 쓸 곳이 없는데
    # 스토어 신고 대상이 된다.
    #
    # ⚠️ 한 줄에 덮어쓰므로 **마지막에 로그인한 기기만 남는다.** 한 계정이 아이폰과
    # 갤럭시를 같이 쓰면 나중 것만 남고, fcmToken 도 마찬가지라 **먼저 쓰던 기기로는
    # 푸시가 가지 않는다.** 다기기를 제대로 다뤄야 하는 시점에 Device 테이블로 분리한다.
    deviceType = models.CharField(
        max_length=10, choices=DeviceType.choices, default='', blank=True, db_index=True
    )
    deviceModel = models.CharField(max_length=100, default='', blank=True)
    osVersion = models.CharField(max_length=20, default='', blank=True)
    appVersion = models.CharField(max_length=20, default='', blank=True)

    # IANA 이름(`Asia/Seoul`). 시각은 UTC 로 저장하지만, 하이파이브 판정과 통계는
    # 사용자의 하루 경계를 알아야 해서 따로 들고 있는다.
    timezone = models.CharField(max_length=50, default='', blank=True)

    # 푸시 발송용. 앱 재설치·데이터 삭제·장기 미사용으로 무효가 되므로, 로그인할 때마다
    # 받아서 갱신한다. 길이는 규격이 정해져 있지 않아 넉넉히 잡는다(현재 160자 안팎).
    fcmToken = models.CharField(max_length=255, default='', blank=True, db_index=True)

    createdAt = models.DateTimeField(auto_now_add=True, db_index=True)

    # `auto_now` 라 이 행을 저장할 때마다 갱신된다. 로그인뿐 아니라 기기 정보 갱신·
    # 닉네임 변경에도 올라가므로, 엄밀히는 '마지막으로 접속한 시각'에 가깝다.
    # 앱이 실행할 때마다 기기 정보를 올리기 때문에 실제 의미는 크게 다르지 않다.
    lastLoginAt = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['loginProvider', 'createdAt'])]

    def __str__(self):
        return self.nickname or f'(닉네임 없음) {self.user.username}'

    @property
    def needNickname(self) -> bool:
        """가입 직후처럼 닉네임을 아직 정하지 않은 상태."""
        return not self.nickname
