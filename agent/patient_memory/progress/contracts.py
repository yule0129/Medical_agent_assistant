from typing import Literal
from pydantic import BaseModel,ConfigDict,Field
from agent.patient_memory.actions.contracts import ActionCandidate


class Reference(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    source_id:str=Field(min_length=1,max_length=150)
    quote:str=Field(min_length=2,max_length=3000)


class Observation(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    state:Literal['WAITING','RESPONSE_RECORDED','RESULT_AVAILABLE','CONFLICT','PLAN_CHANGED','UNCLEAR']
    summary:str=Field(min_length=1,max_length=2000)
    references:list[Reference]=Field(max_length=10)
    actions:list[ActionCandidate]=Field(default_factory=list,max_length=3)
