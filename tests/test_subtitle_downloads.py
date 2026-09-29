import json
from pathlib import Path
import pytest
import requests

import subtitle_downloads as subs

SRT = '1\n00:00:01,000 --> 00:00:03,000\nHello world.\n\n'
CREDENTIALS = {'username': 'test-user', 'password': 'private-password', 'api_key': 'private-key'}
IDENTITY = {'kind': 'episode', 'title': 'Example Show', 'show_tmdb_id': 42, 'season': 1, 'episode': 2}


def candidate(release='Example.Show.S01E02.1080p.WEB-GROUP', episode=2, language='en'):
    return {'attributes': {'language': language, 'release': release, 'download_count': 10,
            'feature_details': {'parent_tmdb_id': 42, 'season_number': 1, 'episode_number': episode},
            'files': [{'file_id': 12, 'file_name': release + '.srt'}]}}


def test_hash_uses_file_size_and_both_end_blocks(tmp_path):
    video = tmp_path / 'video.mkv'
    video.write_bytes(b'\x01' * 65536 + b'\x02' * 65536)
    expected = (131072 + 8192 * (0x0101010101010101 + 0x0202020202020202)) & ((1 << 64) - 1)
    assert subs.movie_hash(video) == f'{expected:016x}'
    video.write_bytes(b'small fixture')
    assert subs.movie_hash(video) is None


def test_matching_rejects_other_episodes_languages_and_release_cuts():
    release = 'Example.Show.S01E02.1080p.WEB-GROUP'
    assert subs.ranked_candidates([candidate()], IDENTITY, release, 'eng') == [12]
    assert subs.ranked_candidates([candidate(episode=3)], IDENTITY, release, 'eng') == []
    assert subs.ranked_candidates([candidate(language='hi')], IDENTITY, release, 'eng') == []
    assert subs.ranked_candidates([candidate(release='Different Cut 720p BluRay')], IDENTITY, release, 'eng') == []
    wrong = candidate()
    wrong['attributes']['feature_details']['parent_tmdb_id'] = 99
    assert subs.ranked_candidates([wrong], IDENTITY, release, 'eng') == []
    assert subs.identity_params({**IDENTITY, 'end_episode': 3}) is None


def test_embedded_and_external_subtitles_skip_only_the_preferred_full_language(tmp_path):
    path = tmp_path / 'video.mkv'
    path.write_bytes(b'video')
    path.with_suffix('.hin.srt').write_text(SRT)
    assert not subs.has_subtitles(path, 'eng', [])
    path.with_suffix('.eng.forced.srt').write_text(SRT)
    assert not subs.has_subtitles(path, 'eng', [])
    assert subs.has_subtitles(path, 'eng', [{'Type': 'Subtitle', 'Language': 'eng'}])
    assert not subs.has_subtitles(path, 'eng', [{'Type': 'Subtitle', 'Language': 'eng', 'IsForced': True}])
    path.with_suffix('.eng.srt').write_text(SRT)
    assert subs.has_subtitles(path, 'eng', [])


def test_fetch_writes_valid_subtitle_beside_exact_version_without_overwriting(tmp_path):
    class Client:
        def search(self, params):
            assert params['parent_tmdb_id'] == 42 and params['episode_number'] == 2
            return [candidate()]
        def download(self, file_id):
            assert file_id == 12
            return SRT, subs.parse_srt(SRT)
    path = tmp_path / 'canonical version.mkv'
    path.write_bytes(b'video fixture')
    result = subs.fetch_one(Client(), path, IDENTITY, candidate()['attributes']['release'], 'eng', [])
    assert result['status'] == 'downloaded'
    assert path.with_suffix('.eng.srt').read_text() == SRT
    assert subs.fetch_one(Client(), path, IDENTITY, 'same', 'eng', [])['status'] == 'existing'
    assert path.read_bytes() == b'video fixture'


def test_exact_hash_match_is_used_before_title_search(tmp_path):
    class Client:
        def search(self, params):
            assert params['moviehash_match'] == 'only' and 'query' not in params
            return [candidate(release='Different provider filename')]
        def download(self, file_id):
            return SRT, subs.parse_srt(SRT)
    path = tmp_path / 'video.mkv'
    path.write_bytes(b'\x01' * 131072)
    assert subs.fetch_one(Client(), path, IDENTITY, 'Original release', 'eng', [])['status'] == 'downloaded'


def test_provider_quota_and_errors_are_redacted():
    class Session:
        headers = {}
        def request(self, *args, **kwargs):
            response = requests.Response()
            response.status_code = 429
            return response
    client = subs.OpenSubtitles(CREDENTIALS, Session())
    with pytest.raises(subs.SubtitleError, match='quota') as error:
        client.login()
    assert error.value.status == 'quota_reached'
    assert CREDENTIALS['password'] not in str(error.value)


def test_provider_rejects_login_host_redirect():
    client = subs.OpenSubtitles(CREDENTIALS)
    client.request = lambda *args, **kwargs: {'token': 'secret-token', 'base_url': 'attacker.example'}
    with pytest.raises(subs.SubtitleError, match='invalid login'):
        client.login()
    assert 'Authorization' not in client.session.headers


def test_download_does_not_follow_unsafe_url_or_accept_html(monkeypatch):
    client = subs.OpenSubtitles(CREDENTIALS)
    client.request = lambda *args, **kwargs: {'link': 'http://127.0.0.1/internal'}
    with pytest.raises(subs.SubtitleError, match='invalid download'):
        client.download(12)
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, size): yield b'<html>Login required</html>'
    client.request = lambda *args, **kwargs: {'link': 'https://dl.opensubtitles.com/file.srt'}
    def get(url, **kwargs):
        assert 'headers' not in kwargs and not kwargs['allow_redirects']
        return Response()
    monkeypatch.setattr(subs.requests, 'get', get)
    with pytest.raises(subs.SubtitleError, match='valid SRT'):
        client.download(12)


def test_account_connection_is_admin_only_and_never_returns_secrets(web, monkeypatch):
    from test_jobs import signed_client
    monkeypatch.setattr(subs.OpenSubtitles, 'login', lambda self: {})
    monkeypatch.setattr(web, '_start_subtitle_scan', lambda: True)
    member = signed_client(web)
    assert member.post('/api/subtitles/account', json=CREDENTIALS).status_code == 403
    admin = signed_client(web, admin=True)
    response = admin.post('/api/subtitles/account', json=CREDENTIALS)
    assert response.status_code == 200 and response.json['connected']
    stored = web.ROOT / 'data/subtitles/account.json'
    assert stored.stat().st_mode & 0o777 == 0o600
    assert json.loads(stored.read_text()) == CREDENTIALS
    public = admin.get('/api/subtitles/account').get_data(as_text=True)
    assert CREDENTIALS['password'] not in public and CREDENTIALS['api_key'] not in public
    assert CREDENTIALS['password'] not in admin.get('/api/settings').get_data(as_text=True)
    assert admin.delete('/api/subtitles/account').json['connected'] is False


def test_invalid_account_is_not_saved(web, monkeypatch):
    from test_jobs import signed_client
    def reject(self):
        raise subs.SubtitleError('Rejected.', 'authentication_required')
    monkeypatch.setattr(subs.OpenSubtitles, 'login', reject)
    admin = signed_client(web, admin=True)
    assert admin.post('/api/subtitles/account', json=CREDENTIALS).status_code == 400
    assert not (web.ROOT / 'data/subtitles/account.json').exists()


def test_automatic_fetch_happens_before_upload_and_does_not_fail_video(web, monkeypatch):
    from test_jobs import make_job, fake_transfers, JOB_ID
    result, directory = make_job(web, download_complete=True, destination=web.LIBRARY_REMOTE)
    monkeypatch.setattr(subs, 'provider_status', lambda root: {'connected': True})
    def fetch(root, files, language, *args):
        assert directory.exists() and language == 'eng'
        assert files[0]['path'] == directory / 'Example.mp4'
        (directory / 'Example.eng.srt').write_text(SRT)
        return {'status': 'quota_reached', 'downloaded': 1, 'files': [str(directory / 'Example.eng.srt')]}
    monkeypatch.setattr(subs, 'fetch_batch', fetch)
    monkeypatch.setattr(web, '_refresh_library_mount', lambda: None)
    monkeypatch.setattr(web, '_find_library_item', lambda *args: 'movie-id')
    monkeypatch.setattr(web.threading.Timer, 'start', lambda *args: None)
    commands = fake_transfers(monkeypatch, web, [0])
    web._run_download(JOB_ID, result, web.LIBRARY_REMOTE, None)
    assert web.JOBS[JOB_ID]['status'] == 'complete'
    assert web.JOBS[JOB_ID]['subtitles_summary']['status'] == 'quota_reached'
    assert 'files' not in web.JOBS[JOB_ID]['subtitles_summary']
    assert commands[0][1][2] == str(directory)


def test_scan_uploads_only_subtitle_to_matching_remote_folder(web, monkeypatch):
    folder = web.ROOT / 'movies'
    folder.mkdir()
    path = folder / 'Example 2020.mkv'
    path.write_bytes(b'video')
    monkeypatch.setattr(web, 'LIBRARY_PATH', folder)
    monkeypatch.setattr(web, 'JELLYFIN_API_KEY', 'server-key')
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {'Items': [{'Type': 'Movie', 'Name': 'Example', 'Id': 'a'*32,
                'MediaSources': [{'Id': 'a'*32, 'Path': str(path), 'MediaStreams': []}]}]}
    monkeypatch.setattr(web.requests, 'get', lambda *args, **kwargs: Response())
    def fetch(client, video, identity, release, language, streams, output_dir):
        target = Path(output_dir) / 'Example 2020.eng.srt'
        target.write_text(SRT)
        return {'status': 'downloaded', 'path': str(target)}
    monkeypatch.setattr(subs, 'fetch_one', fetch)
    commands = []
    monkeypatch.setattr(web.subprocess, 'run', lambda command, **kwargs: commands.append(command))
    monkeypatch.setattr(web, '_refresh_library_mount', lambda: None)
    monkeypatch.setattr(web, '_refresh_library', lambda: None)
    web._scan_library_subtitles()
    assert commands[0][0:2] == ['rclone', 'copyto']
    assert commands[0][3] == web.LIBRARY_REMOTE + '/Example 2020.eng.srt'
    assert '--immutable' in commands[0]
    assert web._subtitle_scan_status()['downloaded'] == 1
    assert path.read_bytes() == b'video'
