import inspect

from app.core.config import settings
from app.services.llm_service import FakeLLM, LLMAdapter, OllamaLLM, get_llm


async def test_fake_llm_returns_string():
    llm = FakeLLM()
    result = await llm.complete("Summarise: hello world")
    assert isinstance(result, str)
    assert len(result) > 0


def test_fake_llm_is_adapter():
    llm = FakeLLM()
    assert isinstance(llm, LLMAdapter)


def test_adapter_complete_is_async():
    # 1.R.1: a sync complete() (blocking httpx.post) inside the async /ask path
    # stalls the event loop for up to 60s — every adapter must be awaitable.
    assert inspect.iscoroutinefunction(FakeLLM.complete)
    assert inspect.iscoroutinefunction(OllamaLLM.complete)


def test_get_llm_returns_fake_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "llm_allow_external", False)
    monkeypatch.setattr(settings, "llm_backend", "fake")
    llm = get_llm()
    assert isinstance(llm, FakeLLM)


def test_get_llm_reads_settings_not_environ(monkeypatch):
    # 1.R.2: the factory must read app.core.config settings (like every other
    # service), not os.environ directly — env alone must not decide the backend.
    monkeypatch.setenv("LLM_BACKEND", "fake")
    monkeypatch.setenv("OLLAMA_MODEL", "env-model")
    monkeypatch.setattr(settings, "llm_backend", "ollama")
    monkeypatch.setattr(settings, "ollama_model", "settings-model")
    monkeypatch.setattr(settings, "ollama_base_url", "http://ollama-host:11434")
    llm = get_llm()
    assert isinstance(llm, OllamaLLM)
    assert llm._model == "settings-model"
    assert llm._base_url == "http://ollama-host:11434"


async def test_llm_complete_accepts_system_prompt():
    llm = FakeLLM()
    result = await llm.complete("What is Python?", system="You are a helpful assistant.")
    assert isinstance(result, str)
