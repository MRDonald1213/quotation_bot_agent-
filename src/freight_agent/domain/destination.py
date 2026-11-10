from pydantic import BaseModel, ConfigDict, Field

class ExtractedField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    value: str | None = None
    snippet: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    explanation: str | None = Field(default=None, max_length=500)
    not_applicable: bool = False


class SchemaExtraction(BaseModel):
    fields: list[ExtractedField]
    issues: list[str] = Field(default_factory=list)

