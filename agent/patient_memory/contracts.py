from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Proposal(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    kind: Literal['VERIFY_INFO', 'RECHECK', 'CONTACT', 'OTHER']
    title: str = Field(min_length=1, max_length=200)
    quote: str = Field(min_length=2, max_length=3000)
    due_text: str = Field(default='', max_length=200)


class Extraction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[Proposal] = Field(max_length=20)
