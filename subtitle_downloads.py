"""Fetch release-matched subtitles and keep credentials outside public settings."""
from __future__ import annotations

from difflib import SequenceMatcher
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
from urllib.parse import urlsplit

import requests

from clips.transcript import parse_srt

API_HOSTS = {'api.opensubtitles.com', 'vip-api.opensubtitles.com'}
LANGUAGES = {'eng': 'en', 'mal': 'ml', 'tam': 'ta', 'tel': 'te', 'hin': 'hi',
             'kan': 'kn', 'ben': 'bn', 'mar': 'mr', 'pan': 'pa', 'spa': 'es',
             'fre': 'fr', 'fra': 'fr', 'ger': 'de', 'deu': 'de', 'jpn': 'ja',
             'kor': 'ko', 'zho': 'zh', 'chi': 'zh', 'ara': 'ar', 'por': 'pt',
             'ita': 'it', 'rus': 'ru', 'dut': 'nl', 'nld': 'nl'}
MAX_SUBTITLE_BYTES = 5_000_000


class SubtitleError(RuntimeError):
    def __init__(self, message, status='unavailable'):
        super().__init__(message)
        self.status = status


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_credentials(root):
    try:
        saved = json.loads((Path(root) / 'data/subtitles/account.json').read_text())
    except (OSError, ValueError):
        saved = {}
    for name in ('api_key', 'username', 'password'):
        saved[name] = os.environ.get('POPCORN_OPENSUBTITLES_' + name.upper()) or saved.get(name, '')
    return saved


def configured(credentials):
    return all(credentials.get(name) for name in ('api_key', 'username', 'password'))


def provider_status(root):
    credentials = load_credentials(root)
    return {'connected': configured(credentials), 'username': credentials.get('username', ''),
            'provider': 'OpenSubtitles'}


class OpenSubtitles:
    def __init__(self, credentials, session=None):
        self.credentials = credentials
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.session.headers.update({'Api-Key': credentials.get('api_key', ''),
                                     'User-Agent': 'Popcorn v1.0', 'Accept': 'application/json'})
        self.base = 'https://api.opensubtitles.com/api/v1'
        self.logged_in = False

    def request(self, method, path, **kwargs):
        try:
            response = self.session.request(method, self.base + path, timeout=(5, 20),
                                            allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise SubtitleError('The subtitle service is temporarily unavailable.') from None
        if response.status_code in {406, 429}:
            raise SubtitleError('Subtitle download quota reached. Try again after it resets.', 'quota_reached')
        if response.status_code in {401, 403}:
            raise SubtitleError('OpenSubtitles rejected the account or API key. Reconnect in Settings.', 'authentication_required')
        if not 200 <= response.status_code < 300:
            raise SubtitleError('The subtitle service is temporarily unavailable.')
        try:
            result = response.json()
        except ValueError:
            raise SubtitleError('The subtitle service returned an invalid response.') from None
        if not isinstance(result, dict):
            raise SubtitleError('The subtitle service returned an invalid response.')
        return result

    def login(self):
        if not configured(self.credentials):
            raise SubtitleError('Connect OpenSubtitles in Settings to download missing subtitles.', 'not_configured')
        result = self.request('POST', '/login', json={name: self.credentials[name] for name in ('username', 'password')})
        token = result.get('token')
        host = result.get('base_url', 'api.opensubtitles.com')
        if not isinstance(host, str) or host not in API_HOSTS or not isinstance(token, str) or not token:
            raise SubtitleError('The subtitle service returned an invalid login response.')
        self.base = f'https://{host}/api/v1'
        self.session.headers['Authorization'] = f'Bearer {token}'
        self.logged_in = True
        return result.get('user', {})

    def search(self, params):
        if not self.logged_in:
            self.login()
        data = self.request('GET', '/subtitles', params=params).get('data', [])
        if not isinstance(data, list):
            raise SubtitleError('The subtitle service returned an invalid search response.')
        return data

    def download(self, file_id):
        result = self.request('POST', '/download', json={'file_id': file_id, 'sub_format': 'srt'})
        link = result.get('link', '')
        parsed = urlsplit(link)
        host = parsed.hostname or ''
        if parsed.scheme != 'https' or parsed.username or parsed.password or not (
                host == 'opensubtitles.com' or host.endswith('.opensubtitles.com')):
            raise SubtitleError('The subtitle service returned an invalid download address.')
        # Do not send the account token/API key to a file download URL.
        try:
            with requests.get(link, timeout=(5, 20), stream=True, allow_redirects=False) as response:
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    content.extend(chunk)
                    if len(content) > MAX_SUBTITLE_BYTES:
                        raise SubtitleError('The subtitle file is too large.')
        except requests.RequestException:
            raise SubtitleError('The subtitle file could not be downloaded.') from None
        text = bytes(content).decode('utf-8-sig', errors='replace')
        segments = parse_srt(text)
        if not segments or any(s.end <= s.start or s.start < 0 for s in segments):
            raise SubtitleError('The provider did not return a valid SRT subtitle file.')
        return text, segments


def movie_hash(path):
    """OpenSubtitles' 64-bit size + first/last 64 KiB checksum."""
    path = Path(path)
    size = path.stat().st_size
    if size < 131072:
        return None
    with path.open('rb') as stream:
        first = stream.read(65536)
        stream.seek(-65536, 2)
        last = stream.read(65536)
    if len(first) != 65536 or len(last) != 65536:
        return None
    value = size + sum(struct.unpack('<8192Q', first)) + sum(struct.unpack('<8192Q', last))
    return f'{value & ((1 << 64) - 1):016x}'


def language_code(language):
    return LANGUAGES.get(language, language if len(language) == 2 else None)


def has_subtitles(path, language, streams=None):
    path = Path(path)
    preferred = language_code(language)
    for sidecar in path.parent.iterdir():
        if sidecar.suffix.lower() not in {'.srt', '.ass', '.ssa', '.vtt', '.sub', '.idx'}:
            continue
        if sidecar.stem != path.stem and not sidecar.name.startswith(path.stem + '.'):
            continue
        suffix = sidecar.name[len(path.stem):].lower().split('.')
        if 'forced' not in suffix and (sidecar.stem == path.stem or language in suffix or preferred in suffix):
            return True
    if streams is None:
        binary = '/usr/lib/jellyfin-ffmpeg/ffprobe'
        if not Path(binary).exists():
            binary = shutil.which('ffprobe')
        try:
            result = subprocess.run([binary, '-v', 'error', '-select_streams', 's', '-show_streams',
                                     '-of', 'json', str(path)], capture_output=True, timeout=20, check=True)
            streams = json.loads(result.stdout).get('streams', [])
        except (OSError, TypeError, ValueError, subprocess.SubprocessError):
            streams = []
    for stream in streams:
        if stream.get('Type', 'Subtitle') != 'Subtitle':
            continue
        lang = stream.get('Language') or stream.get('tags', {}).get('language', '')
        if (lang == language or (preferred and language_code(lang) == preferred)) and not (
                stream.get('IsForced') or stream.get('disposition', {}).get('forced')):
            return True
    return False


def normalized(value):
    return re.sub(r'[^a-z0-9]', '', value.casefold())


def identity_params(identity):
    if identity.get('kind') == 'episode':
        if identity.get('end_episode') is not None:
            return None  # A combined episode requires a combined, correctly timed subtitle.
        params = {'type': 'episode', 'season_number': identity['season'], 'episode_number': identity['episode']}
        if identity.get('show_tmdb_id'):
            params['parent_tmdb_id'] = identity['show_tmdb_id']
        else:
            params['query'] = identity['title']
    else:
        params = {'type': 'movie'}
        if identity.get('imdb_id'):
            params['imdb_id'] = str(identity['imdb_id']).removeprefix('tt')
        elif identity.get('tmdb_id'):
            params['tmdb_id'] = identity['tmdb_id']
        else:
            params['query'] = identity['title']
            if identity.get('year'):
                params['year'] = identity['year']
    return params


def matching_identity(attributes, identity):
    details = attributes.get('feature_details', {})
    if identity.get('kind') == 'episode':
        return (str(details.get('season_number')) == str(identity['season'])
                and str(details.get('episode_number')) == str(identity['episode'])
                and (str(details.get('parent_tmdb_id')) == str(identity['show_tmdb_id'])
                     if identity.get('show_tmdb_id') else normalized(details.get('parent_title', '')) == normalized(identity['title'])))
    if identity.get('imdb_id'):
        return str(details.get('imdb_id')) == str(identity['imdb_id']).removeprefix('tt')
    if identity.get('tmdb_id'):
        return str(details.get('tmdb_id')) == str(identity['tmdb_id'])
    return (normalized(details.get('title', '')) == normalized(identity['title'])
            and (not identity.get('year') or str(details.get('year')) == str(identity['year'])))


def ranked_candidates(candidates, identity, release, language, exact_hash=False):
    preferred = language_code(language)
    ranked = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        attrs = candidate.get('attributes', {})
        if not isinstance(attrs, dict):
            continue
        files = attrs.get('files', [])
        if not isinstance(files, list) or not files or not isinstance(files[0], dict):
            continue
        if attrs.get('language') != preferred or attrs.get('foreign_parts_only') or len(files) != 1:
            continue
        if not exact_hash and not matching_identity(attrs, identity):
            continue
        names = [attrs.get('release', ''), Path(files[0].get('file_name', '')).stem]
        similarity = max(SequenceMatcher(None, normalized(release), normalized(name)).ratio() for name in names)
        # Title matching alone cannot establish which cut/timeline a subtitle uses.
        if not exact_hash and similarity < 0.8:
            continue
        if not isinstance(files[0].get('file_id'), int):
            continue
        score = (similarity, bool(attrs.get('from_trusted')), not attrs.get('machine_translated'), attrs.get('download_count', 0))
        ranked.append((score, files[0]['file_id']))
    return [file_id for _, file_id in sorted(ranked, reverse=True)]


def fetch_one(client, path, identity, release, language, streams=None, output_dir=None):
    path = Path(path)
    if has_subtitles(path, language, streams):
        return {'status': 'existing'}
    lang = language_code(language)
    if not lang:
        return {'status': 'unsupported_language'}
    checksum = movie_hash(path)
    candidates = []
    if checksum:
        found = client.search({'moviehash': checksum, 'moviehash_match': 'only', 'languages': lang})
        candidates = ranked_candidates(found, identity, release, language, exact_hash=True)
    if not candidates:
        params = identity_params(identity)
        if params:
            found = client.search({**params, 'languages': lang, 'order_by': 'download_count', 'order_direction': 'desc'})
            candidates = ranked_candidates(found, identity, release, language)
    if not candidates:
        return {'status': 'no_match'}
    text, segments = client.download(candidates[0])
    if identity.get('duration') and segments[-1].end > identity['duration'] + 30:
        return {'status': 'no_match'}
    target = Path(output_dir or path.parent) / (path.stem + f'.{language}.srt')
    target.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite a subtitle already present beside this exact version.
    fd, temporary = tempfile.mkstemp(prefix='subtitle-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            return {'status': 'existing'}
    finally:
        os.unlink(temporary)
    return {'status': 'downloaded', 'path': str(target)}


def fetch_batch(root, files, language, check_cancelled=lambda: None, on_progress=lambda *args: None):
    credentials = load_credentials(root)
    if not configured(credentials):
        return {'status': 'not_configured', 'downloaded': 0, 'existing': 0, 'no_match': 0}
    client = OpenSubtitles(credentials)
    summary = {'status': 'complete', 'downloaded': 0, 'existing': 0, 'no_match': 0, 'files': []}
    for index, file in enumerate(files):
        check_cancelled()
        on_progress(index + 1, len(files))
        try:
            result = fetch_one(client, file['path'], file['identity'], file['release'], language,
                               file.get('streams'), file.get('output_dir'))
        except SubtitleError as exc:
            summary['status'] = exc.status
            summary['message'] = str(exc)
            break
        except (OSError, ValueError, TypeError, KeyError):
            summary['status'] = 'unavailable'
            summary['message'] = 'A video or subtitle file could not be read.'
            break
        summary[result['status']] = summary.get(result['status'], 0) + 1
        if result.get('path'):
            summary['files'].append(result['path'])
    return summary
