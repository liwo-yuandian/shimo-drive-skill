#!/usr/bin/env python3
"""Read-only private Shimo Drive API; standard-library-only, explicit origin per call."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
from environment import EnvironmentError, resolve_cli
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


class DriveError(Exception):
    def __init__(self, code, message, http_status=None, api_path=None):
        self.code, self.message = code, message
        self.http_status, self.api_path = http_status, api_path
        super().__init__(message)


def normalize_origin(address):
    try:
        parts = urllib.parse.urlsplit(address)
        if (parts.scheme != 'https' or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment
                or parts.path not in ('', '/', '/desktop', '/desktop/')
                or any(c.isspace() or ord(c) < 32 for c in address) or '\\' in address):
            raise ValueError()
        host = parts.hostname.encode('idna').decode('ascii').lower()
        if ':' in host:
            host = '[' + host + ']'
        port = parts.port
        return 'https://' + host + ((':' + str(port)) if port and port != 443 else '')
    except (ValueError, UnicodeError):
        raise DriveError('invalid_address', 'Use an HTTPS deployment root or /desktop address without credentials, query, or fragment.') from None


def config_root():
    if sys.platform == 'win32':
        base = Path(os.environ.get('APPDATA', Path.home() / 'AppData' / 'Roaming'))
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support'
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    return base / 'shimo-drive'


def profile_path(origin, root=None):
    return (root or config_root()) / (hashlib.sha256(origin.encode()).hexdigest() + '.json')


def load_profile(origin, root=None):
    path = profile_path(origin, root)
    if not path.exists():
        return None
    try:
        if path.is_symlink() or (os.name != 'nt' and path.stat().st_mode & 0o077):
            raise DriveError('insecure_config', 'Profile must be a private regular file with mode 600.')
        profile = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(profile, dict) or profile.get('origin') != origin:
            raise ValueError()
        return profile
    except (OSError, ValueError):
        raise DriveError('invalid_config', 'Cannot read a valid profile for this deployment.') from None


def save_profile(origin, token=None, root=None, binding=None):
    path = profile_path(origin, root)
    try:
        if path.parent.is_symlink() or path.is_symlink():
            raise DriveError('insecure_config', 'Profile location must not be a symbolic link.')
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        profile = load_profile(origin, root) or {'origin': origin}
        if token is not None:
            if profile.get('auth_mode') == 'shimocli':
                raise DriveError('auth_conflict', 'This deployment is bound to CLI authentication; do not silently replace it with a bearer token.')
            profile['token'] = validate_token(token)
        if binding is not None:
            if profile.get('token'):
                raise DriveError('auth_conflict', 'This deployment already has a bearer token; choose one authentication mode explicitly.')
            profile.update({'auth_mode': 'shimocli', 'bound_user_id': binding})
        fd, temp = tempfile.mkstemp(prefix='.profile-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(profile, stream)
            os.chmod(temp, 0o600)
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    except OSError:
        raise DriveError('config_write_failed', 'Could not securely save local profile.') from None


def validate_token(token):
    if not isinstance(token, str) or not token or any(c.isspace() or ord(c) < 32 for c in token):
        raise DriveError('invalid_token', 'Personal token must be nonempty and contain no whitespace.')
    return token


def get_token(origin, root=None, environ=None):
    env = os.environ if environ is None else environ
    profile = load_profile(origin, root)
    if profile and profile.get('auth_mode') == 'shimocli':
        raise DriveError('auth_conflict', 'CLI-bound deployment cannot use bearer authentication.')
    if env.get('SHIMO_DRIVE_TOKEN'):
        if not env.get('SHIMO_DRIVE_ORIGIN') or normalize_origin(env['SHIMO_DRIVE_ORIGIN']) != origin:
            raise DriveError('token_origin_mismatch', 'Set SHIMO_DRIVE_ORIGIN to the deployment origin matching the environment token.')
        return validate_token(env['SHIMO_DRIVE_TOKEN'])
    profile = load_profile(origin, root)
    if not profile or not profile.get('token'):
        raise DriveError('auth_required', 'Run login for this address in a trusted terminal, or supply SHIMO_DRIVE_TOKEN and matching SHIMO_DRIVE_ORIGIN.')
    return validate_token(profile['token'])


def validate_cookie(cookie):
    if (not isinstance(cookie, str) or not cookie.strip() or '=' not in cookie
            or any(ord(c) < 32 or ord(c) > 126 for c in cookie)):
        raise DriveError('invalid_cli_cookie', 'CLI cookie is missing or has an invalid header format.')
    return cookie


def cli_cookie(origin, environ=None):
    """Read only the explicit origin's credential from the fixed CLI source."""
    env = os.environ if environ is None else environ
    parts = urllib.parse.urlsplit(normalize_origin(origin))
    if parts.port is not None:
        raise DriveError('unsupported_cookie_origin', 'CLI cookie binding supports the default HTTPS port only.')
    source = Path(env.get('CODEX_HOME') or Path.home() / '.codex') / 'shimocli' / 'auth.json'
    fd = None
    try:
        if any(part.is_symlink() for part in (source, source.parent, source.parent.parent)):
            raise DriveError('insecure_cli_auth', 'CLI credential source must not be a symbolic link.')
        initial = source.lstat()
        if not stat.S_ISREG(initial.st_mode):
            raise DriveError('insecure_cli_auth', 'CLI credential source must be a regular file.')
        fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode) or (os.name != 'nt' and stat.S_IMODE(mode) != 0o600):
            raise DriveError('insecure_cli_auth', 'CLI credential source must be a regular private file with mode 600.')
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            fd = None
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError()
        value = json.loads(raw)
        cookies = value.get('cookies') if isinstance(value, dict) else None
        if not isinstance(cookies, dict) or parts.hostname not in cookies:
            raise DriveError('cli_auth_required', 'No CLI cookie exists for this exact deployment hostname.')
        return validate_cookie(cookies[parts.hostname])
    except (OSError, ValueError, UnicodeError):
        raise DriveError('cli_auth_required', 'Cannot read valid protected CLI credentials; log in with the CLI first.') from None
    finally:
        if fd is not None:
            os.close(fd)


def cli_identity(origin):
    try:
        binary = resolve_cli()
    except EnvironmentError as error:
        raise DriveError(error.code, error.message) from None
    if not binary:
        raise DriveError('cli_missing', 'Run environment.py ensure with an administrator-provided CLI package.')
    try:
        result = subprocess.run([binary, 'whoami', '--origin', origin, '--json'],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError()
        identity = json.loads(result.stdout)
        if not isinstance(identity, dict) or identity.get('origin') != origin:
            raise ValueError()
        return identity_id(identity)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise DriveError('cli_identity_failed', 'CLI identity could not be verified for this deployment.') from None


def identity_id(identity):
    value = identity.get('id') if isinstance(identity, dict) else None
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value):
        raise DriveError('identity_mismatch', 'Identity response did not contain a valid user ID.')
    return str(value)


def verify_cli_identity(client, expected=None):
    cli_id = cli_identity(client.origin)
    api_id = identity_id(client._request('GET', '/api/v1/users/me'))
    if cli_id != api_id or (expected is not None and api_id != str(expected)):
        raise DriveError('identity_mismatch', 'CLI, API and bound user identities must match; no business request was sent.')
    return api_id


def bind_cli(origin, root=None, environ=None, opener=None):
    env = os.environ if environ is None else environ
    profile = load_profile(origin, root) or {}
    if env.get('SHIMO_DRIVE_TOKEN') or profile.get('token'):
        raise DriveError('auth_conflict', 'Bearer credentials exist; CLI binding will not silently replace them.')
    cookie = cli_cookie(origin, env)
    client = Client(origin, cookie, opener=opener, auth_kind='cookie')
    user_id = verify_cli_identity(client, profile.get('bound_user_id'))
    save_profile(origin, root=root, binding=user_id)
    return {'origin': origin, 'auth_mode': 'shimocli', 'bound_user_id': user_id,
            'identity_verified': True, 'cookie_copied': False}


def credentials(origin, root=None, environ=None):
    env = os.environ if environ is None else environ
    profile = load_profile(origin, root)
    if profile and profile.get('auth_mode') == 'shimocli':
        if env.get('SHIMO_DRIVE_TOKEN') or profile.get('token'):
            raise DriveError('auth_conflict', 'Both CLI binding and bearer credentials exist; refusing ambiguous authentication.')
        if profile.get('bound_user_id') is None:
            raise DriveError('invalid_config', 'CLI binding is missing its verified user ID.')
        return cli_cookie(origin, env), 'cookie', str(profile['bound_user_id'])
    return get_token(origin, root, env), 'bearer', None


def authenticated_client(origin, credential, auth_kind, bound_user_id, opener=None):
    client = Client(origin, credential, opener=opener, auth_kind=auth_kind)
    if auth_kind == 'cookie':
        client.identity_guard = lambda: verify_cli_identity(client, bound_user_id)
    return client


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DriveError('redirect_refused', 'API redirect refused; verify the configured deployment address.')


def safe_url(origin, value):
    if value is None:
        return None
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value) or '\\' in value:
        raise DriveError('invalid_response', 'API returned an invalid file URL.')
    resolved = urllib.parse.urljoin(origin + '/', value)
    parts = urllib.parse.urlsplit(resolved)
    if parts.username is not None or parts.password is not None:
        raise DriveError('cross_origin_url', 'API returned a file URL containing credentials.')
    try:
        url_origin = normalize_origin(urllib.parse.urlunsplit((parts.scheme, parts.netloc, '', '', '')))
    except DriveError:
        raise DriveError('cross_origin_url', 'API returned a non-HTTPS or invalid file URL.') from None
    if url_origin != origin:
        raise DriveError('cross_origin_url', 'API returned a file URL outside this deployment.')
    return resolved


def valid_guid(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise DriveError('invalid_guid', 'File/folder GUID must contain only letters, digits, underscores or hyphens (1–128 characters).')
    return value


def file_summary(origin, item):
    if not isinstance(item, dict) or not isinstance(item.get('guid'), str):
        raise DriveError('invalid_response', 'API returned a malformed file record.')
    valid_guid(item['guid'])
    permission_source = item.get('permissionsAndReasons')
    permissions = None
    if isinstance(permission_source, dict):
        permissions = {}
        for key in ('canRead', 'canEdit'):
            entry = permission_source.get(key)
            value = entry.get('value') if isinstance(entry, dict) else None
            permissions[key] = value if type(value) is bool else None
    return {**{k: item.get(k) for k in ('guid', 'name', 'type', 'role', 'isFolder', 'parentGuid')},
            'permissions': permissions, 'url': safe_url(origin, item.get('url'))}


class Client:
    def __init__(self, origin, token, opener=None, timeout=20, auth_kind='bearer'):
        self.origin = normalize_origin(origin)
        if auth_kind not in ('bearer', 'cookie'):
            raise DriveError('invalid_auth_mode', 'Unsupported authentication mode.')
        self.auth_kind = auth_kind
        self.token = validate_cookie(token) if auth_kind == 'cookie' else validate_token(token)
        self.identity_guard = None
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.timeout = timeout

    def request(self, method, path, payload=None, query=None):
        if self.identity_guard is not None:
            self.identity_guard()
        return self._request(method, path, payload, query)

    def _request(self, method, path, payload=None, query=None):
        # Only call-site-owned paths; GUID validation keeps path segments inert.
        if not path.startswith('/api/v1/') or '?' in path or '#' in path or '..' in path:
            raise DriveError('invalid_endpoint', 'Unsupported API path.')
        url = self.origin + path
        if query:
            url += '?' + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={**({'Cookie': self.token} if self.auth_kind == 'cookie' else {'Authorization': 'Bearer ' + self.token}),
                     'Accept': 'application/json', 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.getcode() != 200:
                    raise DriveError('http_error', 'API returned an unexpected HTTP status.', response.getcode(), path)
                if safe_url(self.origin, response.geturl()) != url:
                    raise DriveError('redirect_refused', 'Unexpected response URL.')
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise DriveError('response_too_large', 'API response exceeds the 8 MiB safety limit.')
                result = json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise DriveError('http_error', 'API request failed with HTTP ' + str(exc.code) + '; response body omitted.', exc.code, path) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise DriveError('network_error', 'API request failed; check network, TLS trust, and deployment availability.') from None
        except (ValueError, UnicodeError):
            raise DriveError('invalid_response', 'API response was not valid JSON.') from None
        if not isinstance(result, dict):
            raise DriveError('invalid_response', 'API response must be an object.')
        if result.get('error') or result.get('success') is False or ('code' in result and result['code'] not in (0, '0', 200, '200', None)):
            raise DriveError('api_error', 'API reported failure; server details omitted.')
        return result

    def search(self, queries, size=20, max_results=60, offset=0, ancestors=None, spaces=None):
        if not 1 <= len(queries) <= 6 or any(not isinstance(q, str) or not q.strip() or len(q) > 200 for q in queries):
            raise DriveError('invalid_query', 'Supply 1–6 nonempty queries of at most 200 characters.')
        if not 1 <= size <= 100 or not 1 <= max_results <= 200 or not 0 <= offset <= 10000:
            raise DriveError('invalid_limit', 'size: 1–100; max-results: 1–200; offset: 0–10000.')
        ancestors = [valid_guid(g) for g in (ancestors or [])]
        spaces = [valid_guid(g) for g in (spaces or [])]
        found, pages, truncated = {}, [], False
        for query in dict.fromkeys(queries):
            data = self.request('POST', '/api/v1/search/files', {
                'keyword': query, 'searchFields': 'name,file_name', 'ancestorGuids': ancestors,
                'spaceGuids': spaces, 'size': size, 'offset': offset, 'params': ''})
            rows = data.get('results')
            if not isinstance(rows, list):
                raise DriveError('invalid_response', 'Search response is missing results array; this is not zero matches.')
            if len(rows) > size:
                raise DriveError('invalid_response', 'Search returned more rows than requested.')
            cursor = data.get('next')
            has_more = cursor is not None and cursor != '' and cursor is not False
            truncated = truncated or has_more
            pages.append({'query': query, 'next': cursor, 'returned': len(rows), 'has_more': has_more})
            for row in rows:
                if not isinstance(row, dict):
                    raise DriveError('invalid_response', 'Malformed search result.')
                entry = file_summary(self.origin, row.get('source'))
                if row.get('url') is not None:
                    entry['url'] = safe_url(self.origin, row['url'])
                guid = entry['guid']
                if guid in found:
                    found[guid]['matched_queries'].append(query)
                elif len(found) < max_results:
                    found[guid] = {**entry, 'matched_queries': [query]}
                else:
                    truncated = True
        return {'files': list(found.values()), 'pages': pages, 'truncated': truncated,
                'search_fields': ['name', 'file_name'], 'ranking': 'API order; no semantic ranking',
                'pagination_note': 'next is an opaque server value, not converted to offset; no automatic pagination.'}

    def discover(self, queries, folders=None, scope_limit=30, max_results=60):
        folders = list(dict.fromkeys(folders or []))
        if len(folders) > 2 or not 1 <= scope_limit <= 30 or not 1 <= max_results <= 200:
            raise DriveError('invalid_limit', 'Use at most two folders, scope-limit 1–30 and max-results 1–200.')
        folders = [valid_guid(folder) for folder in folders]
        try:
            result = self.search(queries, size=scope_limit, max_results=max_results, ancestors=folders)
            return {**result, 'method': 'name_search', 'incomplete': result['truncated']}
        except DriveError as exc:
            if (exc.code != 'http_error' or exc.http_status is None
                    or not 500 <= exc.http_status <= 599 or exc.api_path != '/api/v1/search/files'):
                raise
            primary_error = {'code': exc.code, 'http_status': exc.http_status}
        scopes = [('folder', folder) for folder in folders] if folders else [('type', kind) for kind in ('desktop', 'used', 'shared')]
        matches, directories, pages = {}, {}, []
        scanned_count = 0
        capped = False
        for scope_type, scope_value in scopes:
            page = self.list_files(folder=scope_value if scope_type == 'folder' else None,
                                   kind=scope_value if scope_type == 'type' else None, limit=scope_limit)
            scanned_count += len(page['files'])
            pages.append({'scope': scope_type, 'value': scope_value, 'limit': scope_limit,
                          **{key: page[key] for key in ('server_returned_count', 'client_truncated', 'nextURL', 'total', 'has_more')}})
            for entry in page['files']:
                guid = entry['guid']
                if entry.get('isFolder') is True or entry.get('type') == 'folder':
                    if guid not in directories and len(directories) < 20:
                        directories[guid] = entry
                name = entry.get('name')
                matched = [query for query in dict.fromkeys(queries) if isinstance(name, str) and query.casefold() in name.casefold()]
                if not matched:
                    continue
                if guid in matches:
                    matches[guid]['matched_queries'] = list(dict.fromkeys(matches[guid]['matched_queries'] + matched))
                elif len(matches) < max_results:
                    matches[guid] = {**entry, 'matched_queries': matched}
                else:
                    capped = True
        return {'method': 'bounded_name_scan', 'primary_search_error': primary_error,
                'files': list(matches.values()), 'folder_candidates': list(directories.values()),
                'scanned_count': scanned_count, 'scopes': pages, 'incomplete': True,
                'limits': {'per_scope': scope_limit, 'max_results': max_results, 'max_folder_candidates': 20},
                'results_capped': capped,
                'note': 'Only bounded first pages were inspected. No matches does not mean no file exists. No recursion or automatic pagination.'}

    def list_files(self, folder=None, kind=None, limit=20, last_timestamp=None):
        if not 1 <= limit <= 100 or (folder is None) == (kind is None) or (kind and kind not in ('desktop', 'used', 'shared')):
            raise DriveError('invalid_arguments', 'Choose exactly one folder or type desktop/used/shared; limit must be 1–100.')
        query = {'limit': limit}
        if folder is not None:
            query['folder'] = valid_guid(folder)
        elif kind != 'desktop':
            query['type'] = kind
        if last_timestamp is not None:
            query['lastTimestamp'] = last_timestamp
        data = self.request('GET', '/api/v1/files', query=query)
        if not isinstance(data.get('list'), list):
            raise DriveError('invalid_response', 'File list response is missing a valid list.')
        raw_next = data.get('nextURL')
        if isinstance(raw_next, dict):
            stamp = raw_next.get('lastTimestamp')
            if stamp is not None and (isinstance(stamp, bool) or not isinstance(stamp, (str, int))):
                raise DriveError('invalid_response', 'Invalid next-page timestamp.')
            next_page = {'lastTimestamp': stamp} if stamp is not None else {}
        elif isinstance(raw_next, str):
            next_page = safe_url(self.origin, raw_next) if raw_next else ''
        elif raw_next is None:
            next_page = None
        else:
            raise DriveError('invalid_response', 'Invalid next-page indicator.')
        count = len(data['list'])
        return {'files': [file_summary(self.origin, row) for row in data['list'][:limit]],
                'lastTimestamp': data.get('lastTimestamp'), 'nextURL': next_page, 'total': data.get('total'),
                'has_more': True if count > limit else (bool(next_page) if raw_next is not None else data.get('hasMore')),
                'server_returned_count': count, 'client_truncated': count > limit,
                'page_full': count >= limit}

    def info(self, guid):
        return file_summary(self.origin, self.request('GET', '/api/v1/files/' + valid_guid(guid)))

    def read(self, guid):
        data = self.request('GET', '/api/v1/files/' + valid_guid(guid) + '/plain-text')
        if not isinstance(data.get('content'), str):
            raise DriveError('invalid_response', 'Plain-text response is missing string content.')
        return {'guid': guid, 'content': data['content']}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise DriveError('invalid_arguments', 'Invalid command arguments; use --help for usage.')


def parser():
    p = Parser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    for command in ('setup', 'status', 'login', 'bind-cli', 'doctor', 'search', 'discover', 'list', 'info', 'read'):
        s = sub.add_parser(command)
        s.add_argument('--address', required=True, help='HTTPS deployment root or /desktop URL; never defaults to a tenant.')
        if command == 'search':
            s.add_argument('--query', action='append', required=True, help='Name keyword; repeat up to six times.')
            s.add_argument('--size', type=int, default=20)
            s.add_argument('--max-results', type=int, default=60)
            s.add_argument('--offset', type=int, default=0)
            s.add_argument('--ancestor', action='append', default=[])
            s.add_argument('--space', action='append', default=[])
        if command == 'discover':
            s.add_argument('--query', action='append', required=True)
            s.add_argument('--folder', action='append', default=[])
            s.add_argument('--scope-limit', '--size', dest='scope_limit', type=int, default=30)
            s.add_argument('--max-results', type=int, default=60)
        if command == 'list':
            group = s.add_mutually_exclusive_group(required=True)
            group.add_argument('--folder')
            group.add_argument('--type', choices=['desktop', 'used', 'shared'])
            s.add_argument('--limit', type=int, default=20)
            s.add_argument('--last-timestamp')
        if command in ('info', 'read'):
            s.add_argument('--guid', required=True)
    return p


def main(argv=None):
    token = None
    try:
        args = parser().parse_args(argv)
        origin = normalize_origin(args.address)
        if args.command == 'setup':
            save_profile(origin)
            result = {'origin': origin, 'configured': True, 'verified': False, 'note': 'Setup does not verify credentials or connectivity.'}
        elif args.command == 'login':
            if not sys.stdin.isatty():
                raise DriveError('terminal_required', 'Run login in a trusted interactive terminal; do not paste tokens into agent chat.')
            token = validate_token(getpass.getpass('Personal Drive token (hidden): '))
            save_profile(origin, token)
            result = {'origin': origin, 'token_stored': True, 'verified': False}
        elif args.command == 'bind-cli':
            result = bind_cli(origin)
        elif args.command == 'status':
            profile = load_profile(origin)
            try:
                token, auth_kind, bound_user_id = credentials(origin)
                auth = 'available_unverified'
            except DriveError as exc:
                if exc.code not in ('auth_required', 'cli_auth_required'):
                    raise
                auth = 'missing'
            result = {'origin': origin, 'configured': profile is not None, 'auth': auth, 'credential_present': token is not None, 'auth_mode': (profile or {}).get('auth_mode', 'bearer'), 'verified': False}
        else:
            token, auth_kind, bound_user_id = credentials(origin)
            client = authenticated_client(origin, token, auth_kind, bound_user_id)
            if args.command == 'doctor':
                client.list_files(kind='used', limit=1)
                result = {'origin': origin, 'authenticated': True, 'verified_endpoint': '/api/v1/files?type=used&limit=1', 'note': 'Only this read endpoint was verified; search, edit and other file permissions are not verified.'}
            elif args.command == 'search':
                result = client.search(args.query, args.size, args.max_results, args.offset, args.ancestor, args.space)
            elif args.command == 'discover':
                result = client.discover(args.query, args.folder, args.scope_limit, args.max_results)
            elif args.command == 'list':
                result = client.list_files(args.folder, args.type, args.limit, args.last_timestamp)
            else:
                result = getattr(client, args.command)(args.guid)
        output = json.dumps({'ok': True, 'data': result}, ensure_ascii=False)
        if token:
            output = output.replace(json.dumps(token, ensure_ascii=False)[1:-1], '[REDACTED]')
        print(output)
        return 0
    except DriveError as exc:
        print(json.dumps({'ok': False, 'error': {'code': exc.code, 'message': exc.message, 'http_status': exc.http_status}}, ensure_ascii=False))
        return 1
    except (EOFError, KeyboardInterrupt):
        print(json.dumps({'ok': False, 'error': {'code': 'cancelled', 'message': 'Operation cancelled.'}}))
        return 1
    except Exception:
        print(json.dumps({'ok': False, 'error': {'code': 'unexpected_error', 'message': 'Operation failed; diagnostic details suppressed to protect credentials.'}}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
