from openvidia import config


def test_http2_is_the_default(monkeypatch):
    monkeypatch.delenv("OPENVIDIA_HTTP2", raising=False)
    assert config.upstream_http2() is True


def test_http2_can_be_switched_off(monkeypatch):
    for value in ("0", "false", "no", "off", " OFF "):
        monkeypatch.setenv("OPENVIDIA_HTTP2", value)
        assert config.upstream_http2() is False


def test_any_other_value_keeps_http2(monkeypatch):
    monkeypatch.setenv("OPENVIDIA_HTTP2", "1")
    assert config.upstream_http2() is True
