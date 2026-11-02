from typing import Literal
from pydantic import BaseModel,ConfigDict,Field


class ActionCandidate(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    action:Literal['CREATE_FOLLOWUP','RESCHEDULE','REVISE_PLAN','COMPLETE_MATTER','CANCEL_PLAN']
    purpose:Literal['REVIEW_RESULT','VERIFY_INFORMATION','RECORD_RESPONSE']|None=None
    title:str=Field(min_length=1,max_length=200)
    rationale:str=Field(min_length=1,max_length=2000)
    source_ids:list[str]=Field(default_factory=list,max_length=10)
    due_text:str=Field(default='',max_length=200)
