# Opening Agent 구조 정리

현재 구현된 오프닝 에이전트의 구성요소와 흐름을 요약합니다. (날짜는 CLI 인자로 전달, SSO 프로파일 기반)

## LangGraph 실행 플로우 (ReAct 패턴)

```mermaid
flowchart TD
    START([START])
    PF[prefetch_news<br/>DynamoDB 뉴스 + TE 캘린더 프리페치]
    LC[load_context<br/>context_today로 갱신 후 market_context.json 로드]
    PM[prepare_messages<br/>시스템/사용자 메시지 준비]
    AG[agent<br/>Tool 바인딩된 LLM 호출]
    TL[tools<br/>ToolNode로 Tool 실행]
    EX[extract_script<br/>최종 대본 추출]
    END([END])

    START --> PF --> LC --> PM --> AG
    AG -->|tool_calls 있음| TL
    TL --> AG
    AG -->|tool_calls 없음| EX --> END
```

- 상태 스키마: `OpeningState = { date, messages, context_json, news_meta, themes, nutshell, scripts }`
- 프리페치: `prefetch_news` 노드에서
  - DynamoDB 뉴스 메타 프리페치(`prefetch_news`)로 `data/opening/news_list.json`/`titles.txt`/`bodies/`를 준비하고,
  - TradingEconomics 경제 캘린더 프리페치(`prefetch_calendar`)로 `data/opening/calendar.csv`/`calendar.json`을 생성한다. (실패 시 경고 후 계속)
- 컨텍스트 로딩/보강: `context_today`를 실행해 `data/market_context.json`을 갱신 후 읽고,
  `data/opening/titles.txt`에서 상위 단어 빈도(`title_top_words`)를 계산한다.
- 메시지 준비: `prompt/opening_main.yaml`을 로드해
  - `{context_json}`: market context (title_top_words 제외)
  - `{title_top_words}`: 별도 JSON
  - `{calendar_context}`: 캘린더 TSV(id, est_date, title)
  를 주입해 초기 메시지 생성.
- ReAct 루프: `agent` 노드에서 Tool 바인딩된 LLM 호출 → `tool_calls`가 있으면 `tools` 노드에서 실행 → 다시 `agent` 호출 (반복).
- 대본 추출: ReAct 루프가 끝나면 최종 AIMessage에서 대본 추출.
- 종료 처리: 대본 생성 후 `data/market_context.json`, `data/opening`의 뉴스/캘린더 캐시, `data/_tmp_csv` 임시 파일을 삭제.

## Tool 및 유틸 구조 (src/)

```mermaid
flowchart LR
    subgraph tools["tools (@tool 데코레이터)"]
        GL[get_news_list]
        GC[get_news_content]
        LB[list_downloaded_bodies]
        KF[count_keyword_frequency]
        CA[get_calendar]
        OH[get_ohlcv]
    end
    subgraph utils
        AU[AWS 세션 유틸<br/>utils/aws_utils.py]
    end
    subgraph prefetch
        PF[prefetch_news<br/>DynamoDB 뉴스 + TE 캘린더]
    end
    LLM[ChatOpenAI<br/>bind_tools]
    LLM -->|Tool 호출| tools
    PF --> GL
    PF --> GC
    PF --> LB
    GC -->|S3 본문| KF
    AU --> PF
    AU --> GC
```

### Tool 목록 (LLM에 바인딩됨)

| Tool | 설명 |
|------|------|
| `get_news_list` | 로컬 캐시된 뉴스 목록 필터링 (tickers, keywords) |
| `get_news_content` | S3에서 뉴스 본문 조회 또는 로컬 캐시 반환 (LLM 입력용: 태그 제거/길이 절단) |
| `list_downloaded_bodies` | 로컬에 저장된 본문 파일 목록 반환 |
| `count_keyword_frequency` | 제목/본문에서 키워드 출현 빈도 계산 |
| `get_calendar` | 프리페치된 캘린더에서 이벤트 상세 조회(id/date) |
| `get_ohlcv` | yfinance로 과거 OHLCV 데이터 조회 (행 수 제한/반올림 포함) |

- `src/prefetch.py`:
  - `prefetch_news`: 지정 날짜 기준 전일 16:00 ET ~ 당일 18:00 ET, 최근 3일 `gsi_utc_pk` 파티션을 DynamoDB `gsi_latest_utc`로 쿼리 → `data/opening/news_list.json`, `titles.txt`, `bodies/`.
  - `prefetch_calendar`: TradingEconomics 캘린더를 ET 기준 윈도우로 스크래핑 → `data/opening/calendar.csv`, `calendar.json`.
- `src/tools/news_tools.py`: `@tool` 데코레이터로 LangChain Tool 정의. 로컬 캐시 조회/필터링, S3 본문 다운로드+캐시, 다운로드된 본문 목록, 키워드 빈도 분석.
- `src/tools/calendar_tools.py`: `@tool` 데코레이터로 캘린더 상세 조회(id/date)를 제공.
- `src/tools/ohlcv.py`: `@tool` 데코레이터로 LangChain Tool 정의. yfinance 래퍼로 OHLCV 조회.
  - rows가 200개를 초과하면 `rows=[]`와 함께 “기간을 줄이거나 interval을 키우라”는 안내를 반환.
  - open/high/low/close는 소수점 3자리로 반올림해 반환.
- `src/utils/aws_utils.py`: SSO 프로파일 기반 boto3 세션/클라이언트 헬퍼.

## 데이터 경로

- `data/market_context.json`: 시장 지표 컨텍스트 (context_today.py로 생성).
- `data/opening/news_list.json`, `titles.txt`, `bodies/*.txt`: 프리페치/본문 캐시.
- `data/opening/calendar.csv`, `data/opening/calendar.json`: TradingEconomics 경제 캘린더 캐시.

## 설정 및 추적

- OpenAI: `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_REASONING_EFFORT`(추론 모델일 때만), `OPENAI_TEMPERATURE`, `OPENAI_TIMEOUT`, `OPENAI_MAX_RETRIES`.
  - OpeningAgent 오버라이드: `OPENING_OPENAI_*`
- AWS/SSO: `AWS_SDK_LOAD_CONFIG=1`, `AWS_PROFILE=Admins`, `AWS_REGION`, `NEWS_TABLE`, `NEWS_BUCKET`.
- Tool 출력 제한: `NEWS_BODY_MAX_CHARS` (get_news_content가 LLM 입력용 body를 절단)
- LangSmith(LangChain tracing_v2): `LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`, `LANGCHAIN_PROJECT`, `LANGCHAIN_ENDPOINT`(선택).

## 테스트

- `test/test_prefetch.py`: 가짜 DynamoDB로 프리페치 결과와 파일 생성 확인.
- `test/test_news_tools.py`: 뉴스 필터/본문 캐시/S3 목업/키워드 빈도 검사.
- `test/test_ohlcv.py`: yfinance 목업으로 OHLCV 응답 포맷 검사.
