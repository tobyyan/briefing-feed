#!/usr/bin/env python3
"""Pull every source in sources.json, dedupe, and write:
   feed.xml          merged RSS 2.0 (subscribe in any reader)
   feed.json         items for the briefing (newest first)
   source_status.json  per-source ok/failed (failures go in Top 3)
Stdlib only. Usage: fetch_feeds.py [--out DIR] [--since-hours 36]"""
import argparse, json, re, sys, html, hashlib, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime, format_datetime
from urllib.parse import urljoin

UA = "Mozilla/5.0 (compatible; MorningBriefingBot/1.0)"
NS = {"atom": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/",
      "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#", "rss1": "http://purl.org/rss/1.0/"}

def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()

def parse_date(s):
    if not s: return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try: d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception: return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

def parse_feed(raw):
    root = ET.fromstring(raw)
    items = []
    for it in list(root.iter("item")) + list(root.iter("{http://purl.org/rss/1.0/}item")):
        g = lambda t: (it.findtext(t) or it.findtext("{http://purl.org/rss/1.0/}" + t) or "")
        items.append(dict(title=clean(g("title")), link=g("link").strip(),
            summary=clean(g("description"))[:600],
            date=parse_date(g("pubDate") or it.findtext("dc:date", namespaces=NS))))
    for e in root.iter("{http://www.w3.org/2005/Atom}entry"):
        l = e.find("atom:link", NS)
        items.append(dict(title=clean(e.findtext("atom:title", "", NS)),
            link=(l.get("href") if l is not None else ""),
            summary=clean(e.findtext("atom:summary", "", NS) or e.findtext("atom:content", "", NS))[:600],
            date=parse_date(e.findtext("atom:updated", "", NS) or e.findtext("atom:published", "", NS))))
    return items

def scrape_listing(raw, base):
    """Best-effort: take anchor links with substantial text. Dates are unknown, so
    these items carry date=None and are treated as 'new' only if not seen before."""
    txt = raw.decode("utf-8", "ignore")
    out, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', txt, re.S | re.I):
        t = clean(m.group(2))
        if len(t) < 25 or t in seen: continue
        seen.add(t)
        out.append(dict(title=t, link=urljoin(base, m.group(1)), summary="", date=None))
    return out[:40]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="."); ap.add_argument("--since-hours", type=int, default=36)
    ap.add_argument("--sources", default="sources.json"); ap.add_argument("--seen", default="seen.json")
    a = ap.parse_args()
    cfg = json.load(open(a.sources))["sources"]
    try: seen = set(json.load(open(a.seen)))
    except Exception: seen = set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=a.since_hours)
    items, status = [], []
    for s in cfg:
        try:
            raw = get(s["url"])
            got = parse_feed(raw) if s["type"] == "rss" else scrape_listing(raw, s["url"])
            if not got: raise ValueError("parsed zero items (feed moved or page layout changed)")
            n = 0
            for i in got:
                key = hashlib.sha1((i["link"] or i["title"]).encode()).hexdigest()[:16]
                fresh = (i["date"] >= cutoff) if i["date"] else (key not in seen)
                if not fresh or not i["title"]: continue
                i.update(source=s["name"], source_id=s["id"], cat=s["cat"], topic=s["topic"], key=key)
                items.append(i); n += 1
            status.append(dict(id=s["id"], name=s["name"], ok=True, fetched=len(got), new=n))
        except Exception as e:
            status.append(dict(id=s["id"], name=s["name"], ok=False, error=f"{type(e).__name__}: {e}"[:200]))
    uniq, k2 = [], set()
    for i in sorted(items, key=lambda x: x["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        t = re.sub(r"\W+", "", i["title"].lower())[:80]
        if i["key"] in k2 or t in k2: continue
        k2.update([i["key"], t]); uniq.append(i)
    json.dump(sorted(seen | {i["key"] for i in uniq}), open(a.seen, "w"))
    now = datetime.now(timezone.utc)
    ser = lambda i: {**i, "date": i["date"].isoformat() if i["date"] else None}
    json.dump(dict(generated=now.isoformat(), since_hours=a.since_hours, items=[ser(i) for i in uniq]),
              open(f"{a.out}/feed.json", "w"), indent=1)
    failed = [s for s in status if not s["ok"]]
    json.dump(dict(generated=now.isoformat(), failed=len(failed), total=len(status), sources=status),
              open(f"{a.out}/source_status.json", "w"), indent=1)
    rss = ET.Element("rss", version="2.0"); ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "Morning Briefing - merged feed"
    ET.SubElement(ch, "link").text = "https://example.invalid/feed.xml"
    ET.SubElement(ch, "description").text = "Merged, deduplicated items from configured sources"
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(now)
    for i in uniq:
        e = ET.SubElement(ch, "item")
        ET.SubElement(e, "title").text = f"[{i['source']}] {i['title']}"
        ET.SubElement(e, "link").text = i["link"]
        ET.SubElement(e, "guid", isPermaLink="false").text = i["key"]
        ET.SubElement(e, "category").text = i["cat"]
        ET.SubElement(e, "description").text = i["summary"]
        if i["date"]: ET.SubElement(e, "pubDate").text = format_datetime(i["date"])
    ET.ElementTree(rss).write(f"{a.out}/feed.xml", encoding="utf-8", xml_declaration=True)
    print(f"{len(uniq)} new items; {len(failed)}/{len(status)} sources failed")
    for s in failed: print("  FAIL", s["id"], s["error"])
    sys.exit(0)

if __name__ == "__main__": main()
