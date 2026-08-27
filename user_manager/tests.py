from unittest.mock import patch

from django.contrib.auth.models import User
from rest_framework.test import APITestCase

from .firebase import InvalidToken, SocialAccount
from .models import Profile

SIGNIN = '/api/auth/signin/'
AUTOLOGIN = '/api/auth/autologin/'
NICKNAME = '/api/users/nickname/'

CLIENT = {
    'deviceType': 'ios',
    'deviceModel': 'iPhone16,2',
    'osVersion': '18.2',
    'appVersion': '1.0.0',
    'timezone': 'Asia/Seoul',
    'fcmToken': 'fcm-token-1',
}


def _account(uid='uid-1', provider='google', email='a@example.com'):
    return SocialAccount(uid=uid, provider=provider, email=email)


class SignInTest(APITestCase):
    """가입 겸 로그인."""

    def _signin(self, account=None, **body):
        with patch('user_manager.views.verify_id_token',
                   return_value=account or _account()):
            return self.client.post(SIGNIN, {'idToken': 'x', **CLIENT, **body})

    def test_처음이면_가입되고_닉네임이_필요하다(self):
        res = self._signin()

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['s'])
        self.assertIsNone(res.data['profile']['nickname'])
        self.assertEqual(res.data['profile']['loginType'], 'google')
        self.assertTrue(res.data['access'])
        self.assertTrue(res.data['refresh'])

    def test_기기정보가_저장된다(self):
        self._signin()
        profile = Profile.objects.get()

        for field, value in CLIENT.items():
            self.assertEqual(getattr(profile, field), value, field)
        self.assertEqual(profile.loginProvider, 'google')

    def test_uid_를_username_으로_쓴다(self):
        self._signin(_account(uid='firebase-uid-abc'))
        self.assertTrue(User.objects.filter(username='firebase-uid-abc').exists())

    def test_두_번째부터는_가입이_아니다(self):
        self._signin()
        res = self._signin()

        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(Profile.objects.count(), 1)

    def test_기기를_바꾸면_갱신된다(self):
        self._signin()
        self._signin(deviceType='android', deviceModel='SM-S928N', osVersion='14')

        profile = Profile.objects.get()
        self.assertEqual(profile.deviceType, 'android')
        self.assertEqual(profile.deviceModel, 'SM-S928N')
        # 안 보낸 항목은 이전 값이 남는다.
        self.assertEqual(profile.timezone, 'Asia/Seoul')

    def test_기기정보가_없어도_로그인은_된다(self):
        with patch('user_manager.views.verify_id_token', return_value=_account()):
            res = self.client.post(SIGNIN, {'idToken': 'x'})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(Profile.objects.get().deviceModel, '')

    def test_토큰이_유효하지_않으면_401(self):
        with patch('user_manager.views.verify_id_token', side_effect=InvalidToken('bad')):
            res = self.client.post(SIGNIN, {'idToken': 'x'})

        self.assertEqual(res.status_code, 401)
        self.assertFalse(res.data['s'])
        self.assertEqual(User.objects.count(), 0)

    def test_idToken_이_없으면_400(self):
        res = self.client.post(SIGNIN, CLIENT)
        self.assertEqual(res.status_code, 400)

    def test_로그인은_인증_없이_부를_수_있다(self):
        """DRF 기본이 IsAuthenticated 라 AllowAny 를 빠뜨리면 여기서 걸린다."""
        res = self._signin()
        self.assertNotEqual(res.status_code, 401)


class AutoLoginTest(APITestCase):
    """자동 로그인 — 세션 잇기 + 기기 정보·FCM 토큰 갱신."""

    def setUp(self):
        with patch('user_manager.views.verify_id_token', return_value=_account()):
            res = self.client.post(SIGNIN, {'idToken': 'x', **CLIENT})
        self.refresh = res.data['refresh']
        self.access = res.data['access']

    def _login(self, **client):
        return self.client.post(AUTOLOGIN, {'refresh': self.refresh, **client})

    # ── 토큰 ──

    def test_새_토큰을_받는다(self):
        res = self._login()

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['s'])
        self.assertNotEqual(res.data['access'], self.access)

    def test_refresh_도_새것으로_바뀐다(self):
        """ROTATE_REFRESH_TOKENS 가 켜져 있어야 한다. 앱은 이 값으로 덮어써야 한다."""
        res = self._login()
        self.assertNotEqual(res.data['refresh'], self.refresh)

    def test_한_번_쓴_refresh_는_다시_못_쓴다(self):
        """BLACKLIST_AFTER_ROTATION. 탈취된 토큰이 재사용되는 걸 막는다."""
        self._login()
        res = self._login()

        self.assertEqual(res.status_code, 401)

    def test_새_access_로_인증된다(self):
        res = self._login()

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {res.data["access"]}')
        me = self.client.post(NICKNAME, {'nickname': '이름테스트'})
        self.assertEqual(me.status_code, 200)

    def test_프로필도_함께_온다(self):
        res = self._login()

        self.assertIsNone(res.data['profile']['nickname'])
        self.assertEqual(res.data['profile']['loginType'], 'google')
        self.assertEqual(res.data['profile']['deviceModel'], 'iPhone16,2')

    def test_엉터리_토큰은_401(self):
        res = self.client.post(AUTOLOGIN, {'refresh': 'not-a-token'})
        self.assertEqual(res.status_code, 401)
        self.assertFalse(res.data['s'])

    def test_refresh_가_없으면_400(self):
        res = self.client.post(AUTOLOGIN, {})
        self.assertEqual(res.status_code, 400)
        self.assertIn('refresh', res.data['msg'])

    def test_인증_헤더_없이_부를_수_있다(self):
        """access 가 만료된 상태에서 부르는 API 라 AllowAny 여야 한다."""
        res = self._login()
        self.assertEqual(res.status_code, 200)

    # ── 기기 정보·FCM 토큰 갱신 ──
    #
    # 별도의 기기 정보 API 를 두지 않는 이유가 여기다. 로그인 시점이 갱신 시점이다.

    def test_os_를_올리면_반영된다(self):
        res = self._login(osVersion='18.3', appVersion='1.1.0')

        self.assertEqual(res.status_code, 200)
        profile = Profile.objects.get()
        self.assertEqual(profile.osVersion, '18.3')
        self.assertEqual(profile.appVersion, '1.1.0')
        # 안 보낸 항목은 그대로다.
        self.assertEqual(profile.deviceModel, 'iPhone16,2')

    def test_fcm_토큰이_갱신된다(self):
        """앱 재설치 등으로 토큰이 바뀌면 여기서 새 값이 들어와야 푸시가 간다."""
        self._login(fcmToken='fcm-token-2')
        self.assertEqual(Profile.objects.get().fcmToken, 'fcm-token-2')

    def test_타임존_변경도_반영된다(self):
        self._login(timezone='Europe/Paris')
        self.assertEqual(Profile.objects.get().timezone, 'Europe/Paris')

    def test_기기정보를_안_보내도_로그인은_된다(self):
        res = self._login()

        self.assertEqual(res.status_code, 200)
        # 이전 값이 지워지면 안 된다.
        self.assertEqual(Profile.objects.get().deviceModel, 'iPhone16,2')

    def test_접속_시각은_항상_갱신된다(self):
        before = Profile.objects.get().lastLoginAt
        self._login()
        self.assertGreater(Profile.objects.get().lastLoginAt, before)

    def test_모르는_deviceType_은_400(self):
        res = self._login(deviceType='windows')
        self.assertEqual(res.status_code, 400)
        # refresh 문제와 구분돼야 앱이 어디를 고칠지 안다.
        self.assertIn('기기 정보', res.data['msg'])


class NicknameTest(APITestCase):
    def setUp(self):
        self.profile = self._join('uid-1')
        self.other = self._join('uid-2')
        self._login(self.profile)

    def _join(self, uid):
        with patch('user_manager.views.verify_id_token', return_value=_account(uid=uid)):
            self.client.post(SIGNIN, {'idToken': 'x'})
        return Profile.objects.get(user__username=uid)

    def _login(self, profile):
        with patch('user_manager.views.verify_id_token',
                   return_value=_account(uid=profile.user.username)):
            res = self.client.post(SIGNIN, {'idToken': 'x'})
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {res.data["access"]}')

    def test_닉네임을_정할_수_있다(self):
        res = self.client.post(NICKNAME, {'nickname': '달리는사람'})

        self.assertEqual(res.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.nickname, '달리는사람')
        self.assertEqual(res.data['profile']['nickname'], '달리는사람')

    def test_앞뒤_공백은_지운다(self):
        self.client.post(NICKNAME, {'nickname': '  runner  '})
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.nickname, 'runner')

    def test_가운데_공백은_거절한다(self):
        """`달리는사람` 과 `달리는 사람` 이 나란히 존재하면 피드에서 구분할 수 없다."""
        res = self.client.post(NICKNAME, {'nickname': '달리는 사람'})
        self.assertEqual(res.status_code, 400)

    def test_공백은_조용히_붙이지_않는다(self):
        """가운데 공백을 없애 저장하면 사용자가 친 것과 다른 이름이 된다."""
        self.client.post(NICKNAME, {'nickname': '달리는 사람'})
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.nickname)

    def test_같은_닉네임을_여러_사람이_쓸_수_있다(self):
        """표시용 이름이라 겹쳐도 된다. 사용자 식별은 user_id 가 한다."""
        self.other.nickname, self.other.nicknameKey = 'runner', 'runner'
        self.other.save()

        res = self.client.post(NICKNAME, {'nickname': 'runner'})

        self.assertEqual(res.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.nickname, 'runner')

    def test_대소문자가_달라도_그대로_저장한다(self):
        """키는 소문자로 접지만 화면에 보이는 값은 친 그대로 남긴다."""
        res = self.client.post(NICKNAME, {'nickname': 'RUNNER'})

        self.assertEqual(res.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.nickname, 'RUNNER')
        self.assertEqual(self.profile.nicknameKey, 'runner')

    def test_전각_문자는_표준형으로_바뀐다(self):
        """정규화(NFKC)가 검사보다 먼저 돌아야 통과한다.

        순서가 뒤바뀌면 '쓸 수 없는 문자'로 400 이 떨어져 안내가 엉뚱해진다.
        """
        res = self.client.post(NICKNAME, {'nickname': 'ｒｕｎｎｅｒ'})

        self.assertEqual(res.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.nickname, 'runner')

    def test_자기_닉네임은_다시_저장해도_된다(self):
        self.client.post(NICKNAME, {'nickname': 'runner'})
        res = self.client.post(NICKNAME, {'nickname': 'runner'})
        self.assertEqual(res.status_code, 200)

    def test_규칙에_어긋나면_400(self):
        cases = ['a', 'a' * 13, 'run ner', 'run_ner', 'runner!', '🏃running',
                 '관리자김', 'highfive팀', '', '   ']
        for value in cases:
            with self.subTest(value=value):
                res = self.client.post(NICKNAME, {'nickname': value})
                self.assertEqual(res.status_code, 400, value)

    def test_닉네임은_12자까지_허용한다(self):
        value = '가' * 12

        res = self.client.post(NICKNAME, {'nickname': value})

        self.assertEqual(res.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.nickname, value)

    def test_인증_없이는_못_바꾼다(self):
        self.client.credentials()
        res = self.client.post(NICKNAME, {'nickname': 'runner'})
        self.assertEqual(res.status_code, 401)

    def test_프레임워크가_내는_오류도_같은_형식이다(self):
        """DRF 기본은 `{'detail': ...}` 라 앱이 형식 두 가지를 다뤄야 했다.

        config.api.exception_handler 가 이걸 막는다.
        """
        self.client.credentials()
        res = self.client.post(NICKNAME, {'nickname': 'runner'})

        self.assertIn('s', res.data)
        self.assertFalse(res.data['s'])
        self.assertTrue(res.data['msg'])
