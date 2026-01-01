from __future__ import annotations

import textwrap
import json
from typing import Optional, List, Dict, Any, Callable

from openai import OpenAI


class AgentToolkit:
    """
    멀티에이전트에서 공용으로 사용하는 LLM 툴 모음.
    - summarize: 문자열과 프롬프트를 입력받아 요약
    - chat_with_tools: 도구를 사용하는 대화
    추후 감성 분석, 리포트 생성 등 함수도 이 클래스에 확장 가능.
    """

    def __init__(self, model: str = "gpt-5.1-chat-latest"):
        self.client = OpenAI()
        self.model = model
        self._tools: Dict[str, Callable] = {}
        self._tool_definitions: List[Dict] = []

    def register_tool(self, name: str, description: str, parameters: Dict, handler: Callable):
        """도구 등록"""
        self._tools[name] = handler
        self._tool_definitions.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters
            }
        })
    
    def clear_tools(self):
        """등록된 도구 초기화"""
        self._tools = {}
        self._tool_definitions = []
    
    def chat_with_tools(self, instruction: str, max_retries: int = 3) -> str:
        """
        도구를 사용할 수 있는 대화 (Function Calling)
        
        Args:
            instruction: 프롬프트
            max_retries: 최대 재시도 횟수
        
        Returns:
            최종 LLM 응답 텍스트
        """
        messages = [
            {"role": "system", "content": "당신은 주식 분석 전문가입니다. 필요한 경우에만 도구를 사용하세요."},
            {"role": "user", "content": instruction}
        ]
        
        for attempt in range(max_retries):
            try:
                # 도구가 있으면 tools 파라미터 포함
                kwargs = {
                    "model": self.model,
                    "messages": messages,
                    "max_completion_tokens": 2000,
                    "timeout": 30,
                }
                
                if self._tool_definitions:
                    kwargs["tools"] = self._tool_definitions
                    kwargs["tool_choice"] = "auto"
                
                response = self.client.chat.completions.create(**kwargs)
                message = response.choices[0].message
                
                # 도구 호출이 있는 경우
                if message.tool_calls:
                    print(f"🔧 Tool Calling 감지: {len(message.tool_calls)}개 도구 호출")
                    
                    # 도구 결과를 메시지에 추가
                    messages.append(message)
                    
                    for tool_call in message.tool_calls:
                        func_name = tool_call.function.name
                        func_args = json.loads(tool_call.function.arguments)
                        
                        print(f"   → {func_name}({func_args})")
                        
                        # 도구 실행
                        if func_name in self._tools:
                            result = self._tools[func_name](**func_args)
                            print(f"   ← 결과: {str(result)[:100]}...")
                        else:
                            result = f"Unknown tool: {func_name}"
                        
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": str(result)
                        })
                    
                    # 도구 결과로 다시 응답 생성
                    final_response = self.client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        max_completion_tokens=2000,
                        timeout=30,
                    )
                    return final_response.choices[0].message.content or ""
                
                # 도구 호출 없으면 바로 반환
                return message.content or ""
            
            except Exception as exc:
                print(f"⚠️  OpenAI API 호출 실패 (시도 {attempt+1}/{max_retries}): {exc}")
                if attempt == max_retries - 1:
                    return f"LLM 호출 실패: {str(exc)[:100]}"
                import time
                time.sleep(2 ** attempt)
        
        return "LLM 호출 실패"

    def summarize(self, content: str, instruction: str, max_retries: int = 3) -> str:
        """
        주어진 instruction/prompt와 원문을 이용해 간단히 요약합니다.
        
        Args:
            content: 원문
            instruction: 프롬프트/지시사항
            max_retries: 최대 재시도 횟수
        
        Returns:
            LLM 응답 텍스트
        """
        # instruction만 있고 content가 비어있는 경우 (prompt가 이미 완성된 경우)
        if instruction and not content:
            prompt = instruction
        elif not content and not instruction:
            return "본문과 지시사항이 모두 없어 요약할 수 없습니다."
        else:
            prompt = textwrap.dedent(
                f"""
                {instruction}

                원문:
                {content[:8000]}
                """
            ).strip()

        # 재시도 로직
        for attempt in range(max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": "당신은 주식 분석 전문가입니다."},
                        {"role": "user", "content": prompt}
                    ],
                    max_completion_tokens=2000,
                    timeout=30,  # 30초 타임아웃
                )
                return response.choices[0].message.content if response.choices else ""
            
            except Exception as exc:
                print(f"⚠️  OpenAI API 호출 실패 (시도 {attempt+1}/{max_retries}): {exc}")
                if attempt == max_retries - 1:
                    return f"LLM 호출 실패: {str(exc)[:100]}"
                import time
                time.sleep(2 ** attempt)  # 지수 백오프 (2초, 4초, 8초)
        
        return "LLM 호출 실패"

    def chat_json(self, prompt: str, max_retries: int = 3) -> dict:
        """
        JSON 형식 응답을 보장하는 대화 (response_format 사용)
        
        Returns:
            파싱된 JSON dict. 실패 시 빈 dict 반환
        """
        import json
        
        for attempt in range(max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": "당신은 주식 분석 전문가입니다. 반드시 유효한 JSON 형식으로만 응답하세요."},
                        {"role": "user", "content": prompt}
                    ],
                    response_format={"type": "json_object"},
                    max_completion_tokens=4000,
                    timeout=60,
                )
                content = response.choices[0].message.content
                if content:
                    return json.loads(content)
                return {}
            
            except Exception as exc:
                print(f"⚠️  JSON API 호출 실패 (시도 {attempt+1}/{max_retries}): {exc}")
                if attempt == max_retries - 1:
                    return {}
                import time
                time.sleep(2 ** attempt)
        
        return {}
