# GitHub Actions와 Tailscale 자동 배포

`main` push 또는 `main`에서 수동 실행 → GitHub의 Python·JS 테스트 → Tailscale 임시 접속 → 서버 검증·배포 순서입니다. PR은 테스트만 실행합니다. 저장소 변수 `DEPLOY_ENABLED=true`일 때 배포가 켜집니다. 배포 중인 작업을 다음 push가 취소하지 않으며, 서버에서도 중복 배포를 잠금으로 막습니다.

## 서버 구조

```text
~/job_search/
  data/                    기존 DB·개인 지원 이력
  deployment.json          기존 인스턴스 설정
  hermes_job_prompt.txt     개인 프롬프트
  releases/<SHA>-<suffix>/  코드·버전별 .venv-dashboard
  current -> releases/...  현재 서비스 버전
  job_search -> current/job_search
  .deploy/                 비공개 설정·백업·로그·복구 기록
```

각 릴리스의 `data`, `deployment.json`, `hermes_job_prompt.txt`는 루트의 기존 파일을 참조합니다. 서버의 별도 기업 조사 파일 등은 유지합니다. 기존 단일 폴더 설치는 최초 한 번 서비스를 `current` 경로로 전환하고 Hermes 프롬프트를 모듈 명령으로 옮겨야 합니다. 일반 업데이트에서는 개인 프롬프트를 덮어쓰지 않습니다.

`scripts/hermes_dispatch.py`를 `~/.hermes/scripts/hermes_collect.py`에 **일반 파일로 복사**합니다. 이 파일이 `current/scripts/hermes_collect.py`를 실행합니다. Hermes는 scripts 폴더 밖으로 향하는 심볼릭 링크를 거부하므로 링크를 만들지 않습니다. 웹 서비스는 `current/.venv-dashboard/bin/waitress-serve --host=127.0.0.1 --port=8765 --call job_search.dashboard:create_production_app`, 작업 디렉터리는 `current`로 설정합니다. 마감 갱신은 같은 가상환경의 `python -m job_search.refresh_job_availability`입니다. 기존 systemd 단위에 drop-in을 추가해 설정할 수 있습니다.

서버의 `.deploy/config.json`은 `0600` 권한으로 작성합니다. 다음 값은 실제 경로로 바꾸며 Git에 넣지 않습니다.

```json
{
  "repository": "https://github.com/YOUR_ACCOUNT/job-search.git",
  "hermes_home": "/home/YOUR_USER/.hermes",
  "hermes_repo": "/home/YOUR_USER/.hermes/hermes-agent",
  "hermes_python": "/home/YOUR_USER/.hermes/hermes-agent/.hermes/bin/python",
  "job_id": "YOUR_JOB_ID",
  "base_url": "https://YOUR_DEVICE.YOUR_TAILNET.ts.net",
  "environment_file": "/home/YOUR_USER/.config/job-dashboard/environment"
}
```

## GitHub와 Tailscale 연결

1. GitHub에 `production` Environment를 만들고 배포 가능한 브랜치를 `main`으로 제한합니다.
2. Tailscale에서 `tag:job-search-ci`를 만들고 해당 태그가 배포 서버의 TCP 22에 접근하도록 허용합니다. 기존 `* → *` 허용 규칙이 있으면 좁은 규칙을 추가하는 것만으로 접근이 제한되지는 않습니다. 다른 장치의 기존 접근을 보존하면서 CI 태그의 접근 범위를 점검합니다.
3. [Tailscale OIDC 설정](https://tailscale.com/docs/features/workload-identity-federation)에서 GitHub 발급자를 선택합니다. **Subject를 저장소 이름만으로 추측하지 않습니다.** 아래 GitHub API에서 실제 `sub_claim_prefix`를 확인합니다. 기본 템플릿(`use_default: true`)이면 그 값 뒤에 `:environment:production`을 붙여 Subject로 사용합니다. 새 저장소는 `repo:YOUR_ACCOUNT@OWNER_ID/job-search@REPOSITORY_ID:environment:production`처럼 숫자 ID가 포함될 수 있습니다. 사용자 정의 템플릿이면 그 템플릿이 발급하는 전체 Subject에 맞춥니다. 추가 claim `ref`는 `refs/heads/main`으로 제한하고, 권한은 **Auth Keys → Write**, 태그는 `tag:job-search-ci`만 지정합니다.

   ```sh
   gh api repos/YOUR_ACCOUNT/job-search/actions/oidc/customization/sub
   ```

   [GitHub의 immutable Subject 안내](https://github.blog/changelog/2026-04-23-immutable-subject-claims-for-GitHub-actions-oidc-tokens/)를 참고합니다. 토큰 교환이 403으로 거부되면 Tailscale의 해당 OIDC 항목을 View/Edit로 열어 인증 오류와 Subject·Audience·추가 claim을 대조합니다. 이 단계의 실패를 서버 sudo 오류로 판단하지 않습니다.
4. Client ID와 Audience를 GitHub `production` 환경의 `TS_CLIENT_ID`, `TS_AUDIENCE` secret으로 저장합니다. 이 두 식별자는 장기 비밀번호가 아닙니다.
5. 별도 Ed25519 SSH 키를 생성합니다. 서버 `authorized_keys`에는 아래처럼 **강제 명령과 restrict**를 붙인 공개키만 추가하고, 개인키는 GitHub `production`의 `DEPLOY_SSH_KEY`로 저장합니다.

```text
restrict,command="/usr/bin/python3 /home/YOUR_USER/job_search/current/scripts/deploy.py" ssh-ed25519 PUBLIC_KEY job-search-deploy
```

6. 같은 환경에 `DEPLOY_HOST`(Tailscale IPv4/DNS), `DEPLOY_USER`, `DEPLOY_KNOWN_HOSTS`를 저장합니다. 호스트 키는 기존에 신뢰한 서버 연결에서 확인하며, 워크플로에서 새 `ssh-keyscan` 결과를 그대로 신뢰하지 않습니다.
7. 워크플로의 저장소 이름 조건을 자신의 저장소에 맞추고, 준비가 끝나면 저장소 변수 `DEPLOY_ENABLED`를 `true`로 설정합니다.

Tailscale은 네트워크 연결을 제공하고 SSH 공개키는 서버 로그인을 인증합니다. Tailscale SSH 기능이나 관리자 sudo 권한은 이 구성에 필요하지 않습니다. 배포 키는 일반 셸·포트 전달을 열 수 없지만, `main`에 들어간 앱 코드는 서비스 사용자 권한으로 실행됩니다. 따라서 `main` 쓰기 권한을 신뢰 가능한 계정에만 부여해야 합니다.

## 배포와 복구

수신기는 전달받은 전체 SHA가 원격 `main`과 일치하는지 확인합니다. 별도 릴리스에서 의존성 설치·전체 오프라인 테스트·복사한 DB로 운영 앱 시작 검사를 실행합니다. DB 스키마가 바뀌면 자동 배포를 거부합니다. 스키마 변경은 별도 이전 작업을 설계해야 합니다.

### 마감·기업 로고 테이블을 추가하는 첫 배포

기존 서버의 두 DB에는 한 번의 명시적 이전이 필요합니다. 서버에서 검토한 **새 버전 소스**의 스크립트를 일반 관리자 셸로 실행합니다. 새 버전 전체 SHA는 원격 `main`과 일치해야 합니다. 이 명령은 일반 배포와 같은 잠금을 사용하며 CI의 강제 SSH 명령에서는 실행할 수 없습니다.

```sh
python3 /path/to/approved-source/scripts/deploy.py --migrate-card-metadata FULL_MAIN_SHA
```

1. 후보 릴리스를 준비하고 테스트합니다. 실제 DB의 복사본을 후보 버전으로 이전한 뒤 기존·후보 앱의 `/healthz`와 기존 마감 갱신기의 쓰기를 확인합니다.
2. 원래 켜져 있던 서비스·스케줄, 백업 경로와 진행 지점을 `.deploy/transaction.json`에 기록합니다. Hermes 채용 작업과 마감 타이머를 멈추고 실행 중인 작업이 끝나면 웹앱도 중지합니다. 로고 유닛이 설치되어 있으면 경로 감시와 서비스를 먼저 중지합니다. 이 동안 수동 수집·검토·지원 이력 가져오기도 실행하지 않습니다.
3. 쓰기 작업이 멈춘 두 DB를 `.deploy/backups/<release>/`에 백업합니다. 공고 DB와 개인 상태 DB 순으로 새 테이블을 추가하고, 이전된 DB를 다시 복사해 후보 시작 검사를 실행합니다.
4. 후보 버전을 시작해 `/healthz`를 확인합니다. 웹앱과 스케줄은 원래 활성 상태로 복귀합니다. 이후에는 일반 자동 배포를 사용합니다.

첫 DB 이전 직후 중단되거나 후보 앱이 실패해도 이전 코드는 추가된 테이블과 함께 동작합니다. 복구할 때 운영 DB를 백업으로 덮어쓰지 않습니다. 진행 기록이 남아 있으면 같은 새 버전 소스에서 아래 복구 명령을 실행한 뒤 이전 배포 명령을 다시 실행할 수 있습니다. 이미 생긴 테이블은 그대로 사용합니다.

```sh
python3 /path/to/approved-source/scripts/deploy.py --recover
```

개발 환경의 기존 데이터나 별도 서버의 멈춘 DB 복사본은 두 경로를 명시해 이전할 수 있습니다.

```sh
.venv-dashboard/bin/python -m job_search.migrate_card_metadata \
  --jobs-db data/jobs.sqlite --state-db data/dashboard/dashboard.sqlite
```

### 적합도 평가를 켜는 첫 배포

마감·로고 이전을 마친 서버에서 새 버전의 스크립트로 아래 명령을 실행합니다. 기존 배포 잠금과 SQLite 백업 절차를 사용합니다.

```sh
python3 /path/to/approved-source/scripts/deploy.py --migrate-fit FULL_MAIN_SHA
```

DB 복사본에 `posts.fit_review` 열을 추가하고 구버전의 읽기·쓰기와 후보 웹앱 기동을 확인한 뒤, 쓰기 작업이 멈춘 운영 공고 DB에 같은 이전을 적용합니다. 기존 검토의 점수는 비어 있으며, 기존 전달 대기문은 저장된 그대로 재시도됩니다. [Hermes 프롬프트 전환](setup.md#기존-설치에서-적합도-평가-켜기)을 마치면 새 검토부터 점수가 생성됩니다.

구형 검토 방식으로 되돌릴 때는 보관한 개인 프롬프트를 파일과 등록된 Hermes 작업 양쪽에 복원합니다. 추가된 열은 구버전에서도 읽기·쓰기가 가능하므로 유지합니다. 배포 중단 기록이 남으면 위의 `--recover` 절차를 사용합니다.

### 기업 로고 워커를 추가하는 첫 배포

기존 마감·로고 테이블이 있는 서버는 이 워커를 추가할 때 별도 DB 이전이 필요하지 않습니다. SSH 강제 명령은 현재 릴리스의 배포 스크립트를 실행하므로, 워커 도입 전 버전에서는 두 유닛을 제어하지 못합니다. 최초 배포와 그 복구가 끝날 때까지 설치와 활성화를 미룹니다. 두 유닛을 이미 설치했다면 배포 전에 감시를 비활성화하고 서비스를 중지합니다.

```sh
systemctl --user disable --now job-logo-worker.path
systemctl --user stop job-logo-worker.service
systemctl --user show --property=ActiveState job-logo-worker.path job-logo-worker.service
```

설치된 두 유닛이 정지 상태인지 확인합니다. 이전 버전의 배포 스크립트가 실행되는 경우 이 상태에서 첫 배포를 진행합니다. 운영 DB에 마감·로고 테이블도 없다면 위의 DB 이전 절차를 먼저 적용합니다.

워커를 지원하는 릴리스가 `current`에서 정상 동작하고 `.deploy/transaction.json`이 정리된 뒤에 아래 명령을 실행합니다. 릴리스의 가상환경과 작업 디렉터리는 `current`를 따르고, 워커와 감시는 루트의 공유 DB와 신호 디렉터리를 사용합니다.

```sh
cd ~/job_search
install -d -m 0700 data/jobs.sqlite.logo-requests
install -Dm644 current/deploy/job-logo-worker.path ~/.config/systemd/user/job-logo-worker.path
install -Dm644 current/deploy/job-logo-worker.service ~/.config/systemd/user/job-logo-worker.service
install -d ~/.config/systemd/user/job-logo-worker.service.d
cat > ~/.config/systemd/user/job-logo-worker.service.d/current.conf <<'EOF'
[Service]
WorkingDirectory=%h/job_search/current
ExecStart=
ExecStart=%h/job_search/current/.venv-dashboard/bin/python -m job_search.company_logos --db %h/job_search/data/jobs.sqlite --drain
EOF
systemctl --user daemon-reload
systemctl --user enable --now job-logo-worker.path
# 기존 공고를 처리하는 최초 1회 실행
systemctl --user start job-logo-worker.service
journalctl --user -u job-logo-worker.service --since today
```

로고가 갱신되지 않으면 `systemctl --user status job-logo-worker.path job-logo-worker.service`와 워커 로그를 확인합니다. `.service`는 한 회차를 마치면 정지 상태로 돌아갑니다. 수집 완료 뒤 워커가 기동하지 않으면 신호 디렉터리의 존재 여부와 감시 경로를 확인합니다. 수집 로그에 신호 기록 실패가 보이면 `~/job_search/data`와 `jobs.sqlite.logo-requests`의 쓰기 권한을 확인합니다. 권한이나 유닛 경로를 수정한 뒤 워커를 한 번 수동 시작하면 DB에 남은 요청을 처리할 수 있습니다. 처리 결과 필드는 [설치 안내](setup.md#기업-로고-워커)에 설명되어 있습니다.

### 새로고침 워커와 평가 이력 전환

새 버전 소스와 전체 `main` SHA를 준비한 뒤, 릴리스 방식 서버에서 최초 한 번 실행합니다. 명령은 두 DB의 복사본으로 이전을 먼저 확인합니다. 실제 전환에서는 정기 작업과 워커를 정지하고 진행 중인 모델 실행이 끝나기를 기다린 뒤 DB 두 개, 개인 프롬프트 파일, 등록된 작업의 프롬프트를 백업합니다. 백업 위치는 `.deploy/backups/<release>/`입니다. 이 단계가 완료되면 정기 작업의 프롬프트가 저장된 전달문을 반환하도록 바뀝니다.

```sh
python3 /path/to/approved-source/scripts/deploy.py --migrate-refresh FULL_MAIN_SHA
cd ~/job_search
install -Dm644 current/deploy/job-refresh-worker.service ~/.config/systemd/user/job-refresh-worker.service
systemctl --user daemon-reload
systemctl --user enable --now job-refresh-worker.service
systemctl --user status job-refresh-worker.service
```

서비스는 `current` 릴리스와 웹앱과 같은 환경 파일·영속 DB를 사용합니다. Hermes가 다른 위치에 설치돼 있으면 해당 서버의 `HERMES_HOME`, `HERMES_REPO`, `HERMES_PYTHON`을 설정합니다. 정기 프리런과 워커에 같은 경력 자료와 평가 설정을 적용합니다. 일반 배포는 기존 활성 상태를 복원하고 워커는 미완료 작업을 다시 읽습니다. 최초 전환 실패나 중단 시 `--recover`가 이전 릴리스와 두 프롬프트를 복원합니다.

이전 릴리스로 복구한 동안의 수집·검토도 같은 DB에 기록됩니다. 새 릴리스를 다시 시작하면 그동안 수집한 미검토 공고는 정기 검토 대상이 되고, 구버전에서 이미 검토한 공고의 추천 이력은 보존됩니다.

## 일반 배포 동작과 복구

활성화 직전 로고 경로 감시를 먼저 멈추고 로고 서비스의 중단을 확인합니다. Hermes 채용 작업과 마감 타이머를 일시 정지하고 실행 중인 검토·마감 갱신이 끝날 때까지 최대 10분 기다립니다. 웹앱을 잠시 중지하고 SQLite 백업을 만든 다음 `current`를 전환합니다. `/healthz`가 정상이어야 성공하며, 실패하면 이전 코드로 되돌립니다. 배포 중에는 별도의 수동 수집·검토·로고 처리를 시작하지 않습니다.

정지 전에 처리하지 못한 로고 신호는 공유 DB 옆에 남습니다. 로고를 지원하는 릴리스에서 배포 전 활성 상태였던 감시만 재개합니다. 워커 도입 전 릴리스로 복구하면 감시는 정지 상태로 둡니다. 그 릴리스를 계속 운영한다면 `systemctl --user disable --now job-logo-worker.path`로 재부팅 시 시작도 막습니다. 지원 버전으로 다시 배포한 뒤 최초 활성화 절차를 적용합니다.

DB 백업을 자동으로 덮어 복원하지 않습니다. 실패 시에도 새로 입력한 지원 이력을 보존하기 위해서입니다. 로그·DB 백업·실패한 릴리스는 서버에만 남습니다. 용량을 확인하면서 사용하지 않는 예전 릴리스를 수동 정리하며, `current` 대상과 진행 중인 복구 기록의 이전 버전은 남겨 둡니다.

강제 종료나 스케줄 복원 실패로 `.deploy/transaction.json`이 남으면 다음 배포를 중단합니다. 관리자가 원인을 확인한 뒤 서버에서 다음 명령으로 이전 코드·스케줄을 복구합니다.

```sh
python3 ~/job_search/current/scripts/deploy.py --recover
systemctl --user status job-dashboard.service job-availability.timer
```

현재 릴리스가 워커 도입 전 버전이면 로고 유닛을 제어할 수 있는 검토된 새 버전의 `scripts/deploy.py --recover`를 사용합니다. 복구가 이전에 활성 상태였던 로고 유닛을 불러오지 못하면 유닛 파일을 복구하고 `daemon-reload`를 실행한 뒤 복구를 다시 실행합니다.

새 배포를 일시 중지하려면 GitHub 저장소 변수 `DEPLOY_ENABLED=false`로 설정합니다. 테스트는 계속 실행됩니다. 실제 배포 완료 여부는 Actions 성공 표시, 서버 `current/.revision`, 웹 상태를 함께 확인합니다.
