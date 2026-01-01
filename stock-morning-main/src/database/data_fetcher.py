"""
데이터 조회 모듈
로컬 SQLite DB에서 최근 데이터(24h 또는 N일 윈도우)를 가져옵니다.
"""

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv

from src.db import SECDatabase
from src.time_utils import get_last_24h_window, KST

load_dotenv()


class DataFetcher:
    """6시~6시 기준 데이터 조회 클래스"""
    
    def __init__(self):
        self.db = SECDatabase()
    
    def fetch_ticker_data(
        self,
        ticker: str,
        include_file_content: bool = True
    ) -> Dict:
        """
        특정 ticker의 6시~6시 기준 데이터 수집
        
        Args:
            ticker: 종목 코드
            include_file_content: SEC 파일 내용을 포함할지 여부
                                 (False면 메타데이터만)
        
        Returns:
            {
                'ticker': str,
                'period': {'start': datetime, 'end': datetime},
                'news': List[Dict],  # 로컬 뉴스 데이터
                'sec_filings': List[Dict]  # SEC 파일 (메타 + 내용)
            }
        """
        window_days = os.getenv("SEC_CRAWLER_WINDOW_DAYS")
        if window_days:
            days = max(1, int(window_days))
            end = datetime.now(KST)
            start = end - timedelta(days=days)
        else:
            start, end = get_last_24h_window()
        
        # 2. 로컬 DB에서 뉴스 조회
        news = self.db.get_news(
            ticker=ticker,
            start_time=start,
            end_time=end
        )
        
        # 3. 로컬 DB에서 SEC 메타데이터 조회 (최근 N일)
        sec_metadata = self.db.get_filings_between(
            ticker=ticker,
            start_time=start,
            end_time=end
        )
        
        # 4. 가장 최근 10-K, 10-Q는 항상 포함 (기간과 관계없이)
        latest_annuals = self.db.get_latest_annual_quarterly(ticker)
        existing_accession = {m.get('accession_number') for m in sec_metadata}
        
        for form_type in ['10-K', '10-Q']:
            filing = latest_annuals.get(form_type)
            if filing and filing.get('accession_number') not in existing_accession:
                sec_metadata.insert(0, filing)  # 맨 앞에 추가
        
        # 5. 로컬 파일에서 SEC 내용 가져오기
        sec_filings = []
        if include_file_content and sec_metadata:
            for meta in sec_metadata:
                file_path_str = meta.get('file_path')
                if file_path_str:
                    file_path = Path(file_path_str)
                    if file_path.exists():
                        content = file_path.read_text(encoding='utf-8', errors='ignore')
                        sec_filings.append({
                            'metadata': meta,
                            'content': content
                        })
        else:
            # 파일 내용 없이 메타데이터만
            sec_filings = [{'metadata': meta, 'content': None} for meta in sec_metadata]
        
        # 10-K, 10-Q 포함 여부 출력
        forms_included = [f.get('metadata', {}).get('form') for f in sec_filings]
        has_10k = '10-K' in forms_included
        has_10q = '10-Q' in forms_included
        
        result = {
            'ticker': ticker,
            'period': {
                'start': start.isoformat(),
                'end': end.isoformat()
            },
            'news': news,
            'sec_filings': sec_filings,
            'has_10k': has_10k,
            'has_10q': has_10q,
        }
        
        ann_status = f"10-K: {'✅' if has_10k else '❌'}, 10-Q: {'✅' if has_10q else '❌'}"
        print(f"📊 [{ticker}] 데이터 수집: 뉴스 {len(news)}건, SEC 공시 {len(sec_filings)}건 ({ann_status})")
        
        return result
    
    def fetch_all_tickers(
        self,
        tickers: List[str],
        include_file_content: bool = True
    ) -> Dict[str, Dict]:
        """
        여러 ticker의 데이터를 한번에 조회
        
        Args:
            tickers: 종목 코드 리스트
            include_file_content: SEC 파일 내용 포함 여부
        
        Returns:
            {ticker: data} 딕셔너리
        """
        results = {}
        
        for ticker in tickers:
            try:
                data = self.fetch_ticker_data(ticker, include_file_content)
                results[ticker] = data
            except Exception as e:
                print(f"❌ [{ticker}] 데이터 조회 실패: {e}")
                results[ticker] = None
        
        return results
