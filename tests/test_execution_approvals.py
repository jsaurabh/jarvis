from pathlib import Path

from jarvis.config import Settings
from jarvis.core import JarvisCore
from jarvis.sdk_agent import SDKJarvisAgent
from jarvis.tools import ToolExecutor


def test_execution_tools_require_approval_and_describe_capabilities(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        openai_api_key="test",
        jarvis_allowed_roots=[tmp_path],
        jarvis_db_path=tmp_path / "trace.db",
        jarvis_enable_web_search=False,
        jarvis_trace_mode="off",
    )
    agent = SDKJarvisAgent(settings, ToolExecutor(settings))
    tools = {tool.name: tool for tool in agent.agent.tools}
    assert tools["run_command"].needs_approval is True
    assert tools["run_native_command"].needs_approval is True
    vm = JarvisCore._approval_description("run_command", {"argv": ["python3", "-V"]})
    native = JarvisCore._approval_description(
        "run_native_command", {"argv": ["/usr/bin/clang"], "cwd": str(tmp_path)}
    )
    assert "offline" in vm and "disposable" in vm
    assert "modify" in native and str(tmp_path) in native
    agent.close()
