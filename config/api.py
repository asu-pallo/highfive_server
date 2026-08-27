"""API 공통 처리."""

from rest_framework.views import exception_handler as drf_exception_handler


def exception_handler(exc, context):
    """DRF 가 자동으로 내는 오류도 `{'s': False, 'msg': ...}` 로 맞춘다.

    뷰에서 직접 만든 응답만 이 형태였고, 인증 실패·메서드 오류·throttle 처럼
    프레임워크가 가로채는 것들은 `{'detail': ...}` 로 나갔다. 앱이 오류 형식을 두 가지로
    다뤄야 하는데, 어느 쪽이 올지는 서버 사정이라 클라이언트가 알 방법이 없다.

    `detail` 도 함께 남긴다. 기존 형태를 보고 짠 코드가 있어도 깨지지 않는다.
    """
    response = drf_exception_handler(exc, context)
    if response is None:
        # DRF 가 다루지 않는 예외. Django 기본 500 처리로 넘긴다.
        return None

    detail = response.data
    if isinstance(detail, dict) and 'detail' in detail:
        message = str(detail['detail'])
    elif isinstance(detail, list) and detail:
        message = str(detail[0])
    else:
        # 필드별 검증 오류처럼 구조가 있는 경우. 통째로 넘기면 사용자에게 보여줄 수
        # 없으니 첫 메시지만 뽑는다. 원본은 아래 `detail` 에 남는다.
        message = _first_message(detail)

    response.data = {'s': False, 'msg': message, 'detail': detail}
    return response


def _first_message(data) -> str:
    if isinstance(data, dict):
        for value in data.values():
            return _first_message(value)
    if isinstance(data, (list, tuple)) and data:
        return _first_message(data[0])
    return str(data) if data else '요청을 처리하지 못했습니다.'
