# Jarvis

Jarvis is an open-source, local-first personal agent for macOS. It lives in the menu bar, responds to voice or text, works with files and applications on your Mac, and keeps a durable record of its work so you can improve both the agent harness and the model behind it.

This repository is the first public release. It is useful today, but its interfaces, storage format, and setup flow may evolve quickly.

## What it can do

- Hold low-latency voice conversations with streaming speech and interruption support.
- Continue the same work through native text chat.
- Read files inside user-authorized folders, local disks, and mounted network volumes.
- Work with applications, Calendar, the clipboard, and URLs.
- Remember user-approved facts, create reminders, and run scheduled agent tasks.
- Pause sensitive actions for explicit approval.
- Recover safe unfinished work after a restart.
- Capture structured trajectories, feedback, evaluations, and training exports.

## How it works

The native app, trusted supervisor, and confined agent cooperate through private
process pipes:

```text
Voice or text
      │
      ▼
macOS menu-bar app ── Realtime speech, notifications, Keychain, approval dialogs
      │
      ▼
Trusted supervisor ── Model gateway, filesystem scope, one-use approval grants
      │                    │
      ▼                    └── Restricted workers and disposable Linux VMs
Confined agent core ─ Tasks, tools, memory, automations, recovery, local records
```

The app handles audio and macOS permissions. The agent uses the OpenAI Agents
SDK to plan and execute durable work. The supervisor mediates access to the
model and the computer; the agent cannot directly launch programs or access
files outside its runtime state.

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

On first launch, **Setup Jarvis** guides you through the OpenAI API key, authorized filesystem locations, and macOS permissions. The key is stored in macOS Keychain. Jarvis defaults to the user's Home folder; additional folders or mounted volumes must be added explicitly. Microphone, notifications, Accessibility, protected files, Calendar, application automation, and login behavior remain under macOS control.

Open **Setup Jarvis…** from the menu at any time to add or revoke filesystem locations, replace the API key, request a permission again, or jump to the relevant System Settings page. Filesystem scope is saved across restarts and changes take effect immediately in the running agent.

For isolated command execution, open **Setup Jarvis…**, select **Set up…** under **Isolated execution**, and then select **Test**. Setup installs the required external tools and downloads the VM image. Test checks the configured OpenAI key by listing models inside the VM; it does not send workspace files or generate a model response. Squid is a separate GPL-licensed dependency.

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

Jarvis can read only within the folders and volumes listed in **Setup Jarvis**. The local executor resolves paths and symlinks against those roots again at execution time. To use a mounted disk or network share under `/Volumes`, add that volume or one of its folders in Setup. macOS privacy controls continue to protect restricted locations and applications.

The agent runs in a nono-restricted process with access to its runtime state, but no direct access to other user files, process execution, or the network. A separate supervisor holds the configured filesystem scope, mediates tools, and issues one-use approvals for the exact action shown in the app. The supervisor and app run as your macOS user; they remain trusted, as do other software running with your account's privileges. The restricted agent receives a temporary token for model requests; the supervisor injects the OpenAI key through a local streaming gateway.

File operations run in additional nono-restricted workers. Linux commands run in a Shuru VM and require approval. The VM is offline by default. If exact HTTPS destinations are approved, a local Squid proxy inspects TLS and filters traffic; this requires Squid as well as Shuru. Set `JARVIS_ENABLE_COMMANDS=false` to disable commands.

VM input is a bounded temporary snapshot. It excludes common credential filenames, Git metadata, dependency folders, symbolic links and hard links. Filename filtering does not guarantee removal of every secret; avoid running VM commands on workspaces containing secrets. For previews, the VM writes only to a size-limited temporary disk image, and the supervisor independently builds the review after execution. Supported text changes are proposed one file at a time and require a separate approval before application. Proposals expire after an hour and are lost on restart; conflicted files are rejected. A non-cooperating local writer can still race the final conflict check. A separate native tool runs an allowlisted set of compilers under nono after approval; native projects can execute code and modify the selected folder.

Patch review is limited to 2,000 files and 32 MiB per workspace copy, with at most ten pending proposals, 2 MiB per file, and a 12 KiB review per proposed file. Binary changes and links are rejected. Newly created files and directories use private permissions.

The current adapter accepts Shuru 0.11.0 only and rejects other versions until they are tested. Version checking does not authenticate the executable or guest image.

For a separately approved VM request, the trusted app passes its in-memory OpenAI key through private pipes to a local credential gate. The guest receives only a temporary token. The gate permits one request to list models or create a response, with a 64 KiB request limit and at most 2,048 output tokens. Model requests can transmit workspace data and incur charges. Literal-key filtering is not a guarantee against transformed secrets in upstream responses.

Read-only file access does not require an approval. Actions with side effects do. Jarvis asks before it:

- writes or deletes files;
- runs a VM command or native compiler;
- reads the clipboard;
- opens applications or URLs;
- types, clicks, or sends key presses;
- changes Calendar data; or
- installs or changes a skill.

Sensitive actions must be approved in the native review dialog. Child processes receive a minimal environment without the agent's model credentials.

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
jarvis/supervisor.py      Trusted model and tool access boundary
jarvis/broker_authority.py Exact, one-use tool authorization
jarvis/execution.py       Restricted file workers and Shuru VM execution
jarvis/patch_store.py     Host-generated patch review and application
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
jarvis/model_lifecycle.py Candidate evaluation and trusted model selection
jarvis/local_model.py     Local inference adapter
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

## Evaluate and activate a trained model

Install `uv sync --extra dev --extra training` before local training or inference.
Training writes an adapter, tokenizer, a local copy of the base weights, and a
manifest. It uses assistant-only loss and rejects examples that exceed the
context limit rather than silently dropping their targets. Keep the complete
output directory together. Local inference supports text and Qwen-style
`<tool_call>` JSON; choose a model and chat template compatible with that format.
Voice continues to use OpenAI Realtime; the local model handles agent tasks.

The trusted model registry defaults to `models/registry.json`, outside writable
agent state. The existing `snapshot` command reports historical usage quality;
it does not evaluate a model. Candidate evaluation is a separate command:

```bash
jarvis-flywheel register --name candidate-v1 --artifact models/jarvis-qwen-lora
jarvis-flywheel evaluate --name candidate-v1 --cases evals/local-agent.jsonl
jarvis-flywheel promote --name candidate-v1
```

Evaluation uses real local generation through the Responses gateway and Agents
SDK, with disposable files, restricted reads, and writes paused for approval.
Every case must pass. The suite must cover answers, successful file reads, and
write approval. Add your curated regression cases to this suite. Optional
`--baseline NAME` requires a previously evaluated baseline on the same cases.
These checks validate the supplied cases, not general model intelligence.
Reports bind the results to model weights, base weights, cases, and harness
code. Changed artifacts or harness code require reevaluation. Changed case
files require reevaluation before promotion.

Promotion takes effect when Jarvis restarts. The supervisor verifies and loads
the selected local adapter behind the same gateway used by the confined agent;
the agent gets no new host access. To return to the configured OpenAI model:

```bash
jarvis-flywheel use-cloud
```

Restart Jarvis after changing the selection. A broken or modified active model
fails startup rather than silently falling back to another model.

`JARVIS_TRACE_MODE=metadata` omits event bodies and prompt/tool/skill content
from harness trace records. Durable task and conversation state still exists
in SQLite because it is needed to resume work. This setting is not a history
delete switch. Improvement bundles apply the privacy filter to traces,
regression cases, and harness snapshots. Review exports before sharing them.

Run the reproducible CPU training and promotion smoke test with:

```bash
JARVIS_TRAINING_TEST=1 pytest tests/test_training_loop.py -q
```

It creates a tiny synthetic base model, exports human-vetted synthetic traces,
trains and reloads a real LoRA adapter, checks tool use and approval, promotes
it in an isolated registry, and runs it through the confined agent. It also
checks rejection after artifact or case changes. It does not train on your
personal history or change the installed app's model selection.
