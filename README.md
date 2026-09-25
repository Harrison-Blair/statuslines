# statuslines

A portable statusline for AI coding agents: one shared config, rendered by
Claude Code and Codex, plus the session-start hook that keeps it current on
every machine. One clone per machine is the source of truth. Pi support is
planned (it would live in a `pi/` directory) but nothing is built for it yet.

## Layout

```
statusline.py        renderer (`render claude`) and Codex adapter (`sync codex`, `check codex`)
test_statusline.py   renderer and Codex adapter tests
config.json          the shared config: segments, labels, colors, thresholds
hooks/claude.json    SessionStart entry merged into ~/.claude/settings.json
hooks/codex.json     SessionStart entry merged into ~/.codex/hooks.json
scripts/setup.sh     one-time setup, and the --sync command the hooks run
scripts/test_setup.py  installer tests
```

Requires `git` and Python 3.11 or newer (`python3`, for `tomllib`). Linux and
macOS are supported; Windows Git Bash is best-effort.

## Setup and updating

One line:

```sh
curl -fsSL https://raw.githubusercontent.com/Harrison-Blair/statuslines/main/scripts/setup.sh | sh
```

Run with no clone around it, the script clones the repo to
`~/source/statuslines` (set `STATUSLINES_HOME` for another path) and hands the
rest of the run to that copy, so the paths it installs keep working. It refuses
to touch a directory that is already there and is not this repo.

To choose the location yourself, clone anywhere and run setup from it:

```sh
git clone https://github.com/Harrison-Blair/statuslines.git ~/source/statuslines
~/source/statuslines/scripts/setup.sh
```

That run installs one `SessionStart` hook per harness, and the hook runs
`scripts/setup.sh --sync` from this clone at each session start. Sync does
everything setup does, so a pushed change to `config.json`, `statusline.py`, or
a hook template arrives on its own; setup is only needed again after moving or
re-cloning the repo. To sync by hand:

```sh
~/source/statuslines/scripts/setup.sh --sync
```

Both modes do the following, skipping any harness whose config directory
(`~/.claude`, `~/.codex`) is absent:

- Fast-forwards the clone. If the pull moved `HEAD`, the script re-executes
  itself once so the rest of the run comes from the new copy. A failed pull
  still applies the current checkout and never fails a session.
- Sets Claude's `statusLine` in `~/.claude/settings.json` to
  `python3 "<clone>/statusline.py" render claude`, run straight from the clone.
- Runs `statusline.py sync codex`, which writes `[tui].status_line` in
  `~/.codex/config.toml` and leaves the rest of that file alone.
- Merges one `SessionStart` hook into `~/.claude/settings.json` and
  `~/.codex/hooks.json`, next to whatever hooks are already there.

Setup reports each step and exits non-zero if one failed; sync is quiet on
stdout, prints only warnings, and always exits 0 so it never fails a session.
A file is rewritten only when its content changes, atomically and with its
mode kept, so a steady-state sync writes nothing. A file that is not valid
JSON or TOML is left untouched with an error.

Codex does not run new hooks until you trust them: open Codex and run `/hooks`
to review the entry once after setup.

### Claude's existing statusLine

Setup and sync replace Claude's `statusLine` only when it is missing, already
ours (`python3 "<any path>/statusline.py" render claude`, so a moved clone is
picked up), or the legacy `~/.claude/statusline-command.sh` wrapper in any
quoting form. Any other statusLine is custom: it is left alone with a warning.
To replace it anyway:

```sh
~/source/statuslines/scripts/setup.sh --force
```

`--force` is for setup only; `--sync --force` is rejected, so a hook never
overwrites a custom statusLine. Codex's `[tui].status_line` is always managed.

### Ownership and the skills repo

The hook this repo installs runs exactly:

```sh
sh "<clone>/scripts/setup.sh" --sync --hook=statuslines
```

The trailing `--hook=statuslines` marks the entry as ours. Only marked entries
are managed: the one for this clone is rewritten in place when the template
changes, one for a clone that no longer exists on disk is dropped, and one for
a second clone that does exist is kept with a warning. Every other hook is left
alone, including the [skills](https://github.com/Harrison-Blair/skills) repo's
own `scripts/setup.sh --sync` entry, whose matcher in turn does not claim ours.

There is no lock between runs. Steady-state syncs write nothing, but two
installers doing a first install at the same instant could race on the same
settings file; if that happens, run setup again.

## Configuration

`config.json` defines field order and presentation for both harnesses. Claude
Code renders it directly because Claude sends session JSON to a command. Codex
exposes fixed native footer fields, so `statusline.py` translates the same
segment IDs into Codex's `[tui].status_line` setting.

With the committed `config.json`, Claude renders:

```text
Opus: high | ctx 24k/200k | tok 10k in / 1k out | $1.23 | +156 -23
statuslines | main | 5h: 20% 3:05pm | w: 75% Thu 9:30am | status work | cache: warm until 3:05pm
```

| Key | Meaning |
| --- | --- |
| `version` | Always `1`. |
| `lines` | Segment IDs per status-line row. |
| `separator` | Text between segments. |
| `labels` | Required prefix for each labelled segment: `context_tokens`, `five_hour_remaining`, `weekly_remaining`, `session_tokens`, `prompt_cache`. |
| `percentage_suffix` | Appended to quota percentages. |
| `colors` | `enabled`, plus ANSI color names for `model`, `label`, `normal`, `warning`, `critical`, `detail`. |
| `remaining_thresholds` | `warning_at_or_below` and `critical_at_or_below`, in percent remaining. |

Segments: `model`, `context_tokens`, `five_hour_remaining`, `weekly_remaining`,
`project`, `git_branch`, `session_name`, `session_tokens`, `cost`,
`lines_changed`, `prompt_cache`.

Codex has a single footer row, so its mapping flattens `lines`; segments
without a native Codex field (`lines_changed`, `prompt_cache`) are skipped
there. Claude's session token totals are summed from the transcript (main
conversation only, cache reads included) because its `context_window` totals
cover only the latest request.

Quota percentages mean capacity remaining, followed by the local clock time the
window resets (`3:05pm`, or `Thu 3:05pm` when not today). Claude renders
context as a compact `24k/200k` value. Codex maps that segment to its native
`used-tokens` and `context-window-size` fields. Provider fields that are
unavailable are omitted.

## Commands

```sh
# What Claude's statusLine runs; reads Claude session JSON on stdin.
python3 statusline.py render claude

# Apply or verify the native Codex mapping without changing other Codex config.
python3 statusline.py sync codex [--target ~/.codex/config.toml]
python3 statusline.py check codex [--target ~/.codex/config.toml]
```

`--config PATH` (before the command) selects another shared config; setup
always passes this clone's `config.json` and `--target` explicitly. Otherwise
`AI_STATUSLINE_CONFIG` and `CODEX_HOME` are honoured. `sync codex` writes
atomically, preserves the existing file mode, and is idempotent. It refuses
ambiguous TOML rather than overwriting multiple or dotted
`tui.status_line` definitions.

## Publishing edits

Commit and push from the clone. Other machines pick the change up at their next
session start.

## Testing

```sh
python3 test_statusline.py
python3 scripts/test_setup.py
TEST_SH=bash python3 scripts/test_setup.py
shellcheck -s sh scripts/setup.sh
```

The installer tests run `setup.sh` under `$TEST_SH` (default `dash` when
installed, else `sh`) against a fake `HOME` and disposable local git repos;
they never use the network or change your installed config. They cover setup
and sync idempotence, skipped harnesses, legacy and custom statusLines,
`--force`, hook coexistence with the skills and other hooks, dead and second
clones, malformed config, file modes, paths with spaces, pull failure, the
post-pull re-exec, bootstrap, and the Python version check.
