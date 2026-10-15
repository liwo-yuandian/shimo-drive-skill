import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

MODULE = Path(__file__).resolve().parents[1] / 'skills/shimo-drive/scripts/drive_api.py'
sys.path.insert(0, str(MODULE.parent))
spec = importlib.util.spec_from_file_location('drive_api', MODULE)
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
ORIGIN = 'https://tenant.example'


class Response:
    def __init__(self, body, url):
        self.body, self.url = body, url
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, n):
        return self.body[:n]
    def getcode(self):
        return 200
    def geturl(self):
        return self.url


class Opener:
    def __init__(self, values):
        self.values, self.calls = iter(values), []
    def open(self, request, timeout):
        self.calls.append((request, timeout))
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return Response(json.dumps(value).encode(), request.full_url)


def row(guid, name='Name'):
    return {'source': {'guid': guid, 'name': name}, 'url': '/' + guid}


class DriveTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(d.normalize_origin('https://TENANT.example:443/desktop/'), ORIGIN)
        for bad in ('http://tenant.example', 'https://u:p@tenant.example', ORIGIN + '/other', ORIGIN + '/?x=1', ORIGIN + '#x', ORIGIN + '\\evil', ORIGIN + '\n'):
            with self.subTest(bad=bad), self.assertRaises(d.DriveError):
                d.normalize_origin(bad)

    def test_profile_isolation_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'config'
            d.save_profile(ORIGIN, 'secret-token', root)
            self.assertEqual(d.get_token(ORIGIN, root, {}), 'secret-token')
            self.assertIsNone(d.load_profile('https://second.example', root))
            self.assertEqual(d.profile_path(ORIGIN, root).stat().st_mode & 0o777, 0o600)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            with self.assertRaises(d.DriveError):
                d.get_token('https://second.example', root, {})
            with self.assertRaises(d.DriveError):
                d.get_token(ORIGIN, root, {'SHIMO_DRIVE_TOKEN': 'x', 'SHIMO_DRIVE_ORIGIN': 'https://second.example'})
            self.assertEqual(d.get_token(ORIGIN, root, {'SHIMO_DRIVE_TOKEN': 'x', 'SHIMO_DRIVE_ORIGIN': ORIGIN + '/desktop'}), 'x')

    def test_cross_origin_and_null_metadata(self):
        self.assertIsNone(d.file_summary(ORIGIN, {'guid': 'abc'})['permissions'])
        for url in ('https://evil.example/a', '//evil.example/a', 'http://tenant.example/a', 'https://u:p@tenant.example/a'):
            with self.subTest(url=url), self.assertRaises(d.DriveError):
                d.safe_url(ORIGIN, url)
        self.assertEqual(d.safe_url(ORIGIN, '/abc'), ORIGIN + '/abc')

    def test_redirect_refused_before_forwarding_headers(self):
        req = d.urllib.request.Request(ORIGIN + '/api/v1/files', headers={'Authorization': 'Bearer secret'})
        for target in ('https://evil.example/api/v1/files', ORIGIN + '/other'):
            with self.assertRaises(d.DriveError) as caught:
                d.NoRedirect().redirect_request(req, None, 302, 'Found', {}, target)
            self.assertEqual(caught.exception.code, 'redirect_refused')
            self.assertNotIn('secret', str(caught.exception))

    def test_search_dedup_cursor_and_cap(self):
        opener = Opener([{'results': [row('a'), row('b')], 'next': {'opaque': 'cursor'}},
                         {'results': [row('a'), row('c')], 'next': None}])
        result = d.Client(ORIGIN, 'secret', opener).search(['复盘', '反馈'], size=2, max_results=2)
        self.assertEqual([f['guid'] for f in result['files']], ['a', 'b'])
        self.assertEqual(result['files'][0]['matched_queries'], ['复盘', '反馈'])
        self.assertEqual(result['pages'][0]['next'], {'opaque': 'cursor'})
        self.assertTrue(result['truncated'])
        self.assertEqual(len(opener.calls), 2)
        payload = json.loads(opener.calls[0][0].data)
        self.assertEqual(payload['ancestorGuids'], [])
        self.assertEqual(payload['params'], '')
        self.assertEqual(payload['searchFields'], 'name,file_name')
        self.assertEqual(opener.calls[0][1], 20)

    def test_errors_not_empty_results(self):
        for value in ({}, {'results': None}, {'results': [], 'error': 'secret'}, {'code': 403, 'results': []}, {'success': False, 'results': []}):
            with self.subTest(value=value), self.assertRaises(d.DriveError):
                d.Client(ORIGIN, 'secret', Opener([value])).search(['x'])
        result = d.Client(ORIGIN, 'secret', Opener([{'results': []}])).search(['x'])
        self.assertEqual(result['files'], [])
        self.assertFalse(result['truncated'])

    def test_bounds_and_path_escape(self):
        opener = Opener([])
        client = d.Client(ORIGIN, 'secret', opener)
        for queries in ([], ['x'] * 7, ['']):
            with self.assertRaises(d.DriveError):
                client.search(queries)
        for guid in ('../secret', '/abc', 'abc?token=x'):
            with self.assertRaises(d.DriveError):
                client.info(guid)
        self.assertFalse(opener.calls)

    def test_list_read_info(self):
        opener = Opener([{'list': [{'guid': 'a', 'permissions': None}]}, {'content': '正文'}, {'guid': 'a', 'owner': {'email': 'private@example'}}])
        client = d.Client(ORIGIN, 'secret', opener)
        self.assertIsNone(client.list_files(kind='used')['files'][0]['permissions'])
        self.assertEqual(client.read('a')['content'], '正文')
        self.assertNotIn('owner', client.info('a'))
        self.assertIn('type=used', opener.calls[0][0].full_url)

    def test_http_failure_never_echoes_body(self):
        error = urllib.error.HTTPError(ORIGIN, 401, 'secret', {}, io.BytesIO(b'secret'))
        with self.assertRaises(d.DriveError) as caught:
            d.Client(ORIGIN, 'secret', Opener([error])).info('a')
        self.assertNotIn('secret', str(caught.exception))
        self.assertIn('401', str(caught.exception))

    def test_output_redacts_token(self):
        token = 'private-token\"escaped'
        out = io.StringIO()
        with mock.patch.object(d, 'get_token', return_value=token), mock.patch.object(d.Client, 'read', return_value={'content': token}), contextlib.redirect_stdout(out):
            result = d.main(['read', '--address', ORIGIN, '--guid', 'a'])
        self.assertEqual(result, 0)
        self.assertNotIn(token, out.getvalue())
        self.assertTrue(json.loads(out.getvalue())['ok'])

    def test_status_and_doctor_have_distinct_evidence(self):
        out = io.StringIO()
        with mock.patch.object(d, 'load_profile', return_value={'origin': ORIGIN}), mock.patch.object(d, 'get_token', return_value='token'), mock.patch.object(d.Client, 'list_files', return_value={'files': []}) as listing, contextlib.redirect_stdout(out):
            self.assertEqual(d.main(['status', '--address', ORIGIN]), 0)
            listing.assert_not_called()
            status = json.loads(out.getvalue())['data']
            self.assertTrue(status['credential_present'])
            self.assertFalse(status['verified'])
            out.seek(0)
            out.truncate()
            self.assertEqual(d.main(['doctor', '--address', ORIGIN]), 0)
            listing.assert_called_once_with(kind='used', limit=1)
            self.assertTrue(json.loads(out.getvalue())['data']['authenticated'])

    def test_invalid_json_and_unexpected_response_origin(self):
        for response in (Response(b'not json secret', ORIGIN + '/api/v1/files/a'), Response(b'{}', 'https://evil.example/a')):
            opener = mock.Mock()
            opener.open.return_value = response
            with self.assertRaises(d.DriveError) as caught:
                d.Client(ORIGIN, 'secret', opener).info('a')
            self.assertNotIn('secret', str(caught.exception))
            self.assertEqual(opener.open.call_count, 1)

    def test_explicit_cli_binding_saves_reference_not_cookie(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            opener = Opener([{'id': 42}])
            with mock.patch.object(d, 'cli_cookie', return_value='sid=secret; locale=zh'), mock.patch.object(d, 'cli_identity', return_value='42'):
                result = d.bind_cli(ORIGIN, root, {}, opener)
            saved = d.load_profile(ORIGIN, root)
            self.assertEqual(saved['bound_user_id'], '42')
            self.assertEqual(saved['auth_mode'], 'shimocli')
            self.assertNotIn('secret', json.dumps(saved))
            self.assertNotIn('secret', json.dumps(result))
            self.assertEqual(opener.calls[0][0].get_header('Cookie'), 'sid=secret; locale=zh')
            self.assertIsNone(opener.calls[0][0].get_header('Authorization'))

    def test_unbound_status_never_reads_cli_source(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(d, 'cli_cookie') as read:
            with self.assertRaises(d.DriveError):
                d.credentials(ORIGIN, Path(directory), {})
            read.assert_not_called()

    def test_bound_cookie_conflicts_with_environment_bearer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            d.save_profile(ORIGIN, root=root, binding='42')
            with mock.patch.object(d, 'cli_cookie') as read, self.assertRaises(d.DriveError) as caught:
                d.credentials(ORIGIN, root, {'SHIMO_DRIVE_TOKEN': 'secret'})
            self.assertEqual(caught.exception.code, 'auth_conflict')
            read.assert_not_called()
            with self.assertRaises(d.DriveError):
                d.save_profile(ORIGIN, token='secret', root=root)

    def test_cookie_source_exact_host_permissions_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'shimocli' / 'auth.json'
            source.parent.mkdir()
            source.write_text(json.dumps({'cookies': {'tenant.example': 'sid=one', 'other.example': 'sid=other'}}))
            source.chmod(0o600)
            env = {'CODEX_HOME': str(root)}
            self.assertEqual(d.cli_cookie(ORIGIN, env), 'sid=one')
            with self.assertRaises(d.DriveError):
                d.cli_cookie('https://missing.example', env)
            with self.assertRaises(d.DriveError):
                d.cli_cookie(ORIGIN + ':8443', env)
            source.chmod(0o644)
            with self.assertRaises(d.DriveError):
                d.cli_cookie(ORIGIN, env)
            source.chmod(0o600)
            actual = source.with_name('actual.json')
            source.rename(actual)
            source.symlink_to(actual)
            with self.assertRaises(d.DriveError):
                d.cli_cookie(ORIGIN, env)

    def test_identity_mismatch_blocks_business_and_each_request_rechecks(self):
        opener = Opener([{'id': 42}, {'content': 'one'}, {'id': 43}])
        client = d.authenticated_client(ORIGIN, 'sid=secret', 'cookie', '42', opener)
        with mock.patch.object(d, 'cli_identity', return_value='42') as identity:
            self.assertEqual(client.read('a')['content'], 'one')
            with self.assertRaises(d.DriveError) as caught:
                client.read('b')
        self.assertEqual(caught.exception.code, 'identity_mismatch')
        self.assertEqual(identity.call_count, 2)
        paths = [call[0].full_url for call in opener.calls]
        self.assertEqual(paths, [ORIGIN + '/api/v1/users/me', ORIGIN + '/api/v1/files/a/plain-text', ORIGIN + '/api/v1/users/me'])

    def test_failed_binding_does_not_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(d, 'cli_cookie', return_value='sid=secret'), mock.patch.object(d, 'cli_identity', return_value='42'), self.assertRaises(d.DriveError):
                d.bind_cli(ORIGIN, root, {}, Opener([{'id': 43}]))
            self.assertIsNone(d.load_profile(ORIGIN, root))

    def test_cookie_header_injection_rejected(self):
        for value in ('sid=secret\r\nX-Evil: yes', 'not-a-cookie', ''):
            with self.subTest(value=value), self.assertRaises(d.DriveError):
                d.Client(ORIGIN, value, auth_kind='cookie')

    def test_discover_5xx_fallback_is_bounded_and_deduplicated(self):
        failure = urllib.error.HTTPError(ORIGIN, 500, 'error', {}, io.BytesIO(b'private'))
        folder = {'guid': 'folder1', 'name': 'Projects', 'type': 'folder', 'isFolder': True}
        match = {'guid': 'match1', 'name': 'Release PLAN', 'parentGuid': 'folder1'}
        opener = Opener([failure, {'list': [folder, match], 'nextURL': {}},
                         {'list': [match, folder], 'nextURL': {'lastTimestamp': '100'}, 'total': 99},
                         {'list': [{'guid': 'other', 'name': 'unrelated'}]}])
        result = d.Client(ORIGIN, 'secret', opener).discover(['plan', 'release'])
        self.assertEqual(result['method'], 'bounded_name_scan')
        self.assertEqual(result['primary_search_error']['http_status'], 500)
        self.assertTrue(result['incomplete'])
        self.assertEqual(result['scanned_count'], 5)
        self.assertEqual(len(result['files']), 1)
        self.assertEqual(result['files'][0]['matched_queries'], ['plan', 'release'])
        self.assertEqual(len(result['folder_candidates']), 1)
        self.assertEqual(len(opener.calls), 4)
        desktop = d.urllib.parse.parse_qs(d.urllib.parse.urlsplit(opener.calls[1][0].full_url).query)
        self.assertNotIn('type', desktop)
        self.assertEqual(desktop['limit'], ['30'])

    def test_discover_never_falls_back_on_auth_failure(self):
        for status in (401, 403):
            opener = Opener([urllib.error.HTTPError(ORIGIN, status, 'private', {}, None)])
            with self.subTest(status=status), self.assertRaises(d.DriveError) as caught:
                d.Client(ORIGIN, 'secret', opener).discover(['plan'])
            self.assertEqual(caught.exception.http_status, status)
            self.assertEqual(len(opener.calls), 1)

    def test_discover_identity_5xx_is_not_a_search_fallback(self):
        opener = Opener([urllib.error.HTTPError(ORIGIN, 500, 'private', {}, None)])
        client = d.authenticated_client(ORIGIN, 'sid=secret', 'cookie', '42', opener)
        with mock.patch.object(d, 'cli_identity', return_value='42'), self.assertRaises(d.DriveError):
            client.discover(['plan'])
        self.assertEqual(len(opener.calls), 1)
        self.assertTrue(opener.calls[0][0].full_url.endswith('/users/me'))

    def test_discover_explicit_folders_do_not_expand_scope(self):
        failure = urllib.error.HTTPError(ORIGIN, 503, 'error', {}, None)
        opener = Opener([failure, {'list': []}, {'list': []}])
        result = d.Client(ORIGIN, 'secret', opener).discover(['plan'], folders=['one', 'two'])
        self.assertEqual(json.loads(opener.calls[0][0].data)['ancestorGuids'], ['one', 'two'])
        self.assertEqual([scope['value'] for scope in result['scopes']], ['one', 'two'])
        for call, folder in zip(opener.calls[1:], ['one', 'two']):
            query = d.urllib.parse.parse_qs(d.urllib.parse.urlsplit(call[0].full_url).query)
            self.assertEqual(query['folder'], [folder])
            self.assertNotIn('type', query)
        self.assertTrue(result['incomplete'])
        self.assertEqual(result['files'], [])
        self.assertEqual(len(opener.calls), 3)

    def test_desktop_client_cap_and_next_object_whitelist(self):
        records = [{'guid': 'file' + str(i), 'name': 'test'} for i in range(35)]
        opener = Opener([{'list': records, 'nextURL': {'lastTimestamp': '100', 'private': 'secret'}, 'total': 99}])
        result = d.Client(ORIGIN, 'secret', opener).list_files(kind='desktop', limit=30)
        self.assertEqual(len(result['files']), 30)
        self.assertEqual(result['server_returned_count'], 35)
        self.assertTrue(result['client_truncated'])
        self.assertEqual(result['nextURL'], {'lastTimestamp': '100'})
        self.assertNotIn('secret', json.dumps(result))
        self.assertTrue(result['has_more'])
        self.assertEqual(len(opener.calls), 1)

    def test_list_next_url_rejects_other_origin_without_following(self):
        opener = Opener([{'list': [], 'nextURL': 'https://other.example/page'}])
        with self.assertRaises(d.DriveError):
            d.Client(ORIGIN, 'secret', opener).list_files(kind='desktop')
        self.assertEqual(len(opener.calls), 1)

    def test_discover_success_does_not_scan_lists(self):
        opener = Opener([{'results': [row('a')], 'next': ''}])
        result = d.Client(ORIGIN, 'secret', opener).discover(['name'])
        self.assertEqual(result['method'], 'name_search')
        self.assertEqual(len(opener.calls), 1)
        self.assertFalse(result['incomplete'])

    def test_permission_values_are_strict_boolean_whitelist(self):
        item = {'guid': 'a', 'permissions': {'user': {'email': 'private@example'}},
                'permissionsAndReasons': {'canRead': {'value': True, 'reason': 'private reason'},
                                          'canEdit': {'value': False, 'user': {'id': 99}},
                                          'canDelete': {'value': True}}}
        result = d.file_summary(ORIGIN, item)
        self.assertEqual(result['permissions'], {'canRead': True, 'canEdit': False})
        encoded = json.dumps(result)
        for private in ('private@example', 'private reason', 'canDelete', '99'):
            self.assertNotIn(private, encoded)
        for unknown in (None, 'true', 'false', 0, 1, {}, []):
            with self.subTest(unknown=unknown):
                item['permissionsAndReasons'] = {'canRead': {'value': unknown}}
                self.assertEqual(d.file_summary(ORIGIN, item)['permissions'], {'canRead': None, 'canEdit': None})
        self.assertIsNone(d.file_summary(ORIGIN, {'guid': 'a'})['permissions'])
        self.assertIsNone(d.file_summary(ORIGIN, {'guid': 'a', 'permissionsAndReasons': None})['permissions'])

    def test_parser_failure_is_json_and_does_not_echo_args(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = d.main(['login', '--address', ORIGIN, '--token', 'private-token'])
        self.assertEqual(code, 1)
        self.assertNotIn('private-token', out.getvalue())
        self.assertEqual(json.loads(out.getvalue())['error']['code'], 'invalid_arguments')


if __name__ == '__main__':
    unittest.main()
