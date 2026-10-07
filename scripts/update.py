#!/usr/bin/env python3
"""Build data.json for the 'mama lets watch movies' league.

Sources (all public, no API keys):
  * Vulture leaderboard JSON  -> rosters + official scores (URL found by scraping the hub page)
  * Box Office Mojo           -> domestic grosses, release dates, weekly #1 films
  * manual.json               -> Metascores, Letterboxd ratings, awards (edit by hand as results come in)
Scoring rules follow https://www.vulture.com/movies-league/
"""
import json, re, sys, gzip, datetime as dt, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEAGUE = "mama lets watch movies"
HUB = "https://www.vulture.com/movies-league/"
SCORING_START = dt.date(2026, 10, 2)
FIRST_WEEKEND_ISO = 40          # BOM weekend id 2026W40 = Oct 2-4, 2026
CRITIC_DATE = dt.date(2027, 1, 4)
LETTERBOXD_DATE = dt.date(2027, 3, 1)
UA = {"User-Agent": "Mozilla/5.0 (mfl-league-tracker)", "Accept-Encoding": "gzip"}

BOX_BONUSES = [(25, 10), (50, 15), (75, 15), (100, 20), (125, 15), (150, 15), (175, 15), (200, 25)]
CRITIC_BANDS = [(0, -10), (20, -5), (40, 0), (50, 10), (60, 20), (70, 40), (80, 70), (90, 100)]
LB_BANDS = [(0.0, -10), (1.0, -5), (2.0, 0), (2.5, 10), (3.0, 40), (3.5, 70), (4.0, 100)]


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        b = r.read()
        if r.headers.get("Content-Encoding") == "gzip" or b[:2] == b"\x1f\x8b":
            b = gzip.decompress(b)
        return b.decode("utf-8", "replace")


def norm(t):
    t = t.lower().replace("&amp;", "&").replace("&#x27;", "'").replace("’", "'")
    return re.sub(r"[^a-z0-9]+", "", t)


def band(value, bands):
    pts = None
    for lo, p in bands:
        if value >= lo:
            pts = p
    return pts


def box_office_points(gross, weeks_at_1):
    millions = int(gross // 1_000_000)
    parts = [{"kind": "box", "label": "Domestic box office", "detail": f"${gross/1e6:,.1f}M", "points": millions}]
    for thr, p in BOX_BONUSES:
        if gross >= thr * 1_000_000:
            parts.append({"kind": "box", "label": f"Cleared ${thr}M", "detail": "bonus", "points": p})
    if weeks_at_1:
        parts.append({"kind": "box", "label": "No. 1 at box office", "detail": f"{weeks_at_1} weekend(s) × 20", "points": 20 * weeks_at_1})
    return parts


def cells(row):
    return [re.sub(r"\s+", " ", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)] , row


def text(h):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", h)).strip()


def money(s):
    s = re.sub(r"[^\d]", "", s)
    return int(s) if s else 0


def fetch_year_table():
    """title -> {gross, release (date|None)} from BOM yearly chart."""
    h = get("https://www.boxofficemojo.com/year/2026/")
    out = {}
    for row in re.findall(r"<tr[ >].*?</tr>", h, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(tds) < 9:
            continue
        t = [text(x) for x in tds]
        # rank, title, ..., gross, theaters, total gross, release date, distributor
        title = t[1]
        gross = money(t[-5]) if False else None
        money_cells = [x for x in t if x.startswith("$")]
        total = money(money_cells[-1]) if money_cells else 0
        rel = None
        for x in t:
            m = re.fullmatch(r"([A-Z][a-z]{2}) (\d{1,2})", x)
            if m:
                rel = dt.datetime.strptime(f"{m.group(1)} {m.group(2)} 2026", "%b %d %Y").date()
        out[norm(title)] = {"title": title, "gross": total, "release": rel}
    return out


def fetch_number_ones(today):
    """norm(title) -> count of weekends at #1 since scoring start (only completed weekends)."""
    counts, wk = {}, FIRST_WEEKEND_ISO
    while True:
        sunday = dt.date.fromisocalendar(2026, wk, 7)
        if sunday > today:
            break
        try:
            h = get(f"https://www.boxofficemojo.com/weekend/2026W{wk}/")
        except Exception:
            break
        for row in re.findall(r"<tr[ >].*?</tr>", h, re.S):
            t = [text(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            if t and t[0] == "1" and len(t) > 2:
                counts[norm(t[2])] = counts.get(norm(t[2]), 0) + 1
                break
        wk += 1
    return counts


def fetch_vulture():
    hub = get(HUB)
    m = re.search(r'players-url="([^"]+)"', hub)
    if not m:
        raise SystemExit("Could not find leaderboard URL on Vulture hub page")
    url = m.group(1)
    rows = json.loads(get(url))
    upd = re.search(r"Updated ([A-Z][a-z]+ \d{1,2}, \d{4})", hub)
    return url, rows, upd.group(1) if upd else None


def main():
    today = dt.date.today()
    url, rows, updated = fetch_vulture()
    league = [r for r in rows if (r.get("leagueName") or "").strip().lower() == LEAGUE]
    total_players = len(rows)
    manual = json.loads((ROOT / "manual.json").read_text()) if (ROOT / "manual.json").exists() else {}
    aliases = {norm(k): norm(v) for k, v in manual.get("aliases", {}).items()}
    mm = {norm(k): v for k, v in manual.get("movies", {}).items()}

    try:
        year = fetch_year_table()
        ones = fetch_number_ones(today)
        bom_ok = True
    except Exception as e:
        print("BOM fetch failed:", e, file=sys.stderr)
        year, ones, bom_ok = {}, {}, False

    cache = {}

    def movie(title):
        k = norm(title)
        if k in cache:
            return cache[k]
        bk = aliases.get(k, k)
        info = year.get(bk)
        m = mm.get(k, {})
        parts, status = [], "unreleased"
        release = info["release"] if info else None
        if info and release and release >= SCORING_START:
            status = "in theaters"
            parts += box_office_points(info["gross"], ones.get(bk, 0))
        elif info and release:
            status = "released before scoring window"
        if m.get("metascore") is not None and today >= CRITIC_DATE:
            parts.append({"kind": "critic", "label": "Metacritic", "detail": f"Metascore {m['metascore']}", "points": band(m["metascore"], CRITIC_BANDS)})
        elif m.get("metascore") is not None:
            parts.append({"kind": "critic", "label": "Metacritic", "detail": f"Metascore {m['metascore']} (awarded Jan 4, 2027)", "points": 0, "pending": True})
        if m.get("letterboxd") is not None and today >= LETTERBOXD_DATE:
            parts.append({"kind": "audience", "label": "Letterboxd", "detail": f"{m['letterboxd']}★", "points": band(m["letterboxd"], LB_BANDS)})
        elif m.get("letterboxd") is not None:
            parts.append({"kind": "audience", "label": "Letterboxd", "detail": f"{m['letterboxd']}★ (awarded Mar 1, 2027)", "points": 0, "pending": True})
        for a in m.get("awards", []):
            parts.append({"kind": "award", "label": a["event"], "detail": a["category"], "points": a["points"]})
        res = {
            "title": title, "status": status,
            "release": release.isoformat() if release else None,
            "gross": info["gross"] if info and status == "in theaters" else None,
            "points": sum(p["points"] for p in parts), "breakdown": parts,
            "boxEligible": status != "released before scoring window",
            "byKind": {k: sum(p["points"] for p in parts if p["kind"] == k) for k in ("box", "critic", "audience", "award")},
        }
        cache[k] = res
        return res

    players = []
    for r in league:
        titles = [t.strip() for t in r["movies"].split(", ")]
        # titles containing ", " are rare; re-merge if a piece matches nothing and the next merges well
        ms = [movie(t) for t in titles]
        computed = sum(m["points"] for m in ms)
        players.append({
            "name": r["displayName"], "official": r["score"], "officialRank": r["ranking"],
            "total": computed, "movies": sorted(ms, key=lambda m: (-m["points"], m["title"])),
        })
    players.sort(key=lambda p: (-p["total"], -p["official"], p["name"].lower()))
    rank, prev = 0, None
    for i, p in enumerate(players, 1):
        if p["total"] != prev:
            rank, prev = i, p["total"]
        p["rank"] = rank

    scored = {}
    for m in cache.values():
        scored[m["title"]] = m
    out = {
        "league": "Mama Lets Watch Movies",
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "vultureUpdated": updated, "vultureSource": url, "totalPlayersOverall": total_players,
        "boxOfficeLive": bom_ok, "players": players,
    }
    (ROOT / "data.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"{len(players)} players, {len(cache)} movies, BOM ok={bom_ok}, vulture updated {updated}")


if __name__ == "__main__":
    main()
