"""ThemeAgent 단독 실행 진입점.

OpeningAgent가 만든 결과 JSON(예: OpeningAgent/data/opening_result.json)과 동일한
구조의 파일을 받아 ThemeAgent를 실행하고 완성된 스크립트를 출력/저장한다.

Usage:
    python run_theme_agent.py --json opening_result.json --out theme_result.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# ThemeAgent 모듈 import 경로 설정
ROOT = Path(__file__).parent
THEME_AGENT_ROOT = ROOT / "ThemeAgent"
if str(THEME_AGENT_ROOT) not in sys.path:
    sys.path.append(str(THEME_AGENT_ROOT))

from src import theme_agent  # noqa: E402
from src.utils.tracing import configure_tracing  # noqa: E402


def load_opening_result(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"입력 JSON을 찾을 수 없습니다: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    parser = argparse.ArgumentParser(description="ThemeAgent 단독 실행 (JSON 입력 + 날짜 인자)")
    parser.add_argument(
        "--json",
        type=str,
        default="opening_result.json",
        help="OpeningAgent 결과 JSON 경로 (기본: ./opening_result.json)",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="theme_agent_result.json",
        help="ThemeAgent 결과 저장 경로 (기본: ./theme_agent_result.json)",
    )
    parser.add_argument(
        "--date",
        type=str,
        required=True,
        help="브리핑 날짜 (YYYYMMDD 또는 YYYY-MM-DD). Tool BRIEFING_DATE로 사용",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env", override=False)
    configure_tracing()

    input_path = Path(args.json)
    output_path = Path(args.out)

    data = load_opening_result(input_path)
    date_arg = args.date
    if "-" in date_arg:
        from datetime import datetime

        date_arg = datetime.strptime(date_arg, "%Y-%m-%d").strftime("%Y%m%d")
    date_str = date_arg or data.get("date")
    if not date_str:
        raise ValueError("입력 JSON에 'date' 필드가 없으며 --date 인자도 없습니다.")

    # Tool들이 참조할 BRIEFING_DATE 설정
    os.environ["BRIEFING_DATE"] = str(date_str)

    ta_graph = theme_agent.build_theme_graph()
    result = ta_graph.invoke(
        {
            "date": str(date_str),
            "nutshell": data.get("nutshell", ""),
            "themes": data.get("themes", []),
            "base_scripts": data.get("scripts", []),
        }
    )

    output_payload = {
        "date": date_str,
        "nutshell": data.get("nutshell", ""),
        "themes": data.get("themes", []),
        "scripts": result.get("scripts", []),
        "current_section": "stock",
    }
    output_path.write_text(json.dumps(output_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[ThemeAgent 완료] scripts: {len(output_payload['scripts'])}턴")
    print(f"입력: {input_path}")
    print(f"출력: {output_path}")

    try:
        theme_agent.cleanup_cache()
    except Exception:
        pass


if __name__ == "__main__":
    main()
