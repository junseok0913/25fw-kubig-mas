"""
실시간 시장 데이터 수집 모듈 (yfinance)
"""

from __future__ import annotations

from typing import Optional
import yfinance as yf

from multiagent.schemas import MarketData


class MarketDataFetcher:
    """yfinance를 사용한 실시간 시장 데이터 수집"""
    
    def __init__(self):
        pass
    
    def fetch_market_data(self, ticker: str) -> Optional[MarketData]:
        """
        티커의 실시간 주가 및 재무 지표 수집
        
        Args:
            ticker: 주식 티커 (예: GOOG, AAPL)
        
        Returns:
            MarketData 객체 또는 None (실패 시)
        """
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            
            # 빈 info 체크 (잘못된 티커)
            if not info or len(info) < 5:
                print(f"⚠️  [{ticker}] 유효하지 않은 티커 또는 데이터 없음")
                return None
            
            # 안전한 get (키가 없으면 None)
            market_data = MarketData(
                # 주가 정보
                current_price=info.get("currentPrice") or info.get("regularMarketPrice"),
                market_cap=info.get("marketCap"),
                pe_ratio=info.get("trailingPE"),
                forward_pe=info.get("forwardPE"),
                price_to_book=info.get("priceToBook"),
                dividend_yield=info.get("dividendYield"),
                fifty_two_week_high=info.get("fiftyTwoWeekHigh"),
                fifty_two_week_low=info.get("fiftyTwoWeekLow"),
                fifty_day_avg=info.get("fiftyDayAverage"),
                two_hundred_day_avg=info.get("twoHundredDayAverage"),
                beta=info.get("beta"),
                volume=info.get("volume"),
                avg_volume=info.get("averageVolume"),
                
                # 재무 지표
                revenue=info.get("totalRevenue"),
                revenue_growth=info.get("revenueGrowth"),
                gross_margin=info.get("grossMargins"),
                operating_margin=info.get("operatingMargins"),
                profit_margin=info.get("profitMargins"),
                roe=info.get("returnOnEquity"),
                roa=info.get("returnOnAssets"),
                debt_to_equity=info.get("debtToEquity"),
                current_ratio=info.get("currentRatio"),
                free_cash_flow=info.get("freeCashflow"),
            )
            
            return market_data
            
        except Exception as exc:
            print(f"⚠️  [{ticker}] 시장 데이터 수집 실패: {exc}")
            return None
    
    def format_market_data_for_prompt(self, market_data: Optional[MarketData]) -> str:
        """
        MarketData를 LLM 프롬프트용 텍스트로 포맷
        
        Args:
            market_data: MarketData 객체
        
        Returns:
            포맷된 문자열
        """
        if not market_data:
            return "시장 데이터를 가져올 수 없습니다."
        
        lines = ["=== 실시간 시장 데이터 ===\n"]
        
        # 주가 정보
        if market_data.current_price:
            lines.append(f"현재 주가: ${market_data.current_price:,.2f}")
        
        if market_data.market_cap:
            lines.append(f"시가총액: ${market_data.market_cap:,.0f}")
        
        # 52주 고저
        if market_data.fifty_two_week_high and market_data.fifty_two_week_low:
            lines.append(f"52주 범위: ${market_data.fifty_two_week_low:,.2f} ~ ${market_data.fifty_two_week_high:,.2f}")
            if market_data.current_price:
                pct_from_high = ((market_data.current_price - market_data.fifty_two_week_high) 
                                / market_data.fifty_two_week_high * 100)
                lines.append(f"52주 고점 대비: {pct_from_high:+.1f}%")
        
        # 밸류에이션
        lines.append("\n밸류에이션 지표:")
        if market_data.pe_ratio:
            lines.append(f"  • P/E Ratio (TTM): {market_data.pe_ratio:.2f}")
        if market_data.forward_pe:
            lines.append(f"  • Forward P/E: {market_data.forward_pe:.2f}")
        if market_data.price_to_book:
            lines.append(f"  • P/B Ratio: {market_data.price_to_book:.2f}")
        if market_data.dividend_yield:
            lines.append(f"  • 배당 수익률: {market_data.dividend_yield*100:.2f}%")
        
        # 수익성 지표
        lines.append("\n수익성 지표:")
        if market_data.gross_margin:
            lines.append(f"  • Gross Margin: {market_data.gross_margin*100:.1f}%")
        if market_data.operating_margin:
            lines.append(f"  • Operating Margin: {market_data.operating_margin*100:.1f}%")
        if market_data.profit_margin:
            lines.append(f"  • Profit Margin: {market_data.profit_margin*100:.1f}%")
        if market_data.roe:
            lines.append(f"  • ROE: {market_data.roe*100:.1f}%")
        
        # 재무 건전성
        lines.append("\n재무 건전성:")
        if market_data.debt_to_equity:
            lines.append(f"  • Debt/Equity: {market_data.debt_to_equity:.2f}")
        if market_data.current_ratio:
            lines.append(f"  • Current Ratio: {market_data.current_ratio:.2f}")
        if market_data.free_cash_flow:
            lines.append(f"  • Free Cash Flow: ${market_data.free_cash_flow:,.0f}")
        
        # 변동성
        if market_data.beta:
            lines.append(f"\n변동성 (Beta): {market_data.beta:.2f}")
        
        # 거래량
        if market_data.volume and market_data.avg_volume:
            volume_ratio = market_data.volume / market_data.avg_volume
            lines.append(f"\n거래량: {market_data.volume:,} (평균 대비 {volume_ratio:.1f}x)")
        
        return "\n".join(lines)

