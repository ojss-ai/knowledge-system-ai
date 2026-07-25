from app.services.llm_service import FakeLLM, LLMAdapter, get_llm


def test_fake_llm_returns_string():
    llm = FakeLLM()
    result = llm.complete("Summarise: hello world")
    assert isinstance(result, str)
    assert len(result) > 0


def test_fake_llm_is_adapter():
    llm = FakeLLM()
    assert isinstance(llm, LLMAdapter)


def test_get_llm_returns_fake_when_disabled(monkeypatch):
    monkeypatch.setenv("LLM_ALLOW_EXTERNAL", "false")
    monkeypatch.setenv("LLM_BACKEND", "fake")
    llm = get_llm()
    assert isinstance(llm, FakeLLM)


def test_llm_complete_accepts_system_prompt():
    llm = FakeLLM()
    result = llm.complete("What is Python?", system="You are a helpful assistant.")
    assert isinstance(result, str)
