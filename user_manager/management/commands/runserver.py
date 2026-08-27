"""개발 서버를 실기기에서도 붙을 수 있게 띄운다.

Django 기본 `runserver` 는 두 가지가 불편하다.

1. **기본이 `127.0.0.1`** 이라 맥 안에서만 열린다. 아이폰·갤럭시로 테스트하려면
   매번 `runserver 0.0.0.0:8000` 을 쳐야 한다.
2. **주소만 주면 에러가 난다.** `runserver 0.0.0.0` 은
   `"0.0.0.0" is not a valid port number or address:port pair` 로 죽는다.
   주소 뒤에 포트가 반드시 와야 하는 형식이라서다.

여기서 둘 다 없앤다. 아래 셋이 모두 같은 동작이 된다.

    manage.py runserver
    manage.py runserver 0.0.0.0
    manage.py runserver 0.0.0.0:8000

`staticfiles` 의 runserver 를 상속해야 개발 중 정적 파일 서빙이 유지된다.
"""

from django.conf import settings
from django.contrib.staticfiles.management.commands.runserver import (
    Command as StaticfilesRunserver,
)


class Command(StaticfilesRunserver):
    # 개발 중에만 모든 인터페이스에 연다. DEBUG 가 꺼져 있으면 기본값 그대로 둔다 —
    # 운영에서 runserver 를 쓸 일은 없지만, 실수로 밖에 열리는 건 막는다.
    default_addr = '0.0.0.0' if settings.DEBUG else '127.0.0.1'

    def handle(self, *args, **options):
        options['addrport'] = self._with_port(options.get('addrport'))
        return super().handle(*args, **options)

    def _with_port(self, addrport):
        """주소만 준 경우 기본 포트를 붙인다.

        `8000`(포트만)과 `0.0.0.0:8000`(둘 다)은 그대로 둔다.
        """
        if not addrport:
            return addrport
        if ':' in addrport or addrport.isdigit():
            return addrport
        return f'{addrport}:{self.default_port}'
