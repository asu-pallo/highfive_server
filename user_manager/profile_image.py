"""프로필 이미지 검증과 가공.

앱이 보낸 원본을 그대로 저장하지 않는다. 이유가 두 가지다.

1. **EXIF 에 GPS 가 들어 있다.** 러닝 앱에서 프로필 사진은 남에게 보이는 파일이라,
   촬영 위치가 그대로 새어 나가면 집 주소가 노출된다. 좌표를 이렇게까지 조심해서
   다루면서 사진으로 흘리면 아무 의미가 없다.
2. 단말·앱마다 크기와 포맷이 제각각이다. 화면이 쓸 규격을 서버가 정해서 저장해야
   피드가 3MB 짜리 원본을 20장 받는 일이 없다.

`Image.frombytes` 로 픽셀만 새 이미지에 옮겨 담으므로 EXIF·ICC 등 원본 메타데이터는
결과물에 남지 않는다.
"""

from io import BytesIO

from django.conf import settings
from PIL import Image, ImageOps, UnidentifiedImageError


class ProfileImageError(ValueError):
    """사용자에게 그대로 보여줄 수 있는 실패."""


def render_variants(raw: bytes) -> dict[str, bytes]:
    """원본 바이트를 규격별 WebP 로 변환한다. 키는 `PROFILE_IMAGE_SIZES` 의 이름이다."""
    if not raw:
        return _fail('이미지가 비어 있습니다.')
    if len(raw) > settings.PROFILE_IMAGE_MAX_BYTES:
        limit = settings.PROFILE_IMAGE_MAX_BYTES // (1024 * 1024)
        return _fail(f'이미지는 {limit}MB 이하만 올릴 수 있습니다.')

    source = _decode(raw)
    try:
        # 아이폰 세로 사진은 픽셀이 눕고 EXIF 방향으로만 세워져 있다. 메타데이터를
        # 버릴 것이므로 회전을 픽셀에 먼저 반영해야 결과가 눕지 않는다.
        source = ImageOps.exif_transpose(source)
        source = _flatten(source)

        return {
            name: _encode(source, size)
            for name, size in settings.PROFILE_IMAGE_SIZES.items()
        }
    finally:
        source.close()


def _decode(raw: bytes) -> Image.Image:
    try:
        image = Image.open(BytesIO(raw))
        image.load()  # 헤더만 그럴듯한 파일은 여기서 걸린다.
        return image
    except Image.DecompressionBombError:
        return _fail('이미지 크기가 너무 큽니다.')
    except (UnidentifiedImageError, OSError, ValueError):
        return _fail('이미지 파일이 아니거나 열 수 없습니다.')


def _flatten(image: Image.Image) -> Image.Image:
    """투명 배경을 흰색으로 채운다. 프로필은 항상 불투명 한 장으로 다룬다."""
    if image.mode in ('RGBA', 'LA') or (image.mode == 'P' and 'transparency' in image.info):
        rgba = image.convert('RGBA')
        canvas = Image.new('RGB', rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    return image.convert('RGB')


def _encode(source: Image.Image, size: int) -> bytes:
    # fit 은 가운데를 정사각으로 잘라 내면서 리사이즈까지 한 번에 한다.
    square = ImageOps.fit(source, (size, size), method=Image.LANCZOS, centering=(0.5, 0.5))
    try:
        # 픽셀만 옮겨 담아 원본 메타데이터(EXIF·GPS·ICC)를 확실히 끊는다.
        clean = Image.frombytes('RGB', square.size, square.tobytes())
    finally:
        square.close()

    buffer = BytesIO()
    try:
        clean.save(buffer, format='WEBP', quality=82, method=4)
    finally:
        clean.close()
    return buffer.getvalue()


def _fail(message: str):
    raise ProfileImageError(message)
