"""SEC filing tools.

Implements minimal SEC EDGAR access as LangChain tools.

Notes:
- Uses on-disk caching under `cache/{BRIEFING_DATE}/sec/`.
- The tools themselves are generic, but in this project they are currently
  used only by the `debate/` module.
"""

from __future__ import annotations

import json
import logging
import os
import re
import math
from datetime import datetime
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from shared.config import ensure_cache_dir

logger = logging.getLogger(__name__)

SEC_BASE_URL = "https://www.sec.gov"
SEC_DATA_URL = "https://data.sec.gov"

DEFAULT_SEC_TIMEOUT_SECS = 30
DEFAULT_SEC_PAGE_CHARS = 20000

SEC_PAGE_SUMMARY_MODEL = "gpt-5-mini"
SEC_PAGE_SUMMARY_MAX_CHARS = 320


def _sec_user_agent() -> str:
    ua = (os.getenv("SEC_USER_AGENT") or "").strip()
    if ua:
        return ua
    # SEC requires a descriptive User-Agent with contact info.
    # We intentionally fail fast to avoid repeated 403s and unclear errors.
    raise EnvironmentError(
        "SEC_USER_AGENT가 필요합니다. 예: SEC_USER_AGENT='your-name-or-org (contact: email@example.com)'"
    )


def _sec_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update({"User-Agent": _sec_user_agent(), "Accept-Encoding": "gzip, deflate"})
    return sess


def _sec_cache_dir() -> Path:
    # Stored under cache/{BRIEFING_DATE}/sec/ to align with other cached inputs.
    cache_dir = ensure_cache_dir()
    sec_dir = cache_dir / "sec"
    sec_dir.mkdir(parents=True, exist_ok=True)
    return sec_dir


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _fetch_json(url: str, *, timeout: int) -> Dict[str, Any]:
    sess = _sec_session()
    resp = sess.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def _clean_for_llm(text: str) -> str:
    # Strip HTML-ish tags aggressively.
    if "<" in text and ">" in text:
        text = re.sub(r"(?s)<[^>]*>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _company_tickers_path(sec_dir: Path) -> Path:
    return sec_dir / "company_tickers.json"


def _submissions_path(sec_dir: Path, cik_padded: str) -> Path:
    return sec_dir / f"submissions_CIK{cik_padded}.json"


def _get_cik_for_ticker(ticker: str, *, timeout: int) -> tuple[str, str]:
    """Return (cik_padded_10, company_title)."""
    sec_dir = _sec_cache_dir()
    path = _company_tickers_path(sec_dir)

    data: Dict[str, Any] | None = None
    if path.exists():
        data = _read_json(path)

    if not data:
        url = f"{SEC_BASE_URL}/files/company_tickers.json"
        data = _fetch_json(url, timeout=timeout)
        _write_json(path, data)

    ticker_up = ticker.strip().upper()
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("ticker") or "").upper() != ticker_up:
            continue
        cik_raw = entry.get("cik_str")
        try:
            cik_int = int(cik_raw)
        except Exception:
            continue
        cik = str(cik_int).zfill(10)
        title = str(entry.get("title") or "").strip()
        return cik, title

    raise ValueError(f"CIK를 찾을 수 없습니다: ticker={ticker_up!r}")


def _load_submissions(cik_padded: str, *, timeout: int) -> Dict[str, Any]:
    sec_dir = _sec_cache_dir()
    path = _submissions_path(sec_dir, cik_padded)
    cached = _read_json(path) if path.exists() else None
    if cached:
        return cached

    url = f"{SEC_DATA_URL}/submissions/CIK{cik_padded}.json"
    data = _fetch_json(url, timeout=timeout)
    _write_json(path, data)
    return data


def _parse_recent_filings(submissions: Dict[str, Any]) -> List[Dict[str, Any]]:
    filings = submissions.get("filings", {})
    recent = filings.get("recent", {}) if isinstance(filings, dict) else {}
    if not isinstance(recent, dict):
        return []

    forms = recent.get("form") or []
    filing_dates = recent.get("filingDate") or []
    report_dates = recent.get("reportDate") or []
    accession_numbers = recent.get("accessionNumber") or []
    primary_docs = recent.get("primaryDocument") or []

    out: List[Dict[str, Any]] = []
    n = min(len(forms), len(filing_dates), len(accession_numbers))
    for i in range(n):
        form = str(forms[i] or "").strip()
        filed_date = str(filing_dates[i] or "").strip()
        accession = str(accession_numbers[i] or "").strip()
        report_date = str(report_dates[i] or "").strip() if i < len(report_dates) else ""
        primary_doc = str(primary_docs[i] or "").strip() if i < len(primary_docs) else ""
        if not form or not filed_date or not accession:
            continue
        out.append(
            {
                "form": form,
                "filed_date": filed_date,
                "report_date": report_date or None,
                "accession_number": accession,
                "primary_document": primary_doc or None,
            }
        )
    return out


@tool
def get_sec_filing_list(ticker: str, forms: Optional[List[str]] = None, limit: int = 6) -> Dict[str, Any]:
    """List recent SEC filings for a ticker (metadata only).

    Args:
        ticker: e.g. "GOOG"
        forms: optional filter like ["10-K", "10-Q", "8-K"]
        limit: max number of filings to return

    Returns:
        { ticker, cik, company_name, count, filings: [{form,filed_date,report_date,accession_number,primary_document}, ...] }
    """
    timeout = int(os.getenv("SEC_TIMEOUT", str(DEFAULT_SEC_TIMEOUT_SECS)))
    ticker_up = str(ticker).strip().upper()
    if not ticker_up:
        raise ValueError("ticker가 필요합니다.")

    cik, company_name = _get_cik_for_ticker(ticker_up, timeout=timeout)
    submissions = _load_submissions(cik, timeout=timeout)
    recent = _parse_recent_filings(submissions)

    if forms:
        allowed = {str(f).strip().upper() for f in forms if str(f).strip()}
        recent = [r for r in recent if str(r.get("form") or "").strip().upper() in allowed]

    if limit < 0:
        limit = 0
    recent = recent[:limit]

    return {
        "ticker": ticker_up,
        "cik": cik,
        "company_name": str(company_name or submissions.get("name") or "").strip(),
        "count": len(recent),
        "filings": recent,
    }


def _sec_filing_full_cache_path(ticker: str, accession_number: str) -> Path:
    sec_dir = _sec_cache_dir()
    filings_dir = sec_dir / "filings_full"
    filings_dir.mkdir(parents=True, exist_ok=True)
    acc_no_dash = accession_number.replace("-", "")
    safe_ticker = re.sub(r"[^A-Za-z0-9_\\-\\.]", "_", ticker.strip().upper())
    return filings_dir / f"{safe_ticker}_{acc_no_dash}.txt"


def _sec_filing_index_cache_path(ticker: str, accession_number: str) -> Path:
    sec_dir = _sec_cache_dir()
    index_dir = sec_dir / "filings_index"
    index_dir.mkdir(parents=True, exist_ok=True)
    acc_no_dash = accession_number.replace("-", "")
    safe_ticker = re.sub(r"[^A-Za-z0-9_\\-\\.]", "_", ticker.strip().upper())
    return index_dir / f"{safe_ticker}_{acc_no_dash}.json"


def _build_sec_page_summary_llm(*, model: str) -> ChatOpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY가 설정되지 않았습니다. SEC page summary 생성에 필요합니다.")

    timeout = float(os.getenv("SEC_PAGE_SUMMARY_TIMEOUT", "120"))
    max_retries = int(os.getenv("SEC_PAGE_SUMMARY_MAX_RETRIES", "2"))
    temperature = float(os.getenv("SEC_PAGE_SUMMARY_TEMPERATURE", "0.0"))

    # gpt-5-mini, thinking none (reasoning_effort is omitted)
    return ChatOpenAI(model=model, temperature=temperature, timeout=timeout, max_retries=max_retries)


def _is_model_not_found(exc: Exception) -> bool:
    msg = str(exc).lower()
    return ("model_not_found" in msg) or ("does not exist" in msg and "model" in msg) or ("you do not have access" in msg)


def _summarize_sec_page(
    llm: ChatOpenAI,
    *,
    ticker: str,
    accession_number: str,
    form: str | None,
    filed_date: str | None,
    page: int,
    total_pages: int,
    content: str,
) -> str:
    system = SystemMessage(
        content=(
            "당신은 SEC 공시(10-K/10-Q/8-K 등) 원문을 페이지(청크) 단위로 탐색하기 위한 '페이지 요약'을 작성합니다.\n"
            "요약은 한국어로 1~2문장, 최대 200자 내외로 아주 간결하게 작성하세요.\n"
            "이 페이지(청크)가 다루는 주제/섹션을 식별할 수 있도록 핵심 키워드 중심으로 쓰세요.\n"
            "불필요한 서론/의견/투자조언/가정은 금지합니다. 따옴표/마크다운/줄바꿈은 쓰지 마세요.\n"
            "출력은 요약 문장 텍스트만 반환하세요."
        )
    )

    meta = f"ticker={ticker}, accession_number={accession_number}, form={form or 'N/A'}, filed_date={filed_date or 'N/A'}, page={page}/{total_pages}"
    user = HumanMessage(content=f"[메타]\n{meta}\n\n[페이지 원문(청크)]\n{content}")
    resp = llm.invoke([system, user])
    text = str(resp.content or "").strip()
    text = " ".join(text.split())
    if len(text) > SEC_PAGE_SUMMARY_MAX_CHARS:
        text = text[:SEC_PAGE_SUMMARY_MAX_CHARS].rstrip()
    return text


def _build_or_load_sec_filing_index(
    *,
    ticker: str,
    cik_padded: str,
    accession_number: str,
    form: str | None,
    filed_date: str | None,
    primary_document: str | None,
    full_text: str,
    page_chars: int,
    total_pages: int,
    url: str | None,
) -> tuple[list[dict[str, str]], str | None, bool]:
    """Return (index, url, index_cached)."""
    index_path = _sec_filing_index_cache_path(ticker, accession_number)
    lock_path = Path(str(index_path) + ".lock")

    payload = _read_json(index_path) if index_path.exists() else None
    if isinstance(payload, dict):
        idx = payload.get("index")
        idx_page_chars = payload.get("page_chars")
        idx_text_len = payload.get("text_length")
        idx_total_pages = payload.get("total_pages")
        if (
            isinstance(idx, list)
            and idx_page_chars == page_chars
            and idx_text_len == len(full_text)
            and idx_total_pages == total_pages
            and len(idx) == total_pages
        ):
            index_out: list[dict[str, str]] = []
            for item in idx:
                if not isinstance(item, dict):
                    continue
                p = item.get("page")
                s = item.get("page_summary")
                if not isinstance(p, str) or not isinstance(s, str):
                    continue
                index_out.append({"page": p, "page_summary": s})
            if len(index_out) == total_pages and any(i.get("page_summary", "").strip() for i in index_out):
                url_cached = payload.get("url")
                if not url and isinstance(url_cached, str) and url_cached.strip():
                    url = url_cached.strip()
                return index_out, url, True

    # Prevent duplicate expensive index generation when multiple experts call the tool concurrently.
    lock_timeout = int(os.getenv("SEC_INDEX_LOCK_TIMEOUT", "300"))
    lock_stale = int(os.getenv("SEC_INDEX_LOCK_STALE", "1800"))

    def _try_acquire_lock() -> bool:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("x", encoding="utf-8") as f:
                f.write(f"pid={os.getpid()} utc={datetime.utcnow().isoformat()}Z\n")
            return True
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > lock_stale:
                    lock_path.unlink(missing_ok=True)
                    return _try_acquire_lock()
            except FileNotFoundError:
                return _try_acquire_lock()
            except Exception:
                pass
            return False

    acquired = _try_acquire_lock()
    if not acquired:
        start_wait = time.time()
        while time.time() - start_wait < lock_timeout:
            # Another worker may have finished index generation; reuse it if possible.
            payload2 = _read_json(index_path) if index_path.exists() else None
            if isinstance(payload2, dict):
                idx2 = payload2.get("index")
                if (
                    isinstance(idx2, list)
                    and payload2.get("page_chars") == page_chars
                    and payload2.get("text_length") == len(full_text)
                    and payload2.get("total_pages") == total_pages
                    and len(idx2) == total_pages
                ):
                    index_out2: list[dict[str, str]] = []
                    for item in idx2:
                        if not isinstance(item, dict):
                            continue
                        p = item.get("page")
                        s = item.get("page_summary")
                        if not isinstance(p, str) or not isinstance(s, str):
                            continue
                        index_out2.append({"page": p, "page_summary": s})
                    if len(index_out2) == total_pages and any(i.get("page_summary", "").strip() for i in index_out2):
                        url_cached = payload2.get("url")
                        if not url and isinstance(url_cached, str) and url_cached.strip():
                            url = url_cached.strip()
                        return index_out2, url, True
            time.sleep(0.5)

        # Timeout: fall through and attempt generation anyway.

    # build url hint (even if cached full_text)
    cik_int = str(int(cik_padded))  # remove leading zeros
    acc_no_dash = accession_number.replace("-", "")
    base = f"{SEC_BASE_URL}/Archives/edgar/data/{cik_int}/{acc_no_dash}/"
    url_hint = f"{base}{primary_document}" if primary_document else f"{base}{acc_no_dash}.txt"
    url_final = url or url_hint

    if not os.getenv("OPENAI_API_KEY"):
        logger.warning("OPENAI_API_KEY가 없어 SEC filing page index를 생성할 수 없습니다: %s", accession_number)
        if acquired:
            lock_path.unlink(missing_ok=True)
        return [], url_final, False

    requested_model = (os.getenv("SEC_PAGE_SUMMARY_MODEL") or SEC_PAGE_SUMMARY_MODEL).strip() or SEC_PAGE_SUMMARY_MODEL
    fallback_model = (os.getenv("OPENAI_MODEL") or "gpt-5.1").strip() or "gpt-5.1"
    used_model = requested_model

    try:
        llm = _build_sec_page_summary_llm(model=used_model)
    except Exception as exc:
        logger.warning("SEC page summary LLM 초기화 실패: %s", exc)
        if acquired:
            lock_path.unlink(missing_ok=True)
        return [], url_final, False

    index_out: list[dict[str, str]] = []
    any_summary = False
    for page in range(1, total_pages + 1):
        start = (page - 1) * page_chars
        end = start + page_chars
        chunk = full_text[start:end].rstrip()
        if not chunk:
            summary = ""
        else:
            try:
                summary = _summarize_sec_page(
                    llm,
                    ticker=ticker,
                    accession_number=accession_number,
                    form=form,
                    filed_date=filed_date,
                    page=page,
                    total_pages=total_pages,
                    content=chunk,
                )
            except Exception as exc:
                if used_model != fallback_model and _is_model_not_found(exc):
                    logger.warning(
                        "SEC page summary 모델 접근 실패: %s (model=%s). fallback=%s로 재시도합니다.",
                        accession_number,
                        used_model,
                        fallback_model,
                    )
                    try:
                        used_model = fallback_model
                        llm = _build_sec_page_summary_llm(model=used_model)
                        summary = _summarize_sec_page(
                            llm,
                            ticker=ticker,
                            accession_number=accession_number,
                            form=form,
                            filed_date=filed_date,
                            page=page,
                            total_pages=total_pages,
                            content=chunk,
                        )
                    except Exception as exc2:
                        logger.warning("SEC page summary 재시도 실패: %s page=%d (%s)", accession_number, page, exc2)
                        summary = ""
                else:
                    logger.warning("SEC page summary 실패: %s page=%d (%s)", accession_number, page, exc)
                    summary = ""
        if summary:
            any_summary = True
        index_out.append({"page": str(page), "page_summary": summary})

    if any_summary:
        _write_json(
            index_path,
            {
                "ticker": ticker,
                "accession_number": accession_number,
                "form": form,
                "filed_date": filed_date,
                "url": url_final,
                "page_chars": page_chars,
                "text_length": len(full_text),
                "total_pages": total_pages,
                "index": index_out,
                "summary_model": used_model,
                "generated_at": datetime.utcnow().isoformat() + "Z",
            },
        )

    if acquired:
        lock_path.unlink(missing_ok=True)

    return index_out, url_final, False


def _fetch_filing_text(
    *,
    cik_padded: str,
    accession_number: str,
    primary_document: Optional[str],
    timeout: int,
) -> tuple[str, str]:
    cik_int = str(int(cik_padded))  # remove leading zeros for URL
    acc_no_dash = accession_number.replace("-", "")
    base = f"{SEC_BASE_URL}/Archives/edgar/data/{cik_int}/{acc_no_dash}/"

    candidates = []
    if primary_document:
        candidates.append(primary_document)
    candidates.append(f"{acc_no_dash}.txt")

    sess = _sec_session()
    last_exc: Exception | None = None
    for filename in candidates:
        try:
            url = f"{base}{filename}"
            resp = sess.get(url, timeout=timeout)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            return resp.text, url
        except Exception as exc:
            last_exc = exc
            continue

    raise RuntimeError(f"SEC filing 다운로드 실패: {accession_number}") from last_exc


def _parse_page(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        v = int(value)
    except Exception:
        return None
    return v if v >= 1 else None


@tool
def get_sec_filing_content(ticker: str, accession_numbers: List[str], page: Optional[int] = None) -> Dict[str, Any]:
    """Fetch one "page" of SEC filing text for given accession numbers.

    Args:
        ticker: ticker symbol (e.g., "GOOG")
        accession_numbers: list of SEC accession numbers (e.g., ["0001652044-25-000091"])
        page: (optional) 1-based page number (each page is 20000 chars after cleanup)
            - If page is omitted/null/empty: return `index` only (no `content`)

    Returns:
        - index only (page omitted):
          { count, filings: [{accession_number, form, filed_date, url, index, total_pages, cached}] }
        - page content (page provided):
          { count, filings: [{accession_number, form, filed_date, url, index, page, total_pages, content, cached}] }
    """
    timeout = int(os.getenv("SEC_TIMEOUT", str(DEFAULT_SEC_TIMEOUT_SECS)))
    ticker_up = str(ticker).strip().upper()
    if not ticker_up:
        raise ValueError("ticker가 필요합니다.")
    if not isinstance(accession_numbers, list) or not accession_numbers:
        raise ValueError("accession_numbers가 필요합니다.")

    cik, _company_name = _get_cik_for_ticker(ticker_up, timeout=timeout)
    submissions = _load_submissions(cik, timeout=timeout)
    recent = _parse_recent_filings(submissions)
    meta_by_acc = {str(r.get("accession_number")): r for r in recent if isinstance(r, dict)}

    page_chars = DEFAULT_SEC_PAGE_CHARS
    env_raw = (os.getenv("SEC_FILING_PAGE_CHARS") or "").strip()
    if env_raw:
        try:
            page_chars = int(env_raw)
        except Exception:
            page_chars = DEFAULT_SEC_PAGE_CHARS
    if page_chars <= 0:
        page_chars = DEFAULT_SEC_PAGE_CHARS

    page_req = _parse_page(page)
    index_only = page_req is None

    out: List[Dict[str, Any]] = []
    for acc in accession_numbers:
        acc_str = str(acc).strip()
        if not acc_str:
            continue

        meta = meta_by_acc.get(acc_str, {})
        form = str(meta.get("form") or "").strip()
        filed_date = str(meta.get("filed_date") or "").strip()
        primary_document = meta.get("primary_document") if isinstance(meta, dict) else None
        primary_document_str = str(primary_document or "").strip() or None

        cache_path = _sec_filing_full_cache_path(ticker_up, acc_str)
        cached = cache_path.exists()
        full_text = cache_path.read_text(encoding="utf-8", errors="ignore") if cached else ""

        try:
            url: str | None = None
            if not full_text.strip():
                raw_text, url = _fetch_filing_text(
                    cik_padded=cik,
                    accession_number=acc_str,
                    primary_document=primary_document_str,
                    timeout=timeout,
                )
                full_text = _clean_for_llm(raw_text)
                cache_path.write_text(full_text, encoding="utf-8")
                cached = False

            total_pages = int(math.ceil(len(full_text) / page_chars)) if full_text else 0
            if total_pages <= 0:
                total_pages = 0
                index: list[dict[str, str]] = []
                if index_only:
                    out.append(
                        {
                            "accession_number": acc_str,
                            "form": form or None,
                            "filed_date": filed_date or None,
                            "url": url,
                            "index": index,
                            "total_pages": total_pages,
                            "cached": cached,
                        }
                    )
                else:
                    out.append(
                        {
                            "accession_number": acc_str,
                            "form": form or None,
                            "filed_date": filed_date or None,
                            "url": url,
                            "index": index,
                            "page": 1,
                            "total_pages": total_pages,
                            "content": "",
                            "cached": cached,
                        }
                    )
                continue

            index, url, _index_cached = _build_or_load_sec_filing_index(
                ticker=ticker_up,
                cik_padded=cik,
                accession_number=acc_str,
                form=form or None,
                filed_date=filed_date or None,
                primary_document=primary_document_str,
                full_text=full_text,
                page_chars=page_chars,
                total_pages=total_pages,
                url=url,
            )

            if index_only:
                out.append(
                    {
                        "accession_number": acc_str,
                        "form": form or None,
                        "filed_date": filed_date or None,
                        "url": url,
                        "index": index,
                        "total_pages": total_pages,
                        "cached": cached,
                    }
                )
                continue

            page_actual = min(page_req or 1, total_pages)
            start = (page_actual - 1) * page_chars
            end = start + page_chars
            content = full_text[start:end].rstrip()

            out.append(
                {
                    "accession_number": acc_str,
                    "form": form or None,
                    "filed_date": filed_date or None,
                    "url": url,
                    "index": index,
                    "page": page_actual,
                    "total_pages": total_pages,
                    "content": content,
                    "cached": cached,
                }
            )
        except Exception as exc:
            logger.warning("SEC filing fetch failed: %s (%s)", acc_str, exc)
            base_err: Dict[str, Any] = {
                "accession_number": acc_str,
                "form": form or None,
                "filed_date": filed_date or None,
                "url": None,
                "index": [],
                "total_pages": 0,
                "cached": cached,
                "error": str(exc),
            }
            if not index_only:
                base_err.update({"page": page_req or 1, "content": ""})
            out.append(base_err)

    return {"count": len(out), "filings": out}
