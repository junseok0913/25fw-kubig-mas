# TTS Architecture (@TTS/)

## Overview
`@TTS/`는 `Podcast/{YYYYMMDD}/script.json`의 `scripts[]`를 입력으로 받아, **턴 단위(single-speaker) TTS**를 생성하고 gap 규칙을 적용해 **하나의 최종 WAV**로 합치는 파이프라인입니다.

핵심 설계 의도는 아래 3가지입니다.
- **정량적 타임라인(ms) 산출**: 각 turn WAV의 frame 길이를 이용해 `start_time_ms/end_time_ms`를 정확히 계산합니다.
- **재시도/재개 친화적 저장**: turn WAV를 응답 즉시 디스크에 저장하고, 이미 존재하는 파일은 skip합니다.
- **LangSmith 트레이싱 용량 안전**: LangGraph `state`에 PCM/오디오 bytes를 넣지 않고, **경로/frames 메타데이터만** 보관합니다.

## Inputs
### `Podcast/{date}/script.json`
최소 요구 구조(요약):
- `scripts[]`: 각 항목은 `id`(int), `speaker`("진행자"|"해설자"), `text`(str)를 포함
- 선택: `chapter[]` (orchestrator가 채우는 챕터 범위)
  - `{ "name": "opening"|"theme"|"closing", "start_id": int, "end_id": int }`

## Outputs
### Turn WAV (중간 산출물)
- `Podcast/{date}/tts/{id}.wav`
  - 예: `Podcast/20251222/tts/00.wav`
  - turn 응답이 오는 즉시 저장됩니다.

### Timeline
- `Podcast/{date}/tts/timeline.json`
  - 각 turn의 `start_time_ms`, `end_time_ms`, `duration_ms` 포함
  - gap 설정(ms)도 함께 기록합니다.

### Final WAV
- `Podcast/{date}/{date}.wav`
  - 예: `Podcast/20251222/20251222.wav`
  - `tts/*.wav`를 순서대로 읽고, gap(silence)을 삽입하며 스트리밍으로 생성합니다(덮어쓰기).

### Script + time 주입 결과(JSON)
- `Podcast/{date}/{date}.json`
  - `script.json`을 복사한 뒤, 각 `scripts[]` 항목에 `"time": [start_ms, end_ms]`를 주입한 결과(덮어쓰기).

## Configuration
### YAML
- `TTS/config/gemini_tts.yaml`
  - `instructions.speaker1`, `instructions.speaker2`
  - `voices.speaker1`, `voices.speaker2`
  - `temperature`
  - `timeout_seconds` (요청 타임아웃/배치 완료 대기 타임아웃)
  - `max_parallel_requests` (배치 크기 = 동시에 보낼 요청 수)
  - `batch_timeout_seconds` (배치 **쿨다운**: 배치 완료 후 다음 배치 시작 전 sleep)
  - `common_gap_seconds` (챕터 내부 turn 간 gap)
  - `chapter_gap_seconds` (챕터 경계 gap; common gap을 대체)

### Env
- `GEMINI_API_KEY`: 필수
- `GEMINI_TTS_MODEL`: 선택, 기본값 `models/gemini-2.5-pro-preview-tts`
  - 공식 문서 예시에서 자주 사용하는 대안: `models/gemini-2.5-flash-preview-tts`
- LangSmith(선택): `LANGSMITH_TRACING_V2`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`, `LANGSMITH_ENDPOINT`

## Execution Flow (LangGraph)
엔트리포인트:
- `python -m TTS.src.tts {YYYYMMDD}`

그래프 노드 구성(요약):
1. `load_config`
   - `TTS/config/gemini_tts.yaml` 로드
2. `validate_paths`
   - `Podcast/{date}/script.json` 존재 확인
3. `load_script`
   - script.json 로드
4. `map_turns_with_chapter`
   - `chapter[]` 범위를 기준으로 turn→chapter 매핑(없으면 `"all"`)
5. `build_turn_requests`
   - turn 단위 프롬프트 구성
   - `speaker` → label(`speaker1`/`speaker2`) 매핑 후, 해당 speaker의 instruction을 prepend
6. `generate_turn_audio_parallel`
   - 배치 단위로 요청: `max_parallel_requests`개를 동시에 요청하고 **전부 끝나면** 다음 배치 진행
   - 각 turn은 응답 즉시 `Podcast/{date}/tts/{id}.wav`로 저장
   - 이미 `{id}.wav`가 있으면 **skip**하고 frames만 로드
   - 레거시 호환: 기존 `Podcast/{date}/tts/turns/*.wav`가 있으면 자동으로 `tts/*.wav`로 이동
7. `compute_timeline`
   - 각 turn `frames`를 기반으로 `start_time_ms/end_time_ms` 계산
   - gap 적용 규칙:
     - 다음 turn과 챕터가 다르고 둘 다 `opening/theme/closing`이면 `chapter_gap_seconds`
     - 그 외는 `common_gap_seconds`
8. `merge_audio`
   - `tts/*.wav`를 순서대로 스트리밍 read → gap(silence) 삽입 → `Podcast/{date}/{date}.wav` write
9. `write_outputs`
   - `Podcast/{date}/tts/timeline.json` 저장
   - `Podcast/{date}/{date}.json` 저장(각 scripts[]에 `"time"` 추가)

## Gemini TTS Request Shape
Gemini `generateContent`를 사용합니다.
- `generationConfig.responseModalities = ["AUDIO"]`
- `generationConfig.temperature = {temperature}`
- `generationConfig.speechConfig.multiSpeakerVoiceConfig`에 `speaker1/speaker2` voice를 설정
- 프롬프트는 turn 단위로, 해당 turn speaker label 1개만 사용(단일 화자 output 목적)

## Rate Limit Strategy
Gemini 쿼터(RPM) 초과를 피하기 위한 정책:
- 배치 크기: `max_parallel_requests`
- 배치 완료 후 쿨다운: `batch_timeout_seconds`만큼 sleep
- 배치가 “완료될 때까지 기다리는” 타임아웃은 `timeout_seconds`

## Tracing (LangSmith)
LangSmith 업로드 용량 제한을 피하기 위해:
- LangGraph `state`에는 **오디오 bytes(PCM/WAV)** 를 넣지 않습니다.
- turn WAV는 디스크 파일로만 관리하고, state에는 `wav`(상대경로) + `frames`만 저장합니다.

## Resume / Overwrite Policy
- `Podcast/{date}/tts/{id}.wav`가 이미 존재하면 해당 turn은 **TTS 호출을 스킵**합니다.
- 모든 turn WAV가 존재하면 **TTS 호출 없이** timeline/merge 단계로 진행합니다.
- 아래 산출물은 **항상 덮어쓰기**합니다:
  - `Podcast/{date}/{date}.wav`
  - `Podcast/{date}/tts/timeline.json`
  - `Podcast/{date}/{date}.json`

