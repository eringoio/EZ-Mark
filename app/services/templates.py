from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.services.i18n_service import i18n
from app.services.scoring_service import format_score

BASE_DIR = Path(__file__).resolve().parent.parent


class LocalizedJinja2Templates(Jinja2Templates):
    def TemplateResponse(self, request: Request, name: str, context: dict, *args, **kwargs):
        # Retrieve active locale from i18n service
        locale = i18n.get_locale()

        # Inject standard localization context helpers
        context["request"] = request
        context["_"] = lambda key, **kw: i18n.translate(key, locale, **kw)
        context["current_locale"] = locale
        context["supported_locales"] = i18n.supported_locales
        # Demo-mode indicator for the base template (see docs/demo-mode.md).
        context["demo_mode"] = get_settings().demo_mode

        return super().TemplateResponse(request, name, context, *args, **kwargs)


# Export standard unified templates instance
templates = LocalizedJinja2Templates(directory=str(BASE_DIR / "templates"))

# Presentation-only point formatting: 7 -> "7", 7.5 -> "7.5". Stored values are untouched.
templates.env.filters["score_text"] = format_score

# Embed JSON (the |tojson filter) with literal UTF-8 so localized labels and furigana
# stay readable in inline <script> payloads instead of being escaped to \uXXXX. Jinja's
# tojson still escapes HTML-special characters (< > & ') for safe inlining. A fresh dict
# is assigned (not mutated) to avoid touching Jinja's shared default policies.
templates.env.policies["json.dumps_kwargs"] = {
    **templates.env.policies["json.dumps_kwargs"],
    "ensure_ascii": False,
}
