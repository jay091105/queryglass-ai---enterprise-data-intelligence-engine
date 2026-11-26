"""Health and service discovery routes."""

from fastapi import APIRouter

from backend.main import health as health_handler
from backend.main import root as root_handler

router = APIRouter()


@router.get("/")
def root():
    return root_handler()


@router.get("/api/health")
def health():
    return health_handler()
