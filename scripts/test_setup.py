#!/usr/bin/env python3
"""Tests for scripts/setup.sh, run against a fake HOME and disposable git repos.

The shell under test is $TEST_SH (default: dash when installed, else sh). A
`sh` shim pointing at it goes first on PATH, so the hook command, the re-exec,
and the bootstrap hand-off all run on the same shell. Nothing touches the
network or the real HOME.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
TRACKED = [
    ".gitignore",
    "statusline.py",
    "config.json",
    "scripts/setup.sh",
    "hooks/claude.json",
    "hooks/codex.json",
]
REPO_URL = "https://github.com/Harrison-Blair/statuslines.git"
TEST_SH = os.environ.get("TEST_SH") or shutil.which("dash") or shutil.which("sh")
# The matcher skills/scripts/setup.sh uses for its own hook entries.
SKILLS_SYNC_RE = re.compile(r'([^"]*)/scripts/setup\.sh"?\s+--sync\s*$')

HERDR_CLAUDE = {
    "matcher": "^(startup|resume|clear|compact|fork)$",
    "hooks": [{"type": "command", "command": "bash '/x/herdr-agent-state.sh' session", "timeout": 10}],
}
SKILLS_HOOK = {
    "matcher": "startup",
    "hooks": [{"type": "command", "command": 'sh "/src/skills/scripts/setup.sh" --sync', "timeout": 30}],
}
# A skills-style entry for a clone that is gone: not ours, so never dropped.
DEAD_SKILLS_HOOK = {
    "matcher": "startup",
    "hooks": [{"type": "command", "command": 'sh "/gone/skills/scripts/setup.sh" --sync'}],
}


def git(*args: str, cwd: Path, env: dict[str, str]) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


class SetupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="statuslines-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        shim = self.tmp / "bin"
        shim.mkdir()
        (shim / "sh").symlink_to(TEST_SH)
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"CODEX_HOME", "AI_STATUSLINE_CONFIG", "XDG_CONFIG_HOME"}
            and not key.startswith(("STATUSLINES_", "GIT_"))
        }
        env.update(
            HOME=str(self.home),
            PATH=f"{shim}{os.pathsep}{env.get('PATH', '')}",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="t",
            GIT_AUTHOR_EMAIL="t@example.invalid",
            GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@example.invalid",
        )
        self.env = env

        source = self.tmp / "source"
        for name in TRACKED:
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, source / name)
        git("init", "--quiet", "-b", "main", cwd=source, env=env)
        git("add", "-A", cwd=source, env=env)
        git("commit", "--quiet", "-m", "init", cwd=source, env=env)
        self.origin = self.tmp / "origin.git"
        git("clone", "--quiet", "--bare", str(source), str(self.origin), cwd=self.tmp, env=env)
        self.source = source
        self.clone = self.make_clone(self.home / "source" / "statuslines")

    # helpers -------------------------------------------------------------

    def make_clone(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "--quiet", str(self.origin), str(path), cwd=self.tmp, env=self.env)
        return path

    def run_setup(self, *args: str, clone: Path | None = None, **extra: str):
        env = {**self.env, **extra}
        script = (clone or self.clone) / "scripts" / "setup.sh"
        return subprocess.run(
            [TEST_SH, str(script), *args], env=env, capture_output=True, text=True, cwd=self.tmp
        )

    def harnesses(self) -> None:
        (self.home / ".claude").mkdir()
        (self.home / ".codex").mkdir()

    def write_json(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def read_json(self, path: Path):
        return json.loads(path.read_text(encoding="utf-8"))

    @property
    def claude(self) -> Path:
        return self.home / ".claude" / "settings.json"

    @property
    def codex_hooks(self) -> Path:
        return self.home / ".codex" / "hooks.json"

    @property
    def codex_toml(self) -> Path:
        return self.home / ".codex" / "config.toml"

    def sync_cmd(self, clone: Path | None = None) -> str:
        return f'sh "{clone or self.clone}/scripts/setup.sh" --sync --hook=statuslines'

    def status_cmd(self, clone: Path | None = None) -> str:
        return f'python3 "{clone or self.clone}/statusline.py" render claude'

    def our_group(self, clone: Path) -> dict:
        return {"matcher": "startup", "hooks": [{"type": "command", "command": self.sync_cmd(clone)}]}

    def commands(self, path: Path) -> list[str]:
        return [
            hook["command"]
            for group in self.read_json(path)["hooks"]["SessionStart"]
            for hook in group["hooks"]
        ]

    def snapshot(self, *paths: Path):
        return {p: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in paths}

    def push_change(self, edit) -> str:
        """Commit EDIT(source_dir) to origin; return the new commit."""
        work = self.tmp / "work"
        if not work.exists():
            git("clone", "--quiet", str(self.origin), str(work), cwd=self.tmp, env=self.env)
        edit(work)
        git("commit", "--quiet", "-am", "change", cwd=work, env=self.env)
        git("push", "--quiet", "origin", "HEAD:main", cwd=work, env=self.env)
        return git("rev-parse", "HEAD", cwd=work, env=self.env)

    # tests ---------------------------------------------------------------

    def test_setup_writes_statusline_hooks_and_codex(self) -> None:
        self.harnesses()
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/hooks", result.stdout)
        settings = self.read_json(self.claude)
        self.assertEqual(settings["statusLine"], {"type": "command", "command": self.status_cmd()})
        self.assertEqual(self.commands(self.claude), [self.sync_cmd()])
        self.assertEqual(self.commands(self.codex_hooks), [self.sync_cmd()])
        self.assertTrue(self.read_json(self.codex_hooks)["hooks"]["SessionStart"][0]["hooks"][0]["async"])
        check = subprocess.run(
            ["python3", str(self.clone / "statusline.py"), "--config", str(self.clone / "config.json"),
             "check", "codex", "--target", str(self.codex_toml)],
            env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_setup_and_sync_twice_rewrite_nothing(self) -> None:
        self.harnesses()
        self.assertEqual(self.run_setup().returncode, 0)
        files = (self.claude, self.codex_hooks, self.codex_toml)
        before = self.snapshot(*files)
        for args in ((), ("--sync",), ("--sync", "--hook=statuslines")):
            result = self.run_setup(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(self.snapshot(*files), before, args)
        self.assertEqual(self.run_setup("--sync").stdout, "")

    def test_absent_harness_dirs_are_skipped(self) -> None:
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / ".claude").exists())
        self.assertFalse((self.home / ".codex").exists())
        (self.home / ".codex").mkdir()
        self.assertEqual(self.run_setup("--sync").returncode, 0)
        self.assertFalse((self.home / ".claude").exists())
        self.assertTrue(self.codex_toml.exists())

    def test_legacy_wrapper_is_replaced_in_each_quoting_form(self) -> None:
        self.harnesses()
        forms = [
            f"{self.home}/.claude/statusline-command.sh",
            "/home/penguin/.claude/statusline-command.sh",
            'bash "$HOME/.claude/statusline-command.sh"',
            "sh '$HOME/.claude/statusline-command.sh'",
            "~/.claude/statusline-command.sh",
            '"${HOME}/.claude/statusline-command.sh"',
        ]
        for form in forms:
            with self.subTest(form=form):
                self.write_json(self.claude, {"statusLine": {"type": "command", "command": form, "padding": 1}})
                result = self.run_setup("--sync")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    self.read_json(self.claude)["statusLine"],
                    {"type": "command", "command": self.status_cmd(), "padding": 1},
                )

    def test_statusline_of_a_moved_clone_is_ours(self) -> None:
        self.harnesses()
        self.write_json(self.claude, {"statusLine": {"type": "command", "command": self.status_cmd(Path("/old/place"))}})
        self.assertEqual(self.run_setup("--sync").returncode, 0)
        self.assertEqual(self.read_json(self.claude)["statusLine"]["command"], self.status_cmd())

    def test_custom_statusline_kept_with_warning_and_replaced_by_force(self) -> None:
        self.harnesses()
        custom = {"type": "command", "command": "~/.claude/statusline-command.sh --extra"}
        self.write_json(self.claude, {"statusLine": custom})
        for args in ((), ("--sync",)):
            result = self.run_setup(*args)
            self.assertIn("custom statusLine", result.stderr)
            self.assertEqual(self.read_json(self.claude)["statusLine"], custom)
        self.assertEqual(self.commands(self.claude), [self.sync_cmd()])  # hook still merged
        result = self.run_setup("--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_json(self.claude)["statusLine"]["command"], self.status_cmd())

    def test_sync_force_is_rejected(self) -> None:
        self.harnesses()
        for args in (("--sync", "--force"), ("--force", "--sync")):
            result = self.run_setup(*args)
            self.assertEqual(result.returncode, 2)
            self.assertIn("--force", result.stderr)
        self.assertFalse(self.claude.exists())
        self.assertEqual(self.run_setup("--bogus").returncode, 2)

    def test_coexists_with_skills_and_herdr_hooks(self) -> None:
        self.harnesses()
        herdr_codex = {"hooks": [{"command": "bash '/x/herdr.sh' session", "timeout": 10, "type": "command"}]}
        claude_groups = [SKILLS_HOOK, HERDR_CLAUDE, DEAD_SKILLS_HOOK]
        codex_groups = [herdr_codex, SKILLS_HOOK]
        self.write_json(self.claude, {"model": "x", "hooks": {"SessionStart": claude_groups, "Stop": [HERDR_CLAUDE]}})
        self.write_json(self.codex_hooks, {"hooks": {"SessionStart": codex_groups}})
        self.assertEqual(self.run_setup().returncode, 0)
        settings = self.read_json(self.claude)
        self.assertEqual(settings["model"], "x")
        self.assertEqual(settings["hooks"]["Stop"], [HERDR_CLAUDE])
        self.assertEqual(settings["hooks"]["SessionStart"][:3], claude_groups)
        self.assertEqual(self.commands(self.claude)[3:], [self.sync_cmd()])
        self.assertEqual(self.read_json(self.codex_hooks)["hooks"]["SessionStart"][:2], codex_groups)
        # Our entry is invisible to the skills repo's matcher, and ours skips its.
        self.assertIsNone(SKILLS_SYNC_RE.search(self.sync_cmd()))
        before = self.snapshot(self.claude, self.codex_hooks)
        self.assertEqual(self.run_setup("--sync").returncode, 0)
        self.assertEqual(self.snapshot(self.claude, self.codex_hooks), before)

    def test_our_entry_is_rewritten_in_place(self) -> None:
        self.harnesses()
        stale = {"matcher": "old", "hooks": [{"type": "command", "command": self.sync_cmd()}]}
        self.write_json(self.claude, {"hooks": {"SessionStart": [stale, HERDR_CLAUDE, stale]}})
        self.assertEqual(self.run_setup("--sync").returncode, 0)
        groups = self.read_json(self.claude)["hooks"]["SessionStart"]
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["matcher"], "startup")
        self.assertEqual(groups[1], HERDR_CLAUDE)

    def test_dead_clone_dropped_and_other_clone_kept_with_warning(self) -> None:
        self.harnesses()
        other = self.make_clone(self.tmp / "other")
        dead = self.tmp / "gone" / "statuslines"
        self.write_json(self.claude, {"hooks": {"SessionStart": [self.our_group(dead), HERDR_CLAUDE, self.our_group(other)]}})
        result = self.run_setup("--sync")
        self.assertEqual(result.returncode, 0)
        self.assertIn(f"another clone: {other}", result.stderr)
        self.assertEqual(self.commands(self.claude)[1:], [self.sync_cmd(other), self.sync_cmd()])
        self.assertEqual(self.read_json(self.claude)["hooks"]["SessionStart"][0], HERDR_CLAUDE)

    def test_malformed_config_fails_safely(self) -> None:
        self.harnesses()
        bad = {self.claude: "{not json", self.codex_hooks: "[1, 2", self.codex_toml: "[tui\nstatus_line = ["}
        for path, text in bad.items():
            path.write_text(text, encoding="utf-8")
        result = self.run_setup()
        self.assertEqual(result.returncode, 1)
        self.assertIn("not valid JSON", result.stderr)
        self.assertIn("invalid TOML", result.stderr)
        self.assertEqual(self.run_setup("--sync").returncode, 0)
        for path, text in bad.items():
            self.assertEqual(path.read_text(encoding="utf-8"), text)
        self.assertEqual(sorted(p.name for p in (self.home / ".codex").iterdir()), ["config.toml", "hooks.json"])

    def test_file_modes_are_preserved(self) -> None:
        self.harnesses()
        self.write_json(self.claude, {})
        self.write_json(self.codex_hooks, {})
        self.codex_toml.write_text('model = "x"\n', encoding="utf-8")
        modes = {self.claude: 0o640, self.codex_hooks: 0o604, self.codex_toml: 0o640}
        for path, mode in modes.items():
            path.chmod(mode)
        self.assertEqual(self.run_setup().returncode, 0)
        for path, mode in modes.items():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode, path)
        self.assertIn('model = "x"', self.codex_toml.read_text(encoding="utf-8"))

    def test_clone_path_with_spaces(self) -> None:
        self.harnesses()
        clone = self.make_clone(self.home / "my source" / "status lines")
        result = self.run_setup(clone=clone)
        self.assertEqual(result.returncode, 0, result.stderr)
        before = self.snapshot(self.claude, self.codex_hooks, self.codex_toml)
        hook = subprocess.run(["sh", "-c", self.commands(self.claude)[0]], env=self.env, capture_output=True, text=True)
        self.assertEqual((hook.returncode, hook.stdout, hook.stderr), (0, "", ""))
        self.assertEqual(self.snapshot(self.claude, self.codex_hooks, self.codex_toml), before)
        line = subprocess.run(
            ["sh", "-c", self.read_json(self.claude)["statusLine"]["command"]],
            env=self.env, input='{"model": {"display_name": "Opus"}}', capture_output=True, text=True,
        )
        self.assertEqual(line.returncode, 0, line.stderr)
        self.assertIn("Opus", line.stdout)

    def test_pull_failure_does_not_fail_sync(self) -> None:
        self.harnesses()
        shutil.rmtree(self.origin)
        result = self.run_setup("--sync")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_json(self.claude)["statusLine"]["command"], self.status_cmd())

    def test_pull_that_moves_head_reexecs_once(self) -> None:
        self.harnesses()
        log = self.home / "reexec.log"

        def add_probe(work: Path) -> None:
            script = work / "scripts" / "setup.sh"
            text = script.read_text(encoding="utf-8")
            probe = 'set -u\necho "run reexec=${STATUSLINES_REEXEC:-} args=$*" >> "$HOME/reexec.log"\n'
            script.write_text(text.replace("set -u\n", probe, 1), encoding="utf-8")

        new_head = self.push_change(add_probe)
        custom = {"type": "command", "command": "my-line"}
        self.write_json(self.claude, {"statusLine": custom})
        result = self.run_setup("--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.clone, env=self.env), new_head)
        self.assertEqual(log.read_text(encoding="utf-8"), "run reexec=1 args=--force\n")
        # --force survived the hand-off to the new copy.
        self.assertEqual(self.read_json(self.claude)["statusLine"]["command"], self.status_cmd())

    def test_bootstrap_from_pipe(self) -> None:
        self.harnesses()
        target = self.home / "src" / "statuslines"
        env = {
            **self.env,
            "STATUSLINES_HOME": str(target),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{self.origin}.insteadOf",
            "GIT_CONFIG_VALUE_0": REPO_URL,
        }
        script = (ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")
        result = subprocess.run([TEST_SH], input=script, env=env, capture_output=True, text=True, cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"cloning {REPO_URL} into {target}", result.stdout)
        self.assertEqual(self.commands(self.claude), [self.sync_cmd(target)])

        # An existing directory that is not a clone is refused and left alone.
        blocker = self.home / "blocker"
        (blocker / "scripts").mkdir(parents=True)
        (blocker / "keep.txt").write_text("mine", encoding="utf-8")
        env["STATUSLINES_HOME"] = str(blocker)
        result = subprocess.run([TEST_SH], input=script, env=env, capture_output=True, text=True, cwd=self.tmp)
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not a clone", result.stderr)
        self.assertEqual(sorted(p.name for p in blocker.iterdir()), ["keep.txt", "scripts"])

    def test_old_python_is_refused(self) -> None:
        self.harnesses()
        fake = self.tmp / "oldpy"
        fake.mkdir()
        (fake / "python3").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        (fake / "python3").chmod(0o755)
        path = f"{fake}{os.pathsep}{self.env['PATH']}"
        result = self.run_setup(PATH=path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("3.11", result.stderr)
        self.assertEqual(self.run_setup("--sync", PATH=path).returncode, 0)
        self.assertFalse(self.claude.exists())


if __name__ == "__main__":
    print(f"shell under test: {TEST_SH}", file=sys.stderr)
    unittest.main()
