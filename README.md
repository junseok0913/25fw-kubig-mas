# 25fw-kubig-mas
25FW KUBIG Conference Project

## Yahoo Finance Latest 수집 파이프라인

이 리포지토리는 Yahoo Finance Latest News를 주기적으로 크롤링해 **DynamoDB**와 **S3**에 저장하는 컨테이너 기반 AWS Lambda 함수를 제공합니다. 상세한 함수 동작은 [`Lambda.md`](./Lambda.md)에 정리되어 있습니다.

### 주요 동작 요약
- **트리거**: EventBridge 스케줄러(`kubig-LambdaTrigger`)가 약 30분마다 실행
- **핸들러**: `Lambda/aws_lambda_handler.handler`
- **1단계**: `Lambda/upload_db.py`가 최신 뉴스 메타데이터를 수집하고 DynamoDB에 멱등 삽입
- **2단계**: `Lambda/detail_crawl.py`가 본문을 크롤링해 XML로 S3에 저장하고 DynamoDB 레코드를 업데이트

### 환경 변수
- `TABLE_NAME`: DynamoDB 테이블명 (기본값 `latest_news_list`)
- `AWS_REGION`: AWS 리전
- `BUCKET_NAME`: 본문 XML을 저장할 S3 버킷명

### 로컬 테스트 (컨테이너 이미지 기반)
컨테이너 기반 Lambda 이미지를 사용하므로, Docker가 설치된 환경에서 Lambda 엔트리포인트를 실행해 볼 수 있습니다.

```bash
# 필수 환경 변수 지정 후 Lambda 핸들러 호출
TABLE_NAME=latest_news_list AWS_REGION=ap-northeast-2 BUCKET_NAME=my-bucket \
  python -m Lambda.aws_lambda_handler
```

### 배포 메모
- `Lambda.Dockerfile`을 사용해 이미지를 빌드하고 ECR에 푸시합니다.
- 빌드된 이미지를 사용해 Lambda 함수를 생성하고, EventBridge 스케줄러(`0/30 * * * * ? *`)를 연결합니다.
