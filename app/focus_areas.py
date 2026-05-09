from __future__ import annotations


FOCUS_AREAS: dict[str, dict[str, object]] = {
    "financial": {
        "label": "Financial",
        "description": "Compensation, benefits, equity, financial wellbeing.",
        "seed_questions": [
            "How fairly do you feel you're compensated for the work you do?",
            "Are there benefits you wish we offered, or current ones you don't find useful?",
            "How is your overall financial wellbeing affecting how you show up at work?",
        ],
    },
    "technical": {
        "label": "Technical",
        "description": "Tooling, codebase, technical debt, engineering process.",
        "seed_questions": [
            "Which tool or part of our stack slows you down the most day to day?",
            "Where in the codebase or process do you feel technical debt is hurting us?",
            "What change to how we ship would have the biggest impact on your productivity?",
        ],
    },
    "growth": {
        "label": "Career growth",
        "description": "Learning, promotions, mentorship, career path.",
        "seed_questions": [
            "How clear is your next step in your career here?",
            "What skills do you wish you had more time or support to develop?",
            "Who, if anyone, is currently helping you grow — and where could you use more support?",
        ],
    },
    "wellbeing": {
        "label": "Wellbeing",
        "description": "Workload, burnout, work-life balance, stress.",
        "seed_questions": [
            "How sustainable is your current workload over the next few months?",
            "When did you last feel genuinely rested and ready for the week?",
            "Is anything outside of work right now making it harder to be at your best here?",
        ],
    },
    "manager": {
        "label": "Manager / leadership",
        "description": "Relationship with manager, leadership clarity, support.",
        "seed_questions": [
            "How well do you feel your manager understands what you're working on?",
            "When you raise something with your manager, do you feel heard and supported?",
            "How clear is the direction coming from leadership right now?",
        ],
    },
    "team": {
        "label": "Team dynamics",
        "description": "Collaboration, communication, team culture, psychological safety.",
        "seed_questions": [
            "How safe do you feel disagreeing or pushing back within your team?",
            "Where does communication or handoff break down most often on your team?",
            "What would make collaborating with your team feel better next month?",
        ],
    },
}


def normalize_focus_slugs(slugs: list[str] | None) -> list[str]:
    if not slugs:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for slug in slugs:
        if slug in FOCUS_AREAS and slug not in seen:
            seen.add(slug)
            ordered.append(slug)
    return ordered


def focus_label(slug: str) -> str:
    entry = FOCUS_AREAS.get(slug)
    return str(entry["label"]) if entry else slug


def focus_labels(slugs: list[str]) -> list[str]:
    return [focus_label(s) for s in slugs]
