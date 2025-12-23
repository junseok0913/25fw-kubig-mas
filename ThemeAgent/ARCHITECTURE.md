# Theme Agent 구조 정리

현재 설계된 ThemeAgent의 구성요소와 흐름을 요약합니다.  
(OpeningAgent/ARCHITECTURE.md와 동일한 스타일로, 날짜는 Orchestrator에서 CLI 인자로 전달)

---

## 1. 역할 및 목표

### 1-1. ThemeAgent의 역할

- 상위 Orchestrator에서 **OpeningAgent가 만든 결과**를 입력으로 받는다.
  - `nutshell`: 오늘 시장 한마디
  - `themes[]`: 1~3개의 핵심 테마 (headline / description / related_news)
  - `scripts`: 오프닝 대본 (진행자/해설자)
- 각 테마별로 **심층 분석 대본**을 생성한다. (ThemeWorkerAgent × N 병렬)
- 오프닝 대본과 테마별 대본을 **하나의 연속된 방송 대본**처럼 자연스럽게 이어지도록 재편집한다.
  - 테마 사이 전환 멘트
  - 톤/호흡 정리 (역할/팩트는 유지)
- 최종적으로 상위 `BriefingState.scripts`를 **Opening+Theme 완성본**으로 교체하고,  
  `current_section = "stock"`으로 넘겨서 다음 StockAgent 단계로 연결한다.

### 1-2. 입력 / 출력 (상위 Orchestrator 기준)

```python
class Theme(TypedDict):
    headline: str
    description: str
    related_news: list[dict]  # {"pk", "title", ...}

class ScriptTurn(TypedDict):
    speaker: str              # "진행자" | "해설자"
    text: str
    sources: list[dict]       # {"pk", "title", ...}

class BriefingState(TypedDict, total=False):
    date: str                 # YYYYMMDD, EST 기준
    user_tickers: list[str]
    nutshell: str
    themes: list[Theme]
    scripts: list[ScriptTurn]
    current_section: str      # "opening" | "theme" | "stock" | "closing" | "ending" | "citation"
```

- ThemeAgent 진입 시 (Opening 이후):
  - `current_section = "theme"`
  - `scripts`: **오프닝 대본**만 포함
- ThemeAgent 종료 시:
  - `scripts`: 오프닝 + 테마별 심층 대본을 포함한 **완성본**
  - `current_section = "stock"`

---

## 2. 최상위 Orchestrator와 ThemeAgent

### 2-1. 상위 LangGraph에서의 위치

```mermaid
flowchart LR
    subgraph ORCH["Orchestrator (orchestrator.py)"]
        O["OpeningAgent<br>state=opening"]
        T["ThemeAgent<br>state=theme"]
        S["StockAgent (계획)<br>state=stock"]
        E["EndingAgent (계획)<br>state=ending"]
        C["CitationAgent (계획)<br>state=citation"]
    end

    O --> T --> S --> E --> C --> END([END])
```

- Orchestrator는 `BriefingState`를 공유하면서  
  `opening → theme → stock → ending → citation` 순으로 하위 그래프를 호출한다.

### 2-2. theme 노드 구현 스케치

```python
from ThemeAgent.src import theme_agent

def theme_node(state: BriefingState) -> BriefingState:
    ta_graph = theme_agent.build_theme_graph()
    result = ta_graph.invoke({
        "date": state["date"],
        "nutshell": state["nutshell"],
        "themes": state["themes"],
        "base_scripts": state.get("scripts", []),
    })

    try:
        theme_agent.cleanup_cache()
    except Exception:
        # 캐시 삭제 실패는 치명적이지 않으므로 경고만 남긴다.
        ...

    return {
        **state,
        "scripts": result["scripts"],   # Opening+Theme 완성본
        "current_section": "stock",     # 다음은 StockAgent 단계
    }
```

---

## 3. ThemeAgent 상위 그래프 (ThemeGraph)

ThemeAgent는 내부적으로 **ThemeGraph + ThemeWorkerGraph × N** 구조를 가진다.

### 3-1. ThemeGraph 흐름

```mermaid
flowchart TD
    TG_START([START])
    SPLIT[split_themes<br/>Theme별 입력 분리]
    PAR[run_theme_workers<br/>ThemeWorkerGraph × N 병렬 실행]
    MERGE[merge_scripts<br/>Opening + Theme 스크립트 병합]
    REF[refine_transitions<br/>전환·톤 편집 LLM 호출]
    TG_END([END])

    TG_START --> SPLIT --> PAR --> MERGE --> REF --> TG_END
```

### 3-2. ThemeGraph 상태 스키마 (`ThemeState`)

```python
class ThemeState(TypedDict, total=False):
    # 공통 메타
    date: str
    nutshell: str

    # 입력 데이터
    themes: list[Theme]               # OpeningAgent에서 전달
    base_scripts: list[ScriptTurn]    # 오프닝 대본 (읽기 전용)

    # 중간 결과
    theme_scripts: list[list[ScriptTurn]]  # 각 ThemeWorker가 생성한 스크립트 묶음

    # 출력
    scripts: list[ScriptTurn]         # Opening+Theme 전체를 편집·정제한 최종 스크립트
```

### 3-3. ThemeGraph 노드별 역할

- **`split_themes`**
  - `themes[]`를 순회하며 각 ThemeWorker에 전달할 입력 패킷을 생성.
  - 예:
    ```python
    worker_inputs = [
        {
            "date": state["date"],
            "nutshell": state["nutshell"],
            "theme": theme,
            "base_scripts": state.get("base_scripts", []),
        }
        for theme in state.get("themes", [])
    ]
    ```

- **`run_theme_workers`**
  - 각 입력에 대해 `ThemeWorkerGraph`를 **병렬 실행**.
    - 예: `asyncio.gather(*(worker_graph.ainvoke(inp) for inp in worker_inputs))`
  - 결과를 `theme_scripts`에 저장:
    ```python
    theme_results = [...]  # [{"scripts": [...]}, ...]
    state["theme_scripts"] = [res["scripts"] for res in theme_results]
    ```

- **`merge_scripts`**
  - Opening 스크립트와 ThemeWorker 스크립트를 테마 순서대로 연결:
    ```python
    opening_scripts = state.get("base_scripts", [])
    merged: list[ScriptTurn] = []
    merged.extend(opening_scripts)
    for idx, theme_sc in enumerate(state.get("theme_scripts", [])):
        for turn in theme_sc:
            # 선택: section/theme_index 메타 추가 가능
            # turn["section"] = "theme"
            # turn["theme_index"] = idx
            merged.append(turn)
    state["scripts"] = merged
    ```

- **`refine_transitions` (Refiner)**
  - 역할:
    - `scripts` 전체를 보고,  
      Opening → Theme1, Theme1 → Theme2 등 **경계 지점**에서 전환이 부드럽도록  
      연결 문장/브릿지 멘트를 추가·수정.
    - 사실/수치/출처는 최대한 유지하고, 표현/호흡만 다듬는 **방송 대본 편집자** 역할.
  - 입력:
    - `ThemeState.scripts` (Opening+Theme 전체)
    - `themes`, `nutshell` (참고용)
  - 출력:
    ```json
    {
      "scripts": [
        {"speaker": "진행자", "text": "...", "sources": [...]},
        {"speaker": "해설자", "text": "...", "sources": [...]}
      ]
    }
    ```
  - Refiner는 새로운 배열을 반환하고, 이를 `ThemeState["scripts"]`에 덮어씀.

---

## 4. ThemeWorkerGraph (단일 Theme 워커 그래프)

각 ThemeWorker는 “한 개의 테마”에 대해 심층 분석 대본을 작성한다.

### 4-1. 전체 흐름

```mermaid
flowchart TD
    TW_START([START])
    PF[prefetch_news<br/>DynamoDB→뉴스 메타 캐시]
    CTX[load_context<br/>단일 테마 컨텍스트 구성]
    MSG[prepare_messages<br/>프롬프트 구성]
    AG[agent<br/>Tool 바인딩 LLM 호출]
    TL[tools<br/>뉴스/OHLCV Tool 실행]
    EX[extract_scripts<br/>해당 테마 스크립트 추출]
    TW_END([END])

    TW_START --> PF --> CTX --> MSG --> AG
    AG -->|tool_calls 있음| TL --> AG
    AG -->|tool_calls 없음| EX --> TW_END
```

### 4-2. ThemeWorker 상태 스키마 (`ThemeWorkerState`)

```python
class ThemeWorkerState(TypedDict, total=False):
    # 공통 메타
    date: str               # YYYYMMDD
    nutshell: str           # 오늘 시장 한마디

    # 단일 테마 입력
    theme: Theme            # 이 워커가 담당하는 단일 테마
    base_scripts: list[ScriptTurn]  # (선택) 오프닝 대본 전체, 참고용

    # ReAct용
    messages: Annotated[Sequence[BaseMessage], add_messages]
    theme_context: dict[str, Any]   # 이 테마에 대한 뉴스/지표 요약 JSON

    # 출력
    scripts: list[ScriptTurn]       # 이 테마에서 새로 생성된 턴들만 포함
```

### 4-3. Worker 노드별 역할

#### `prefetch_news`

- OpeningAgent의 `prefetch_news(today=...)`와 **동일한 시간 범위/쿼리 로직**으로 동작.
  - 입력: `date` (YYYYMMDD, EST 기준)
  - 처리:
    - `date` 기준 전일 16:00 ET ~ 당일 18:00 ET 범위를 계산.
    - DynamoDB `NEWS_TABLE`의 `gsi_latest_utc`를 쿼리하여 이 범위의 뉴스 메타데이터를 조회.
  - 출력(파일):
    - ThemeAgent 전용 캐시 디렉터리 (예시)
      ```text
      ThemeAgent/data/opening/
      ├── news_list.json   # DynamoDB 조회 결과 (메타데이터)
      ├── titles.txt       # 모든 뉴스 제목
      └── bodies/          # 본문 캐시 (Tool을 통해 온디맨드로 채워짐)
      ```
  - ThemeAgent 실행이 끝나면 `cleanup_cache()`에서 위 디렉터리를 삭제.

#### `load_context`

- 역할:
  - `theme`와 `related_news` 정보, 그리고 `prefetch_news`에서 준비한 로컬 캐시를 바탕으로
    **이 테마 전용 컨텍스트 JSON**을 구성.
  - 1차 구현 예:
    - `theme.headline`, `theme.description`
    - `related_news`의 제목/티커 요약
    - 필요 시 `get_news_content`로 일부 뉴스 본문을 조회하여 핵심 내용을 추출
    - 필요 시 `get_ohlcv`로 관련 지수/섹터/티커 추세 간단 요약
- 출력:
  - `state["theme_context"] = {...}` (JSON)  
    → 그대로 프롬프트에 주입.

#### `prepare_messages`

- 역할:
  - `ThemeAgent/prompt/theme_worker.yaml` / `ThemeAgent/prompt/theme_refine.yaml`를 로드.
  - 플레이스홀더 치환:
    - `{{date}}` → 한국어 날짜 (예: `"11월 25일"`)
    - `{{nutshell}}` → 오늘 시장 한마디
    - `{{theme}}` → 현재 단일 테마의 headline/description/related_news 요약
    - `{{theme_context}}` → 위에서 구성한 테마 컨텍스트 JSON
    - `{{base_scripts}}` → (선택) 오프닝 스크립트 전체, 맥락 전달용
    - `{{tools}}` → 바인딩된 Tool 목록/설명
  - 출력:
    - `state["messages"] = [SystemMessage(...), HumanMessage(...)]`

#### `agent` / `tools`

- `agent`:
  - `_build_llm()`로 OpenAI Chat 모델 생성 (OpeningAgent와 동일 패턴).
  - `llm.bind_tools(TOOLS)`로 뉴스/지표 Tool을 바인딩 후 `messages`를 입력으로 호출.
- `tools`:
  - `langgraph.prebuilt.ToolNode(TOOLS)` 사용.
  - Tool 목록은 아래 **5장 Tool 구조** 참조.
- 흐름:
  - `agent` → (tool_calls 존재 시) `tools` → 다시 `agent` (ReAct 루프).

#### `extract_scripts`

- 역할:
  - 마지막 `AIMessage.content`에서 ```json ... ``` 블록을 추출해 파싱.
  - 스키마 예:
    ```json
    {
      "scripts": [
        {
          "speaker": "진행자",
          "text": "...",
          "sources": [{"pk": "...", "title": "..."}]
        },
        {
          "speaker": "해설자",
          "text": "...",
          "sources": [{"pk": "...", "title": "..."}]
        }
      ]
    }
    ```
- 출력:
  - `state["scripts"] = parsed["scripts"]`  
    (해당 Theme에 대한 스크립트만 포함).

---

## 5. Tool 및 캐시 구조

### 5-1. Tool 목록 (OpeningAgent와 공통)

ThemeAgent에서도 OpeningAgent의 Tool 세트를 그대로 바인딩한다.

| Tool | 설명 |
|------|------|
| `get_news_list` | 로컬 캐시된 뉴스 목록 필터링 (tickers, keywords) |
| `get_news_content` | S3에서 뉴스 본문 조회 또는 로컬 캐시 반환 |
| `list_downloaded_bodies` | 로컬에 저장된 본문 파일 목록 반환 |
| `count_keyword_frequency` | 제목/본문에서 키워드 출현 빈도 계산 |
| `get_ohlcv` | yfinance로 과거 OHLCV 데이터 조회 |

- 구현 시에는 `OpeningAgent/src/tools/*.py`를 재사용하되,  
  캐시 경로만 ThemeAgent용 디렉터리로 맞추는 식으로 정리.

### 5-2. ThemeAgent 캐시 디렉터리 (예시)

```text
ThemeAgent/data/theme1/
├── news_list.json          # DynamoDB 조회 결과 (prefetch_news)
├── titles.txt              # 모든 뉴스 제목 (prefetch_news)
└── bodies/                 # get_news_content가 온디맨드로 채우는 본문 캐시
    ├── h#abcdef01.txt
    ├── h#12345678.txt
    └── ...
ThemeAgent/data/theme2/
├── news_list.json          # DynamoDB 조회 결과 (prefetch_news)
├── titles.txt              # 모든 뉴스 제목 (prefetch_news)
└── bodies/                 # get_news_content가 온디맨드로 채우는 본문 캐시
    ├── h#abcdef01.txt
    ├── h#12345678.txt
    └── ...
...
```

- ThemeAgent는 실행이 끝난 후 `cleanup_cache()`에서 위 디렉터리 및 임시 파일들을 삭제한다.

---

## 6. State 요약 테이블

| 이름 | 사용 위치 | 주요 필드 | 비고 |
|------|-----------|-----------|------|
| `BriefingState` | Orchestrator | `date`, `user_tickers`, `nutshell`, `themes`, `scripts`, `current_section` | 상위 공용 State |
| `OpeningState` | OpeningAgent 내부 | `date`, `messages`, `context_json`, `news_meta`, `themes`, `nutshell`, `scripts` | OpeningAgent 전용 (이미 구현) |
| `ThemeState` | ThemeGraph | `date`, `nutshell`, `themes`, `base_scripts`, `theme_scripts`, `scripts` | ThemeAgent 상위 그래프 State |
| `ThemeWorkerState` | ThemeWorkerGraph | `date`, `nutshell`, `theme`, `base_scripts`, `messages`, `theme_context`, `scripts` | 단일 Theme 워커용 State |

---

## 7. 설정 및 환경변수 (공통)

OpeningAgent와 동일한 환경변수 세트를 사용한다.

- OpenAI:
  - `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_REASONING_EFFORT`, `OPENAI_TEMPERATURE`
- AWS/SSO:
  - `AWS_SDK_LOAD_CONFIG=1`
  - `AWS_PROFILE`, `AWS_REGION`
  - `NEWS_TABLE`, `NEWS_BUCKET`
- 기타:
  - LangSmith/LangChain 추적: `LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`, `LANGCHAIN_PROJECT`, `LANGCHAIN_ENDPOINT`(선택)

---

## 8. 정리

- ThemeAgent는 **Opening 이후 Theme 섹션 전체**를 책임지는 에이전트로,
  - ThemeWorkerGraph × N 병렬 실행으로 각 테마별 심층 대본 생성,
  - Refiner LLM으로 Opening+Theme 전체 스크립트의 전환과 톤을 다듬는다.
- 각 실행은 ThemeAgent 전용 캐시 디렉터리를 사용하며,  
  종료 시 `cleanup_cache()`를 통해 캐시를 정리하는 구조로 OpeningAgent와 패턴을 맞춘다.
