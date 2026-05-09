import markdown as md
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from app.bot_logic import survey_status_label
from app.config import settings
from app.models import sentiment_band
from app.reports import report_status_label


templates = Jinja2Templates(directory="app/templates")


def nl2br(value: str) -> Markup:
    return Markup("<br>".join(escape(value or "").splitlines()))


def render_markdown(value: str) -> Markup:
    html = md.markdown(value or "", extensions=["extra", "sane_lists", "nl2br"])
    return Markup(html)


templates.env.filters["nl2br"] = nl2br
templates.env.filters["markdown"] = render_markdown
templates.env.filters["report_status_label"] = report_status_label
templates.env.filters["survey_status_label"] = survey_status_label
templates.env.filters["sentiment_band"] = sentiment_band
templates.env.globals["settings"] = settings
