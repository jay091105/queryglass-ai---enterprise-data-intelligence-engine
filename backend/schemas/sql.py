"""Request schemas for dataset and SQL API operations."""

from typing import Any

from pydantic import BaseModel, Field


class StoredDatasetPreprocessRequest(BaseModel):
    options: dict[str, Any] = Field(default_factory=dict)


class WorkflowPreprocessRequest(BaseModel):
    options: dict[str, Any] = Field(default_factory=dict)


class GenerateRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    table_name: str | None = Field(default=None, min_length=1, max_length=100)


class ValidateRequest(BaseModel):
    question: str = ""
    sql: str = Field(min_length=1, max_length=20000)
    table_name: str = Field(min_length=1, max_length=100)
    reliability_id: str | None = None


class ExecuteRequest(ValidateRequest):
    pass


class ReliabilityTestRunRequest(BaseModel):
    table_name: str = Field(min_length=1, max_length=100)
    test_id: str | None = Field(default=None, min_length=1, max_length=100)
