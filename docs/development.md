# 개발과 유지보수

## 코드 찾기

| 파일 | 역할 |
| --- | --- |
| `job_search/collect_jobs.py`, `evidence.py` | 공고 수집, 본문 추출, 검토 결과와 전달 대기열 |
| `job_search/dashboard.py`, `dashboard_store.py` | Flask 라우트, 공고 조회, 개인 지원 상태 |
| `job_search/job_availability.py`, `refresh_job_availability.py` | 마감일 판정과 출처 재조회 |
| `job_search/import_profile_applications.py` | 기존 지원 자료 가져오기 |
| `job_search/run_job_backfill.py` | 기간별 일괄 검토 |
| `job_search/templates/`, `static/` | 웹 화면과 스타일·동작 |
| `scripts/hermes_collect.py` | 기존 Hermes에 설치하는 프리런 |
| `skills/job-search-pipeline/assets/` | 스킬만 복사해도 쓸 수 있는 수집용 런타임 |

앱은 저장소 루트에서 `python -m job_search.<모듈>`로 실행합니다. 패키지 내부 파일을 직접 `python job_search/collect_jobs.py`로 실행하지 않습니다. `scripts/hermes_collect.py`는 Hermes에 설치할 수 있는 독립 실행 스크립트입니다.

## 검증

```sh
make test
make test-live

# 필요한 Python 테스트만 실행
.venv-dashboard/bin/python -m unittest tests.test_job_availability -v
```

`make test`는 외부 접속 없이 Python 테스트와 Node.js 테스트를 실행합니다. `make test-live`는 별도 `tests/integration/`의 실제 RSS 검사를 실행합니다. 테스트는 임시 DB를 사용하며 운영 데이터로 저장 동작을 시험하지 않습니다.

수집 스킬 번들은 앱 패키지와 독립적인 배포 단위입니다. 번들 코드를 바꿀 때는 `assets/runtime-manifest.json`도 갱신하고 `tests.test_job_search_skill`로 복사·설정·실행을 검증합니다.

## Git과 배포

기능별 브랜치에서 수정 → 테스트 → 검토 → 병합 순서로 작업합니다. Git push만으로 운영 서버를 배포하거나 개인 DB를 이동하지 않습니다. 데이터와 서버별 설정은 Git에서 제외하고 별도로 백업합니다.

### 기존 단일 폴더 설치에서 이동

이번 구조 변경은 파일 위치와 실행 경로를 바꿉니다. 운영 중인 기존 설치에는 코드만 덮어쓰지 말고 다음 항목을 함께 변경합니다.

| 이전 | 현재 |
| --- | --- |
| `python3 collect_jobs.py ...` | `python3 -m job_search.collect_jobs ...` |
| `python3 run_job_backfill.py` | `python3 -m job_search.run_job_backfill` |
| `python3 refresh_job_availability.py` | `python3 -m job_search.refresh_job_availability` |
| `python3 import_profile_applications.py ...` | `python3 -m job_search.import_profile_applications ...` |
| `python3 prototype_job_evidence.py ID` | `python3 -m job_search.evidence ID` |
| `dashboard:create_production_app` | `job_search.dashboard:create_production_app` |
| `hermes_collect.py` | `scripts/hermes_collect.py` |
| `requirements-dashboard.txt` | `requirements.txt` |

DB·`deployment.json`·개인 프롬프트는 기존 루트에 유지합니다. 진행 중인 Hermes 실행이 없는 시점에 프리런을 교체하고, `hermes_job_prompt.txt` 안의 수집기 명령과 Hermes에 저장된 작업 프롬프트를 함께 갱신합니다. systemd 단위도 새 버전으로 설치한 뒤 daemon-reload와 웹 서비스 재시작이 필요합니다. 실행 위치는 프로젝트 루트여야 합니다.

새 버전을 별도 경로에서 검증하고 SQLite 백업을 만든 다음 적용합니다. 이 구조 변경 자체는 운영 서버에 자동 반영되지 않습니다.
