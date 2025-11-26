# Opening Agent 구조 정리

현재 구현된 오프닝 에이전트의 구성요소와 흐름을 요약합니다. (TODAY=20251125, SSO 프로파일 기반)

## LangGraph 실행 플로우

```mermaid
flowchart TD
    START([START])
    PF[prefetch_news<br/>DynamoDB→news_list/titles 캐시]
    LT[load_tools<br/>Tool 레지스트리 적재]
    LC[load_context<br/>context_today로 갱신 후 market_context.json 로드]
    DS[draft_script<br/>프롬프트로 LLM 호출]
    END([END])

    START --> PF --> LT --> LC --> DS --> END
```

- 상태 스키마: `OpeningState = { news_meta, context_json, tools, script_markdown }`
- 프리페치: `prefetch_news`로 DynamoDB `gsi_latest_utc` 범위를 조회해 `data/opening/news_list.json`/`titles.txt`/`bodies/`를 준비(실패 시 경고 후 계속).
- 도구 로딩: `TOOL_REGISTRY`를 상태에 주입하여 후속 노드에서 참조 가능하도록 준비.
- 컨텍스트 로딩: `context_today`를 실행해 `data/market_context.json`을 갱신 후 읽어 `context_json`에 넣음.
- 대본 생성: `prompt/opening_script.yaml`의 system/user 템플릿과 컨텍스트로 LLM 호출. LangSmith 트레이싱은 `LANGCHAIN_TRACING_V2=true` 시 활성화.
- 종료 처리: 대본 생성 후 `data/market_context.json`, `data/opening`의 뉴스 캐시, `data/_tmp_csv` 임시 파일을 삭제.

## Tool 및 유틸 구조 (src/)

```mermaid
flowchart LR
    subgraph tools
        GL[get_news_list]
        GC[get_news_content]
        LB[list_downloaded_bodies]
        KF[count_keyword_frequency]
        OH[get_ohlcv]
    end
    subgraph utils
        AU[AWS 세션 유틸<br/>utils/aws_utils.py]
    end
    subgraph prefetch
        PF[prefetch_news<br/>DynamoDB → news_list.json/titles.txt]
    end
    PF --> GL
    PF --> GC
    PF --> LB
    GC -->|S3 본문| KF
    AU --> PF
    AU --> GC
```

- `src/prefetch.py`: TODAY 기준 전일 16:00 ET ~ 당일 18:00 ET, 최근 3일 `gsi_utc_pk` 파티션을 DynamoDB `gsi_latest_utc`로 쿼리 → `data/opening/news_list.json`, `titles.txt`, `bodies/`.
- `src/tools/news_tools.py`: 로컬 캐시 조회/필터링, S3 본문 다운로드+캐시, 다운로드된 본문 목록, 키워드 빈도 분석.
- `src/tools/ohlcv.py`: yfinance 래퍼로 OHLCV 조회.
- `src/utils/aws_utils.py`: SSO 프로파일 기반 boto3 세션/클라이언트 헬퍼.

## 데이터 경로

- `data/market_context.json`: 시장 지표 컨텍스트 (context_today.py로 생성).
- `data/opening/news_list.json`, `titles.txt`, `bodies/*.txt`: 프리페치/본문 캐시.

## 설정 및 추적

- OpenAI: `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_REASONING_EFFORT`, `OPENAI_TEMPERATURE`.
- AWS/SSO: `AWS_SDK_LOAD_CONFIG=1`, `AWS_PROFILE=Admins`, `AWS_REGION`, `NEWS_TABLE`, `NEWS_BUCKET`, `TODAY`.
- LangSmith(LangChain tracing_v2): `LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`, `LANGCHAIN_PROJECT`, `LANGCHAIN_ENDPOINT`(선택).

## 테스트

- `test/test_prefetch.py`: 가짜 DynamoDB로 프리페치 결과와 파일 생성 확인.
- `test/test_news_tools.py`: 뉴스 필터/본문 캐시/S3 목업/키워드 빈도 검사.
- `test/test_ohlcv.py`: yfinance 목업으로 OHLCV 응답 포맷 검사.
