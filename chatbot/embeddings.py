"""
Embedding model used for BOTH storing documents and searching.

nomic-embed-text was trained with task prefixes: stored text must start with
"search_document: " and questions with "search_query: ". Without them the
vectors for questions and documents land in slightly different places and
search quality drops. LangChain's OllamaEmbeddings does not add them, so this
small wrapper does.

Important: ingestion and retrieval must use this same class, otherwise the
stored vectors and the question vectors are not comparable.
"""

from langchain_core.embeddings import Embeddings
from langchain_ollama import OllamaEmbeddings

from chatbot.config import EMBED_MODEL

DOC_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "


class NomicEmbeddings(Embeddings):
    """OllamaEmbeddings + the task prefixes nomic-embed-text expects."""

    def __init__(self, model: str = EMBED_MODEL):
        self._ollama = OllamaEmbeddings(model=model)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        # Called when chunks are added to the vector store.
        return self._ollama.embed_documents([DOC_PREFIX + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        # Called when a user's question is turned into a vector for search.
        return self._ollama.embed_query(QUERY_PREFIX + text)
