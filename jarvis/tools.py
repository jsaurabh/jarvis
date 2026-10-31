from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .security import AccessDenied, prompt_injection_warning, redact_text, resolve_allowed

FUNCTION_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "list_filesystem_roots",
        "description": "List the local folders and mounted volumes Jarvis is authorized to access.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "list_directory",
        "description": "List files and folders in an allowed local or mounted directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute directory path"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["path", "limit"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "read_file",
        "description": "Read a UTF-8 text file from an allowed path. Use offsets for large files.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 100000},
            },
            "required": ["path", "offset", "max_chars"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "search_files",
        "description": "Search file names and optionally text content below an allowed directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "root": {"type": "string"},
                "query": {"type": "string"},
                "search_contents": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["root", "query", "search_contents", "limit"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "write_file",
        "description": "Create or replace a UTF-8 text file. Always requires human approval.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "create_parents": {"type": "boolean"},
            },
            "required": ["path", "content", "create_parents"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "run_command",
        "description": "Run a local program without a shell. Pass the executable and each argument separately. Always requires human approval.",
        "parameters": {
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 64,
                },
                "cwd": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
            },
            "required": ["argv", "cwd", "timeout_seconds"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


class ToolExecutor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def definitions(self) -> list[dict[str, Any]]:
        result = []
        for definition in FUNCTION_TOOLS:
            if definition["name"] == "write_file" and not self.settings.jarvis_enable_writes:
                continue
            if definition["name"] == "run_command" and not self.settings.jarvis_enable_commands:
                continue
            result.append(definition)
        if self.settings.jarvis_enable_web_search:
            result.append({"type": "web_search"})
        return result

    def needs_approval(self, name: str, arguments: dict[str, Any]) -> bool:
        del arguments
        return name in {"write_file", "run_command"}

    def describe(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "write_file":
            return (
                f"Write {len(arguments.get('content', ''))} characters to {arguments.get('path')}"
            )
        if name == "run_command":
            return "Run: " + " ".join(arguments.get("argv", []))
        return f"Use {name}"

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "list_filesystem_roots": self._list_filesystem_roots,
            "list_directory": self._list_directory,
            "read_file": self._read_file,
            "search_files": self._search_files,
            "write_file": self._write_file,
            "run_command": self._run_command,
            "get_frontmost_application": self._get_frontmost_application,
            "list_running_applications": self._list_running_applications,
            "read_clipboard": self._read_clipboard,
            "set_clipboard": self._set_clipboard,
            "open_application": self._open_application,
            "open_url": self._open_url,
            "type_text": self._type_text,
            "press_key": self._press_key,
            "click_screen": self._click_screen,
            "list_calendar_events": self._list_calendar_events,
            "create_calendar_event": self._create_calendar_event,
        }
        if name not in handlers:
            return {"ok": False, "error": f"Unknown local tool: {name}"}
        try:
            result = await handlers[name](**arguments)
            return {"ok": True, "result": result}
        except (OSError, UnicodeError, AccessDenied, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    async def _list_filesystem_roots(self) -> dict[str, Any]:
        return {"roots": [str(root) for root in self.settings.allowed_roots]}

    async def _list_directory(self, path: str, limit: int) -> dict[str, Any]:
        target = resolve_allowed(path, self.settings.allowed_roots)
        if not target.is_dir():
            raise ValueError(f"Not a directory: {target}")
        entries = []
        for child in sorted(
            target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())
        )[:limit]:
            stat = child.stat()
            entries.append(
                {
                    "name": child.name,
                    "path": str(child),
                    "kind": "directory" if child.is_dir() else "file",
                    "size": stat.st_size,
                    "modified_ns": stat.st_mtime_ns,
                }
            )
        return {"path": str(target), "entries": entries, "truncated": len(entries) == limit}

    async def _read_file(self, path: str, offset: int, max_chars: int) -> dict[str, Any]:
        target = resolve_allowed(path, self.settings.allowed_roots)
        if not target.is_file():
            raise ValueError(f"Not a file: {target}")
        if target.stat().st_size > 20_000_000:
            raise ValueError("File is larger than the 20 MB local read limit")
        text = target.read_text(encoding="utf-8", errors="replace")
        chunk = redact_text(text[offset : offset + max_chars])
        result = {
            "path": str(target),
            "content": chunk,
            "offset": offset,
            "next_offset": offset + len(chunk),
            "eof": offset + len(chunk) >= len(text),
        }
        warning = prompt_injection_warning(chunk)
        if warning:
            result["security_warning"] = warning
        return result

    async def _search_files(
        self, root: str, query: str, search_contents: bool, limit: int
    ) -> dict[str, Any]:
        target = resolve_allowed(root, self.settings.allowed_roots)
        needle = query.casefold()
        matches: list[dict[str, Any]] = []
        skipped = {".git", ".venv", "node_modules", "Library", "__pycache__"}
        for current, directories, files in os.walk(target):
            directories[:] = [item for item in directories if item not in skipped]
            for filename in files:
                path = Path(current) / filename
                if needle in filename.casefold():
                    matches.append({"path": str(path), "match": "name"})
                elif search_contents and path.stat().st_size <= 2_000_000:
                    try:
                        content = path.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    index = content.casefold().find(needle)
                    if index >= 0:
                        snippet = redact_text(content[max(0, index - 100) : index + 300])
                        match: dict[str, Any] = {
                            "path": str(path),
                            "match": "content",
                            "snippet": snippet,
                        }
                        warning = prompt_injection_warning(snippet)
                        if warning:
                            match["security_warning"] = warning
                        matches.append(match)
                if len(matches) >= limit:
                    return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    async def _write_file(self, path: str, content: str, create_parents: bool) -> dict[str, Any]:
        if not self.settings.jarvis_enable_writes:
            raise AccessDenied("Writes are disabled")
        target = resolve_allowed(path, self.settings.allowed_roots)
        if create_parents:
            target.parent.mkdir(parents=True, exist_ok=True)
        elif not target.parent.is_dir():
            raise ValueError(f"Parent directory does not exist: {target.parent}")
        target.write_text(content, encoding="utf-8")
        return {"path": str(target), "characters": len(content)}

    async def _run_command(self, argv: list[str], cwd: str, timeout_seconds: int) -> dict[str, Any]:
        if not self.settings.jarvis_enable_commands:
            raise AccessDenied("Commands are disabled")
        working_directory = resolve_allowed(cwd, self.settings.allowed_roots)
        if not working_directory.is_dir():
            raise ValueError(f"Not a directory: {working_directory}")
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=working_directory,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._sanitized_environment(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ValueError(f"Command timed out after {timeout_seconds} seconds") from exc
        return {
            "exit_code": process.returncode,
            "stdout": redact_text(
                stdout.decode(errors="replace")[: self.settings.jarvis_tool_output_limit]
            ),
            "stderr": redact_text(
                stderr.decode(errors="replace")[: self.settings.jarvis_tool_output_limit]
            ),
        }

    async def _get_frontmost_application(self) -> dict[str, Any]:
        output = await self._run_program(
            [
                "/usr/bin/osascript",
                "-e",
                'tell application "System Events" to get name of first application process whose frontmost is true',
            ]
        )
        return {"application": output.strip()}

    async def _list_running_applications(self) -> dict[str, Any]:
        output = await self._run_program(
            [
                "/usr/bin/osascript",
                "-e",
                'tell application "System Events" to get name of every application process whose background only is false',
            ]
        )
        return {"applications": [item.strip() for item in output.split(",") if item.strip()]}

    async def _read_clipboard(self) -> dict[str, Any]:
        output = redact_text(await self._run_program(["/usr/bin/pbpaste"]))
        result: dict[str, Any] = {"text": output[: self.settings.jarvis_tool_output_limit]}
        warning = prompt_injection_warning(output)
        if warning:
            result["security_warning"] = warning
        return result

    async def _set_clipboard(self, text: str) -> dict[str, Any]:
        await self._run_program(["/usr/bin/pbcopy"], input_text=text)
        return {"characters": len(text)}

    async def _open_application(self, name: str) -> dict[str, Any]:
        if not name.strip() or len(name) > 128:
            raise ValueError("Invalid application name")
        await self._run_program(["/usr/bin/open", "-a", name])
        return {"application": name, "opened": True}

    async def _open_url(self, url: str) -> dict[str, Any]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Only absolute http and https URLs can be opened")
        await self._run_program(["/usr/bin/open", url])
        return {"url": url, "opened": True}

    async def _type_text(self, text: str) -> dict[str, Any]:
        if len(text) > 10_000:
            raise ValueError("Text is too long for UI typing")
        script = (
            'on run argv\ntell application "System Events" to keystroke (item 1 of argv)\nend run'
        )
        await self._run_program(["/usr/bin/osascript", "-e", script, text])
        return {"characters": len(text)}

    async def _press_key(self, key: str) -> dict[str, Any]:
        key_codes = {
            "return": 36,
            "tab": 48,
            "space": 49,
            "delete": 51,
            "escape": 53,
            "left": 123,
            "right": 124,
            "down": 125,
            "up": 126,
        }
        if key not in key_codes:
            raise ValueError(f"Unsupported key: {key}")
        await self._run_program(
            [
                "/usr/bin/osascript",
                "-e",
                f'tell application "System Events" to key code {key_codes[key]}',
            ]
        )
        return {"key": key, "pressed": True}

    async def _click_screen(self, x: int, y: int) -> dict[str, Any]:
        if not (0 <= int(x) <= 20_000 and 0 <= int(y) <= 20_000):
            raise ValueError("Screen coordinates are out of range")
        await self._run_program(
            [
                "/usr/bin/osascript",
                "-e",
                f'tell application "System Events" to click at {{{int(x)}, {int(y)}}}',
            ]
        )
        return {"x": int(x), "y": int(y), "clicked": True}

    async def _list_calendar_events(self, days: int) -> dict[str, Any]:
        days = max(1, min(int(days), 31))
        script = """
        on run argv
          set dayCount to (item 1 of argv) as integer
          set startDate to current date
          set endDate to startDate + (dayCount * days)
          tell application "Calendar"
            set rows to {}
            repeat with cal in calendars
              repeat with ev in (every event of cal whose start date is greater than or equal to startDate and start date is less than endDate)
                set end of rows to ((summary of ev as text) & " | " & (start date of ev as text) & " | " & (name of cal as text))
              end repeat
            end repeat
          end tell
          set AppleScript's text item delimiters to linefeed
          return rows as text
        end run
        """
        output = await self._run_program(["/usr/bin/osascript", "-e", script, str(days)])
        events = [redact_text(line) for line in output.splitlines() if line.strip()]
        return {"days": days, "events": events[:200]}

    async def _create_calendar_event(
        self, title: str, start_at: str, end_at: str, calendar_name: str
    ) -> dict[str, Any]:
        if not title.strip():
            raise ValueError("Event title is required")
        try:
            start = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
            end = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Calendar times must be ISO 8601") from exc
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("Calendar times need timezones and end_at must be after start_at")
        local_start = start.astimezone().strftime("%m/%d/%Y %I:%M:%S %p")
        local_end = end.astimezone().strftime("%m/%d/%Y %I:%M:%S %p")
        script = """
        on run argv
          set eventTitle to item 1 of argv
          set startText to item 2 of argv
          set endText to item 3 of argv
          set calendarName to item 4 of argv
          set startDate to date startText
          set endDate to date endText
          tell application "Calendar"
            tell calendar calendarName
              make new event with properties {summary:eventTitle, start date:startDate, end date:endDate}
            end tell
          end tell
        end run
        """
        await self._run_program(
            ["/usr/bin/osascript", "-e", script, title, local_start, local_end, calendar_name]
        )
        return {"created": True, "title": title, "start_at": start_at, "end_at": end_at}

    async def _run_program(self, argv: list[str], input_text: str | None = None) -> str:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._sanitized_environment(),
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input_text.encode() if input_text is not None else None), timeout=30
        )
        if process.returncode != 0:
            raise ValueError(
                redact_text(stderr.decode(errors="replace").strip() or "Program failed")
            )
        return stdout.decode(errors="replace")

    @staticmethod
    def _sanitized_environment() -> dict[str, str]:
        blocked = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL")
        return {
            key: value
            for key, value in os.environ.items()
            if not any(part in key.upper() for part in blocked)
        }
