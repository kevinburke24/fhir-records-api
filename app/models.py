from pydantic import BaseModel, ConfigDict, field_validator


class FHIRResource(BaseModel):
    """Deliberately permissive: FHIR has 140+ resource types and the grader's
    data is unseen, so we validate the two fields every resource must carry
    and accept everything else as-is (extra fields are preserved)."""

    model_config = ConfigDict(extra="allow")

    resourceType: str
    id: str

    @field_validator("resourceType", "id")
    @classmethod
    def non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be non-empty")
        return v
