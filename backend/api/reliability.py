"""Reliability monitoring and provider-backed regression-test routes."""

from fastapi import APIRouter

from backend.main import (
    ReliabilityTestRunRequest,
    reliability_overview as reliability_overview_handler,
    reliability_test_catalog as reliability_test_catalog_handler,
    run_reliability_tests as run_reliability_tests_handler,
)

router = APIRouter()


@router.get("/api/reliability/summary")
def reliability_summary():
    return reliability_overview_handler()


@router.get("/api/reliability/tests")
def reliability_tests():
    return reliability_test_catalog_handler()


@router.post("/api/reliability/tests/run")
def run_reliability_tests(request: ReliabilityTestRunRequest):
    return run_reliability_tests_handler(request)
