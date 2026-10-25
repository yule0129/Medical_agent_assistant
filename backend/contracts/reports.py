from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MatchingCriteria(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    test_name: str = Field(min_length=1, max_length=120)
    specimen_id: str | None = Field(default=None, min_length=1, max_length=100)
    order_id: str | None = Field(default=None, min_length=1, max_length=100)
    report_id: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def identifier_required(self):
        if not (self.specimen_id or self.order_id or self.report_id):
            raise ValueError("自动匹配至少需要标本号、医嘱号或报告号之一")
        return self


class LabPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    report_id: str = Field(min_length=1, max_length=100)
    report_version: int = Field(ge=1)
    test_name: str = Field(min_length=1, max_length=120)
    specimen_id: str | None = Field(default=None, min_length=1, max_length=100)
    order_id: str | None = Field(default=None, min_length=1, max_length=100)
    result_text: str = Field(min_length=1, max_length=100000)


class LabImport(LabPayload):
    doctor_id: int = Field(gt=0)
    patient_id: int = Field(gt=0)
    encounter_id: int = Field(gt=0)
    event_key: str = Field(min_length=1, max_length=100)
    reported_at: datetime

    @field_validator("reported_at")
    @classmethod
    def timezone_required(cls, value):
        if value.utcoffset() is None:
            raise ValueError("报告时间必须带时区")
        return value
