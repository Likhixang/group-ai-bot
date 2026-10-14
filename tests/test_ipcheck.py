import ipcheck


def test_check_ip_ignores_non_mapping_source_payloads(monkeypatch):
    monkeypatch.setattr(ipcheck, "_fetch", lambda url: "rate limited")

    result = ipcheck.check_ip("8.8.8.8")

    assert result["ip"] == "8.8.8.8"
    assert result["score"] == 100
    assert result["sources"]["ip.net.coffee"] == {}


def test_check_ip_parses_net_coffee_response(monkeypatch):
    sample_payload = {
        "ip": "1.1.1.1",
        "country": "Australia",
        "countryCode": "au",
        "region": "Queensland",
        "city": "South Brisbane",
        "isp": "Cloudflare, Inc.",
        "asn": 13335,
        "asOrganization": "Cloudflare, Inc.",
        "trust_score": 41,
        "is_datacenter": True,
        "isResidential": False,
        "is_vpn": False,
        "is_proxy": False,
        "is_tor": False,
        "is_abuser": False,
        "is_crawler": False,
        "is_mobile": False,
        "rdns": "one.one.one.one",
        "ai_verdict": {
            "label": "公共 DNS 任播",
            "reasoning": "Cloudflare Public DNS",
        },
    }
    monkeypatch.setattr(ipcheck, "_fetch", lambda url: sample_payload)

    result = ipcheck.check_ip("1.1.1.1")
    assert result["score"] == 41
    assert result["risk_level"] == "严重污染"
    assert result["is_datacenter"] is True
    assert result["asn"] == "AS13335 Cloudflare, Inc."
    assert "公共 DNS 任播" in result["tags"]

    report = ipcheck.format_report(result)
    assert "1.1.1.1" in report
    assert "Net.Coffee" in report
    assert "评分: 41/100" in report
