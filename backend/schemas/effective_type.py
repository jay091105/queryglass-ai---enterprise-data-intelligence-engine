from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class EffectiveType(str, Enum):
    NUMERIC = "numeric"
    INTEGER = "integer"
    FLOAT = "float"
    DATE = "date"
    DATETIME = "datetime"
    TIME = "time"
    BOOLEAN = "boolean"
    ENUM = "enum"
    STRING = "string"
    MIXED = "mixed"
    NULL = "null"


class ColumnEffectiveType(BaseModel):
    name: str
    schema_type: str = ""
    effective_type: EffectiveType
    confidence: float = Field(ge=0.0, le=1.0)
    sample_size: int = Field(ge=0)
    pattern_examples: list[str] = Field(default_factory=list)
    numeric_range: Optional[dict[str, float]] = None
    top_values: dict[str, int] = Field(default_factory=dict)
    cardinality: int = Field(ge=0, default=0)

    def is_compatible_with_operation(self, operation: str) -> bool:
        op = operation.lower()
        if op in {"sum", "avg", "average", "min", "max", "numeric"}:
            return self.effective_type in {EffectiveType.NUMERIC, EffectiveType.INTEGER, EffectiveType.FLOAT}
        if op in {"date", "datetime", "time", "day", "month", "year"}:
            return self.effective_type in {EffectiveType.DATE, EffectiveType.DATETIME, EffectiveType.TIME}
        if op in {"count", "distinct", "group", "filter", "string"}:
            return self.effective_type != EffectiveType.NULL
        return True


class TableEffectiveTypes(BaseModel):
    table_name: str
    dataset_id: Optional[str] = None
    columns: list[ColumnEffectiveType] = Field(default_factory=list)
    computed_at: Optional[str] = None

    def get_column_type(self, name: str) -> Optional[ColumnEffectiveType]:
        wanted = name.casefold()
        return next((column for column in self.columns if column.name.casefold() == wanted), None)
