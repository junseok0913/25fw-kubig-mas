# Podcast 데이터 폴더 (@Podcast/)

## 목적
- 날짜별 최종 대본을 `Podcast/{date}/script.json`으로 보관합니다.
- TTS 파이프라인 코드는 `TTS/`에서 구축합니다. (상세: `TTS/PLAN.md`)

## 디렉터리 구조
- `Podcast/{date}/`
  - `script.json`: 최종 산출물(JSON) — `date`, `nutshell`, `user_tickers`, `chapter`, `scripts`
  - `tts/`: TTS 파이프라인 실행 결과(턴 오디오, 타임라인, final 오디오 등)
- `Podcast/podcast.db`: 팟캐스트 인덱스(SQLite) — 웹에서 리스트/재생 용도

## 생성/갱신 규칙
- `orchestrator.py` 실행 시:
  - `Podcast/{date}/script.json` 저장
  - `Podcast/podcast.db` upsert (script 저장시간 갱신)
- `python -m TTS.src.tts {date}` 실행 시:
  - `Podcast/podcast.db` update (tts 산출여부/최종파일 저장시간 갱신)

## 스키마(요약)
- `script.json`
  - `date`: `"YYYYMMDD"` (ET 기준)
  - `nutshell`: `string` (오늘 장 한마디)
  - `user_tickers`: `string[]`
  - `chapter`: `{ name:"opening"|"theme"|"closing", start_id:int, end_id:int }[]` (선택)
  - `scripts`: `{ id:int, speaker:str, text:str, sources:any[] }[]`

- `podcast.db` (`podcasts` 테이블)
  - `date`: `TEXT` (PK, `"YYYYMMDD"`)
  - `nutshell`: `TEXT`
  - `user_tickers`: `TEXT` (JSON string)
  - `script_saved_at`: `TEXT` (UTC ISO8601)
  - `tts_done`: `BOOLEAN` (TRUE/FALSE)
  - `final_saved_at`: `TEXT` (UTC ISO8601)
