"""Freeze the pre-refactor public argument surface without running services."""
import argparse
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

import ato_cli
import ato_core
from ato_cli.parser import build_parser

ROOT = Path(__file__).resolve().parents[1]


class CLISurfaceTests(unittest.TestCase):
    def surface(self):
        parser = build_parser('synthetic description')
        sub = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        return {name: {a.dest: dict(flags=a.option_strings, required=a.required, nargs=a.nargs,
            default=a.default, type=getattr(a.type, '__name__', None), choices=a.choices)
            for a in command._actions if a.dest != 'help'} for name, command in sub.choices.items()}

    def test_all_55_commands_aliases_flags_defaults_and_types_match_original(self):
        baseline = json.loads((ROOT/'tests/fixtures/cli-surface.json').read_text())
        self.assertEqual(len(baseline), 55)
        self.assertEqual(self.surface(), baseline)

    def test_every_parser_command_has_one_domain_handler(self):
        self.assertEqual(set(self.surface()), set(ato_cli._HANDLERS))
        for handler in ato_cli._HANDLERS.values():
            self.assertTrue(handler.__module__.startswith('ato_cli.'))

    def test_dispatch_preserves_exit_codes_and_safe_exception_handling(self):
        context = SimpleNamespace(__doc__='fixture', Halt=ato_core.Halt)
        with patch.object(sys, 'argv', ['fixture', 'trace', 'temporary-run']), \
                patch.dict(ato_cli._HANDLERS, trace=lambda args, core: 7):
            self.assertEqual(ato_cli.main(context), 7)
        for error in (ValueError('synthetic invalid argument'), ato_core.Halt('stopped')):
            def failed(args, core): raise error
            stream = io.StringIO()
            with patch.object(sys, 'argv', ['fixture', 'trace', 'temporary-run']), \
                    patch.dict(ato_cli._HANDLERS, trace=failed), redirect_stderr(stream):
                self.assertEqual(ato_cli.main(context), 1)
            self.assertEqual(stream.getvalue(), str(error)+'\n')
