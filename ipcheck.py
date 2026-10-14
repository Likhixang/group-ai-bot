#!/usr/bin/env python3
"""
IP 纯净度检测核心模块
基于 https://ip.net.coffee/ 提供的单一权威数据源进行查询与评估。
"""

import html as html_mod
import json
import sys
import urllib.error
import urllib.request

API_URL = "https://ip.net.coffee/api/ip/lookup/{ip}"
TIMEOUT = 10

# 台湾地区码归一到 CN，保持与一个中国原则一致
_CN_REGIONS = {"TW"}


def _cn_norm_cc(cc: str) -> str:
    """把港澳台地区码归一到 CN"""
    if not cc:
        return cc
    c = cc.upper()
    return "CN" if c in _CN_REGIONS else c


def _flag(cc: str) -> str:
    """国家代码转国旗 emoji，如 JP → 🇯🇵（港澳台归一到 CN）"""
    if not cc or len(cc) != 2:
        return ""
    cc = _cn_norm_cc(cc)
    try:
        return chr(ord(cc[0].upper()) + 0x1F1E6 - 0x41) + chr(ord(cc[1].upper()) + 0x1F1E6 - 0x41)
    except Exception:
        return ""


def _score_color(score: int) -> str:
    if score >= 85:
        return "🟢"
    elif score >= 70:
        return "🟡"
    elif score >= 55:
        return "🟠"
    elif score >= 40:
        return "🔴"
    else:
        return "❌"


def _risk_label(score: int) -> str:
    if score >= 85:
        return "高纯净"
    elif score >= 70:
        return "可用"
    elif score >= 55:
        return "有风险"
    elif score >= 40:
        return "严重污染"
    elif score >= 20:
        return "黑名单"
    else:
        return "高危"


def _is_valid_asn(asn_val) -> bool:
    """ASN 有效检测：过滤 AS0/ASNone/空 等占位值"""
    if not asn_val or not isinstance(asn_val, str):
        return False
    s = asn_val.strip()
    if not s or s.upper() in ("AS0", "ASNONE", "AS?", "NONE", "NULL", ""):
        return False
    return True


def _fetch(url: str):
    """GET 请求，返回 JSON object 或错误对象。"""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
            return payload if isinstance(payload, dict) else {"_error": "unexpected JSON response"}
    except Exception as e:
        return {"_error": str(e)}


def check_ip(ip: str) -> dict:
    """
    通过 ip.net.coffee 检测指定 IP，返回结构化结果。
    """
    clean_ip = ip.strip("[]")
    result = {
        "ip": clean_ip,
        "score": 100,
        "risk_level": "高纯净",
        "is_proxy": False,
        "is_vpn": False,
        "is_tor": False,
        "is_datacenter": False,
        "is_abuser": False,
        "is_crawler": False,
        "is_mobile": False,
        "is_residential": False,
        "location": {},
        "isp": "",
        "asn": "",
        "asn_num": None,
        "asn_org": "",
        "asn_tbps": "",
        "asn_ipv4_count": None,
        "cidr": "",
        "rdns": "",
        "rpki_status": "",
        "reddit_blocked": False,
        "public_service": None,
        "intelligence": {},
        "related_domains": [],
        "tags": [],
        "ai_verdict": None,
        "error": None,
        "sources": {},
    }

    url = API_URL.format(ip=clean_ip)
    raw_data = _fetch(url)
    data = raw_data if isinstance(raw_data, dict) else {}
    result["sources"]["ip.net.coffee"] = data if "_error" not in data else {}

    if "_error" in data or not data:
        result["error"] = data.get("_error", "unknown error")
        return result

    # 信任评分
    trust_score = data.get("trust_score")
    if trust_score is not None and isinstance(trust_score, (int, float)):
        score = int(trust_score)
    else:
        score = 100
    score = max(0, min(100, score))
    result["score"] = score
    result["risk_level"] = _risk_label(score)

    # 风险与网络属性
    result["is_datacenter"] = bool(data.get("is_datacenter", False))
    result["is_residential"] = bool(data.get("isResidential", False))
    result["is_vpn"] = bool(data.get("is_vpn", False))
    result["is_proxy"] = bool(data.get("is_proxy", False))
    result["is_tor"] = bool(data.get("is_tor", False))
    result["is_crawler"] = bool(data.get("is_crawler", False))
    result["is_abuser"] = bool(data.get("is_abuser", False))
    result["is_mobile"] = bool(data.get("is_mobile", False))

    # 位置信息
    country = data.get("country", "") or ""
    country_code = data.get("countryCode", "") or ""
    city = data.get("city", "") or ""
    state = data.get("region", "") or ""
    result["location"] = {
        "country": country,
        "country_code": country_code,
        "city": city,
        "state": state,
    }

    # ISP / 组织
    isp = data.get("isp", "") or data.get("company_name", "") or ""
    result["isp"] = isp
    result["cidr"] = data.get("cidr", "") or ""
    result["rdns"] = data.get("rdns", "") or ""
    result["rpki_status"] = data.get("rpki_status", "") or ""
    result["reddit_blocked"] = bool(data.get("reddit_blocked", False))

    # ASN 详情
    asn_num = data.get("asn")
    as_org = data.get("asOrganization", "") or data.get("asname", "") or ""
    result["asn_num"] = asn_num
    result["asn_org"] = as_org
    result["asn_tbps"] = data.get("asn_tbps", "") or ""
    result["asn_ipv4_count"] = data.get("asn_ipv4_count")
    if asn_num:
        result["asn"] = f"AS{asn_num} {as_org}".strip()
    elif as_org:
        result["asn"] = as_org

    # 公共服务
    if data.get("is_public_service") and isinstance(data.get("public_service"), dict):
        result["public_service"] = data["public_service"]

    # 威胁与情报
    intel = data.get("intelligence")
    if isinstance(intel, dict):
        result["intelligence"] = intel

    # 关联域名（取前 5 个）
    rel = data.get("related_domains")
    if isinstance(rel, list):
        result["related_domains"] = [
            d.get("domain") for d in rel if isinstance(d, dict) and d.get("domain")
        ][:5]

    # AI / 场景分析
    ai_verdict = data.get("ai_verdict")
    if isinstance(ai_verdict, dict):
        result["ai_verdict"] = ai_verdict

    # 标签
    tags = []
    if result["is_residential"]:
        tags.append("原生住宅")
    if result["is_datacenter"]:
        tags.append("数据中心/机房")
    if result["is_mobile"]:
        tags.append("移动网络")
    if result["is_vpn"]:
        tags.append("VPN")
    if result["is_proxy"]:
        tags.append("代理")
    if result["is_tor"]:
        tags.append("Tor出口")
    if result["is_crawler"]:
        tags.append("爬虫")
    if result["is_abuser"]:
        tags.append("已知滥用")

    if ai_verdict and isinstance(ai_verdict, dict) and ai_verdict.get("label"):
        lbl = ai_verdict["label"]
        if lbl not in tags and lbl not in ("IDC", "Non-routable"):
            tags.append(lbl)

    result["tags"] = tags
    return result


def format_report(result: dict) -> str:
    """格式化为 Telegram HTML 报告"""
    ip = result["ip"]
    score = result["score"]
    color = _score_color(score)
    risk = result["risk_level"]
    tags = result.get("tags", [])
    loc = result.get("location", {})

    def esc(t):
        return html_mod.escape(str(t or ""))

    lines = []
    lines.append("🛡 <b>IP 纯净度检测</b>")
    lines.append(f"📍 <code>{esc(ip)}</code>")
    lines.append("")
    lines.append(f"{color} <b>评分: {score}/100</b> — {risk}")
    lines.append("")

    parts = []
    if loc.get("city"):
        parts.append(esc(loc["city"]))
    if loc.get("state") and loc.get("state") != loc.get("city"):
        parts.append(esc(loc["state"]))
    if loc.get("country"):
        parts.append(esc(loc["country"]))

    main_cc = loc.get("country_code", "")
    if parts:
        main_flag = _flag(main_cc) if main_cc else ""
        lines.append(f"🌍 <b>位置:</b> {' '.join(p for p in [main_flag, *parts] if p)}")

    if result.get("isp"):
        lines.append(f"🏢 <b>ISP:</b> {esc(result['isp'])}")

    asn = result.get("asn", "")
    if asn and _is_valid_asn(asn):
        asn_str = esc(asn)
        asn_extra = []
        if result.get("asn_tbps"):
            asn_extra.append(esc(result["asn_tbps"]))
        if result.get("asn_ipv4_count"):
            cnt = result["asn_ipv4_count"]
            if cnt >= 10000:
                cnt_str = f"{cnt // 10000}万 IP"
            else:
                cnt_str = f"{cnt} IP"
            asn_extra.append(cnt_str)
        if asn_extra:
            asn_str += f" ({' · '.join(asn_extra)})"
        lines.append(f"🔗 <b>ASN:</b> {asn_str}")
    elif asn:
        lines.append(f"🔗 <b>ASN:</b> {esc(asn)}")
    else:
        lines.append("🔗 <b>ASN:</b> 未知")

    # CIDR & RPKI 状态
    cidr_parts = []
    if result.get("cidr"):
        cidr_parts.append(esc(result["cidr"]))
    if result.get("rpki_status"):
        rpki_map = {"valid": "RPKI 有效", "invalid": "RPKI 无效", "not_found": "RPKI 未宣告"}
        rpki_text = rpki_map.get(result["rpki_status"].lower(), f"RPKI {result['rpki_status']}")
        cidr_parts.append(rpki_text)
    if cidr_parts:
        lines.append(f"🛣 <b>路由:</b> {' · '.join(cidr_parts)}")

    if result.get("rdns"):
        lines.append(f"🌐 <b>rDNS:</b> {esc(result['rdns'])}")

    # 公共服务
    pub = result.get("public_service")
    if pub and isinstance(pub, dict) and pub.get("service"):
        pub_desc = esc(pub["service"])
        if pub.get("note"):
            pub_desc += f" ({esc(pub['note'])})"
        lines.append(f"🏛 <b>公共服务:</b> {pub_desc}")

    if tags:
        lines += ["", f"🏷 <b>标记:</b> {' · '.join(esc(t) for t in tags)}"]

    lines += ["", "<b>检测项:</b>"]
    check_items = [
        ("数据中心", result.get("is_datacenter", False)),
        ("原生住宅", result.get("is_residential", False)),
        ("VPN", result.get("is_vpn", False)),
        ("代理", result.get("is_proxy", False)),
        ("Tor出口", result.get("is_tor", False)),
        ("已知滥用", result.get("is_abuser", False)),
    ]

    for name, is_flagged in check_items:
        if name == "原生住宅":
            icon = "✅" if is_flagged else "ℹ️"
            val = "是" if is_flagged else "否"
        else:
            icon = "❌" if is_flagged else "✅"
            val = "是" if is_flagged else "否"
        lines.append(f"{icon} {name}: {val}")

    # 社区与威胁情报
    intel = result.get("intelligence") or {}
    threat_labels = []
    if isinstance(intel.get("threats"), list):
        for th in intel["threats"]:
            if isinstance(th, dict) and th.get("label"):
                threat_labels.append(th["label"])

    intel_items = []
    if intel.get("abuser_score_raw"):
        intel_items.append(f"滥用等级: {esc(intel['abuser_score_raw'])}")
    if threat_labels:
        intel_items.append(f"命中威胁: {esc(' · '.join(threat_labels))}")
    if result.get("reddit_blocked"):
        intel_items.append("Reddit: 已封禁")

    if intel_items:
        lines += ["", f"🚨 <b>威胁情报:</b> {' | '.join(intel_items)}"]

    ai_v = result.get("ai_verdict")
    if ai_v and isinstance(ai_v, dict) and ai_v.get("reasoning"):
        lines += ["", f"💡 <b>评估说明:</b> {esc(ai_v['reasoning'])}"]

    # 关联域名
    rel_domains = result.get("related_domains") or []
    if rel_domains:
        lines += ["", f"📑 <b>解析域名:</b> <code>{'</code>, <code>'.join(esc(d) for d in rel_domains[:3])}</code>"]

    lines += [
        "",
        "📊 <b>数据源:</b>",
        '<blockquote>• <a href="https://ip.net.coffee/">Net.Coffee</a></blockquote>',
    ]

    return "\n".join(lines)


def format_short(ip: str, result: dict) -> str:
    """简短格式，适合快速查看"""
    color = _score_color(result["score"])
    tags = result.get("tags", [])
    tag_str = f" · {' '.join(tags[:3])}" if tags else ""
    return f"{color} `{ip}`: **{result['score']}/100** ({result['risk_level']}){tag_str}"


def is_valid_ip(ip: str) -> bool:
    """校验 IPv4 或 IPv6（用 stdlib 确保全覆盖）"""
    import ipaddress
    try:
        ipaddress.ip_address(ip.strip("[]"))
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 ipcheck.py <IP地址>")
        sys.exit(1)

    ip_arg = sys.argv[1]
    res = check_ip(ip_arg)
    print(format_report(res))
