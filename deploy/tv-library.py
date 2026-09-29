#!/usr/bin/env python3
"""Prepare, apply and verify the initial TV migration. Every move has a durable manifest."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import media_library as media
import requests

WORK = ROOT / 'data/tv-migration'
MANIFEST = WORK / 'manifest.json'
RC = ['rclone', '--config=/home/amal/.config/rclone/rclone.conf', '--contimeout=10s', '--timeout=2m']


def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def environment():
    result = {}
    for line in Path('/etc/popcorn.env').read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            result[key] = shlex.split(value)[0] if value else ''
    return result


def client():
    env = environment()
    session = requests.Session()
    session.trust_env = False
    session.headers['Authorization'] = 'MediaBrowser Client="Popcorn", Version="1.0.0", Token="' + env['POPCORN_JELLYFIN_API_KEY'] + '"'
    return session


def api(session, method, path, **kwargs):
    response = session.request(method, 'http://127.0.0.1:8096/jellyfin/' + path, timeout=60, **kwargs)
    response.raise_for_status()
    return response


def save(data):
    temporary = MANIFEST.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2))
    temporary.replace(MANIFEST)


def prepare():
    # Prepare as the service account, so later imports can reuse cached metadata.
    if os.geteuid() == 0:
        user = pwd.getpwnam('amal')
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
    inventory = json.loads(Path('/tmp/popcorn-tv-inventory.json').read_text())
    files = json.loads(Path('/tmp/popcorn-tv-remote.json').read_text())
    by_path = {f['Path']: f for f in files}
    tmdb = inventory['tmdb']
    cache = ROOT / 'data/media-cache'
    cache.mkdir(parents=True, exist_ok=True)
    import hashlib
    for path, data in tmdb.items():
        if '?' in path:
            from urllib.parse import parse_qsl
            path, query = path.split('?', 1)
            params = dict(parse_qsl(query))
            params['include_adult'] = 'false'
        else:
            params = {}
        name = hashlib.sha256((path + json.dumps(params, sort_keys=True)).encode()).hexdigest() + '.json'
        (cache / name).write_text(json.dumps({'fetched_at': time.time(), 'data': data}))
    # Seed the last successfully verified resolver address; fresh DNS is still checked after five minutes.
    (cache / 'api.themoviedb.org.dns.json').write_text(json.dumps({'addresses': ['3.175.86.37'], 'checked_at': time.time()}))
    WORK.mkdir(parents=True, exist_ok=True)
    entries, art = [], []
    shows = {}
    for show_id, label in [(262838, "India's Got Latent"), (97546, 'Ted Lasso'), (95350, 'Lanterns')]:
        show = tmdb[f'tv/{show_id}']
        shows[str(show_id)] = show
        items = [item for item in inventory['items'] if any(
            ('/IGL/' in m.get('Path', '') if show_id == 262838 else media.name_key(media.show_query(Path(m.get('Path', '')).name)) == media.name_key(label))
            for m in item.get('MediaSources', []))]
        paths = [m['Path'] for item in items for m in item.get('MediaSources', [])]
        seasons = {int(p.rsplit('/', 1)[1]): d for p, d in tmdb.items() if p.startswith(f'tv/{show_id}/season/')}
        overrides = {Path(p).name: [1, int(media.LOOSE_EPISODE.search(Path(p).stem.replace('_', ' '))[1]), None]
                     for p in paths if show_id == 262838 and not media.SPECIAL.search(Path(p).stem.replace('_', ' '))}
        plan = media.episode_plan(paths, label, show, seasons, 'Original', overrides)
        folder = WORK / 'metadata' / media.show_folder(show)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'tvshow.nfo').write_text(media.nfo('tvshow', {'title': show['name'], 'plot': show.get('overview'),
            'year': show['first_air_date'][:4], 'premiered': show['first_air_date'], 'tmdbid': show_id},
            [g['name'] for g in show.get('genres', [])], show_id))
        for entry in plan:
            source = str(Path(entry['source']).relative_to('/home/amal/gdrive-movies'))
            entry.update(source_remote='gdrive:Movies/' + source,
                         target_remote='gdrive:TV Shows/' + entry['relative'], size=by_path[source]['Size'], show_id=show_id)
            entry['old_media_source_id'] = next(m['Id'] for item in items for m in item.get('MediaSources', []) if m['Path'] == entry['source'])
            entry['old_item_id'] = next(item['Id'] for item in items if any(m['Path'] == entry['source'] for m in item.get('MediaSources', [])))
            ep = entry['metadata']
            target = WORK / 'metadata' / entry['relative']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.with_suffix('.nfo').write_text(media.nfo('episodedetails', {'title': ep['name'], 'showtitle': show['name'],
                'season': entry['season'], 'episode': entry['episode'], 'plot': ep.get('overview'), 'aired': ep.get('air_date')}, unique_id=ep.get('id')))
            # Matching external subtitles retain their language/forced/hearing-impaired suffixes.
            original = Path(source)
            for sidecar, info in by_path.items():
                side = Path(sidecar)
                if side.parent == original.parent and side.suffix.lower() in {'.srt', '.ass', '.ssa', '.vtt', '.sub', '.idx'} and (side.stem == original.stem or side.name.startswith(original.stem + '.')):
                    suffix = side.name[len(original.stem):]
                    entries.append({'source_remote': 'gdrive:Movies/' + sidecar, 'target_remote': 'gdrive:TV Shows/' + str(Path(entry['relative']).with_name(Path(entry['relative']).stem + suffix)), 'size': info['Size'], 'sidecar': True})
            entries.append(entry)
        art.extend(media.artwork_tasks(folder, plan, show))
    data = {'created_at': time.time(), 'shows': shows, 'entries': entries, 'metadata_remote': 'gdrive:TV Shows'}
    save(data)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(media.save_artwork, target, url, cache) for target, url in art]
        for future in futures:
            future.result()
    print('Prepared', len(shows), 'shows and', len(entries), 'files.', flush=True)
    for entry in entries:
        print(entry['source_remote'], '->', entry['target_remote'], flush=True)


def preserve_previews():
    session = client()
    data = json.loads(MANIFEST.read_text())
    items = api(session, 'GET', 'Items', params={'Recursive': 'true', 'IncludeItemTypes': 'Movie,Episode',
                'Fields': 'Trickplay,MediaSources', 'Limit': 10000}).json()['Items']
    by_id = {i['Id']: i for i in items}
    tasks = []
    for entry in data['entries']:
        if entry.get('sidecar'):
            continue
        sizes = by_id.get(entry['old_item_id'], {}).get('Trickplay', {}).get(entry['old_media_source_id'], {})
        if not sizes:
            continue
        width = min(map(int, sizes))
        info = sizes[str(width)]
        preview = {'width': width, 'height': info['Height'], 'columns': info['TileWidth'], 'rows': info['TileHeight'],
                   'count': info['ThumbnailCount'], 'interval': info['Interval'], 'asset_id': entry['old_media_source_id']}
        entry['previews'] = preview
        count = (preview['count'] + preview['columns'] * preview['rows'] - 1) // (preview['columns'] * preview['rows'])
        for index in range(count):
            target = WORK / 'previews' / preview['asset_id'] / f'{index}.jpg'
            if not target.exists():
                tasks.append((entry, width, index, target))
    def capture(task):
        entry, width, index, target = task
        response = api(session, 'GET', f"Videos/{entry['old_item_id']}/Trickplay/{width}/{index}.jpg",
                       params={'MediaSourceId': entry['old_media_source_id']})
        assert response.content.startswith(b'\xff\xd8\xff'), 'Invalid preview image.'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(response.content)
        user = pwd.getpwnam('amal')
        os.chown(target.parent, user.pw_uid, user.pw_gid)
        os.chown(target, user.pw_uid, user.pw_gid)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for future in [pool.submit(capture, task) for task in tasks]:
            future.result()
    save(data)
    print('Preserved previews for', sum(bool(e.get('previews')) for e in data['entries']), 'TV videos.', flush=True)


def apply():
    data = json.loads(MANIFEST.read_text())
    session = client()
    playing = [s for s in api(session, 'GET', 'Sessions').json() if s.get('NowPlayingItem') and s.get('LastPlaybackCheckIn')]
    if playing:
        raise RuntimeError('Playback is active; finish the session before library migration.')
    if not data.get('backup'):
        backup = ROOT / 'backups' / ('tv-library-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
        backup.mkdir(parents=True, exist_ok=True)
        for source, name in [(ROOT / 'data/popcorn.sqlite3', 'popcorn.sqlite3'), (Path('/var/lib/jellyfin/data/jellyfin.db'), 'jellyfin.db')]:
            with sqlite3.connect(str(source)) as src, sqlite3.connect(str(backup / name)) as dest:
                src.backup(dest)
        (backup / 'libraries.json').write_text(api(session, 'GET', 'Library/VirtualFolders').text)
        shutil.copy2('/etc/popcorn.env', backup / 'popcorn.env')
        data['backup'] = str(backup)
        save(data)
    run(['install', '-d', '-o', 'amal', '-g', 'amal', '-m', '0755', '/home/amal/gdrive-shows'])
    run(['install', '-m', '0644', str(ROOT / 'deploy/popcorn-shows.service'), '/etc/systemd/system/popcorn-shows.service'])
    run(['systemctl', 'daemon-reload'])
    run(['rclone', '--config=/home/amal/.config/rclone/rclone.conf', 'mkdir', 'gdrive:TV Shows'])
    run(['systemctl', 'enable', '--now', 'popcorn-shows.service'])
    # Upload metadata first, then move the existing videos within Drive (no redownload).
    run([*RC, 'copy', str(WORK / 'metadata'), 'gdrive:TV Shows', '--transfers=4'])
    for entry in data['entries']:
        if entry.get('moved'):
            continue
        run([*RC, 'moveto', entry['source_remote'], entry['target_remote'], '--immutable'])
        entry['moved'] = True
        save(data)
        print('Moved', entry['target_remote'], flush=True)
    for url in ['http://127.0.0.1:5572/', 'http://127.0.0.1:5573/']:
        run(['rclone', 'rc', 'vfs/refresh', 'recursive=true', '--url', url], stdout=subprocess.DEVNULL)
    folders = api(session, 'GET', 'Library/VirtualFolders').json()
    if not any(f.get('CollectionType') == 'tvshows' and '/home/amal/gdrive-shows' in f.get('Locations', []) for f in folders):
        options = {'EnableRealtimeMonitor': False, 'EnableInternetProviders': False, 'EnableAutomaticSeriesGrouping': True,
                   'SaveLocalMetadata': False, 'EnableTrickplayImageExtraction': True, 'ExtractTrickplayImagesDuringLibraryScan': False,
                   'SaveTrickplayWithMedia': False, 'SeasonZeroDisplayName': 'Specials',
                   'PathInfos': [{'Path': '/home/amal/gdrive-shows'}]}
        api(session, 'POST', 'Library/VirtualFolders', params={'name': 'TV Shows', 'collectionType': 'tvshows', 'refreshLibrary': 'true'},
            json={'LibraryOptions': options})
    api(session, 'POST', 'Library/Refresh')
    run(['systemctl', 'restart', 'popcorn.service'])
    print('TV library deployed; indexing requested.', flush=True)


def finish_migration():
    session = client()
    data = json.loads(MANIFEST.read_text())
    parents = sorted({str(Path(entry['source_remote'].removeprefix('gdrive:Movies/')).parent)
                      for entry in data['entries'] if not entry.get('sidecar')})
    for parent in parents:
        if parent == '.' or parent in data.get('archived_folders', []):
            continue
        remote = 'gdrive:Movies/' + parent
        remaining = json.loads(run([*RC, 'lsjson', remote, '--recursive', '--files-only'], capture_output=True, text=True).stdout)
        assert not any(Path(f['Path']).suffix.lower() in media.VIDEO_EXTENSIONS for f in remaining), 'Original folder still contains a video.'
        if remaining:
            destination = 'gdrive:Popcorn Migration Backups/' + str(int(data['created_at'])) + '/' + parent
            run([*RC, 'moveto', remote, destination, '--immutable'])
            print('Archived leftover metadata:', parent, flush=True)
        else:
            run([*RC, 'rmdirs', remote])
        data.setdefault('archived_folders', []).append(parent)
        save(data)
        run(['rclone', 'rc', 'vfs/forget', 'dir=' + parent, '--url', 'http://127.0.0.1:5572/'], stdout=subprocess.DEVNULL)
    run(['rclone', 'rc', 'vfs/refresh', 'recursive=true', '--url', 'http://127.0.0.1:5572/'], stdout=subprocess.DEVNULL)
    api(session, 'POST', 'Library/Media/Updated', json={'Updates': [
        {'Path': '/home/amal/gdrive-movies/' + parent, 'UpdateType': 'Deleted'} for parent in parents if parent != '.']})
    api(session, 'POST', 'Library/Refresh')
    print('Removed the empty original TV folders from the Movies library.', flush=True)


def reconcile_jobs():
    session = client()
    data = json.loads(MANIFEST.read_text())
    items = api(session, 'GET', 'Items', params={'Recursive': 'true', 'IncludeItemTypes': 'Episode',
                 'Fields': 'Path,SeriesId', 'Limit': 10000}).json()['Items']
    by_path = {item['Path']: item for item in items}
    changed = 0
    with sqlite3.connect(str(ROOT / 'data/popcorn.sqlite3')) as connection:
        for job_id, raw in connection.execute('SELECT id,payload FROM jobs').fetchall():
            job = json.loads(raw)
            if job.get('kind') == 'clips':
                continue
            matches = [entry for entry in data['entries'] if not entry.get('sidecar') and
                       (entry['source'] in job.get('media_paths', []) or job.get('library_item_id') == entry['old_item_id'])]
            if not matches:
                continue
            show = data['shows'][str(matches[0]['show_id'])]
            paths = ['/home/amal/gdrive-shows/' + entry['relative'] for entry in matches]
            series_id = next((by_path[path].get('SeriesId') for path in paths if path in by_path), None)
            assert series_id, 'Show must be indexed before relinking its download history.'
            job.update(media_paths=paths, media_kind='tv', show_metadata=show, show_tmdb_id=show['id'],
                       library_item_id=series_id, destination='gdrive:TV Shows', display_title=show['name'])
            connection.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(job), job_id))
            changed += 1
    print('Relinked', changed, 'download history records to the correct shows.', flush=True)


def generate_missing_previews():
    session = client()
    data = json.loads(MANIFEST.read_text())
    items = api(session, 'GET', 'Items', params={'Recursive': 'true', 'IncludeItemTypes': 'Movie,Episode',
                'Fields': 'Path,MediaSources,Trickplay', 'Limit': 10000}).json()['Items']
    preserved = {'/home/amal/gdrive-shows/' + e['relative'] for e in data['entries'] if e.get('previews')}
    user = pwd.getpwnam('amal')
    for item in items:
        for source in item.get('MediaSources', []):
            if source.get('Path') in preserved or item.get('Trickplay', {}).get(source['Id']):
                continue
            target = ROOT / 'data/preview-cache' / source['Id']
            manifest = target / 'manifest.json'
            if manifest.exists():
                continue
            path = Path(source['Path'])
            if not path.is_file():
                continue
            # Reuse complete local VFS files without fetching them again.
            if path.is_relative_to('/home/amal/gdrive-movies'):
                cached = Path('/home/amal/.cache/rclone-jellyfin/vfs/gdrive/Movies') / path.relative_to('/home/amal/gdrive-movies')
                if cached.exists() and cached.stat().st_blocks * 512 >= cached.stat().st_size:
                    path = cached
            target.mkdir(parents=True, exist_ok=True)
            print('Generating missing previews:', item['Name'], flush=True)
            command = ['/usr/lib/jellyfin-ffmpeg/ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                       '-threads', '2', '-skip_frame', 'nokey', '-i', str(path), '-an', '-sn',
                       '-vf', 'fps=fps=1/10:round=up:start_time=0,scale=320:-2,tile=10x10',
                       '-threads', '2', '-q:v', '4', str(target / '%d.jpg')]
            run(command, timeout=1800)
            # ffmpeg numbers image sequences from one; the player uses zero-based sheets.
            tiles = sorted(target.glob('*.jpg'), key=lambda p: int(p.stem))
            for tile in tiles:
                tile.rename(target / f'{int(tile.stem)-1}.jpg')
            assert (target / '0.jpg').is_file(), 'No preview frames generated.'
            probe = json.loads(run(['/usr/lib/jellyfin-ffmpeg/ffprobe', '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height', '-of', 'json', str(target / '0.jpg')], capture_output=True, text=True).stdout)['streams'][0]
            import math
            count = math.ceil((source.get('RunTimeTicks') or item.get('RunTimeTicks') or 0) / 100_000_000)
            info = {'media_path': source['Path'], 'previews': {'width': probe['width']//10, 'height': probe['height']//10,
                'columns': 10, 'rows': 10, 'count': min(count, len(tiles)*100), 'interval': 10000, 'asset_id': source['Id']}}
            manifest.write_text(json.dumps(info))
            for file in target.iterdir():
                os.chown(file, user.pw_uid, user.pw_gid)
            os.chown(target, user.pw_uid, user.pw_gid)
            print('Generated', len(tiles), 'preview sheets.', flush=True)


def verify():
    session = client()
    items = api(session, 'GET', 'Items', params={'Recursive': 'true', 'IncludeItemTypes': 'Movie,Series,Season,Episode',
                'Fields': 'Path,ProviderIds,MediaSources,Trickplay', 'Limit': 10000}).json()['Items']
    data = json.loads(MANIFEST.read_text())
    expected = {'/home/amal/gdrive-shows/' + e['relative'] for e in data['entries'] if not e.get('sidecar')}
    indexed = {i.get('Path') for i in items if i['Type'] == 'Episode'}
    preserved = {'/home/amal/gdrive-shows/' + e['relative'] for e in data['entries'] if e.get('previews')}
    def has_previews(item):
        return bool(item.get('Trickplay')) or item.get('Path') in preserved or any(
            (ROOT / 'data/preview-cache' / source['Id'] / 'manifest.json').is_file()
            for source in item.get('MediaSources', []))
    print('Indexed TV episodes:', len(expected & indexed), '/', len(expected), flush=True)
    for item in items:
        if item['Type'] in {'Series', 'Episode'}:
            print(item['Type'], item['Name'], item.get('ParentIndexNumber'), item.get('IndexNumber'),
                  'artwork', bool(item.get('ImageTags')), 'previews', has_previews(item), flush=True)
    remote = json.loads(run([*RC, 'lsjson', 'gdrive:TV Shows', '--recursive', '--files-only'], capture_output=True, text=True).stdout)
    sizes = {f['Path']: f['Size'] for f in remote}
    assert all(sizes[e['target_remote'].removeprefix('gdrive:TV Shows/')] == e['size'] for e in data['entries'])
    assert expected <= indexed, 'TV indexing is not yet complete.'
    assert all(i.get('IndexNumber') is not None and i.get('ParentIndexNumber') is not None and i.get('ImageTags') for i in items if i.get('Path') in expected), 'Episode details/artwork are still being indexed.'
    assert len([i for i in items if i['Type'] == 'Series']) >= 3
    old_ids = {entry['old_item_id'] for entry in data['entries'] if not entry.get('sidecar')}
    assert not any(i['Type'] == 'Movie' and i['Id'] in old_ids for i in items), 'Old TV records remain in Movies.'
    print('All migrated file sizes and episode paths verified.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'preserve-previews', 'apply', 'finish-migration', 'reconcile-jobs', 'generate-missing-previews', 'verify'])
    arguments = parser.parse_args()
    {'prepare': prepare, 'preserve-previews': preserve_previews, 'apply': apply, 'finish-migration': finish_migration, 'reconcile-jobs': reconcile_jobs, 'generate-missing-previews': generate_missing_previews, 'verify': verify}[arguments.action]()
