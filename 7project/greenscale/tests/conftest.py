import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def isolate_llm_settings(monkeypatch):
    """Keep tests independent of a developer's .env (Vertex AI, real API key)."""
    monkeypatch.setattr(settings, "use_vertex_ai", False)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
