"""
멀티 에이전트 그래프 첫 노드: 티커 데이터 준비
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from src.database.data_fetcher import DataFetcher
from aws_fetchers.yahoo_news_fetcher import YahooNewsFetcher
from multiagent.services import AgentToolkit
from multiagent.services.market_data import MarketDataFetcher
from multiagent.agents.fundamental_analyst import FundamentalAnalyst
from multiagent.agents.risk_manager import RiskManager
from multiagent.agents.growth_analyst import GrowthAnalyst
from multiagent.agents.sentiment_analyst import SentimentAnalyst


def prepare_ticker_dataset(
    ticker: str,
    hours: int = 24,
    news_limit: Optional[int] = 10,
) -> Dict:
    """
    티커를 입력받아 AWS 뉴스(S3 + DynamoDB)와
    로컬 SEC 데이터(sec_filings.db)를 동시에 수집합니다.
    LangGraph 첫 노드에서 그대로 사용할 수 있는 유틸 함수입니다.
    """
    ticker_upper = ticker.upper()

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=hours)

    # 1) AWS에서 뉴스 가져오기 (에러 핸들링)
    aws_news = []
    try:
        yahoo_fetcher = YahooNewsFetcher()
        aws_news = yahoo_fetcher.fetch(ticker_upper, limit=news_limit or 5)
    except Exception as exc:
        print(f"⚠️  [{ticker_upper}] AWS 뉴스 수집 실패: {exc}")
        aws_news = []

    # 2) 로컬 SEC 데이터 (최근 24시간)
    fetcher = DataFetcher()
    sec_data = fetcher.fetch_ticker_data(ticker_upper, include_file_content=True)

    # 3) 실시간 시장 데이터 (yfinance) - 에러 핸들링
    market_data = None
    market_data_text = ""
    try:
        market_fetcher = MarketDataFetcher()
        market_data = market_fetcher.fetch_market_data(ticker_upper)
        market_data_text = market_fetcher.format_market_data_for_prompt(market_data)
        
        if market_data and market_data.current_price:
            print(f"💰 [{ticker_upper}] 현재 주가: ${market_data.current_price:,.2f}")
    except Exception as exc:
        print(f"⚠️  [{ticker_upper}] 시장 데이터 수집 실패: {exc}")
        market_data = None
        market_data_text = "시장 데이터를 가져올 수 없습니다."

    dataset = {
        "ticker": ticker_upper,
        "period": sec_data.get("period"),
        "aws_news": aws_news,
        "sec_filings": sec_data.get("sec_filings"),
        "market_data": market_data,
        "market_data_text": market_data_text,
    }

    # 4명의 전문가 초기화
    toolkit = AgentToolkit()
    fundamental = FundamentalAnalyst(toolkit)
    risk = RiskManager(toolkit)
    growth = GrowthAnalyst(toolkit)
    sentiment = SentimentAnalyst(toolkit)

    # 각 전문가의 초기 분석 (Blind Assessment) - 병렬 실행으로 속도 4배 향상
    import concurrent.futures
    
    def run_blind_assessment(agent, name):
        return name, agent.blind_assessment(dataset)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(run_blind_assessment, fundamental, "fundamental"),
            executor.submit(run_blind_assessment, risk, "risk"),
            executor.submit(run_blind_assessment, growth, "growth"),
            executor.submit(run_blind_assessment, sentiment, "sentiment"),
        ]
        
        results = {}
        for future in concurrent.futures.as_completed(futures):
            name, result = future.result()
            results[name] = result
    
    initial_fundamental = results["fundamental"]
    initial_risk = results["risk"]
    initial_growth = results["growth"]
    initial_sentiment = results["sentiment"]

    # 5) 출처 정보 구성 (검증 에이전트용)
    sec_filings_for_sources = sec_data.get("sec_filings", [])
    sources = _build_sources(
        ticker=ticker_upper,
        sec_filings=sec_filings_for_sources,
        aws_news=aws_news,
        market_data=market_data,
    )

    return {
        "dataset": dataset,
        "initial_fundamental": initial_fundamental,
        "initial_risk": initial_risk,
        "initial_growth": initial_growth,
        "initial_sentiment": initial_sentiment,
        "sources": sources,
    }


def _build_sources(ticker: str, sec_filings: list, aws_news: list, market_data) -> Dict:
    """검증 에이전트를 위한 출처 정보 구성 (20251222.json 형식)"""
    from datetime import datetime, timezone
    
    sources = {
        "ticker": ticker,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "sources": [],  # 모든 출처를 단일 배열로 (type으로 구분)
    }
    
    # SEC 공시 출처
    for filing in sec_filings or []:
        meta = filing.get("metadata", {})
        sources["sources"].append({
            "type": "sec_filing",
            "form": meta.get("form"),
            "filed_date": meta.get("filed_date"),
            "reporting_for": meta.get("reporting_for"),
            "accession_number": meta.get("accession_number"),
            "file_path": meta.get("file_path"),
        })
    
    # 뉴스 기사 출처 (pk 형식)
    for news in aws_news or []:
        pk = news.get("pk") or news.get("id") or ""
        sources["sources"].append({
            "type": "article",
            "pk": pk,
            "title": news.get("title", "")[:100],
        })
    
    # 시장 데이터 출처 (차트 형식)
    if market_data:
        today = datetime.now().strftime("%Y-%m-%d")
        sources["sources"].append({
            "type": "chart",
            "ticker": ticker,
            "source": "yfinance",
            "current_price": getattr(market_data, "current_price", None),
            "pe_ratio": getattr(market_data, "pe_ratio", None),
            "market_cap": getattr(market_data, "market_cap", None),
        })
    
    return sources
