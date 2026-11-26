"""Runtime settings and table export routes."""

from fastapi import APIRouter

from backend.main import (
    export_table as export_table_handler,
    settings as settings_handler,
)

router = APIRouter()


@router.get("/api/settings")
def settings():
    return settings_handler()


@router.get("/api/export/{table_name}.csv")
def export_table(table_name: str):
    return export_table_handler(table_name)
