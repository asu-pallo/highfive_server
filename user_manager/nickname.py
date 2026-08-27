"""닉네임 정규화와 검증.

닉네임은 **중복을 허용**한다. 여기서 만드는 키는 중복 판정이 아니라 검색·정렬용이다.
"""

import re
import unicodedata

MIN_LENGTH = 2
MAX_LENGTH = 12

# 한글(완성형), 영문, 숫자만 허용한다. **공백은 쓸 수 없다.**
#
# 공백을 허용하면 `달리는사람` 과 `달리는 사람` 이 서로 다른 닉네임으로 공존하는데,
# 피드에서 이 둘을 구분할 수 있는 사람은 없다. 대소문자를 이미 같은 것으로 보고
# 있으니(`Runner` = `runner`) 공백만 예외로 두면 앞뒤가 맞지 않는다.
#
# 앞뒤 공백은 제거한다. IME 가 붙이는 공백 때문에 거절당하면 이유를 알기 어렵다.
# 밑줄·탭·줄바꿈·이모지도 막는다.
#
# 낱자 자모(`ㅋㅋㅋ`)도 걸린다. NFKC 를 거치면 호환 자모가 조합용 자모로 바뀌어
# `가-힣` 범위에 들지 않기 때문이다. 의도한 동작이다 — 허용하려면 여기를 넓힌다.
_ALLOWED = re.compile(r'^[가-힣a-zA-Z0-9]+$')

# 부분 일치로 막는다. 이 단어가 들어가면 운영 주체로 오인될 수 있다.
_RESERVED = (
    'highfive',
    'hifive',
    '하이파이브',
    '운영자',
    '관리자',
    'admin',
    'official',
)


class NicknameError(ValueError):
    """닉네임이 규칙에 맞지 않는다. 메시지를 그대로 사용자에게 보여준다."""


def normalize(raw: str) -> str:
    """화면에 보일 값. **검사보다 먼저** 거친다.

    NFKC 로 전각/호환 문자를 표준형으로 모은다. `ｒｕｎｎｅｒ` 는 `runner` 가 된다.

    정규화를 검사 뒤에 두면 전각 입력이 "쓸 수 없는 문자" 로 거절돼서, 전각 IME 를
    쓰는 사용자에게는 영문을 쳤는데 안 된다고 나온다. 먼저 모아 두면 통과하고,
    같은 이름이 이미 있으면 중복으로 걸린다 — 안내가 정확해진다.

    저장되는 값도 늘 표준형이라, 눈으로 구별되지 않는 닉네임이 나란히 존재할 수 없다.

    앞뒤 공백만 제거한다. 가운데 공백은 없애지 않고 [validate] 에서 거절한다 —
    조용히 붙여 버리면 사용자가 친 것과 다른 이름이 저장된다.
    """
    return unicodedata.normalize('NFKC', raw or '').strip()


def to_key(nickname: str) -> str:
    """중복 판정용 키. 이미 정규화된 값을 받아 대소문자만 지운다."""
    return nickname.casefold()


def validate(raw: str) -> tuple[str, str]:
    """검사해서 (표시용, 키) 를 돌려준다. 어긋나면 [NicknameError].

    길이는 정규화 전 값이 아니라 **화면에 보일 값** 기준으로 센다.
    """
    nickname = normalize(raw)

    if not nickname:
        raise NicknameError('닉네임을 입력해 주세요.')

    if len(nickname) < MIN_LENGTH or len(nickname) > MAX_LENGTH:
        raise NicknameError(f'닉네임은 {MIN_LENGTH}~{MAX_LENGTH}자로 입력해 주세요.')

    if not _ALLOWED.match(nickname):
        raise NicknameError('닉네임에는 한글, 영문, 숫자만 쓸 수 있어요. (공백 불가)')

    key = to_key(nickname)

    # 금지어는 키 기준으로 본다. `Ａdmin`, `ADMIN` 도 같이 걸린다.
    for word in _RESERVED:
        if word in key:
            raise NicknameError('사용할 수 없는 닉네임이에요.')

    return nickname, key
