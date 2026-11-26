from typing import Dict, Optional
from pydantic import BaseModel, Field


class ColumnMetadata(BaseModel):
    name: str
    type: str = ""
    position: int = Field(ge=1)
    nullable: bool = True
    primary_key: bool = False
    unique: bool = False


class ContentSignature(BaseModel):
    sample_hash: str
    row_count: int = Field(ge=0)
    sample_size: int = Field(ge=0)
    value_distribution: Dict[str, int] = Field(default_factory=dict)


class TableFingerprint(BaseModel):
    table_name: str
    dataset_id: Optional[str] = None
    fingerprint_hash: str
    structural_hash: str
    content_hash: str
    columns: list[ColumnMetadata] = Field(default_factory=list)
    content_signature: Optional[ContentSignature] = None
    computed_at: Optional[str] = None

    def matches(self, other: "TableFingerprint") -> dict[str, bool]:
        structure_match = self.structural_hash == other.structural_hash
        content_match = self.content_hash == other.content_hash
        return {
            "structure": structure_match,
            "content": content_match,
            "structure_match": structure_match,
            "content_match": content_match,
            "overall_match": structure_match and content_match,
        }
