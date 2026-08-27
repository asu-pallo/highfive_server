from django.urls import path

from . import views

urlpatterns = [
    # 로그인은 둘뿐이다. 별도의 회원가입 API 도, 기기 정보 API 도 없다.
    # 처음이면 signin 이 계정을 만들고, 기기 정보·FCM 토큰은 두 로그인이 모두 갱신한다.
    path('auth/signin/', views.sign_in),
    path('auth/autologin/', views.auto_login),

    path('users/nickname/', views.set_nickname),
    path('users/nickname/check/', views.check_nickname),
]
