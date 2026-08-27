# CLAUDE.md

이 파일은 Claude Code가 이 저장소에서 작업할 때 참고하는 가이드다.

## 프로젝트 개요

**HighFive 서버** — 달리기 기록 기반 소셜 러닝 앱의 백엔드.

핵심 기능: 달리기 중 **같은 시간, 같은 장소에 있었던 사용자끼리 "하이파이브"로 표기**한다.
경로가 겹친 지점에 박수 아이콘을 표시한다.

## 경로

| 대상 | 경로 |
|---|---|
| **HighFive 루트** | `/Users/asu/Documents/pallo/highfive` |
| **HighFive 서버 (이 저장소)** | `/Users/asu/Documents/pallo/highfive/server` |
| **HighFive 앱** (Flutter) | `/Users/asu/Documents/pallo/highfive/app` |

앱은 이 서버와 짝을 이루는 클라이언트다.
API 스펙이나 데이터 형식이 궁금하면 **추측하지 말고 앱 코드를 직접 읽어서 확인할 것.**

## 현재 상태

**사용자·인증까지 되어 있다.** 러닝 기록과 하이파이브 판정은 아직 없다.

- `user_manager` — Profile 모델 + 소셜/자동 로그인 + 닉네임 API. 테스트 36개
- 러닝 기록 테이블 없음. 앱은 아직 건강 데이터를 **기기 안에서만** 다룬다
- DB 는 개발용 SQLite. 운영 DB 는 미정
- git 저장소다(앱과 별개). 커밋은 유저가 직접 관리한다

## 기술 스택

열품타(`pi_django`)와 같은 계열로 맞췄다. 버전은 `requirements.txt` 에 고정돼 있다.

| 항목 | 선택 |
|---|---|
| 프레임워크 | Django 5.2 |
| API | Django REST Framework |
| 인증 | SimpleJWT (access 30분 / refresh 90일, 회전 + 블랙리스트) |
| 설정 주입 | python-dotenv (`.env`) |
| DB | SQLite (개발) |

> **열품타와 다른 점 — JWT 수명.**
> 열품타는 access 토큰을 36,500일(100년)로 두고 refresh 를 쓰지 않아, 토큰이 유출돼도
> 무효화할 방법이 없다. 여기서는 짧은 access + 회전되는 refresh 로 간다.

## 구조

```
server/
├─ manage.py
├─ requirements.txt
├─ .env            # 커밋 안 됨
├─ .env.example    # 형식만 커밋
├─ secrets/        # Firebase 서비스 계정 키. 커밋 안 됨
├─ config/         # 프로젝트 설정 패키지 (열품타의 pi/ 에 해당)
│  ├─ settings.py
│  ├─ api.py       # DRF 예외를 {'s': false, 'msg': ...} 로 통일
│  ├─ urls.py      # 앱이 부르는 엔드포인트는 전부 /api/ 아래
│  ├─ asgi.py
│  └─ wsgi.py
└─ user_manager/   # 사용자·인증
   ├─ models.py       Profile
   ├─ views.py        signin · autologin · nickname
   ├─ firebase.py     ID 토큰 검증
   ├─ nickname.py     정규화·검증 규칙
   └─ management/commands/runserver.py   # 아래 참고
```

> **`runserver` 를 덮어썼다.** 기본이 `127.0.0.1` 이라 실기기가 못 붙고,
> `runserver 0.0.0.0` 은 포트가 없다고 에러가 난다. 그래서 기본 주소를 `0.0.0.0` 으로
> 바꾸고 주소만 줘도 포트를 붙이게 했다. `DEBUG=False` 면 `127.0.0.1` 로 되돌아간다.
>
> ⚠️ 이 때문에 **`INSTALLED_APPS` 에서 `user_manager` 가 맨 앞**에 있다. Django 는
> 목록을 뒤에서부터 훑으며 관리 명령을 등록해서, 뒤에 두면 `staticfiles` 의 것이
> 우리 걸 덮어쓴다.

## 실행

처음 받았을 때:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # DJANGO_SECRET_KEY 를 채운다
.venv/bin/python manage.py migrate
```

개발 서버. **기본이 `0.0.0.0:8000`** 이라 그냥 띄우면 실기기에서도 붙는다:

```bash
.venv/bin/python manage.py runserver
```

앱은 `app/lib/src/core/config/api_config.dart` 의 `_devUrl` 을 본다. 실기기로 테스트할
때는 거기에 **맥의 LAN 주소**가 들어가 있어야 한다(와이파이가 바뀌면 달라진다).

확인: `GET /api/health/` → `{"s": true}`

## 규약

- 응답은 열품타와 같이 `{'s': bool, ...}` 형태를 기본으로 한다.
- DRF 기본 권한이 `IsAuthenticated` 다. **공개 API 는 뷰에서 `AllowAny` 를 명시**한다.
- 시각은 전부 UTC 로 저장한다. 앱도 UTC 로 보낸다.
- 모델 필드는 **camelCase** 로 쓴다(열품타와 맞춤). Django 관례와 다르지만 두 서버를
  오갈 때 헷갈리지 않는 쪽을 택했고, API JSON 도 camelCase 라 변환 계층이 없다.

## API

| 엔드포인트 | 권한 | 하는 일 |
|---|---|---|
| `GET /api/health/` | 공개 | 헬스체크 |
| `POST /api/auth/signin/` | 공개 | **소셜 로그인.** 가입을 겸한다 |
| `POST /api/auth/autologin/` | 공개 | **자동 로그인.** refresh 토큰으로 세션을 잇는다 |
| `POST /api/users/nickname/` | 인증 | 닉네임 설정·변경 |

**로그인은 이 둘뿐이다. 별도의 회원가입 API 도, 기기 정보 API 도 없다.**

소셜 로그인은 클라이언트가 "처음인지"를 알 수 없어서 `signin` 이 uid 로 판단해 없으면
만든다. 앱은 응답 `profile.nickname`이 비었는지로 닉네임 설정 필요 여부를 판단한다.

```
앱 실행 → 저장된 refresh 있음? ─ 예 → POST /auth/autologin/
                              └ 아니오 → 소셜 로그인 → POST /auth/signin/
                                            ↓
                                   profile.nickname 이 비었으면 닉네임 화면
```

### 기기 정보·FCM 토큰은 로그인이 갱신한다

두 로그인 API 모두 아래를 **선택 항목**으로 받는다. 안 보내면 이전 값을 유지한다.

```
deviceType  deviceModel  osVersion  appVersion  timezone  fcmToken
```

갱신 시점을 "로그인할 때" 하나로 묶었다. access 가 30분이라 앱은 자주 `autologin` 을
부르게 되고, 그때마다 최신 값이 올라온다. 갱신 전용 API 를 따로 두면 앱이 두 군데서
같은 일을 하게 된다.

`autologin` 은 access 만료 재발급도 겸한다. **만료된 access 로는 부를 수 없어서
`AllowAny`** 이고, 인증은 본문의 refresh 토큰 자체가 한다.

`ROTATE_REFRESH_TOKENS` 라 **refresh 도 새것으로 바뀐다.** 앱은 응답의 refresh 로
저장값을 덮어써야 한다. 예전 것을 계속 쓰면 블랙리스트에 걸려 다음부터 막힌다.

> ⚠️ Profile 한 줄에 덮어쓰므로 **마지막에 로그인한 기기만 남는다.** 한 계정이 아이폰과
> 갤럭시를 같이 쓰면 먼저 쓰던 기기로는 푸시가 가지 않는다. 다기기를 제대로 다뤄야 할
> 때 Device 테이블로 분리한다.

## 인증

앱이 Firebase Auth 로 구글·애플 로그인 → **Firebase ID 토큰**을 `signin` 에 보냄 →
서버가 `firebase-admin` 으로 검증. 제공자별 검증 코드를 따로 두지 않아도 된다.

**클라이언트가 보낸 uid 를 그대로 믿지 않는다.** 반드시 토큰 검증을 거친 uid만 쓴다
(열품타는 이 검증이 없다).

서비스 계정 키는 `secrets/` 에 두고 `.env` 의 `FIREBASE_CREDENTIALS` 로 경로를 준다.
키가 없으면 `signin` 만 500 과 함께 안내 메시지를 낸다. 나머지 API 는 정상 동작한다.

## Profile 컬럼

```
nickname  nicknameKey  loginProvider
deviceType  deviceModel  osVersion  appVersion  timezone  fcmToken
createdAt  lastLoginAt
```

테이블 이름은 Django 기본 규칙에 따라 `user_manager_profile` — 열품타와 같다.
필드는 camelCase 로 맞췄다.

## 닉네임

- `Profile.nickname` — 화면에 보이는 값
- `Profile.nicknameKey` — 중복 판정용. **unique 는 여기에만** 건다
- 앞뒤 공백을 제거한 뒤 **2~20자**만 허용한다
- 한글 완성형·영문·숫자와 글자 사이 공백을 허용한다. 밑줄·이모지는 허용하지 않는다
- 연속된 중간 공백은 한 칸으로 합쳐 공백 개수만 다른 중복 닉네임을 막는다

키는 NFKC 정규화 + `casefold()` 다. `Runner` / `runner` / `ｒｕｎｎｅｒ` 가 모두 같은
이름으로 취급된다. **정규화가 문자 검사보다 먼저** 돌아야 한다 — 순서가 바뀌면 전각
입력이 "쓸 수 없는 문자" 로 거절돼 안내가 엉뚱해진다.

중복은 조회로 막지 않고 **DB unique 제약이 걸리는 걸 잡아서** 처리한다(409).
`check` API 는 편의일 뿐이고 최종 판정은 저장 시점에 한다 — 조회와 저장 사이에 다른
요청이 끼어들 수 있기 때문이다(열품타가 이 경쟁에 열려 있다).

## 문서

- [`../highfive_guide.md`](../highfive_guide.md) — **설계 가이드 (앱·서버 공통)**

앱·서버 공통의 설계 결정과 미결정 사항이 이 문서에 모인다. **루트에 있다.**
날짜로 나누지 않고 **계속 다듬어 나가는 문서**이므로, 설계가 바뀌면 새 파일을 만들지 말고 이 문서를 갱신할 것.
설계 관련 작업 전에 먼저 읽을 것.

**서버에만 해당하는 내용**이 필요해지면 이 폴더에 별도 문서를 만든다.
공통 내용은 루트 가이드에 두고 중복해서 적지 않는다.

### 주요 미결정 사항

1. 좌표 단순화 알고리즘 · 허용 오차
2. 하이파이브 정밀 판정 방식
3. 데이터 변환기 필드 매핑 (실제 데이터 확인 후)
4. 지역 코드 체계 — 법정동 vs 행정동
5. 지원 데이터 소스 확정 범위

## 파일 작성 규칙

- **가이드·설계 문서** — 날짜 없이 고정 파일명. 새로 만들지 말고 기존 문서를 갱신
- **일회성 산출물** (분석 결과, 리포트 등) — `YYYYMMDD_HHMM_파일명` 형식 (생성순 정렬)
- 백업 파일(`.backup_*`) 생성 금지
- 한국어로 응답
