# `debate/`: 티커 단위 멀티에이전트 디베이트 (프로토타입)

`debate/`는 “티커 1개 → 4인 토론(라운드) → 중재자 결론”을 **독립 실행**할 수 있는 디베이트 전용 모듈입니다.  
최종 산출물은 **대본이 아니라**, 이후 `script_writer` 단계에 컨텍스트로 주입하기 위한 **Debate JSON 아티팩트**입니다.

구현은 `agents/debate/graph.py`에 있으며, `debate/graph.py`는 기존 실행/임포트를 위한 호환용 wrapper입니다.

자세한 그래프/툴 스키마/캐시 원리는 `debate/ARCHITECTURE.md`를 참고하세요.

## 출력(JSON) 스키마

```json
{
  "ticker": "GOOG",
  "date": "20251222",
  "rounds": [
    {
      "round": 1,
      "fundamental": { "text": "…", "action": "BUY|HOLD|SELL", "confidence": 0.0, "sources": [/* Source[] */] },
      "risk": { "text": "…", "action": "BUY|HOLD|SELL", "confidence": 0.0, "sources": [/* Source[] */] },
      "growth": { "text": "…", "action": "BUY|HOLD|SELL", "confidence": 0.0, "sources": [/* Source[] */] },
      "sentiment": { "text": "…", "action": "BUY|HOLD|SELL", "confidence": 0.0, "sources": [/* Source[] */] }
    }
  ],
  "conclusion": {
    "text": "…(중재자 결론, 대본 톤 금지)…",
    "action": "BUY|HOLD|SELL",
    "confidence": 0.0
  }
}
```

`rounds[*].{role}.sources`는 프로젝트의 `ScriptTurn.sources`와 동일 스키마(`article|chart|event|sec_filing`)를 사용합니다.

## 프롬프트 파일

- Debate(전문가/중재자): `agents/debate/prompt/debate_main.yaml` (직접 로드: `agents/debate/graph.py`)
- Ticker Script(Worker/Refiner): `debate/prompt/ticker_script_worker.yaml`, `debate/prompt/ticker_script_refine.yaml`

## 실행 방법

### 1) 가장 간단한 실행(권장)

`OPENAI_API_KEY`가 설정되어 있어야 합니다.
비밀이 아닌 설정(모델/라운드/timeout 등)은 `config/app.yaml`로 관리할 수 있습니다. (비밀키는 `.env` 권장)
SEC EDGAR를 호출하는 경우 `SEC_USER_AGENT` 설정을 권장합니다.
`SEC_USER_AGENT`가 없으면 SEC 공시 목록은 자동으로 비워진 채로 진행됩니다.

```bash
python -m debate.graph 20251222 GOOG
```

- 기본값으로 `cache/{date}/news_list.json`이 없으면 `prefetch_all()`을 실행해 캐시를 채우려고 시도합니다.
- AWS/DynamoDB/S3 접근 권한이 없다면 이 단계에서 실패할 수 있습니다.
- 라운드는 **env로 관리**합니다:
  - `DEBATE_MIN_ROUNDS` (최소 2라운드 강제)
  - `DEBATE_MAX_ROUNDS` (최대 라운드)
  - `DEBATE_CONSENSUS_CONFIDENCE` (합의 임계치)
  - `--max-rounds N`을 주면 `DEBATE_MAX_ROUNDS`를 임시로 오버라이드합니다.

### 2) 캐시가 이미 있을 때(네트워크/AWS 최소화)

```bash
python -m debate.graph 20251222 GOOG --max-rounds 2 --no-prefetch
```

필수 파일:
- `cache/20251222/news_list.json` (뉴스 메타데이터)

선택(있으면 S3 호출 회피):
- `cache/20251222/bodies/{pk}.txt` (뉴스 본문 캐시)

> 참고: LLM이 `get_news_content()`를 호출할 수 있으므로, AWS S3를 쓰지 않으려면 bodies 캐시를 미리 만들어 두는 편이 안전합니다.

SEC 캐시(자동 생성):
- `cache/{date}/sec/company_tickers.json`
- `cache/{date}/sec/submissions_CIK{cik}.json`
- `cache/{date}/sec/filings_full/{ticker}_{accessionNoDash}.txt` (LLM 입력용 정리/절단 원문)
- `cache/{date}/sec/filings_index/{ticker}_{accessionNoDash}.json` (페이지별 요약 index)

### 3) 도움말

```bash
python -m debate.graph --help
```

## 테스트/검증(로컬)

### 파이썬 컴파일 체크

```bash
python -m py_compile debate/graph.py debate/types.py agents/debate/graph.py agents/debate/types.py
```

### LLM 프로파일(선택)

`agents/debate/graph.py`는 아래 prefix로 LLM 설정을 읽습니다. (`debate/graph.py`는 호환용 wrapper)
- 전문가: `DEBATE_FUNDAMENTAL`, `DEBATE_RISK`, `DEBATE_GROWTH`, `DEBATE_SENTIMENT`
- 중재자: `DEBATE_MODERATOR`

권장(현재 프로젝트 기본값):
- 전문가/중재자: `gpt-5.1` + `OPENAI_REASONING_EFFORT=low` (thinking low)
- SEC filing 페이지 요약(index 생성): `SEC_PAGE_SUMMARY_MODEL=gpt-5-mini` (thinking none)

예:
```bash
export DEBATE_FUNDAMENTAL_OPENAI_MODEL="gpt-5.1"
export DEBATE_FUNDAMENTAL_OPENAI_REASONING_EFFORT="low"
export DEBATE_MODERATOR_OPENAI_MODEL="gpt-5.1"
export DEBATE_MODERATOR_OPENAI_REASONING_EFFORT="low"
```

## 캐시를 직접 만들고 싶다면(최소 형태)

`get_news_list()`는 `cache/{date}/news_list.json`을 읽습니다. 최소한 아래 필드는 있어야 합니다.

```json
{
  "articles": [
    {
      "pk": "id#example",
      "title": "Some headline",
      "tickers": ["GOOG"],
      "path": "s3/object/key/optional"
    }
  ]
}
```

`get_news_content()`가 S3를 호출하지 않게 하려면:
- `cache/{date}/bodies/id#example.txt` 파일을 만들어 두세요.

## SEC 환경변수(권장)

SEC는 User-Agent를 요구합니다.

```bash
export SEC_USER_AGENT="your-name-or-org (contact: email@example.com)"
```

SEC 공시는 먼저 `get_sec_filing_content(... )`처럼 **page를 생략**해 `index`(페이지 요약/목차)만 받고, 필요한 페이지만 `page=...`로 content를 조회하는 방식을 권장합니다. (페이지 크기 기본 20000자)

## UserTicker 스크립트(Worker/Refiner) 실행

`debate/ticker_script.py`는 “(오프닝+테마) 누적 대본 + (티커별 debate JSON) → 티커별 대본 생성(병렬) → 병합 → 전환 refiner”를 실행합니다.

```bash
# base scripts는 기본으로 temp/theme.json을 사용합니다.
# --debate-json은 tickers와 같은 순서/개수로 전달합니다.
python -m debate.ticker_script 20251222 GOOG AAPL --debate-json out/GOOG_debate.json out/AAPL_debate.json
```

> `--debate-json`을 생략하면 내부에서 `run_debate()`를 호출해 debate까지 같이 실행합니다. (비용/시간 주의)
