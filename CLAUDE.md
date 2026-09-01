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

**사용자·인증, 운동 원본 동기화와 H3 경로 인덱스 저장까지 되어 있다.** 하이파이브는
별도 테이블이나 Redis 캐시 없이 피드 10건을 조회할 때 최신 H3 구간으로 일괄 계산한다.

- `user_manager` — Profile 모델 + 소셜/자동 로그인 + 닉네임 API
- `workout_manager` — 운동별 준비 API + Presigned POST 객체 직접 업로드 + 완료 검증,
  H3 Resolution 11 연속 체류 구간 저장 + 피드 조회 시 하이파이브 실시간 판정 + 서버 시각 기반
  10건 커서 다운로드와 상세 API
- 앱 피드는 서버에서 내려받아 Drift에 캐시한 운동만 표시한다
- DB는 Docker PostgreSQL 17로 전환했다. 객체 저장소는 로컬 MinIO다
- git 저장소다(앱과 별개). 커밋은 유저가 직접 관리한다

## 기술 스택

열품타(`pi_django`)와 같은 계열로 맞췄다. 버전은 `requirements.txt` 에 고정돼 있다.

| 항목 | 선택 |
|---|---|
| 프레임워크 | Django 5.2 |
| API | Django REST Framework |
| 인증 | SimpleJWT (access 30분 / refresh 90일, 회전 + 블랙리스트) |
| 설정 주입 | python-dotenv (`.env`) |
| DB | PostgreSQL 17 (로컬 Docker, 운영 AWS RDS) |
| 객체 저장소 | Django Storage + MinIO (개발), 비공개 S3 (운영) |

### PostgreSQL 선택 방향

- 로컬은 Docker PostgreSQL, 운영은 AWS RDS PostgreSQL을 사용한다.
- H3 세그먼트의 체류 시간은 `DateTimeRangeField`/`tstzrange`로 저장한다.
- 하이파이브 후보는 동일 H3 셀과 `period && currentPeriod` 조건으로 조회한다.
- `btree_gist`와 GiST 복합 인덱스는 실제 실행 계획과 부하 테스트 후 확정한다.
- 향후 정밀 거리·지역 통계가 필요할 때만 PostGIS를 추가한다. 초기 H3 구현에는 넣지 않는다.
- GPS·심박 원본은 PostgreSQL에 배열 행으로 넣지 않고 기존처럼 비공개 객체 저장소에 둔다.
- 기존 SQLite 개발 데이터는 이관하지 않았고 현재 모델 기준 초기 migration을 새로 만들었다.

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
├─ user_manager/   # 사용자·인증
   ├─ models.py       Profile
   ├─ views.py        signin · autologin · nickname
   ├─ firebase.py     ID 토큰 검증
   ├─ nickname.py     정규화·검증 규칙
   └─ management/commands/runserver.py   # 아래 참고
└─ workout_manager/ # 운동 저장·상세 파일·증분 동기화
   ├─ models.py       Workout · WorkoutDetail · SpatialIndexVersion · TrajectorySegment
   ├─ spatial_index.py H3 Resolution 11 변환·통과 셀 보완·구간 저장
   ├─ high_five.py    동일 H3 셀·시간 겹침 후보 조회와 상대 사용자별 대표 판정
   ├─ serializers.py  업로드 검증·다운로드 응답
   └─ views.py        stateless prepare/create upload · download_workouts · detail
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

개발 환경은 `manage.py`와 같은 위치의 `dev.py`로 함께 관리한다. **기본이
`0.0.0.0:8000`**이라 실기기에서도 붙는다.

```bash
python dev.py up       # PostgreSQL·MinIO·migrate 후 Django 실행
python dev.py status   # 서버와 컨테이너 상태
python dev.py logs     # Docker 로그
python dev.py db       # 컨테이너의 psql 콘솔
python dev.py down     # 종료, 데이터 유지
python dev.py reset    # 로컬 PostgreSQL 스키마·MinIO 초기화 후 다시 up
```

Android 실단말의 MinIO 직접 다운로드는 `.env`의
`S3_PUBLIC_ENDPOINT_URL=http://127.0.0.1:9000`을 사용한다. `dev.py up`은 연결된 단말에
`adb reverse tcp:9000 tcp:9000`을 설정하고 `down`은 제거한다. MinIO API는 LAN에 직접
공개하지 않는다.

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
| `POST /api/workouts/upload/prepare/` | 인증 | 메타데이터·해시·크기 검증 후 필요한 경우 5분 Presigned POST 폼 발급 |
| `POST /api/workouts/upload/create/` | 인증 | 경로·심박 S3 객체를 각각 검증한 후 운동·상세·H3 생성 |
| `GET /api/workouts/` | 인증 | 변경된 내 운동을 고정 스냅샷에서 10건씩 다운로드 |
| `POST /api/workouts/high-fives/` | 인증 | 현재 피드 운동 ID 최대 10개의 HighFive 요약을 실시간 일괄 계산 |
| `GET /api/workouts/<id>/detail/` | 인증 | 소유권 확인 후 GPS·심박 원본의 5분 다운로드 URL 반환 |
| `GET /api/workouts/<id>/h3/` | 인증 | 소유권 확인 후 현재 H3 구간·육각형 경계 반환 |

운동 목록은 `since`, `snapshot`, `cursor`를 사용한다. 정렬 기준은
`(updatedAt DESC, id DESC)`이며 커서는 서버가 발급한 불투명 문자열이다. 앱은 첫
페이지의 `serverTime`을 다음 페이지의 `snapshot`으로 유지하고, 마지막 페이지까지
저장한 뒤에만 다음 동기화의 `since`로 기록한다.

상세 API는 원본 JSON이나 객체 path만 반환하지 않는다. 환경별 객체 저장소/CDN 주소와
서명이 포함된 완성된 `downloadUrl`, `expiresInSeconds`, `contentHash`, `fileSize`를
반환한다. 앱은 저장소 인증키나 base URL을 갖지 않고 URL에서 파일을 직접 다운로드한다.

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
- `Profile.nicknameKey` — 검색·정렬용 정규화 값. 중복 판정에는 쓰지 않는다
- 앞뒤 공백을 제거한 뒤 **2~12자**만 허용한다
- 한글 완성형·영문·숫자만 허용하며 공백·밑줄·이모지는 허용하지 않는다

키는 NFKC 정규화 + `casefold()` 다. `Runner` / `runner` / `ｒｕｎｎｅｒ` 를 검색할 때
같은 값으로 찾기 위한 것이며, 닉네임 자체는 중복을 허용한다. 사용자 식별은 닉네임이
아니라 `user_id`로 한다. 정규화는 문자 검사보다 먼저 수행한다.

## 문서

- [`../highfive_guide.md`](../highfive_guide.md) — **설계 가이드 (앱·서버 공통)**

앱·서버 공통의 설계 결정과 미결정 사항이 이 문서에 모인다. **루트에 있다.**
날짜로 나누지 않고 **계속 다듬어 나가는 문서**이므로, 설계가 바뀌면 새 파일을 만들지 말고 이 문서를 갱신할 것.
설계 관련 작업 전에 먼저 읽을 것.

**서버에만 해당하는 내용**이 필요해지면 이 폴더에 별도 문서를 만든다.
공통 내용은 루트 가이드에 두고 중복해서 적지 않는다.

### 주요 미결정 사항

1. 지도용 좌표 단순화 알고리즘 · 허용 오차
2. H3 Resolution 11 최종 확정과 최소 시간 겹침
3. H3 단계의 PostgreSQL 인덱스와 비동기 처리 방식
4. 원본 GPS·심박 파일 포맷의 향후 버전 전략

## 파일 작성 규칙

- **가이드·설계 문서** — 날짜 없이 고정 파일명. 새로 만들지 말고 기존 문서를 갱신
- **일회성 산출물** (분석 결과, 리포트 등) — `YYYYMMDD_HHMM_파일명` 형식 (생성순 정렬)
- 백업 파일(`.backup_*`) 생성 금지
- 한국어로 응답
