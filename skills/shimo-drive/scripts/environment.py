#!/usr/bin/env python3
"""Offline dependency inspection and bounded, user-local native CLI installation."""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import zipfile

MAX_PACKAGE = 200 * 1024 * 1024
MAX_BINARY = 100 * 1024 * 1024


class EnvironmentError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def host():
    machine = platform.machine().lower()
    return platform.system(), {'aarch64': 'arm64', 'amd64': 'x86_64'}.get(machine, machine)


def install_path():
    if os.name == 'nt':
        root = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local')))
        return root / 'shimo-drive/bin/shimocli.exe'
    return Path.home() / '.local/share/shimo-drive/bin/shimocli'


def executable(path):
    return path.is_file() and os.access(str(path), os.X_OK)


def resolve_cli():
    override = os.environ.get('SHIMO_DRIVE_CLI')
    if override is not None:
        path = Path(override)
        if not override or not path.is_absolute() or not executable(path):
            raise EnvironmentError('cli_override_invalid', 'SHIMO_DRIVE_CLI must name an absolute executable file; no fallback was attempted.')
        return str(path)
    found = shutil.which('shimocli')
    if found:
        return found
    for path in (install_path(), Path('/opt/homebrew/bin/shimocli'),
                 Path('/usr/local/bin/shimocli'), Path.home() / '.local/bin/shimocli'):
        if executable(path):
            return str(path)
        if path == install_path() and (path.exists() or path.is_symlink()):
            raise EnvironmentError('cli_invalid', 'An existing managed CLI is not executable; preserved for administrator inspection.')
    return None


def cli_version(path):
    try:
        result = subprocess.run([str(path), 'version'], capture_output=True, text=True, timeout=15)
        match = re.search(r'(?<![\w.])v?(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)(?![\w.])', result.stdout)
        if result.returncode or not match:
            raise ValueError()
        return match.group(1)
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        raise EnvironmentError('cli_invalid', 'CLI version verification failed. Check the compatible build and OS security prompts with the administrator; existing files were preserved and security restrictions were not bypassed.') from None


def check():
    system, arch = host()
    report = {'python': {'executable': sys.executable, 'version': platform.python_version(),
                         'ready': sys.version_info >= (3, 9)},
              'platform': system, 'arch': arch, 'cli': {'status': 'source_required', 'path': None, 'version': None}, 'ready': False}
    try:
        path = resolve_cli()
        if path:
            report['cli'].update(path=path, version=cli_version(path), status='ready')
        else:
            report['error'] = {'code': 'source_required', 'message': 'Ask the deployment administrator for a compatible local shimocli package; no public download source is configured.'}
    except EnvironmentError as error:
        report['cli']['status'] = error.code
        report['error'] = {'code': error.code, 'message': error.message}
    report['ready'] = report['python']['ready'] and report['cli']['status'] == 'ready'
    return report


def validate_binary(data):
    if len(data) < 64 or len(data) > MAX_BINARY:
        raise EnvironmentError('invalid_binary', 'CLI binary size is invalid.')
    system = arch = None
    if data[:4] in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf'):
        endian = '<' if data[:4] == b'\xcf\xfa\xed\xfe' else '>'
        cpu = struct.unpack(endian + 'I', data[4:8])[0]
        system, arch = 'Darwin', {0x100000c: 'arm64', 0x1000007: 'x86_64'}.get(cpu)
    elif data[:4] == b'\x7fELF' and data[4] == 2 and data[5] in (1, 2):
        machine = struct.unpack(('<' if data[5] == 1 else '>') + 'H', data[18:20])[0]
        system, arch = 'Linux', {183: 'arm64', 62: 'x86_64'}.get(machine)
    elif data[:2] == b'MZ':
        offset = struct.unpack('<I', data[60:64])[0]
        if offset + 6 <= len(data) and data[offset:offset + 4] == b'PE\x00\x00':
            machine = struct.unpack('<H', data[offset + 4:offset + 6])[0]
            system, arch = 'Windows', {0xaa64: 'arm64', 0x8664: 'x86_64'}.get(machine)
    if arch is None or (system, arch) != host():
        raise EnvironmentError('platform_mismatch', 'Package must contain a native arm64 or x86_64 CLI matching this operating system and architecture; scripts and universal binaries are unsupported.')


def archive_binary(data, kind, expected_name=None):
    candidates = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data)) if kind == 'zip' else tarfile.open(fileobj=io.BytesIO(data), mode='r:gz')
        with archive:
            members = archive.infolist() if kind == 'zip' else archive
            total = 0
            for count, member in enumerate(members, 1):
                if count > 1000:
                    raise EnvironmentError('invalid_package', 'Package has too many entries.')
                name = member.filename if kind == 'zip' else member.name
                parts = PurePosixPath(name).parts
                if not parts or name.startswith('/') or '\\' in name or '..' in parts or ':' in name:
                    raise EnvironmentError('unsafe_archive', 'Archive contains an unsafe path.')
                if kind == 'zip':
                    mode = member.external_attr >> 16
                    regular = not member.is_dir() and (mode & 0o170000) in (0, 0o100000)
                    if (mode & 0o170000) not in (0, 0o100000, 0o040000):
                        raise EnvironmentError('unsafe_archive', 'Archive links and special files are unsupported.')
                    size = member.file_size
                else:
                    regular, size = member.isfile(), member.size
                    if not regular and not member.isdir():
                        raise EnvironmentError('unsafe_archive', 'Archive links and special files are unsupported.')
                total += size
                if size < 0 or total > MAX_PACKAGE:
                    raise EnvironmentError('invalid_package', 'Uncompressed package exceeds the size limit.')
                if parts[-1] in ('shimocli', 'shimocli.exe'):
                    if not regular or size > MAX_BINARY or (expected_name and name != expected_name):
                        raise EnvironmentError('invalid_package', 'CLI archive entry does not match the expected regular binary.')
                    stream = archive.open(member) if kind == 'zip' else archive.extractfile(member)
                    with stream:
                        binary = stream.read(MAX_BINARY + 1)
                    candidates.append(binary)
    except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError, RuntimeError):
        raise EnvironmentError('invalid_package', 'Cannot read the provided archive.') from None
    if len(candidates) != 1:
        raise EnvironmentError('invalid_package', 'Package must contain exactly one CLI binary.')
    return candidates[0]


def read_package(path, sha256=None):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_PACKAGE:
        raise EnvironmentError('invalid_package', 'Provide an absolute path to a regular local package no larger than 200 MiB.')
    with path.open('rb') as stream:
        data = stream.read(MAX_PACKAGE + 1)
    if len(data) > MAX_PACKAGE:
        raise EnvironmentError('invalid_package', 'Package exceeds the size limit.')
    if sha256 is not None and (not re.fullmatch(r'[a-fA-F0-9]{64}', sha256) or hashlib.sha256(data).hexdigest() != sha256.lower()):
        raise EnvironmentError('checksum_mismatch', 'Package SHA-256 does not match the administrator-provided checksum.')
    if path.suffix.lower() == '.sh':
        split = re.split(br'(?m)^__PAYLOAD_BELOW__\r?\n', data)
        if len(split) != 2:
            raise EnvironmentError('invalid_package', 'Unsupported self-extracting installer format.')
        header, encoded = split
        def field(name, pattern):
            matches = re.findall(rb'(?m)^' + name.encode() + rb'="(' + pattern + rb')"\r?$', header)
            if len(matches) != 1:
                raise EnvironmentError('invalid_package', 'Installer metadata is missing or ambiguous.')
            return matches[0].decode('ascii')
        version = field('VERSION', rb'\d+\.\d+\.\d+')
        package = field('PKG', rb'shimocli-\d+\.\d+\.\d+-darwin-(?:arm64|x86_64)')
        checksum = field('EXPECTED_SHA256', rb'[a-fA-F0-9]{64}')
        if package != 'shimocli-' + version + '-darwin-' + host()[1] or host()[0] != 'Darwin':
            raise EnvironmentError('platform_mismatch', 'Installer metadata does not match this host.')
        try:
            payload = base64.b64decode(re.sub(rb'\s+', b'', encoded), validate=True)
        except ValueError:
            raise EnvironmentError('invalid_package', 'Installer payload is not valid base64.') from None
        if hashlib.sha256(payload).hexdigest() != checksum.lower():
            raise EnvironmentError('checksum_mismatch', 'Embedded archive checksum mismatch.')
        binary = archive_binary(payload, 'tar', package + '/shimocli')
    else:
        if sha256 is None:
            raise EnvironmentError('checksum_required', 'Ask the administrator for the package SHA-256 and pass --sha256.')
        if path.name.endswith(('.tar.gz', '.tgz')):
            binary = archive_binary(data, 'tar')
        elif path.suffix.lower() == '.zip':
            binary = archive_binary(data, 'zip')
        else:
            binary = data
    validate_binary(binary)
    return binary


def preserve_quarantine(source, target):
    if platform.system() != 'Darwin':
        return
    try:
        attrs = subprocess.run(['/usr/bin/xattr', str(source)], capture_output=True, text=True, timeout=10)
        if attrs.returncode:
            raise ValueError()
        if 'com.apple.quarantine' not in attrs.stdout.splitlines():
            return
        value = subprocess.run(['/usr/bin/xattr', '-px', 'com.apple.quarantine', str(source)], capture_output=True, text=True, timeout=10)
        encoded = re.sub(r'\s+', '', value.stdout)
        if value.returncode or not re.fullmatch(r'(?:[a-fA-F0-9]{2})+', encoded):
            raise ValueError()
        result = subprocess.run(['/usr/bin/xattr', '-wx', 'com.apple.quarantine', encoded, str(target)], capture_output=True, timeout=10)
        if result.returncode:
            raise ValueError()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise EnvironmentError('quarantine_preservation_failed', 'Cannot preserve package security attributes; installation stopped.') from None


def ensure(package=None, sha256=None):
    report = check()
    if report['ready']:
        report['action'] = 'none'
        return report
    if report['cli']['status'] != 'source_required' or package is None:
        return report
    binary = read_package(package, sha256)
    destination = install_path()
    # Do not follow a redirected installation directory or overwrite any existing file.
    for parent in (destination.parent, *destination.parent.parents):
        if parent.is_symlink():
            raise EnvironmentError('unsafe_destination', 'Managed CLI directory must not contain symbolic links.')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination.parent, 0o700)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='.shimocli-', suffix='.exe' if os.name == 'nt' else '', dir=str(destination.parent))
        with os.fdopen(fd, 'wb') as stream:
            stream.write(binary)
        os.chmod(temporary, 0o700)
        preserve_quarantine(package, temporary)
        cli_version(temporary)
        # Hard-link creation is atomic and refuses an existing destination.
        os.link(temporary, destination)
    except FileExistsError:
        raise EnvironmentError('destination_exists', 'Existing CLI preserved; automatic replacement is disabled.') from None
    except OSError:
        raise EnvironmentError('install_failed', 'User-local CLI installation failed; existing files were preserved.') from None
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    report = check()
    report['action'] = 'installed'
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check', 'ensure'))
    parser.add_argument('--cli-package')
    parser.add_argument('--sha256')
    args = parser.parse_args(argv)
    try:
        report = check() if args.command == 'check' else ensure(args.cli_package, args.sha256)
    except EnvironmentError as error:
        report = check()
        report['ready'] = False
        report['error'] = {'code': error.code, 'message': error.message}
    except OSError:
        report = {'ready': False, 'error': {'code': 'install_failed', 'message': 'Cannot access the local package or installation directory. Check local file permissions.'}}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['ready'] else 1


if __name__ == '__main__':
    sys.exit(main())
