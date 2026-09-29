"""Generic liveness endpoint. Reports nothing about configuration, dependencies or versions."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api.dependencies import public

router = APIRouter()


@router.get("/healthz", include_in_schema=False, dependencies=[Depends(public)])
def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})
