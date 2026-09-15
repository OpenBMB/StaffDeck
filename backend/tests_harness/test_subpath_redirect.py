from starlette.requests import Request


def test_single_port_root_keeps_deployment_prefix():
    import single_port_app
    for prefix in ("", "/test", "/test/"):
        request=Request({"type":"http","method":"GET","path":"/","root_path":prefix,"headers":[],"query_string":b""})
        response=single_port_app.root_redirect(request)
        assert response.headers["location"]==prefix.rstrip("/")+"/chat/"
