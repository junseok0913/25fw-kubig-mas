# Podcast 데이터 폴더 (@Podcast/)

## 목적
- 날짜별 최종 대본을 `Podcast/{date}/script.json`으로 보관합니다.
- TTS 파이프라인 코드는 `TTS/`에서 구축합니다. (상세: `TTS/PLAN.md`)

## 디렉터리 구조
- `Podcast/{date}/`
  - `script.json`: 최종 산출물(JSON) — `date`, `user_tickers`, `scripts`
  - `tts/`: TTS 파이프라인 실행 결과(턴 오디오, 타임라인, final 오디오 등)

## 생성/갱신 규칙
- `orchestrator.py` 실행 시:
  - `Podcast/{date}/script.json` 저장

## 스키마(요약)
- `script.json`
  - `date`: `"YYYYMMDD"` (ET 기준)
  - `user_tickers`: `string[]`
  - `scripts`: `{ id:int, speaker:str, text:str, sources:any[] }[]`
