# 소스 지원 범위

2026-11-14 검증 스냅샷이다. 수량·공고·조건은 추가 시점에 다시 확인한다.

| 소스 | 현재 코드/검증 | 수집 근거 |
| --- | --- | --- |
| Inthiswork | 번들 수집기 구현 | 공개 WordPress `/wp-json/wp/v2/posts`, 전체 본문·원문 URL·지원 링크. 신입/인턴·주니어 분류, 마감 분류 제외 |
| Greenhouse | 조회 검증, 어댑터 미구현 | [공식 API](https://docs.greenhouse.io/job-board.html): `/v1/boards/{board}/jobs?content=true`. Commvault 55/한국 6, Riot 195/한국 6. 전체 JD와 지역·원문 URL, 별도 지원 URL은 없을 수 있음 |
| Lever | 조회 검증, 어댑터 미구현 | [공식 API](https://github.com/lever/postings-api): `/v0/postings/{site}?mode=json`. Palantir 302/한국 5. description + lists + additional 전체를 합치고 hostedUrl/applyUrl 보존 |
| Remotive | 조회 검증, 어댑터 미구현 | [공식 API](https://github.com/remotive-com/remote-jobs-api): JD·지역 제한·공고 URL. 출처명/원문 링크 필수, 공개 데이터 24시간 지연, 하루 4회 이하 권고. 실제 카테고리 요청에 타 직무가 섞였음 |
| We Work Remotely | RSS 검증, 어댑터 미구현 | [공식 RSS](https://weworkremotely.com/remote-job-rss-feed): JD 포함, 출처 링크 필요. Worldwide 표시와 미국 거주 필수 본문이 충돌한 사례, 지원 버튼이 회원가입으로 연결된 사례 확인 |
| Ashby | 공식 문서만 확인 | [공식 API](https://developers.ashbyhq.com/docs/public-job-posting-api), 현재 환경의 성공 호출은 미검증 |

Greenhouse·Lever는 회사별 보드 주소가 필요하고 전체 기업 검색 API가 아니다. 소스 확장 시 한국 근무 가능성을 JD와 함께 판단한다. Remote/APAC/Worldwide만으로 자격을 확정하지 않는다.

현재 DB 키는 Inthiswork 정수 ID다. 새 소스를 연결하려면 source+source_id 키 및 기존 데이터 이전을 설계해야 한다. 서로 다른 서비스의 숫자 ID를 기존 posts에 그대로 섞지 않는다. 동일 공고 중복은 회사·직무·최종 지원 URL을 함께 확인한다. 검증 자료를 운영 소스로 추가하는 작업은 사용자의 별도 요청 범위에서 한다.
