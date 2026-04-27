from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from openai import AsyncOpenAI

from app.config import settings
from app.models import Employee, Message


logger = logging.getLogger(__name__)


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
        "unnecessary sensitive personal details. Ask at most 3 questions in total "
        "(including the opening question already sent). After the employee answers "
        "the third question, set conversation_done to true and produce the report. "
        "Your goal is to capture how the employee feels about work, the main "
        "workplace concern, useful context for HR, and appropriate follow-up. "
        "Return only valid JSON with exactly these keys: reply_to_employee, "
        "conversation_done, report_markdown. report_markdown must be null until "
        "conversation_done is true. When conversation_done is true, report_markdown "
        "must contain one complete HR-facing report formatted as well-structured "
        "GitHub-Flavored Markdown using these sections in order: "
        "## Summary (2-3 sentence overview), "
        "## Sentiment (one of Positive / Neutral / Concerned / At-risk, with one-line justification), "
        "## Key Themes (bulleted list), "
        "## Notable Quotes (blockquoted lines from the employee), "
        "## Suggested Follow-up (numbered actions for HR). "
        "Use Markdown headings (##), bullet lists (-), numbered lists (1.), and "
        "blockquotes (>) appropriately. Do not wrap the markdown in code fences. "
        "Do not return separate risk_level, primary_theme, summary, or suggested_actions fields."
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


_FENCE_RE = re.compile(r"^\s*```(?:md|markdown)?\s*\n(.*?)\n?\s*```\s*$", re.DOTALL | re.IGNORECASE)


def strip_markdown_fence(text: str) -> str:
    match = _FENCE_RE.match(text)
    return match.group(1).strip() if match else text.strip()


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
    if isinstance(report, str):
        parsed["report_markdown"] = strip_markdown_fence(report)
    return parsed


async def generate_bot_turn(employee: Employee, messages: list[Message]) -> dict[str, Any]:
    client = _get_client()
    logger.info("LLM call start: model=%s turn=%d employee=%s", settings.openrouter_model, len(messages), employee.name)
    started = time.perf_counter()
    try:
        response = await client.chat.completions.create(
            model=settings.openrouter_model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": build_system_prompt()},
                {"role": "user", "content": build_conversation_context(employee, messages)},
            ],
        )
    except Exception as exc:
        elapsed = time.perf_counter() - started
        logger.warning("LLM call failed after %.2fs: %s", elapsed, exc)
        raise LLMError(f"LLM request failed: {exc}") from exc
    elapsed = time.perf_counter() - started
    usage = getattr(response, "usage", None)
    tokens = f"in={usage.prompt_tokens} out={usage.completion_tokens}" if usage else "tokens=?"
    logger.info("LLM call done in %.2fs (%s)", elapsed, tokens)
    content = response.choices[0].message.content or ""
    return parse_llm_json(content)
