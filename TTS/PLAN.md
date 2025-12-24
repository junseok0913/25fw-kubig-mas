# TTS 파이프라인 계획서 (@TTS/)

## 목표
- 입력 `Podcast/{date}/script.json`(진행자/해설자 턴)을 기반으로 **단일 `final.wav` 1개**를 생성합니다.
- 생성은 Gemini TTS로 “대화 전체를 한 번에” 합성하는 것을 1차 목표로 합니다.
- 무드/턴별 스타일 분기는 제외하고, 고정 instructions 1개만 적용합니다.

## 고정 설정(요청 반영)
- Gemini TTS 모델: `models/gemini-2.5-pro-preview-tts` (고정)
- 멀티스피커:
  - 진행자 → `speaker1` / voiceName `Zephyr`
  - 해설자 → `speaker2` / voiceName `Charon`
- `temperature`: `1`
- `instructions`(고정, 영어):
  - `Podcast-style Korean U.S. stock market close briefing: brisk and professional; Host sounds warm and conversational, Analyst sounds confident and data-driven;`

## 환경 변수
- `.env.example`에 Gemini API 키 입력 항목을 추가합니다.
  - 예: `GEMINI_API_KEY=...`

## 입출력 경로
### 입력
- `Podcast/{date}/script.json`

### 출력
- 출력 디렉터리: `Podcast/{date}/tts/`
  - `final.wav`
  - `prompt.txt` (실제로 사용한 멀티스피커 프롬프트)
  - (옵션) `request.json` 등 디버그용 파일

## Overwrite 정책(단일 실행)
- `Podcast/{date}/tts/`가 이미 존재하면 **ERROR 로깅 후 즉시 중단**합니다. (덮어쓰기 금지)

## 스크립트 → 멀티스피커 프롬프트 변환
- `script.json`의 `scripts[]`를 순서대로 합쳐 하나의 “대화 텍스트”를 만듭니다.
- 매핑:
  - `speaker == "진행자"` → `speaker1: {text}`
  - `speaker == "해설자"` → `speaker2: {text}`
- 프롬프트 구조(예시):
  - 상단에 고정 instructions 1줄을 둔 뒤
  - `speaker1:` / `speaker2:` 라인들을 이어붙입니다.
- 중요: `multiSpeakerVoiceConfig.speakerVoiceConfigs[].speaker` 값은 프롬프트에 쓰는 `speaker1/speaker2`와 **완전히 동일**해야 합니다.

## Gemini TTS 요청 형태(요약)
- `generateContent`로 오디오 모달리티를 요청합니다.
  - `generationConfig.responseModalities = ["AUDIO"]`
  - `generationConfig.temperature = 1`
  - `generationConfig.speechConfig.multiSpeakerVoiceConfig.speakerVoiceConfigs = [...]`
- 오디오 결과는 `inlineData.data`(base64)로 반환됩니다.
  - 문서 예시 기준 출력은 base64 PCM(s16le, 24kHz, mono)이며, `wave` 모듈로 `wav`로 저장합니다.

## 오류 처리(요청 반영)
- 재시도/재분할 없이 단일 호출로 시도합니다.
- 길이 제한/429/네트워크 오류 등이 발생하면 **ERROR 로깅 후 중단**합니다.
