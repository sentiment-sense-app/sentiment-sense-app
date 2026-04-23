import json
import logging
from enum import Enum

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import OPENROUTER_KEY, AI_MODEL, QUESTIONS_PER_ROUND

logger = logging.getLogger(__name__)

client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_KEY)


class QuestionType(str, Enum):
    TEXT = "text"
    SCALE = "scale"
    MULTIPLE_CHOICE = "multiple_choice"


class LLMQuestion(BaseModel):
    text: str
    type: QuestionType
    options: list[str] | None = None


class LLMResponse(BaseModel):
    questions: list[LLMQuestion]


def _strip_fences(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[: -3]
    return content.strip()


def _extract_usage(response) -> dict:
    usage = getattr(response, "usage", None)
    if not usage:
        return {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "model": AI_MODEL}
    raw = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
    return {
        "prompt_tokens": int(raw.get("prompt_tokens") or 0),
        "completion_tokens": int(raw.get("completion_tokens") or 0),
        "cost_usd": float(raw.get("cost") or 0),
        "model": getattr(response, "model", None) or AI_MODEL,
    }


_EMPTY_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "model": AI_MODEL}


SYSTEM_PROMPT = """You are an expert organizational psychologist designing employee surveys to assess sentiment and identify attrition risk.

Generate questions tailored to the employee's profile. Mix question types:
- "text": open-ended questions
- "scale": 1-5 rating (strongly disagree to strongly agree)
- "multiple_choice": predefined options (provide 3-5 options)

Cover: job satisfaction, growth, team dynamics, workload, management, culture.
Adapt based on previous answers - explore concerning areas deeper.

Return JSON: {"questions": [{"text": "...", "type": "text|scale|multiple_choice", "options": ["a","b","c"] or null}]}"""


async def generate_questions(
    employee: dict,
    prior_qa: list,
    focus_area: str | None,
    remaining: int,
    customs_in_round: list[dict] | None = None,
) -> tuple[list[dict], dict]:
    batch_size = min(QUESTIONS_PER_ROUND, remaining)

    parts = [
        f"Employee: {employee['name']}, Role: {employee.get('role', 'N/A')}, "
        f"Project: {employee.get('project', 'N/A')}, "
        f"Experience: {employee.get('experience_years', 'N/A')} years"
    ]

    if focus_area:
        parts.append(f"Focus area: {focus_area}")
    if prior_qa:
        parts.append(f"Previous Q&A:\n{json.dumps(prior_qa, indent=2)}")
    else:
        parts.append("This is the first round - start with broad questions.")
    if customs_in_round:
        parts.append(
            "These admin-supplied questions will also appear in this round - "
            "do NOT duplicate their topics:\n"
            + json.dumps([{"text": q["text"], "type": q["type"]} for q in customs_in_round], indent=2)
        )
    parts.append(f"Generate exactly {batch_size} questions.")

    try:
        response = await client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "\n\n".join(parts)},
            ],
            response_format={"type": "json_object"},
        )
        parsed = LLMResponse.model_validate_json(_strip_fences(response.choices[0].message.content))
        return [q.model_dump(mode="json") for q in parsed.questions[:batch_size]], _extract_usage(response)
    except Exception:
        logger.exception("LLM question generation failed; using fallback questions")
        return [
            {"text": "How would you rate your overall job satisfaction?", "type": "scale", "options": None},
            {"text": "What aspects of your work do you find most fulfilling?", "type": "text", "options": None},
            {"text": "How supported do you feel by your direct manager?", "type": "scale", "options": None},
        ][:batch_size], dict(_EMPTY_USAGE)


CLEANUP_PROMPT = """You are formatting survey questions for an employee sentiment survey.

For each plain-text question supplied, determine the most appropriate question type and format:
- "scale": agree/disagree statements, 1-5 Likert ratings
- "multiple_choice": questions with clear discrete options (provide 3-5 options)
- "text": open-ended questions

Preserve the admin's original intent. You may lightly reword for clarity and consistency, but don't change the meaning.

Return JSON: {"questions": [{"text": "...", "type": "text|scale|multiple_choice", "options": ["a","b","c"] or null}]}
Return questions in the same order as the input."""


async def cleanup_custom_questions(plain_texts: list[str]) -> tuple[list[dict], dict]:
    if not plain_texts:
        return [], dict(_EMPTY_USAGE)
    try:
        response = await client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": CLEANUP_PROMPT},
                {"role": "user", "content": "Questions:\n" + "\n".join(f"- {t}" for t in plain_texts)},
            ],
            response_format={"type": "json_object"},
        )
        parsed = LLMResponse.model_validate_json(_strip_fences(response.choices[0].message.content))
        return [q.model_dump(mode="json") for q in parsed.questions], _extract_usage(response)
    except Exception:
        logger.exception("Cleanup failed; treating all as plain text")
        return [{"text": t, "type": "text", "options": None} for t in plain_texts], dict(_EMPTY_USAGE)
