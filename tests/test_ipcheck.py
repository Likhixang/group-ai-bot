import ipcheck


def test_check_ip_ignores_non_mapping_source_payloads(monkeypatch):
    monkeypatch.setattr(ipcheck, "_fetch", lambda url: "rate limited")
    monkeypatch.setattr(ipcheck, "_fetch_post", lambda url, data: ["temporary error"])

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"data": []}'

    monkeypatch.setattr(ipcheck.urllib.request, "urlopen", lambda *args, **kwargs: Response())

    result = ipcheck.check_ip("8.8.8.8")

    assert result["ip"] == "8.8.8.8"
    assert result["score"] == 100
    assert result["sources"]["ipapi.is"] == {}
    assert result["sources"]["iplogs.com"] == {}
