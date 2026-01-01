# 📊 Stock Morning 데이터 수집 상세 문서

> 작성일: 2024-12-28  
> 버전: 2.2

---

## 1. 개요

Stock Morning 시스템은 **3가지 데이터 소스**에서 주식 분석에 필요한 정보를 수집합니다:

| 데이터 소스 | 수집 방법 | 저장 위치 | 수집 내용 |
|------------|----------|----------|----------|
| **SEC EDGAR** | REST API | SQLite + 로컬 파일 | 10-K, 10-Q (항상), 8-K, Form 4 (윈도우 내) |
| **Yahoo Finance 뉴스** | AWS (DynamoDB) | 임시 파일 → 분석 후 삭제 | 기업 관련 뉴스 기사 |
| **실시간 시장 데이터** | yfinance | 메모리 | 주가, P/E, 시가총액 등 30+ 지표 |

---

## 2. 실행 스크립트

### `run.py` - 통합 실행 스크립트

```bash
# 전체 파이프라인 (크롤링 + 분석 + JSON 저장)
uv run run.py --ticker GOOG

# 크롤링 생략 (기존 데이터 사용)
uv run run.py --ticker GOOG --skip-crawl

# 결과 JSON 저장 안 함
uv run run.py --ticker GOOG --no-save
```

**실행 순서:**
```
run.py
├── run_crawling()                    # 1단계: SEC 크롤링
│   ├── SECCrawler.crawl_filings_in_window()
│   │   └── 최근 N일 공시 다운로드 (기본 90일)
│   ├── SECCrawler.crawl_latest_annual_quarterly()
│   │   └── 10-K, 10-Q 항상 포함 (기간 무관)
│   └── SQLite DB + 로컬 파일 저장
│
├── run_analysis()                    # 2단계: 4명 전문가 토론
│   └── run_multiagent_pipeline(ticker)
│       ├── collect_data_node         # 데이터 수집 + sources 생성
│       ├── moderator_analysis_node   # 중재자 분석
│       ├── guided_debate_node (x3)   # 토론 라운드
│       └── conclusion_node           # 최종 결론 + sources 출력
│
└── cleanup_unused_files()            # 3단계: 파일 정리
    ├── 뉴스 임시 파일 전체 삭제 (pk로 DynamoDB 재조회 가능)
    └── SEC 파일: 10-K/10-Q + sources 포함 파일 유지
```

---

## 3. 데이터 수집 윈도우

### 크롤러 vs 분석기 윈도우

| 구분 | 크롤러 | 분석기 (data_fetcher) |
|------|--------|---------------------|
| 기본값 | 90일 | `SEC_CRAWLER_WINDOW_DAYS` 또는 24시간 |
| 10-K/10-Q | 항상 포함 | 항상 포함 |
| Form 4 등 | 윈도우 내 | 윈도우 내 |

### 예시

```
오늘: 2025-12-28
윈도우: 24시간

사용 가능:
✅ 10-K (2025-02-05) - 항상 포함
✅ 10-Q (2025-10-30) - 항상 포함
❌ Form 4 (2025-12-18) - 24시간 외
```

---

## 4. SEC EDGAR 공시 수집

**파일:** `src/sec_crawler.py`

### 수집 과정

```
1. 티커 → CIK 변환
   GET https://www.sec.gov/files/company_tickers.json
   예: GOOG → CIK 0001652044

2. 공시 목록 조회
   GET https://data.sec.gov/submissions/CIK{CIK}.json
   - 기본 윈도우: 90일 (SEC_CRAWLER_WINDOW_DAYS 환경변수)
   - 10-K, 10-Q는 기간 무관하게 최신 1건 항상 포함

3. 공시 파일 다운로드
   GET https://www.sec.gov/Archives/edgar/data/{CIK}/{ACCESSION}/{FILENAME}
   - 형식 우선순위: XML > HTML > TXT

4. 로컬 저장
   - 파일: downloads/sec_filings/{CIK}_{ACCESSION}_{FILENAME}
   - 메타데이터: sec_filings.db (SQLite)
```

### 10-K/10-Q 항상 포함

```python
# src/sec_crawler.py
def crawl_latest_annual_quarterly(self, ticker: str):
    """최신 10-K와 10-Q를 기간 무관하게 크롤링"""
    # 최신 10-K 1건
    # 최신 10-Q 1건
```

```python
# src/database/data_fetcher.py
# 4. 가장 최근 10-K, 10-Q는 항상 포함 (기간과 관계없이)
latest_annuals = self.db.get_latest_annual_quarterly(ticker)
for form_type in ['10-K', '10-Q']:
    filing = latest_annuals.get(form_type)
    if filing and filing.get('accession_number') not in existing_accession:
        sec_metadata.insert(0, filing)  # 맨 앞에 추가
```

---

## 5. 출처 정보 (Sources) - 검증 에이전트용

### 생성 위치

**파일:** `multiagent/nodes/data_collector.py`

```python
def _build_sources(ticker, sec_filings, aws_news, market_data) -> Dict:
    """검증 에이전트를 위한 출처 정보 구성 (20251222.json 형식)"""
```

### Sources 스키마 (새 형식)

```json
{
  "sources": {
    "ticker": "GOOG",
    "collected_at": "2025-12-28T06:43:00+00:00",
    "sources": [
      {
        "type": "sec_filing",
        "form": "10-Q",
        "filed_date": "2025-10-30",
        "reporting_for": "2025-09-30",
        "accession_number": "0001652044-25-000091",
        "file_path": "downloads/sec_filings/0001652044_000165204425000091_FilingSummary.xml"
      },
      {
        "type": "sec_filing",
        "form": "10-K",
        "filed_date": "2025-02-05",
        "reporting_for": "2024-12-31",
        "accession_number": "0001652044-25-000014",
        "file_path": "downloads/sec_filings/0001652044_000165204425000014_FilingSummary.xml"
      },
      {
        "type": "article",
        "pk": "id#e3faffb...",
        "title": "Google started the year behind in the AI race..."
      },
      {
        "type": "chart",
        "ticker": "GOOG",
        "source": "yfinance",
        "current_price": 314.96,
        "pe_ratio": 31.06,
        "market_cap": 1950000000000
      }
    ]
  }
}
```

### 저장 위치

`data/agent_results/{TICKER}_{TIMESTAMP}_debate.json`

---

## 6. 파일 정리 로직

**파일:** `run.py` - `cleanup_unused_files()`

### 정리 규칙

| 파일 유형 | 정리 정책 |
|----------|----------|
| **뉴스 파일** | 분석 후 전체 삭제 (pk로 DynamoDB 재조회 가능) |
| **10-K/10-Q** | 항상 유지 (FilingSummary.xml) |
| **기타 SEC** | sources에 있으면 유지 |

```python
# 뉴스 임시 파일 전체 삭제
for f in ticker_files:
    f.unlink()

# 10-K/10-Q는 항상 유지
if "FilingSummary" in stem:
    kept_count += 1
    continue
```

---

## 7. 4명 전문가 토론 시스템

### 전문가 페르소나

| 전문가 | 스타일 | 분석 초점 |
|--------|-------|----------|
| 💼 **Fundamental Analyst** | Charlie Munger | 재무제표, 비즈니스 모델, 경쟁우위 |
| ⚠️ **Risk Manager** | Ray Dalio | 리스크 요인, 최악의 시나리오 |
| 🚀 **Growth Analyst** | Cathie Wood | 혁신, 성장 촉매, AI 전환 |
| 📊 **Sentiment Analyst** | George Soros | 시장 심리, 뉴스 톤, 과열 여부 |

### 토론 흐름

```
Round 1: Blind Analysis
├── 4명 전문가 독립 분석 (병렬)
└── 중재자: 합의점/쟁점 정리

Round 2-4: Guided Debate
├── 중재자 가이드 기반 데이터 중심 토론
├── 모든 전문가: get_news_detail 도구 사용 가능
└── 중재자: 추가 토론 필요 여부 판단

Final: Conclusion
├── 팟캐스트 대본 (줄글)
├── 구조화된 분석 (JSON)
└── sources 출력 (검증용)
```

### 뉴스 도구 (모든 에이전트)

```python
# 모든 전문가가 뉴스 상세 조회 가능
get_news_detail(news_id=8)
→ "Google started the year behind in the AI race..."
```

---

## 8. 최종 출력 형식

### 팟캐스트 대본 (줄글)

```
오늘 분석한 구글(Alphabet Inc.)에 대해 최종 결론을 말씀드리겠습니다.
최근 제출된 10-Q(2025-10-30)에 따르면 영업이익률이 30%를 유지하고 있고
약 480억 달러의 현금흐름을 기록했습니다...
```

**특징:**
- 전문가 역할명 없음 (Fundamental, Risk 등)
- 뉴스/공시 날짜 정확히 인용
- 바로 발표/영상에 사용 가능

### JSON 출력

```json
{
  "action": "BUY/HOLD/SELL",
  "position_size": 10,
  "debate_summary": "...",
  "buy_reasons": ["근거1 (출처, 날짜)", ...],
  "risk_factors": ["리스크1", ...],
  "immediate_action": "...",
  "short_term_strategy": "...",
  "long_term_strategy": "..."
}
```

---

## 9. 환경 설정

### 필수 환경변수 (.env)

```bash
# OpenAI API (필수) - GPT-5.1 사용
OPENAI_API_KEY=sk-...

# AWS (뉴스 수집용)
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=ap-northeast-2

# LangSmith (선택, 디버깅용)
LANGCHAIN_TRACING_V2=true
LANGCHAIN_PROJECT=stock-morning
LANGCHAIN_API_KEY=...

# SEC 크롤러 설정 (선택)
SEC_CRAWLER_WINDOW_DAYS=90  # 기본값: 90일 (10-K/10-Q는 무관)
```

---

## 10. 파일 구조

```
stock-morning/
├── run.py                            # 📌 메인 실행 스크립트
│
├── multiagent/                       # 4명 전문가 토론 시스템
│   ├── graph.py                      # LangGraph 파이프라인
│   ├── nodes/
│   │   └── data_collector.py         # 데이터 수집 + sources 생성
│   ├── agents/
│   │   ├── fundamental_analyst.py
│   │   ├── risk_manager.py
│   │   ├── growth_analyst.py
│   │   ├── sentiment_analyst.py
│   │   └── moderator.py
│   ├── services/
│   │   ├── toolkit.py                # GPT-5.1 API
│   │   └── conclusion_parser.py
│   ├── prompts.py                    # 프롬프트 (모든 에이전트 뉴스 도구 포함)
│   └── schemas.py
│
├── src/                              # 데이터 수집
│   ├── sec_crawler.py                # SEC 크롤러 (10-K/10-Q 항상 포함)
│   ├── db.py                         # SQLite (get_latest_annual_quarterly)
│   └── database/data_fetcher.py      # 데이터 조회 (10-K/10-Q 항상 포함)
│
├── aws_fetchers/                     # AWS 뉴스 수집
│   ├── yahoo_fetcher.py
│   └── news_saver.py
│
├── downloads/sec_filings/            # SEC 원문 파일 (영구 저장)
├── aws_results/                      # 뉴스 임시 파일 (분석 후 삭제)
├── sec_filings.db                    # SQLite DB
└── data/agent_results/               # 결과 JSON (sources 포함)
```

---

## 11. 실행 예시

```bash
# 전체 파이프라인
uv run run.py --ticker GOOG
```

**출력:**
```
====================================================================================================
🚀 STOCK MORNING - 통합 분석 파이프라인
📊 Ticker: GOOG
====================================================================================================

📥 SEC 크롤링: 7건 (10-K: ✅, 10-Q: ✅)
✅ 뉴스 수집: 10건
💰 현재 주가: $314.96

🎯 4-EXPERT DEBATE PIPELINE
├── Round 1: Blind Analysis
├── Round 2-4: Guided Debate
└── Final: 결론 도출

📋 FINAL CONCLUSION
────────────────────────────────────────
🔵 최종 판단: BUY
추천 포지션: 10%
────────────────────────────────────────

📚 참고 자료 (검증용)
  • SEC 공시: 7건 - 10-Q (2025-10-30), 10-K (2025-02-05)
  • 뉴스 기사: 10건
  • 시장 데이터: yfinance ($314.96)

💾 결과 저장 완료: data/agent_results/GOOG_20251228_154422_debate.json
🧹 뉴스 임시 파일 삭제: 10개

✨ PIPELINE COMPLETED (약 2분)
```
