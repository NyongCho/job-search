# 기존 작업 실행과 상태 확인

대상 앱의 `deployment.json`과 실제 Hermes cron 설정을 먼저 읽는다. 오래된 `validation`/`next_run_verified` 값은 과거 검증 기록이다. 현재 실행 상태를 대신하지 않는다. 구버전 앱은 `prepare-backfill` 명령이나 설정 가능한 경로가 없을 수 있으므로 `--help`와 파일 내용을 확인한다.

앱에 `job_search/collect_jobs.py`가 있으면 패키지 구조다. 아래의 `python3 collect_jobs.py ...`는 프로젝트 루트에서 `python3 -m job_search.collect_jobs ...`로 실행하고, 기간 드라이버는 `python3 -m job_search.run_job_backfill`을 사용한다. 프리런 원본은 `scripts/hermes_collect.py`다. 이 스킬의 준비 도구가 만든 독립 수집 폴더는 기존 단일 파일 명령을 그대로 쓴다. 두 구조의 코드를 섞어 복사하지 않는다.

## 단일 실행

- 수집만 요청하면 앱에서 `python3 collect_jobs.py collect`를 실행한다. 이 명령은 Hermes 검토나 Slack 전달을 하지 않는다.
- 전체 추천 실행은 기존 작업 ID에 대해 설치된 Hermes의 `cron run JOB_ID` 또는 cron 도구의 `action=run`을 사용한다. 새 작업을 만들지 않는다.
- 프리런 스크립트는 **한 개의 실제 활성 Hermes 실행**을 요구한다. 직접 실행해 가짜 실행 ID를 주입하지 않는다.
- `collect_jobs.py --db PATH status|pending|show|review`를 이용한다. `--db`는 하위 명령 앞에 온다. `status`도 새 DB를 만들 수 있으므로 기존 경로 존재 여부를 확인한다.
- 프리런은 활성 실행의 기존 미전달 추천부터 회수한다. 신규 공고는 `collect_jobs.py scheduled --execution-id ID`로 처리하고, 외부 Hermes 작업은 반환된 `scheduled_digest`만 전달한다. 설치된 프리런이 활성 ID와 작업 프로필을 제공한다.
- 기존 DB는 활성 쓰기가 끝난 뒤 백업하고, Hermes 프로필 환경에서 `python3 fit_work.py --db data/jobs.sqlite --state data/dashboard/dashboard.sqlite`로 이전한다. 이때 기존 평가 기준 S0가 고정된다. 그런 다음 배포된 전달 전용 프롬프트로 정기 작업을 전환한다.
- 평가 기준은 `memories/USER.md`, `memories/MEMORY.md`의 경력 관련 문단과 명시적으로 연결된 경력 문서를 사용한다. 별도로 연결할 파일은 `FIT_PROFILE_FILES`에 경로 구분자로 지정한다. 기본 `FIT_BATCH_SIZE=3`, `FIT_CONCURRENCY=1`이다. 모델 실행은 Hermes의 도구 없는 독립 세션을 사용한다.
- `fit_items`는 JD와 사용자 답변별 평가 결과를, `fit_attempts`는 실제 세션별 사용량을 보관한다. `uncertain`이면 `<DB>.fit-runs`의 해당 세션 출력과 제출 상태를 확인한다. 이전 실행이 남긴 출력은 다음 워커에서 회수한다.

## 기간 일괄 처리

1. 같은 작업의 활성 실행과 기간 처리 프로세스를 확인한다. 살아 있으면 해당 실행을 추적한다. 활성 배치 중에는 DB/manifest/프롬프트를 교체하지 않는다.
2. 새 기간 처리 요청이면 고정 시작·끝 시각과 ID 목록을 만든다. 새 버전의 예:

```sh
cd /path/to/job-project
python3 collect_jobs.py prepare-backfill --days 14 --manifest data/backfill-14days.json
```

이 명령은 전체 페이지를 수집한 뒤 저장하며, 정기 수집 체크포인트와 변경되지 않은 기존 판정을 유지한다. 같은 manifest가 있으면 덮어쓰지 않는다. 이전 완료 기록은 날짜별 이름으로 보관한 후 새 작업을 준비한다. 중단된 작업을 이어갈 때는 기존 manifest를 그대로 쓴다. 파일명은 호환성을 위해 `backfill-14days.json`이지만 실제 범위는 파일 안의 시각과 ID가 기준이다.

기존 앱이 이 명령을 지원하지 않으면 스킬 번들로 대상 머신의 별도 임시 작업 폴더를 준비하고, 그 수집기를 `--db 기존앱/data/jobs.sqlite prepare-backfill --manifest 기존앱/data/backfill-14days.json`처럼 절대 경로로 실행할 수 있다. 먼저 현재 DB 스키마가 번들과 호환되고 같은 작업이 비활성인지 확인한다. 새 스킬 설치만으로 운영 앱을 덮어쓰지 않는다.

3. `run_job_backfill.py`가 읽는 Hermes 경로·작업 ID와 `hermes_job_prompt.txt`를 확인한다. 해당 Hermes 환경의 Python(보통 `hermes_repo/venv/bin/python`)으로 드라이버를 실행한다. 백그라운드 실행은 사용 가능한 프로세스 관리자로 시작하고 정확한 PID/서비스 핸들을 기록한다. 드라이버는 기존 작업을 배치별로 실행하고 마지막에 기본 프롬프트를 복원한다.
4. 사용자가 요청한 간격으로 **실제 프로세스와 실행 ledger**를 함께 확인한다. 완료한 배치별로 DB 판정 수가 늘기 때문에 같은 수치만으로 중단으로 판단하지 않는다. 관찰 타임아웃도 작업 종료를 뜻하지 않는다.

## 검증과 복구

주요 상태 원본:

| 원본 | 의미 |
| --- | --- |
| 앱 `data/jobs.sqlite`, `posts` | JD, 판정, 이유, 근거 줄, 검토 시각 |
| 같은 DB `outbox` | 저장 추천문, 시도한 실행 ID들, 전달 확인 여부 |
| Hermes 홈 `cron/executions.db`, `executions` | 작업별 실행 ID, 상태, `delivery_outcome` |
| `data/backfill-status.json`, 프로세스 핸들 | 기간 처리 진행 기록과 실제 생존 여부 |
| 실제 Slack 스레드 | 사용자에게 보이는 추천과 완료 알림 |

조회는 SQLite `mode=ro`로 한다. Hermes 내부 스키마는 버전별로 확인한다. `recommend` 수나 빈 pending만으로 전송 성공을 주장하지 않는다. 추천은 연결된 실행이 **completed + delivered**여야 확인 처리한다. 실패한 실행의 오류 알림은 추천 전달 증거가 아니다. 미확인 outbox는 다음 실행에서 신규 검토보다 우선 재전달한다. 확인 기록 유실 시 중복 전송 가능성이 있는 at-least-once 방식이다.

기간 처리 완료 기준은 manifest의 모든 ID가 존재하고 판정됨, 이유·근거의 유효성, 미전달 outbox 0, 완료 알림의 실제 전달, 기본 프롬프트 복원이다. `needs_review`는 처리되었지만 지원 가능 판정이 보류된 건수로 별도 보고한다.

CLI 반환 코드와 요약보다 **이번에 생성된 정확한 실행 ID**의 ledger를 우선한다. 실제 실패가 확인되면 이미 완료한 판정을 유지하며 원인을 해결한다. 정상 활성 실행은 재시작하지 않는다. 구버전 드라이버의 `state=complete`도 최종 알림 전달 기록과 대조한다.
