# 설치와 실행

저장소 루트에서 아래 명령을 실행합니다. Python 3.12 이상을 사용하며, Hermes 일괄 검토와 systemd 예시는 Linux 기준입니다. 가상환경과 패키지 설치는 [README](../README.md)의 시작하기를 따릅니다.

## 수집과 웹앱

```sh
# 빈 DB 준비 또는 현재 상태 조회
.venv-dashboard/bin/python -m job_search.collect_jobs status

# 최근 공고 수집: Inthiswork 네트워크 요청
.venv-dashboard/bin/python -m job_search.collect_jobs collect
.venv-dashboard/bin/python -m job_search.collect_jobs pending
```

수집만으로 추천이 생성되지는 않습니다. Hermes 또는 사용자의 명시적인 JD 검토 후 추천 / 제외 / 확인 필요 판정을 저장합니다.

로컬 웹앱은 루프백 주소에서만 실행합니다.

```sh
export DASHBOARD_DEV_MODE=1
export DASHBOARD_BASE_URL=http://127.0.0.1:8765
export DASHBOARD_SECRET_KEY="$(.venv-dashboard/bin/python -c 'import secrets; print(secrets.token_hex(32))')"
.venv-dashboard/bin/flask --app 'job_search.dashboard:create_app' run --host 127.0.0.1 --port 8765
```

기본 데이터 경로는 `data/jobs.sqlite`, `data/dashboard/dashboard.sqlite`입니다. 앱 코드 폴더 안에 DB를 생성하지 않습니다. 웹앱에서는 `JOBS_DB`, `DASHBOARD_STATE_DB`로 변경할 수 있고, 수집기에서는 `--db`를 하위 명령 앞에 지정합니다.

기존 DB를 다시 사용하는 경우 먼저 [마감·기업 로고 테이블 이전](deployment.md#마감기업-로고-테이블을-추가하는-첫-배포)을 진행합니다. 이전을 마친 공고의 기업 로고는 다음 명령을 반복 실행해 나눠 채울 수 있습니다. 새로 수집하거나 변경된 공고는 수집이 끝난 뒤 일부씩 처리합니다.

```sh
.venv-dashboard/bin/python -m job_search.company_logos --db data/jobs.sqlite --limit 20
```

## Hermes와 Slack

루트 `deployment.json`에 실제 작업 ID·Slack 채널·웹 주소를 기록합니다. [설정 예시](../deploy/deployment.example.json)를 참고하며 기존 설정은 보존합니다. JSON만 작성하면 예약되는 것은 아닙니다. Hermes에서 작업을 만들고 그 ID와 실행 설정을 일치시켜야 합니다.

이 저장소용 프리런은 [scripts/hermes_collect.py](../scripts/hermes_collect.py)입니다. 기본 앱 위치 `~/job_search`, 기본 Hermes 홈 `~/.hermes`를 사용하며, Hermes의 `scripts/`에 복사해 실행해도 앱 패키지를 찾습니다. 프리런은 설치된 릴리스의 `.venv-dashboard`에서 수집과 로고 처리를 실행합니다. 프리런은 실제 활성 실행 ID를 요구하므로 수동 시험 대신 Hermes 작업을 통해 실행합니다.

새 설치에서 기본 프롬프트를 만들 때는 루트에서 다음 명령을 사용합니다. 이미 `hermes_job_prompt.txt`가 있으면 덮어쓰지 않고 실패합니다.

```sh
python3 - <<'PY'
from pathlib import Path
root = Path.cwd()
template = root / 'skills/job-search-pipeline/assets/runtime/hermes_job_prompt.txt.tpl'
prompt = template.read_text().replace('__APP_DIR_TEXT__', str(root))
prompt = prompt.replace('python3 collect_jobs.py', 'python3 -m job_search.collect_jobs')
with (root / 'hermes_job_prompt.txt').open('x') as output:
    output.write(prompt)
PY
```

Hermes 작업에 프롬프트, 프리런, Python, 작업 디렉터리, 시간대, 일정과 전달 채널을 등록합니다. 실제 Slack 인증과 봇 초대도 필요합니다. 토큰과 사용자 경력은 기존 Hermes의 개인 설정에 둡니다.

별도 경로나 Hermes 프로필에 **수집 전용 인스턴스**를 만들려면 [스킬 설치 안내](../skills/job-search-pipeline/references/setup.md)를 사용합니다. 스킬 번들은 독립 배포를 위해 자체 런타임을 포함하며, 그 생성 결과는 기존 `collect_jobs.py` 방식입니다. 웹앱은 이 저장소를 사용합니다.

## 서버 운영

1. 설치 경로를 `~/job_search`로 두거나 아래 서비스 파일의 경로를 조정합니다.
2. [환경 예시](../deploy/dashboard.env.example)를 기준으로 `~/.config/job-dashboard/environment`를 만들고 권한을 `0600`으로 설정합니다. 실제 로그인·서버 주소·세션 키를 채우고 개발 모드를 끕니다.
3. [웹 서비스](../deploy/job-dashboard.service)와 [마감 확인 서비스](../deploy/job-availability.service), [타이머](../deploy/job-availability.timer)를 `~/.config/systemd/user/`에 설치합니다.
4. `systemctl --user daemon-reload` 후 `systemctl --user enable --now job-dashboard.service job-availability.timer`를 실행합니다.
5. Waitress의 `127.0.0.1:8765`를 Tailscale Serve HTTPS로 연결합니다. 허용한 사용자 계정에서 실제 접근을 확인합니다.

운영 진입점은 `job_search.dashboard:create_production_app`이며 개발 모드를 거부합니다. Tailscale 사용자 신원과 CSRF를 검증합니다. 인증 헤더를 보존하지 않는 일반 공개 프록시에 그대로 노출하는 구성은 지원하지 않습니다.

마감 확인 타이머는 09:10·18:10 KST에 실행합니다. 수동 재조회는 다음 명령을 사용합니다.

```sh
python3 -m job_search.refresh_job_availability
systemctl --user status job-dashboard.service job-availability.timer
journalctl --user -u job-availability.service --since today
```

## 지원 기록 가져오기

```sh
python3 -m job_search.import_profile_applications \
  --profile-root /path/to/profile \
  --repository-url https://github.com/OWNER/PROFILE/tree/main \
  --confirmed-on YYYY-MM-DD
```

기본은 읽기 전용 미리보기입니다. 실제 지원한 기록임을 확인한 뒤 `--apply --confirmed-applied`로 저장합니다. 확인일과 실제 지원일은 구분합니다. 기존 출처 키는 건너뛰어 중복과 결과 덮어쓰기를 방지합니다.
