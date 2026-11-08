"""Local language interpretation produces criteria, never rankings or arithmetic."""
from typing import Literal
from pydantic import BaseModel
from .model import make_agent
from .prompts import wrap_untrusted


class ComparisonIntent(BaseModel):
    criterion: Literal['cheapest', 'fastest', 'balance', 'risk', 'inventory', 'unclear']
    origin: str | None = None
    destination: str | None = None
    pricing_unit: str | None = None


def interpret(settings, question: str) -> ComparisonIntent:
    agent = make_agent(settings, ComparisonIntent,
        'Interpret the freight comparison request. Return criteria only, never calculate or invent filters. '
        'Use unclear when the request cannot be understood. Origin, destination and pricing unit must be explicitly requested.',
        'comparison_intent')
    return agent.run_sync(wrap_untrusted('question.txt', question, 'user_question')).output
