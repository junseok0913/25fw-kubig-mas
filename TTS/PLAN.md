# TTS 파이프라인 계획서 (@TTS/)

## 목표
- 입력 `Podcast/{date}/script.json`(진행자/해설자 턴)을 기반으로 **단일 `{date}.wav` 1개**를 생성합니다.
- 이후 “턴별 start/end 타임코드(ms)”가 필요하므로, **멀티스피커 1회 생성**이 아니라 **턴(1개) 단위로 단일 화자(single-speaker) 오디오를 생성**합니다.
- 각 턴의 오디오 길이(정확한 sample/frame 기반)를 이용해, **공통 gap + 챕터 gap을 포함한 start_time_ms / end_time_ms를 한 번에 계산**합니다.
- 동시 요청은 턴 단위로 **최대 4개 병렬(in-flight)** 까지 허용합니다.
- 무드/턴별 스타일 분기는 제외하고, **진행자/해설자별 고정 instructions**만 적용합니다.

## 설정(요청 반영)
- Gemini TTS 모델: `models/gemini-2.5-pro-preview-tts` (코드 상 고정)
- TTS 설정 파일: `TTS/config/gemini_tts.yaml`
  - 진행자/해설자별 `instructions` / `voices` + 공통 `temperature`를 여기서 관리
  - `timeout_seconds`(요청 타임아웃)도 여기서 관리
  - 병렬도 설정 파일에서 관리: `max_parallel_requests` (=4)
  - 배치 쿨다운도 설정 파일에서 관리: `batch_timeout_seconds` (=60)
    - **배치(=max_parallel_requests개) 4개가 모두 응답된 뒤 60초 대기**하고 다음 배치를 시작합니다(RPM 제한 회피).
    - 배치 완료(4개 응답)까지 기다리는 타임아웃은 `timeout_seconds`를 사용합니다.
  - gap은 2개로 분리
    - `chapter_gap_seconds`: 챕터 간 gap (opening 끝 → theme 시작, theme 끝 → closing 시작)
    - `common_gap_seconds`: 같은 챕터 내 “턴 ↔ 턴” 사이 기본 gap

## 환경 변수
- `.env.example`에 Gemini API 키 입력 항목을 추가합니다.
  - 예: `GEMINI_API_KEY=...`

## 아키텍처 (LangGraph)
복잡도가 커지고(향후 `orchestrator.py`에 합칠 예정) 파이프라인 단계가 명확하므로, 다른 에이전트와 동일하게 LangGraph로 구성합니다.

### 엔트리포인트
- 메인 실행: `python -m TTS.src.tts {YYYYMMDD}`

### State 스키마(초안)
- `date`: `YYYYMMDD`
- `script_path`: `Podcast/{date}/script.json`
- `out_dir`: `Podcast/{date}/tts/`
- `instructions` / `temperature` / `speaker1_voice` / `speaker2_voice`
- `timeout_seconds`
- `max_parallel_requests`
- `batch_timeout_seconds`
- `chapter_gap_seconds` / `common_gap_seconds`
- `turns`: `scripts[]`를 id순으로 정렬한 턴 목록(+chapter name 매핑)
- `turn_audios`: 턴별 응답 오디오(bytes)
- `timeline`: 턴별 타임라인(start/end ms)
- `out_wav`: 최종 저장 경로

### Graph 노드(초안)
- `load_config`: `TTS/config/gemini_tts.yaml` 로드
- `validate_paths`: 입력 파일 존재, 출력 디렉터리 미존재(덮어쓰기 금지)
- `load_script`: `script.json` 로드
- `map_turns_with_chapter`: `chapter` 범위로 turn→chapter 매핑(없으면 all로 간주)
- `build_turn_prompts`: 턴 단위(single speaker) 프롬프트 구성
- `generate_turn_audio_parallel`: 턴 단위 Gemini TTS 호출(배치=4개, 배치가 전부 응답해야 다음 배치 진행, 배치 타임아웃 적용)
  - 각 턴의 응답이 오는 즉시 `Podcast/{date}/tts/turns/{id}.wav`로 저장합니다(중간 실패에도 부분 결과 유지).
- `compute_timeline`: PCM frame 기반으로 duration_ms 계산 후, gap 규칙으로 start/end ms 산출
- `merge_audio`: PCM 정규화 + gap 삽입 후 전체 결합
- `write_outputs`:
  - `turns/{id}.wav` (턴별 오디오)
  - `timeline.json` (턴별 start_time_ms/end_time_ms)
  - `{date}.wav`

## 입출력 경로
### 입력
- `Podcast/{date}/script.json`

### 출력
- 출력 디렉터리: `Podcast/{date}/tts/`
  - `{date}.wav`
  - `turns/` (턴별 wav 저장)
  - `timeline.json` (턴별 타임라인)
- 프로젝트 루트: `{date}.json`
  - `Podcast/{date}/script.json`을 복사한 뒤, 각 `scripts[]` 항목에 `"time": [start_ms, end_ms]`를 추가한 결과

## Overwrite 정책(단일 실행)
- `Podcast/{date}/tts/turns/{id}.wav`가 이미 존재하면 해당 turn은 **TTS 호출을 스킵**하고 파일을 그대로 사용합니다.
- 모든 turn wav가 이미 존재하면 **TTS 호출 없이** timeline/merge 단계로 진행합니다.
- `Podcast/{date}/tts/{date}.wav`와 `Podcast/{date}/tts/timeline.json`은 **항상 덮어쓰기**합니다.

## 스크립트 → 단일 화자(턴) 프롬프트 변환
- 턴마다 **해당 speaker의 voice 1개만 선택**하여 single-speaker TTS를 요청합니다.
  - 예: `speaker == "진행자"` → `voices.speaker1`
  - 예: `speaker == "해설자"` → `voices.speaker2`
- 프롬프트는 “대화 전체”가 아니라 **해당 turn의 text만 포함**합니다(짧아짐).
  - 상단에 고정 instructions 1줄 + 본문 text

## Gemini TTS 요청 형태(요약)
- `generateContent`로 오디오 모달리티를 요청합니다(턴 단위).
  - `generationConfig.responseModalities = ["AUDIO"]`
  - `generationConfig.temperature = {temperature}`
  - voice는 “단일 화자 1개”만 적용(해당 턴 speaker에 매핑된 voiceName 사용)
- 오디오 결과는 `inlineData.data`(base64)로 반환됩니다.
  - 문서 예시 기준 출력은 base64 PCM(s16le, 24kHz, mono)이며, `wave` 모듈로 `wav`로 저장합니다.

## LangSmith 추적
- 추적 활성화는 다른 에이전트와 동일하게 `LANGSMITH_TRACING_V2=true`, `LANGSMITH_API_KEY=...`로 제어합니다.
- 로컬에는 프롬프트 원문을 파일로 저장하지 않고, LangSmith trace로 확인합니다.

## 오류 처리(요청 반영)
- 재시도 없이 턴 단위로 시도합니다(요청 실패 시 전체 중단).
- 길이 제한/429/네트워크 오류 등이 발생하면 **ERROR 로깅 후 중단**합니다.

## 확인 질문(의미 확인용)
- “single speaker audio”는 **턴당 1개의 화자 음성만 포함**(다만 진행자/해설자에 따라 voice는 다를 수 있음)으로 이해했습니다. 맞나요?
- `chapter_gap_seconds`는 챕터 경계에서 **common gap을 대체**하는 것으로 이해했습니다(=둘 다 더하지 않음). 맞나요?
