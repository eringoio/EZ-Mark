from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.database import init_database
from app.routes import (
    answer_keys,
    dashboard,
    exams,
    exports,
    peer_review,
    results,
    review,
    scans,
    settings,
    sheets,
    students,
    test_templates,
)
from app.services.i18n_service import I18nMiddleware
from app.services.templates import templates

BASE_DIR = Path(__file__).resolve().parent


def create_app() -> FastAPI:
    app = FastAPI(title="EZ Mark")
    app.add_middleware(I18nMiddleware)

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    # Language selector route
    @app.get("/lang/{locale}")
    def set_locale(locale: str, request: Request):
        if locale not in ["en", "ja"]:
            locale = "en"
        referer = request.headers.get("referer", "/")
        response = RedirectResponse(referer, status_code=303)
        response.set_cookie(key="lang", value=locale, max_age=30 * 24 * 60 * 60, path="/")
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "app": "ezmark"}

    app.include_router(dashboard.router)
    app.include_router(exams.router)
    app.include_router(test_templates.router)
    app.include_router(students.router)
    app.include_router(answer_keys.router)
    app.include_router(sheets.router)
    app.include_router(scans.router)
    app.include_router(peer_review.router)
    app.include_router(settings.router)
    app.include_router(review.router)
    app.include_router(results.router)
    app.include_router(exports.router)

    @app.on_event("startup")
    def on_startup() -> None:
        init_database()
        from app.services.demo_service import reset_demo_on_start

        reset_demo_on_start()

    @app.exception_handler(404)
    async def not_found(request: Request, _exc: Exception) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "errors/404.html",
            {"request": request},
            status_code=404,
        )

    @app.exception_handler(500)
    async def server_error(request: Request, _exc: Exception) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "errors/500.html",
            {"request": request},
            status_code=500,
        )

    return app


app = create_app()
