"""Tests for the Swagger/OpenAPI docs and multi-file upload.

Run with: python test_swagger_and_upload.py
Uses the local SQLite fallback when DATABASE_URL is not set, so it needs no live
database.
"""
import io
import os
import sys

# Use a tiny per-file limit so size-limit tests don't allocate ~100MB buffers.
os.environ.setdefault('MAX_UPLOAD_MB', '1')

from app import create_app
from app.models import ApiKey, Model3D, User


def _login(app, client, username='swagtester'):
    with app.app_context():
        if not User.get_by_username(username):
            u = User(username=username, email=f'{username}@example.com')
            u.set_password('pw123456')
            u.save()
    client.post('/auth/login', data={'login_field': username, 'password': 'pw123456'})


def main():
    app = create_app()
    client = app.test_client()
    glb = b'glTF' + b'\x00' * 64

    # --- OpenAPI spec ---
    r = client.get('/api/openapi.json')
    assert r.status_code == 200, r.status_code
    spec = r.get_json()
    assert spec['openapi'].startswith('3.0')
    upload_schema = spec['paths']['/upload']['post']['requestBody'][
        'content']['multipart/form-data']['schema']
    assert upload_schema['properties']['file']['type'] == 'array', \
        'spec should document multi-file upload'
    upload_security = spec['paths']['/upload']['post']['security']
    assert {'uploadApiKey': []} in upload_security, \
        'spec should document upload API key auth'
    print('PASS: /api/openapi.json serves a valid spec with multi-file upload')

    # --- Swagger UI ---
    r = client.get('/api/docs')
    assert r.status_code == 200 and 'swagger-ui' in r.get_data(as_text=True)
    assert '/api/openapi.json' in r.get_data(as_text=True)
    print('PASS: /api/docs renders Swagger UI against the spec')

    # --- Nav/footer link present (both base templates) ---
    assert '/api/docs' in client.get('/').get_data(as_text=True), \
        'home page (base_3d.html) should link to API docs'
    print('PASS: API docs link present in navigation')

    # --- Upload API key auth ---
    r = client.post('/api/upload', data={
        'is_public': 'true', 'file': (io.BytesIO(glb), 'no-auth.glb'),
    }, content_type='multipart/form-data')
    assert r.status_code == 401, r.get_data(as_text=True)

    with app.app_context():
        api_user = User.get_by_username('apitester')
        if not api_user:
            api_user = User(username='apitester', email='apitester@example.com')
            api_user.set_password('pw123456')
            api_user.save()
        api_key, token = ApiKey.create_for_user(api_user.id, name='Tellus test', scopes=['upload'])

    r = client.post('/api/upload', headers={'Authorization': f'Bearer {token}'}, data={
        'name': 'API Key Model', 'is_public': 'true',
        'file': (io.BytesIO(glb), 'api-key-model.glb'),
    }, content_type='multipart/form-data')
    assert r.status_code == 201, r.get_json()
    model_id = r.get_json()['model']['id']
    with app.app_context():
        model = Model3D.get_by_id(model_id)
        assert model.user_id == api_user.id, model.user_id
        assert ApiKey.revoke_for_user(api_key.id, api_user.id)

    r = client.post('/api/upload', headers={'Authorization': f'Bearer {token}'}, data={
        'name': 'Revoked Key Model', 'is_public': 'true',
        'file': (io.BytesIO(glb), 'revoked-key-model.glb'),
    }, content_type='multipart/form-data')
    assert r.status_code == 401, r.get_json()
    print('PASS: upload API keys can upload as owner and revoked keys are rejected')

    _login(app, client)

    # --- Upload page UI ---
    up = client.get('/upload').get_data(as_text=True)
    assert 'Choose Folder' in up, 'upload page should offer folder selection'
    assert 'MAX_UPLOAD_MB' in up, 'upload page should expose the size limit to JS'
    print('PASS: upload page has folder picker and size limit')

    # --- Single-file upload stays backward compatible ---
    r = client.post('/api/upload', data={
        'name': 'Single Model', 'is_public': 'true',
        'file': (io.BytesIO(glb), 'single.glb'),
    }, content_type='multipart/form-data')
    assert r.status_code == 201, r.get_json()
    j = r.get_json()
    assert j['model']['name'] == 'Single Model', j
    print('PASS: single-file upload returns {model} with the given name')

    # --- Single-file upload WITHOUT a name auto-names from the filename ---
    # This is the per-file batch case: the web client uploads each file in its
    # own request and omits 'name', so the server must auto-name (not reject).
    r = client.post('/api/upload', data={
        'is_public': 'true', 'file': (io.BytesIO(glb), 'robot/walk_cycle.glb'),
    }, content_type='multipart/form-data')
    assert r.status_code == 201, r.get_json()
    assert r.get_json()['model']['name'] == 'walk cycle', r.get_json()
    print('PASS: single-file upload without a name auto-names from the filename')

    # --- Multi-file upload: auto-named, per-file errors ---
    r = client.post('/api/upload', data={
        'is_public': 'true', 'tags': 'batch',
        'file': [
            (io.BytesIO(glb), 'robot/walk_cycle.glb'),
            (io.BytesIO(glb), 'robot/idle-pose.glb'),
            (io.BytesIO(b'x'), 'readme.txt'),  # unsupported -> reported as error
        ],
    }, content_type='multipart/form-data')
    assert r.status_code == 201, r.get_json()
    j = r.get_json()
    assert sorted(m['name'] for m in j['uploaded']) == ['idle pose', 'walk cycle'], j
    assert len(j['errors']) == 1 and j['errors'][0]['filename'] == 'readme.txt', j
    print('PASS: multi-file upload auto-names from filenames and reports errors')

    # --- Batch as the web client does it: one request per file, no name ---
    # Mirrors the browser's sequential per-file uploader. Each request has a
    # single file and no 'name'; all must succeed and be auto-named.
    batch = ['hero/walk_cycle.glb', 'hero/idle-pose.glb', 'props/crate.glb']
    names = []
    for path in batch:
        r = client.post('/api/upload', data={
            'is_public': 'true', 'tags': 'scene',
            'file': (io.BytesIO(glb), path),
        }, content_type='multipart/form-data')
        assert r.status_code == 201, (path, r.get_json())
        names.append(r.get_json()['model']['name'])
    assert names == ['walk cycle', 'idle pose', 'crate'], names
    print('PASS: per-file batch (separate requests, no name) all succeed + auto-named')

    # --- Multi-file upload where every file is invalid -> 400 ---
    r = client.post('/api/upload', data={
        'is_public': 'true',
        'file': [(io.BytesIO(b'x'), 'a.txt'), (io.BytesIO(b'y'), 'b.doc')],
    }, content_type='multipart/form-data')
    assert r.status_code == 400, r.get_json()
    print('PASS: multi-file upload with all-invalid files returns 400')

    # --- Per-file size limit is enforced on the file content, not the request ---
    max_file_bytes = app.config['MAX_FILE_BYTES']
    too_big = b'a' * (max_file_bytes + 1024)
    # Send below the request cap (MAX_CONTENT_LENGTH > MAX_FILE_BYTES) so the
    # view runs and rejects on the per-file check rather than Flask's 413.
    assert len(too_big) < app.config['MAX_CONTENT_LENGTH'], 'test file must fit the request cap'
    r = client.post('/api/upload', data={
        'name': 'Too Big', 'is_public': 'true',
        'file': (io.BytesIO(too_big), 'huge.glb'),
    }, content_type='multipart/form-data')
    assert r.status_code == 400, r.status_code
    assert 'too large' in r.get_json()['error'].lower(), r.get_json()
    print('PASS: a file over the per-file limit is rejected with a clear error')

    # --- Oversized *request* returns clean JSON (not an HTML 413 page) ---
    over_request = b'a' * (app.config['MAX_CONTENT_LENGTH'] + 1024)
    r = client.post('/api/upload', data={
        'name': 'Over Request', 'is_public': 'true',
        'file': (io.BytesIO(over_request), 'over.glb'),
    }, content_type='multipart/form-data')
    assert r.status_code == 413, r.status_code
    assert r.is_json and 'error' in r.get_json(), r.get_data(as_text=True)[:200]
    print('PASS: oversized request returns JSON 413 (per-file limit message)')

    print('\nALL TESTS PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
