# Job Search

채용 공고 수집, JD에 근거한 개인화 추천, 지원 진행 관리를 연결하는 개인 채용함입니다.

Inthiswork에서 공고 본문과 지원 링크를 수집하고, Hermes가 사용자의 경력 자료를 참고해 추천 여부를 판단합니다. Slack으로 추천을 받고 Flask 웹앱에서 관심 공고와 지원 결과를 관리합니다.

## 기능

- 공고 본문·원문 URL·지원 링크 수집, 중복 방지와 변경된 JD 재검토
- 추천 / 제외 / 확인 필요 판정과 JD 근거 줄 번호 저장
- 추천 전달 대기열과 Hermes 실행 기록을 통한 재전송 관리
- 추천·관심·지원 준비·지원 현황·보관·만료 목록
- 서류, 1차 면접, 2차 면접, 최종 결과 및 날짜·메모 기록
- 명시된 접수 마감일과 Inthiswork 마감 분류를 통한 만료 판정
- 기존 지원 자료 가져오기, Codex·Hermes용 이식 가능한 스킬

기본 수집 소스는 Inthiswork입니다. 다른 플랫폼의 자동 수집이나 마감 재조회는 아직 포함하지 않습니다. 개인화 추천에는 별도로 설정된 Hermes와 사용자 경력 정보가 필요하며, 웹앱 자체가 추천 모델을 실행하지는 않습니다.

## 구성

```text
Inthiswork → Python 수집기 → jobs.sqlite
                              ↓
                     Hermes JD 검토 → Slack
                              ↓
                     Flask 개인 채용함
                              ↓
                     dashboard.sqlite
```

| 경로 | 역할 |
| --- | --- |
| `collect_jobs.py`, `prototype_job_evidence.py` | 공고 수집, 본문 추출, 검토 결과 저장 |
| `hermes_collect.py`, `run_job_backfill.py` | Hermes 실행 연결, 기간별 일괄 검토 |
| `dashboard.py`, `dashboard_store.py` | 웹앱, 데이터 조회, 개인 상태 관리 |
| `job_availability.py`, `refresh_job_availability.py` | 마감 판정, 기존 공고의 출처 재조회 |
| `import_profile_applications.py` | 기존 지원 기록 가져오기 |
| `templates/`, `static/` | 웹 화면 |
| `deploy/` | Tailscale 기반 운영 설정 예시와 systemd 단위 |
| `skills/job-search-pipeline/` | 독립 수집 작업을 준비하는 에이전트 스킬 |

웹앱과 마감 확인기는 `data/jobs.sqlite`을 읽기 전용으로 사용합니다. 지원 상태·전형 결과·출처 확인 정보는 별도의 `data/dashboard/dashboard.sqlite`에 저장합니다. 추천 판정과 개인 지원 상태는 서로 독립적입니다.

## 로컬 실행

Python 3.12 이상을 사용합니다. 수집기는 표준 라이브러리만 사용하고 웹앱에는 Flask와 Waitress가 필요합니다. Hermes 일괄 검토와 systemd 운영 예시는 Linux 기준입니다.

```sh
git clone https://github.com/NyongCho/job-search.git job_search
cd job_search
python3 -m venv .venv-dashboard
.venv-dashboard/bin/python -m pip install -r requirements-dashboard.txt

# 외부 호출 없이 빈 데이터베이스 생성
.venv-dashboard/bin/python collect_jobs.py status

# 선택: 최근 공고 수집 (Inthiswork 네트워크 요청)
.venv-dashboard/bin/python collect_jobs.py collect
```

로컬 미리보기는 루프백 주소에서만 사용합니다.

```sh
export DASHBOARD_DEV_MODE=1
export DASHBOARD_BASE_URL=http://127.0.0.1:8765
export DASHBOARD_SECRET_KEY="$(.venv-dashboard/bin/python -c 'import secrets; print(secrets.token_hex(32))')"
.venv-dashboard/bin/flask --app 'dashboard:create_app' run --host 127.0.0.1 --port 8765
```

공고를 수집해도 자동으로 추천되지는 않습니다. 미검토 목록은 `python3 collect_jobs.py pending`으로 확인하고, Hermes 또는 사용자의 명시적인 JD 검토 후 판정을 저장합니다.

## Hermes · Slack

새 인스턴스는 [스킬 설치·실행 안내](skills/job-search-pipeline/references/setup.md)를 따릅니다. `prepare_workspace.py`는 비어 있는 별도 폴더에 수집용 런타임과 설정을 생성합니다. 이 번들은 웹앱을 포함하지 않으므로 웹앱을 함께 사용할 때에는 이 저장소도 설치해야 합니다.

기존 `~/job_search`와 기본 Hermes 프로필에 연결할 때에는 `deployment.example.json`을 `deployment.json`으로 복사하고 실제 작업 ID·채널·주소를 입력합니다. 루트의 `hermes_collect.py`는 이 기본 경로 구성을 사용합니다. 다른 경로나 Hermes 프로필을 쓰는 경우 스킬이 생성하는 설정 가능한 런타임을 사용합니다.

개인화 프롬프트는 [템플릿](skills/job-search-pipeline/assets/runtime/hermes_job_prompt.txt.tpl)을 바탕으로 로컬 `hermes_job_prompt.txt`에 준비합니다. `__APP_DIR_TEXT__`는 실제 앱 경로로 바꿉니다. Hermes 작업 등록 및 Slack 인증은 별도 설정이며, JSON 파일을 복사하는 것만으로 예약 실행되지는 않습니다.

## 운영과 데이터

- [환경 변수 예시](deploy/dashboard.env.example)의 경로·Tailscale 주소·허용 계정·세션 키를 자신의 환경에 맞춥니다. 실제 설정 파일은 저장소 밖에 보관합니다.
- 웹 서비스 예시는 `~/job_search`를 설치 경로로 사용합니다. Waitress를 `127.0.0.1:8765`에 두고 Tailscale Serve HTTPS로 연결합니다.
- `dashboard:create_production_app`은 개발 모드를 거부하며 Tailscale 사용자 신원과 CSRF를 검증합니다. 인증 헤더를 보존하지 않는 일반 공개 프록시에 그대로 노출하는 구성은 지원하지 않습니다.
- 마감 확인 타이머는 매일 09:10·18:10 KST에 Inthiswork를 재조회합니다. 타이머는 설치·활성화해야 동작합니다. 수동 실행은 `python3 refresh_job_availability.py`입니다.
- 마감이 불명확하거나 조회에 실패한 경우 임의로 만료 처리하지 않습니다. 알려진 날짜는 화면 조회 시 현재 시각과 비교합니다.
- 배포 전 SQLite 백업을 만들고 해당 버전의 테스트를 실행합니다. 서버별 설정과 DB를 Git 체크아웃으로 덮어쓰지 않습니다.

이 저장소에는 실행 코드, 테스트와 설정 예시만 포함합니다. 실제 DB, 이력서, 지원 이력, 채팅 추출본, 개인 프롬프트, 서버 설정, 검증 데이터는 로컬에 남기며 `.gitignore`로 제외합니다. 공개 코드 저장소와 개인 웹앱의 접근 권한은 별개입니다.

## 테스트와 개발

기본 검증은 임시 DB와 가상 입력을 사용하는 오프라인 테스트입니다. JavaScript 테스트에는 Node.js 18 이상이 필요합니다.

```sh
.venv-dashboard/bin/python -m unittest \
  test_collect_jobs test_job_backfill test_job_search_skill \
  test_dashboard_store test_dashboard test_application_tracking \
  test_job_availability test_hermes_collect -v
node --test test_dashboard_js.cjs
node --check static/dashboard.js
```

`python3 -m unittest discover -v`는 위 테스트 외에 실제 RSS에 접속하는 `test_inthiswork_rss.py`도 실행합니다.

기능별 브랜치에서 수정하고 테스트 후 병합합니다. 운영 서버에는 검증한 커밋의 필요한 파일을 배포하며, Git push 자체로 서비스가 변경되지는 않습니다. 수집 스킬 번들의 코드를 바꿀 때는 `assets/runtime-manifest.json`도 갱신하고 이식성 테스트를 실행합니다.
