from __future__ import annotations

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import APP_ROOT, MODELS_DIR, ensure_system_dirs
from app.core.cvat import check_cvat_connection
from app.modules.training.router import router as training_router
from app.modules.prediction.router import router as prediction_router
from app.modules.jobs.router import router as jobs_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_system_dirs()
    yield


app = FastAPI(
    title="CVAT AI Toolkit Web",
    description="Interface Web para Treinamento e Auto-Anotação com YOLO e CVAT",
    version="0.1.0",
    lifespan=lifespan,
)

# Montagem de arquivos estáticos (CSS, JS)
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "app" / "static")), name="static")

# Montagem de arquivos de modelos e gráficos de treinamento
app.mount("/model_files", StaticFiles(directory=str(MODELS_DIR)), name="model_files")

# Inclusão dos módulos/abas
app.include_router(training_router)
app.include_router(prediction_router)
app.include_router(jobs_router)


@app.get("/")
async def root():
    return RedirectResponse(url="/training")


@app.get("/api/health")
async def health_check():
    cvat_status = check_cvat_connection()
    return {
        "status": "healthy",
        "cvat": cvat_status,
    }
