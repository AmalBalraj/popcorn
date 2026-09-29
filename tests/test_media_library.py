from pathlib import Path
from xml.etree import ElementTree as ET
import pytest

import media_library as media
from sources.models import Release

SHOW = {'id': 42, 'name': 'Example Show', 'first_air_date': '2020-01-01', 'genres': [{'name': 'Comedy'}],
        'seasons': [{'season_number': 0}, {'season_number': 1}]}
SEASONS = {1: {'episodes': [{'episode_number': 2, 'season_number': 1, 'id': 102, 'name': 'Second', 'overview': 'A & B'}]},
           0: {'episodes': [{'episode_number': 7, 'season_number': 0, 'id': 107, 'name': 'Bonus EP 4'}]}}


@pytest.mark.parametrize('value,expected', [
    ('Example.S01E02.1080p.mkv', (1, 2, None)),
    ('Example S01E02-E03.mkv', (1, 2, 3)),
    ('Example 1x02.mkv', (1, 2, None)),
    ('Example/Season 02/EP_04.mp4', (2, 4, None)),
    ('Example/Bonus_EP_04.mp4', None),
])
def test_episode_patterns(value, expected):
    assert media.coordinates(value) == expected


def test_separate_sources_join_same_show_and_season_without_overwriting():
    a = media.episode_plan([Path('Example.S01E02.WEB.mkv')], 'Example Show S01E02', SHOW, SEASONS, '1080p aaaaaaaa')
    b = media.episode_plan([Path('Example Show S01E02 BluRay.mp4')], 'Example Show S01E02', SHOW, SEASONS, '4K bbbbbbbb')
    assert Path(a[0]['relative']).parent == Path(b[0]['relative']).parent
    assert '[tmdbid-42]' in a[0]['relative']
    assert a[0]['relative'] != b[0]['relative']
    assert a[0]['episode'] == b[0]['episode'] == 2


def test_bonus_numbering_uses_provider_specials_order():
    plan = media.episode_plan([Path('IGL_Bonus_EP_4_ft_SeedheMaut.mp4')], 'Example Show', SHOW, SEASONS, 'original')
    assert (plan[0]['season'], plan[0]['episode']) == (0, 7)
    assert 'S00E07' in plan[0]['relative']


def test_ambiguous_season_and_specials_require_identification():
    show = {**SHOW, 'seasons': [{'season_number': 1}, {'season_number': 2}]}
    with pytest.raises(media.IdentificationRequired):
        media.episode_plan([Path('EP_02.mp4')], 'Example Show', show, SEASONS, 'original')
    with pytest.raises(media.IdentificationRequired):
        media.episode_plan([Path('Bonus_EP_9.mp4')], 'Example Show', SHOW, SEASONS, 'original')


def test_staging_keeps_originals_and_language_subtitles(tmp_path):
    download = tmp_path / 'download'
    download.mkdir()
    source = download / 'Example.S01E02.mkv'
    source.write_bytes(b'video fixture')
    (download / 'Example.S01E02.eng.forced.srt').write_text('subtitle fixture')
    (download / 'Other.eng.srt').write_text('unrelated subtitles')
    plan = media.episode_plan([source], 'Example Show S01E02', SHOW, SEASONS, 'test')
    stage = tmp_path / 'stage'
    media.stage_tv(download, stage, plan, SHOW)
    target = stage / plan[0]['relative']
    assert source.exists() and source.stat().st_ino == target.stat().st_ino
    assert target.with_name(target.stem + '.eng.forced.srt').read_text() == 'subtitle fixture'
    assert not list(stage.rglob('Other*'))
    nfo = ET.parse(target.with_suffix('.nfo')).getroot()
    assert nfo.findtext('season') == '1' and nfo.findtext('episode') == '2'
    assert nfo.findtext('plot') == 'A & B'
    media.stage_tv(download, stage, plan, SHOW)  # Restart/retry does not overwrite media.


def test_similarly_named_shows_are_not_guessed():
    def get(path, **params):
        return {'results': [{'id': 1, 'name': 'Example Show'}, {'id': 2, 'name': 'Example Show'}]}
    with pytest.raises(media.IdentificationRequired):
        media.resolve_show('Example Show S01E02', get)


def test_sample_movie_is_not_mistaken_for_tv():
    assert not media.looks_like_tv('Movie 2020 1080p', [Path('Movie.2020.mp4')])
    assert media.looks_like_tv("India's Got Latent", [Path('IGL_EP_01.mp4')])


def test_unresolved_tv_retains_files_and_waits_without_upload(web, monkeypatch):
    from test_jobs import make_job, fake_transfers, JOB_ID
    result, directory = make_job(web, download_complete=True, destination=web.LIBRARY_REMOTE)
    result.title = 'Example Show S01E02'
    (directory / 'Example.mp4').rename(directory / 'Example.S01E02.mp4')
    monkeypatch.setattr(web, '_tmdb_get', lambda *args, **kwargs: {'results': []})
    commands = fake_transfers(monkeypatch, web, [])
    web._run_download(JOB_ID, result, web.LIBRARY_REMOTE, None)
    assert web.JOBS[JOB_ID]['status'] == 'needs_identification'
    assert web.JOBS[JOB_ID]['identification_files'] == ['Example.S01E02.mp4']
    assert not commands and directory.exists()
    web.JOBS.clear()
    web._init_state()
    assert web.JOBS[JOB_ID]['status'] == 'needs_identification'
    assert not web.JOBS[JOB_ID]['recoverable']


def test_tv_import_uploads_normalized_stage_and_indexes_series(web, monkeypatch):
    from test_jobs import make_job, fake_transfers, JOB_ID
    result, directory = make_job(web, download_complete=True, destination=web.LIBRARY_REMOTE)
    result.title = 'Example Show S01E02'
    (directory / 'Example.mp4').rename(directory / 'Example.S01E02.mp4')
    def get(path, **params):
        if path == 'search/tv': return {'results': [{'id': 42, 'name': 'Example Show'}]}
        if '/season/' in path: return SEASONS[int(path.rsplit('/', 1)[1])]
        return SHOW
    monkeypatch.setattr(web, '_tmdb_get', get)
    monkeypatch.setattr(web.media_library, 'artwork_tasks', lambda *args: [])
    monkeypatch.setattr(web, '_refresh_library_mount', lambda: None)
    monkeypatch.setattr(web, '_find_library_item', lambda *args: 'series-id')
    monkeypatch.setattr(web.threading.Timer, 'start', lambda *args: None)
    commands = fake_transfers(monkeypatch, web, [0])
    web._run_download(JOB_ID, result, web.LIBRARY_REMOTE, None)
    job = web.JOBS[JOB_ID]
    assert job['status'] == 'complete' and job['destination'] == web.SHOWS_REMOTE
    assert job['media_kind'] == 'tv' and job['library_item_id'] == 'series-id'
    assert 'library-stage' in commands[0][1][2]
    assert commands[0][1][3] == web.SHOWS_REMOTE
    assert all(str(web.SHOWS_PATH) in p and 'S01E02' in p for p in job['media_paths'])
    assert not directory.exists()


def test_identification_is_owner_protected(web, monkeypatch):
    from test_jobs import make_job, signed_client, JOB_ID
    make_job(web, status='needs_identification', download_complete=True)
    monkeypatch.setattr(web, '_start_download_job', lambda *args: True)
    assert signed_client(web, user='someone-else').post(f'/api/jobs/{JOB_ID}/identify', json={'show_tmdb_id': 42}).status_code == 404
    response = signed_client(web).post(f'/api/jobs/{JOB_ID}/identify', json={'show_tmdb_id': 42, 'tv_season': 1})
    assert response.status_code == 202 and web.JOBS[JOB_ID]['show_tmdb_id'] == 42


def test_preview_descriptor_uses_selected_version(web):
    info = {'Height': 180, 'TileWidth': 10, 'TileHeight': 10, 'ThumbnailCount': 220, 'Interval': 10000}
    item = {'Id': 'a'*32, 'Trickplay': {'source-a': {'320': info}}}
    preview = web._preview_descriptor(item, 'source-a')
    assert 'MediaSourceId=source-a' in preview['url'] and preview['count'] == 220
    assert web._preview_descriptor(item, 'source-b') is None


def test_migrated_preview_fallback_follows_file_identity(web):
    import json
    source_id = 'b' * 32
    path = str(web.SHOWS_PATH / 'Example/Season 01/Example S01E02.mp4')
    root = web.ROOT / 'data/tv-migration'
    root.mkdir(parents=True)
    (root / 'manifest.json').write_text(json.dumps({'entries': [{'relative': 'Example/Season 01/Example S01E02.mp4',
        'previews': {'asset_id': 'a' * 32, 'width': 320, 'height': 180, 'columns': 10, 'rows': 10, 'count': 220, 'interval': 10000}}]}))
    item = {'Id': source_id, 'Path': path, 'MediaSources': [{'Id': source_id, 'Path': path}]}
    preview = web._preview_descriptor(item, source_id)
    assert preview['url'] == f'/api/previews/{source_id}/{source_id}/{{index}}.jpg'
    assert preview['count'] == 220
    assert web._preview_descriptor(item, 'c' * 32) is None


def test_generated_preview_fallback_checks_media_path(web):
    import json
    source_id = 'd' * 32
    root = web.ROOT / 'data/preview-cache' / source_id
    root.mkdir(parents=True)
    (root / 'manifest.json').write_text(json.dumps({'media_path': '/movie.mp4', 'previews': {'asset_id': source_id,
        'width': 320, 'height': 180, 'columns': 10, 'rows': 10, 'count': 220, 'interval': 10000}}))
    item = {'Id': source_id, 'Path': '/movie.mp4'}
    assert web._preview_descriptor(item, source_id)['count'] == 220
    item['Path'] = '/different.mp4'
    assert web._preview_descriptor(item, source_id) is None


def test_episode_still_and_inherited_art_use_the_actual_image_kind(web):
    with web.app.test_request_context():
        episode = {'Id': 'a' * 32, 'Type': 'Episode', 'ImageTags': {'Primary': 'still'}}
        assert '/poster?' in web._art(episode, 'thumb')['src']
        inherited = {'Id': 'b' * 32, 'Type': 'Episode', 'SeriesId': 'c' * 32,
                     'ParentBackdropImageTags': ['backdrop']}
        assert '/'+ 'c' * 32 + '/backdrop?' in web._art(inherited, 'thumb')['src']
