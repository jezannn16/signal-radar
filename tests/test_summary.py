import json

import pytest

from signal_radar.cli import llm_opted_in
from signal_radar.summary import OpenAISummaryProvider, SummaryError, summarize_rows


def test_llm_summary_requires_explicit_opt_in():
    assert not llm_opted_in("")
    assert not llm_opted_in("false")
    assert llm_opted_in("true")
    assert llm_opted_in("1")


def test_openai_summary_adapter_is_bounded_strict_and_has_no_tools():
    calls=[]
    def transport(url,headers,body,timeout):
        payload=json.loads(body);calls.append((url,headers,payload,timeout))
        return json.dumps({"output_text":json.dumps({"summary":"Вышел релиз.","citation_urls":["https://example.test/release"]})}).encode()
    provider=OpenAISummaryProvider("synthetic-key","synthetic-model",transport=transport)
    result=provider.summarize([{"title":"Release","url":"https://example.test/release","content":"x"*5000}])
    assert result["summary"]=="Вышел релиз."
    url,headers,payload,_=calls[0]
    assert url.endswith("/v1/responses") and headers["Authorization"]=="Bearer synthetic-key"
    assert payload["store"] is False and "tools" not in payload
    assert len(json.loads(payload["input"][1]["content"][0]["text"])[0]["content"])==2000
    assert "untrusted quoted data" in payload["input"][0]["content"][0]["text"]


def test_summary_validation_rejects_uncited_or_untrusted_links():
    rows=[{"title":"Release","canonical_url":"https://example.test/release","content":"Facts"}]
    class FakeProvider:
        def __init__(self):self.result={"summary":"Релиз вышел.","citation_urls":[]}
        def summarize(self,articles):return self.result
    provider=FakeProvider()
    with pytest.raises(SummaryError,match="минимум на один источник"):
        summarize_rows(rows,provider)
    provider.result={"summary":"Релиз вышел.","citation_urls":["https://attacker.example/"]}
    with pytest.raises(SummaryError,match="вне набора"):
        summarize_rows(rows,provider)


def test_summary_provider_timeout_reports_baseline_fallback():
    def timeout(*_args):raise TimeoutError("synthetic timeout")
    provider=OpenAISummaryProvider("synthetic-key","synthetic-model",transport=timeout)
    with pytest.raises(SummaryError,match="исходный digest"):
        provider.summarize([{"title":"t","url":"https://example.test","content":"c"}])
