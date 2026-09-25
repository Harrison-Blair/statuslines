#!/bin/sh
# Point Claude Code and Codex at this clone's statusline, or (--sync) pull and
# reconcile.
#
#   scripts/setup.sh           one-time setup after clone
#   scripts/setup.sh --force   setup, also replacing a custom Claude statusLine
#   scripts/setup.sh --sync    what the session-start hooks run: pull, then
#                              re-apply the statusline and hook entries
#
# With no clone around it -- piped from curl, or a lone downloaded copy -- the
# script first clones the repo to ${STATUSLINES_HOME:-~/source/statuslines} and
# hands the run over to that copy, so the paths it installs keep working.
#
# Every statement lives in a function and the file ends with one `main "$@"`, so
# the shell parses the whole script before a pull can replace it on disk.
set -u

REPO_URL="https://github.com/Harrison-Blair/statuslines.git"
# Trailing token of the hook command. It marks the entry as ours, and keeps the
# skills repo's `setup.sh --sync` matcher from claiming it.
HOOK_MARK="--hook=statuslines"
# Directory holding this script's clone, empty when the script has no file to
# sit next to: piped into a shell, $0 is the shell's own name.
REPO=""
[ -f "$0" ] && REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
MODE=setup
FORCE=0
STATUS=0

warn() { echo "warn: $*" >&2; }

usage() {
  echo "usage: $0 [--force | --sync]" >&2
  exit 2
}

# True when DIR is a clone of this repo: the layout is there, and git does not
# report a different work-tree root (a copied setup.sh inside another project).
looks_like_clone() {
  [ -n "$1" ] && [ -d "$1" ] || return 1
  [ -f "$1/scripts/setup.sh" ] && [ -f "$1/statusline.py" ] && [ -d "$1/hooks" ] || return 1
  lc_top="$(git -C "$1" rev-parse --show-toplevel 2>/dev/null)" || return 0
  [ "$(cd "$lc_top" && pwd -P)" = "$(cd "$1" && pwd -P)" ]
}

# The script arrived on its own: get a real clone and hand the run to its copy.
bootstrap() {
  if [ "${STATUSLINES_BOOTSTRAPPED:-}" = 1 ]; then
    echo "error: bootstrap did not produce a usable clone" >&2
    exit 1
  fi
  bs_clone="${STATUSLINES_HOME:-$HOME/source/statuslines}"
  if looks_like_clone "$bs_clone"; then
    echo "using clone: $bs_clone"
  elif [ -e "$bs_clone" ] || [ -L "$bs_clone" ]; then
    echo "error: $bs_clone exists and is not a clone of $REPO_URL; move it or set" >&2
    echo "       STATUSLINES_HOME to another path" >&2
    exit 1
  else
    echo "cloning $REPO_URL into $bs_clone"
    mkdir -p "$(dirname "$bs_clone")" || exit 1
    git clone --quiet "$REPO_URL" "$bs_clone" || { echo "error: git clone failed" >&2; exit 1; }
  fi
  export STATUSLINES_BOOTSTRAPPED=1
  exec sh "$bs_clone/scripts/setup.sh" "$@"
}

# statusline.py needs tomllib. A sync must never fail a session, so it only warns.
check_python() {
  if python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
    return 0
  fi
  if [ "$MODE" = --sync ]; then
    warn "python3 >= 3.11 not found; statusline not synced"
    exit 0
  fi
  echo "error: python3 >= 3.11 is required" >&2
  exit 1
}

# Fast-forward the clone, then re-run the new copy if HEAD moved: the shell
# reads a script as it runs it, so continuing could mix old and new code.
# STATUSLINES_REEXEC stops the new copy from pulling again.
pull() {
  [ "${STATUSLINES_REEXEC:-}" = 1 ] && return 0
  pl_before="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)" || return 0
  GIT_TERMINAL_PROMPT=0 git -C "$REPO" pull --ff-only --quiet >/dev/null 2>&1 || return 0
  pl_after="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)" || return 0
  [ "$pl_before" = "$pl_after" ] && return 0
  export STATUSLINES_REEXEC=1
  exec sh "$REPO/scripts/setup.sh" "$@"
}

# apply_json FILE TEMPLATE STATUSLINE: merge TEMPLATE's hook entries into FILE
# and, when STATUSLINE is 1, set Claude's statusLine. Hook entries whose
# command ends in `scripts/setup.sh" --sync --hook=statuslines` are ours: this
# clone's is rewritten in place, one for a clone that is gone is dropped, one
# for another clone that exists is kept with a warning. Everything else is left
# as it is. FILE is replaced atomically, and only when its content changes.
apply_json() {
  SYNC_CMD="sh \"$REPO/scripts/setup.sh\" --sync $HOOK_MARK" \
    STATUS_CMD="python3 \"$REPO/statusline.py\" render claude" \
    SET_STATUSLINE="$3" FORCE="$FORCE" \
    python3 - "$1" "$2" <<'PY'
import json, os, re, sys, tempfile

path, template_path = sys.argv[1], sys.argv[2]
sync_cmd, status_cmd = os.environ["SYNC_CMD"], os.environ["STATUS_CMD"]
HOOK_RE = re.compile(r'^sh "(.+)/scripts/setup\.sh" --sync --hook=statuslines$')
OURS_RE = re.compile(r'^python3 "(.+)/statusline\.py" render claude$')
LEGACY_RE = re.compile(
    r"""^(?:(?:ba)?sh\s+)?(["']?)(?:~|\$HOME|\$\{HOME\}|/[^"']*)?"""
    r"""/\.claude/statusline-command\.sh\1\s*$"""
)


def fill(node):
    if isinstance(node, dict):
        return {key: fill(value) for key, value in node.items()}
    if isinstance(node, list):
        return [fill(value) for value in node]
    return sync_cmd if node == "__SYNC_CMD__" else node


def group_clone(group):
    """Clone path a hook group belongs to, or None when it is not ours."""
    entries = group.get("hooks") if isinstance(group, dict) else None
    if not isinstance(entries, list) or not entries:
        return None
    clones = set()
    for entry in entries:
        command = entry.get("command") if isinstance(entry, dict) else None
        match = HOOK_RE.match(command) if isinstance(command, str) else None
        if not match:
            return None
        clones.add(match.group(1))
    return clones.pop() if len(clones) == 1 else None


with open(template_path, encoding="utf-8") as handle:
    template = fill(json.load(handle))
self_clone = HOOK_RE.match(sync_cmd).group(1)

try:
    with open(path, encoding="utf-8") as handle:
        original = handle.read()
except FileNotFoundError:
    original = None
try:
    data = json.loads(original) if original and original.strip() else {}
except ValueError as exc:
    sys.exit("error: %s is not valid JSON; left alone: %s" % (path, exc))
if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
    sys.exit("error: %s has no usable hooks object; left alone" % path)
before = json.dumps(data, sort_keys=True)

hooks = data.setdefault("hooks", {})
for event, groups in template["hooks"].items():
    existing = hooks.setdefault(event, [])
    if not isinstance(existing, list):
        print("warn: %s hooks.%s is not a list; left alone" % (path, event), file=sys.stderr)
        continue
    mine, drop = [], []
    for index, group in enumerate(existing):
        clone = group_clone(group)
        if clone is None:
            continue
        if clone == self_clone:
            mine.append(index)
        elif not os.path.isdir(clone):
            drop.append(index)
        else:
            print("warn: %s keeps a %s hook for another clone: %s" % (path, event, clone),
                  file=sys.stderr)
    for index, group in zip(mine, groups):
        existing[index] = group  # in place, so the order around it survives
    drop.extend(mine[len(groups):])
    for index in sorted(drop, reverse=True):
        del existing[index]
    existing.extend(groups[len(mine):])

if os.environ["SET_STATUSLINE"] == "1":
    current = data.get("statusLine")
    command = current.get("command") if isinstance(current, dict) else None
    replaceable = (
        current is None
        or (isinstance(command, str) and (OURS_RE.match(command) or LEGACY_RE.match(command)))
    )
    if replaceable or os.environ["FORCE"] == "1":
        line = dict(current) if isinstance(current, dict) else {}
        line.update(type="command", command=status_cmd)
        data["statusLine"] = line
    else:
        print("warn: %s has a custom statusLine; left alone (setup --force replaces it)"
              % path, file=sys.stderr)

if original is not None and json.dumps(data, sort_keys=True) == before:
    print("ok: %s already up to date" % path)
    sys.exit(0)
directory = os.path.dirname(path) or "."
fd, temp = tempfile.mkstemp(dir=directory, prefix=".statuslines-", suffix=".json")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    if original is not None:
        os.chmod(temp, os.stat(path).st_mode & 0o7777)
    os.replace(temp, path)
except BaseException:
    if os.path.exists(temp):
        os.unlink(temp)
    raise
print("updated: %s" % path)
PY
}

reconcile() {
  echo "repo: $REPO"
  if [ -d "$HOME/.claude" ]; then
    apply_json "$HOME/.claude/settings.json" "$REPO/hooks/claude.json" 1 || STATUS=1
  else
    echo "skip: $HOME/.claude not found"
  fi
  if [ -d "$HOME/.codex" ]; then
    python3 "$REPO/statusline.py" --config "$REPO/config.json" \
      sync codex --target "$HOME/.codex/config.toml" || STATUS=1
    apply_json "$HOME/.codex/hooks.json" "$REPO/hooks/codex.json" 0 || STATUS=1
  else
    echo "skip: $HOME/.codex not found"
  fi
}

next_steps() {
  cat <<MSG

Next steps:
  - Codex: open Codex and run /hooks to review and trust the new SessionStart hook.
MSG
}

main() {
  for arg in "$@"; do
    case "$arg" in
      --sync) MODE=--sync ;;
      --force) FORCE=1 ;;
      "$HOOK_MARK") ;;
      *) usage ;;
    esac
  done
  if [ "$MODE" = --sync ] && [ "$FORCE" = 1 ]; then
    echo "error: --force is for setup only; a sync never replaces a custom statusLine" >&2
    exit 2
  fi

  looks_like_clone "$REPO" || bootstrap "$@"
  check_python
  pull "$@"
  if [ "$MODE" = --sync ]; then
    # A sync runs at session start: quiet on stdout, and never fails a session.
    reconcile >/dev/null
    return 0
  fi
  reconcile
  [ "$STATUS" = 0 ] || { echo "error: setup did not finish cleanly; see above" >&2; return 1; }
  next_steps
}

main "$@"
