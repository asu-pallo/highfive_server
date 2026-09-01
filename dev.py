#!/usr/bin/env python
"""로컬 Docker 서비스와 Django 개발 서버를 함께 관리한다."""

from __future__ import annotations

import argparse
import http.client
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from redis import Redis
from redis.exceptions import RedisError


SERVER_DIR = Path(__file__).resolve().parent
COMPOSE_FILE = SERVER_DIR / 'docker-compose.yml'
RUN_DIR = SERVER_DIR / '.run'
PID_FILE = RUN_DIR / 'django.pid'
MANAGE_PY = SERVER_DIR / 'manage.py'


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=SERVER_DIR, check=check)


def compose(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    return run(
        ['docker', 'compose', '-f', str(COMPOSE_FILE), *arguments],
        check=check,
    )


def load_environment() -> None:
    env_file = SERVER_DIR / '.env'
    if not env_file.exists():
        raise SystemExit('.env가 없습니다. cp .env.example .env 를 먼저 실행하세요.')

    # 셸로 실행하지 않으므로 괄호·공백이 들어간 값도 안전하게 읽는다.
    load_dotenv(env_file)
    os.environ.setdefault('S3_ENDPOINT_URL', 'http://localhost:9000')
    os.environ.setdefault('S3_ACCESS_KEY', 'minioadmin')
    os.environ.setdefault('S3_SECRET_KEY', 'minioadmin')
    os.environ.setdefault('S3_BUCKET_NAME', 'highfive-private')
    os.environ.setdefault('S3_REGION', 'ap-northeast-2')


def check_python_dependencies() -> None:
    try:
        __import__('django')
        __import__('psycopg')
        __import__('storages')
        __import__('django_redis')
    except ImportError:
        raise SystemExit(
            '서버 패키지가 부족합니다. '
            '.venv/bin/pip install -r requirements.txt 를 실행하세요.'
        )


def check_docker() -> None:
    if not COMPOSE_FILE.exists():
        raise SystemExit(f'Docker Compose 파일이 없습니다: {COMPOSE_FILE}')
    try:
        subprocess.run(
            ['docker', 'info'],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ['docker', 'compose', 'version'],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        raise SystemExit('Docker Desktop과 Docker Compose가 실행 중인지 확인하세요.')


def configure_android_minio_reverse() -> None:
    """연결된 Android 개발 단말의 localhost:9000을 로컬 MinIO로 전달한다."""
    public_endpoint = urlparse(os.getenv('S3_PUBLIC_ENDPOINT_URL', ''))
    if public_endpoint.hostname not in {'localhost', '127.0.0.1'}:
        return
    try:
        devices = subprocess.run(
            ['adb', 'devices'],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        print('Android adb가 없어 MinIO 포트 전달을 건너뜁니다.')
        return

    serials = [
        line.split('\t', 1)[0]
        for line in devices.stdout.splitlines()[1:]
        if line.endswith('\tdevice')
    ]
    if not serials:
        print('연결된 Android 단말이 없어 MinIO 포트 전달을 건너뜁니다.')
        return

    for serial in serials:
        result = subprocess.run(
            ['adb', '-s', serial, 'reverse', 'tcp:9000', 'tcp:9000'],
            check=False,
            capture_output=True,
            text=True,
        )
        registered = subprocess.run(
            ['adb', '-s', serial, 'reverse', '--list'],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and 'tcp:9000 tcp:9000' in registered.stdout:
            print(
                f'Android MinIO 연결 완료 · {serial}: '
                '단말 localhost:9000 → Mac localhost:9000'
            )
        else:
            reason = result.stderr.strip() or registered.stderr.strip() or '등록 확인 실패'
            print(f'Android MinIO 포트 전달 실패 · {serial}: {reason}')


def remove_android_minio_reverse() -> None:
    try:
        subprocess.run(
            ['adb', 'reverse', '--remove', 'tcp:9000'],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass


def wait_for_minio(timeout_seconds: int = 30) -> None:
    endpoint = 'http://localhost:9000/minio/health/ready'
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(endpoint, timeout=1) as response:
                if response.status == 200:
                    return
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            ConnectionError,
            TimeoutError,
        ):
            time.sleep(1)
    raise SystemExit(f'MinIO가 {timeout_seconds}초 안에 준비되지 않았습니다.')


def wait_for_postgres(timeout_seconds: int = 30) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = compose(
            'exec', '-T', 'postgres',
            'pg_isready', '-U', os.environ['DB_USER'], '-d', os.environ['DB_NAME'],
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise SystemExit(f'PostgreSQL이 {timeout_seconds}초 안에 준비되지 않았습니다.')


def wait_for_redis(timeout_seconds: int = 30) -> None:
    client = Redis.from_url(os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/1'))
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if client.ping():
                return
        except (RedisError, OSError):
            pass
        time.sleep(1)
    raise SystemExit(
        f'공용 Redis가 {timeout_seconds}초 안에 준비되지 않았습니다. '
        'pallo-redis가 127.0.0.1:6379에서 실행 중인지 확인하세요.'
    )


def stop_django() -> None:
    if not PID_FILE.exists():
        print('실행 중인 Django 개발 서버가 없습니다.')
        return

    try:
        process_group = int(PID_FILE.read_text().strip())
        os.killpg(process_group, signal.SIGTERM)
        print(f'Django를 종료했습니다. PID={process_group}')
    except (ValueError, ProcessLookupError):
        print('Django PID 파일이 오래되어 정리합니다.')
    finally:
        PID_FILE.unlink(missing_ok=True)


def up() -> None:
    load_environment()
    check_python_dependencies()
    check_docker()
    RUN_DIR.mkdir(exist_ok=True)

    print('[1/6] Docker 개발 서비스를 시작합니다.')
    compose('up', '-d')
    try:
        print('[2/6] PostgreSQL 준비를 확인합니다.')
        wait_for_postgres()

        print('[3/6] Redis 준비를 확인합니다.')
        wait_for_redis()

        print('[4/6] MinIO 준비와 비공개 버킷을 확인합니다.')
        wait_for_minio()
        compose('run', '--rm', 'minio-init')

        print('[5/6] Django 마이그레이션을 적용합니다.')
        run([sys.executable, str(MANAGE_PY), 'migrate'])

        # reset/up 준비 도중 무선 adb가 재연결될 수 있으므로 모든 준비가 끝난 뒤 설정한다.
        configure_android_minio_reverse()

        host = os.getenv('DEV_SERVER_HOST', '0.0.0.0')
        port = os.getenv('DEV_SERVER_PORT', '8000')
        print(f'[6/6] Django를 시작합니다: http://{host}:{port}')
        print('종료하려면 Ctrl+C를 누르세요. Django 로그는 아래에 바로 출력됩니다.')

        process = subprocess.Popen(
            [sys.executable, str(MANAGE_PY), 'runserver', f'{host}:{port}'],
            cwd=SERVER_DIR,
            start_new_session=True,
        )
        PID_FILE.write_text(str(process.pid))
        try:
            return_code = process.wait()
            if return_code != 0:
                raise SystemExit(return_code)
        except KeyboardInterrupt:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait()
    finally:
        PID_FILE.unlink(missing_ok=True)
        print('Docker 개발 서비스를 종료합니다. 데이터 볼륨은 유지됩니다.')
        compose('down', check=False)


def down() -> None:
    stop_django()
    remove_android_minio_reverse()
    if subprocess.run(
        ['docker', 'info'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0:
        compose('down', check=False)
    else:
        print('Docker가 실행 중이 아니므로 컨테이너 종료는 건너뜁니다.')


def status() -> None:
    if PID_FILE.exists():
        try:
            os.kill(int(PID_FILE.read_text().strip()), 0)
            print(f'Django: 실행 중 · PID={PID_FILE.read_text().strip()}', flush=True)
        except (ValueError, ProcessLookupError):
            print('Django: 중지됨 · 오래된 PID 파일 있음', flush=True)
    else:
        print('Django: 중지됨', flush=True)

    result = subprocess.run(
        ['docker', 'info'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode == 0:
        compose('ps', check=False)
    else:
        print('Docker: 실행 중이 아님')


def logs() -> None:
    print('Django 로그는 `python dev.py up`을 실행한 터미널에 표시됩니다.')
    print('Docker 서비스 로그를 표시합니다. 종료하려면 Ctrl+C를 누르세요.')
    compose('logs', '--follow', '--tail', '100', check=False)


def database_shell() -> None:
    load_environment()
    check_docker()
    compose('up', '-d', 'postgres')
    wait_for_postgres()
    compose(
        'exec', 'postgres', 'psql',
        '-U', os.environ['DB_USER'], '-d', os.environ['DB_NAME'],
    )


def reset() -> None:
    """로컬 PostgreSQL 스키마와 MinIO 파일을 삭제하고 빈 상태로 만든다."""
    load_environment()
    check_python_dependencies()

    if PID_FILE.exists():
        try:
            os.kill(int(PID_FILE.read_text().strip()), 0)
        except (ValueError, ProcessLookupError):
            PID_FILE.unlink(missing_ok=True)
        except PermissionError:
            raise SystemExit('Django 실행 상태를 확인할 수 없습니다. dev.py down을 먼저 실행하세요.')
        else:
            raise SystemExit('Django가 실행 중입니다. 먼저 python dev.py down을 실행하세요.')

    database_host = os.environ['DB_HOST']
    database_name = os.environ['DB_NAME']
    database_user = os.environ['DB_USER']
    if database_host not in {'localhost', '127.0.0.1'} or database_name != 'highfive':
        raise SystemExit(
            '로컬 HighFive PostgreSQL만 초기화할 수 있습니다. '
            f'host={database_host}, database={database_name}'
        )

    reset_local_object_storage()

    check_docker()
    compose('up', '-d', 'postgres')
    wait_for_postgres()
    print(f'로컬 PostgreSQL 스키마를 초기화합니다: {database_name}')
    compose(
        'exec', '-T', 'postgres', 'psql',
        '-v', 'ON_ERROR_STOP=1', '-U', database_user, '-d', database_name,
        '-c', 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;',
    )

    print('빈 DB에 마이그레이션을 적용합니다.')
    run([sys.executable, str(MANAGE_PY), 'migrate'])
    wait_for_redis()
    redis_client = Redis.from_url(
        os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/1')
    )
    keys = list(redis_client.scan_iter(match='*highfive:*'))
    if keys:
        redis_client.delete(*keys)
    print('로컬 PostgreSQL, MinIO 파일과 Redis 캐시 초기화가 완료됐습니다.')


def reset_local_object_storage() -> None:
    endpoint = urlparse(os.environ['S3_ENDPOINT_URL'])
    bucket = os.environ['S3_BUCKET_NAME']
    if endpoint.hostname not in {'localhost', '127.0.0.1'} or bucket != 'highfive-private':
        raise SystemExit(
            '로컬 MinIO가 아닌 저장소는 초기화할 수 없습니다. '
            f'endpoint={endpoint.geturl()}, bucket={bucket}'
        )

    check_docker()
    running = subprocess.run(
        [
            'docker', 'compose', '-f', str(COMPOSE_FILE),
            'ps', '--status', 'running', '--services',
        ],
        cwd=SERVER_DIR,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    minio_was_running = 'minio' in running

    try:
        print('로컬 MinIO를 준비합니다.')
        compose('up', '-d', 'minio')
        wait_for_minio()
        compose('run', '--rm', 'minio-init')
        print(f'로컬 MinIO 버킷 파일을 삭제합니다: {bucket}')
        compose(
            'run', '--rm', '--entrypoint', '/bin/sh', 'minio-init', '-c',
            'mc alias set local http://minio:9000 minioadmin minioadmin '
            '&& mc rm --recursive --force local/highfive-private '
            '&& mc anonymous set none local/highfive-private',
        )
    finally:
        if not minio_was_running:
            compose('stop', 'minio', check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    for command in ('up', 'down', 'status', 'logs', 'db'):
        subparsers.add_parser(command)
    subparsers.add_parser('reset')

    arguments = parser.parse_args()
    if arguments.command == 'reset':
        reset()
        up()
    else:
        {'up': up, 'down': down, 'status': status, 'logs': logs, 'db': database_shell}[
            arguments.command
        ]()


if __name__ == '__main__':
    main()
