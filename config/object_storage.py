"""객체 저장소(MinIO/S3) 공용 헬퍼.

앱이 파일을 직접 주고받는 주소를 만드는 곳이 두 군데(운동 원본, 프로필 이미지)라
개발용 호스트 추론 규칙을 한곳에 둔다. 규칙이 갈라지면 실단말에서만 깨진다.
"""

from urllib.parse import urlparse

import boto3
from django.conf import settings


def public_object_endpoint(request) -> str | None:
    """앱이 접근할 수 있는 객체 저장소 주소.

    개발에서는 API 요청이 들어온 호스트를 그대로 재사용한다. 실단말은 맥의 LAN 주소로,
    에뮬레이터는 adb reverse 로 들어오는데 그 주소를 서버가 미리 알 수 없기 때문이다.
    """
    if not settings.DEBUG:
        return settings.S3_PUBLIC_ENDPOINT_URL

    hostname = urlparse(f'//{request.get_host()}').hostname
    if not hostname:
        return settings.S3_PUBLIC_ENDPOINT_URL
    return f'{request.scheme}://{hostname}:9000'


def build_client(endpoint: str | None):
    return boto3.client(
        's3',
        endpoint_url=endpoint,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
    )


def public_asset_url(request, object_key: str) -> str:
    """공개 버킷 객체의 서명 없는 URL.

    서명을 붙이지 않으므로 URL 이 안 변하고, 앱 이미지 캐시와 CDN 이 그대로 동작한다.
    대신 키를 아는 사람은 로그인 없이 볼 수 있으므로 공개해도 되는 파일만 올린다.
    """
    # 개발에서는 운동 상세 다운로드와 똑같이 API 요청 호스트를 쓴다. 로컬 설정에
    # localhost CDN 주소가 남아 있어도 실단말에는 Mac의 LAN 주소를 내려줘야 한다.
    # 운영에서만 고정 CDN 주소가 요청 호스트보다 우선한다.
    if not settings.DEBUG and settings.PUBLIC_ASSET_BASE_URL:
        return f'{settings.PUBLIC_ASSET_BASE_URL.rstrip("/")}/{object_key}'

    endpoint = public_object_endpoint(request)
    return f'{endpoint.rstrip("/")}/{settings.S3_PUBLIC_BUCKET_NAME}/{object_key}'
