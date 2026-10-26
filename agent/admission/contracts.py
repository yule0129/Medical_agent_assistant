from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

HEADINGS = ("主诉", "现病史", "既往史", "个人史", "婚育史", "家族史", "体格检查", "辅助检查", "初步诊断")
TOOLS = ("read_patient_profile", "read_interview", "read_examinations")


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")
    heading: str
    text: str = Field(min_length=1, max_length=6000)
    source_ids: list[str] = Field(max_length=30)
    missing: bool


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["call_tool", "submit_draft"]
    tool_name: Literal["read_patient_profile", "read_interview", "read_examinations"] | None = None
    sections: list[Section] = Field(default_factory=list, max_length=9)


class DraftError(RuntimeError):
    def __init__(self, code, trace=None):
        super().__init__(code)
        self.trace = trace or []
