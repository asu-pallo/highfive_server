"""Firebase ID 토큰 검증.

앱은 Firebase Auth 로 구글·애플 로그인을 한 뒤 **Firebase ID 토큰**을 서버로 보낸다.
서버는 그 토큰이 우리 Firebase 프로젝트가 발급한 것인지, 만료되지 않았는지를
확인한다. 제공자(구글/애플)별로 검증 코드를 따로 두지 않아도 되는 게 이 방식의 이점이다.

**클라이언트가 보낸 uid 를 그대로 믿으면 안 된다.** 남의 uid 를 적어 보내면 그 계정으로
로그인된다. 반드시 토큰을 검증해서 나온 uid 만 쓴다.
"""

from dataclasses import dataclass

import firebase_admin
from django.conf import settings
from firebase_admin import auth, credentials

_app: firebase_admin.App | None = None


class InvalidToken(Exception):
    """토큰이 위조됐거나 만료됐거나 다른 프로젝트 것이다."""


class NotConfigured(Exception):
    """서비스 계정 키가 없어 검증할 수 없다. 서버 설정 문제다."""


@dataclass(frozen=True)
class SocialAccount:
    """검증을 통과한 토큰에서 뽑은 것."""

    uid: str
    provider: str  # 'google' | 'apple' | 그 외 firebase 가 알려주는 값
    email: str | None


def _ensure_app() -> firebase_admin.App:
    global _app
    if _app is not None:
        return _app

    path = settings.FIREBASE_CREDENTIALS
    if not path:
        raise NotConfigured(
            'FIREBASE_CREDENTIALS 가 설정되지 않았다. '
            'Firebase 콘솔 → 프로젝트 설정 → 서비스 계정에서 키를 내려받아 '
            '.env 에 경로를 넣어야 한다.'
        )

    # 이름을 주지 않으면 기본 앱이 되어, 다른 곳에서 initialize_app 을 또 부르면 충돌한다.
    _app = firebase_admin.initialize_app(credentials.Certificate(str(path)), name='highfive')
    return _app


def _provider_of(claims: dict) -> str:
    """어느 제공자로 로그인했는지.

    `firebase.sign_in_provider` 는 `google.com` / `apple.com` 형태로 온다.
    우리 DB 는 `google` / `apple` 로 저장하므로 도메인을 떼어 낸다.
    """
    raw = (claims.get('firebase') or {}).get('sign_in_provider', '')
    return raw.removesuffix('.com')


def verify_id_token(id_token: str) -> SocialAccount:
    """토큰을 검증하고 계정 정보를 돌려준다. 실패하면 [InvalidToken]."""
    if not id_token:
        raise InvalidToken('idToken 이 비어 있다.')

    try:
        claims = auth.verify_id_token(id_token, app=_ensure_app())
    except NotConfigured:
        raise
    except Exception as error:
        # firebase_admin 은 만료·서명 불일치·잘못된 형식을 제각각 다른 예외로 낸다.
        # 어느 쪽이든 클라이언트에게는 "다시 로그인" 하나로 충분하다.
        raise InvalidToken(str(error)) from error

    return SocialAccount(
        uid=claims['uid'],
        provider=_provider_of(claims),
        email=claims.get('email'),
    )
