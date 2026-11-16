# Job Search

채용 공고 수집부터 개인화 추천, 지원 결과 관리까지 연결하는 개인 채용함입니다.

```text
Inthiswork → 공고·JD 수집 → Hermes 검토 → Slack 알림
                              ↓
                       웹 채용함 · 지원 현황
```

- **근거 있는 추천** — JD와 사용자 경력을 비교하고 추천 이유·근거를 저장합니다.
- **지원 관리** — 관심·준비·지원·보관, 서류·면접·최종 결과를 기록합니다.
- **마감 확인** — 지난 공고를 만료 목록으로 분리하고 출처를 다시 확인합니다.
- **개인 웹앱** — Flask·SQLite로 실행하며 Tailscale로 접근합니다.

## 시작하기

Python 3.12 이상이 필요합니다.

```sh
git clone https://github.com/NyongCho/job-search.git job_search
cd job_search
python3 -m venv .venv-dashboard
.venv-dashboard/bin/python -m pip install -r requirements.txt
.venv-dashboard/bin/python -m job_search.collect_jobs status
```

마지막 명령은 외부 접속 없이 빈 DB를 준비합니다. 실제 수집은 `status` 대신 `collect`를 사용합니다. 개인화 추천에는 별도로 설정한 Hermes와 사용자 경력 정보가 필요합니다.

- [로컬 웹앱 · Hermes · 서버 설치](docs/setup.md)
- [개발 · 테스트 · 구조 변경 안내](docs/development.md)
- [GitHub Actions · Tailscale 자동 배포](docs/deployment.md)
- [Codex·Hermes에서 사용하는 스킬](skills/job-search-pipeline/SKILL.md)

## 프로젝트 구조

```text
job_search/    앱 코드, 웹 템플릿과 정적 파일
scripts/       Hermes에 설치하는 실행 스크립트
tests/         오프라인 테스트와 선택적 실제 RSS 검사
deploy/        환경 설정 예시, systemd 서비스·타이머
docs/          설치와 개발 안내
skills/        별도 환경으로 복사할 수 있는 에이전트 스킬
```

실행 데이터는 저장소 루트의 `data/`에 둡니다. `jobs.sqlite`에는 공고·추천 결과를, `dashboard/dashboard.sqlite`에는 개인 지원 상태·전형 결과를 저장합니다. 웹앱은 공고 DB를 읽기 전용으로 사용합니다.

## 개발

```sh
make test       # Python + JavaScript, 외부 호출 없음
make test-live  # 실제 Inthiswork RSS 확인
```

JS 테스트에는 Node.js 18 이상이 필요합니다. 다른 Python 환경은 `make test PYTHON=python3`로 지정합니다.

현재 자동 수집·마감 재조회 소스는 Inthiswork입니다. 마감이 불명확한 공고는 임의로 만료시키지 않습니다. 공개 저장소에는 코드·테스트·설정 예시만 포함하며 실제 DB, 지원 이력, 채팅 추출본과 인증 설정은 제외합니다.
