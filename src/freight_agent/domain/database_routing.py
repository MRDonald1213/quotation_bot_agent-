from pydantic import BaseModel, ConfigDict, Field

class DatabaseCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database_id: str
    confidence: float = Field(ge=0, le=1)
    reason: str

class DatabaseRouting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[DatabaseCandidate]
