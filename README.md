# Jarvis for macOS

Jarvis is a native, always-available macOS menu-bar agent. There is no web interface. A persistent local Jarvis Core process now owns durable tasks, OpenAI Agents SDK sessions, approvals, cancellation, restart recovery, and traces; voice is one realtime interface to that core.

Version 1.1 includes explicit long-term memory, durable reminders, scheduled full-agent automations, native text chat, inspectable skills, macOS application and Calendar tools, approval checkpoints, versioned agent trajectories, trace-backed evaluation gates, and tool-aware local LoRA training support.

Saved memories remain local and are not silently inserted into model requests. Voice or automatic recall requires a separate privacy opt-in because recalled content would be sent to OpenAI to formulate the answer.

When Listening is enabled, Jarvis keeps a low-latency OpenAI Realtime audio session open. Say **Jarvis** followed by a request, then continue the conversation naturally without repeating the wake word:

> “Jarvis, summarize the latest notes in my Documents folder.”

Microphone audio is streamed to the configured Realtime model while Listening is enabled. Responses begin playing as they are generated, and speaking over Jarvis interrupts it immediately. File, device, mounted-drive, current-web, and action requests delegate to the existing local tool agent; file writes and local commands still require a spoken **approve** or **deny** first.

## First-time setup

From the repository root:

```bash
# Reproducible install from uv.lock
uv sync --extra dev
source .venv/bin/activate
./scripts/build_macos.sh
./scripts/enable_start_at_login.sh
open dist/Jarvis.app
```

Without `uv`, create a virtual environment and run `python -m pip install -e '.[dev]'`.

First launch performs three one-time steps:

1. Enter an OpenAI API key. It is stored in macOS Keychain, not `.env` or source control.
2. Allow microphone and notification access.
3. When first using computer or Calendar tools, approve the relevant macOS Automation or Accessibility permission.

Jarvis then appears in the menu bar. Its native controls include:

- **Ask Jarvis…** — typed chat using the same durable agent as voice.
- **Review Pending Action…** — approve or deny a paused side effect without using voice.
- **Recent Activity…** — inspect recent durable agent work.
- **Listening** — pause or resume the wake word.
- **Start at Login** — start automatically after signing in or restarting. Enabled by default on first launch.
- **Saved Memories…** — inspect locally saved memories and forget individual items.
- **Reminders…** — inspect active reminders and cancel individual items.
- **Automations…** — inspect, pause, resume, or cancel scheduled full-agent work.
- **Skills…** — inspect installed Jarvis procedures.
- **Learning & Quality…** — inspect feedback and training eligibility.
- **Rate Last Result…** — mark a result useful, unhelpful, or save a corrected answer.
- **Quit Jarvis** — end the current process. If Start at Login remains enabled, it starts again next login.

The two toggle values persist across restarts. No browser or local web server is involved.

## Voice behavior

- Say **“Jarvis”** and then the first request. Follow-up turns are conversational.
- Interrupt Jarvis simply by speaking; queued audio is discarded and the new turn takes priority.
- Ask **“What are you working on?”** for the latest durable task state.
- Say **“Remember that …”** to save an explicit local memory.
- Say **“Remind me …”** to create a durable native notification.
- Say **“Every morning, review …”** to schedule recurring full-agent work.
- Say **“Stop”** or **“Cancel that task”** to cancel ongoing agent work.
- For a write or command, Jarvis describes the action and asks you to say **approve** or **deny**.
- Say **“Jarvis, that was useful”** to mark the last result as training-eligible.
- Say **“Jarvis, correction …”** followed by the better answer to save a corrected target.

The Listening toggle is the privacy boundary: when it is on, microphone audio is streamed to the OpenAI Realtime API; when it is off, the live session and microphone capture stop.

## Machine access

The native launcher gives the tool layer `/` as its allowed root, matching the requested machine-wide scope. macOS privacy controls still apply. To reach protected folders, add `Jarvis.app` under **System Settings → Privacy & Security → Full Disk Access**.

Mounted network shares appear below `/Volumes` and work like local paths. Remote machines can be reached through an approved `ssh` command after SSH keys and host access are configured separately.

File reads happen without a prompt. Clipboard reads, Calendar access, file writes, executable commands, opening apps or URLs, typing, key presses, clicks, skill changes, and calendar writes require fresh approval. Approvals can be spoken or reviewed from the menu. Commands execute as an argument array without an intervening shell, and API keys and credential-shaped environment variables are removed from child processes.

Application control uses macOS Accessibility and Apple Events. Jarvis does not bypass CAPTCHA, security warnings, paywalls, or permission dialogs. It treats documents, websites, emails, calendar entries, clipboard text, screen content, and tool results as untrusted context rather than user permission.

## Agent runtime

The native application starts one long-lived `jarvis.daemon` child and exchanges newline-delimited JSON messages over private process pipes. The daemon can serve concurrent status and cancellation requests while an agent task is running.

Each request becomes a durable task in SQLite before model work begins. Safe, pre-approval work is requeued after a restart. Work that had crossed an approved side-effect boundary is retained as interrupted for review instead of being blindly repeated. SDK approval checkpoints are serialized in SQLite and can be resumed after the native UI returns.

The OpenAI Agents SDK owns the model/tool loop and its OpenAI trace. Jarvis Core separately owns task lifecycle, local audit events, permission policy, reminders, automations, skills, provider-independent state, and native notifications.

Scheduled automations launch ordinary durable tasks. They can pause for approval, survive restarts, report results through native notifications, and are prevented from blindly repeating a side effect after a crash.

## Traces and model flywheel

Runs are stored locally in `data/jarvis.db`. Each `jarvis.trajectory.v2` episode preserves the observable request, model messages, tool calls, tool results, approvals, final answer, timing, deterministic scores, and human feedback. It also snapshots the exact prompt, tool schemas, model settings, installed skills, and their hashes, so a result can be tied to the harness that produced it. Raw microphone audio, hidden chain-of-thought, and encrypted reasoning are never put in the training dataset.

```bash
source .venv/bin/activate

# Preferred agent SFT: preserves tool calls, tool results, and harness context
jarvis-export --format agent_sft --output exports/jarvis-agent-sft.jsonl

# Chosen/rejected pairs created from your corrections
jarvis-export --format preference --output exports/jarvis-preference.jsonl

# Common open-model conversation format
jarvis-export --format chatml --output exports/jarvis-chatml.jsonl

# Complete agent trajectories
jarvis-export --format trajectory --output exports/jarvis-trajectories.jsonl
```

Every export produces a sibling `*.eval.jsonl` and a `*.manifest.json` containing hashes and selection counts. The split is grouped by durable task or conversation-day rather than individual turns, reducing train/evaluation leakage. Common credentials and personal identifiers are excluded by default; `--include-sensitive` is an explicit local override.

All activity is retained as local review/evaluation evidence, but only explicitly useful replies and corrections enter supervised training exports. Corrections additionally become preference pairs and permanent regression cases.

The 1.1 flywheel is gated rather than self-modifying:

```bash
# Inspect quality and create a permanent evaluation snapshot
jarvis-flywheel report
jarvis-flywheel snapshot

# Package traces, regression cases, harness versions, and a Codex handoff
jarvis-flywheel bundle --bundle-dir exports/flywheel

# Build a trajectory dataset with deterministic holdout
jarvis-flywheel export --output exports/jarvis-trajectories.jsonl

# Optional: install local training dependencies and train a LoRA adapter
python -m pip install -e '.[training]'
jarvis-train --base-model Qwen/Qwen3-4B --dataset exports/jarvis-agent-sft.jsonl --output models/jarvis-qwen-lora

# Registration and promotion are refused until quality gates pass
jarvis-flywheel register --name jarvis-qwen --artifact models/jarvis-qwen-lora --base-model Qwen/Qwen3-4B
jarvis-flywheel promote --name jarvis-qwen
```

Promotion requires at least 25 human-vetted examples, 10 rated runs, an 80% positive rating rate, and no more than a 10% run failure rate. The local trainer uses the model's chat template, preserves tool-call structure, and masks loss to assistant output where the tokenizer supports it. Training artifacts include a dataset hash and manifest for reproducibility. Jarvis never retrains, rewrites its harness, or changes its active model without review and a gated, explicit promotion step.

To serve an owned model later, set `JARVIS_BASE_URL` and `JARVIS_MODEL` in the native process environment or adapt `BackendController`; the endpoint must implement the OpenAI Responses API tool-calling contract.

## Development

```bash
source .venv/bin/activate
ruff check jarvis tests
ruff format --check jarvis tests
swift format lint --recursive macos
pytest
python -m compileall jarvis
./scripts/build_macos.sh
```

Key files:

```text
macos/Jarvis.swift       Native menu bar, Keychain, persistent Core transport
macos/RealtimeVoice.swift Persistent Realtime audio, VAD, playback, interruption handling
jarvis/daemon.py         Long-lived private JSON task daemon
jarvis/core.py           Durable tasks, recovery, cancellation, and approvals
jarvis/automations.py    Once, interval, and daily full-agent schedules
jarvis/reminders.py      Durable local notification queue
jarvis/memory.py         User-controlled local memory
jarvis/skills.py         Inspectable and editable local skills
jarvis/sdk_agent.py      OpenAI Agents SDK adapter and resumable tool loop
jarvis/native_bridge.py  Legacy one-shot compatibility bridge
jarvis/agent.py          Legacy Responses loop and shared agent instructions
jarvis/tools.py          Local capabilities and deterministic policy
jarvis/store.py          SQLite tasks, traces, sessions, feedback, approvals
jarvis/learning.py       Versioned trajectories, harness snapshots, privacy filters
jarvis/export.py         Training and evaluation exporters
jarvis/flywheel.py       Review bundle and durable regression-case builder
jarvis/evals.py          Quality snapshots and model promotion gates
jarvis/train.py          Optional local LoRA training pipeline
```

## License

Jarvis is available under the [MIT License](LICENSE).
