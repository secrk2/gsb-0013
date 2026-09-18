"""报关通 · 后端入口（FastAPI，监听 7106）。"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import Base, engine
from .models import BizError
from .routers import auth, clients, declarations, dashboard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("baoguantong")

app = FastAPI(title=settings.APP_NAME, version="1.0.0")

# Nginx 反代 /api → 7106；本地直连前端开发服务器时也放行
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(BizError)
async def biz_error_handler(request: Request, exc: BizError):
    """所有业务拦截统一结构：{error: {code, reason}}，前端直接把 reason 展示给用户。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "reason": exc.reason}},
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    log.exception("unhandled error: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "server_error", "reason": f"服务异常：{exc}"}},
    )


app.include_router(auth.router, prefix="/api")
app.include_router(clients.router, prefix="/api")
app.include_router(declarations.router, prefix="/api")
app.include_router(dashboard.router, prefix="/api")


@app.get("/api/health")
def health():
    return {"ok": True, "service": "baoguantong", "port": 7106}


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    if settings.SEED_ON_STARTUP:
        from .seed import seed_if_empty
        seed_if_empty()
    log.info("报关通后端已启动 :7106")
