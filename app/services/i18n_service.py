import contextvars
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Thread-safe request context for current locale
_current_locale: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_locale", default="en"
)


class I18nService:
    def __init__(self, locales_dir: Path = Path(__file__).parent.parent / "locales"):
        self.locales_dir = locales_dir
        self.translations: dict[str, dict[str, Any]] = {}
        self.supported_locales = ["en", "ja"]
        self.default_locale = "en"
        self.load_locales()

    def load_locales(self):
        self.locales_dir.mkdir(parents=True, exist_ok=True)
        for locale in self.supported_locales:
            file_path = self.locales_dir / f"{locale}.json"
            if file_path.exists():
                try:
                    with open(file_path, encoding="utf-8") as f:
                        self.translations[locale] = json.load(f)
                except Exception as e:
                    logger.error(f"Error loading {locale}.json: {e}")
                    self.translations[locale] = {}
            else:
                logger.warning(f"Translation file not found: {file_path}")
                self.translations[locale] = {}

    def get_locale(self) -> str:
        try:
            return _current_locale.get()
        except LookupError:
            return self.default_locale

    def set_locale(self, locale: str):
        if locale in self.supported_locales:
            _current_locale.set(locale)

    def translate(self, key: str, locale: str = None, **kwargs) -> str:
        if locale is None:
            locale = self.get_locale()

        keys = key.split(".")
        data = self.translations.get(locale, {})

        # Traverse nested keys
        for k in keys:
            if isinstance(data, dict) and k in data:
                data = data[k]
            else:
                # Fallback to English if translation is missing in selected language
                if locale != self.default_locale:
                    return self.translate(key, self.default_locale, **kwargs)
                return key

        if not isinstance(data, str):
            return key

        if kwargs:
            try:
                return data.format(**kwargs)
            except Exception:
                return data
        return data


# Global instances
i18n = I18nService()


# Global translation helper shorthand for python code
def _(key: str, **kwargs) -> str:
    return i18n.translate(key, **kwargs)


class I18nMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # 1. Check query param
        lang = request.query_params.get("lang")

        # 2. Check cookies
        if not lang or lang not in i18n.supported_locales:
            lang = request.cookies.get("lang")

        # 3. Check Accept-Language header
        if not lang or lang not in i18n.supported_locales:
            accept_lang = request.headers.get("accept-language", "")
            for lang_tag in accept_lang.split(","):
                clean = lang_tag.split(";")[0].strip().split("-")[0].lower()
                if clean in i18n.supported_locales:
                    lang = clean
                    break

        # Fallback
        if not lang or lang not in i18n.supported_locales:
            lang = i18n.default_locale

        # Bind to context
        token = _current_locale.set(lang)

        try:
            response = await call_next(request)
        finally:
            _current_locale.reset(token)

        # Update cookie if it was explicitly requested via query parameter
        if "lang" in request.query_params:
            # We must get the response first and then set the cookie
            response.set_cookie("lang", lang, max_age=30 * 24 * 60 * 60, path="/")

        return response
