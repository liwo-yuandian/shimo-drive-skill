"""CLI behavioral tests: all commands are mocked; no credentials or remote writes."""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/shimo-drive/scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('shimo_document', SCRIPTS / 'document.py')
doc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doc)
ORIGIN = 'https://tenant.example'
URL = ORIGIN + '/docs/doc123'
CONTENT = '# Heading\n\nStatus: draft\n\n![image](asset.png)\n'
EDITED = CONTENT.replace('Status: draft', 'Status: approved')
PATCH = [{'old': 'Status: draft', 'new': 'Status: approved'}]


class FakeCLI:
    def __init__(self, heads=(1, 1, 1, 2, 2), readback=EDITED, push_error=None):
        self.heads = iter(heads)
        self.readback = readback
        self.push_error = push_error
        self.calls = []
        self.pull_count = 0
        self.pushed = []

    def check(self, origin, guid):
        self.calls.append(('check', origin, guid))
        return {'origin': origin, 'guid': guid, 'auth': 'ready', 'head': next(self.heads)}

    def run(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == 'connect':
            return 'connected'
        if args[0] == 'pull':
            self.pull_count += 1
            output = Path(args[2])
            text = CONTENT if self.pull_count == 1 else self.readback
            if text is not None:
                output.write_text(text, encoding='utf-8')
            return {'head': 1 if self.pull_count == 1 else 2}
        if args[0] == 'push':
            self.pushed.append(Path(args[1]).read_text(encoding='utf-8'))
            if self.push_error:
                raise self.push_error
            return {'head': 2}
        raise AssertionError('Unexpected fake command')


class DocumentTests(unittest.TestCase):
    def edit(self, cli, sha=None, patch=PATCH):
        return doc.operate(cli, ORIGIN, URL, 'edit', patch, sha or doc.digest(CONTENT))

    def test_cross_origin_never_connects(self):
        cli = FakeCLI()
        with self.assertRaises(doc.DriveError) as caught:
            doc.operate(cli, ORIGIN, 'https://other.example/docs/doc123', 'read')
        self.assertEqual(caught.exception.code, 'origin_mismatch')
        self.assertFalse(cli.calls)

    def test_unsafe_target_urls_rejected(self):
        for url in (ORIGIN + '/sheets/doc123', URL + '?token=secret', URL + '#section',
                    'http://tenant.example/docs/doc123', ORIGIN + '/docs/../x',
                    'https://user:secret@tenant.example/docs/doc123'):
            with self.subTest(url=url), self.assertRaises(doc.DriveError):
                doc.target(ORIGIN, url)
        self.assertEqual(doc.target(ORIGIN + '/desktop', URL), (ORIGIN, 'doc123'))

    def test_cli_status_target_and_auth_guard(self):
        cli = object.__new__(doc.CLI)
        for state in ({'origin': ORIGIN, 'guid': 'other', 'auth': 'ready', 'head': 1},
                      {'origin': 'https://other.example', 'guid': 'doc123', 'auth': 'ready', 'head': 1},
                      {'origin': ORIGIN, 'guid': 'doc123', 'auth': 'missing', 'head': 1},
                      {'origin': ORIGIN, 'guid': 'doc123', 'auth': 'ready'}):
            with self.subTest(state=state), mock.patch.object(cli, 'run', return_value=state), self.assertRaises(doc.DriveError):
                cli.check(ORIGIN, 'doc123')

    def test_ambiguous_and_missing_text_never_push(self):
        for old in ('\n', 'not present'):
            cli = FakeCLI(heads=(1, 1))
            with self.subTest(old=old), self.assertRaises(doc.DriveError) as caught:
                self.edit(cli, patch=[{'old': old, 'new': 'replacement'}])
            self.assertEqual(caught.exception.code, 'ambiguous_patch')
            self.assertFalse(cli.pushed)

    def test_stale_hash_never_pushes(self):
        cli = FakeCLI(heads=(1, 1))
        with self.assertRaises(doc.DriveError) as caught:
            self.edit(cli, sha='0' * 64)
        self.assertEqual(caught.exception.code, 'document_changed')
        self.assertFalse(cli.pushed)

    def test_revision_change_during_read_or_before_push_never_pushes(self):
        for heads in ((1, 2), (1, 1, 2)):
            cli = FakeCLI(heads=heads)
            with self.subTest(heads=heads), self.assertRaises(doc.DriveError) as caught:
                self.edit(cli)
            self.assertEqual(caught.exception.code, 'document_changed')
            self.assertFalse(cli.pushed)

    def test_success_pushes_once_and_verifies_entire_readback(self):
        cli = FakeCLI()
        result = self.edit(cli)
        self.assertEqual(cli.pushed, [EDITED])
        self.assertEqual(cli.pull_count, 2)
        self.assertTrue(result['verified'])
        self.assertEqual(result['head'], 2)
        self.assertEqual(result['sha256'], doc.digest(EDITED))
        self.assertIn('![image](asset.png)', cli.pushed[0])

    def test_readback_mismatch_never_replays(self):
        cli = FakeCLI(readback='different server content')
        with self.assertRaises(doc.DriveError) as caught:
            self.edit(cli)
        self.assertEqual(caught.exception.code, 'write_unverified')
        self.assertEqual(len(cli.pushed), 1)
        self.assertEqual(cli.pull_count, 2)

    def test_readback_revision_drift_never_replays(self):
        cli = FakeCLI(heads=(1, 1, 1, 2, 3))
        with self.assertRaises(doc.DriveError) as caught:
            self.edit(cli)
        self.assertEqual(caught.exception.code, 'write_unverified')
        self.assertEqual(len(cli.pushed), 1)

    def test_push_timeout_does_not_replay(self):
        cli = FakeCLI(push_error=doc.DriveError('cli_timeout', 'Timed out; outcome unknown.'))
        with self.assertRaises(doc.DriveError) as caught:
            self.edit(cli)
        self.assertEqual(caught.exception.code, 'cli_timeout')
        self.assertEqual(len(cli.pushed), 1)
        self.assertEqual(cli.pull_count, 1)

    def test_push_ndjson_progress_returns_final_record(self):
        cli = object.__new__(doc.CLI)
        cli.binary = '/mock/shimocli'
        records = [{'stage': 'compose_start'}, {'stage': 'compose_progress', 'progress': 50},
                   {'stage': 'compose_ok', 'head': 2}]
        proc = subprocess.CompletedProcess([], 0, stdout='\n'.join(json.dumps(item) for item in records) + '\n', stderr='')
        with mock.patch.object(doc.subprocess, 'run', return_value=proc) as run:
            result = cli.run('push', '/mock/candidate.md', '--json')
        self.assertEqual(result, records[-1])
        self.assertEqual(run.call_count, 1)

    def test_ndjson_failure_record_is_not_hidden_by_later_success(self):
        cli = object.__new__(doc.CLI)
        cli.binary = '/mock/shimocli'
        for failure in ({'ok': False}, {'error': 'private diagnostic'}):
            proc = subprocess.CompletedProcess([], 0, stdout=json.dumps(failure) + '\n' + json.dumps({'stage': 'compose_ok'}), stderr='')
            with self.subTest(failure=failure), mock.patch.object(doc.subprocess, 'run', return_value=proc), self.assertRaises(doc.DriveError) as caught:
                cli.run('push', '/mock/candidate.md', '--json')
            self.assertEqual(caught.exception.code, 'cli_response')
            self.assertNotIn('private diagnostic', str(caught.exception))

    def test_missing_readback_file_is_unverified_without_replay(self):
        cli = FakeCLI(readback=None)
        with self.assertRaises(doc.DriveError) as caught:
            self.edit(cli)
        self.assertEqual(caught.exception.code, 'write_unverified')
        self.assertEqual(len(cli.pushed), 1)
        self.assertEqual(cli.pull_count, 2)

    def test_unreadable_readback_is_unverified_without_replay(self):
        original_read = Path.read_text
        for failure in (PermissionError('private path'), UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid')):
            cli = FakeCLI()
            def read(path, *args, **kwargs):
                if path.name == 'readback.md':
                    raise failure
                return original_read(path, *args, **kwargs)
            with self.subTest(failure=type(failure).__name__), mock.patch.object(Path, 'read_text', read), self.assertRaises(doc.DriveError) as caught:
                self.edit(cli)
            self.assertEqual(caught.exception.code, 'write_unverified')
            self.assertEqual(len(cli.pushed), 1)
            self.assertEqual(cli.pull_count, 2)

    def test_correct_existing_target_skips_connect(self):
        cli = FakeCLI(heads=(1, 1))
        result = doc.operate(cli, ORIGIN, URL, 'read')
        self.assertEqual(result['content'], CONTENT)
        self.assertFalse(any(call[0] == 'connect' for call in cli.calls))

    def test_changed_bound_identity_blocks_edit_before_file_access(self):
        cli = mock.Mock()
        cli.run.return_value = {'origin': ORIGIN, 'id': 43}
        output = io.StringIO()
        with mock.patch.object(doc, 'CLI', return_value=cli), mock.patch.object(doc, 'operation_lock', return_value=contextlib.nullcontext()), mock.patch.object(doc, 'load_profile', return_value={'auth_mode': 'shimocli', 'bound_user_id': '42'}), mock.patch.object(doc, 'operate') as operate, contextlib.redirect_stdout(output):
            result = doc.main(['edit', '--address', ORIGIN, '--url', URL, '--patch-file', '/not-read.json', '--expected-sha', '0' * 64])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue())['error']['code'], 'identity_mismatch')
        operate.assert_not_called()
        cli.run.assert_called_once_with('whoami', '--origin', ORIGIN, '--json')

    def test_noop_skips_push(self):
        cli = FakeCLI(heads=(1, 1))
        result = self.edit(cli, patch=[{'old': 'Status: draft', 'new': 'Status: draft'}])
        self.assertFalse(result['changed'])
        self.assertFalse(cli.pushed)

    def test_cli_failure_output_is_not_forwarded(self):
        cli = object.__new__(doc.CLI)
        cli.binary = '/mock/shimocli'
        failed = subprocess.CompletedProcess([], 1, stdout='REVISION_CONFLICT secret-token', stderr='private details')
        with mock.patch.object(doc.subprocess, 'run', return_value=failed), self.assertRaises(doc.DriveError) as caught:
            cli.run('push', '/mock/candidate.md', '--json')
        self.assertEqual(caught.exception.code, 'revision_conflict')
        self.assertNotIn('secret-token', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
