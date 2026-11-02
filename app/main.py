from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.v1 import router as api_router
from app.core.config import get_settings
from app.core.logging import setup_logging

setup_logging()
settings = get_settings()

app = FastAPI(
    title="FBR Greenfield Agentic HR DSS",
    version="1.0.0",
    description="Enterprise HR Decision Support — live HRMS + OCR Personal Files + policy RAG + rule engines",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Request-Id"] = request.headers.get("x-request-id") or response.headers.get(
            "x-request-id", ""
        )
        if not response.headers.get("X-Request-Id"):
            from uuid import uuid4

            response.headers["X-Request-Id"] = uuid4().hex
        return response


app.add_middleware(SecurityHeadersMiddleware)
app.include_router(api_router)


@app.get("/")
def root():
    return {
        "service": "FBR Greenfield Agentic HR DSS",
        "version": "1.0.0",
        "environment": settings.environment,
        "docs": "/docs",
        "health": "/api/v1/health",
        "ready": "/api/v1/ready",
        "ui": "/ui/",
    }


frontend_dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/ui", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")
