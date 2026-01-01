"""
데이터베이스 모듈
SEC 공시 및 뉴스 데이터를 SQLite DB에 저장하고 관리합니다.
"""

import hashlib
import sqlite3
from pathlib import Path
from typing import Dict, Optional, List
from datetime import datetime


class SECDatabase:
    """SEC 공시 및 뉴스 자료를 저장하는 데이터베이스 클래스"""
    
    def __init__(self, db_path: str = "sec_filings.db"):
        """
        Args:
            db_path: SQLite 데이터베이스 파일 경로
        """
        self.db_path = db_path
        self.init_db()
    
    def get_connection(self):
        """데이터베이스 연결 반환"""
        return sqlite3.connect(self.db_path)
    
    def init_db(self):
        """데이터베이스 초기화 및 테이블 생성"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # filings 테이블 생성
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS filings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker VARCHAR(10) NOT NULL,
                    cik VARCHAR(10) NOT NULL,
                    accession_number VARCHAR(50) UNIQUE NOT NULL,
                    form VARCHAR(20) NOT NULL,
                    filed_date DATE NOT NULL,
                    acceptance_date DATE,
                    reporting_for DATE,
                    filing_entity VARCHAR(255) NOT NULL,
                    file_path VARCHAR(500) NOT NULL,
                    file_format VARCHAR(10) NOT NULL,
                    file_size INTEGER,
                    downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # 기존 테이블에 acceptance_date 컬럼이 없으면 추가 (마이그레이션)
            try:
                cursor.execute("SELECT acceptance_date FROM filings LIMIT 1")
            except sqlite3.OperationalError:
                # acceptance_date 컬럼이 없으면 추가
                cursor.execute("""
                    ALTER TABLE filings ADD COLUMN acceptance_date DATE
                """)
                print("acceptance_date 컬럼을 추가했습니다.")
            
            # 인덱스 생성 (조회 성능 향상)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_ticker ON filings(ticker)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_cik ON filings(cik)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_filed_date ON filings(filed_date)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_acceptance_date ON filings(acceptance_date)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_accession_number ON filings(accession_number)
            """)

            # 뉴스 테이블
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS news (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker VARCHAR(10) NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT,
                    url TEXT NOT NULL,
                    source VARCHAR(255),
                    published_at TIMESTAMP,
                    content TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(ticker, url)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_news_ticker_published
                ON news(ticker, published_at)
            """)
            try:
                cursor.execute("SELECT content FROM news LIMIT 1")
            except sqlite3.OperationalError:
                cursor.execute("""
                    ALTER TABLE news ADD COLUMN content TEXT
                """)
                print("news.content 컬럼을 추가했습니다.")
            
            conn.commit()
            print(f"데이터베이스 초기화 완료: {self.db_path}")
    
    def check_duplicate(self, accession_number: str) -> bool:
        """
        중복 체크 (accession_number 기준)
        
        Args:
            accession_number: 접수 번호
            
        Returns:
            중복이면 True, 아니면 False
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM filings WHERE accession_number = ?
            """, (accession_number,))
            count = cursor.fetchone()[0]
            return count > 0
    
    def save_filing(
        self,
        ticker: str,
        filing_info: Dict,
        file_path: Path
    ) -> Optional[int]:
        """
        공시 자료를 데이터베이스에 저장
        
        Args:
            ticker: 주식 티커 심볼
            filing_info: 공시 정보 딕셔너리 (form, filed, reporting_for, filing_entity, accession_number, cik 포함)
            file_path: 다운로드된 파일 경로
            
        Returns:
            저장된 레코드의 ID 또는 None (중복인 경우)
        """
        accession_number = filing_info.get("accession_number")
        
        # 중복 체크
        if self.check_duplicate(accession_number):
            print(f"이미 저장된 공시 자료입니다: {accession_number}")
            try:
                file_path.unlink(missing_ok=True)
            except Exception:
                pass
            return None
        
        # 파일 크기 계산
        file_size = file_path.stat().st_size if file_path.exists() else None
        
        # 파일 형식 추출
        file_format = file_path.suffix[1:] if file_path.suffix else "unknown"
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    INSERT INTO filings (
                        ticker, cik, accession_number, form, filed_date,
                        acceptance_date, reporting_for, filing_entity, file_path, file_format, file_size
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    ticker.upper(),
                    filing_info.get("cik"),
                    accession_number,
                    filing_info.get("form"),
                    filing_info.get("filed_date") or filing_info.get("filed"),
                    filing_info.get("acceptance_date"),  # SEC에 올라온 날짜
                    filing_info.get("reporting_for"),
                    filing_info.get("filing_entity"),
                    str(file_path),
                    file_format,
                    file_size
                ))
                
                conn.commit()
                record_id = cursor.lastrowid
                print(f"DB 저장 완료: ID {record_id}, {ticker} - {filing_info.get('form')}")
                return record_id
                
            except sqlite3.IntegrityError as e:
                print(f"DB 저장 실패 (중복 또는 제약 조건 위반): {e}")
                return None
            except Exception as e:
                print(f"DB 저장 중 오류 발생: {e}")
                conn.rollback()
                return None
    
    def get_filings_by_ticker(self, ticker: str, limit: Optional[int] = None) -> List[Dict]:
        """
        티커로 공시 자료 조회
        
        Args:
            ticker: 주식 티커 심볼
            limit: 조회할 최대 개수 (None이면 전체)
            
        Returns:
            공시 자료 리스트
        """
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row  # 딕셔너리 형태로 결과 반환
            cursor = conn.cursor()
            
            query = """
                SELECT * FROM filings 
                WHERE ticker = ? 
                ORDER BY filed_date DESC
            """
            
            if limit:
                query += f" LIMIT {limit}"
            
            cursor.execute(query, (ticker.upper(),))
            rows = cursor.fetchall()
            
            return [dict(row) for row in rows]

    def get_filings_between(
        self,
        ticker: str,
        start_time: datetime,
        end_time: datetime
    ) -> List[Dict]:
        """
        특정 기간 동안의 공시 자료 조회
        """
        start_iso = start_time.date().isoformat()
        end_iso = end_time.date().isoformat()
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM filings
                WHERE ticker = ?
                  AND acceptance_date IS NOT NULL
                  AND acceptance_date BETWEEN ? AND ?
                ORDER BY acceptance_date DESC
                """,
                (ticker.upper(), start_iso, end_iso)
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_latest_annual_quarterly(self, ticker: str) -> Dict[str, Dict]:
        """
        가장 최근 10-K (연간보고서)와 10-Q (분기보고서)를 가져옴
        
        Returns:
            {'10-K': {...} or None, '10-Q': {...} or None}
        """
        result = {'10-K': None, '10-Q': None}
        
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # 가장 최근 10-K
            cursor.execute("""
                SELECT * FROM filings 
                WHERE ticker = ? AND form = '10-K'
                ORDER BY filed_date DESC
                LIMIT 1
            """, (ticker.upper(),))
            row = cursor.fetchone()
            if row:
                result['10-K'] = dict(row)
            
            # 가장 최근 10-Q
            cursor.execute("""
                SELECT * FROM filings 
                WHERE ticker = ? AND form = '10-Q'
                ORDER BY filed_date DESC
                LIMIT 1
            """, (ticker.upper(),))
            row = cursor.fetchone()
            if row:
                result['10-Q'] = dict(row)
        
        return result

    def save_news_items(self, ticker: str, news_items: List[Dict]) -> int:
        """
        뉴스 데이터를 저장 (중복은 무시)
        """
        if not news_items:
            return 0
        inserted = 0
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for item in news_items:
                try:
                    cursor.execute(
                        """
                        INSERT OR IGNORE INTO news
                        (ticker, title, summary, url, source, published_at, content)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            (item.get("ticker") or ticker).upper(),
                            item.get("title"),
                            item.get("summary"),
                            item.get("url"),
                            item.get("source"),
                            item.get("published_at"),
                            item.get("content"),
                        )
                    )
                    if cursor.rowcount > 0:
                        inserted += 1
                except Exception as exc:
                    print(f"❌ 뉴스 저장 실패: {exc}")
            conn.commit()
        return inserted

    def get_news(
        self,
        ticker: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: Optional[int] = None
    ) -> List[Dict]:
        """
        뉴스 데이터 조회
        """
        params: List = [ticker.upper()]
        conditions = ["ticker = ?"]
        if start_time and end_time:
            conditions.append("published_at BETWEEN ? AND ?")
            params.extend([start_time.isoformat(), end_time.isoformat()])
        query = f"""
            SELECT * FROM news
            WHERE {' AND '.join(conditions)}
            ORDER BY published_at DESC
        """
        if limit:
            query += f" LIMIT {limit}"
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_news_without_content(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: Optional[int] = None,
        tickers: Optional[List[str]] = None,
    ) -> List[Dict]:
        """
        본문이 비어 있는 뉴스 조회
        """
        params: List = []
        conditions = ["(content IS NULL OR content = '')"]

        if tickers:
            placeholders = ",".join(["?"] * len(tickers))
            conditions.append(f"UPPER(ticker) IN ({placeholders})")
            params.extend([t.upper() for t in tickers])

        if start_time and end_time:
            conditions.append("published_at BETWEEN ? AND ?")
            params.extend([start_time.isoformat(), end_time.isoformat()])

        where_clause = " AND ".join(conditions)
        query = f"""
            SELECT * FROM news
            WHERE {where_clause}
            ORDER BY published_at DESC
        """
        if limit:
            query += f" LIMIT {limit}"

        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def update_news_content(self, news_id: int, content: str) -> bool:
        """
        뉴스 본문 업데이트
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    UPDATE news
                    SET content = ?
                    WHERE id = ?
                    """,
                    (content, news_id),
                )
                conn.commit()
                return cursor.rowcount > 0
            except Exception as exc:
                print(f"뉴스 본문 업데이트 실패 (id={news_id}): {exc}")
                conn.rollback()
                return False
    
    def get_filing_by_accession(self, accession_number: str) -> Optional[Dict]:
        """
        접수 번호로 공시 자료 조회
        
        Args:
            accession_number: 접수 번호
            
        Returns:
            공시 자료 딕셔너리 또는 None
        """
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM filings WHERE accession_number = ?
            """, (accession_number,))
            row = cursor.fetchone()
            
            return dict(row) if row else None
    
    def get_statistics(self) -> Dict:
        """
        데이터베이스 통계 정보 반환
        
        Returns:
            통계 정보 딕셔너리
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 전체 레코드 수
            cursor.execute("SELECT COUNT(*) FROM filings")
            total_count = cursor.fetchone()[0]
            
            # 티커별 개수
            cursor.execute("""
                SELECT ticker, COUNT(*) as count 
                FROM filings 
                GROUP BY ticker 
                ORDER BY count DESC
            """)
            by_ticker = {row[0]: row[1] for row in cursor.fetchall()}
            
            # 형식별 개수
            cursor.execute("""
                SELECT form, COUNT(*) as count 
                FROM filings 
                GROUP BY form 
                ORDER BY count DESC
            """)
            by_form = {row[0]: row[1] for row in cursor.fetchall()}
            
            return {
                "total_filings": total_count,
                "by_ticker": by_ticker,
                "by_form": by_form
            }


class QuartrDatabase:
    """Quartr earnings call 트랜스크립트를 저장/조회하는 데이터베이스 클래스"""

    def __init__(self, db_path: str = "quartr_calls.db"):
        self.db_path = db_path
        self.init_db()

    def get_connection(self):
        return sqlite3.connect(self.db_path)

    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS earning_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker VARCHAR(10) NOT NULL,
                    quartr_event_id VARCHAR(64) UNIQUE NOT NULL,
                    call_date TIMESTAMP NOT NULL,
                    call_type VARCHAR(50),
                    timezone VARCHAR(50),
                    source_url TEXT,
                    transcript_hash VARCHAR(64),
                    transcript_text TEXT,
                    transcript_path TEXT,
                    transcript_size INTEGER,
                    language VARCHAR(20),
                    downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS quartr_fetch_state (
                    ticker VARCHAR(10) PRIMARY KEY,
                    last_call_datetime TIMESTAMP,
                    last_success_run TIMESTAMP,
                    last_cursor TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_earning_calls_ticker_calldate
                ON earning_calls(ticker, call_date)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_earning_calls_hash
                ON earning_calls(transcript_hash)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_quartr_fetch_state_last_call
                ON quartr_fetch_state(last_call_datetime)
                """
            )

            conn.commit()
            print(f"Quartr DB 초기화 완료: {self.db_path}")

    def _normalize_datetime(self, value: Optional[datetime]) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    def check_duplicate_event(self, event_id: str) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) FROM earning_calls WHERE quartr_event_id = ?
                """,
                (event_id,),
            )
            return cursor.fetchone()[0] > 0

    def save_earning_call(
        self,
        ticker: str,
        call_info: Dict,
        transcript_text: Optional[str] = None,
        transcript_path: Optional[Path] = None,
    ) -> Optional[int]:
        """
        Quartr에서 수집한 컨퍼런스 콜 정보를 저장합니다.
        transcript_text 또는 transcript_path 중 하나만 있어도 됩니다.
        """
        event_id = call_info.get("event_id")
        if not event_id:
            raise ValueError("call_info에 event_id가 필요합니다.")

        if self.check_duplicate_event(event_id):
            print(f"이미 저장된 Quartr 이벤트입니다: {event_id}")
            return None

        transcript_hash = call_info.get("transcript_hash")
        if not transcript_hash and transcript_text:
            transcript_hash = hashlib.sha256(transcript_text.encode("utf-8")).hexdigest()

        stored_path: Optional[str] = None
        file_size: Optional[int] = None
        if transcript_path:
            path_obj = Path(transcript_path)
            stored_path = str(path_obj)
            if path_obj.exists():
                file_size = path_obj.stat().st_size

        call_datetime = call_info.get("call_datetime")
        call_datetime_str = self._normalize_datetime(call_datetime)
        if not call_datetime_str:
            raise ValueError("call_info에 call_datetime이 필요합니다.")

        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO earning_calls (
                        ticker,
                        quartr_event_id,
                        call_date,
                        call_type,
                        timezone,
                        source_url,
                        transcript_hash,
                        transcript_text,
                        transcript_path,
                        transcript_size,
                        language
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ticker.upper(),
                        event_id,
                        call_datetime_str,
                        call_info.get("call_type"),
                        call_info.get("timezone"),
                        call_info.get("source_url"),
                        transcript_hash,
                        transcript_text,
                        stored_path,
                        file_size,
                        call_info.get("language"),
                    ),
                )
                conn.commit()
                record_id = cursor.lastrowid
                print(f"Quartr DB 저장 완료: ID {record_id}, {ticker}")
                return record_id
            except sqlite3.IntegrityError as exc:
                print(f"Quartr DB 저장 실패 (무결성 위반): {exc}")
                return None
            except Exception as exc:
                conn.rollback()
                print(f"Quartr DB 저장 중 오류 발생: {exc}")
                return None

    def get_calls_by_ticker(
        self, ticker: str, limit: Optional[int] = None
    ) -> List[Dict]:
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            query = """
                SELECT *
                FROM earning_calls
                WHERE ticker = ?
                ORDER BY call_date DESC
            """
            if limit:
                query += f" LIMIT {limit}"
            cursor.execute(query, (ticker.upper(),))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_call_by_event_id(self, event_id: str) -> Optional[Dict]:
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM earning_calls WHERE quartr_event_id = ?
                """,
                (event_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_fetch_state(self, ticker: str) -> Optional[Dict]:
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT *
                FROM quartr_fetch_state
                WHERE ticker = ?
                """,
                (ticker.upper(),),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_fetch_state(
        self,
        ticker: str,
        *,
        last_call_datetime: Optional[datetime] = None,
        last_cursor: Optional[str] = None,
        last_success_run: Optional[datetime] = None,
    ) -> None:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO quartr_fetch_state (
                    ticker,
                    last_call_datetime,
                    last_success_run,
                    last_cursor,
                    updated_at
                )
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(ticker) DO UPDATE SET
                    last_call_datetime=COALESCE(excluded.last_call_datetime, quartr_fetch_state.last_call_datetime),
                    last_success_run=COALESCE(excluded.last_success_run, quartr_fetch_state.last_success_run),
                    last_cursor=COALESCE(excluded.last_cursor, quartr_fetch_state.last_cursor),
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    ticker.upper(),
                    self._normalize_datetime(last_call_datetime),
                    self._normalize_datetime(last_success_run),
                    last_cursor,
                ),
            )
            conn.commit()

    def mark_successful_run(self, ticker: str, run_time: Optional[datetime] = None):
        """최근 성공 실행 시간을 업데이트합니다."""
        self.update_fetch_state(
            ticker,
            last_success_run=run_time or datetime.utcnow(),
        )
