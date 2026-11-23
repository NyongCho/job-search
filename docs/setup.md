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

기존 DB에 마감·로고 테이블이 없다면 먼저 [DB 이전](deployment.md#마감기업-로고-테이블을-추가하는-첫-배포)을 진행합니다. 수집기는 공고를 DB에 저장한 뒤 로고 워커에 신호를 남깁니다. 검토는 로고 다운로드를 기다리지 않고 진행할 수 있습니다.

### 기업 로고 워커

아래 명령은 코드와 가상환경을 `~/job_search`에 둔 단일 폴더 설치용입니다. 워커는 이 폴더에서 `.venv-dashboard/bin/python`과 `data/jobs.sqlite`를 사용합니다. `current`로 버전을 전환하는 서버는 [릴리스 설치 절차](deployment.md#기업-로고-워커를-추가하는-첫-배포)를 따릅니다.

```sh
cd ~/job_search
install -d -m 0700 data/jobs.sqlite.logo-requests
install -Dm644 deploy/job-logo-worker.path ~/.config/systemd/user/job-logo-worker.path
install -Dm644 deploy/job-logo-worker.service ~/.config/systemd/user/job-logo-worker.service
systemctl --user daemon-reload
systemctl --user enable --now job-logo-worker.path
# 기존 공고를 처리하는 최초 1회 실행
systemctl --user start job-logo-worker.service
journalctl --user -u job-logo-worker.service --since today
```

다른 설치 경로나 DB를 사용하면 두 유닛의 경로를 함께 맞춥니다. `.path`의 `DirectoryNotEmpty`는 수집기에 지정한 DB의 `<DB>.logo-requests` 디렉터리를 가리켜야 합니다. 워커는 처리 가능한 요청을 기본 20건씩 반복 처리합니다. 한 회차의 로그에는 처리 건수(`checked`), 로고 확인 건수(`ready`), 로고 없음 건수(`missing`), 재시도 예정 건수(`retry`)가 기록됩니다. 로고 조회 중 타임아웃 같은 일시 실패는 다음 수집에서 재시도합니다. 새 공고가 없어도 성공한 수집은 신호를 남깁니다. 워커 자체가 오류로 종료하면 systemd가 30초 뒤 다시 실행합니다. 신호 디렉터리에는 수집기가 만든 신호 파일만 둡니다.

systemd를 사용하지 않는 환경에서는 같은 DB를 지정해 수동으로 처리할 수 있습니다.

```sh
.venv-dashboard/bin/python -m job_search.company_logos --db data/jobs.sqlite --drain
```

## Hermes와 Slack

루트 `deployment.json`에 실제 작업 ID·Slack 채널·웹 주소를 기록합니다. [설정 예시](../deploy/deployment.example.json)를 참고하며 기존 설정은 보존합니다. JSON만 작성하면 예약되는 것은 아닙니다. Hermes에서 작업을 만들고 그 ID와 실행 설정을 일치시켜야 합니다.

이 저장소용 프리런은 [scripts/hermes_collect.py](../scripts/hermes_collect.py)입니다. 기본 앱 위치 `~/job_search`, 기본 Hermes 홈 `~/.hermes`를 사용하며, Hermes의 `scripts/`에 복사해 실행해도 앱 패키지를 찾습니다. 프리런은 설치된 릴리스의 `.venv-dashboard`에서 수집을 실행합니다. 로고는 수집 신호를 받은 별도 서비스가 처리합니다. 프리런은 실제 활성 실행 ID를 요구하므로 수동 시험 대신 Hermes 작업을 통해 실행합니다.

새 설치에서 기본 프롬프트를 만들 때는 루트에서 다음 명령을 사용합니다. 이미 `hermes_job_prompt.txt`가 있으면 덮어쓰지 않고 실패합니다.

```sh
python3 - <<'PY'
from pathlib import Path
root = Path.cwd()
current = Path.home() / 'job_search/current'
workdir = current if current.is_dir() and current.samefile(root) else root
template = root / 'skills/job-search-pipeline/assets/runtime/hermes_job_prompt.txt.tpl'
prompt = template.read_text().replace('__APP_DIR_TEXT__', str(workdir))
prompt = prompt.replace('python3 collect_jobs.py', 'python3 -m job_search.collect_jobs')
with (root / 'hermes_job_prompt.txt').open('x') as output:
    output.write(prompt)
PY
```

Hermes 작업에 프롬프트, 프리런, Python, 작업 디렉터리, 시간대, 일정과 전달 채널을 등록합니다. 실제 Slack 인증과 봇 초대도 필요합니다. 토큰과 사용자 경력은 기존 Hermes의 개인 설정에 둡니다.

### 기존 설치에서 적합도 평가 켜기

새 DB에는 평가 결과를 담을 열이 있습니다. 기존 개발 DB는 아래 명령으로 이전합니다. 릴리스 방식의 운영 서버는 [적합도 이전 절차](deployment.md#적합도-평가를-켜는-첫-배포)를 따릅니다.

```sh
.venv-dashboard/bin/python -m job_search.collect_jobs --db data/jobs.sqlite migrate-fit
```

진행 중인 검토가 끝나면 현재 개인 프롬프트를 백업합니다. 템플릿의 항목별 평가, 모집 상태 확인, 결과 저장·전달 지시를 개인 프롬프트와 등록된 Hermes 작업 양쪽에 반영합니다. 패키지 설치에서는 템플릿의 `python3 collect_jobs.py`를 `python3 -m job_search.collect_jobs`로 바꾸고, 릴리스 방식의 작업 디렉터리는 `~/job_search/current`로 지정합니다. 기존의 `--digest` 파일 생성 지시는 새 `review … --execution-id …` 호출로 교체합니다.

새 검토부터 핵심 업무 60점, 필수 요건 30점, 우대 요건 10점을 적용합니다. 상세에서 항목별 경력 근거와 JD 줄 번호를 확인할 수 있습니다. 80점은 선호 밖 추천의 노출 기준입니다. 기존 추천에는 '미산정'을 표시합니다.

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
