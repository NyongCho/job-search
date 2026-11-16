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
3. [Tailscale OIDC 설정](https://tailscale.com/docs/features/workload-identity-federation)에서 GitHub 발급자를 선택합니다. Subject는 `repo:YOUR_ACCOUNT/job-search:environment:production`, 추가 claim `ref`는 `refs/heads/main`으로 제한합니다. 권한은 **Auth Keys → Write**, 태그는 `tag:job-search-ci`만 지정합니다.
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

활성화 직전 채용 cron 작업만 일시 정지하고 실행 중인 검토·마감 갱신이 끝날 때까지 최대 10분 기다립니다. 웹앱을 잠시 중지하고 SQLite 백업을 만든 다음 `current`를 전환합니다. `/healthz`가 정상이어야 성공하며, 실패하면 이전 코드로 되돌립니다. 배포 전 이미 정지한 cron은 켜지 않습니다. 배포 중에는 별도의 수동 수집·검토 실행을 시작하지 않습니다.

DB 백업을 자동으로 덮어 복원하지 않습니다. 실패 시에도 새로 입력한 지원 이력을 보존하기 위해서입니다. 로그·DB 백업·실패한 릴리스는 서버에만 남습니다. 용량을 확인하면서 사용하지 않는 예전 릴리스를 수동 정리하며, `current` 대상과 진행 중인 복구 기록의 이전 버전은 남겨 둡니다.

강제 종료나 스케줄 복원 실패로 `.deploy/transaction.json`이 남으면 다음 배포를 중단합니다. 관리자가 원인을 확인한 뒤 서버에서 다음 명령으로 이전 코드·스케줄을 복구합니다.

```sh
python3 ~/job_search/current/scripts/deploy.py --recover
systemctl --user status job-dashboard.service job-availability.timer
```

새 배포를 일시 중지하려면 GitHub 저장소 변수 `DEPLOY_ENABLED=false`로 설정합니다. 테스트는 계속 실행됩니다. 실제 배포 완료 여부는 Actions 성공 표시, 서버 `current/.revision`, 웹 상태를 함께 확인합니다.
