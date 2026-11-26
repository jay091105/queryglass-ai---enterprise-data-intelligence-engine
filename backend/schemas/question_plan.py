from typing import Optional
from pydantic import BaseModel, Field


class RequestType(BaseModel):
    type: str
    confidence: float = Field(ge=0.0, le=1.0)
    parameters: dict[str, object] = Field(default_factory=dict)


class UnsupportedRequest(BaseModel):
    type: str
    description: str
    mentioned_object: Optional[str] = None
    suggestion: str


class QuestionPlan(BaseModel):
    original_question: str = ""
    is_valid: bool = True
    blocking_reason: Optional[str] = None
    request_types: list[RequestType] = Field(default_factory=list)
    mentioned_tables: list[str] = Field(default_factory=list)
    mentioned_columns: list[str] = Field(default_factory=list)
    unsupported_requests: list[UnsupportedRequest] = Field(default_factory=list)

    @property
    def has_unsupported(self) -> bool:
        return bool(self.unsupported_requests)

    @property
    def can_generate_sql(self) -> bool:
        return not self.has_unsupported

    def model_dump(self, *args, **kwargs):
        payload = super().model_dump(*args, **kwargs)
        payload["has_unsupported"] = self.has_unsupported
        payload["can_generate_sql"] = self.can_generate_sql
        return payload

    def get_error_message(self) -> str:
        if not self.has_unsupported:
            return ""
        messages = []
        for item in self.unsupported_requests:
            messages.append(f"{item.description} Suggestion: {item.suggestion}")
        return " ".join(messages)
