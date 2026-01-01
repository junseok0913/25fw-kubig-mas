# 📈 Stock Morning

**4명의 AI 전문가가 토론하는 주식 분석 파이프라인**

SEC 공시와 최신 뉴스를 자동으로 수집하고, 4명의 AI 전문가가 서로 다른 관점에서 토론한 뒤 투자 결론을 도출합니다. 최종 결과는 **팟캐스트 대본 형식**으로 출력되어 바로 영상/발표에 활용할 수 있습니다.

## 🎯 주요 기능

### 📥 데이터 수집
- **SEC EDGAR 크롤러**: 10-K, 10-Q는 항상 포함 + 최근 N일 공시 (8-K, Form 4 등)
- **Yahoo Finance 뉴스**: AWS DynamoDB에서 최신 뉴스 10건 수집
- **실시간 주가**: yfinance 통합 (P/E, ROE, 부채비율 등 30+ 지표)

### 🤖 4명 전문가 토론 시스템
| 전문가 | 스타일 | 역할 |
|--------|--------|------|
| 💼 **Fundamental Analyst** | Charlie Munger | 재무제표와 비즈니스 모델 평가 |
| ⚠️ **Risk Manager** | Ray Dalio | 리스크 요인과 최악의 시나리오 분석 |
| 🚀 **Growth Analyst** | Cathie Wood | 혁신과 성장 촉매 발굴 |
| 📊 **Sentiment Analyst** | George Soros | 시장 심리와 뉴스 분석 |

### 📝 출력 형식
- **팟캐스트 대본**: 자연스러운 줄글로 바로 영상/발표에 사용 가능
- **구조화된 분석**: 매수 근거, 리스크, 실행 전략
- **JSON 결과**: 검증 에이전트용 sources 포함

## 🚀 빠른 시작

### 1. 설치

```bash
git clone https://github.com/YOUR_USERNAME/stock-morning.git
cd stock-morning

# UV (권장)
uv sync
```

### 2. 환경변수 설정

`.env` 파일 생성:

```bash
# OpenAI API (필수)
OPENAI_API_KEY=sk-...

# AWS (뉴스 수집용)
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=ap-northeast-2

# LangSmith 추적 (선택)
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=...
LANGCHAIN_PROJECT=stock-morning
```

### 3. 실행

```bash
# 전체 파이프라인 (크롤링 + 분석 + JSON 저장)
uv run run.py --ticker GOOG

# 크롤링 생략 (기존 데이터 사용)
uv run run.py --ticker GOOG --skip-crawl

# 결과 JSON 저장 안 함
uv run run.py --ticker GOOG --no-save
```

## 📊 실행 결과 예시

```
====================================================================================================
🚀 STOCK MORNING - 통합 분석 파이프라인
📊 Ticker: GOOG
====================================================================================================

📥 SEC 크롤링: 7건 (10-K: ✅, 10-Q: ✅)
✅ 뉴스 수집: 10건

🎯 4-EXPERT DEBATE PIPELINE
├── Round 1: Blind Analysis (각 전문가 독립 분석)
├── Round 2-4: Guided Debate (중재자 가이드 기반 토론)
└── Final: 최종 결론 도출

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

✨ PIPELINE COMPLETED
```

## 📁 프로젝트 구조

```
stock-morning/
├── run.py                    # 📌 메인 실행 스크립트
├── multiagent/               # 4명 전문가 토론 시스템
│   ├── graph.py              # LangGraph 파이프라인
│   ├── agents/               # 4명 전문가 + 중재자
│   │   ├── fundamental_analyst.py
│   │   ├── risk_manager.py
│   │   ├── growth_analyst.py
│   │   ├── sentiment_analyst.py
│   │   └── moderator.py
│   ├── nodes/
│   │   └── data_collector.py # 데이터 수집 + sources 생성
│   ├── services/
│   │   └── toolkit.py        # LLM 호출 (GPT-5.1)
│   ├── prompts.py            # 프롬프트 템플릿
│   └── schemas.py            # Pydantic 스키마
├── src/                      # 데이터 수집
│   ├── sec_crawler.py        # SEC EDGAR 크롤러
│   ├── db.py                 # SQLite 관리
│   └── database/
│       └── data_fetcher.py   # 데이터 조회
├── aws_fetchers/             # AWS 뉴스 수집
│   ├── yahoo_fetcher.py
│   └── news_saver.py
├── downloads/
│   └── sec_filings/          # SEC 공시 원문 (영구 저장)
└── data/
    └── agent_results/        # 분석 결과 JSON (sources 포함)
```

## 🔧 기술 스택

| 카테고리 | 기술 |
|----------|------|
| **LLM** | OpenAI GPT-5.1 |
| **오케스트레이션** | LangGraph |
| **데이터베이스** | SQLite |
| **SEC 데이터** | SEC EDGAR API |
| **뉴스** | Yahoo Finance (AWS DynamoDB) |
| **주가 데이터** | yfinance |
| **추적** | LangSmith (선택) |

## 📋 출력 스키마 (검증 에이전트용)

결과 JSON에 포함되는 `sources` 필드:

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
        "accession_number": "0001652044-25-000091",
        "file_path": "downloads/sec_filings/..."
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
        "current_price": 314.96
      }
    ]
  }
}
```

## 📝 커맨드 옵션

```bash
uv run run.py --help

옵션:
  --ticker TICKER     분석할 티커 (필수)
  --skip-crawl        SEC 크롤링 생략
  --crawl-only        크롤링만 실행
  --no-save           결과 JSON 저장 안 함 (기본: 저장)
  --output-dir DIR    저장 디렉토리 (기본: data/agent_results)
```

## 📄 라이선스

MIT License
