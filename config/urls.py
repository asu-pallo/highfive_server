"""HighFive API 라우팅.

앱이 부르는 엔드포인트는 전부 `/api/` 아래에 둔다. 나중에 버전을 나눠야 하면
`/api/v1/` 로 한 겹 더 감싸면 되고, 그전까지는 경로를 단순하게 유지한다.
"""

from django.contrib import admin
from django.http import JsonResponse
from django.urls import path


def health(_request):
    """배포·로드밸런서가 찔러 보는 용도. 인증 없이 열려 있다."""
    return JsonResponse({'s': True})


urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/health/', health),
]
