#!/usr/bin/env python3
"""Serialize public shimocli commands and apply checked, exact text replacements."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
from environment import EnvironmentError, resolve_cli
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from drive_api import DriveError, config_root, load_profile, normalize_origin, valid_guid


def target(address, url):
    origin = normalize_origin(address)
    parts = urlsplit(url)
    if (parts.query or parts.fragment or parts.username or parts.password
            or '\\' in url or any(c.isspace() for c in url)):
        raise DriveError('invalid_url', 'Use a direct document URL without query, fragment or credentials.')
    if normalize_origin(parts.scheme + '://' + parts.netloc) != origin:
        raise DriveError('origin_mismatch', 'Document must belong to the configured deployment.')
    segments = parts.path.strip('/').split('/')
    if len(segments) != 2 or segments[0] != 'docs':
        raise DriveError('unsupported_type', 'This release edits only lightweight documents under /docs/.')
    return origin, valid_guid(segments[1])


def digest(content):
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def apply_replacements(content, replacements):
    if not isinstance(replacements, list) or not 1 <= len(replacements) <= 20:
        raise DriveError('invalid_patch', 'Patch must be an array of 1–20 exact old/new replacements.')
    for item in replacements:
        if (not isinstance(item, dict) or set(item) != {'old', 'new'}
                or not isinstance(item['old'], str) or not item['old']
                or not isinstance(item['new'], str)):
            raise DriveError('invalid_patch', 'Each replacement requires nonempty old text and string new text.')
        if content.count(item['old']) != 1:
            raise DriveError('ambiguous_patch', 'Old text must occur exactly once; read again and include more context.')
        content = content.replace(item['old'], item['new'], 1)
    return content


@contextmanager
def operation_lock():
    root = config_root()
    if root.is_symlink():
        raise DriveError('insecure_lock', 'Lock directory must not be a symbolic link.')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = root / 'cli-operation.lock'
    if lock.is_symlink():
        raise DriveError('insecure_lock', 'Lock must not be a symbolic link.')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b'0')
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise DriveError('busy', 'Another Shimo document operation is running.') from None
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DriveError('busy', 'Another Shimo document operation is running.') from None
        yield
    finally:
        os.close(fd)


class CLI:
    def __init__(self):
        try:
            self.binary = resolve_cli()
        except EnvironmentError as error:
            raise DriveError(error.code, error.message) from None
        if not self.binary:
            raise DriveError('cli_missing', 'Run environment.py ensure with an administrator-provided CLI package.')

    def run(self, *args, json_result=True):
        try:
            proc = subprocess.run([self.binary, *args], capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise DriveError('cli_timeout', 'CLI timed out; mutation outcome may be unknown. Read the target before any retry.') from None
        if proc.returncode:
            # Do not relay arbitrary CLI output: it may contain auth/debug data.
            combined = proc.stdout + proc.stderr
            code = 'cli_failed'
            for marker in ('REVISION_CONFLICT', 'EXISTING_EMBED_OMITTED'):
                if marker in combined:
                    code = marker.lower()
            raise DriveError(code, 'CLI failed. Check login, permissions, server compatibility and network; read before retrying a write.')
        if not json_result:
            return proc.stdout.strip()
        try:
            # push emits JSON progress records (NDJSON); status/pull emit one object.
            records = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
            if not records or any(not isinstance(item, dict) for item in records):
                raise ValueError()
            if any(item.get('ok') is False or item.get('error') for item in records):
                raise ValueError()
            return records[-1]
        except ValueError:
            raise DriveError('cli_response', 'CLI did not return a JSON object.') from None

    def check(self, origin, guid):
        state = self.run('status', '--json')
        if state.get('origin') != origin or state.get('guid') != guid or state.get('auth') != 'ready':
            raise DriveError('target_changed', 'CLI target or authentication changed; no write will proceed.')
        if state.get('head') is None:
            raise DriveError('missing_revision', 'CLI did not provide a document revision.')
        return state


def operate(cli, address, url, action, patch=None, expected_sha=None):
    origin, guid = target(address, url)
    # Reusing an already-correct target avoids needless profile replacement and
    # host-agent file-operation prompts. A different target still reconnects.
    try:
        before = cli.check(origin, guid)
    except DriveError as exc:
        if exc.code not in ('target_changed', 'cli_failed'):
            raise
        cli.run('connect', url, json_result=False)
        before = cli.check(origin, guid)
    with tempfile.TemporaryDirectory(prefix='shimo-drive-', dir=Path.cwd()) as temp:
        source = Path(temp) / 'source.md'
        pulled = cli.run('pull', '--output', str(source), '--json')
        content = source.read_text(encoding='utf-8')
        if len(content.encode()) > 2 * 1024 * 1024:
            raise DriveError('document_too_large', 'Document exceeds this release\'s 2 MiB editing limit.')
        state = cli.check(origin, guid)
        if before['head'] != state['head'] or pulled.get('head') != state['head']:
            raise DriveError('document_changed', 'Document changed during read; read again before editing.')
        result = {'url': url, 'guid': guid, 'head': state['head'], 'sha256': digest(content)}
        if action == 'read':
            return {**result, 'content': content}
        if expected_sha != digest(content):
            raise DriveError('document_changed', 'Content differs from the reviewed read; re-read and regenerate the patch.')
        edited = apply_replacements(content, patch)
        if edited == content:
            return {**result, 'changed': False, 'verified': True}
        candidate = Path(temp) / 'candidate.md'
        candidate.write_text(edited, encoding='utf-8')
        if cli.check(origin, guid)['head'] != state['head']:
            raise DriveError('document_changed', 'Document changed before write; re-read and regenerate the patch.')
        cli.run('push', str(candidate), '--json')
        # Never replay a mutation after an uncertain response. Readback may fail independently.
        try:
            cli.check(origin, guid)
            readback = Path(temp) / 'readback.md'
            final_pull = cli.run('pull', '--output', str(readback), '--json')
            actual = readback.read_text(encoding='utf-8')
            final = cli.check(origin, guid)
            if actual != edited or final_pull.get('head') != final['head']:
                raise DriveError('verification_mismatch', 'Write submitted but readback differs; inspect document before another edit.')
        except (DriveError, OSError, UnicodeError):
            raise DriveError('write_unverified', 'Write submitted, but exact readback was not confirmed. Do not retry; read the target and inspect changes.') from None
        return {**result, 'head': final['head'], 'sha256': digest(actual), 'changed': True, 'verified': True}


def main(argv=None):
    try:
        p = argparse.ArgumentParser(description=__doc__)
        sub = p.add_subparsers(dest='action', required=True)
        for action in ('doctor', 'read', 'edit'):
            s = sub.add_parser(action)
            s.add_argument('--address', required=True)
            if action != 'doctor':
                s.add_argument('--url', required=True)
            if action == 'edit':
                s.add_argument('--patch-file', required=True)
                s.add_argument('--expected-sha', required=True)
        args = p.parse_args(argv)
        origin = normalize_origin(args.address)
        cli = CLI()
        with operation_lock():
            if args.action == 'doctor':
                version = cli.run('version', json_result=False)
                identity = cli.run('whoami', '--origin', origin, '--json')
                if identity.get('origin') != origin or identity.get('id') is None:
                    raise DriveError('identity_mismatch', 'CLI did not verify an identity for this deployment.')
                result = {'origin': origin, 'version': version, 'user_id': identity['id'],
                          'note': 'CLI identity only; Drive search and document writes not verified.'}
            else:
                profile = load_profile(origin)
                if profile and profile.get('auth_mode') == 'shimocli':
                    identity = cli.run('whoami', '--origin', origin, '--json')
                    if (identity.get('origin') != origin or profile.get('bound_user_id') is None
                            or str(identity.get('id')) != str(profile['bound_user_id'])):
                        raise DriveError('identity_mismatch', 'CLI user differs from the Drive binding; no document operation was sent.')
                patch = json.loads(Path(args.patch_file).read_text(encoding='utf-8')) if args.action == 'edit' else None
                result = operate(cli, origin, args.url, args.action, patch, getattr(args, 'expected_sha', None))
        print(json.dumps({'ok': True, 'data': result}, ensure_ascii=False))
        return 0
    except DriveError as exc:
        print(json.dumps({'ok': False, 'error': {'code': exc.code, 'message': exc.message}}, ensure_ascii=False))
        return 1
    except (OSError, ValueError):
        print(json.dumps({'ok': False, 'error': {'code': 'local_file_error', 'message': 'Could not read valid local files.'}}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
