"""Dependency failures and installation behavior, isolated from real tools/auth."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/shimo-drive/scripts'
sys.path.insert(0, str(SCRIPTS))
import environment as env
import document
import drive_api


def mach(arch=0x100000c):
    return b'\xcf\xfa\xed\xfe' + struct.pack('<I', arch) + b'\0' * 80


def archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as bundle:
        for name, value in entries:
            entry = tarfile.TarInfo(name)
            entry.size = len(value)
            bundle.addfile(entry, io.BytesIO(value))
    return output.getvalue()


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='shimo environment test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.binary = self.root / 'managed/bin/shimocli'

    def package(self, name='cli.tar.gz', data=None):
        data = data if data is not None else archive([('pkg/shimocli', mach())])
        path = self.root / name
        path.write_bytes(data)
        return path, hashlib.sha256(data).hexdigest()

    def test_missing_cli_identifies_source_required(self):
        with mock.patch.object(env, 'resolve_cli', return_value=None):
            result = env.check()
        self.assertFalse(result['ready'])
        self.assertEqual(result['error']['code'], 'source_required')
        self.assertIsNone(result['cli']['path'])

    def test_current_cli_is_noop_and_never_reads_package(self):
        with mock.patch.object(env, 'resolve_cli', return_value='/existing/shimocli'), \
             mock.patch.object(env, 'cli_version', return_value='0.2.0'), \
             mock.patch.object(env, 'read_package') as read:
            result = env.ensure('/nonexistent/package')
        self.assertTrue(result['ready'])
        self.assertEqual(result['action'], 'none')
        read.assert_not_called()

    def test_user_local_cli_found_without_path_and_used_by_both_adapters(self):
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(mach())
        self.binary.chmod(0o700)
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(env.shutil, 'which', return_value=None), \
             mock.patch.object(env, 'install_path', return_value=self.binary):
            self.assertEqual(env.resolve_cli(), str(self.binary))
            self.assertEqual(document.CLI().binary, str(self.binary))
            with mock.patch.object(drive_api.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{"id":"u1","origin":"https://tenant.example"}', '')) as run:
                drive_api.cli_identity('https://tenant.example')
            self.assertEqual(run.call_args.args[0][0], str(self.binary))

    def test_invalid_override_does_not_fallback(self):
        for path in ('', 'relative/cli', str(self.root / 'missing')):
            with self.subTest(path=path), mock.patch.dict(os.environ, {'SHIMO_DRIVE_CLI': path}), self.assertRaises(env.EnvironmentError) as caught:
                env.resolve_cli()
            self.assertEqual(caught.exception.code, 'cli_override_invalid')

    def test_checksum_required_and_mismatch(self):
        path, sha = self.package()
        for value, code in ((None, 'checksum_required'), ('0' * 64, 'checksum_mismatch')):
            with self.subTest(value=value), self.assertRaises(env.EnvironmentError) as caught:
                env.read_package(path, value)
            self.assertEqual(caught.exception.code, code)
        with mock.patch.object(env, 'host', return_value=('Darwin', 'arm64')):
            self.assertEqual(env.read_package(path, sha), mach())

    def test_self_extracting_package_is_read_without_executing_header(self):
        payload = archive([('shimocli-0.2.0-darwin-arm64/shimocli', mach())])
        content = ('#!/bin/bash\nexit 99\nVERSION="0.2.0"\nPKG="shimocli-0.2.0-darwin-arm64"\nEXPECTED_SHA256="' + hashlib.sha256(payload).hexdigest() + '"\n__PAYLOAD_BELOW__\n').encode() + base64.b64encode(payload)
        path, _ = self.package('install.sh', content)
        with mock.patch.object(env, 'host', return_value=('Darwin', 'arm64')):
            self.assertEqual(env.read_package(path), mach())
            path.write_bytes(content.replace(hashlib.sha256(payload).hexdigest().encode(), b'0' * 64))
            with self.assertRaises(env.EnvironmentError) as caught:
                env.read_package(path)
            self.assertEqual(caught.exception.code, 'checksum_mismatch')

    def test_unsafe_archive_and_duplicate_binary_refused(self):
        for entries in ([('../escape', b'x'), ('shimocli', mach())], [('.', b'x')], [('one/shimocli', mach()), ('two/shimocli', mach())]):
            with self.subTest(entries=[x[0] for x in entries]), self.assertRaises(env.EnvironmentError):
                env.archive_binary(archive(entries), 'tar')

    def test_archive_symlink_refused(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as bundle:
            member = tarfile.TarInfo('shimocli')
            member.type, member.linkname = tarfile.SYMTYPE, '/external'
            bundle.addfile(member)
        with self.assertRaises(env.EnvironmentError):
            env.archive_binary(output.getvalue(), 'tar')

    def test_zip_extracts_only_binary(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as bundle:
            bundle.writestr('package/readme.txt', 'documentation')
            bundle.writestr('package/shimocli.exe', b'native')
        self.assertEqual(env.archive_binary(output.getvalue(), 'zip'), b'native')

    def test_binary_host_checks(self):
        with mock.patch.object(env, 'host', return_value=('Darwin', 'arm64')):
            env.validate_binary(mach())
            for data in (mach(0x1000007), b'#!/bin/bash\n' + b'x' * 80):
                with self.assertRaises(env.EnvironmentError):
                    env.validate_binary(data)
        elf = bytearray(80)
        elf[:6], elf[18:20] = b'\x7fELF\x02\x01', struct.pack('<H', 62)
        with mock.patch.object(env, 'host', return_value=('Linux', 'x86_64')):
            env.validate_binary(elf)
        pe = bytearray(100)
        pe[:2], pe[60:64], pe[64:70] = b'MZ', struct.pack('<I', 64), b'PE\0\0' + struct.pack('<H', 0xaa64)
        with mock.patch.object(env, 'host', return_value=('Windows', 'arm64')):
            env.validate_binary(pe)

    def test_install_is_private_and_discoverable(self):
        path, sha = self.package()
        with mock.patch.object(env, 'host', return_value=('Darwin', 'arm64')), \
             mock.patch.object(env, 'install_path', return_value=self.binary), \
             mock.patch.object(env, 'resolve_cli', side_effect=lambda: str(self.binary) if self.binary.exists() else None), \
             mock.patch.object(env, 'cli_version', return_value='0.2.0'), \
             mock.patch.object(env, 'preserve_quarantine'):
            result = env.ensure(str(path), sha)
        self.assertTrue(result['ready'])
        self.assertEqual(result['action'], 'installed')
        self.assertEqual(self.binary.read_bytes(), mach())
        self.assertEqual(self.binary.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(self.binary.parent.iterdir()), [self.binary])

    def test_failed_version_never_installs(self):
        path, sha = self.package()
        with mock.patch.object(env, 'host', return_value=('Darwin', 'arm64')), \
             mock.patch.object(env, 'install_path', return_value=self.binary), \
             mock.patch.object(env, 'resolve_cli', return_value=None), \
             mock.patch.object(env, 'preserve_quarantine'), \
             mock.patch.object(env, 'cli_version', side_effect=env.EnvironmentError('cli_invalid', 'failed')), \
             self.assertRaises(env.EnvironmentError):
            env.ensure(str(path), sha)
        self.assertFalse(self.binary.exists())
        self.assertEqual(list(self.binary.parent.iterdir()), [])

    def test_existing_destination_is_never_overwritten(self):
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(b'preserve')
        path, sha = self.package()
        with mock.patch.object(env, 'host', return_value=('Darwin', 'arm64')), \
             mock.patch.object(env, 'install_path', return_value=self.binary), \
             mock.patch.object(env, 'resolve_cli', return_value=None), \
             mock.patch.object(env, 'preserve_quarantine'), \
             mock.patch.object(env, 'cli_version', return_value='0.2.0'), \
             self.assertRaises(env.EnvironmentError):
            env.ensure(str(path), sha)
        self.assertEqual(self.binary.read_bytes(), b'preserve')

    def test_version_failure_does_not_echo_private_output(self):
        with mock.patch.object(env.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, 'private credential', 'sensitive')), self.assertRaises(env.EnvironmentError) as caught:
            env.cli_version('/mock/cli')
        self.assertNotIn('private', str(caught.exception))

    def test_quarantine_attribute_is_preserved(self):
        with mock.patch.object(env.platform, 'system', return_value='Darwin'), \
             mock.patch.object(env.subprocess, 'run', side_effect=[
                 subprocess.CompletedProcess([], 0, 'com.apple.quarantine\n', ''),
                 subprocess.CompletedProcess([], 0, '30 30 38 31\n', ''),
                 subprocess.CompletedProcess([], 0, b'', b'')]) as run:
            env.preserve_quarantine('/package', '/binary')
        self.assertEqual(run.call_args.args[0], ['/usr/bin/xattr', '-wx', 'com.apple.quarantine', '30303831', '/binary'])


@unittest.skipUnless(shutil.which('bash'), 'Bash entrypoint requires Bash')
class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='shimo bootstrap test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copyfile(SCRIPTS / 'bootstrap.sh', self.root / 'bootstrap.sh')
        for script in ('environment.py', 'drive_api.py'):
            (self.root / script).write_text('import json,sys; print(json.dumps({"args":sys.argv[1:]}))')
        self.env = dict(os.environ, SHIMO_DRIVE_PYTHON=sys.executable)
        self.env.pop('BASH_ENV', None)

    def run_bootstrap(self, *args):
        return subprocess.run(['/bin/bash', str(self.root / 'bootstrap.sh'), *args], env=self.env, capture_output=True, text=True)

    def test_path_with_spaces_and_exact_arguments(self):
        result = self.run_bootstrap('run', 'drive_api.py', '--query', '季度 报告', '--address', 'https://tenant.example')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['args'], ['--query', '季度 报告', '--address', 'https://tenant.example'])

    def test_existing_python_check_and_ensure_forward(self):
        for mode in ('check', 'ensure'):
            result = self.run_bootstrap(mode)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['args'], [mode])

    def test_old_or_broken_explicit_python_is_not_silently_replaced(self):
        fake = self.root / 'old python'
        fake.write_text('#!/bin/sh\nexit 1\n')
        fake.chmod(0o700)
        self.env['SHIMO_DRIVE_PYTHON'] = str(fake)
        result = self.run_bootstrap('ensure')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['error'], 'python_override_invalid')

    def test_missing_python_reports_before_calling_python(self):
        # Hide Python candidates through Bash's command lookup; do not uninstall tools.
        hook = self.root / 'lookup.sh'
        hook.write_text('command() { case "$*" in *python*|*Python*) return 1;; esac; builtin command "$@"; }\n')
        self.env.pop('SHIMO_DRIVE_PYTHON')
        self.env['BASH_ENV'] = str(hook)
        result = self.run_bootstrap('check')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['error'], 'python_missing')

    def test_missing_python_installs_then_reprobes_and_failure_stops(self):
        hook = self.root / 'lookup.sh'
        hook.write_text('''
uname() { printf Darwin; }
command() {
  if [ "$1" = -v ]; then
    case "${@: -1}" in
      *python*|*Python*)
        [ -f "$TEST_INSTALLED" ] || return 1
        printf '%s\\n' "$TEST_PYTHON"; return 0 ;;
      brew|*/brew) printf '%s\\n' "$TEST_BREW"; return 0 ;;
    esac
  fi
  builtin command "$@"
}
''')
        manager = self.root / 'fake brew'
        manager.write_text('#!/bin/sh\n[ "$1" = install ] && [ "$2" = python@3.13 ] || exit 99\n[ "$TEST_FAIL" != 1 ] || exit 1\ntouch "$TEST_INSTALLED"\n')
        manager.chmod(0o700)
        self.env.pop('SHIMO_DRIVE_PYTHON')
        self.env.update(BASH_ENV=str(hook), TEST_PYTHON=sys.executable, TEST_BREW=str(manager), TEST_INSTALLED=str(self.root / 'installed'), TEST_FAIL='1')
        failed = self.run_bootstrap('ensure')
        self.assertEqual(json.loads(failed.stdout)['error'], 'python_install_required')
        self.env['TEST_FAIL'] = '0'
        result = self.run_bootstrap('ensure', '--cli-package', '/path with spaces/cli.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['args'], ['ensure', '--cli-package', '/path with spaces/cli.sh'])

    def test_disallowed_script_cannot_run(self):
        result = self.run_bootstrap('run', '../unrelated.py')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['error'], 'invalid_script')


if __name__ == '__main__':
    unittest.main()
