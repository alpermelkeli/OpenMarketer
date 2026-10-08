"""Health: whether the process answers. It does not check the database or the models."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]


@router.get("/health", operation_id="getHealth", response_model=HealthResponse)
def get_health() -> HealthResponse:
    return HealthResponse(status="ok")
