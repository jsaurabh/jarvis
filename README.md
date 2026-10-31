# Jarvis

Jarvis is an open-source, local-first personal agent for macOS. It lives in the menu bar, responds to voice or text, works with files and applications on your Mac, and keeps a durable record of its work so you can improve both the agent harness and the model behind it.

This repository is the first public release. It is useful today, but its interfaces, storage format, and setup flow may evolve quickly.

## What it can do

- Hold low-latency voice conversations with streaming speech and interruption support.
- Continue the same work through native text chat.
- Read files across local disks and mounted network volumes.
- Work with applications, Calendar, the clipboard, URLs, and local commands.
- Remember user-approved facts, create reminders, and run scheduled agent tasks.
- Pause sensitive actions for explicit approval.
- Recover safe unfinished work after a restart.
- Capture structured trajectories, feedback, evaluations, and training exports.

## How it works

Jarvis has two cooperating layers:

```text
Voice or text
      │
      ▼
macOS menu-bar app ── Realtime speech, notifications, Keychain, approvals
      │
      ▼
Local agent core ──── Tasks, tools, memory, automations, traces, recovery
      │                    │
      ├── OpenAI models    └── SQLite data store
      └── Your Mac
```

The menu-bar app handles audio, native controls, notifications, and macOS permissions. A persistent local Python process runs the agent loop and manages durable work. The two communicate through private process pipes.

OpenAI Realtime provides streaming speech. The OpenAI Agents SDK drives model and tool execution. Agent state, approvals, feedback, and observable execution traces are stored locally in SQLite.

Jarvis instruments the Agents SDK with OpenInference, exports OpenTelemetry spans over OTLP/HTTP, and runs a self-hosted Arize Phoenix collector and viewer on `127.0.0.1:6006`. The OpenInference instrumentor replaces the Agents SDK's default trace processor, so agent traces are not sent to OpenAI's hosted trace service. Phoenix is started with both internet access and its own product telemetry disabled.

**View Recent Activity…** opens the local Phoenix trace viewer. Trace spans carry the Jarvis session, run, task, interface, and harness version as correlation metadata.

## Requirements

- macOS 14 or newer on Apple silicon
- Python 3.11 or newer
- Xcode Command Line Tools
- An OpenAI API key
- [`uv`](https://docs.astral.sh/uv/) recommended for Python environment management

## Install

Clone the repository and run:

```bash
uv sync --extra dev
source .venv/bin/activate
./scripts/build_macos.sh
./scripts/enable_start_at_login.sh
open dist/Jarvis.app
```

The setup script registers Jarvis to open automatically. Manage that behavior in **System Settings → General → Login Items & Extensions**.

Without `uv`, create a virtual environment and install the project with:

```bash
python -m pip install -e '.[dev]'
```

On first launch, **Setup Jarvis** guides you through the OpenAI API key and macOS permissions. The key is stored in macOS Keychain. Microphone, notifications, Accessibility, protected files, Calendar, application automation, and login behavior remain under macOS control.

Open **Setup Jarvis…** from the menu at any time to replace the API key, request a permission again, or jump to the relevant System Settings page.

To install the built application in the standard Applications folder:

```bash
ditto dist/Jarvis.app /Applications/Jarvis.app
open /Applications/Jarvis.app
```

## Use Jarvis

Turn on **Listening**, then say the wake word followed by a request:

> “Jarvis, summarize the latest notes in my Documents folder.”

After the first request, continue the conversation naturally. Speak while Jarvis is responding to interrupt it.

Useful phrases include:

- “Remember that …”
- “Remind me …”
- “Every morning, review …”
- “What are you working on?”
- “Stop” or “Cancel that task”
- “That was useful”
- “Correction: …”

The menu provides text chat, recent activity, pending approvals, memories, reminders, automations, skills, learning metrics, listening controls, **Setup Jarvis…**, and a **Quit Jarvis** command.

When **Listening** is on, microphone audio is streamed to the configured OpenAI Realtime model. Turning it off closes the live session and stops microphone capture.

## Permissions and safety

Jarvis can read from the filesystem roots available to its process, including mounted volumes under `/Volumes`. macOS privacy controls continue to protect restricted locations and applications.

Read-only file access does not require an approval. Actions with side effects do. Jarvis asks before it:

- writes or deletes files;
- runs local commands;
- reads the clipboard;
- opens applications or URLs;
- types, clicks, or sends key presses;
- changes Calendar data; or
- installs or changes a skill.

Approvals can be spoken or reviewed from the menu. Commands run as argument arrays without an intervening shell, and credential-shaped environment variables are removed from child processes.

Documents, websites, messages, calendar entries, clipboard contents, screen content, and tool results are treated as untrusted context. They cannot grant permission or override the approval policy.

## Local data and privacy

Runtime data is stored in `data/jarvis.db` and is excluded from Git. The repository also excludes exports, model artifacts, build products, virtual environments, environment files, and session identifiers.

Saved memories are not automatically inserted into model prompts. Raw microphone audio and hidden model reasoning are not added to training datasets.

Deleting `data/jarvis.db` removes local Jarvis history, memories, reminders, automations, feedback, and task state. Back it up first if you want to preserve that data.

Phoenix stores its local OpenTelemetry history separately in `data/phoenix.db`. Removing that file clears the trace viewer without deleting Jarvis tasks, memories, feedback, or curated training records.

## Training-data flywheel

Jarvis records each agent run as a structured trajectory containing the observable request, model messages, tool calls, tool results, approvals, final answer, timing, scores, feedback, and a snapshot of the harness that produced it.

Only explicitly useful answers and corrections are selected for supervised training exports. Corrections also produce preference pairs and reusable regression cases. Common credentials and personal identifiers are excluded by default.

Export datasets with:

```bash
# Tool-aware supervised fine-tuning data
jarvis-export --format agent_sft --output exports/jarvis-agent-sft.jsonl

# Chosen/rejected preference pairs
jarvis-export --format preference --output exports/jarvis-preference.jsonl

# Open-model conversation format
jarvis-export --format chatml --output exports/jarvis-chatml.jsonl

# Complete observable trajectories
jarvis-export --format trajectory --output exports/jarvis-trajectories.jsonl
```

Each export includes a deterministic evaluation split and a manifest with hashes and selection counts.

Inspect and package the improvement loop with:

```bash
jarvis-flywheel report
jarvis-flywheel snapshot
jarvis-flywheel bundle --bundle-dir exports/flywheel
jarvis-flywheel export --output exports/jarvis-trajectories.jsonl
```

Use the local Phoenix viewer for low-level OpenInference spans, latency, model calls, tool calls, and trace inspection. Use Jarvis's reviewed JSONL exports for fine-tuning datasets and deterministic evaluation splits.

Optional LoRA training is available through the `training` dependency group:

```bash
python -m pip install -e '.[training]'
jarvis-train \
  --base-model Qwen/Qwen3-4B \
  --dataset exports/jarvis-agent-sft.jsonl \
  --output models/jarvis-qwen-lora
```

Candidate models must pass evaluation gates before registration or promotion. Jarvis does not retrain itself or change its active model automatically.

## Project layout

```text
macos/                    Native menu-bar application and realtime voice
jarvis/core.py            Durable task lifecycle, recovery, and approvals
jarvis/sdk_agent.py       Agents SDK model and tool loop
jarvis/tools.py           Local capabilities and permission policy
jarvis/store.py           SQLite state and execution traces
jarvis/automations.py     Scheduled agent work
jarvis/memory.py          User-controlled long-term memory
jarvis/learning.py        Trajectories, harness snapshots, privacy filters
jarvis/export.py          Training and evaluation dataset exports
jarvis/evals.py           Quality metrics and model promotion gates
jarvis/train.py           Optional local LoRA training pipeline
tests/                    Agent, policy, storage, and export tests
```

## Development

```bash
uv sync --extra dev
source .venv/bin/activate

ruff check jarvis tests
ruff format --check jarvis tests
swift format lint --recursive macos
pytest
python -m compileall jarvis
./scripts/build_macos.sh
```

GitHub Actions runs the same checks on every push and pull request. See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines and [SECURITY.md](SECURITY.md) for vulnerability reporting.

## License

Jarvis is available under the [MIT License](LICENSE).
