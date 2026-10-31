from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.agent import JarvisAgent
from jarvis.config import Settings
from jarvis.store import TraceStore
from jarvis.tools import ToolExecutor


class FakeResponse(SimpleNamespace):
    def model_dump(self, mode: str = "json"):
        del mode
        return {
            "id": self.id,
            "output_text": self.output_text,
            "output": [vars(item) for item in self.output],
        }


class FakeResponses:
    def __init__(self, target: Path) -> None:
        self.target = target
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            call = SimpleNamespace(
                type="function_call",
                call_id="call-1",
                name="write_file",
                arguments='{"path":"'
                + str(self.target)
                + '","content":"approved","create_parents":false}',
            )
            return FakeResponse(id="response-1", output=[call], output_text="")
        assert kwargs["previous_response_id"] == "response-1"
        return FakeResponse(id="response-2", output=[], output_text="Done")


@pytest.mark.asyncio
async def test_write_pauses_then_resumes_after_approval(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        openai_api_key="test",
        jarvis_allowed_roots=[tmp_path],
        jarvis_db_path=tmp_path / "trace.db",
        jarvis_enable_web_search=False,
    )
    store = TraceStore(settings.jarvis_db_path)
    target = tmp_path / "result.txt"
    agent = JarvisAgent(settings, store, ToolExecutor(settings))
    agent.client = SimpleNamespace(responses=FakeResponses(target))
    session = store.create_session()

    pending = await agent.chat(session, "write the result")
    assert pending["status"] == "approval_required"
    assert not target.exists()

    finished = await agent.resolve_approval(pending["approval"]["id"], True)
    assert finished["message"] == "Done"
    assert target.read_text() == "approved"
