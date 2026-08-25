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

**Django 골격만 있는 상태.** 도메인 앱(사용자·러닝·하이파이브)은 아직 없다.

- git 저장소 아님 (유저가 직접 관리)
- DB 는 개발용 SQLite. 운영 DB 는 미정

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
└─ config/         # 프로젝트 설정 패키지 (열품타의 pi/ 에 해당)
   ├─ settings.py
   ├─ urls.py      # 앱이 부르는 엔드포인트는 전부 /api/ 아래
   ├─ asgi.py
   └─ wsgi.py
```

## 실행

처음 받았을 때:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # DJANGO_SECRET_KEY 를 채운다
.venv/bin/python manage.py migrate
```

개발 서버 (앱 실기기에서 붙으려면 `0.0.0.0` 이어야 한다):

```bash
.venv/bin/python manage.py runserver 0.0.0.0:8000
```

확인: `GET /api/health/` → `{"s": true}`

## 규약

- 응답은 열품타와 같이 `{'s': bool, ...}` 형태를 기본으로 한다.
- DRF 기본 권한이 `IsAuthenticated` 다. **공개 API 는 뷰에서 `AllowAny` 를 명시**한다.
- 시각은 전부 UTC 로 저장한다. 앱도 UTC 로 보낸다.

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
