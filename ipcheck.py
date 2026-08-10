#!/usr/bin/env python3
"""
IP 纯净度检测核心模块
聚合 ipapi.is + ip-api.com 两个免费源，输出完整的 IP 风险画像。
"""

import json
import time
import urllib.request
import urllib.error
import sys

API_URLS = {
    "ipapi": "https://api.ipapi.is?q={ip}",
    "ipapi_com": "http://ip-api.com/json/{ip}?fields=status,country,regionName,city,isp,org,as,proxy,hosting,query",
    "iplogs": "https://iplogs.com/v1/check",
    "ipinfo": "https://ipinfo.io/{ip}/json",
    "ip2location": "https://api.ip2location.io/?ip={ip}",
}

TIMEOUT = 10


def _fetch(url):
    """GET 请求，返回 JSON 或 None"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ipcheck-bot/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"_error": str(e)}


def _fetch_post(url, data):
    """POST JSON，返回 JSON 或 None"""
    try:
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json", "User-Agent": "ipcheck-bot/1.0"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"_error": str(e)}


def _score_color(score):
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


def _risk_label(score):
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
    if not s:
        return False
    if s.upper() in ("AS0", "ASNONE", "AS?", "NONE", "NULL", ""):
        return False
    return True


def check_ip(ip: str) -> dict:
    """
    检测指定 IP，返回结构化结果。
    """
    result = {
        "ip": ip,
        "score": 100,
        "risk_level": "高纯净",
        "is_proxy": False,
        "is_vpn": False,
        "is_tor": False,
        "is_datacenter": False,
        "is_abuser": False,
        "is_mobile": False,
        "location": {},
        "isp": "",
        "asn": "",
        "org": "",
        "tags": [],
        "sources": {},
        "vpn_details": None,
        "abuse_contact": None,
        "error": None,
    }

    # ---- ipapi.is ----
    data1 = _fetch(API_URLS["ipapi"].format(ip=ip))
    result["sources"]["ipapi.is"] = data1

    if data1 and "_error" not in data1:
        # Risk flags
        result["is_datacenter"] = data1.get("is_datacenter", False)
        result["is_proxy"] = data1.get("is_proxy", False)
        result["is_vpn"] = data1.get("is_vpn", False)
        result["is_tor"] = data1.get("is_tor", False)
        result["is_abuser"] = data1.get("is_abuser", False)
        result["is_mobile"] = data1.get("is_mobile", False)

        # Location
        loc = data1.get("location", {})
        if loc:
            result["location"] = {
                "country": loc.get("country", ""),
                "country_code": loc.get("country_code", ""),
                "city": loc.get("city", ""),
                "state": loc.get("state", ""),
            }

        # ASN / ISP
        asn = data1.get("asn", {})
        if asn:
            result.setdefault("asn_sources", {})["ipapi.is"] = {
                "num": asn.get("asn"),
                "raw": f"AS{asn.get('asn', '?')} {asn.get('descr', '')}",
            }

        company = data1.get("company", {})
        if company:
            result["org"] = company.get("name", "")
            result["isp"] = company.get("name", "")

        # VPN details
        vpn = data1.get("vpn")
        if vpn:
            result["vpn_details"] = {
                "service": vpn.get("service", ""),
                "type": vpn.get("type", ""),
                "last_seen": vpn.get("last_seen_str", ""),
            }

        # Abuse contact
        abuse = data1.get("abuse", {})
        if abuse:
            result["abuse_contact"] = abuse.get("email", "")

        # Build tags
        tags = []
        if data1.get("is_datacenter"):
            tags.append("数据中心/机房")
        if data1.get("is_proxy"):
            tags.append("代理")
        if data1.get("is_vpn"):
            tags.append("VPN")
        if data1.get("is_tor"):
            tags.append("Tor出口")
        if data1.get("is_abuser"):
            tags.append("已知滥用")
        if data1.get("is_crawler"):
            tags.append("爬虫")
        if data1.get("is_mobile"):
            tags.append("移动网络")
        result["tags"] = tags

    # ---- ip-api.com ----
    data2 = _fetch(API_URLS["ipapi_com"].format(ip=ip))
    result["sources"]["ip-api.com"] = data2

    if data2 and "_error" not in data2 and data2.get("status") == "success":
        if not result.get("isp"):
            result["isp"] = data2.get("isp", "")
        if not result.get("org"):
            result["org"] = data2.get("org", "")
        result.setdefault("asn_sources", {})["ip-api"] = {
            "raw": data2.get("as", ""),
        }
        if not result.get("location"):
            result["location"] = {
                "country": data2.get("country", ""),
                "city": data2.get("city", ""),
                "state": data2.get("regionName", ""),
            }

        # Proxy/hosting from ip-api
        if data2.get("proxy"):
            if "代理" not in result["tags"]:
                result["tags"].append("代理(ip-api)")
            result["is_proxy"] = True
        if data2.get("hosting"):
            if "数据中心/机房" not in result["tags"]:
                result["tags"].append("数据中心/机房")
            result["is_datacenter"] = True

    # ---- IPLogs ----
    data3 = _fetch_post(API_URLS["iplogs"], {"ip": ip})
    result["sources"]["iplogs.com"] = data3

    if data3 and "_error" not in data3:
        info = data3.get("ip_info") or {}
        verdict = data3.get("verdict", "")

        # Type from IPLogs
        iplogs_type = info.get("type", "")
        if iplogs_type == "datacenter" and not result["is_datacenter"]:
            result["is_datacenter"] = True
        if info.get("is_vpn") and not result["is_vpn"]:
            result["is_vpn"] = True
        if info.get("is_proxy") and not result["is_proxy"]:
            result["is_proxy"] = True

        # Abuse contact (fallback)
        if info.get("abuse_contact") and not result.get("abuse_contact"):
            result["abuse_contact"] = info["abuse_contact"]

        # Verdict-based tags
        if verdict in ("vpn_detected", "vpn_likely"):
            tag = "VPN(iplogs)"
            if tag not in result["tags"]:
                result["tags"].append(tag)
        if iplogs_type == "datacenter":
            tag = "数据中心/机房"
            if tag not in result["tags"]:
                result["tags"].append(tag)

        # ISP/ASN fallback
        if info.get("org") and not result.get("isp"):
            result["isp"] = info["org"]
        result.setdefault("asn_sources", {})["iplogs"] = {
            "raw": info.get("asn", ""),
        }

    # ---- ipinfo.io（地理定位参考） ----
    data4 = _fetch(API_URLS["ipinfo"].format(ip=ip))
    result["sources"]["ipinfo.io"] = data4

    if data4 and "_error" not in data4:
        # 仅用 ipinfo 补位置和 org，不做风险判定
        if not result.get("location", {}).get("country"):
            result["location"] = {
                "country": data4.get("country", ""),
                "city": data4.get("city", ""),
                "state": data4.get("region", ""),
            }
        org = data4.get("org", "")
        if org and not result.get("isp"):
            # org 格式是 "AS15169 Google LLC"
            parts = org.split(" ", 1)
            result["isp"] = parts[-1] if len(parts) > 1 else org
        result.setdefault("asn_sources", {})["ipinfo.io"] = {
            "raw": org or "",
        }

    # ---- PeeringDB netixlan（按 IP 查 ASN） ----
    result["peeringdb"] = None
    try:
        # IPv4 用 ipaddr4, IPv6 用 ipaddr6
        is_v6 = ":" in ip
        pdb_field = "ipaddr6" if is_v6 else "ipaddr4"
        pdb_url = f"https://peeringdb.com/api/netixlan?{pdb_field}={ip}"
        pdb_req = urllib.request.Request(pdb_url, headers={"User-Agent": "ipcheck-bot/1.0", "Accept": "application/json"})
        with urllib.request.urlopen(pdb_req, timeout=8) as resp:
            pdb_data = json.loads(resp.read().decode())
        pdb_records = pdb_data.get("data", [])
        if pdb_records:
            rec = pdb_records[0]
            result["peeringdb"] = {
                "asn": rec.get("asn"),
                "name": rec.get("name", ""),
                "speed": rec.get("speed", 0),
                "ix_id": rec.get("ix_id"),
            }
            result.setdefault("asn_sources", {})["peeringdb"] = {
                "num": rec.get("asn"),
                "raw": f"AS{rec.get('asn')}" if rec.get("asn") else "",
            }
            # 从 IX name 提取位置 (e.g. "Equinix Hong Kong")
            pdb_name = rec.get("name", "")
            for loc_hint in ["Hong Kong", "Singapore", "Tokyo", "London",
                             "New York", "Los Angeles", "Frankfurt", "Paris",
                             "Amsterdam", "Sydney", "Seoul", "Taipei",
                             "Shanghai", "Beijing", "Mumbai", "Chicago",
                             "Dallas", "Miami", "San Jose", "Toronto",
                             "Zurich", "Stockholm", "Milan", "Madrid",
                             "Sao Paulo", "Dubai", "Kuala Lumpur", "Jakarta"]:
                if loc_hint in pdb_name:
                    result["peeringdb"]["city"] = loc_hint
                    # 简单国家映射
                    country_map = {
                        "Hong Kong": "HK", "Singapore": "SG",
                        "Tokyo": "JP", "London": "GB", "New York": "US",
                        "Los Angeles": "US", "Frankfurt": "DE", "Paris": "FR",
                        "Amsterdam": "NL", "Sydney": "AU", "Seoul": "KR",
                        "Taipei": "TW", "Shanghai": "CN", "Beijing": "CN",
                        "Mumbai": "IN", "Chicago": "US", "Dallas": "US",
                        "Miami": "US", "San Jose": "US", "Toronto": "CA",
                        "Zurich": "CH", "Stockholm": "SE", "Milan": "IT",
                        "Madrid": "ES", "Sao Paulo": "BR", "Dubai": "AE",
                        "Kuala Lumpur": "MY", "Jakarta": "ID",
                    }
                    result["peeringdb"]["country"] = country_map.get(loc_hint, "")
                    break
    except Exception:
        pass

    # ---- ip2location.io（位置/ASN/代理检测） ----
    data5 = _fetch(API_URLS["ip2location"].format(ip=ip))
    result["sources"]["ip2location.io"] = data5

    if data5 and "_error" not in data5:
        # 位置补缺（仅当其他源全无数据时）
        if not result.get("location", {}).get("country"):
            result["location"] = {
                "country": data5.get("country_name", ""),
                "country_code": data5.get("country_code", ""),
                "city": data5.get("city_name", ""),
                "state": data5.get("region_name", ""),
            }
        # ISP/ASN 补缺
        if data5.get("as") and not result.get("isp"):
            result["isp"] = data5["as"]
        asn_num = data5.get("asn")
        if asn_num:
            result.setdefault("asn_sources", {})["ip2location.io"] = {
                "num": str(asn_num),
                "raw": f"AS{asn_num} {data5.get('as', '')}",
            }
        # Proxy 标记
        if data5.get("is_proxy") and not result["is_proxy"]:
            result["is_proxy"] = True

    # ---- 解析 ASN：全源收集，多数优先 ----
    result["asn"] = ""
    asn_sources = result.get("asn_sources", {})
    if asn_sources:
        # 提取 AS 号码，过滤无效值
        from collections import Counter
        asn_votes = {}
        for src, info in asn_sources.items():
            raw = info.get("raw", "")
            num = info.get("num")
            if num is not None and str(num) not in ("0", "None", ""):
                asn_votes[src] = {"num": str(num), "raw": raw}
            elif raw:
                # 从 raw 字符串里提取 AS+数字
                import re as _re
                m = _re.match(r"AS(\d+)", raw.strip())
                if m and m.group(1) != "0":
                    asn_votes[src] = {"num": m.group(1), "raw": raw}
        if asn_votes:
            # 多数决
            vote_count = Counter(v["num"] for v in asn_votes.values())
            most_common_num = vote_count.most_common(1)[0][0]
            primary_raw = ""
            minority_by_asn = {}  # {"9312": {"sources": ["ip-api", "iplogs"]}}
            for src, v in asn_votes.items():
                num = v["num"]
                if num == most_common_num:
                    if not primary_raw:
                        primary_raw = v["raw"]
                else:
                    minority_by_asn.setdefault(num, {"sources": []})
                    minority_by_asn[num]["sources"].append(src)
            if minority_by_asn:
                parts = []
                for asn_num, grp in minority_by_asn.items():
                    parts.append(f"AS{asn_num}({','.join(grp['sources'])})")
                result["asn_minority"] = " / ".join(parts)
            if primary_raw:
                result["asn"] = primary_raw
            else:
                result["asn"] = f"AS{most_common_num}"

    # ---- Compute score: 各源风险分加权 ----
    # 每个源输出自己的风险值 0.0(纯净) ~ 1.0(高危)
    source_risks = {}

    # IPLogs: 原生风险分 (0-1), 唯一有评分的源
    data3 = result["sources"].get("iplogs.com")
    if data3 and "_error" not in data3:
        source_risks["iplogs"] = data3.get("score", 0)

    # ipapi.is: 无原生评分, 从标记推导
    data1 = result["sources"].get("ipapi.is")
    if data1 and "_error" not in data1:
        flag_count = sum([
            data1.get("is_tor", False),
            data1.get("is_abuser", False),
            data1.get("is_vpn", False),
            data1.get("is_proxy", False),
            data1.get("is_datacenter", False),
        ])
        source_risks["ipapi"] = min(1.0, flag_count * 0.2)

    # ip-api.com: 无原生评分, 从 proxy/hosting 推导
    data2 = result["sources"].get("ip-api.com")
    if data2 and "_error" not in data2:
        if data2.get("proxy"):
            source_risks["ipapi_com"] = 0.8
        elif data2.get("hosting"):
            source_risks["ipapi_com"] = 0.4
        else:
            source_risks["ipapi_com"] = 0.0

    # 加权合并 (可信度: IPLogs > ipapi.is > ip-api.com)
    weights = {"iplogs": 0.50, "ipapi": 0.35, "ipapi_com": 0.15}
    total_w = sum(weights[k] for k in source_risks if k in weights)
    if total_w > 0:
        combined_risk = sum(source_risks[k] * weights.get(k, 0) for k in source_risks) / total_w
    else:
        combined_risk = 0.0

    # 机房附加风险：保留其他维度的差异
    if result["is_datacenter"]:
        combined_risk += 0.15

    score = int((1 - combined_risk) * 100)
    score = max(0, min(100, score))

    result["score"] = score
    result["risk_level"] = _risk_label(score)

    return result


def format_report(result: dict) -> str:
    """格式化为 Telegram HTML 报告"""
    import html as html_mod
    ip = result["ip"]
    score = result["score"]
    color = _score_color(score)
    risk = result["risk_level"]
    tags = result["tags"]
    loc = result.get("location", {})

    def esc(t):
        return html_mod.escape(str(t or ""))

    lines = []
    lines.append(f"🛡 <b>IP 纯净度检测</b>")
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

    # 收集各源国家代码，分歧追加 inline（同位置合并括号）
    main_cc = loc.get("country_code", "")
    alt_groups = {}  # (cc, city) -> [source_names]

    def add_alt(cc, city, source_name):
        if not main_cc or not cc or cc == main_cc:
            return
        key = (cc.upper(), city)
        if key not in alt_groups:
            alt_groups[key] = {"sources": [], "flag": _flag(cc.upper())}
        alt_groups[key]["sources"].append(source_name)

    # ipinfo.io
    ipinfo_raw = result.get("sources", {}).get("ipinfo.io")
    if isinstance(ipinfo_raw, dict) and "_error" not in ipinfo_raw and ipinfo_raw.get("country"):
        add_alt(ipinfo_raw["country"], ipinfo_raw.get("city", ""), "ipinfo.io")

    # ip-api.com
    ipapi_com_raw = result.get("sources", {}).get("ip-api.com")
    if isinstance(ipapi_com_raw, dict) and "_error" not in ipapi_com_raw and ipapi_com_raw.get("status") == "success":
        add_alt(ipapi_com_raw.get("countryCode", ""), ipapi_com_raw.get("city", ""), "ip-api")

    # IPLogs
    iplogs_raw = result.get("sources", {}).get("iplogs.com")
    if isinstance(iplogs_raw, dict) and "_error" not in iplogs_raw:
        info = iplogs_raw.get("ip_info") or {}
        add_alt(info.get("country_code", ""), info.get("city", ""), "iplogs")

    # PeeringDB
    pdb = result.get("peeringdb")
    if isinstance(pdb, dict) and pdb.get("country"):
        add_alt(pdb["country"], pdb.get("city", ""), "peeringdb")

    # ip2location.io
    ip2loc_raw = result.get("sources", {}).get("ip2location.io")
    if isinstance(ip2loc_raw, dict) and "_error" not in ip2loc_raw and ip2loc_raw.get("country_code"):
        add_alt(ip2loc_raw["country_code"], ip2loc_raw.get("city_name", ""), "ip2location.io")

    alt_positions = []
    for (cc, city), group in alt_groups.items():
        label = f"{group['flag']}{cc}/{city}({','.join(group['sources'])})" if city else f"{group['flag']}{cc}({','.join(group['sources'])})"
        alt_positions.append(label)

    if parts:
        main_flag = _flag(main_cc) if main_cc else ""
        line = f"🌍 <b>位置:</b> {' '.join(p for p in [main_flag, *parts] if p)}"
        if alt_positions:
            line += " / " + " / ".join(alt_positions)
        lines.append(line)

    if result.get("isp"):
        lines.append(f"🏢 <b>ISP:</b> {esc(result['isp'])}")
    if result.get("asn") and _is_valid_asn(result["asn"]):
        asn_line = f"🔗 <b>ASN:</b> {esc(result['asn'])}"
        if result.get("asn_minority"):
            asn_line += f" / {esc(result['asn_minority'])}"
        lines.append(asn_line)
    else:
        lines.append("🔗 <b>ASN:</b> 未知")

    if tags:
        lines += ["", f"🏷 <b>标记:</b> {' · '.join(esc(t) for t in tags)}"]

    # ---- 检测项：各源投票，多者优先，分歧标注 ----
    src = result.get("sources", {})
    d1 = src.get("ipapi.is", {})
    d2 = src.get("ip-api.com", {})
    d3 = src.get("iplogs.com", {})
    ipapi_ok = isinstance(d1, dict) and "_error" not in d1
    ipapi2_ok = isinstance(d2, dict) and "_error" not in d2 and d2.get("status") == "success"
    iplogs_ok = isinstance(d3, dict) and "_error" not in d3
    d4 = src.get("ip2location.io", {})
    ip2loc_ok = isinstance(d4, dict) and "_error" not in d4

    def vote(name: str) -> str:
        """返回格式化的检测项行，标注各源意见"""
        yes_srcs = []  # 说"是"的源
        no_srcs = []   # 说"否"的源（仅算有检测能力的）

        if ipapi_ok:
            if name == "数据中心":
                if d1.get("is_datacenter"):
                    yes_srcs.append("ipapi.is")
                else:
                    no_srcs.append("ipapi.is")
            elif name == "VPN":
                if d1.get("is_vpn"):
                    yes_srcs.append("ipapi.is")
                else:
                    no_srcs.append("ipapi.is")
            elif name == "代理":
                if d1.get("is_proxy"):
                    yes_srcs.append("ipapi.is")
                else:
                    no_srcs.append("ipapi.is")
            elif name == "Tor出口":
                if d1.get("is_tor"):
                    yes_srcs.append("ipapi.is")
                else:
                    no_srcs.append("ipapi.is")
            elif name == "已知滥用":
                if d1.get("is_abuser"):
                    yes_srcs.append("ipapi.is")
                else:
                    no_srcs.append("ipapi.is")

        if ipapi2_ok:
            if name == "代理" and d2.get("proxy"):
                yes_srcs.append("ip-api")
            elif name == "数据中心" and d2.get("hosting"):
                yes_srcs.append("ip-api")

        if iplogs_ok:
            info = d3.get("ip_info") or {}
            verdict = d3.get("verdict", "")
            if name == "数据中心" and info.get("type") == "datacenter":
                yes_srcs.append("iplogs")
            elif name == "VPN":
                if verdict == "vpn_detected":
                    yes_srcs.append("iplogs")
                elif verdict == "clean":
                    no_srcs.append("iplogs")

        if ip2loc_ok:
            if name == "代理" and d4.get("is_proxy"):
                yes_srcs.append("ip2location.io")

        verdict_flag = yes_srcs or no_srcs  # 至少有一个源能判断
        if not verdict_flag:
            return ""

        majority = bool(yes_srcs)
        icon = "❌" if majority else "✅"
        base = f"{icon} {name}: {'是' if majority else '否'}"

        # 仅有分歧时才标注各源（一致的不用重复）
        if yes_srcs and no_srcs:
            base += f" 是({','.join(yes_srcs)}) / 否({','.join(no_srcs)})"
        return base

    lines += ["", "<b>检测项:</b>"]
    for item in ["数据中心", "VPN", "代理", "Tor出口", "已知滥用"]:
        line = vote(item)
        if line:
            lines.append(line)

    vpn = result.get("vpn_details")
    if vpn and vpn.get("service"):
        lines.append(f"\n🔒 <b>VPN详情:</b> {esc(vpn['service'])} ({esc(vpn['type'])})")

    lines += [
        "",
        "📊 <b>数据源:</b>",
        "<blockquote>• <a href=\"https://ipapi.is\">IpApiIs</a> , <a href=\"https://ip-api.com\">Ip-Api</a>",
        "• <a href=\"https://iplogs.com\">IpLogs</a> , <a href=\"https://ipinfo.io\">IpInfo</a>",
        "• <a href=\"https://peeringdb.com\">PeeringDB</a> , <a href=\"https://ip2location.io\">Ip2Location</a></blockquote>",
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

    ip = sys.argv[1]
    result = check_ip(ip)
    print(format_report(result))
