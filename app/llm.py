from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from app.config import settings
from app.models import Employee, Message


class LLMError(RuntimeError):
    pass


_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        if not settings.openrouter_key:
            raise LLMError("OPENROUTER_KEY is not configured")
        _client = AsyncOpenAI(base_url=settings.openrouter_base_url, api_key=settings.openrouter_key)
    return _client


def build_system_prompt() -> str:
    return (
        "You are a confidential employee pulse assistant helping HR identify workplace "
        "concerns early and take supportive action. Ask short, respectful follow-up "
        "questions. Do not sound accusatory. Do not make promises HR cannot keep. "
        "Do not diagnose medical or mental health conditions. Avoid collecting "
        "unnecessary sensitive personal details. Stop once enough information has "
        "been collected to summarize how the employee feels about work, the main "
        "workplace concern, useful context for HR, and appropriate follow-up. "
        "Return only valid JSON with exactly these keys: reply_to_employee, "
        "conversation_done, report_markdown. report_markdown must be null until "
        "conversation_done is true. When conversation_done is true, report_markdown "
        "must contain one complete HR-facing Markdown report. Do not return separate "
        "risk_level, primary_theme, summary, or suggested_actions fields."
    )


def build_conversation_context(employee: Employee, messages: list[Message]) -> str:
    lines = [
        "Employee context:",
        f"Name: {employee.name}",
        f"Department: {employee.department}",
        f"Manager: {employee.manager}",
        f"Project: {employee.project}",
        f"Role: {employee.role}",
        "",
        "Conversation so far:",
    ]
    for message in messages:
        speaker = "Employee" if message.direction == "employee" else "Bot"
        lines.append(f"{speaker}: {message.text}")
    return "\n".join(lines)


def parse_llm_json(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LLMError("LLM returned invalid JSON") from exc

    if not isinstance(parsed, dict):
        raise LLMError("LLM JSON response must be an object")
    if not isinstance(parsed.get("reply_to_employee"), str) or not parsed["reply_to_employee"].strip():
        raise LLMError("LLM response is missing reply_to_employee")
    if not isinstance(parsed.get("conversation_done"), bool):
        raise LLMError("LLM response is missing conversation_done")
    report = parsed.get("report_markdown")
    if parsed["conversation_done"] and (not isinstance(report, str) or not report.strip()):
        raise LLMError("Completed LLM response is missing report_markdown")
    if not parsed["conversation_done"] and report is not None:
        raise LLMError("Incomplete LLM response must use null report_markdown")
    return parsed


async def generate_bot_turn(employee: Employee, messages: list[Message]) -> dict[str, Any]:
    client = _get_client()
    response = await client.chat.completions.create(
        model=settings.openrouter_model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": build_conversation_context(employee, messages)},
        ],
    )
    content = response.choices[0].message.content or ""
    return parse_llm_json(content)
