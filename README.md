# 25fw-kubig-mas
25FW KUBIG Conference Project

## Yahoo Finance Latest → DynamoDB

This repo fetches Yahoo Finance Latest headlines and stores them idempotently in DynamoDB.

Table schema (primary):
- PK `pk` (String) — `id#<hash16>` or `h#<hash16>` from URL
- No sort key on the primary table

GSI for 24h queries:
- Name `gsi_latest_utc`
- PK `gsi_utc_pk` (String) = `UTC#<YYYY-MM-DD>`
- SK `utc_ms` (Number) = epoch ms

### Usage (CLI)
최신 기사 목록을 DynamoDB에 적재합니다(테이블/GSI가 이미 존재한다고 가정):
```
python -m news_list.upload_db
```

Environment (필수):
- `TABLE_NAME`
- `AWS_REGION`

### AWS Lambda + EventBridge
- 핸들러: `news_list.aws_lambda_handler.handler`
- 동작: Yahoo Finance Latest 수집 후 DynamoDB에 멱등 업로드
- 환경 변수:
  - `TABLE_NAME`
  - `AWS_REGION` (예: `ap-northeast-2`)
- 배포 패키지 구성:
- 필수 코드(폴더 통째로 패키징 권장): `news_list/` 디렉터리 전체
  - 주요 파일: `news_list/aws_lambda_handler.py`, `news_list/upload_db.py`, `news_list/yahoo_fetch.py`, `news_list/aws_dynamo.py`
  - 의존성: `requests`, `beautifulsoup4` (Layer 또는 패키지에 포함)
- EventBridge 스케줄 생성(예: 15분 간격) → 타깃으로 Lambda 연결
