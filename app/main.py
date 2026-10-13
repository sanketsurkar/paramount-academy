import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .config import BASE_DIR, IS_PRODUCTION, SECRET_KEY, SESSION_HOURS, VERSION
from .db import Base, SessionLocal, engine
from .deps import Redirect, render
from .routes import admin, auth, staff, student
from .seed import seed
from .storage import files

log = logging.getLogger("uvicorn.error")
STATIC = BASE_DIR / "app" / "static"


def upgrade_schema() -> None:
    """Add columns introduced in v2 to databases created by v1 (safe to run every start)."""
    insp = inspect(engine)
    if "subjects" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("subjects")}
    extra = {"colour": "VARCHAR(20) DEFAULT 'indigo'", "icon": "VARCHAR(8) DEFAULT '📘'",
             "archived": "BOOLEAN DEFAULT FALSE"}
    with engine.begin() as conn:
        for name, ddl in extra.items():
            if name not in cols:
                conn.execute(text(f"ALTER TABLE subjects ADD COLUMN {name} {ddl}"))
                log.info("Upgraded database: subjects.%s added", name)


@asynccontextmanager
async def lifespan(_: FastAPI):
    upgrade_schema()
    Base.metadata.create_all(engine)
    files.setup()
    with SessionLocal() as db:
        seed(db)
    log.info("Ready - database: %s, files: %s, mode: %s", engine.dialect.name, files.name,
             "production" if IS_PRODUCTION else "local")
    yield


app = FastAPI(title="Paramount Academy Lite", version=VERSION, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="tpa_session",
                   max_age=SESSION_HOURS * 3600, same_site="lax", https_only=IS_PRODUCTION)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("Content-Security-Policy",
                 "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                 "script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    if IS_PRODUCTION:
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    if h.get("content-type", "").startswith("text/html"):
        h.setdefault("Cache-Control", "private, no-cache")
    return response


@app.exception_handler(Redirect)
async def redirect_handler(request: Request, exc: Redirect):
    if request.headers.get("x-partial") == "1" or "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"redirect": exc.url}, status_code=401)
    return RedirectResponse(exc.url, 303)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    messages = {404: "We couldn't find that page.", 405: "That action isn't allowed here."}
    message = exc.detail if isinstance(exc.detail, str) and exc.detail not in ("Not Found", "Method Not Allowed") \
        else messages.get(exc.status_code, "Please try again.")
    with SessionLocal() as db:
        return render(request, "error.html", db, status_code=exc.status_code, code=exc.status_code, message=message)


@app.exception_handler(RequestValidationError)
async def bad_input(request: Request, exc: RequestValidationError):
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"error": "Invalid request."}, status_code=400)
    with SessionLocal() as db:
        return render(request, "error.html", db, status_code=400, code=400,
                      message="Some of the information sent wasn't valid. Go back and check the form.")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok", "version": VERSION}

@app.get("/.well-known/assetlinks.json", include_in_schema=False)
def assetlinks():
    """Links the Android app to this website (file comes from PWABuilder)."""
    path = STATIC / "assetlinks.json"
    if not path.exists():
        raise HTTPException(404, "Not set up yet.")
    return FileResponse(path, media_type="application/json")


for module in (auth, student, staff, admin):
    app.include_router(module.router)
