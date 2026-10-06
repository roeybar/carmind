from typing import Any, Literal

from pydantic import BaseModel, Field

# The context_flag enum in schema/knowledge.schema.json. Flags are per request and never stored.
ContextFlag = Literal[
    "repeat_problem",
    "recent_long_service",
    "near_service_interval",
    "fresh_handover",
    "no_pre_delivery_test",
    "after_service_hours",
]


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    language: str = "he-IL"
    messages: list[Message] = []
    context_flags: list[ContextFlag] = []
    caller_number: Any = None
    plate: Any = None


class AgentRequest(BaseModel):
    customer_id: Any = None
    caller_number: Any = None
    plate: Any = None
    problem: str = Field(min_length=1, max_length=4000)
    after_hours: bool = False
