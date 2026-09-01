# CLAUDE.md

이 파일은 HighFive Django 저장소에서 작업할 때 필요한 **서버 전용 지침**만 담는다.
제품 정책과 앱 구현을 반복해서 설명하지 않는다.

## 먼저 확인할 문서

| 작업 | 문서 |
|---|---|
| 운동 동기화·H3·하이파이브·DB 정책 | [`../highfive_guide.md`](../highfive_guide.md)의 관련 절 |
| 앱 요청·응답 사용 방식 | 앱 코드와 [`../app/highfive_local_client.md`](../app/highfive_local_client.md) |
| 남은 작업 | [`../TODO.md`](../TODO.md) |

`TODO.md`는 기존 항목을 수정하거나 삭제하지 않고 새 항목만 추가한다.

## 현재 서버 경계

- Django 5.2 + Django REST Framework
- SimpleJWT: access 30분, refresh 90일, 회전 및 블랙리스트
- PostgreSQL 17: 로컬 Docker, 운영 AWS RDS
- MinIO: 로컬 객체 저장소, 운영은 비공개 S3/CDN
- 운동 목록은 고정 snapshot과 불투명 cursor로 10건씩 반환한다.
- GPS·심박 원본은 DB 배열이 아니라 분리된 비공개 객체로 저장한다.
- 서버가 원본 GPS를 H3 Resolution 11 체류 구간으로 변환한다.
- 하이파이브 관계 테이블과 서버 Redis 캐시는 없다. 요청받은 운동 최대 10건을 실시간 판정한다.

## 경로

| 대상 | 경로 |
|---|---|
| 프로젝트 루트 | `/Users/asu/Documents/pallo/highfive` |
| 서버 | `/Users/asu/Documents/pallo/highfive/server` |
| 앱 | `/Users/asu/Documents/pallo/highfive/app` |

API 스펙을 바꿀 때 앱 파서와 호출 순서를 직접 확인한다.

## 구조

```text
server/
├─ dev.py
├─ config/             설정·공통 API 예외·루트 URL
├─ user_manager/       Profile·Firebase 로그인·닉네임
└─ workout_manager/
   ├─ models.py        Workout·WorkoutDetail·SpatialIndexVersion·TrajectorySegment
   ├─ serializers.py   업로드 검증·운동 응답
   ├─ spatial_index.py H3 변환과 구간 저장
   ├─ high_five.py     페이지 단위 실시간 판정
   └─ views.py         업로드·다운로드·상세·H3 API
```

## 실행

```bash
python dev.py up       # PostgreSQL·MinIO·Redis 확인, migrate, 0.0.0.0:8000 실행
python dev.py status
python dev.py logs
python dev.py db
python dev.py down     # 데이터 유지
python dev.py reset    # 로컬 PostgreSQL·MinIO·Redis 초기화 후 다시 실행
```

`reset`은 로컬 개발 데이터에만 사용한다. 실기기 연결을 위해 Django는 기본
`0.0.0.0:8000`으로 실행한다. MinIO 접근 방식과 LAN/ADB reverse 설정은 `dev.py`의 현재
구현을 기준으로 확인한다.

## API

| 엔드포인트 | 역할 |
|---|---|
| `POST /api/auth/signin/` | 소셜 로그인 및 가입 |
| `POST /api/auth/autologin/` | refresh 기반 자동 로그인 |
| `POST /api/users/nickname/` | 닉네임 설정·변경 |
| `POST /api/workouts/upload/prepare/` | 검증 후 필요한 Presigned POST 발급 |
| `POST /api/workouts/upload/create/` | 객체 검증 후 운동·상세·H3 생성 |
| `GET /api/workouts/` | 변경된 내 운동 10건 커서 다운로드 |
| `POST /api/workouts/high-fives/` | 내 운동 최대 10건의 하이파이브 실시간 계산 |
| `GET /api/workouts/<id>/detail/` | GPS·심박의 짧은 다운로드 URL 반환 |
| `GET /api/workouts/<id>/h3/` | 소유자의 H3 구간과 셀 경계 반환 |

응답은 기본적으로 `{'s': bool, ...}` 형태다. 공개 API는 `AllowAny`를 명시하고 나머지는
기본 `IsAuthenticated`를 유지한다.

## 데이터·판정 원칙

- 모델과 API JSON 필드는 현재 프로젝트 규칙에 따라 camelCase를 사용한다.
- 시각은 UTC로 저장하고 범위는 `[start, end)`로 판정한다.
- 운동 식별은 `UNIQUE(user, source, sourceName, sourceWorkoutId)`로 보장한다.
- 업로드는 객체 해시·크기·소유권을 검증한 뒤 트랜잭션으로 메타데이터와 H3를 확정한다.
- 하이파이브는 다른 사용자, 같은 활성 인덱스 버전, 같은 H3 셀, 양의 시간 겹침이 조건이다.
- 한 운동에서 같은 상대 사용자는 여러 번 겹쳐도 한 명으로 집계한다.
- 하이파이브는 업로드 시 저장하지 않고 피드에서 요청한 운동만 계산한다.
- GPS·심박 원본이나 presigned URL을 릴리즈 로그에 남기지 않는다.

## 인증·프로필

- 로그인 응답은 토큰과 `profile`을 분리한다.
- `profile.nickname`이 비어 있으면 앱이 닉네임 화면으로 보낸다.
- 닉네임은 앞뒤 공백 제거 후 2~12자의 한글 완성형·영문·숫자만 허용하고 중복은 허용한다.
- 사용자 식별은 닉네임이 아니라 user ID로 한다.
- refresh 실패가 네트워크 오류인지 세션 만료인지 구분한다.

## 확인 명령

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test
```

모델 변경 시 migration과 테스트를 함께 갱신한다. 초기 migration을 직접 합치는 작업은 로컬
DB를 reset하기로 합의된 개발 단계에서만 한다.

## 문서 작성 규칙

- 공통 설계는 루트 가이드에만 쓰고 이 파일에는 작업 지침만 둔다.
- 날짜별 설계 문서를 새로 만들지 않고 기존 기준 문서를 갱신한다.
- 백업 Markdown 파일을 만들지 않는다.
- 한국어로 응답한다.
