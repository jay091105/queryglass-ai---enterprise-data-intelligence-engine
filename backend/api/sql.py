"""SQL generation, validation, execution, and hallucination routes."""

from fastapi import APIRouter

from backend.main import (
    ExecuteRequest,
    GenerateRequest,
    HallucinationDetectRequest,
    HallucinationReduceRequest,
    ValidateRequest,
    detect_hallucinations as detect_hallucinations_handler,
    execute as execute_handler,
    generate as generate_handler,
    reduce_hallucinations as reduce_hallucinations_handler,
    validate as validate_handler,
)

router = APIRouter()


@router.post("/api/sql/generate")
def generate(request: GenerateRequest):
    return generate_handler(request)


@router.post("/api/sql/validate")
def validate(request: ValidateRequest):
    return validate_handler(request)


@router.post("/api/sql/execute")
def execute(request: ExecuteRequest):
    return execute_handler(request)


@router.post("/api/sql/hallucination/detect")
def detect_hallucinations(request: HallucinationDetectRequest):
    return detect_hallucinations_handler(request)


@router.post("/api/sql/hallucination/reduce")
def reduce_hallucinations(request: HallucinationReduceRequest):
    return reduce_hallucinations_handler(request)
