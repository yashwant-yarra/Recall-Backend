import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from recall import ask
from recall.ask import source_label


def test_doc_label():
    assert source_label({"kind": "doc", "page": 2}) == "page 2"


def test_voice_note_label():
    assert source_label({"kind": "audio", "page": None}) == "voice note"


def test_label_includes_document_name():
    assert source_label({"kind": "doc", "page": 3, "name": "Physics Ch 3.pdf"}) == "Physics Ch 3.pdf, page 3"


@pytest.mark.parametrize("mode", ["answer", "explain"])
def test_latest_question_stays_separate_from_tutor_self_check(monkeypatch, mode):
    chat_id, file_id = uuid4(), uuid4()
    history = [SimpleNamespace(role="user", content="what is this about?"),
               SimpleNamespace(role="assistant", content="Electrochemistry. Check: how do galvanic and electrolytic cells differ?")]
    execute = AsyncMock(side_effect=[
        SimpleNamespace(scalar_one=lambda: False),
        SimpleNamespace(first=lambda: SimpleNamespace(id=chat_id, file_id=file_id)),
        SimpleNamespace(all=lambda: history), None, None,
    ])

    class Engine:
        @asynccontextmanager
        async def begin(self):
            yield SimpleNamespace(execute=execute)

    captured = {}

    async def generate(**kwargs):
        captured.update(kwargs)
        async def chunks():
            yield SimpleNamespace(text="This material introduces electrochemistry. [1]")
        return chunks()

    genai = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content_stream=generate)))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=Engine(), genai=genai)))
    search = AsyncMock(return_value=[{"id": uuid4(), "file_id": file_id, "kind": "doc", "page": 1,
                                    "name": "Electrochemistry", "content": "Electrochemistry relates electrical and chemical energy."}])
    monkeypatch.setattr(ask, "hybrid_search", search)

    async def run():
        response = await ask.ask(ask.AskRequest(question="what is this about?", chat_id=chat_id, mode=mode), request, "me")
        return "".join([event async for event in response.body_iterator])

    events = asyncio.run(run())
    messages = captured["contents"]
    assert [message.role for message in messages] == ["user", "model", "user"]
    assert messages[1].parts[0].text == history[1].content
    assert messages[-1].parts[-1].text == "Latest user question:\nwhat is this about?"
    assert "Electrochemistry relates" in messages[-1].parts[0].text
    assert "galvanic and electrolytic" not in messages[-1].parts[0].text
    assert "Answer the latest user question" in captured["config"].system_instruction
    assert "optional self-checks" in captured["config"].system_instruction
    assert "event: done" in events
    assert execute.await_count == 5
    search.assert_awaited_once_with(request.app, "me", "what is this about?\nwhat is this about?", file_id)


def test_quick_answer_supports_overviews_and_keeps_source_refusal():
    assert "For an overview question" in ask.ANSWER_SYSTEM
    assert "I don't know based on your documents." in ask.ANSWER_SYSTEM
    assert "without adding a self-check question" in ask.ANSWER_SYSTEM


def test_both_modes_require_document_relevance_and_allow_related_followups():
    for prompt in (ask.ANSWER_SYSTEM, ask.EXPLAIN_SYSTEM):
        assert "Only answer questions related to the subjects" in prompt
        assert "requests for more detail beyond the document" in prompt
        assert "do not answer it" in prompt


def test_explain_distinguishes_requested_background_from_missing_document_facts():
    assert "When the student asks for related details beyond the document" in ask.EXPLAIN_SYSTEM
    assert "do not begin with 'I don't know based on your documents.'" in ask.EXPLAIN_SYSTEM
    assert "If the student asks what the document says about a specific fact" in ask.EXPLAIN_SYSTEM
