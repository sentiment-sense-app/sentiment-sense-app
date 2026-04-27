from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from app.config import settings
from app.reports import report_status_label


templates = Jinja2Templates(directory="app/templates")


def nl2br(value: str) -> Markup:
    return Markup("<br>".join(escape(value or "").splitlines()))


templates.env.filters["nl2br"] = nl2br
templates.env.filters["report_status_label"] = report_status_label
templates.env.globals["settings"] = settings
