"""
LangGraph 기반 중재자(Moderator) 토론 파이프라인
- 중재자가 쟁점 정리 및 추가 토론 필요 여부 판단
- 전문가들은 중재자 가이드에 따라 데이터 기반 응답
"""

from __future__ import annotations

from typing import Any, Dict, List, TypedDict
import concurrent.futures

from langgraph.graph import StateGraph, START, END

from multiagent.nodes.data_collector import prepare_ticker_dataset
from multiagent.services import AgentToolkit
from multiagent.services.conclusion_parser import ConclusionParser
from multiagent.agents.fundamental_analyst import FundamentalAnalyst
from multiagent.agents.risk_manager import RiskManager
from multiagent.agents.growth_analyst import GrowthAnalyst
from multiagent.agents.sentiment_analyst import SentimentAnalyst
from multiagent.agents.moderator import Moderator
from multiagent.prompts import GUIDED_DEBATE_PROMPT, SENTIMENT_GUIDED_PROMPT
from multiagent.schemas import InvestmentConclusion


class AgentState(TypedDict, total=False):
    ticker: str
    dataset: Dict[str, Any]
    agents: Dict[str, Any]
    moderator: Any
    rounds: List[Dict[str, str]]
    fundamental_statement: str
    risk_statement: str
    growth_statement: str
    sentiment_statement: str
    
    # 중재자 분석 결과
    moderator_analysis: Dict[str, Any]
    moderator_analyses: List[Dict[str, Any]]  # 각 라운드별 중재자 분석 저장
    key_agreements: List[str]
    key_disagreements: List[str]
    
    # 중재자 이전 가이드 (반복 질문 방지)
    previous_moderator_guidance: List[Dict[str, str]]
    
    # 뉴스 캐시 (중복 호출 방지)
    news_cache: Dict[int, str]
    
    # 출처 정보 (검증 에이전트용)
    sources: Dict[str, Any]
    
    should_continue: bool
    debate_transcript: str
    conclusion: str
    readable_summary: str
    structured_conclusion: InvestmentConclusion


def collect_data_node(state: AgentState) -> AgentState:
    """데이터 수집 + 4명의 전문가 초기 분석 (Blind Assessment)"""
    ticker = state["ticker"]
    info = prepare_ticker_dataset(ticker)
    dataset = info["dataset"]
    
    initial_round = {
        "round": 1,
        "fundamental": info["initial_fundamental"],
        "risk": info["initial_risk"],
        "growth": info["initial_growth"],
        "sentiment": info["initial_sentiment"],
    }
    
    print("=" * 100)
    print("🔍 ROUND 1: BLIND ANALYSIS - 각 전문가의 독립적 초기 분석")
    print("=" * 100)
    print("\n💼 Fundamental Analyst (Charlie Munger 스타일)")
    print(info["initial_fundamental"])
    print("\n" + "-" * 100)
    print("⚠️  Risk Manager (Ray Dalio 스타일)")
    print(info["initial_risk"])
    print("\n" + "-" * 100)
    print("🚀 Growth Catalyst Hunter (Cathie Wood 스타일)")
    print(info["initial_growth"])
    print("\n" + "-" * 100)
    print("📊 Market Sentiment Analyst (George Soros 스타일)")
    print(info["initial_sentiment"])
    
    # 에이전트 인스턴스 생성 (재사용)
    toolkit = AgentToolkit()
    agents = {
        "fundamental": FundamentalAnalyst(toolkit),
        "risk": RiskManager(toolkit),
        "growth": GrowthAnalyst(toolkit),
        "sentiment": SentimentAnalyst(toolkit),
    }
    moderator = Moderator(toolkit)
    
    return {
        "ticker": ticker,
        "dataset": dataset,
        "agents": agents,
        "moderator": moderator,
        "rounds": [initial_round],
        "fundamental_statement": info["initial_fundamental"],
        "risk_statement": info["initial_risk"],
        "growth_statement": info["initial_growth"],
        "sentiment_statement": info["initial_sentiment"],
        "key_agreements": [],
        "key_disagreements": [],
        "previous_moderator_guidance": [],
        "news_cache": {},
        "sources": info.get("sources", {}),  # 출처 정보 (검증 에이전트용)
        "should_continue": True,
    }


def moderator_analysis_node(state: AgentState) -> AgentState:
    """중재자가 라운드를 분석하고 쟁점 정리 + 추가 토론 필요 여부 판단"""
    ticker = state.get("ticker", "")
    moderator = state.get("moderator")
    rounds = state.get("rounds", [])
    previous_guidance = state.get("previous_moderator_guidance", [])
    
    if not moderator:
        toolkit = AgentToolkit()
        moderator = Moderator(toolkit)
    
    current_round = len(rounds)
    
    print("\n" + "=" * 100)
    print(f"🎯 MODERATOR ANALYSIS - Round {current_round} 분석")
    print("=" * 100)
    
    # 중재자 분석 (이전 가이드 정보 포함)
    analysis = moderator.analyze_round(
        ticker=ticker,
        fundamental=state.get("fundamental_statement", ""),
        risk=state.get("risk_statement", ""),
        growth=state.get("growth_statement", ""),
        sentiment=state.get("sentiment_statement", ""),
        round_number=current_round,
        previous_guidance=previous_guidance  # 이전 가이드 전달
    )
    
    # 결과 출력
    print(f"\n✅ 합의점:")
    for agreement in analysis.get("key_agreements", []):
        print(f"  • {agreement}")
    
    print(f"\n❌ 쟁점:")
    for disagreement in analysis.get("key_disagreements", []):
        print(f"  • {disagreement}")
    
    needs_more = analysis.get("needs_more_debate", False)
    reason = analysis.get("reason", "")
    
    if needs_more:
        print(f"\n🔄 추가 토론 필요: {reason}")
        guidance = analysis.get("guidance", {})
        print(f"\n📋 다음 라운드 가이드:")
        for expert, guide in guidance.items():
            print(f"  • {expert}: {guide}")
    else:
        print(f"\n✅ 토론 종료: {reason}")
    
    # 최대 라운드 체크 (Round 1 = Blind, Round 2-3 = Guided Debate)
    if current_round >= 4:
        print(f"\n⏱️  최대 라운드 도달 (Round {current_round}) - 종료")
        needs_more = False
    
    # 이전 가이드 목록 업데이트
    new_previous_guidance = list(previous_guidance)
    if analysis.get("guidance"):
        new_previous_guidance.append({
            "round": current_round,
            "guidance": analysis.get("guidance", {})
        })
    
    # 중재자 분석 누적 저장 (JSON 출력용)
    existing_analyses = list(state.get("moderator_analyses", []))
    existing_analyses.append({
        "round": current_round,
        "key_agreements": analysis.get("key_agreements", []),
        "key_disagreements": analysis.get("key_disagreements", []),
        "needs_more_debate": needs_more,
        "reason": reason,
        "guidance": analysis.get("guidance", {})
    })
    
    new_state = dict(state)
    new_state["moderator_analysis"] = analysis
    new_state["moderator_analyses"] = existing_analyses  # 누적 저장
    new_state["key_agreements"] = analysis.get("key_agreements", [])
    new_state["key_disagreements"] = analysis.get("key_disagreements", [])
    new_state["previous_moderator_guidance"] = new_previous_guidance  # 가이드 기록 저장
    new_state["should_continue"] = needs_more
    return new_state


def guided_debate_node(state: AgentState) -> AgentState:
    """중재자 가이드에 따라 데이터 기반 토론 진행"""
    ticker = state.get("ticker", "")
    dataset = state.get("dataset", {})
    agents = state.get("agents", {})
    moderator_analysis = state.get("moderator_analysis", {})
    guidance = moderator_analysis.get("guidance", {})
    news_cache = state.get("news_cache", {})  # 뉴스 캐시
    
    # 에이전트가 없으면 생성 (fallback)
    if not agents:
        toolkit = AgentToolkit()
        agents = {
            "fundamental": FundamentalAnalyst(toolkit),
            "risk": RiskManager(toolkit),
            "growth": GrowthAnalyst(toolkit),
            "sentiment": SentimentAnalyst(toolkit),
        }
    
    rounds = state.get("rounds", [])
    round_number = len(rounds) + 1
    
    # 직전 라운드 의견 수집
    prev_fundamental = state.get("fundamental_statement", "")
    prev_risk = state.get("risk_statement", "")
    prev_growth = state.get("growth_statement", "")
    prev_sentiment = state.get("sentiment_statement", "")
    
    # 데이터 컨텍스트 생성
    market_data = dataset.get("market_data_text", "")
    sec_summary = _summarize_sec_data(dataset.get("sec_filings", []))
    
    # Round 2+에서는 뉴스 헤드라인만 전달 (효율성)
    news_items = dataset.get("aws_news", [])
    news_headlines = _get_news_headlines(news_items)
    
    data_context = f"""
=== 시장 데이터 ===
{market_data}

=== SEC 공시 요약 ===
{sec_summary}

=== 뉴스 헤드라인 (상세 내용은 get_news_detail 도구로 조회 가능) ===
{news_headlines}
"""
    
    # 역할 이름과 가이드 매핑
    role_names = {
        "fundamental": "가치투자 전문가 (Charlie Munger 스타일)",
        "risk": "리스크 관리 전문가 (Ray Dalio 스타일)",
        "growth": "성장주 전문가 (Cathie Wood 스타일)",
        "sentiment": "시장 심리 전문가 (George Soros 스타일)"
    }
    
    opponents_map = {
        "fundamental": f"[Risk] {prev_risk[:300]}...\n[Growth] {prev_growth[:300]}...\n[Sentiment] {prev_sentiment[:300]}...",
        "risk": f"[Fundamental] {prev_fundamental[:300]}...\n[Growth] {prev_growth[:300]}...\n[Sentiment] {prev_sentiment[:300]}...",
        "growth": f"[Fundamental] {prev_fundamental[:300]}...\n[Risk] {prev_risk[:300]}...\n[Sentiment] {prev_sentiment[:300]}...",
        "sentiment": f"[Fundamental] {prev_fundamental[:300]}...\n[Risk] {prev_risk[:300]}...\n[Growth] {prev_growth[:300]}..."
    }
    
    print("\n" + "=" * 100)
    print(f"💬 ROUND {round_number}: GUIDED DEBATE - 중재자 가이드 기반 데이터 중심 토론")
    print("=" * 100)
    
    # 뉴스 조회 도구 핸들러 (캐시 사용)
    def get_news_detail_handler(news_id: int) -> str:
        """뉴스 번호로 상세 내용 조회 (캐시 활용)"""
        # 캐시에 있으면 바로 반환
        if news_id in news_cache:
            print(f"   📦 캐시에서 반환: 뉴스 {news_id}")
            return news_cache[news_id]
        
        # 캐시에 없으면 조회 후 캐시에 저장
        if 1 <= news_id <= len(news_items):
            news = news_items[news_id - 1]
            title = news.get("title") or news.get("pk") or "제목 없음"
            content = news.get("article_raw") or news.get("summary") or "내용 없음"
            result = f"[뉴스 {news_id}] {title}\n\n{content[:1500]}"
            news_cache[news_id] = result  # 캐시에 저장
            return result
        return f"뉴스 {news_id}번을 찾을 수 없습니다."
    
    # 각 에이전트에 tool calling 적용
    def get_guided_response(agent_name: str):
        agent = agents[agent_name]
        toolkit = agent.toolkit
        
        # 도구 초기화 및 등록
        toolkit.clear_tools()
        toolkit.register_tool(
            name="get_news_detail",
            description="뉴스 번호(1-N)로 해당 뉴스의 전체 내용을 조회합니다. 토론에서 특정 뉴스를 인용해야 할 때 사용하세요.",
            parameters={
                "type": "object",
                "properties": {
                    "news_id": {
                        "type": "integer",
                        "description": "뉴스 번호 (1부터 시작)"
                    }
                },
                "required": ["news_id"]
            },
            handler=get_news_detail_handler
        )
        
        # Sentiment Analyst는 뉴스 필수 프롬프트 사용
        if agent_name == "sentiment":
            prompt = SENTIMENT_GUIDED_PROMPT.format(
                moderator_guidance=guidance.get(agent_name, "시장 심리와 뉴스 분석을 제시하세요"),
                opponents=opponents_map[agent_name],
                your_data=data_context
            )
        else:
            prompt = GUIDED_DEBATE_PROMPT.format(
                role=role_names[agent_name],
                moderator_guidance=guidance.get(agent_name, "데이터 기반 근거를 제시하세요"),
                opponents=opponents_map[agent_name],
                your_data=data_context
            )
        
        # tool calling 지원하는 chat 사용
        return toolkit.chat_with_tools(prompt)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            name: executor.submit(get_guided_response, name)
            for name in ["fundamental", "risk", "growth", "sentiment"]
        }
        
        results = {
            name: future.result()
            for name, future in futures.items()
        }
    
    fundamental_reply = results["fundamental"]
    risk_reply = results["risk"]
    growth_reply = results["growth"]
    sentiment_reply = results["sentiment"]
    
    # 출력
    print("\n💼 Fundamental Analyst")
    print(fundamental_reply)
    print("\n" + "-" * 100)
    print("⚠️  Risk Manager")
    print(risk_reply)
    print("\n" + "-" * 100)
    print("🚀 Growth Catalyst Hunter")
    print(growth_reply)
    print("\n" + "-" * 100)
    print("📊 Market Sentiment Analyst")
    print(sentiment_reply)
    
    # 라운드 저장
    rounds = list(state.get("rounds", []))
    rounds.append({
        "round": round_number,
        "fundamental": fundamental_reply,
        "risk": risk_reply,
        "growth": growth_reply,
        "sentiment": sentiment_reply,
    })
    
    new_state = dict(state)
    new_state["rounds"] = rounds
    new_state["fundamental_statement"] = fundamental_reply
    new_state["risk_statement"] = risk_reply
    new_state["growth_statement"] = growth_reply
    new_state["sentiment_statement"] = sentiment_reply
    new_state["news_cache"] = news_cache  # 캐시 저장 (다음 라운드에서 재사용)
    return new_state


def conclusion_node(state: AgentState) -> AgentState:
    """중재자가 최종 결론 생성 (근거 + 출처 기반)"""
    ticker = state.get("ticker", "")
    moderator = state.get("moderator")
    rounds = state.get("rounds", [])
    key_agreements = state.get("key_agreements", [])
    key_disagreements = state.get("key_disagreements", [])
    
    if not moderator:
        toolkit = AgentToolkit()
        moderator = Moderator(toolkit)
    
    print("\n" + "=" * 100)
    print("📋 FINAL CONCLUSION - 근거 기반 최종 결론")
    print("=" * 100)
    
    # 중재자가 최종 결론 생성
    conclusion_text = moderator.generate_final_summary(
        ticker=ticker,
        all_rounds=rounds,
        final_agreements=key_agreements,
        final_disagreements=key_disagreements
    )
    
    print(conclusion_text)
    
    # JSON 파싱
    parser = ConclusionParser()
    confidence = 0.8  # 중재자 기반이므로 기본 신뢰도 높음
    structured_conclusion = parser.parse(ticker, conclusion_text, confidence)
    
    # 읽기 쉬운 요약
    readable_summary = _format_readable_conclusion(structured_conclusion, key_agreements, key_disagreements)
    
    print("\n" + "=" * 100)
    print("📊 한눈에 보는 결론")
    print("=" * 100)
    print(readable_summary)
    
    # 출처 정보 출력
    sources = state.get("sources", {})
    if sources:
        print("\n" + "-" * 100)
        print("📚 참고 자료 (검증용)")
        print("-" * 100)
        
        # type별 카운트
        all_sources = sources.get("sources", [])
        sec_items = [s for s in all_sources if s.get("type") == "sec_filing"]
        news_items = [s for s in all_sources if s.get("type") == "article"]
        chart_items = [s for s in all_sources if s.get("type") == "chart"]
        
        print(f"  • SEC 공시: {len(sec_items)}건")
        for f in sec_items[:3]:
            print(f"    - {f.get('form')} ({f.get('filed_date')})")
        print(f"  • 뉴스 기사: {len(news_items)}건")
        for n in news_items[:3]:
            print(f"    - {n.get('title', '')[:50]}...")
        if chart_items:
            chart = chart_items[0]
            print(f"  • 시장 데이터: yfinance (${chart.get('current_price', 'N/A')})")
    
    new_state = dict(state)
    new_state["conclusion"] = conclusion_text
    new_state["structured_conclusion"] = structured_conclusion
    new_state["readable_summary"] = readable_summary
    new_state["debate_transcript"] = _format_rounds(rounds)
    return new_state


def _summarize_sec_data(sec_filings: List) -> str:
    """SEC 데이터 요약 - 10-K, 10-Q 우선 표시"""
    if not sec_filings:
        return "관련 SEC 공시 없음"
    
    lines = []
    
    # 10-K, 10-Q 먼저 분리
    annual_quarterly = []
    others = []
    
    for filing in sec_filings:
        meta = filing.get("metadata", {})
        form = meta.get("form", "N/A")
        if form in ['10-K', '10-Q']:
            annual_quarterly.append(filing)
        else:
            others.append(filing)
    
    # 10-K, 10-Q 강조 표시 (날짜 명확히)
    if annual_quarterly:
        lines.append("📊 **핵심 재무 공시 (반드시 이 날짜를 인용하세요!):**")
        for filing in annual_quarterly:
            meta = filing.get("metadata", {})
            form = meta.get("form", "N/A")
            filed = meta.get("filed_date") or meta.get("filed") or "N/A"
            reporting_for = meta.get("reporting_for") or "N/A"
            content = filing.get("content", "")[:800] if filing.get("content") else ""
            lines.append(f"  • {form} (제출일: {filed}, 보고기간: {reporting_for})")
            if content:
                lines.append(f"    내용 요약: {content[:500]}...")
        lines.append("")
    
    # 기타 공시 (최근 3개만)
    if others:
        lines.append("📄 최근 기타 공시:")
        for filing in others[:3]:
            meta = filing.get("metadata", {})
            form = meta.get("form", "N/A")
            filed = meta.get("filed_date") or meta.get("filed") or "N/A"
            lines.append(f"  • {form} (제출일: {filed})")
    
    return "\n".join(lines)


def _summarize_news_data(news_items: List) -> str:
    """뉴스 데이터 요약"""
    if not news_items:
        return "관련 뉴스 없음"
    
    lines = []
    for news in news_items[:5]:
        title = news.get("title", "제목 없음")
        summary = news.get("summary") or news.get("article_raw", "")[:200]
        lines.append(f"• {title}: {summary}...")
    
    return "\n".join(lines)


def _get_news_headlines(news_items: List) -> str:
    """뉴스 헤드라인만 추출 (tool calling용)"""
    if not news_items:
        return "관련 뉴스 없음"
    
    lines = []
    for i, news in enumerate(news_items, 1):
        title = news.get("title") or news.get("pk") or "제목 없음"
        published = news.get("published_at") or ""
        lines.append(f"{i}. [{published}] {title}")
    
    lines.append("")
    lines.append("💡 특정 뉴스의 상세 내용이 필요하면 get_news_detail(news_id=번호) 도구를 사용하세요.")
    
    return "\n".join(lines)


def _format_readable_conclusion(
    conclusion: InvestmentConclusion,
    agreements: List[str],
    disagreements: List[str]
) -> str:
    """구조화된 결론을 읽기 쉬운 형태로 포맷"""
    lines = []
    
    action_emoji = {
        "STRONG_BUY": "🟢",
        "BUY": "🔵", 
        "HOLD": "⚪",
        "SELL": "🟠",
        "STRONG_SELL": "🔴"
    }
    emoji = action_emoji.get(conclusion.action, "⚪")
    
    lines.append(f"\n{emoji} **최종 판단: {conclusion.action}**")
    lines.append(f"추천 포지션: {conclusion.position_size}%\n")
    
    # 핵심 요약
    lines.append("**📝 핵심 요약**")
    lines.append(conclusion.executive_summary)
    
    # 합의점
    if agreements:
        lines.append("\n**✅ 전문가 합의**")
        for a in agreements[:3]:
            lines.append(f"• {a}")
    
    # 쟁점
    if disagreements:
        lines.append("\n**⚠️ 미해결 쟁점**")
        for d in disagreements[:2]:
            lines.append(f"• {d}")
    
    # 점수별 근거
    if conclusion.key_debates:
        lines.append("\n**📋 점수별 근거**")
        for reason in conclusion.key_debates:
            lines.append(f"• {reason}")
    
    # 실행 계획
    if conclusion.immediate_action:
        lines.append(f"\n**⚡ 즉시 행동**: {conclusion.immediate_action}")
    
    if conclusion.short_term_strategy:
        lines.append(f"**📅 단기 전략**: {conclusion.short_term_strategy}")
    
    if conclusion.long_term_strategy:
        lines.append(f"**🎯 장기 전략**: {conclusion.long_term_strategy}")
    
    # 트리거
    if conclusion.bullish_trigger:
        lines.append(f"\n**📈 상승 시**: {conclusion.bullish_trigger.condition}")
    
    if conclusion.bearish_trigger:
        lines.append(f"**📉 하락 시**: {conclusion.bearish_trigger.condition}")
    
    return "\n".join(lines)


def _format_rounds(rounds: List[Dict[str, str]]) -> str:
    """토론 기록을 텍스트로 포맷"""
    lines = []
    for entry in rounds:
        rid = entry.get("round")
        lines.append(f"\n{'='*80}")
        lines.append(f"Round {rid}")
        lines.append(f"{'='*80}")
        lines.append(f"\n[Fundamental Analyst]\n{entry.get('fundamental', '')}")
        lines.append(f"\n[Risk Manager]\n{entry.get('risk', '')}")
        lines.append(f"\n[Growth Catalyst Hunter]\n{entry.get('growth', '')}")
        lines.append(f"\n[Market Sentiment Analyst]\n{entry.get('sentiment', '')}")
    return "\n".join(lines)


# 중재자 기반 토론 계속 여부 결정
def should_continue_debate(state: AgentState) -> str:
    """중재자 판단에 따라 토론 계속 여부 결정"""
    should_continue = state.get("should_continue", False)
    
    if should_continue:
        return "guided_debate"
    else:
        return "conclusion"


# LangGraph 구성: 중재자 기반 파이프라인
graph_builder = StateGraph(AgentState)

# 노드 추가
graph_builder.add_node("collect_data", collect_data_node)
graph_builder.add_node("moderator_analysis", moderator_analysis_node)
graph_builder.add_node("guided_debate", guided_debate_node)
graph_builder.add_node("conclusion", conclusion_node)

# 엣지 연결
graph_builder.add_edge(START, "collect_data")
graph_builder.add_edge("collect_data", "moderator_analysis")

# 중재자 분석 후 → 조건부 (추가 토론 필요하면 guided_debate, 아니면 conclusion)
graph_builder.add_conditional_edges(
    "moderator_analysis",
    should_continue_debate,
    {
        "guided_debate": "guided_debate",
        "conclusion": "conclusion"
    }
)

# guided_debate 후 → 다시 moderator_analysis (루프)
graph_builder.add_edge("guided_debate", "moderator_analysis")

# conclusion → END
graph_builder.add_edge("conclusion", END)

compiled_graph = graph_builder.compile()


def run_multiagent_pipeline(ticker: str) -> AgentState:
    """
    중재자 기반 4명의 전문가 토론 파이프라인 실행
    
    Args:
        ticker: 분석할 주식 티커
    
    Returns:
        최종 State (데이터, 토론 기록, 결론 포함)
    """
    initial_state: AgentState = {"ticker": ticker.upper()}
    return compiled_graph.invoke(initial_state)
