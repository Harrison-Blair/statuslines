#!/usr/bin/env python3
"""Provider-neutral status-line renderer and native configuration adapter."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
from typing import Any


SUPPORTED_SEGMENTS = {
    "model",
    "context_tokens",
    "five_hour_remaining",
    "weekly_remaining",
    "project",
    "git_branch",
    "session_name",
    "session_tokens",
    "cost",
    "lines_changed",
    "prompt_cache",
}

UNLABELED_SEGMENTS = {"model", "project", "git_branch", "session_name", "cost", "lines_changed"}

# Segments with no native Codex equivalent map to nothing.
CODEX_SEGMENTS = {
    "model": ("model-with-reasoning",),
    "context_tokens": ("used-tokens", "context-window-size"),
    "five_hour_remaining": ("five-hour-limit",),
    "weekly_remaining": ("weekly-limit",),
    "project": ("project-name",),
    "git_branch": ("git-branch",),
    "session_name": ("thread-name",),
    "session_tokens": ("total-input-tokens", "total-output-tokens"),
    "cost": ("estimated-thread-cost",),
    "lines_changed": (),
    "prompt_cache": (),
}

ANSI = {
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "dim": "\033[2m",
}
RESET = "\033[0m"

TABLE_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
STATUS_RE = re.compile(r"^(\s*)status_line\s*=")
DOTTED_STATUS_RE = re.compile(r"^\s*tui\.status_line\s*=")


class ConfigError(ValueError):
    """Raised when the shared or provider configuration is invalid."""


def default_shared_config_path() -> Path:
    override = os.environ.get("AI_STATUSLINE_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path(__file__).with_name("config.json")


def default_codex_config_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "config.toml"
    return Path.home() / ".codex" / "config.toml"


def load_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"could not read shared config {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in shared config {path}: {exc}") from exc

    if not isinstance(data, dict) or data.get("version") != 1:
        raise ConfigError("shared config must be an object with version 1")

    lines = data.get("lines")
    if (
        not isinstance(lines, list)
        or not lines
        or any(not isinstance(line, list) or not line for line in lines)
    ):
        raise ConfigError("lines must be a non-empty list of non-empty lists")
    segments = [item for line in lines for item in line]
    if any(not isinstance(item, str) for item in segments):
        raise ConfigError("every segment ID must be a string")
    unknown = set(segments) - SUPPORTED_SEGMENTS
    if unknown:
        raise ConfigError(f"unsupported segment IDs: {', '.join(sorted(unknown))}")
    if len(segments) != len(set(segments)):
        raise ConfigError("segment IDs must not be repeated")

    separator = data.get("separator")
    if not isinstance(separator, str):
        raise ConfigError("separator must be a string")

    labels = data.get("labels")
    if not isinstance(labels, dict):
        raise ConfigError("labels must be an object")
    for key in SUPPORTED_SEGMENTS - UNLABELED_SEGMENTS:
        if not isinstance(labels.get(key), str) or not labels[key]:
            raise ConfigError(f"labels.{key} must be a non-empty string")

    suffix = data.get("percentage_suffix")
    if not isinstance(suffix, str):
        raise ConfigError("percentage_suffix must be a string")

    thresholds = data.get("remaining_thresholds")
    if not isinstance(thresholds, dict):
        raise ConfigError("remaining_thresholds must be an object")
    warning = _number(thresholds.get("warning_at_or_below"))
    critical = _number(thresholds.get("critical_at_or_below"))
    if warning is None or critical is None or not 0 <= critical <= warning <= 100:
        raise ConfigError(
            "remaining thresholds must satisfy 0 <= critical <= warning <= 100"
        )

    colors = data.get("colors")
    if not isinstance(colors, dict) or not isinstance(colors.get("enabled"), bool):
        raise ConfigError("colors must be an object with a boolean enabled field")
    for key in ("model", "label", "normal", "warning", "critical", "detail"):
        value = colors.get(key)
        if value not in ANSI:
            raise ConfigError(f"colors.{key} must be one of: {', '.join(ANSI)}")

    return data


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _remaining_percentage(container: Any) -> int | None:
    if not isinstance(container, dict):
        return None

    remaining = _number(container.get("remaining_percentage"))
    if remaining is None:
        used = _number(container.get("used_percentage"))
        if used is None:
            return None
        remaining = 100.0 - used

    return max(0, min(100, int(remaining + 0.5)))


def _token_count(value: Any) -> int | None:
    number = _number(value)
    if number is None or number < 0:
        return None
    return int(number)


def _format_token_count(value: int) -> str:
    if value >= 1_000_000:
        rounded = int(value / 100_000 + 0.5) / 10
        return f"{rounded:g}M"
    return f"{int(value / 1_000 + 0.5)}k"


def _context_token_segment(data: dict[str, Any], config: dict[str, Any]) -> str | None:
    context = data.get("context_window")
    if not isinstance(context, dict):
        return None
    usage = context.get("current_usage")
    if not isinstance(usage, dict):
        return None
    window_size = _token_count(context.get("context_window_size"))
    if window_size is None:
        return None

    token_fields = (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    counts = [_token_count(usage.get(field, 0)) for field in token_fields]
    if any(count is None for count in counts):
        return None
    used = sum(count for count in counts if count is not None)

    label = _styled(config["labels"]["context_tokens"], "label", config)
    return f"{label} {_format_token_count(used)}/{_format_token_count(window_size)}"


def _styled(text: str, style: str, config: dict[str, Any]) -> str:
    colors = config["colors"]
    if not colors["enabled"] or not text:
        return text
    return f"{ANSI[colors[style]]}{text}{RESET}"


def _reset_time(container: Any, now: datetime) -> str | None:
    """Local clock time of the window reset; prefixed with weekday if not today."""
    if not isinstance(container, dict):
        return None
    return _clock(container.get("resets_at"), now)


def _percentage_segment(
    segment: str, remaining: int, reset: str | None, config: dict[str, Any]
) -> str:
    thresholds = config["remaining_thresholds"]
    if remaining <= thresholds["critical_at_or_below"]:
        level = "critical"
    elif remaining <= thresholds["warning_at_or_below"]:
        level = "warning"
    else:
        level = "normal"

    label = _styled(f"{config['labels'][segment]}:", "label", config)
    value = _styled(f"{remaining}%", level, config)
    suffix = _styled(config["percentage_suffix"], "detail", config)
    rendered = f"{label} {value}{suffix}"
    if reset is not None:
        rendered += " " + _styled(reset, "detail", config)
    return rendered


def _git_branch(cwd: Any) -> str | None:
    if not isinstance(cwd, str) or not cwd:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    branch = result.stdout.strip()
    return branch if result.returncode == 0 and branch else None


def _session_tokens(transcript_path: Any) -> tuple[int, int] | None:
    """Sum input (incl. cache) and output tokens over the main conversation.

    Claude's context_window totals cover only the latest request, so session
    totals come from the transcript. Each API message is logged once per content
    block with the same usage, so messages are counted once by ID.
    """
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    usage_by_id: dict[str, dict[str, Any]] = {}
    try:
        with open(transcript_path, encoding="utf-8") as handle:
            for line in handle:
                if '"assistant"' not in line or '"usage"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant" or entry.get("isSidechain"):
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                message_id = message.get("id")
                if isinstance(usage, dict) and isinstance(message_id, str):
                    usage_by_id[message_id] = usage
    except OSError:
        return None
    if not usage_by_id:
        return None

    input_fields = (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    total_in = total_out = 0
    for usage in usage_by_id.values():
        total_in += sum(_token_count(usage.get(f, 0)) or 0 for f in input_fields)
        total_out += _token_count(usage.get("output_tokens", 0)) or 0
    return total_in, total_out


def _clock(epoch: Any, now: datetime) -> str | None:
    number = _number(epoch)
    if number is None:
        return None
    when = datetime.fromtimestamp(number, tz=now.tzinfo)
    clock = when.strftime("%-I:%M%p").lower()
    if when.date() == now.date():
        return clock
    return f"{when:%a} {clock}"


def _session_segments(
    data: dict[str, Any], config: dict[str, Any], now: datetime
) -> dict[str, str]:
    values: dict[str, str] = {}
    labels = config["labels"]
    wanted = {item for line in config["lines"] for item in line}

    workspace = data.get("workspace")
    if not isinstance(workspace, dict):
        workspace = {}
    project_dir = workspace.get("project_dir") or data.get("cwd")
    if isinstance(project_dir, str) and project_dir:
        values["project"] = _styled(Path(project_dir).name, "model", config)

    if "git_branch" in wanted:
        branch = _git_branch(workspace.get("current_dir") or data.get("cwd"))
        if branch is not None:
            values["git_branch"] = branch

    session_name = data.get("session_name")
    if isinstance(session_name, str) and session_name:
        values["session_name"] = _styled(session_name, "detail", config)

    if "session_tokens" in wanted:
        totals = _session_tokens(data.get("transcript_path"))
        if totals is not None:
            label = _styled(labels["session_tokens"], "label", config)
            total_in, total_out = totals
            values["session_tokens"] = (
                f"{label} {_format_token_count(total_in)} in"
                f" / {_format_token_count(total_out)} out"
            )

    cost = data.get("cost")
    if isinstance(cost, dict):
        usd = _number(cost.get("total_cost_usd"))
        if usd is not None:
            values["cost"] = f"${usd:.2f}"
        added = _token_count(cost.get("total_lines_added"))
        removed = _token_count(cost.get("total_lines_removed"))
        if added is not None and removed is not None:
            values["lines_changed"] = (
                _styled(f"+{added}", "normal", config)
                + " "
                + _styled(f"-{removed}", "critical", config)
            )

    cache = data.get("prompt_cache")
    if isinstance(cache, dict) and cache.get("caching_observed") is True:
        label = _styled(f"{labels['prompt_cache']}:", "label", config)
        expires = _clock(cache.get("expires_at"), now)
        if cache.get("warm") is True and expires is not None:
            state = _styled("warm", "normal", config) + " " + _styled(
                f"until {expires}", "detail", config
            )
        else:
            state = _styled("cold", "warning", config)
        values["prompt_cache"] = f"{label} {state}"

    return values


def render_claude(
    data: Any, config: dict[str, Any], now: datetime | None = None
) -> str:
    if not isinstance(data, dict):
        data = {}
    if now is None:
        now = datetime.now().astimezone()

    limits = data.get("rate_limits")
    if not isinstance(limits, dict):
        limits = {}

    values: dict[str, str] = {}

    model = data.get("model")
    if isinstance(model, dict):
        model_name = model.get("display_name") or model.get("id")
        if isinstance(model_name, str) and model_name:
            rendered_model = _styled(model_name, "model", config)
            effort = data.get("effort")
            effort_level = effort.get("level") if isinstance(effort, dict) else None
            if isinstance(effort_level, str) and effort_level:
                rendered_model += _styled(f": {effort_level}", "detail", config)
            values["model"] = rendered_model

    context_tokens = _context_token_segment(data, config)
    if context_tokens is not None:
        values["context_tokens"] = context_tokens

    percentage_sources = {
        "five_hour_remaining": limits.get("five_hour"),
        "weekly_remaining": limits.get("seven_day"),
    }
    for segment, source in percentage_sources.items():
        remaining = _remaining_percentage(source)
        if remaining is not None:
            values[segment] = _percentage_segment(
                segment, remaining, _reset_time(source, now), config
            )

    values.update(_session_segments(data, config, now))

    rendered_lines = []
    for line in config["lines"]:
        parts = [values[segment] for segment in line if segment in values]
        if parts:
            rendered_lines.append(config["separator"].join(parts))
    return "\n".join(rendered_lines)


def parse_json_stream(stream: Any) -> Any:
    try:
        return json.load(stream)
    except (json.JSONDecodeError, OSError, TypeError):
        return {}


def codex_status_line(config: dict[str, Any]) -> list[str]:
    return [
        field
        for line in config["lines"]
        for segment in line
        for field in CODEX_SEGMENTS[segment]
    ]


def _assignment_end(lines: list[str], start: int) -> int:
    """Return the exclusive end of a status_line assignment."""
    line = lines[start]
    value = line.split("=", 1)[1]
    bracket_depth = 0
    quote: str | None = None
    escaped = False

    for index in range(start, len(lines)):
        chunk = value if index == start else lines[index]
        for char in chunk:
            if escaped:
                escaped = False
                continue
            if quote:
                if char == "\\" and quote == '"':
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in ('"', "'"):
                quote = char
            elif char == "#":
                break
            elif char == "[":
                bracket_depth += 1
            elif char == "]":
                bracket_depth -= 1
                if bracket_depth < 0:
                    raise ConfigError("malformed status_line array")
        if quote is None and bracket_depth <= 0:
            return index + 1
    raise ConfigError("unterminated status_line assignment")


def update_codex_toml(text: str, fields: list[str]) -> str:
    try:
        parsed = tomllib.loads(text) if text.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"existing Codex config is invalid TOML: {exc}") from exc

    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    tui_headers: list[int] = []
    dotted_assignments: list[int] = []
    status_assignments: list[tuple[int, int]] = []
    current_table: str | None = None

    for index, line in enumerate(lines):
        bare = line.rstrip("\r\n")
        table_match = TABLE_RE.match(bare)
        if table_match:
            current_table = table_match.group(1).strip()
            if current_table == "tui":
                tui_headers.append(index)
            continue
        if DOTTED_STATUS_RE.match(bare):
            dotted_assignments.append(index)
        if current_table == "tui" and STATUS_RE.match(bare):
            status_assignments.append((index, _assignment_end(lines, index)))

    if len(tui_headers) > 1 or len(status_assignments) > 1 or dotted_assignments:
        raise ConfigError(
            "ambiguous Codex config: expected at most one [tui] status_line assignment"
        )

    assignment = f"status_line = {json.dumps(fields, separators=(', ', ': '))}{newline}"

    if status_assignments:
        start, end = status_assignments[0]
        lines[start:end] = [assignment]
    elif tui_headers:
        insert_at = tui_headers[0] + 1
        lines.insert(insert_at, assignment)
    else:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += newline
        if lines and lines[-1].strip():
            lines.append(newline)
        lines.extend([f"[tui]{newline}", assignment])

    updated = "".join(lines)
    try:
        reparsed = tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"generated Codex config is invalid TOML: {exc}") from exc
    if reparsed.get("tui", {}).get("status_line") != fields:
        raise ConfigError("generated Codex status_line did not validate")
    return updated


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        existing_mode = 0o600

    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, existing_mode)
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def sync_codex(path: Path, config: dict[str, Any]) -> bool:
    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        original = ""
    except OSError as exc:
        raise ConfigError(f"could not read Codex config {path}: {exc}") from exc

    updated = update_codex_toml(original, codex_status_line(config))
    if updated == original:
        return False
    write_atomic(path, updated)
    return True


def check_codex(path: Path, config: dict[str, Any]) -> bool:
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"could not parse Codex config {path}: {exc}") from exc
    return parsed.get("tui", {}).get("status_line") == codex_status_line(config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=default_shared_config_path(),
        help="shared JSON configuration path",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    render = subparsers.add_parser("render", help="render provider session data")
    render.add_argument("provider", choices=("claude",))

    for command in ("sync", "check"):
        provider = subparsers.add_parser(
            command, help=f"{command} a provider's native configuration"
        )
        provider.add_argument("provider", choices=("codex",))
        provider.add_argument(
            "--target",
            type=Path,
            default=default_codex_config_path(),
            help="provider configuration path",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config.expanduser())
        if args.command == "render":
            print(render_claude(parse_json_stream(sys.stdin), config), end="")
            return 0

        target = args.target.expanduser()
        if args.command == "sync":
            changed = sync_codex(target, config)
            state = "updated" if changed else "already in sync"
            print(f"Codex status line {state}: {target}")
            return 0

        in_sync = check_codex(target, config)
        print(f"Codex status line {'in sync' if in_sync else 'out of sync'}: {target}")
        return 0 if in_sync else 1
    except ConfigError as exc:
        print(f"ai-statusline: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
