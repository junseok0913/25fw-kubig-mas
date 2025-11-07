# DynamoDB 적재 설계서 (Yahoo Finance Latest 목록 · 업로드 단계 · 최종)

## 0) 범위·목표

* **대상**: Yahoo Finance Latest(US)에서 추출한 기사 목록(개수 가변, 최대 ≈50)
* **목표**: 항목을 **멱등적으로** DynamoDB에 저장하고, 이후 본문 크롤링 단계의 입력으로 사용
* **핵심 요구사항**

  * **중복 방지 & 기존값 보존**: 중복 시 **어떤 필드도 갱신하지 않음**
  * **관련 티커**: **리스트(List of String)** 로 저장(0/1/N 가능)
  * **업로드 시각/날짜**: **UTC / New York(EST/EDT) / KST** 각각 ISO·epoch(ms)·일자 + 뉴욕 **DST 여부/약어**
  * **중복 판정 기준**: **기사 링크(원본 URL) 자체**로 판정 (**정규화 미사용**, URL이 한 글자라도 다르면 별도 항목)
  * **PK 접두사**: `id#` / `h#` 사용 (**설명은 §2**)

---

## 1) 입력 데이터 (크롤러 → 업로드)

각 항목은 다음 필드를 가진다.

* `title` (string): 목록상의 제목
* `url` (string): 기사 **원본 링크(그대로)**
* `tickers` (list<string>): 관련 티커 대문자 목록 (없으면 빈 리스트 허용)

> 수집 개수는 **가변**이다(50보다 적어도 정상). 들어온 만큼만 적재한다.

---

## 2) 키 전략 (Partition Key = `pk`)

* **중복 판정 = 기사 링크 문자열 자체**
  → DynamoDB의 유일성도 **원본 URL의 해시**로 보장한다.
* **PK 구성 규칙**

  * 원본 URL에 **숫자형 기사 ID가 포함되어 있으면**: `pk = "id#<sha256(원본 URL)[:16]>"`
  * **그 외**(ID가 없으면): `pk = "h#<sha256(원본 URL)[:16]>"`
* **의미**

  * `id#` / `h#`는 **URL 내 ID 존재 여부를 구분하는 태그**이며,
    **두 경우 모두 URL 해시를 사용**하므로 **중복 판정은 항상 ‘링크 동일성’**으로 일관된다.
* **길이 통일 불필요**: 접두사+16hex 형태(둘 다 동일 길이)로 관리되며 운영/가독성을 높인다.

> 결과적으로, **같은 링크**면 같은 `pk`가 생성되어 중복으로 간주되고,
> **링크가 조금이라도 다르면**(쿼리/프래그먼트/리다이렉트 파라미터 등) **다른 항목**으로 저장된다.

---

## 3) 테이블 스키마

* **테이블명(예)**: `news_items`
* **파티션 키**: `pk` (String) / **정렬 키 없음**

| 필드                             | 타입         | 설명                                               |
| ------------------------------ | ---------- | ------------------------------------------------ |
| `pk`                           | S          | `"id#<hash16>"` 또는 `"h#<hash16>"` (원본 URL 해시 기반) |
| `source`                       | S          | 수집 소스(예: `"yf_latest"`)                          |
| `title`                        | S          | 목록상의 제목                                          |
| `url`                          | S          | 기사 **원본 링크** (중복 판정 근거)                          |
| `tickers`                      | **L of S** | 관련 티커 목록(0/1/N)                      |
| `uploaded_at_utc_iso`          | S          | 업로드 시각 ISO(UTC, `...Z`)                          |
| `uploaded_at_utc_ms`           | N          | 업로드 시각 epoch(ms, UTC)                            |
| `uploaded_at_est_iso`          | S          | 업로드 시각 ISO(`America/New_York`)                   |
| `uploaded_at_kst_iso`          | S          | 업로드 시각 ISO(`Asia/Seoul`)                         |
| `dt_utc` / `dt_est` / `dt_kst` | S          | 각 타임존의 일자(`YYYY-MM-DD`)                          |
| `tz_est_abbr`                  | S          | 뉴욕 타임존 약어(`EST`/`EDT`)                           |
| `tz_est_is_dst`                | BOOL       | 뉴욕 DST 여부                                        |

> **`url_norm` 컬럼은 삭제**한다. 분석/참고용이라도 보관하지 않음.
> **중복 체크는 오직 기사 링크(URL)의 해시로 구성된 `pk`로 수행**한다.

---

## 4) 시간대 저장 규칙

* 기준 시각: 업로드 실행 시점의 **UTC**
* 변환·저장:

  * **UTC**: ISO(접미사 `Z`), epoch(ms), `dt_utc`
  * **New York**: ISO(EST/EDT 반영), `dt_est`, `tz_est_abbr`, `tz_est_is_dst`
  * **KST**: ISO(+09:00), `dt_kst`

---

## 5) 쓰기 정책 (멱등 & 기존값 보존)

* **최초 삽입**: `PutItem` + `ConditionExpression attribute_not_exists(pk)` 로 기록
* **중복(이미 존재)**: **완전 보존** 정책
  → **어떤 필드도 업데이트하지 않음**(최초 기록 그대로 유지)

---

## 6) 예시 레코드

### (A) URL에 기사 ID가 있는 경우

```json
{
  "pk": "id#e4a1b3f7c9d81234",
  "source": "yf_latest",
  "title": "A Trump Supreme Court tariff defeat would add to trade uncertainty",
  "url": "https://finance.yahoo.com/news/trump-supreme-court-tariff-defeat-060559667.html",
  "tickers": [],
  "uploaded_at_utc_iso": "2025-11-06T00:35:12Z",
  "uploaded_at_utc_ms": 1762398912000,
  "uploaded_at_est_iso": "2025-11-05T19:35:12-05:00",
  "uploaded_at_kst_iso": "2025-11-06T09:35:12+09:00",
  "dt_utc": "2025-11-06",
  "dt_est": "2025-11-05",
  "dt_kst": "2025-11-06",
  "tz_est_abbr": "EST",
  "tz_est_is_dst": false
}
```

### (B) URL에 기사 ID가 없는 경우

```json
{
  "pk": "h#a1b2c3d4e5f67890",
  "source": "yf_latest",
  "title": "Example headline without numeric id",
  "url": "https://finance.yahoo.com/news/some-article?src=latest&guccounter=1",
  "tickers": ["NVDA","PLTR"],
  "uploaded_at_utc_iso": "2025-11-06T00:35:12Z",
  "uploaded_at_utc_ms": 1762398912000,
  "uploaded_at_est_iso": "2025-11-05T19:35:12-05:00",
  "uploaded_at_kst_iso": "2025-11-06T09:35:12+09:00",
  "dt_utc": "2025-11-06",
  "dt_est": "2025-11-05",
  "dt_kst": "2025-11-06",
  "tz_est_abbr": "EST",
  "tz_est_is_dst": false
}
```

---

## 7) 결정 사항 요약

* **중복 체크**: **기사 링크(원본 URL) 기준**으로만 수행
* **PK**: 원본 URL의 `sha256` 16hex로 유일성 확보 +
  URL에 ID가 있으면 `id#`, 없으면 `h#` 접두사로 구분
* **중복 시**: **기존값 100% 보존**(업데이트 없음)
* **저장 필드**: `title`, `url`, `tickers`, 시간·일자 3타임존 + DST/약어
* **정규화 URL(`url_norm`)**: **저장하지 않음**