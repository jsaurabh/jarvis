#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
PROJECT_DIR=${SCRIPT_DIR:h}
export HOMEBREW_NO_AUTO_UPDATE=1
export HOMEBREW_NO_INSTALL_CLEANUP=1

if ! command -v brew >/dev/null; then
  print -u2 "Homebrew is required to install the isolated execution runtimes."
  exit 1
fi
if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  print -u2 "The Jarvis Python environment is missing. Install the project dependencies first."
  exit 1
fi
"$PROJECT_DIR/.venv/bin/python" -c 'import nono_py, h11, httpx'
if [[ ! -x /opt/homebrew/bin/shuru ]]; then
  brew install superhq-ai/tap/shuru
fi
if [[ "$(/opt/homebrew/bin/shuru --version)" != "shuru 0.11.0" ]]; then
  print -u2 "Jarvis requires Shuru 0.11.0. The installed version has not been validated."
  exit 1
fi
if [[ ! -x /opt/homebrew/opt/squid/sbin/squid ]]; then
  brew install squid
fi
if [[ "$(/opt/homebrew/opt/squid/sbin/squid -v | head -n 1)" != "Squid Cache: Version 7.8" ]]; then
  print -u2 "Jarvis requires Squid 7.8. The installed version has not been validated."
  exit 1
fi
if [[ ! -f "$HOME/.local/share/shuru/VERSION" ]] || [[ "$(<"$HOME/.local/share/shuru/VERSION")" != "0.11.0" ]]; then
  /opt/homebrew/bin/shuru init
fi
