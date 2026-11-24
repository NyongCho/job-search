# 새 작업 폴더 준비

Python 3.12 이상, 표준 라이브러리만 사용한다. 수집기는 독립적으로 실행할 수 있고, 자동 개인화·Slack 전달에는 설정된 Hermes가 필요하다. 기간 처리 드라이버는 `fcntl`을 사용하는 Linux/macOS용이다.

스킬 폴더는 호출 환경에서 실제 경로를 찾아 사용한다. 다른 에이전트나 머신으로 옮길 때 `SKILL.md`, `references/`, `scripts/`, `assets/`를 포함한 폴더 전체를 복사한다. `instance.json`이 있다면 옮긴 환경의 값으로 별도 설정한다.

## 오프라인 준비

아래 예시의 스킬 경로와 작업 경로를 실제 절대 경로로 바꾼다. 경로에 공백이 있으면 인용한다. 셸 문자열을 만들기보다 도구의 인자 배열을 사용한다.

```sh
python3 /path/to/job-search-pipeline/scripts/prepare_workspace.py /path/to/new-job-project --hermes-home /path/to/hermes-profile
cd /path/to/new-job-project
python3 -m unittest test_collect_jobs test_job_backfill -v
python3 collect_jobs.py status
```

준비 명령은 비어 있는 폴더만 사용한다. 기존 DB·이력서·토큰·추천 기록을 가져오거나 Hermes 작업을 등록하지 않는다. 상태가 비어 있고 15개 테스트가 통과하면 독립 실행 준비가 된 것이다. 플래그는 `--help`로 확인한다.

`deployment.json`의 `app_dir`, `hermes_home`, `hermes_repo`, `hermes_cli`, `python`은 **실제로 실행할 머신의 경로**다. 가능하면 대상 머신에서 준비 명령을 실행한다. 다른 머신에서 만든 폴더를 이동했다면 해당 설정, 프리런 스크립트의 렌더된 앱 경로, 프롬프트의 작업 경로를 이동 후 검증한다. 준비 명령을 기존 데이터 위에 재실행하지 않는다.

## Hermes에 연결

사용자가 연결·예약 실행을 요청한 경우에만 진행한다. 기존 Hermes 모델·메모리·게이트웨이를 재사용한다.

1. 설치된 Hermes의 `cron --help`, `cron create --help` 또는 cron 도구 스키마를 읽는다. 버전별 옵션을 추측하지 않는다. 기존 작업이 있다면 ID와 스크립트 경로를 확인하여 중복 등록을 피한다.
2. Slack 채널과 봇 참여 상태를 확인한다. 사용자의 기존 승인 대상이 있으면 재사용한다. 자격증명은 기존 Hermes 설정에 남겨 둔다.
3. 생성한 `hermes_collect.py`를 해당 프로필의 `scripts/` 아래에 복사한다. 동일 이름 파일이 있으면 연결 대상을 확인하고 이름 충돌을 해결한 뒤 등록한다.
4. `hermes_job_prompt.txt`, 프리런 스크립트, Python 인터프리터, 앱 작업 디렉터리, 시간대, 일정, 전달 채널을 작업에 설정한다. 기본 예시는 `0 9,18 * * *`, `Asia/Seoul`이며 사용자 요청이 우선이다. 실제 생성된 ID를 `deployment.json.job_id`에 기록한다. JSON의 채널·일정 값만 바꿔서는 Hermes 작업 설정이 바뀌지 않는다.
5. 검토·전달 시험도 요청 범위 안에서 한다. 실제 전송을 시험하지 않았다면 연결 준비만 끝났다고 보고한다. 실제 시험을 했다면 새 실행 ID의 완료·전달 기록과 수신 채널을 확인한다.

새 프로필의 Hermes 위치를 설정만 했다고 해당 계정이 생성되거나 로그인되는 것은 아니다. 계정 연결이 필요한 경우 그 단계만 사용자에게 안내한다.

포스터 판독을 사용할 설치에서는 선택한 Python 환경에 Pillow를 설치하고 Tesseract에서 `kor`와 `eng` 언어 자료를 확인합니다. 시스템 경로에 없는 경우 정기 실행 환경의 `POSTER_TESSERACT`에 실행 파일 또는 래퍼의 절대 경로를 지정합니다. 준비한 작업 폴더에서는 `python poster_backfill.py --db data/jobs.sqlite --dry-run --limit 3`으로 판독 대상을 확인한 뒤 `--dry-run`을 빼고 소량 처리할 수 있습니다. 모델은 `--post-id ID --model`로 요청한 불확실한 포스터에만 사용합니다.
