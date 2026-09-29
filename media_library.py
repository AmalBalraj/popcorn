"""Source-independent TV identities, episode parsing and recoverable import plans."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import subprocess
import time
import uuid
import unicodedata
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from xml.etree import ElementTree as ET

VIDEO_EXTENSIONS = {'.avi', '.m2ts', '.m4v', '.mkv', '.mov', '.mp4', '.mpeg', '.mpg', '.ts', '.webm', '.wmv'}
EPISODE = re.compile(r'\bS(\d{1,2})[ ._-]*E(\d{1,3})(?:[ ._-]*-?[ ._-]*E(\d{1,3}))?\b|\b(\d{1,2})x(\d{1,3})\b', re.I)
SEASON = re.compile(r'\b(?:S|Season[ ._-]*)(\d{1,2})\b', re.I)
LOOSE_EPISODE = re.compile(r'\b(?:EP?|Episode)[ ._-]*(\d{1,3})\b', re.I)
SPECIAL = re.compile(r'\b(?:bonus|specials?|deleted|discarded)\b', re.I)


class IdentificationRequired(ValueError):
    """Keep completed files local until their identity can be established."""


def name_key(value):
    value = unicodedata.normalize('NFKD', value).casefold()
    return re.sub(r'[^a-z0-9]', '', value)


def safe_name(value):
    value = re.sub(r'[\x00-\x1f/\\:*?"<>|]', ' ', str(value))
    return re.sub(r'\s+', ' ', value).strip(' .')[:160] or 'Untitled'


def show_query(value):
    value = re.sub(r'^(?:copy of\s+|www\.\S+\s*-\s*)', '', value, flags=re.I)
    value = value.replace('.', ' ').replace('_', ' ')
    marker = re.search(r'\b(?:S\d{1,2}(?:\s*E\d{1,3})?|Season\s*\d+|\d{1,2}x\d{1,3}|EP?\s*\d+|19\d{2}|20\d{2}|2160p|1080p|720p|480p|Complete)\b', value, re.I)
    return (value[:marker.start()] if marker else value).strip(' -[]()')


def coordinates(value, fallback_season=None):
    text = str(value).replace('_', ' ').replace('.', ' ')
    match = EPISODE.search(text)
    if match:
        season = int(match[1] or match[4])
        episode = int(match[2] or match[5])
        end = int(match[3]) if match[3] else None
        if end is not None and end < episode:
            raise IdentificationRequired('The episode range is reversed.')
        return season, episode, end
    if SPECIAL.search(text):
        return None  # Bonus numbering is not the metadata provider's specials numbering.
    match = LOOSE_EPISODE.search(text)
    if match:
        season_match = SEASON.search(text)
        season = int(season_match[1]) if season_match else fallback_season
        if season is not None:
            return int(season), int(match[1]), None
    return None


def looks_like_tv(title, paths=()):
    return bool(EPISODE.search(title.replace('_', ' ').replace('.', ' ')) or SEASON.search(title.replace('.', ' '))
                or any(coordinates(str(p), 1) or SPECIAL.search(Path(p).stem.replace('_', ' ')) for p in paths))


def _curl(url, headers=None, resolve=None, limit=8_000_000):
    # Credentials go through stdin; failed URLs/headers are never included in errors.
    config = 'url = ' + json.dumps(url) + '\n'
    for key, value in (headers or {}).items():
        config += 'header = ' + json.dumps(f'{key}: {value}') + '\n'
    command = ['curl', '--fail', '--silent', '--show-error', '--connect-timeout', '4',
               '--max-time', '10', '--retry', '2', '--retry-all-errors', '--retry-delay', '1',
               '--retry-max-time', '20', '--max-filesize', str(limit), '--config', '-']
    if resolve:
        command.extend(['--resolve', resolve])
    try:
        result = subprocess.run(command, input=config.encode(), stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=25, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('The metadata service is temporarily unavailable.') from exc
    if result.returncode or len(result.stdout) > limit:
        raise RuntimeError('The metadata service is temporarily unavailable.')
    return result.stdout


def metadata_bytes(url, headers=None, cache_dir=None):
    host = urlsplit(url).hostname
    if host not in {'api.themoviedb.org', 'image.tmdb.org'} or urlsplit(url).scheme != 'https':
        raise ValueError('Unsupported metadata host.')
    cache_dir = Path(cache_dir) if cache_dir else None
    dns_file = cache_dir / f'{host}.dns.json' if cache_dir else None
    cached = []
    fresh = False
    if dns_file and dns_file.exists():
        try:
            saved = json.loads(dns_file.read_text())
            cached = saved.get('addresses', [])
            fresh = time.time() - saved.get('checked_at', 0) < 300
        except (ValueError, OSError):
            pass
    started = time.monotonic()
    if fresh and cached:
        try:
            return _curl(url, headers, f'{host}:443:{cached[0]}')
        except RuntimeError:
            fresh = False
    addresses = list(cached)
    try:
        payload = json.loads(_curl('https://cloudflare-dns.com/dns-query?' + urlencode({'name': host, 'type': 'A'}),
                                   {'Accept': 'application/dns-json'}))
        addresses += [a['data'] for a in payload.get('Answer', []) if a.get('type') == 1]
    except (RuntimeError, ValueError):
        pass
    addresses = list(dict.fromkeys(str(ipaddress.IPv4Address(a)) for a in addresses))
    candidates = [f'{host}:443:{a}' for a in addresses[:8]] + [None]
    for resolve in candidates:
        if time.monotonic() - started > 45:
            break
        try:
            data = _curl(url, headers, resolve)
            if dns_file and resolve:
                cache_dir.mkdir(parents=True, exist_ok=True)
                temporary = dns_file.with_suffix(f'.{uuid.uuid4().hex}.tmp')
                temporary.write_text(json.dumps({'addresses': [resolve.rsplit(':', 1)[1]], 'checked_at': time.time()}))
                temporary.replace(dns_file)
            return data
        except RuntimeError:
            continue
    raise RuntimeError('The metadata service is temporarily unavailable. Finished files are kept for retry.')


def tmdb_get(key, path, cache_dir=None, **params):
    if not key:
        raise RuntimeError('TMDB is not configured.')
    cached = None
    if cache_dir:
        cache_path = Path(cache_dir) / (hashlib.sha256((path + json.dumps(params, sort_keys=True)).encode()).hexdigest() + '.json')
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text())
                if time.time() - cached['fetched_at'] < (3600 if path.startswith('search/') else 86400):
                    return cached['data']
            except (ValueError, KeyError, OSError):
                cached = None
    headers = {'Accept': 'application/json'}
    if key.startswith('eyJ'):
        headers['Authorization'] = f'Bearer {key}'
    else:
        params['api_key'] = key
    url = 'https://api.themoviedb.org/3/' + path.lstrip('/') + '?' + urlencode(params)
    try:
        data = json.loads(metadata_bytes(url, headers, cache_dir))
    except RuntimeError:
        if cached and not path.startswith('search/'):
            return cached['data']
        raise
    if cache_dir:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(f'.{uuid.uuid4().hex}.tmp')
        temporary.write_text(json.dumps({'fetched_at': time.time(), 'data': data}))
        temporary.replace(cache_path)
    return data


def resolve_show(query, get, tmdb_id=None):
    if tmdb_id:
        return get(f'tv/{int(tmdb_id)}')
    query = show_query(query)
    results = get('search/tv', query=query, include_adult='false').get('results', [])
    matches = [r for r in results if name_key(query) in {name_key(r.get('name', '')), name_key(r.get('original_name', ''))}]
    if len(matches) != 1:
        raise IdentificationRequired('Choose the correct TV show before these files are imported.')
    return get(f"tv/{matches[0]['id']}")


def show_folder(show):
    year = str(show.get('first_air_date') or '')[:4]
    return f"{safe_name(show['name'])}{f' ({year})' if year else ''} [tmdbid-{int(show['id'])}]"


def _special_episode(path, episodes):
    text = Path(path).stem.replace('_', ' ').replace('.', ' ')
    bonus = re.search(r'\b(Bonus|Discarded)\s*(?:EP\s*)?(\d+)\b', text, re.I)
    if bonus:
        target = name_key(f'{bonus[1]} EP {int(bonus[2])}')
        matches = [e for e in episodes if name_key(e.get('name', '')) == target]
    elif 'deleted' in text.casefold():
        words = set(re.findall(r'[a-z]+', text.casefold())) - {'igl', 'bonus', 'ep', 'deleted', 'moments'}
        matches = [e for e in episodes if 'deleted' in e.get('name', '').casefold()
                   and (words <= set(re.findall(r'[a-z]+', e['name'].casefold())) if words else '1-3' in e['name'])]
    else:
        matches = []
    if len(matches) != 1:
        raise IdentificationRequired(f'Identify the special episode: {Path(path).name}')
    return matches[0]


def episode_plan(paths, release_title, show, seasons, version_id, overrides=None):
    """Stable show/season identity; versions coexist without overwriting other sources."""
    plan = []
    overrides = overrides or {}
    release_coordinates = coordinates(release_title)
    release_season = release_coordinates[0] if release_coordinates else None
    if release_season is None:
        season_match = SEASON.search(release_title.replace('.', ' '))
        release_season = int(season_match[1]) if season_match else None
    regular = [s['season_number'] for s in show.get('seasons', []) if s['season_number'] > 0]
    fallback = release_season if release_season is not None else (regular[0] if len(regular) == 1 else None)
    used = set()
    for path in sorted(paths, key=str):
        path = Path(path)
        override = overrides.get(path.name)
        coord = tuple(override) if override else coordinates(str(path), fallback)
        if coord is None and SPECIAL.search(path.stem.replace('_', ' ')):
            episode = _special_episode(path, seasons.get(0, {}).get('episodes', []))
            coord = (0, episode['episode_number'], None)
        if coord is None and len(paths) == 1:
            coord = release_coordinates
        if coord is None:
            raise IdentificationRequired(f'Could not determine the season and episode of {path.name}.')
        season, number, end = coord
        episode = next((e for e in seasons.get(season, {}).get('episodes', []) if e['episode_number'] == number), None)
        # Provider catalogs can lag a release. Preserve explicit numbering and a readable local title.
        if episode is None:
            episode = {'episode_number': number, 'season_number': season, 'name': f'Episode {number}'}
        tag = f'S{season:02d}E{number:02d}' + (f'-E{end:02d}' if end else '')
        version = safe_name(version_id)[:48]
        stem = f"{safe_name(show['name'])} {tag} - {version}"
        relative = Path(show_folder(show)) / f'Season {season:02d}' / (stem + path.suffix.lower())
        if relative in used:
            raise IdentificationRequired(f'Multiple files claim {tag}; identify their episode numbers before importing.')
        used.add(relative)
        plan.append({'source': str(path), 'relative': str(relative), 'season': season,
                     'episode': number, 'end_episode': end, 'metadata': episode})
    return plan


def nfo(root_tag, values, genres=(), unique_id=None):
    root = ET.Element(root_tag)
    for key, value in values.items():
        if value is not None and value != '':
            ET.SubElement(root, key).text = str(value)
    for genre in genres:
        ET.SubElement(root, 'genre').text = genre
    if unique_id:
        ET.SubElement(root, 'uniqueid', {'type': 'tmdb', 'default': 'true'}).text = str(unique_id)
    return ET.tostring(root, encoding='unicode', xml_declaration=True)


def stage_tv(download_dir, stage_dir, plan, show):
    """Hardlink finished videos and their matching sidecars; retain originals until upload commits."""
    download_dir, stage_dir = Path(download_dir).resolve(), Path(stage_dir)
    folder = stage_dir / show_folder(show)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'tvshow.nfo').write_text(nfo('tvshow', {
        'title': show['name'], 'plot': show.get('overview'), 'year': str(show.get('first_air_date') or '')[:4],
        'premiered': show.get('first_air_date'), 'tmdbid': show['id'],
    }, [g['name'] for g in show.get('genres', [])], show['id']))
    for entry in plan:
        source = Path(entry['source']).resolve()
        if not source.is_relative_to(download_dir):
            raise ValueError('Import source is outside the download directory.')
        target = stage_dir / entry['relative']
        if not target.resolve().is_relative_to(stage_dir.resolve()):
            raise ValueError('Import destination is outside its staging directory.')
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            os.link(source, target)
        ep = entry['metadata']
        target.with_suffix('.nfo').write_text(nfo('episodedetails', {
            'title': ep['name'], 'showtitle': show['name'], 'season': entry['season'],
            'episode': entry['episode'], 'endepisode': entry.get('end_episode'),
            'plot': ep.get('overview'), 'aired': ep.get('air_date'),
        }, unique_id=ep.get('id')))
        for sidecar in source.parent.iterdir():
            if sidecar.suffix.lower() not in {'.srt', '.ass', '.ssa', '.vtt', '.sub', '.idx'}:
                continue
            if sidecar.stem == source.stem or sidecar.name.startswith(source.stem + '.'):
                if not sidecar.resolve().is_relative_to(download_dir):
                    raise ValueError('Subtitle source is outside the download directory.')
                suffix = sidecar.name[len(source.stem):]
                destination = target.with_name(target.stem + suffix)
                if not destination.exists():
                    os.link(sidecar, destination)
    return folder


def artwork_tasks(folder, plan, show):
    tasks = []
    for key, name, width in [('poster_path', 'poster.jpg', 'w500'), ('backdrop_path', 'fanart.jpg', 'w1280')]:
        if show.get(key):
            tasks.append((folder / name, f"https://image.tmdb.org/t/p/{width}{show[key]}"))
    for entry in plan:
        still = entry['metadata'].get('still_path')
        if still:
            target = folder.parent / entry['relative']
            tasks.append((target.with_name(target.stem + '-thumb.jpg'), f'https://image.tmdb.org/t/p/w500{still}'))
    return tasks


def save_artwork(target, url, cache_dir):
    target = Path(target)
    if target.exists():
        return
    data = metadata_bytes(url, cache_dir=cache_dir)
    if not data.startswith((b'\xff\xd8\xff', b'\x89PNG')):
        raise RuntimeError('The metadata service did not return a valid image.')
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f'.{uuid.uuid4().hex}.tmp')
    temporary.write_bytes(data)
    temporary.replace(target)
