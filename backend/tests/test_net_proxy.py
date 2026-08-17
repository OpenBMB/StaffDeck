"""正向代理收口:绕过判定、后缀通配、私网自动直连、httpx 注入、环境收敛。"""

import os

import pytest

import app.net_proxy as net_proxy


@pytest.fixture(autouse=True)
def _clean_proxy_env(monkeypatch):
    """每个用例前后都还原代理环境变量(apply_proxy_env 直接写 os.environ,须防泄漏)。"""
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
    saved = {key: os.environ.get(key) for key in keys}
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    yield
    for key in keys:
        if saved[key] is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved[key]


def _settings(monkeypatch, *, http_proxy="", https_proxy="", no_proxy=""):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(
            http_proxy=http_proxy, https_proxy=https_proxy, no_proxy=no_proxy
        ),
    )


def test_private_hosts_auto_bypass_without_config(monkeypatch) -> None:
    _settings(monkeypatch, http_proxy="http://proxy.corp:8080")
    for host in ("localhost", "127.0.0.1", "10.1.2.3", "192.168.1.10", "172.16.5.5", "::1", "nas"):
        assert net_proxy.should_bypass_proxy(host) is True, host
    assert net_proxy.should_bypass_proxy("open.feishu.cn") is False


def test_no_proxy_suffix_and_wildcard_matching(monkeypatch) -> None:
    _settings(
        monkeypatch,
        http_proxy="http://proxy.corp:8080",
        no_proxy=".corp.internal, mirror.example.com, *.partner.cn",
    )
    assert net_proxy.should_bypass_proxy("llm-center.corp.internal") is True
    assert net_proxy.should_bypass_proxy("corp.internal") is True
    assert net_proxy.should_bypass_proxy("mirror.example.com") is True
    assert net_proxy.should_bypass_proxy("a.partner.cn") is True
    # *. 与 . 后缀形态都含裸域(更直觉,少踩坑)
    assert net_proxy.should_bypass_proxy("partner.cn") is True
    assert net_proxy.should_bypass_proxy("evilcorp.internal") is False
    assert net_proxy.should_bypass_proxy("api.weixin.qq.com") is False


def test_no_proxy_star_bypasses_everything(monkeypatch) -> None:
    _settings(monkeypatch, http_proxy="http://proxy.corp:8080", no_proxy="*")
    assert net_proxy.proxy_for_url("https://api.weixin.qq.com") is None


def test_proxy_for_url_prefers_explicit_and_bypasses(monkeypatch) -> None:
    _settings(
        monkeypatch,
        http_proxy="http://proxy.corp:8080",
        https_proxy="http://proxy.corp:8443",
        no_proxy=".corp.internal",
    )
    assert net_proxy.proxy_for_url("https://api.weixin.qq.com") == "http://proxy.corp:8443"
    assert net_proxy.proxy_for_url("http://ilinkai.weixin.qq.com") == "http://proxy.corp:8080"
    # 内网模型服务:命中后缀名单,直连
    assert net_proxy.proxy_for_url("https://llm-center.corp.internal/v1") is None
    # 私网 IP:不配名单也直连
    assert net_proxy.proxy_for_url("http://10.0.0.8:8080/v1") is None


def test_proxy_for_url_unset_returns_none_for_env_fallback(monkeypatch) -> None:
    _settings(monkeypatch)
    assert net_proxy.proxy_for_url("https://api.weixin.qq.com") is None


def test_httpx_proxy_kwargs_forms(monkeypatch) -> None:
    _settings(monkeypatch, http_proxy="http://proxy.corp:8080", no_proxy=".corp.internal")
    # 显式代理
    kwargs = net_proxy.httpx_proxy_kwargs("https://api.weixin.qq.com")
    assert kwargs == {"proxy": "http://proxy.corp:8080"}
    # 绕过:精确主机无代理 mounts(强制直连)
    bypass = net_proxy.httpx_proxy_kwargs("https://llm-center.corp.internal")
    assert "proxy" not in bypass
    assert "all://llm-center.corp.internal" in bypass["mounts"]
    # 未配置:空(trust_env 接管)
    _settings(monkeypatch)
    assert net_proxy.httpx_proxy_kwargs("https://api.weixin.qq.com") == {}


def test_apply_proxy_env_converges_configured_keys(monkeypatch) -> None:
    _settings(
        monkeypatch,
        http_proxy="http://proxy.corp:8080",
        https_proxy="http://proxy.corp:8443",
        no_proxy=".corp.internal",
    )
    net_proxy.apply_proxy_env()
    assert os.environ["HTTP_PROXY"] == "http://proxy.corp:8080"
    assert os.environ["http_proxy"] == "http://proxy.corp:8080"
    assert os.environ["HTTPS_PROXY"] == "http://proxy.corp:8443"
    assert os.environ["NO_PROXY"] == ".corp.internal"
    assert os.environ["no_proxy"] == ".corp.internal"


def test_apply_proxy_env_unset_keeps_existing_env(monkeypatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://existing:3128")
    _settings(monkeypatch)  # 未配置
    net_proxy.apply_proxy_env()
    assert os.environ["HTTP_PROXY"] == "http://existing:3128"


def test_proxy_host_for_allowlist(monkeypatch) -> None:
    _settings(monkeypatch, https_proxy="http://proxy.corp:8443")
    assert net_proxy.proxy_host_for_allowlist() == "proxy.corp"
    _settings(monkeypatch)
    assert net_proxy.proxy_host_for_allowlist() == ""
