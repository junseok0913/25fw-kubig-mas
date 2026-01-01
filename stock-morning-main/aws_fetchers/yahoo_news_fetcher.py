"""
S3 + DynamoDB에 저장된 Yahoo Finance 뉴스를 불러오는 유틸

요건:
- DynamoDB 테이블: kubig-YahoofinanceNews
- S3 버킷: kubig-yahoofinancenews
- 각 Item에는 ticker, pk, path, et_iso 등이 포함되어 있다고 가정
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Attr


class YahooNewsFetcher:
    def __init__(
        self,
        table_name: str = "kubig-YahoofinanceNews",
        bucket_name: str = "kubig-yahoofinancenews",
        output_dir: str = "aws_results",
        region_name: Optional[str] = "ap-northeast-2",
    ):
        self.table_name = table_name
        self.bucket_name = bucket_name
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        session = boto3.Session(region_name=region_name) if region_name else boto3.Session()
        self.dynamo = session.resource("dynamodb").Table(table_name)
        self.s3 = session.client("s3")

    def fetch(
        self,
        ticker: str,
        limit: int = 10,
    ) -> List[Dict]:
        """
        DynamoDB에서 ticker에 해당하는 최신 뉴스 limit개를 가져오고,
        S3에서 원문을 내려받아 JSON 파일로 저장합니다.
        """
        ticker_upper = ticker.upper()
        items = self._scan_ticker(ticker_upper)
        if not items:
            print(f"⚠️  DynamoDB에 해당 티커({ticker_upper}) 뉴스가 없습니다.")
            return []

        # 최신순 정렬 (et_iso 기준)
        sorted_items = sorted(
            items,
            key=lambda x: x.get("et_iso", ""),
            reverse=True,
        )[:limit]

        saved = []
        for idx, item in enumerate(sorted_items, 1):
            article = self._download_article(item)
            if article:
                filename = self._save_article(ticker_upper, article, idx)
                saved.append({"filepath": str(filename), **article})

        print(f"✅ {ticker_upper} 뉴스 {len(saved)}/{len(sorted_items)}건 저장")
        return saved

    def _scan_ticker(self, ticker: str) -> List[Dict]:
        """
        ticker attribute를 기준으로 DynamoDB를 스캔.
        (테이블 설계에 따라 적절히 수정 필요)
        """
        items: List[Dict] = []
        kwargs = {
            "FilterExpression": Attr("tickers").contains(ticker),
        }

        while True:
            response = self.dynamo.scan(**kwargs)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key

        return items

    def _download_article(self, item: Dict) -> Optional[Dict]:
        pk = item.get("pk")
        path = item.get("path")
        if not pk or not path:
            print(f"⚠️  pk/path 정보가 없어 스킵: {item}")
            return None

        key = self._build_s3_key(path, pk)

        try:
            obj = self.s3.get_object(Bucket=self.bucket_name, Key=key)
            body = obj["Body"].read().decode("utf-8")
        except Exception as exc:
            print(f"❌ S3 다운로드 실패 ({key}): {exc}")
            return None

        return {
            "pk": pk,
            "path": path,
            "ticker": item.get("ticker"),
            "published_at": item.get("et_iso"),
            "source": item.get("source"),
            "title": item.get("title"),
            "article_raw": body,
        }

    def _save_article(self, ticker: str, article: Dict, index: int) -> Path:
        timestamp = article.get("published_at") or datetime.utcnow().isoformat()
        safe_ts = timestamp.replace(":", "").replace("-", "")
        filename = self.output_dir / f"{ticker}_{safe_ts}_{index}.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(article, f, ensure_ascii=False, indent=2)
        return filename

    @staticmethod
    def _build_s3_key(path: str, pk: str) -> str:
        """
        DynamoDB path 값이 여러 형태일 수 있으므로 안전하게 S3 키를 생성합니다.
        - path가 이미 .xml로 끝나면 그대로 사용
        - path가 폴더이면 pk.xml을 붙임
        """
        normalized = path.strip()
        if normalized.endswith(".xml"):
            return normalized
        if not normalized.endswith("/"):
            normalized = f"{normalized}/"
        return f"{normalized}{pk}.xml"
