# AWS Lambda: Yahoo Finance 뉴스 수집 파이프라인

이 문서는 `Lambda.Dockerfile` 및 `Lambda/` 폴더의 코드를 기반으로, 해당 컨테이너 기반 AWS Lambda 함수의 동작을 정리한 것입니다.  
이 Lambda는 **AWS EventBridge 스케줄러(kubig-LambdaTrigger)** 에 의해 `0/30 * * * * ? *` 주기로 실행되며, Docker 이미지(`Lambda.Dockerfile`로 빌드 후 ECR 푸시)를 사용해 AWS Lambda에서 구동됩니다.

---

## 전체 아키텍처 개요

- **트리거**
  - EventBridge 스케줄러 규칙 이름: `kubig-LambdaTrigger`
  - 스케줄: 약 **30분마다** 실행 (`0/30 * * * * ? *`)
  - 대상: ECR에 배포된 컨테이너 이미지 기반 Lambda 함수

- **핸들러**
  - 엔트리포인트: `Lambda.aws_lambda_handler.handler`
  - 환경변수:
    - `TABLE_NAME`: DynamoDB 테이블명 (기본: `kubig-YahoofinanceNews`)
    - `AWS_REGION`: DynamoDB 및 S3가 위치한 리전
    - `BUCKET_NAME`: 뉴스 본문 XML 파일을 저장하는 S3 버킷명

- **주요 단계**
  1. `Lambda/upload_db.py`  
     - `Lambda/yahoo_fetch.py`를 통해 **Yahoo Finance Latest News(US)** 목록을 크롤링
     - 수집된 기사 메타데이터를 DynamoDB 테이블에 **멱등 삽입** (신규 기사만 저장)
  2. `Lambda/detail_crawl.py`  
     - DynamoDB에서 아직 본문이 처리되지 않은 레코드를 조회 (`path` 필드가 비어있는 항목)
     - 각 기사 상세 페이지를 크롤링 (`Lambda/article_crawler.py`)
     - 본문 및 메타데이터를 XML로 직렬화하여 S3에 저장 (`Lambda/upload_s3.py`)
     - DynamoDB 레코드를 업데이트(본문 경로, 제공자, 발행일, 관련 기사 등)


---

## Lambda 핸들러 동작 흐름

핸들러: `Lambda/aws_lambda_handler.py`

```python
def handler(event, context):
    # 1) 최신 뉴스 목록 → DynamoDB에 적재
    upload_res = run_upload(table_override, region_override)

    # 2) 개별 기사 페이지 크롤링 + S3 XML 업로드 + DynamoDB 필드 업데이트
    bucket = os.getenv("BUCKET_NAME")
    detail_res = run_detail_crawl(
        table_name=upload_res["table"],
        region=upload_res["region"],
        bucket=bucket,
        max_items=100,
    )
    return {"ok": ..., "upload": upload_res, "detail": detail_res}
```

- **1단계 (`run_upload`)**
  - `Lambda/yahoo_fetch.py`의 `fetch_news_list()`로 Latest News 페이지에서 기사 리스트를 수집
  - `Lambda/upload_db.py`의 `build_items()`로 DynamoDB 아이템 문서로 변환
  - `Lambda/aws_dynamo.py`의 `put_items_idempotent()`로 `attribute_not_exists(pk)` 조건을 사용해 **중복 없이** 삽입

- **2단계 (`run_detail_crawl`)**
  - DynamoDB에서 아직 본문이 처리되지 않은 레코드(`path`가 비어있는 레코드)를 조회
  - 각 URL에 대해 `Lambda/article_crawler.py`의 `crawl_yahoo_finance_page()`로 기사 본문/제공자/작성자/발행 시각 등을 추출
  - `Lambda/upload_s3.py`의 `put_article_xml()`을 사용해 XML로 S3에 업로드
  - 관련 기사(related articles)를 제목 기반으로 다시 DynamoDB에서 검색해 `related_articles` 목록으로 저장
  - 최종적으로 해당 레코드의 `title`, `provider`, `publish_et_iso`, `path`, `related_articles`를 `UpdateItem`으로 업데이트


---

## 외부 사이트 크롤링: Yahoo Finance Latest News

### 목록 크롤링 (`Lambda/yahoo_fetch.py`)

- 대상 URL: `https://finance.yahoo.com/topic/latest-news/`
- HTTP 클라이언트: `requests`
- HTML 파서: `BeautifulSoup`
- User-Agent, Accept-Language, Referer 헤더를 지정해 브라우저 유사 요청으로 처리
- 주요 선택자:
  - `ul.stream-items li.stream-item.story-item section[data-testid="storyitem"]`
  - 각 섹션에서 `a[href*="/news/"][aria-label]` 또는 `a[href*="/news/"][title]`을 기사 링크/제목으로 사용
  - 섹션 내부의 `a[href^="/quote/"]` 링크를 통해 티커 심볼을 수집

반환되는 각 뉴스 항목 구조:

```python
{
    "title": "<기사 제목>",
    "url": "<원본 기사 URL>",
    "tickers": ["AAPL", "TSLA", ...],  # 관련 티커(symbol) 목록, 없으면 빈 리스트
}
```


### 상세 페이지 크롤링 (`Lambda/article_crawler.py`)

- 기사 상세 페이지 URL 별로 본문과 메타데이터를 수집
- 주요 기능:
  - `_fetch_html(url)`: `requests`로 HTML 수신
  - `_find_article_wrappers(soup)`: `article[data-testid="article-content-wrapper"]` 등에서 article 래퍼들을 찾음
  - `_parse_provider()`: 상단 로고/텍스트 및 byline 영역에서 **뉴스 제공자(언론사)**를 추출
  - `_parse_author()`: byline 영역에서 기자 이름(작성자)을 추출
  - `_parse_time()`: `<time>` 태그에서 발행 시각 정보를 추출하고, UTC 기준 ISO 및 epoch ms로 변환
  - `_extract_body_text()`: 본문 컨테이너 내 `h1~h6`, `p`, `li` 태그 텍스트를 필터링·중복 제거 후 리스트로 반환

`crawl_yahoo_finance_page(url)` 반환 구조:

```python
{
    "page_url": "<기사 URL>",
    "main_article": {
        "title": "<메인 기사 제목>",
        "provider": "<언론사/제공자>",
        "author": "<작성자>",
        "time_display": "<페이지 내 표시 문자열>",
        "time_iso_utc": "<UTC 기준 ISO8601 문자열 또는 None>",
        "time_utc_ms": 1710000000000,  # epoch ms 또는 None
        "url": "<기사 URL>",
        "body_text": "<본문 전체 텍스트 (문단 사이에 공백 줄)>",
        "body_paragraph_count": 23,     # 추출된 문단 개수
    },
    "related_articles": [
        {
            "title": "<관련 기사 제목>",
            "provider": "<관련 기사 제공자>",
        },
        ...
    ],
}
```


---

## DynamoDB 스키마 구조

Lambda는 단일 DynamoDB 테이블을 사용하여 Yahoo Finance 기사 목록과 처리 상태를 관리합니다.  
현재 운영 중인 테이블 예시는 `kubig-YahoofinanceNews`이며, 환경변수 `TABLE_NAME`으로 이름을 주입합니다.

### 테이블 및 인덱스 정의

**메인 테이블**

| 항목              | 값                                  |
| ----------------- | ----------------------------------- |
| 테이블 이름       | `kubig-YahoofinanceNews` (`TABLE_NAME`) |
| 파티션 키 (PK)    | `pk` (String)                       |
| 정렬 키 (SK)      | 없음                                 |
| Billing mode      | `PAY_PER_REQUEST`                   |

**글로벌 보조 인덱스 (GSI)** – `gsi_latest_utc`

| 항목          | 값                            |
| ------------- | ----------------------------- |
| 인덱스 이름   | `gsi_latest_utc`             |
| 파티션 키     | `gsi_utc_pk` (String)        |
| 정렬 키       | `utc_ms` (Number)            |
| Projection    | `ALL`                         |


### 속성별 스키마 요약

`Lambda/upload_db.py`와 `Lambda/detail_crawl.py`에서 사용되는 주요 속성은 아래와 같습니다.

| 필드명           | 타입            | 키 역할                 | 설명                                                                 | 예시 값                                                |
| ---------------- | --------------- | ------------------------ | -------------------------------------------------------------------- | ------------------------------------------------------ |
| `pk`             | String          | 메인 테이블 PK          | 기사 URL 기반 해시 키. 숫자형 기사 ID가 있으면 `id#`, 아니면 `h#` + `sha256(url)` 앞 16자리 | `h#abcdef0123456789`                                   |
| `gsi_utc_pk`     | String          | GSI 파티션 키           | UTC 기준 날짜 버킷 (`UTC#YYYY-MM-DD`)                               | `UTC#2025-03-01`                                       |
| `utc_ms`         | Number          | GSI 정렬 키             | 수집 시각의 epoch milliseconds (UTC)                                | `1710000000000`                                        |
| `title`          | String          | -                        | 기사 제목                                                           | `Tesla shares rally after earnings beat`               |
| `url`            | String          | -                        | Yahoo Finance 기사 원본 URL                                         | `https://finance.yahoo.com/news/...`                   |
| `tickers`        | List\<String\>  | -                        | 관련 종목(티커) 목록                                                | `["AAPL", "TSLA"]`                                     |
| `et_iso`         | String          | -                        | 수집 시각(현재 시각)의 America/New_York ISO 문자열                  | `2025-03-01T09:30:00-05:00`                            |
| `publish_et_iso` | String          | -                        | 기사 발행 시각(본문 페이지 기준)을 America/New_York으로 변환한 ISO | `2025-03-01T09:35:00-05:00`                            |
| `provider`       | String          | -                        | 뉴스 제공자/언론사                                                  | `Yahoo Finance`, `Reuters`                             |
| `path`           | String          | -                        | S3에 저장된 기사 XML 파일의 객체 키                                 | `UTC#2025-03-01/h#abcdef0123456789.xml`                |
| `related_articles` | List\<String\> | -                       | 제목 기준으로 매칭된 관련 기사들의 `pk` 목록                        | `["h#1234abcd...", "h#5678efgh..."]`                   |

> 참고: `gsi_utc_pk`, `utc_ms`, `et_iso` 세 필드는 `build_items()` 내부 `_now_fields()`에서 자동으로 채워지며,  
> 최근 N일 기사 조회 및 시간 기준 정렬에 활용할 수 있습니다.


### 상세 크롤링 이후의 업데이트 필드

`Lambda/detail_crawl.py`의 `process_single_item()`에서는 각 기사에 대해 다음 필드를 업데이트합니다:

- `title`
  - 실제 크롤링된 메인 기사 제목(`title_crawled`)이 있으면 그 값으로 덮어씌움
- `provider`
  - `article_crawler.crawl_yahoo_finance_page()`에서 추출한 제공자(언론사)
- `publish_et_iso`
  - 본문 페이지의 UTC 시간 정보를 `America/New_York` 타임존으로 변환한 ISO 문자열
  - 내부 변환 함수: `_utc_iso_to_et_iso()`
- `path`
  - S3에 저장된 기사 XML 파일의 객체 키 (예: `"UTC#2025-03-01/h#abcdef0123456789.xml"`)
- `related_articles`
  - 관련 기사들의 `pk` 목록 (중복 제거 후 문자열 리스트)

또한, 제공자가 `"PREMIUM"`인 기사에 대해서는 **테이블에서 삭제**하는 처리도 포함됩니다.


---

## S3와의 상호작용

### XML 업로드 흐름

1. `Lambda/detail_crawl.py`에서 기사 상세 정보를 `article_doc` 딕셔너리로 구성:

   ```python
   article_doc = {
       "pk": pk,
       "url": main.get("url") or url,
       "provider": provider,
       "author": main.get("author") or "",
       "publish_iso_utc": time_iso_utc or "",
       "publish_et_iso": publish_et_iso or "",
       "body_text": body_text,
   }
   ```

2. S3 객체 키 생성:

   ```python
   s3_key = f"{gsi_utc_pk}/{pk}.xml"
   # 예: "UTC#2025-03-01/h#abcdef0123456789.xml"
   ```

3. `Lambda/upload_s3.py`의 `put_article_xml(bucket, key, article_doc)` 호출:
   - 내부에서 `build_article_xml(article_doc)`으로 XML 문자열 생성
   - `boto3.client("s3").put_object()`로 업로드
   - ContentType: `application/xml`


### S3 객체 키 구조

- 버킷: 환경변수 `BUCKET_NAME`로 지정
- 키 패턴:

```text
<gsi_utc_pk>/<pk>.xml
예) UTC#2025-03-01/h#abcdef0123456789.xml
```

- 특징:
  - 날짜별 prefix를 가짐 → 특정 날짜(또는 최근 N일) 기사만 쉽게 필터링 가능
  - DynamoDB의 `path` 필드와 1:1로 매핑되어, 애플리케이션에서 기사 본문을 조회할 때 바로 S3 객체를 읽어올 수 있음


---

## 개별 뉴스 기사 XML 파일 구조

XML 구조는 `Lambda/upload_s3.py`의 `build_article_xml()`에서 정의됩니다.  
각 기사는 하나의 `<article>` 루트 엘리먼트로 표현되며, 필드는 다음과 같습니다.

### XML 요소 구조

```xml
<article>
  <pk>h#abcdef0123456789</pk>
  <url>https://finance.yahoo.com/news/...</url>
  <provider>Yahoo Finance</provider>
  <author>John Doe</author>
  <publish_iso_utc>2025-03-01T14:30:00Z</publish_iso_utc>
  <publish_et_iso>2025-03-01T09:30:00-05:00</publish_et_iso>
  <body><![CDATA[
여기에 기사 본문 전체 텍스트가 들어갑니다.
단락 간에는 빈 줄이 포함됩니다.
  ]]></body>
</article>
```

### 필드 설명

- `<pk>`
  - DynamoDB 파티션 키와 동일한 값
  - 예: `h#abcdef0123456789`

- `<url>`
  - 실제 기사 페이지 URL

- `<provider>`
  - 언론사/뉴스 제공자
  - 예: `Yahoo Finance`, `Reuters`, `Bloomberg` 등

- `<author>`
  - 기사 작성자(기자) 이름
  - Yahoo 페이지의 byline에서 추출

- `<publish_iso_utc>`
  - UTC 기준 발행 시각, ISO8601 형식
  - 예: `2025-03-01T14:30:00Z`

- `<publish_et_iso>`
  - `America/New_York` 타임존으로 변환된 발행 시각
  - 예: `2025-03-01T09:30:00-05:00`

- `<body>`
  - 기사 본문 전체 텍스트
  - `<![CDATA[ ... ]]>` 블록 안에 들어가며,  
    내부 줄바꿈/특수문자 등을 이스케이프 없이 그대로 보존


---

## 에러 처리 및 재실행 특성

- **멱등성 보장**
  - 새 기사 삽입 시 `attribute_not_exists(pk)` 조건을 사용하므로, 이미 존재하는 기사(`pk`)는 다시 실행해도 중복 삽입되지 않음.
  - 상세 크롤링 단계에서 이미 `path`가 채워진 아이템은 `list_unprocessed_items()`에서 제외되므로, 동일 기사를 반복 처리하지 않음.

- **PREMIUM 기사**
  - `provider`가 `"PREMIUM"`인 경우:
    - 해당 레코드는 DynamoDB에서 삭제
    - 유료/Premium 기사에 대한 데이터 축적을 피하는 용도

- **실패 처리**
  - 크롤링 실패, S3 업로드 실패, DynamoDB 업데이트 실패 등의 경우
    - 표준 오류(stderr)에 로그 출력
    - 결과 요약에 `action` 값 (`crawl_failed`, `s3_failed`, `update_failed` 등)으로 표시
  - 다음 스케줄 실행 시에도 처리되지 않은 레코드는 다시 시도할 수 있음


---

## 요약

- 이 Lambda 함수는 **30분마다** Yahoo Finance Latest News 페이지를 수집하고,
  - **DynamoDB**에는 기사 메타데이터 및 처리 상태를 저장하고,
  - **S3**에는 각 기사별 본문을 담은 **XML 파일**을 저장합니다.
- DynamoDB는 `pk`를 기준으로 멱등 삽입을 수행하며,  
  날짜 버킷(`gsi_utc_pk`)과 시간(`utc_ms`, `et_iso`)을 통해 최근 기사 조회를 지원합니다.
- S3 XML 파일은 `<article>` 루트 아래에 기사 식별자, URL, 제공자, 작성자, 발행 시각, 본문을 구조화해서 저장하며,  
  DynamoDB의 `path` 필드를 통해 각 기사와 XML 파일이 연결됩니다.
