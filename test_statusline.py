#!/usr/bin/env python3
from __future__ import annotations

import copy
from datetime import datetime, timezone, timedelta
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

import statusline


HERE = Path(__file__).resolve().parent


class RendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = statusline.load_config(HERE / "config.json")
        self.config = copy.deepcopy(self.config)
        self.config["colors"]["enabled"] = False

    # Tue 2026-08-25 14:00 in a fixed UTC-4 zone.
    TZ = timezone(timedelta(hours=-4))
    NOW = datetime(2026, 8, 25, 14, 0, tzinfo=TZ)

    def test_full_claude_payload_uses_remaining_capacity(self) -> None:
        payload = {
            "model": {"display_name": "Opus 4.6"},
            "effort": {"level": "high"},
            "context_window": {
                "context_window_size": 200_000,
                "current_usage": {
                    "input_tokens": 10_000,
                    "cache_creation_input_tokens": 2_000,
                    "cache_read_input_tokens": 38_000,
                },
            },
            "rate_limits": {
                "five_hour": {
                    "used_percentage": 80,
                    "resets_at": datetime(2026, 8, 25, 15, 5, tzinfo=self.TZ).timestamp(),
                },
                "seven_day": {
                    "used_percentage": 25,
                    "resets_at": datetime(2026, 8, 27, 9, 30, tzinfo=self.TZ).timestamp(),
                },
            },
        }
        self.assertEqual(
            statusline.render_claude(payload, self.config, now=self.NOW),
            "Opus 4.6: high | ctx 50k/200k\n5h: 20% 3:05pm | w: 75% Thu 9:30am",
        )

    def test_reset_time_omitted_when_absent_or_invalid(self) -> None:
        payload = {
            "rate_limits": {
                "five_hour": {"used_percentage": 80},
                "seven_day": {"used_percentage": 25, "resets_at": "soon"},
            },
        }
        self.assertEqual(
            statusline.render_claude(payload, self.config, now=self.NOW),
            "5h: 20% | w: 75%",
        )

    def test_reset_time_uses_local_zone(self) -> None:
        # 15:05 in UTC-4 is 19:05 UTC; rendering in UTC crosses midnight = no.
        payload = {
            "rate_limits": {
                "five_hour": {
                    "used_percentage": 80,
                    "resets_at": datetime(2026, 8, 25, 23, 5, tzinfo=self.TZ).timestamp(),
                },
            },
        }
        utc_now = self.NOW.astimezone(timezone.utc)
        self.assertEqual(
            statusline.render_claude(payload, self.config, now=utc_now),
            "5h: 20% Wed 3:05am",
        )

    def test_missing_context_usage_omits_context_segment(self) -> None:
        payload = {
            "model": {"id": "claude-test"},
            "context_window": {"context_window_size": 200_000},
        }
        self.assertEqual(
            statusline.render_claude(payload, self.config),
            "claude-test",
        )

    def test_quota_values_round_and_clamp(self) -> None:
        payload = {
            "rate_limits": {
                "five_hour": {"used_percentage": 84.5},
                "seven_day": {"remaining_percentage": 101},
            },
        }
        self.assertEqual(
            statusline.render_claude(payload, self.config),
            "5h: 16% | w: 100%",
        )

    def test_context_token_count_formats_millions(self) -> None:
        payload = {
            "context_window": {
                "context_window_size": 2_000_000,
                "current_usage": {
                    "input_tokens": 1_000,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 1_249_000,
                },
            }
        }
        self.assertEqual(
            statusline.render_claude(payload, self.config),
            "ctx 1.3M/2M",
        )

    def test_malformed_json_becomes_empty_payload(self) -> None:
        payload = statusline.parse_json_stream(io.StringIO("{not json"))
        self.assertEqual(statusline.render_claude(payload, self.config), "")

    def test_codex_mapping_follows_shared_order(self) -> None:
        self.assertEqual(
            statusline.codex_status_line(self.config),
            [
                "model-with-reasoning",
                "used-tokens",
                "context-window-size",
                "total-input-tokens",
                "total-output-tokens",
                "estimated-thread-cost",
                "project-name",
                "git-branch",
                "five-hour-limit",
                "weekly-limit",
                "thread-name",
            ],
        )

    def test_second_line_renders_session_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "myproj"
            repo.mkdir()
            subprocess.run(
                ["git", "init", "-q", "-b", "feature-x", str(repo)], check=True
            )
            transcript = repo / "t.jsonl"

            def assistant(msg_id: str, sidechain: bool = False) -> str:
                usage = {
                    "input_tokens": 1_000,
                    "cache_creation_input_tokens": 2_000,
                    "cache_read_input_tokens": 7_000,
                    "output_tokens": 500,
                }
                return json.dumps({
                    "type": "assistant",
                    "isSidechain": sidechain,
                    "message": {"id": msg_id, "usage": usage},
                })

            # msg_a is logged twice (one entry per content block); the
            # sidechain message belongs to a subagent and is excluded.
            transcript.write_text("\n".join([
                assistant("msg_a"),
                assistant("msg_a"),
                assistant("msg_b"),
                assistant("msg_side", sidechain=True),
                json.dumps({"type": "user", "message": {"content": "hi"}}),
            ]))
            payload = {
                "cwd": str(repo),
                "workspace": {"current_dir": str(repo), "project_dir": str(repo)},
                "session_name": "status work",
                "transcript_path": str(transcript),
                "cost": {
                    "total_cost_usd": 1.234,
                    "total_lines_added": 156,
                    "total_lines_removed": 23,
                },
                "prompt_cache": {
                    "caching_observed": True,
                    "warm": True,
                    "expires_at": datetime(2026, 8, 25, 15, 5, tzinfo=self.TZ).timestamp(),
                },
            }
            self.assertEqual(
                statusline.render_claude(payload, self.config, now=self.NOW),
                "tok 20k in / 1k out | $1.23 | +156 -23\n"
                "myproj | feature-x | status work | cache: warm until 3:05pm",
            )

    def test_cold_cache_and_both_lines(self) -> None:
        payload = {
            "model": {"display_name": "Opus"},
            "prompt_cache": {"caching_observed": True, "warm": False, "expires_at": None},
        }
        self.assertEqual(
            statusline.render_claude(payload, self.config, now=self.NOW),
            "Opus\ncache: cold",
        )


class CodexSyncTests(unittest.TestCase):
    FIELDS = [
        "model-with-reasoning",
        "used-tokens",
        "context-window-size",
        "five-hour-limit",
        "weekly-limit",
    ]

    def test_adds_tui_table_without_changing_existing_config(self) -> None:
        original = 'model = "gpt-test"\n'
        updated = statusline.update_codex_toml(original, self.FIELDS)
        self.assertIn('model = "gpt-test"', updated)
        self.assertEqual(
            statusline.tomllib.loads(updated)["tui"]["status_line"], self.FIELDS
        )

    def test_updates_multiline_assignment_and_preserves_other_tui_keys(self) -> None:
        original = """[tui]
animations = true
status_line = [
  "current-dir",
]
theme = "test"
"""
        updated = statusline.update_codex_toml(original, self.FIELDS)
        parsed = statusline.tomllib.loads(updated)
        self.assertTrue(parsed["tui"]["animations"])
        self.assertEqual(parsed["tui"]["theme"], "test")
        self.assertEqual(parsed["tui"]["status_line"], self.FIELDS)

    def test_refuses_dotted_assignment(self) -> None:
        with self.assertRaises(statusline.ConfigError):
            statusline.update_codex_toml(
                'tui.status_line = ["current-dir"]\n', self.FIELDS
            )

    def test_sync_is_idempotent_and_preserves_mode(self) -> None:
        config = statusline.load_config(HERE / "config.json")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config.toml"
            target.write_text('model = "gpt-test"\n', encoding="utf-8")
            target.chmod(0o640)

            self.assertTrue(statusline.sync_codex(target, config))
            first = target.read_text(encoding="utf-8")
            self.assertFalse(statusline.sync_codex(target, config))
            self.assertEqual(target.read_text(encoding="utf-8"), first)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
            self.assertTrue(statusline.check_codex(target, config))


if __name__ == "__main__":
    unittest.main()
