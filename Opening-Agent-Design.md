# 오프닝 에이전트 설계

> 장마감 브리핑의 **오프닝 파트**를 담당하는 에이전트 아키텍처 설계 문서

---

## 오프닝 파트 개요

### 목표

- 당일 수집된 뉴스 기사들을 분석하여 **1~3개의 핵심 테마** 도출
- 시장을 대표하는 **한마디** 포함할 것
- **진행자-해설자 대화 형식**의 오프닝 대본 작성

### 뉴스 수집 시간 범위

<aside>
⏰

**시작**: 전날 장마감 시간 **EST 16:00**
**종료**: 금일 장마감 + 90분 **EST 17:30**

e.g. 11/19 브리핑 → EST 11/18 16:00 ~ EST 11/19 17:30 (KST 11/20 07:30)

</aside>

### 입력 데이터

- **뉴스 기사**: DynamoDB (`kubig-YahoofinanceNews`)에서 위 시간 범위의 뉴스 메타데이터 조회
- **뉴스 본문**: S3 버킷에서 XML 파일로 저장된 기사 본문
- **주가 지수**: S&P 500, Nasdaq, Dow Jones 등 당일 데이터

### 출력 (예시, 더 많은 멀티턴도 가능)

**[진행자]**

10월 31일, 장 마감 브리핑입니다. 오늘 시장은 **한마디로 ‘AI의 힘’과 ‘연준의 경고’가 정면으로 충돌**한 하루였습니다. 아마존과 구글이 AI를 등에 업고 폭발적인 클라우드 성장을 발표하며 기술주 랠리를 이끌었는데요. 하지만 동시에 연준 위원들은 인플레이션을 이유로 금리 인하에 반대 목소리를 높였습니다. 이 상반된 두 힘이 시장을 어떻게 움직였는지, 지금부터 집중 분석합니다.


**[해설자]**

네, 오늘 시장의 가장 큰 동력은 단연 AI였습니다. 특히 클라우드 부문에서 AI가 어떻게 실질적인 매출로 이어지는지가 명확히 드러났습니다. Amazon Web Services, 즉 AWS는 3분기 매출이 330억 달러로 전년 대비 20%나 증가했습니다. 이는 2022년 말 이후 가장 높은 성장률로, 시장의 예상을 뛰어넘는 수치입니다.

---

## 에이전트 아키텍처

### 전체 흐름

```
┌─────────────────────────────────────────────────────────────┐
│                    Opening Agent Orchestrator               │
├─────────────────────────────────────────────────────────────┤
│  1. 뉴스 수집 → 2. 테마 분석 → 3. 한마디 정하기 → 4. 대본 작성    │
└─────────────────────────────────────────────────────────────┘
```

### 단계별 프로세스

- **Step 1: 뉴스 데이터 수집**
    - DynamoDB GSI (`gsi_latest_utc`)를 통해 **전날 EST 16:00 ~ 금일 EST 17:30** 범위의 뉴스 목록 조회
- **Step 2: 테마/키워드 분석**
    - **에이전트가 뉴스 제목들을 읽고 직접 키워드 후보를 추론**
    - `count_keyword_frequency` Tool로 키워드 빈도 검증 (title 전체 / 개별 본문)
    - 빈도 분석 결과를 바탕으로 상위 1~3개 테마 선정
- **Step 3: 한마디 정하기**
    - 테마 분석 결과를 바탕으로 5가지 유형 중 선택:
        - 단일 테마 집중형
        - 원인-결과형
        - 국면 전환형
        - 질문형
        - A vs B 대립형
- **Step 4: 대본 작성**
    - 진행자: 전체 흐름 소개, 청취자 친화적 언어, 해설자에게 질문
    - 해설자: 전문적 분석, 데이터 기반 설명

---

## 필요 Tool 목록

### 0. 사전 데이터 수집 (Pre-fetch)

<aside>
🔄

**에이전트 실행 전 데이터 준비 (뉴스 메타데이터만)**

에이전트 실행 전에 **뉴스 리스트와 제목**만 사전 다운로드됩니다. **본문은 에이전트가 필요할 때 온디맨드로 S3에서 조회**합니다.

</aside>

<aside>
⚡

**DynamoDB 사전 조회 (Pre-fetch Script)**

- **파티션 키**(`gsi_utc_pk`): 최근 3일 파티션만 지정 (`UTC#2025-11-18`, `UTC#2025-11-19`, ...)
- **정렬 키**(`utc_ms`): `BETWEEN start_ms AND end_ms` 조건으로 **정밀한 시간 범위 필터링**
- 결과를 `data/opening/news_list.json` 및 `titles.txt`에 저장
- **본문(`bodies/`)은 사전 다운로드하지 않음**
</aside>

### 1. 뉴스 데이터 조회 Tools

<aside>
📂

**하이브리드 조회 방식**

- **뉴스 리스트/제목**: 사전 다운로드된 로컬 파일에서 조회
- **뉴스 본문**: 에이전트가 선택한 기사만 **S3에서 실시간 조회** → context 반환 + 로컬 캐싱
</aside>

| **Tool Name** | **기능** | **상세 설명** |
| --- | --- | --- |
| `get_news_list` | 뉴스 목록 조회 + 필터링 | `news_list.json`에서 조회. **티커/키워드 필터링** 옵션 지원 |
| `get_news_content` | **S3에서** 뉴스 본문 조회 | 선택한 기사 본문을 **S3에서 실시간 조회** → context 반환 + `bodies/{pk}.txt`에 캐싱 |

### 2. 테마/키워드 분석 Tools

<aside>
🧠

**설계 원칙**: 키워드 추출 및 테마 도출은 **에이전트가 스스로 추론**합니다. Tool은 에이전트가 생각한 키워드의 **빈도 검증**만 담당합니다.

</aside>

<aside>
📂

**로컬 파일 기반 분석**: `count_keyword_frequency`는 **LLM이 텍스트를 직접 전달하지 않고**, `data/opening/`에 저장된 로컬 파일에서 키워드를 카운트합니다. 에이전트는 키워드 리스트만 전달하면 됩니다.

</aside>

| **Tool Name** | **기능** | **상세 설명** |
| --- | --- | --- |
| `list_downloaded_bodies` | 다운로드된 본문 목록 조회 | `data/opening/bodies/`에 저장된 기사들의 **pk와 title 목록** 반환. 에이전트가 특정 기사 선택 시 활용 |
| `count_keyword_frequency` | 키워드 빈도 분석 | **로컬 저장된 파일**(`titles.txt`, `bodies/*.txt`)에서 키워드 출현 빈도 계산. LLM이 텍스트를 던지지 않음 |

### 3. 당일 시장 데이터 (Context 제공)

<aside>
📊

**당일 마감 데이터는 Tool이 아닌 Context로 제공**

에이전트 실행 시 아래 지표들의 당일 데이터가 context에 포함됩니다. **지표 특성에 따라 제공 데이터가 다릅니다.**

</aside>

**제공 지표 목록 (총 18개):**

- **주가지수** (6개)
    
    
    | **티커** | **지표명** | **제공 데이터** |
    | --- | --- | --- |
    | ^GSPC | S&P 500 | 시가, 고가, 저가, 종가, **등락폭(pt)**, **등락률(%)** |
    | ^IXIC | Nasdaq Composite | 시가, 고가, 저가, 종가, **등락폭(pt)**, **등락률(%)** |
    | ^NDX | Nasdaq 100 | 시가, 고가, 저가, 종가, **등락폭(pt)**, **등락률(%)** |
    | ^DJI | Dow Jones Industrial | 시가, 고가, 저가, 종가, **등락폭(pt)**, **등락률(%)** |
    | ^RUT | Russell 2000 | 시가, 고가, 저가, 종가, **등락폭(pt)**, **등락률(%)** |
    | ^NYA | NYSE Composite | 시가, 고가, 저가, 종가, **등락폭(pt)**, **등락률(%)** |
- **채권 (국채 수익률)** (3개)
    
    
    | **티커** | **지표명** | **제공 데이터** |
    | --- | --- | --- |
    | ^TNX | 미 국채 10Y | **수익률(%)**, **변동폭(bp)** |
    | ^TYX | 미 국채 30Y | **수익률(%)**, **변동폭(bp)** |
    | ^IRX | 미 국채 2Y (13주) | **수익률(%)**, **변동폭(bp)** |
- **통화 (달러 인덱스)** (1개)
    
    
    | **티커** | **지표명** | **제공 데이터** |
    | --- | --- | --- |
    | DX-Y.NYB | Dollar Index | 시가, 고가, 저가, 종가, **등락률(%)** |
- **원자재** (4개)
    
    
    | **티커** | **지표명** | **제공 데이터** |
    | --- | --- | --- |
    | CL=F | WTI 원유 | 시가, 고가, 저가, 종가, 거래량, **등락률(%)** |
    | NG=F | 천연가스 | 시가, 고가, 저가, 종가, 거래량, **등락률(%)** |
    | GC=F | 금 | 시가, 고가, 저가, 종가, 거래량, **등락률(%)** |
    | SI=F | 은 | 시가, 고가, 저가, 종가, 거래량, **등락률(%)** |
- **암호화폐** (1개)
    
    
    | **티커** | **지표명** | **제공 데이터** |
    | --- | --- | --- |
    | BTC-USD | Bitcoin | 시가, 고가, 저가, 종가, 거래량, **24시간 등락률(%)** |

<aside>
📈

**용어 정리**

- **등락폭(pt)**: 전일 종가 대비 포인트 변동 (지수)
- **변동폭(bp)**: 전일 대비 basis point 변동 (1bp = 0.01%)
- **등락률/변동률(%)**: 전일 대비 백분율 변동
</aside>

### 4. 과거 시장 데이터 조회 Tool

<aside>
📈

**과거 OHLCV 조회**: 에이전트가 필요시 **티커, 기간, 봉(interval)**을 직접 결정하여 요청

</aside>

| **Tool Name** | **기능** | **상세 설명** |
| --- | --- | --- |
| `get_ohlcv` | 과거 OHLCV 데이터 조회 | 에이전트가 **티커, 기간, interval**을 지정하여 과거 가격 데이터 조회 |

---

## Tool 상세 스펙

### `get_news_list`

```python
def get_news_list(
    tickers: list[str] | None = None,   # 티커 필드 기반 필터링
    keywords: list[str] | None = None,  # 제목 텍스트 기반 필터링
) -> dict:
    """
    사전 다운로드된 로컬 파일에서 뉴스 목록 조회 + 필터링
    (DynamoDB 직접 접근 없음)
    
    Data Source: data/opening/news_list.json
    
    Args:
        tickers: tickers 필드에 해당 심볼이 포함된 기사만 필터링 (대소문자 무시)
        keywords: 제목에 해당 키워드가 포함된 기사만 필터링 (대소문자 무시)
        limit: 반환할 최대 뉴스 개수
    
    Note:
        - tickers와 keywords를 동시에 지정하면 AND 조건 (둘 다 만족)
        - 필터 미지정 시 전체 기사 반환
    
    Returns:
        {
            "count": 87,
            "filters": {"tickers": ["NVDA"], "keywords": ["AI"]},
            "articles": [
                {
                    "pk": "h#abcdef0123456789",
                    "title": "NVIDIA beats expectations on AI demand",
                    "tickers": ["NVDA"],
                    "publish_et_iso": "2025-11-19T15:30:00-05:00"
                },
                ...
            ]
        }
    
    Usage Examples:
        # 전체 뉴스 조회
        get_news_list()
        
        # NVDA, TSLA 관련 기사만
        get_news_list(tickers=["NVDA", "TSLA"])
        
        # 제목에 "AI" 포함된 기사만
        get_news_list(keywords=["AI"])
        
        # NVDA 관련 + 제목에 "earnings" 포함
        get_news_list(tickers=["NVDA"], keywords=["earnings"])
    """
```

### `get_news_content`

```python
def get_news_content(
    pks: list[str]  # 뉴스 pk 리스트 (e.g., ["h#abcdef01", "h#12345678"])
) -> dict:
    """
    S3에서 뉴스 본문 실시간 조회 + 로컬 캐싱
    (이미 캐시된 본문은 로컬에서 바로 반환)
    
    동작:
        1. 로컬 캐시 확인 (data/opening/bodies/{pk}.txt)
        2. 캐시 미스 → S3에서 XML 다운로드 후 파싱
        3. 본문을 context로 반환 + 로컬 캐싱
    
    Args:
        pks: 뉴스 고유 식별자 리스트
    
    Returns:
        {
            "count": 3,
            "articles": [
                {
                    "pk": "h#abcdef01",
                    "title": "Tesla shares rally after earnings beat",
                    "body": "Tesla Inc. reported quarterly earnings...",
                    "cached": false  # S3에서 새로 조회
                },
                {
                    "pk": "h#12345678",
                    "title": "NVIDIA beats expectations on AI demand",
                    "body": "NVIDIA Corporation announced...",
                    "cached": true   # 로컬 캐시에서 조회
                },
                ...
            ]
        }
    """
```

### `list_downloaded_bodies`

```python
def list_downloaded_bodies() -> dict:
    """
    로컬에 다운로드된 뉴스 본문 목록 조회
    (에이전트가 ReAct 구조로 특정 기사 선택 시 활용)
    
    Returns:
        {
            "count": 45,
            "articles": [
                {"pk": "h#abcdef01", "title": "Tesla shares rally after earnings beat"},
                {"pk": "h#12345678", "title": "NVIDIA beats expectations on AI demand"},
                {"pk": "h#deadbeef", "title": "Fed signals rate pause amid inflation concerns"},
                ...
            ]
        }
    
    Usage Example (ReAct):
        Thought: NVIDIA 관련 기사 본문에서 'AI', 'datacenter' 키워드 빈도를 확인해야겠다
        Action: list_downloaded_bodies()
        Observation: {"count": 45, "articles": [{"pk": "h#12345678", "title": "NVIDIA beats..."}, ...]}
        Thought: h#12345678이 NVIDIA 관련 기사이므로 이 본문에서 키워드 카운트
        Action: count_keyword_frequency(keywords=["AI", "datacenter"], source="bodies", news_pks=["h#12345678"])
    """
```

### `count_keyword_frequency`

```python
def count_keyword_frequency(
    keywords: list[str],       # 에이전트가 추론한 키워드 리스트
    source: str = "titles",    # "titles" | "bodies"
    news_pks: list[str] | None = None  # source="bodies"일 때 대상 기사 pk
) -> dict:
    """
    로컬 저장된 파일에서 키워드 출현 빈도 분석
    (LLM이 텍스트를 직접 전달하지 않음)
    
    Args:
        keywords: 검색할 키워드 리스트 (대소문자 무시)
        source: 
            - "titles": data/opening/titles.txt 파일에서 분석
            - "bodies": data/opening/bodies/{pk}.txt 파일들에서 분석
        news_pks: source="bodies"일 때 분석 대상 기사 pk 목록
    
    Returns:
        {
            "NVIDIA": {
                "count": 23,
                "article_pks": ["h#abc...", "h#def...", ...]
            },
            "AI": {
                "count": 45,
                "article_pks": ["h#abc...", "h#ghi...", ...]
            },
            ...
        }
    """
```

### `get_ohlcv`

```python
def get_ohlcv(
    ticker: str,                    # 티커 심볼 (e.g., "NVDA", "^GSPC", "CL=F")
    period: str = "1mo",            # 조회 기간
    interval: str = "1d"            # 봉 간격
) -> dict:
    """
    yfinance를 통해 과거 OHLCV(시가/고가/저가/종가/거래량) 데이터 조회
    (에이전트가 필요시 티커, 기간, 봉을 직접 결정)
    
    Args:
        ticker: Yahoo Finance 티커 심볼 e.g.)
            - 개별 종목: "NVDA", "AAPL", "TSLA"
            - 지수: "^GSPC" (S&P 500), "^IXIC" (Nasdaq), "^DJI" (Dow)
            - 원자재: "CL=F" (WTI), "GC=F" (Gold)
            - ETF: "SPY", "QQQ", "IWM"
        
        period: 조회 기간 (start/end 대신 사용)
            - "1d", "5d": 최근 1일, 5일
            - "1mo", "3mo", "6mo": 최근 1/3/6개월
            - "1y", "2y", "5y", "10y": 최근 1/2/5/10년
            - "ytd": 연초부터 현재까지
            - "max": 전체 기간
        
        interval: 봉(캔들스틱) 간격
            - 분봉: "1m", "5m", "15m", "30m" (최근 24시간 이내만 가능)
            - 시간봉: "1h" (최근 7일 이내만 가능)
            - 일봉: "1d"
            - 주봉: "1wk"
            - 월봉: "1mo" (ytd, max 기간은 월봉만 허용)
    
    Period-Interval 제약:
        - period="1d": 분봉(1m~30m)만 가능
        - period="5d", "1wk": 시간봉(1h) 이하만 가능
        - period="1mo"~"10y": 일봉(1d) 이상만 가능
        - period="max": 월봉(1mo)만 가능
    
    Note:
        - 분봉(1m, 5m, 15m, 30m)은 최근 24시간 데이터만 제공
        - 시간봉(1h)은 최근 7일 데이터만 제공
        - max 기간은 월봉(1mo)만 조회 가능
    """
```

---

## 로컬 데이터 저장 구조

<aside>
🔄

**하이브리드 데이터 저장**

- **Pre-fetch Script**: 뉴스 메타데이터만 사전 저장 (`news_list.json`, `titles.txt`)
- **`get_news_content`**: 본문은 에이전트가 필요할 때 S3에서 조회 후 캐싱 (`bodies/`)
</aside>

```jsx
data/opening/
├── news_list.json          # [Pre-fetch] DynamoDB 조회 결과
├── titles.txt              # [Pre-fetch] 모든 뉴스 제목
└── bodies/                 # [On-demand] 에이전트가 조회한 본문만 캐싱
    ├── h#abcdef01.txt
    ├── h#12345678.txt
    └── ...
```

### 파일별 용도

| 파일 | 생성 주체 | 용도 |
| --- | --- | --- |
| `news_list.json` | **Pre-fetch Script** | 뉴스 메타데이터 (pk, title, url, tickers, publish_et_iso, path) |
| `titles.txt` | **Pre-fetch Script** | 전체 제목 텍스트 → `count_keyword_frequency(source="titles")` |
| `bodies/{pk}.txt` | **`get_news_content`** (온디맨드) | 개별 본문 텍스트 → 재사용 시 캐시 히트 + `count_keyword_frequency(source="bodies")` |

---

## ReAct 탐색 플로우 예시

에이전트가 **자유롭게 뉴스를 탐색하며 테마를 도출**하는 과정 예시:

```jsx
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. 전체 뉴스 리스트 조회                                                     │
│    get_news_list() → 87개 기사 메타데이터                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│ 2. 제목 기반 키워드 빈도 분석                                                │
│    count_keyword_frequency(["NVIDIA", "AI", "Fed", "earnings"], "titles")   │
│    → NVIDIA: 23, AI: 45, Fed: 12, earnings: 31                             │
├─────────────────────────────────────────────────────────────────────────────┤
│ 3. 고빈도 키워드로 기사 필터링                                               │
│    get_news_list(keywords=["NVIDIA", "AI"]) → 28개 기사                     │
├─────────────────────────────────────────────────────────────────────────────┤
│ 4. 선별된 기사 본문 조회 (S3 → context + 로컬 캐싱)                          │
│    get_news_content(["h#123...", "h#456...", ...]) → 10개 본문              │
├─────────────────────────────────────────────────────────────────────────────┤
│ 5. 본문에서 추가 키워드 분석                                                 │
│    count_keyword_frequency(["datacenter", "Blackwell", "China"],            │
│                            "bodies", ["h#123...", "h#456..."])              │
│    → datacenter: 15, Blackwell: 8, China: 5                                │
├─────────────────────────────────────────────────────────────────────────────┤
│ 6. 새 키워드로 추가 기사 탐색 (반복)                                         │
│    get_news_list(keywords=["Blackwell"]) → 5개 추가 기사                    │
│    get_news_content([...]) → 본문 조회                                      │
├─────────────────────────────────────────────────────────────────────────────┤
│ 7. 테마 확정 및 대본 생성                                                    │
│    Theme 1: "NVIDIA/AI 데이터센터 수요 급증"                                 │
│    Theme 2: "Fed 금리 동결 시사"                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

<aside>
💡

**핵심 포인트**: 에이전트는 고정된 파이프라인이 아닌, **ReAct 스타일로 자유롭게 탐색**합니다.

- 키워드 빈도 확인 → 관련 기사 필터링 → 본문 조회 → 추가 키워드 발견 → 반복
- 본문은 **필요할 때만** S3에서 조회하고 **로컬 캐싱**하여 재사용
</aside>

---

## 프롬프트 설계 가이드라인

### 1. 페르소나 정의

<aside>
🎙️

**진행자 (Host)**

- **역할**: 청취자와 해설자 사이의 다리 역할, 전체 흐름 안내
- **어조**: 친근하고 명확함, 궁금증 유발, 청취자 눈높이
- **특징**: 복잡한 내용을 쉬운 질문으로 전환, 핵심 키워드 강조
- **말투**: "~인데요", "~했습니다", 감탄사 적절히 사용
</aside>

<aside>
📊

**해설자 (Analyst)**

- **역할**: 전문적 분석 제공, 데이터 기반 설명
- **어조**: 신뢰감 있고 논리적, 객관적 분석
- **특징**: 구체적 수치 인용, 인과관계 설명, 시장 맥락 제공
- **말투**: "~입니다", "~로 분석됩니다", 전문 용어 + 쉬운 설명
</aside>

### 2. 한마디 (Headline) 설계

**목적**: 당일 시장을 **한 문장**으로 요약하여 청취자의 관심 유도

- **유형별 상세 가이드**
    
    
    | **유형** | **선택 조건** |
    | --- | --- |
    | **단일 테마 집중형** | 하나의 테마가 뉴스의 50%+ 차지 |
    | **원인-결과형** | 이벤트와 시장 움직임의 인과관계 명확 |
    | **국면 전환형** | 최근 트렌드와 당일 움직임이 반대 |
    | **질문형** | 불확실성이 높고 방향성 불명확 |
    | **A vs B 대립형** | 리스크온/오프 테마가 동시 부각 |

### 3. Few-shot Examples

- **Good Example**
    
    <aside>
    ✅
    
    **[진행자]**
    
    10월 31일, 장 마감 브리핑입니다. 오늘 시장은 **한마디로 'AI의 힘'과 '연준의 경고'가 정면으로 충돌**한 하루였습니다. 아마존과 구글이 AI를 등에 업고 폭발적인 클라우드 성장을 발표하며 기술주 랠리를 이끌었는데요. 하지만 동시에 연준 위원들은 인플레이션을 이유로 금리 인하에 반대 목소리를 높였습니다.
    
    **[해설자]**
    
    네, 오늘 시장의 가장 큰 동력은 단연 AI였습니다. Amazon Web Services, 즉 AWS는 3분기 매출이 **330억 달러**로 **전년 대비 20%** 증가했습니다. 이는 **2022년 말 이후 가장 높은 성장률**로, 시장의 예상을 뛰어넘는 수치입니다.
    
    ✅ **좋은 점**: 구체적 수치, 맥락 설명, 역할 구분 명확
    
    </aside>
    
- **Bad Example**
    
    <aside>
    ❌
    
    **[진행자]**
    
    오늘 시장은 다양한 요인으로 움직였습니다. 여러 기업들이 실적을 발표했고, 연준 관련 뉴스도 있었습니다. 자세한 내용을 알아보겠습니다.
    
    **[해설자]**
    
    네, 오늘은 기술주가 좋았습니다. 아마존 실적이 좋게 나왔고, AI 관련주들이 상승했습니다. 앞으로도 상승할 것으로 예상됩니다.
    
    ❌ **문제점**: 모호한 표현, 구체적 수치 없음, 투자 조언 포함
    
    </aside>
    

---

## 참고 자료

### DynamoDB 스키마

@Lamda.md

### 대본 레퍼런스

ReferenceScript/*.md