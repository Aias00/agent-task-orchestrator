"""Keep the public README aligned with shipped sources and local links valid."""
from pathlib import Path
import re
import subprocess
import unittest
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


class DocumentationTests(unittest.TestCase):
    def test_readme_uses_current_modules_commands_and_guides(self):
        readme = (ROOT/'README.md').read_text()
        for obsolete in ('message_adapters.py', 'mailbox-register', 'mailbox-pump',
                         '--origin-endpoint', 'upgrade_runtime.py'):
            self.assertNotIn(obsolete, readme)
        for command in ('ato --help', 'ato project validate', 'ato run', 'ato list',
                        'ato status', 'ato inbox', 'ato answer', 'ato accept', 'ato stop', 'ato resume'):
            self.assertIn(command, readme)
        for guide in ('runtime-upgrade', 'ato-unified-contract', 'ato-host-setup'):
            self.assertIn('(docs/'+guide+'.md)', readme)
        self.assertIn('scripts/install.py', readme)
        self.assertNotIn('ctask ', readme)

    def test_all_repository_markdown_relative_links_resolve(self):
        names = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '-z',
            '--cached', '--others', '--exclude-standard']).decode().split('\0')
        links = re.compile(r'!?\[[^\]\n]*\]\(\s*(<[^>]*>|[^\s)]+)(?:\s+[^)]*)?\)'
                           r'|^\[[^\]\n]+\]:\s*(<[^>]*>|\S+)', re.M)
        checked = 0
        for name in names:
            if not name.endswith('.md') or not (ROOT/name).is_file():
                continue
            source = ROOT/name
            for match in links.finditer(source.read_text()):
                target = (match.group(1) or match.group(2)).strip('<>')
                url = urlsplit(target)
                if url.scheme or url.netloc or not url.path:
                    continue
                checked += 1
                with self.subTest(source=name, target=target):
                    self.assertTrue((source.parent/unquote(url.path)).exists(), 'broken Markdown link')
        self.assertGreater(checked, 0)
