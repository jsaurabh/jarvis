"""Opt-in real CPU LoRA training, SDK inference, evaluation and selection."""

import asyncio
import json
import os
import subprocess
import sys

import pytest

from jarvis.flywheel import build_improvement_bundle
from jarvis.store import TraceStore


def test_bundle_filters_all_files_and_metadata_has_no_content(tmp_path):
    store = TraceStore(tmp_path / "data.db", "metadata")
    session = store.create_session()
    run = store.create_run(session, "Contact private@example.com", "test")
    store.event(run, "sdk_trace", {"items": [{"role": "user", "content": "private text"}]})
    store.event(run, "realtime_metadata", {"items": ["private voice"]})
    store.set_run_context(run, {"instructions": "Contact private@example.com"})
    store.finish_run(run, "wrong")
    store.feedback(run, -1, "private@example.com")
    assert all("private" not in json.dumps(event["payload"]) for event in store.events_for_run(run))
    build_improvement_bundle(store, tmp_path / "bundle")
    for file in (tmp_path / "bundle").iterdir():
        assert "private@example.com" not in file.read_text()


def test_failed_and_interrupted_voice_not_training_eligible(tmp_path):
    store = TraceStore(tmp_path / "data.db")
    session = store.create_session()
    for status in ["failed", "cancelled"]:
        run = store.record_completed_turn(session, "hello", "partial", "voice", {"status": status})
        store.feedback(run, 1, None)
        assert store.all_runs()[-1]["status"] == status
    assert store.exportable_runs() == []


@pytest.mark.skipif(os.getenv("JARVIS_TRAINING_TEST") != "1", reason="Opt-in real CPU training")
def test_real_training_evaluation_promotion_and_supervisor(tmp_path, monkeypatch):
    import torch
    from tokenizers import Tokenizer, models, pre_tokenizers
    from transformers import GPT2Config, GPT2LMHeadModel, PreTrainedTokenizerFast

    from jarvis.config import Settings
    from jarvis.export import export_dataset
    from jarvis.model_lifecycle import active_backend, evaluate_candidate, promote
    from jarvis.supervisor import Supervisor
    from jarvis.train import train_lora

    torch.set_num_threads(2)
    torch.manual_seed(7)
    monkeypatch.setenv("JARVIS_TRAIN_CPU", "1")
    base, artifact = tmp_path / "base", tmp_path / "candidate"
    instruction = "You are Jarvis."
    read = '<tool_call> {"name":"read_file","arguments":{"max_chars":1000,"offset":0,"path":"note.txt"}} </tool_call>'
    write = '<tool_call> {"name":"write_file","arguments":{"path":"note.txt","content":"changed"}} </tool_call>'
    conversations = [
        [
            {"role": "system", "content": instruction},
            {"role": "user", "content": "Say ready"},
            {"role": "assistant", "content": "ready"},
        ],
        [
            {"role": "system", "content": instruction},
            {"role": "user", "content": "Read note"},
            {"role": "assistant", "content": read},
        ],
        [
            {"role": "system", "content": instruction},
            {"role": "user", "content": "Read note"},
            {"role": "assistant", "content": read},
            {
                "role": "tool",
                "content": '{"ok": true, "result": {"content": "synthetic evaluation note\\n"}}',
            },
            {"role": "assistant", "content": "ready"},
        ],
        [
            {"role": "system", "content": instruction},
            {"role": "user", "content": "Write note"},
            {"role": "assistant", "content": write},
        ],
    ]
    from jarvis.instructions import AGENT_INSTRUCTIONS

    conversations.append(
        [
            {"role": "system", "content": AGENT_INSTRUCTIONS},
            {"role": "user", "content": "Say ready"},
            {"role": "assistant", "content": "ready"},
        ]
    )
    words = set(
        [
            "system",
            "user",
            "assistant",
            "tool",
            "result",
            "You",
            "are",
            "Jarvis.",
            "Say",
            "ready",
            "Read",
            "note",
            "Write",
            "<tool_call>",
            "</tool_call>",
        ]
    )
    for messages in conversations:
        for message in messages:
            words.update(message["content"].split())
    vocabulary = {
        word: index for index, word in enumerate(["[UNK]", "[PAD]", "[EOS]"] + sorted(words))
    }
    raw = Tokenizer(models.WordLevel(vocabulary, unk_token="[UNK]"))
    raw.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    tokenizer = PreTrainedTokenizerFast(
        tokenizer_object=raw, unk_token="[UNK]", pad_token="[PAD]", eos_token="[EOS]"
    )
    tokenizer.chat_template = """{% for m in messages %}{{m['role'] + ' '}}{% if m['role'] == 'tool' %}{{'result'}}{% elif m.get('tool_calls') %}{{'<tool_call> {"name":"' + m['tool_calls'][0]['function']['name'] + '","arguments":' + (m['tool_calls'][0]['function']['arguments'] | tojson | replace(' ', '')) + '} </tool_call>'}}{% else %}{{m.get('content', '')}}{% endif %}{{' [EOS] '}}{% endfor %}{% if add_generation_prompt %}{{'assistant '}}{% endif %}"""
    tokenizer.save_pretrained(base)
    # Bootstrap a small base with tool syntax, then actually adapt its answer with LoRA.
    # This is a reproducible plumbing fixture, not an intelligence benchmark.
    model = GPT2LMHeadModel(
        GPT2Config(
            vocab_size=len(vocabulary),
            n_positions=4096,
            n_embd=96,
            n_layer=2,
            n_head=2,
            eos_token_id=2,
            pad_token_id=1,
        )
    )
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.0005)
    model.train()
    for step in range(2000):
        messages = conversations[step % len(conversations)]
        ids = tokenizer.apply_chat_template(messages, tokenize=True, return_tensors="pt")
        labels = ids.clone()
        prefix = tokenizer.apply_chat_template(
            messages[:-1], tokenize=True, add_generation_prompt=True
        )
        labels[:, : len(prefix)] = -100
        loss = model(ids, labels=labels).loss
        loss.backward()
        optimizer.step()
        optimizer.zero_grad()
    model.save_pretrained(base)
    store = TraceStore(tmp_path / "traces.db")
    session = store.create_session()
    for _ in range(8):
        for messages in conversations:
            run = store.create_run(session, messages[1]["content"], "synthetic-teacher")
            store.set_run_context(run, {"instructions": messages[0]["content"], "tools": []})
            # Preserve full observable tool conversation via SDK trace items.
            store.event(run, "sdk_trace", {"items": messages[1:]})
            store.finish_run(run, messages[-1]["content"])
            store.feedback(run, 1, None)
    dataset = tmp_path / "train.jsonl"
    assert export_dataset(store, dataset, "agent_sft", 0)[0] == 40
    train_lora(str(base), dataset, artifact, epochs=2, learning_rate=0.0001, max_length=512)
    registry = tmp_path / "registry.json"
    cli = [sys.executable, "-m", "jarvis.evals"]
    cli_options = ["--registry", str(registry), "--db", str(tmp_path / "traces.db")]
    subprocess.run(
        cli + ["register", "--name", "tiny", "--artifact", str(artifact)] + cli_options,
        check=True,
        capture_output=True,
    )
    with pytest.raises(ValueError):
        promote(registry, "tiny")
    cases = tmp_path / "cases.jsonl"
    rows = [
        {
            "id": "answer",
            "instructions": instruction,
            "input": "Say ready",
            "expected": "ready",
            "forbidden_tools": ["read_file", "write_file"],
        },
        {
            "id": "read",
            "instructions": instruction,
            "input": "Read note",
            "required_tools": ["read_file"],
            "expected": "ready",
        },
        {
            "id": "approval",
            "instructions": instruction,
            "input": "Write note",
            "required_tools": ["write_file"],
            "approval_required": True,
        },
    ]
    cases.write_text("\n".join(json.dumps(row) for row in rows))
    report = asyncio.run(evaluate_candidate(registry, "tiny", cases))
    (tmp_path / "evaluation.json").write_text(json.dumps(report, indent=2))
    assert report["passed"], report
    subprocess.run(
        cli + ["promote", "--name", "tiny"] + cli_options, check=True, capture_output=True
    )
    name, backend = active_backend(registry)
    assert name == "tiny"
    assert (
        backend.respond({"model": "local:tiny", "instructions": instruction, "input": "Say ready"})[
            "output"
        ][0]["content"][0]["text"]
        == "ready"
    )

    # The real supervisor selects the promoted artifact and confines the SDK agent.
    async def boot():
        state = tmp_path / "state"
        state.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        supervisor = Supervisor(
            Settings(
                _env_file=None,
                openai_api_key="unused-local",
                jarvis_db_path=state / "jarvis.db",
                jarvis_skills_path=state / "skills",
                jarvis_allowed_roots=[workspace],
                jarvis_model_registry=registry,
                jarvis_trace_mode="off",
            )
        )
        await supervisor.start()
        try:
            assert supervisor.settings.jarvis_model == "local:tiny"
            assert supervisor.child and supervisor.child.returncode is None
            await supervisor.forward(
                {"id": "runtime-check", "action": "chat", "payload": {"text": "Say ready"}}
            )
            while True:
                line = await asyncio.wait_for(supervisor.child.stdout.readline(), 30)
                assert line, "Agent exited before completing the runtime check"
                message = json.loads(line)
                if message.get("id") == "runtime-check":
                    assert message["result"]["status"] == "completed", message
                    assert message["result"]["message"].strip() == "ready", message
                    break
        finally:
            await supervisor.close()

    asyncio.run(boot())
    # Changed cases invalidate approval; changed weights invalidate serving.
    cases.write_text(cases.read_text() + "\n")
    with pytest.raises(ValueError):
        promote(registry, "tiny")
    (artifact / "tampered.txt").write_text("changed")
    with pytest.raises(ValueError):
        active_backend(registry)
