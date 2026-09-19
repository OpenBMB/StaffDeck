from starlette.requests import Request


def test_single_port_root_keeps_deployment_prefix():
    import single_port_app
    for prefix in ("", "/test", "/test/"):
        request=Request({"type":"http","method":"GET","path":"/","root_path":prefix,"headers":[],"query_string":b""})
        response=single_port_app.root_redirect(request)
        assert response.headers["location"]==prefix.rstrip("/")+"/chat/"


def test_business_deep_links_serve_spa_without_masking_missing_api(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import single_port_app
    (tmp_path / 'index.html').write_text('<html><body>frontend-contract-marker</body></html>')
    monkeypatch.setattr(single_port_app, 'ENTERPRISE_DIST', tmp_path)
    host = FastAPI()
    host.mount('/test', single_port_app.app)
    client = TestClient(host)
    for path in ('/test/plaza', '/test/plaza/knowledge/k', '/test/new', '/test/workspace/skill/s'):
        response = client.get(path)
        assert response.status_code == 200 and 'text/html' in response.headers['content-type']
        assert 'frontend-contract-marker' in response.text
    assert client.get('/test/api/nonexistent-contract-route').status_code == 404
