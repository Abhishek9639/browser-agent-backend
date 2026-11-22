"""Malformed navigation targets are rejected before browser or database work."""

import pytest

from app.agent.browser.driver import host_is_allowed


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:bad/",
        "http://localhost:99999/",
        "http://[broken/",
        "file://localhost/etc/passwd",
        "javascript:alert(1)",
        "not-a-url",
        "http://user:password@localhost/",
        "http://local host/",
    ],
)
@pytest.mark.parametrize("allowed", [("localhost",), ("*",)])
def test_invalid_urls_are_refused_even_with_wildcard(url, allowed):
    assert not host_is_allowed(url, allowed)


def test_default_ports_match_explicit_allow_list():
    assert host_is_allowed("https://example.test/", ("example.test:443",))
    assert not host_is_allowed("http://example.test/", ("example.test:443",))
