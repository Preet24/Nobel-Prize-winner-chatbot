"""
The chat model. Everything else asks for it through get_llm(), so switching
to another model (or later to Claude via langchain-anthropic) is a change in
this one file.
"""

from langchain_ollama import ChatOllama

from chatbot.config import CHAT_MODEL, CHAT_NUM_CTX, CHAT_TEMPERATURE


def get_llm(model: str = CHAT_MODEL, thinking: bool = False,
            temperature: float = CHAT_TEMPERATURE) -> ChatOllama:
    """
    thinking: qwen3.5 can "think out loud" before answering. That improves
    hard questions but is several times slower on a small GPU, so it is off
    by default. When on, the thoughts arrive separately from the answer
    (in additional_kwargs["reasoning_content"]), so they never mix into it.
    temperature: 0 for data extraction (same text -> same answer).
    """
    return ChatOllama(
        model=model,
        temperature=temperature,
        num_ctx=CHAT_NUM_CTX,  # Ollama's default window is too small for our context
        reasoning=thinking,
    )
