"""Dashboard summary routes."""

from fastapi import APIRouter

from backend.main import dashboard as dashboard_handler

router = APIRouter()


@router.get("/api/dashboard")
def dashboard():
    return dashboard_handler()
