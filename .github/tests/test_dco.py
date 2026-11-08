"""Exercise the DCO job's actual shell against GitHub commit fixtures, without credentials."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest

WORKFLOW = Path(__file__).resolve().parents[1] / 'workflows/gate.yml'
WORKERS = '349565860+fieldmouseworks-workers[bot]@users.noreply.github.com'
DEPENDABOT = '49699333+dependabot[bot]@users.noreply.github.com'


class Dco(unittest.TestCase):
    def check(self, opener, author, committer, verified, expected, signed=False, mixed=False):
        source = WORKFLOW.read_text().split('  dco:\n', 1)[1].split('\n  schema:', 1)[0]
        script = textwrap.dedent(source.rsplit('        run: |\n', 1)[1])
        script = re.sub(r'\$\{\{.*?\}\}', 'fixture', script)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'commit.json').write_text(json.dumps({'commit': {
                'author': {'email': author}, 'committer': {'email': committer},
                'verification': {'verified': verified}}}))
            # Git supplies one signed/unsigned commit, or a bot commit followed by an unrelated unsigned commit.
            (root / 'git').write_text('#!/bin/sh\nprintf "%s\\n" "$TEST_LOG"\n')
            (root / 'gh').write_text('''#!/bin/sh
if [ "$2" = repos/fixture/commits/other ]; then echo false; exit; fi
jq -r "$4" "$TEST_COMMIT"
''')
            for name in ('git', 'gh'):
                (root / name).chmod(0o755)
            log = 'bot Signed-off-by: Fixture' if signed else 'bot'
            if mixed:
                log += '\nother'
            result = subprocess.run(['bash', '-e', '-c', script], capture_output=True, text=True,
                                    env={**os.environ, 'PATH': tmp + ':' + os.environ['PATH'],
                                         'PR_AUTHOR': opener, 'DEPENDABOT_PR': str(opener == 'dependabot[bot]').lower(), 'REPO': 'fixture', 'TEST_LOG': log,
                                         'TEST_COMMIT': str(root / 'commit.json')})
            self.assertEqual(result.returncode == 0, expected, result.stdout + result.stderr)

    def test_verified_workers(self):
        self.check('fieldmouseworks-workers[bot]', WORKERS, WORKERS, True, True)

    def test_workers_rejections(self):
        for opener, author, committer, verified in [
            ('someone', WORKERS, WORKERS, True),
            ('fieldmouseworks-workers[bot]', 'someone', WORKERS, True),
            ('fieldmouseworks-workers[bot]', WORKERS, 'someone', True),
            ('fieldmouseworks-workers[bot]', WORKERS, 'noreply@github.com', True),
            ('fieldmouseworks-workers[bot]', WORKERS, WORKERS, False),
        ]:
            with self.subTest(opener=opener, author=author, committer=committer, verified=verified):
                self.check(opener, author, committer, verified, False)

    def test_mixed_workers_pr(self):
        self.check('fieldmouseworks-workers[bot]', WORKERS, WORKERS, True, False, mixed=True)

    def test_dependabot_preserved(self):
        for committer in (DEPENDABOT, 'noreply@github.com'):
            with self.subTest(committer=committer):
                self.check('dependabot[bot]', DEPENDABOT, committer, True, True)
        self.check('dependabot[bot]', DEPENDABOT, DEPENDABOT, False, False)
        self.check('someone', DEPENDABOT, DEPENDABOT, True, False)

    def test_signed_human(self):
        self.check('someone', 'someone', 'someone', False, True, signed=True)

    def test_unsigned_human(self):
        self.check('someone', 'someone', 'someone', False, False)


if __name__ == '__main__':
    unittest.main()
