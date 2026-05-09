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


def build_system_prompt(
    total_questions: int,
    custom_questions: list[str],
    turn_cap: int,
    force_finalize: bool = False,
) -> str:
    extra = max(0, turn_cap - total_questions)
    custom_block = ""
    if custom_questions:
        rendered = "\n".join(f"- {q}" for q in custom_questions)
        custom_block = (
            f"\nThe HR admin requires you to ask the following {len(custom_questions)} "
            f"question(s) verbatim or with only minor rewording for tone, spread across "
            f"the conversation. Do not skip them. Do not duplicate their topics in your "
            f"own follow-ups:\n{rendered}\n"
        )
    finalize_block = ""
    if force_finalize:
        finalize_block = (
            "\nThe turn budget is exhausted. You MUST set conversation_done=true on "
            "this turn and emit the final report_markdown now."
        )
    return (
        "You are a confidential employee pulse assistant helping HR identify workplace "
        "concerns early and take supportive action. Ask short, respectful follow-up "
        "questions. Do not sound accusatory. Do not make promises HR cannot keep. "
        "Do not diagnose medical or mental health conditions. Avoid collecting "
        "unnecessary sensitive personal details. "
        f"This survey asks exactly {total_questions} substantive question(s) in total. "
        f"You have a budget of {turn_cap} bot turns to deliver them — that's "
        f"{total_questions} for the questions plus {extra} buffer turn(s) for natural "
        "lead-ins, smooth transitions between topics, and the final acknowledgement. "
        "Spend the buffer where it helps the conversation feel human, not only at the "
        "end. Make this feel like one coherent conversation, not a checklist: briefly "
        "acknowledge what the employee just said before moving to a new topic, and "
        "reference earlier answers when it makes the next question land more "
        "naturally. Do not jump from one unrelated question to the next. "
        f"While substantive questions remain unasked, set conversation_done=false. "
        f"Once all {total_questions} substantive questions have been asked AND the "
        "employee has answered the last one, set conversation_done=true. On that "
        "closing turn, reply_to_employee MUST be a brief, warm acknowledgement (e.g., "
        "\"Thanks for sharing — your input has been recorded confidentially.\") and "
        "MUST NOT contain a new question or any sentence ending in '?'. The system "
        "will send the survey-complete message right after your acknowledgement."
        f"{custom_block}"
        f"{finalize_block} "
        "Your goal is to capture how the employee feels about work, the main "
        "workplace concern, useful context for HR, and appropriate follow-up. "
        "Return only valid JSON with exactly these keys: reply_to_employee, "
        "conversation_done, report_markdown, sentiment_score. report_markdown and "
        "sentiment_score must be null until conversation_done is true. When "
        "conversation_done is true, report_markdown must contain one complete "
        "HR-facing report formatted as well-structured GitHub-Flavored Markdown "
        "using these sections in order: "
        "## Summary (2-3 sentence overview), "
        "## Sentiment (one of Positive / Neutral / Concerned / At-risk, with one-line justification), "
        "## Key Themes (bulleted list), "
        "## Notable Quotes (blockquoted lines from the employee), "
        "## Suggested Follow-up (numbered actions for HR). "
        "Use Markdown headings (##), bullet lists (-), numbered lists (1.), and "
        "blockquotes (>) appropriately. Do not wrap the markdown in code fences. "
        "Do not return separate risk_level, primary_theme, summary, or suggested_actions fields. "
        "When conversation_done is true, sentiment_score must be an integer from 0 "
        "to 100 reflecting the employee's overall workplace sentiment in this "
        "conversation: 0 means severe distress or strong dissatisfaction, 50 means "
        "neutral or mixed, 100 means clearly positive and engaged. Lower scores "
        "should map to greater HR urgency. "
        "Ground every statement in the report in the employee context provided and the "
        "actual conversation transcript. Do NOT invent or assume specifics that the "
        "employee did not state — including budgets, deadlines, project timelines, "
        "headcount, manager names or actions, team dynamics, performance history, or "
        "any other concrete details. If the employee did not mention something, omit it "
        "rather than guess. Notable Quotes must be verbatim excerpts from the employee's "
        "messages, not paraphrased or fabricated."
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
    if isinstance(report, str):
        parsed["report_markdown"] = strip_markdown_fence(report)
    if parsed["conversation_done"]:
        parsed["sentiment_score"] = _coerce_score(parsed.get("sentiment_score"))
    else:
        parsed["sentiment_score"] = None
    return parsed


def _coerce_score(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        score = int(round(value))
    elif isinstance(value, str):
        try:
            score = int(round(float(value.strip())))
        except (ValueError, AttributeError):
            return None
    else:
        return None
    return max(0, min(100, score))


def _extract_usage(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    if not usage:
        return {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
    raw = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
    return {
        "prompt_tokens": int(raw.get("prompt_tokens") or 0),
        "completion_tokens": int(raw.get("completion_tokens") or 0),
        "cost_usd": float(raw.get("cost") or 0),
    }


async def generate_bot_turn(
    employee: Employee,
    messages: list[Message],
    total_questions: int,
    turn_cap: int,
    custom_questions: list[str] | None = None,
    force_finalize: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    client = _get_client()
    extra = max(0, turn_cap - total_questions)
    logger.info(
        "LLM call start: model=%s turn=%d employee=%s budget=%d cap=%d extra=%d customs=%d finalize=%s",
        settings.openrouter_model,
        len(messages),
        employee.name,
        total_questions,
        turn_cap,
        extra,
        len(custom_questions or []),
        force_finalize,
    )
    started = time.perf_counter()
    try:
        response = await client.chat.completions.create(
            model=settings.openrouter_model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": build_system_prompt(total_questions, custom_questions or [], turn_cap, force_finalize)},
                {"role": "user", "content": build_conversation_context(employee, messages)},
            ],
        )
    except Exception as exc:
        elapsed = time.perf_counter() - started
        logger.warning("LLM call failed after %.2fs: %s", elapsed, exc)
        raise LLMError(f"LLM request failed: {exc}") from exc
    elapsed = time.perf_counter() - started
    usage = _extract_usage(response)
    logger.info(
        "LLM call done in %.2fs (in=%d out=%d cost=$%.6f)",
        elapsed,
        usage["prompt_tokens"],
        usage["completion_tokens"],
        usage["cost_usd"],
    )
    content = response.choices[0].message.content or ""
    return parse_llm_json(content), usage
