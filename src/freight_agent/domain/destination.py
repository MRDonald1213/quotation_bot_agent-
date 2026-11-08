from pydantic import BaseModel, ConfigDict

class ExtractedField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    value: str | None = None
    snippet: str | None = None


class SchemaExtraction(BaseModel):
    fields: list[ExtractedField]
    issues: list[str] = []

