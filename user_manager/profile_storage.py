"""프로필 이미지 객체 저장.

운동 원본과 달리 **읽기 공개 버킷**에 서버가 직접 올린다. 파일이 작고(수십 KB),
서버가 어차피 바이트를 검사·변환해야 해서 Presigned 왕복을 둘 이유가 없다.
생성 API 를 못 부르고 끝났을 때 남는 고아 객체도 생기지 않는다.

공개 버킷 설정이 없으면 Django 기본 저장소로 떨어진다. 테스트와 S3 없이 띄운
로컬 개발이 그대로 동작하게 하려는 것이다.
"""

import logging
from uuid import uuid4

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from config.object_storage import build_client, public_asset_url, public_object_endpoint

logger = logging.getLogger(__name__)

CONTENT_TYPE = 'image/webp'

# 키에 UUID 가 들어가 내용이 바뀌면 키도 바뀐다. 그래서 영구 캐시로 둬도 안전하다.
CACHE_CONTROL = 'public, max-age=31536000, immutable'


def save_variants(variants: dict[str, bytes]) -> dict[str, str]:
    """규격별 이미지를 한 폴더에 올리고 이름→객체 키를 돌려준다."""
    folder = f'profiles/{uuid4().hex}'
    keys = {name: f'{folder}/{name}.webp' for name in variants}

    if settings.S3_PUBLIC_BUCKET_NAME:
        client = build_client(settings.S3_ENDPOINT_URL)
        for name, content in variants.items():
            client.put_object(
                Bucket=settings.S3_PUBLIC_BUCKET_NAME,
                Key=keys[name],
                Body=content,
                ContentType=CONTENT_TYPE,
                CacheControl=CACHE_CONTROL,
            )
    else:
        for name, content in variants.items():
            default_storage.save(keys[name], ContentFile(content))

    return keys


def delete_objects(object_keys: list[str]) -> None:
    """이전 이미지를 지운다. 실패해도 요청을 실패시키지 않는다.

    새 이미지가 이미 저장되고 프로필도 갱신된 뒤에 부르므로, 여기서 예외를 올리면
    사용자에게는 '업로드 실패'로 보이면서 실제로는 바뀌어 있는 상태가 된다.
    """
    keys = [key for key in object_keys if key]
    if not keys:
        return

    try:
        if settings.S3_PUBLIC_BUCKET_NAME:
            client = build_client(settings.S3_ENDPOINT_URL)
            client.delete_objects(
                Bucket=settings.S3_PUBLIC_BUCKET_NAME,
                Delete={'Objects': [{'Key': key} for key in keys]},
            )
        else:
            for key in keys:
                default_storage.delete(key)
    except Exception:  # noqa: BLE001 - 정리 실패는 기록만 하고 넘어간다.
        logger.warning('이전 프로필 이미지를 지우지 못했습니다: %s', keys, exc_info=True)


def profile_image_url(request, object_key: str) -> str:
    if settings.S3_PUBLIC_BUCKET_NAME and (
        settings.PUBLIC_ASSET_BASE_URL or public_object_endpoint(request)
    ):
        return public_asset_url(request, object_key)
    return request.build_absolute_uri(default_storage.url(object_key))
