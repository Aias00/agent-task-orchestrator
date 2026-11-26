"""Freeze the public v1 surface and the deliberate removal of legacy names."""
import argparse
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import unittest

import ato_cli
from ato_task.ato_task_store import TaskError


BASELINE = json.loads((Path(__file__).parent/'fixtures/cli-surface.json').read_text())


def subcommands(parser):
    return next(action.choices for action in parser._actions if isinstance(action, argparse._SubParsersAction))


class CLISurfaceTests(unittest.TestCase):
    def test_exact_daily_and_management_surfaces(self):
        daily = subcommands(ato_cli.build_parser())
        groups = ("project", "agent", "system")
        self.assertEqual(list(daily), BASELINE["daily"] + list(groups))
        for group in groups:
            self.assertEqual(list(subcommands(daily[group])), BASELINE[group])

    def test_retired_commands_aliases_and_private_verbs_are_rejected(self):
        for key in ("removed_ctask", "removed_hub", "removed_runtime"):
            for command in BASELINE[key]:
                with self.subTest(command=command), self.assertRaises(TaskError) as caught:
                    ato_cli.build_parser().parse_args([command])
                self.assertEqual(caught.exception.code, "USAGE")

    def test_argument_errors_are_json_and_redacted(self):
        task = "task-" + "0"*32
        for argv in (["status", "/private/retired-workflow"], ["status", task, "--json"],
                     ["run", "--rep", "sensitive", "--spec", "x"],
                     ["run", "--repo", "x", "--spec", "y", "--foreground", "--foreground"],
                     ["answer", "request-"+"1"*32, "--answer", "sensitive", "--answer", "x"],
                     ["run", "--repo", "x", "--repo", "y", "--spec", "x"],
                     ["stop", task, "--actor-file", "sensitive"],
                     ["resume", task, "--retry-failed"], ["run", "--plan", "sensitive"]):
            with self.subTest(argv=argv), redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                self.assertEqual(ato_cli.main(argv), 2)
                self.assertIn(json.loads(out.getvalue())["error"]["code"], {"USAGE", "INVALID_ID"})
                self.assertNotIn("sensitive", out.getvalue())
                self.assertEqual(err.getvalue(), "")
