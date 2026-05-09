import json

import markdown as md
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from app.bot_logic import survey_status_label
from app.config import settings
from app.focus_areas import focus_labels
from app.models import sentiment_band
from app.reports import report_status_label


templates = Jinja2Templates(directory="app/templates")


def nl2br(value: str) -> Markup:
    return Markup("<br>".join(escape(value or "").splitlines()))


def render_markdown(value: str) -> Markup:
    html = md.markdown(value or "", extensions=["extra", "sane_lists", "nl2br"])
    return Markup(html)


def focus_area_labels(value: str | list[str] | None) -> list[str]:
    if isinstance(value, list):
        return focus_labels(value)
    if not value:
        return []
    try:
        return focus_labels(json.loads(value))
    except (ValueError, TypeError):
        return []


templates.env.filters["nl2br"] = nl2br
templates.env.filters["markdown"] = render_markdown
templates.env.filters["report_status_label"] = report_status_label
templates.env.filters["survey_status_label"] = survey_status_label
templates.env.filters["sentiment_band"] = sentiment_band
templates.env.filters["focus_area_labels"] = focus_area_labels
templates.env.globals["settings"] = settings
