#!/usr/bin/env python3
"""
MD-6 Election Night Live Scraper
---------------------------------
Fetches Maryland SBE results pages AND AP News county eevp data, then
serves parsed data at localhost:8765 so the tracker HTML can auto-update.

HOW TO USE:
  1. Double-click  Start_Scraper.command  (Mac) to launch this script.
  2. Keep the Terminal window open all night.
  3. The tracker will auto-update every few seconds.
  4. UPDATE THE URLS BELOW on election night with the 2026 live links.
"""

import urllib.request, json, time, threading, re, sys, os, datetime
try:
    from zoneinfo import ZoneInfo
    _EASTERN = ZoneInfo("America/New_York")
except ImportError:
    _EASTERN = None  # Python < 3.9 fallback

def _now_eastern(fmt):
    if _EASTERN:
        return datetime.datetime.now(_EASTERN).strftime(fmt)
    # Fallback: subtract 4 hours (EDT offset) — not DST-aware but close enough
    return (datetime.datetime.utcnow() - datetime.timedelta(hours=4)).strftime(fmt)

# ── UPDATE THESE URLS ON ELECTION NIGHT ──────────────────────────────────────
METHOD_URL = (
    "https://elections.maryland.gov/elections/2026/primary_results/"
    "gen_results_2026_4_6.html"
)
COUNTY_URL = (
    "https://elections.maryland.gov/elections/2026/primary_results/"
    "gen_detail_results_2026_4_6_Democratic_District%206.html"
)

# AP News page for MD-6 results — will go live election night
# URL pattern: https://apnews.com/projects/elections-2026/maryland-primary-results-us-house/#6
AP_URL = "https://apnews.com/projects/elections-2026/maryland-primary-results-us-house/#6"

SBOE_REFRESH_SECS = 5    # SBOE scrape interval
AP_REFRESH_SECS   = 15   # AP eevp scrape interval (seconds) — needs full browser render, ~15s minimum
PORT              = int(os.environ.get("PORT", 8765))

# Per-county pages — give Early / Election Day / Mail-In breakdown by candidate
# SBOE numbers counties alphabetically starting at 1 (Allegany=1, Anne Arundel=2, etc.)
# Verified: Allegany=1, Frederick=11, Garrett=12, Montgomery=16, Washington=22.
# Verify these URLs at elections.maryland.gov on election night if results don't appear.
COUNTY_METHOD_URLS = {
    'Allegany':   'https://elections.maryland.gov/elections/2026/primary_results/gen_results_2026_by_county_1.html',
    'Frederick':  'https://elections.maryland.gov/elections/2026/primary_results/gen_results_2026_by_county_11.html',
    'Garrett':    'https://elections.maryland.gov/elections/2026/primary_results/gen_results_2026_by_county_12.html',
    'Montgomery': 'https://elections.maryland.gov/elections/2026/primary_results/gen_results_2026_by_county_16.html',
    'Washington': 'https://elections.maryland.gov/elections/2026/primary_results/gen_results_2026_by_county_22.html',
}
# ─────────────────────────────────────────────────────────────────────────────

# Check for Playwright
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False

# ── OPERATOR PASSWORD ─────────────────────────────────────────────────────────
# Set via environment variable on Railway: OPERATOR_PASSWORD=yourpassword
# Falls back to 'md6night' if not set — change this before deploying!
OPERATOR_PASSWORD = os.environ.get('OPERATOR_PASSWORD', 'md6night')

# ── SHARED STATE (operator pushes, viewers pull) ───────────────────────────────
# Persistent data directory — use Railway Volume mounted at /app/data to survive redeployments.
# If the directory doesn't exist (local dev), files are written to the current directory instead.
_DATA_DIR   = '/app/data' if os.path.isdir('/app/data') else '.'
STATE_FILE  = os.path.join(_DATA_DIR, 'tracker_state.json')
CONFIG_FILE = os.path.join(_DATA_DIR, 'tracker_config.json')
stored_state = None
stored_config = {}   # persisted operator config (counties, priors, candidates)

def _load_stored_state():
    global stored_state, _prev_county_method
    try:
        with open(STATE_FILE) as f:
            stored_state = json.load(f)
        print(f"[State] Loaded saved tracker state from {STATE_FILE}")
        # Restore method baselines so scraper restarts don't lose drop detection context
        if stored_state and '_prevCountyMethod' in stored_state:
            _prev_county_method = stored_state['_prevCountyMethod']
            print(f"[State] Restored method baselines for {len(_prev_county_method)} counties")
    except:
        stored_state = None

def _save_stored_state():
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(stored_state, f)
    except Exception as e:
        print(f"[State] Failed to persist state: {e}")

def _load_stored_config():
    global stored_config
    try:
        with open(CONFIG_FILE) as f:
            stored_config = json.load(f)
        print(f"[Config] Loaded saved config from {CONFIG_FILE}")
        latest["config"] = stored_config
    except:
        stored_config = {}

def _save_stored_config():
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(stored_config, f)
    except Exception as e:
        print(f"[Config] Failed to persist config: {e}")

_load_stored_state()

# ── VOTE DROP DETECTION ───────────────────────────────────────────────────────

_prev_county        = {}   # county_name -> {cand_name: votes}
_prev_county_method = {}   # county_name -> {early, ed, mail, provisional}
_pending_drops      = {}   # county_name -> {entry, snap_cands, snap_methods, cycles_waited}

APRIL_NAME    = "April McClain Delaney"
TRONE_NAME    = "David J. Trone"
METHOD_LABELS = {'mail': 'Mail-In', 'early': 'Early Vote', 'ed': 'Election Day', 'provisional': 'Provisional'}
MAX_PENDING_CYCLES = 6   # wait up to 6 cycles (~30s at 5s interval) for method data to catch up

def _detect_drops():
    """Diff county results vs previous scrape; append new vote drops to stored_state voteFeed."""
    global _prev_county, _prev_county_method, _pending_drops, stored_state

    new_county  = latest.get('county', {})
    new_methods = {k: {m: v.get(m, 0) for m in ('early','ed','mail','provisional')}
                   for k, v in latest.get('countyMethod', {}).items()}

    # First run — capture baseline, don't emit drops
    if not _prev_county:
        _prev_county        = {k: dict(v) for k, v in new_county.items()}
        _prev_county_method = dict(new_methods)
        return

    ts   = _now_eastern("%-I:%M %p ET")
    feed = list((stored_state or {}).get('voteFeed', []))

    # ── Try to resolve pending entries (method data may have caught up) ──
    resolved = []
    for county, pending in list(_pending_drops.items()):
        # Use snap_methods as baseline; if empty (e.g. post-restart), fall back to current _prev_county_method
        old_m = pending['snap_methods'] or _prev_county_method.get(county, {})
        new_m = new_methods.get(county, {})
        method_key = None
        if new_m:
            best = max(('mail','early','ed','provisional'), key=lambda m: new_m.get(m,0) - old_m.get(m,0))
            if new_m.get(best, 0) - old_m.get(best, 0) > 0:
                method_key = best

        pending['cycles_waited'] += 1
        if method_key or pending['cycles_waited'] >= MAX_PENDING_CYCLES:
            # Emit with best available method (may still be None if data never updated)
            entry = pending['entry']
            entry['methodKey']   = method_key
            entry['methodLabel'] = METHOD_LABELS.get(method_key) if method_key else None
            feed = [entry] + feed
            resolved.append(county)
            print(f"[Drop] {county} +{entry['delta']:,} ({method_key or '?'}) — "
                  f"April +{entry['candDeltas'].get(APRIL_NAME,0)}, "
                  f"Trone +{entry['candDeltas'].get(TRONE_NAME,0)}"
                  + ('' if method_key else ' [method unknown]'))
            # Advance method snapshot now that we've published
            if new_m:
                _prev_county_method[county] = dict(new_m)

    for county in resolved:
        del _pending_drops[county]

    # ── Check for new vote deltas ──
    for county, cands in new_county.items():
        if county in _pending_drops:
            continue  # already waiting on a pending drop for this county

        old_cands = _prev_county.get(county, {})
        delta     = sum(cands.values()) - sum(old_cands.values())
        if delta <= 0:
            # No vote change — advance method snapshot so future deltas are relative
            if new_methods.get(county):
                _prev_county_method[county] = dict(new_methods[county])
            continue

        cand_deltas  = {c: cands[c] - old_cands.get(c, 0) for c in cands
                        if cands[c] - old_cands.get(c, 0) > 0}
        april_delta  = cand_deltas.get(APRIL_NAME, 0)
        trone_delta  = cand_deltas.get(TRONE_NAME, 0)
        margin_delta = april_delta - trone_delta

        # Try to resolve method now
        old_m = _prev_county_method.get(county, {})
        new_m = new_methods.get(county, {})
        method_key = None
        if old_m and new_m:
            best = max(('mail','early','ed','provisional'), key=lambda m: new_m.get(m,0) - old_m.get(m,0))
            if new_m.get(best, 0) - old_m.get(best, 0) > 0:
                method_key = best

        entry = {
            'ts':          ts,
            'county':      county,
            'delta':       delta,
            'candDeltas':  cand_deltas,
            'marginDelta': margin_delta,
            'methodKey':   method_key,
            'methodLabel': METHOD_LABELS.get(method_key) if method_key else None,
        }

        if method_key:
            # Method resolved immediately — emit now
            feed = [entry] + feed
            print(f"[Drop] {county} +{delta:,} ({method_key}) — "
                  f"April +{april_delta}, Trone +{trone_delta}")
            if new_m:
                _prev_county_method[county] = dict(new_m)
        else:
            # Method unclear — hold for up to MAX_PENDING_CYCLES cycles
            print(f"[Drop] {county} +{delta:,} (pending method) — "
                  f"April +{april_delta}, Trone +{trone_delta}")
            _pending_drops[county] = {
                'entry':         entry,
                'snap_methods':  dict(old_m) if old_m else {},
                'cycles_waited': 0,
            }

        # Always advance vote snapshot so we don't re-detect the same delta
        _prev_county[county] = dict(cands)

    if stored_state is None:
        stored_state = {}
    stored_state['voteFeed'] = feed[:50]
    stored_state['_prevCountyMethod'] = _prev_county_method  # persist so restarts don't lose baselines
    _save_stored_state()

    _prev_county        = {k: dict(v) for k, v in new_county.items()}
    # Note: _prev_county_method is updated per-county above; don't overwrite here

# ─────────────────────────────────────────────────────────────────────────────

latest = {
    "method": {},
    "county": {},
    "countyMethod": {},  # { "Frederick": { "early": 1200, "ed": 4500, "mail": 800 }, ... }
    "apEevp": {},        # { "Allegany": 45.2, "Frederick": 62.1, ... }
    "apTotalVotes": {},  # { "Allegany": 840, "Frederick": 12500, ... } — AP's reported total votes per county
    "ts": None,
    "apTs": None,
    "log": [],
    "refreshSecs": SBOE_REFRESH_SECS,   # tracker polls at this interval
    "config": {},        # operator's tracker config (candidates, counties, priors) — set via POST /config
}

_load_stored_config()  # restore persisted config into latest["config"] on startup


# ── SBOE HELPERS ─────────────────────────────────────────────────────────────

def fetch_url(url):
    # Append timestamp to bust any CDN/server-side cache — critical when running on hosted servers
    bust = f"?_={int(time.time())}" if '?' not in url else f"&_={int(time.time())}"
    req = urllib.request.Request(url + bust, headers={
        "User-Agent":      "Mozilla/5.0",
        "Cache-Control":   "no-cache, no-store",
        "Pragma":          "no-cache",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s)


def clean(s):
    return re.sub(r"\s+", " ", strip_tags(s)).strip()


def parse_tables(html):
    tables = []
    for tm in re.finditer(r"<table[^>]*>(.*?)</table>", html, re.I | re.S):
        rows = []
        for rm in re.finditer(r"<tr[^>]*>(.*?)</tr>", tm.group(1), re.I | re.S):
            cells = [clean(cm.group(1))
                     for cm in re.finditer(r"<t[hd][^>]*>(.*?)</t[hd]>", rm.group(1), re.I | re.S)]
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def pv(s):
    """Parse a vote string like '22,985 (40.38%)' → integer."""
    return int(re.sub(r"[^0-9]", "", s.split("(")[0]) or "0")


# ── SBOE SCRAPE ───────────────────────────────────────────────────────────────

def scrape_sboe():
    log = []
    result = {
        "method": {},
        "county": {},
        "ts": _now_eastern("%I:%M:%S %p"),
        "log": log,
        "refreshSecs": SBOE_REFRESH_SECS,
    }

    # ── VOTE METHOD PAGE ─────────────────────────────────────────────────────
    try:
        html = fetch_url(METHOD_URL)
        tables = parse_tables(html)
        method_data = {}

        for table in tables:
            if not table:
                continue
            headers = table[0]
            try:
                early_i = next(i for i, h in enumerate(headers) if "early"        in h.lower())
                ed_i    = next(i for i, h in enumerate(headers) if "election day"  in h.lower())
                mail_i  = next(i for i, h in enumerate(headers) if "mail"          in h.lower())
            except StopIteration:
                continue
            prov_i = next((i for i, h in enumerate(headers) if "prov" in h.lower()), None)
            full_text = " ".join(c for row in table for c in row).lower()
            if "democratic" not in full_text:
                continue

            for row in table[1:]:
                if len(row) <= max(early_i, ed_i, mail_i):
                    continue
                name = re.sub(r"winner selected|democratic|republican", "", row[0], flags=re.I).strip()
                if not name or "totals" in name.lower():
                    continue
                method_data[name] = {
                    "early":       pv(row[early_i]),
                    "ed":          pv(row[ed_i]),
                    "mail":        pv(row[mail_i]),
                    "provisional": pv(row[prov_i]) if prov_i is not None and prov_i < len(row) else 0,
                }

            if method_data:
                early_tot = sum(v.get("early",       0) for v in method_data.values())
                ed_tot    = sum(v.get("ed",           0) for v in method_data.values())
                mail_tot  = sum(v.get("mail",         0) for v in method_data.values())
                prov_tot  = sum(v.get("provisional",  0) for v in method_data.values())
                log.append(
                    f"✅ Vote method: {len(method_data)} candidates — "
                    f"Early: {early_tot:,} | ED: {ed_tot:,} | Mail: {mail_tot:,} | Prov: {prov_tot:,}"
                )
                break

        if not method_data:
            log.append("⚠️ Vote method: no Democratic table found")
        result["method"] = method_data

    except Exception as e:
        log.append(f"❌ Method page: {e}")

    # ── COUNTY BREAKDOWN PAGE ────────────────────────────────────────────────
    try:
        html = fetch_url(COUNTY_URL)
        tables = parse_tables(html)
        county_data = {}

        for table in tables:
            if not table:
                continue
            headers = table[0]
            if not headers or "jurisdiction" not in headers[0].lower():
                continue

            cand_names = []
            for h in headers[1:]:
                name = re.sub(r"winner selected|democratic|republican", "", h, flags=re.I).strip()
                cand_names.append(name)

            for row in table[1:]:
                if not row:
                    continue
                county = re.sub(r"\(.*\)", "", row[0]).strip()
                if not county or "total" in county.lower():
                    continue
                if county not in county_data:
                    county_data[county] = {}
                for i, cname in enumerate(cand_names, 1):
                    if not cname or i >= len(row):
                        continue
                    v = pv(row[i])
                    county_data[county][cname] = county_data[county].get(cname, 0) + v

        if county_data:
            sample = ", ".join(list(county_data.keys())[:3])
            log.append(f"✅ County: {len(county_data)} jurisdictions ({sample}…)")
        else:
            log.append("⚠️ County: no breakdown tables found")
        result["county"] = county_data

        # ── DISTRICT-WIDE PRECINCT COUNT from same page ──────────────────────
        pm = re.search(r'\(?\s*(\d[\d,]*)\s+of\s+(\d[\d,]*)\s+election\s+day\s+precinct', html, re.I)
        if not pm:
            pm = re.search(r'(\d[\d,]*)\s+of\s+(\d[\d,]*)\s+precinct', html, re.I)
        if pm:
            rep = int(pm.group(1).replace(',', ''))
            tot = int(pm.group(2).replace(',', ''))
            if not latest.get('precincts'):
                latest['precincts'] = {}
            latest['precincts']['district'] = {'reporting': rep, 'total': tot}
            log.append(f"✅ Precincts: {rep} of {tot} reporting districtwide")
        else:
            log.append("⚠️ Precincts: no precinct count found on county page")

    except Exception as e:
        log.append(f"❌ County page: {e}")

    latest.update(result)
    # Preserve apEevp across sboe scrapes
    if not latest.get("apEevp"):
        latest["apEevp"] = {}
    status = " | ".join(log)
    print(f"[{result['ts']}] SBOE: {status}")


# ── PER-COUNTY METHOD SCRAPE ─────────────────────────────────────────────────

def scrape_county_methods():
    """Fetch each county's by_county page and extract Early/ED/Mail totals for CD-6 Dems."""
    county_method = {}
    log = []

    def parse_vote(s):
        s = str(s).strip()
        if not s or s.upper() in ('NR', 'N/A', '—', '-', ''):
            return 0
        return int(re.sub(r'[^0-9]', '', s.split('(')[0]) or '0')

    for county_name, url in COUNTY_METHOD_URLS.items():
        try:
            html = fetch_url(url)

            # Locate the "Representative in Congress" section
            congress_pos = html.lower().find('representative in congress')
            if congress_pos == -1:
                log.append(f"⚠️ {county_name}: no Congress section")
                continue

            # Within that, find "District 6"
            d6_pos = html.lower().find('district 6', congress_pos)
            if d6_pos == -1:
                log.append(f"⚠️ {county_name}: no District 6")
                continue

            # Grab a chunk large enough to cover the District 6 Democratic table
            chunk = html[d6_pos:d6_pos + 10000]
            tables = parse_tables(chunk)

            found = False
            for table in tables:
                if not table or len(table) < 2:
                    continue
                headers = table[0]

                # Need Early Voting, Election Day, Mail-In columns (Provisional optional)
                try:
                    early_i = next(i for i, h in enumerate(headers) if 'early' in h.lower())
                    ed_i    = next(i for i, h in enumerate(headers) if 'election' in h.lower() and 'day' in h.lower())
                    mail_i  = next(i for i, h in enumerate(headers) if 'mail' in h.lower())
                except StopIteration:
                    continue
                prov_i = next((i for i, h in enumerate(headers) if 'prov' in h.lower()), None)

                # Must be a Democratic table
                text = ' '.join(c for row in table for c in row).lower()
                if 'democratic' not in text:
                    continue

                # Grab per-candidate rows AND totals row
                candidates = {}
                for row in table:
                    if not row:
                        continue
                    name = row[0].strip()
                    if not name:
                        continue
                    is_total = re.sub(r'\W', '', name).lower() in ('totals', 'total')
                    vals = {
                        'early':       parse_vote(row[early_i]) if early_i < len(row) else 0,
                        'ed':          parse_vote(row[ed_i])    if ed_i    < len(row) else 0,
                        'mail':        parse_vote(row[mail_i])  if mail_i  < len(row) else 0,
                        'provisional': parse_vote(row[prov_i])  if prov_i is not None and prov_i < len(row) else 0,
                    }
                    if is_total:
                        county_method[county_name] = {**vals, 'candidates': candidates}
                        found = True
                        break
                    else:
                        # Clean candidate name (strip party labels etc.)
                        clean_name = re.sub(r'winner selected|democratic|republican', '', name, flags=re.I).strip()
                        if clean_name:
                            candidates[clean_name] = vals

                if found:
                    break

            if found:
                m = county_method[county_name]
                log.append(f"✅ {county_name}: Early {m['early']:,} | ED {m['ed']:,} | Mail {m['mail']:,} | Prov {m.get('provisional',0):,}")
            else:
                log.append(f"⚠️ {county_name}: table parse failed")

            # ── Per-county precinct count ─────────────────────────────────────
            # Search the CD6 chunk first (most specific), then fall back to full page
            precinct_patterns = [
                r'\(?\s*(\d[\d,]*)\s+of\s+(\d[\d,]*)\s+election\s+day\s+precinct',
                r'(\d[\d,]*)\s+of\s+(\d[\d,]*)\s+precinct',
                r'precinct[^:]*?:\s*(\d[\d,]*)\s+of\s+(\d[\d,]*)',
            ]
            pm = None
            for pat in precinct_patterns:
                pm = re.search(pat, chunk, re.I)   # try CD6 section first
                if pm:
                    break
            if not pm:
                for pat in precinct_patterns:
                    pm = re.search(pat, html, re.I) # fall back to full page
                    if pm:
                        break
            if pm:
                rep = int(pm.group(1).replace(',', ''))
                tot = int(pm.group(2).replace(',', ''))
                if not latest.get('precincts'):
                    latest['precincts'] = {}
                latest['precincts'][county_name] = {'reporting': rep, 'total': tot}
                log.append(f"✅ {county_name} precincts: {rep}/{tot}")
            else:
                log.append(f"⚠️ {county_name}: no precinct count found")

        except Exception as e:
            log.append(f"❌ {county_name}: {e}")

    if county_method:
        latest['countyMethod'] = county_method

    ts = _now_eastern("%I:%M:%S %p")
    status = ' | '.join(log)
    print(f"[{ts}] CountyMethod: {status}")


# ── AP NEWS SCRAPE ────────────────────────────────────────────────────────────

def scrape_ap():
    """Scrape county-level Est. Votes Counted % from AP News results page."""
    if not AP_URL:
        return
    if not PLAYWRIGHT_OK:
        print("[AP] Playwright not installed — skipping AP scrape. Run: pip install playwright && playwright install chromium")
        return

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            MD6_COUNTIES = {"Allegany", "Frederick", "Garrett", "Montgomery", "Washington"}
            ap_eevp = {}
            ap_total_votes = {}

            # Intercept all apelections.org JSON responses
            captured = {}  # url -> body
            def on_response(response):
                try:
                    if "apelections.org" not in response.url:
                        return
                    captured[response.url] = response.text()
                except:
                    pass

            page.on("response", on_response)
            page.goto(AP_URL, wait_until="domcontentloaded", timeout=30000)
            # Scroll down to trigger lazy-load of results widget
            page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            page.wait_for_timeout(5000)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(5000)



            # Find the Democratic race by checking metadata party field
            dem_race_id = None
            for url, body in captured.items():
                if "/races/MD/" in url and "metadata.json" in url:
                    try:
                        meta = json.loads(body)
                        party = str(meta.get("party") or meta.get("partyID") or meta.get("partyName") or "")
                        if "Dem" in party or "Democrat" in party:
                            m = re.search(r'/races/MD/(20260623MD\w+)/', url)
                            if m:
                                dem_race_id = m.group(1)
                                print(f"[AP] Democratic race ID: {dem_race_id}")
                                break
                    except:
                        pass

            # Fallback: 20260623MD21841 was the Dem primary in this election
            target_race = dem_race_id or "20260623MD21841"

            # Parse detail.json — structure: {fipsCode: {reportingunitName, eevp, candidates:[{voteCount}]}}
            for url, body in captured.items():
                if f"/races/MD/{target_race}/detail.json" in url:
                    try:
                        data = json.loads(body)
                        for fips, unit in data.items():
                            if not isinstance(unit, dict):
                                continue
                            name = unit.get("reportingunitName", "")
                            county = name.replace(" County", "").strip()
                            if county not in MD6_COUNTIES or county in ap_eevp:
                                continue
                            eevp = unit.get("eevp")
                            total = sum(c.get("voteCount", 0) for c in unit.get("candidates", []))
                            if eevp is not None:
                                ap_eevp[county] = float(eevp)
                                ap_total_votes[county] = total
                                print(f"[AP] {county}: {eevp}% ({total} votes)")
                    except Exception as e:
                        print(f"[AP] Parse error: {e}")
                    break

            browser.close()

            if ap_eevp:
                latest["apEevp"] = ap_eevp
                latest["apTotalVotes"] = ap_total_votes
                latest["apTs"] = _now_eastern("%I:%M:%S %p")
                counties = ", ".join(f"{k} {v}% ({ap_total_votes.get(k,'?')} votes)" for k, v in ap_eevp.items())
                print(f"[{latest['apTs']}] AP: {len(ap_eevp)} counties → {counties}")
            else:
                print(f"[AP] No county eevp data found at {AP_URL}")

    except Exception as e:
        print(f"[AP] Error: {e}")


# ── HTTP SERVER ───────────────────────────────────────────────────────────────

from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def _send_json(self, body_bytes, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        self.wfile.write(body_bytes)

    def do_GET(self):
        if self.path == '/state':
            # Shared tracker state — viewers poll this
            self._send_json(json.dumps(stored_state or {}).encode())
        elif self.path == '/verify':
            # Password check — returns {"ok":true} or {"ok":false}
            pw = self.headers.get('X-Operator-Password', '')
            ok = pw == OPERATOR_PASSWORD
            self._send_json(json.dumps({"ok": ok}).encode())
        elif self.path == '/health':
            self._send_json(b'{"ok":true}')
        elif self.path == '/debug':
            import shutil
            info = {}
            for f in [STATE_FILE, CONFIG_FILE]:
                try:
                    info[f] = f'{round(os.path.getsize(f)/1024, 1)} KB'
                except:
                    info[f] = 'missing'
            try:
                total, used, free = shutil.disk_usage(_DATA_DIR)
                info['disk_total_mb'] = round(total/1024/1024, 1)
                info['disk_used_mb']  = round(used/1024/1024, 1)
                info['disk_free_mb']  = round(free/1024/1024, 1)
            except:
                pass
            info['countyMethod'] = latest.get('countyMethod', {})
            info['prevCountyMethod'] = _prev_county_method
            info['pendingDrops'] = list(_pending_drops.keys())
            self._send_json(json.dumps(info, indent=2).encode())
        else:
            # Default: live scraper data
            self._send_json(json.dumps(latest).encode())

    def do_POST(self):
        global stored_state
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)

        if self.path == '/state':
            # Operator pushes full tracker state — password required
            pw = self.headers.get('X-Operator-Password', '')
            if pw != OPERATOR_PASSWORD:
                self._send_json(b'{"error":"unauthorized"}', 403)
                return
            try:
                incoming = json.loads(body)
                # Preserve server-managed voteFeed — don't let client overwrite it
                server_feed = (stored_state or {}).get('voteFeed', [])
                stored_state = incoming
                if server_feed:
                    stored_state['voteFeed'] = server_feed
                _save_stored_state()
                self._send_json(b'{"ok":true}')
            except Exception as e:
                self._send_json(b'{"error":"bad json"}', 400)

        elif self.path == '/config':
            # Tracker config (candidates, priors) — persist to disk so it survives restarts
            try:
                global stored_config
                stored_config = json.loads(body)
                latest["config"] = stored_config
                _save_stored_config()
                self._send_json(b'{"ok":true}')
            except:
                self.send_response(400); self.end_headers()

        else:
            self.send_response(404); self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def log_message(self, *args):
        pass


def sboe_loop():
    while True:
        try:
            scrape_sboe()
            scrape_county_methods()
            # Detect drops AFTER county method data is updated so method attribution is correct
            _detect_drops()
        except Exception as e:
            print(f"SBOE loop error: {e}")
        time.sleep(SBOE_REFRESH_SECS)


def ap_loop():
    import concurrent.futures
    time.sleep(10)  # let SBOE run first
    while True:
        print(f"[AP] Starting scrape cycle...")
        try:
            # Run scrape_ap() in a thread with a hard 60-second timeout.
            # page.wait_for_timeout() can hang indefinitely if Playwright stalls —
            # this ensures the loop always continues even if the browser freezes.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(scrape_ap)
                try:
                    future.result(timeout=60)
                except concurrent.futures.TimeoutError:
                    print(f"[AP] Scrape timed out after 60s — skipping cycle")
        except Exception as e:
            print(f"[AP] Loop error: {e}")
        time.sleep(AP_REFRESH_SECS)


# ── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  MD-6 Election Night Scraper")
    print(f"  Serving live data → http://0.0.0.0:{PORT}")
    print(f"  SBOE: refreshing every {SBOE_REFRESH_SECS}s")
    if AP_URL:
        print(f"  AP:   refreshing every {AP_REFRESH_SECS}s  →  {AP_URL}")
    else:
        print("  AP:   no URL set — add AP_URL in scraper to enable")
    if not PLAYWRIGHT_OK:
        print("  ⚠️  Playwright not found. AP eevp scraping disabled.")
        print("     To enable: pip install playwright && playwright install chromium")
    print("  Keep this window open all night!")
    print("  Press Ctrl+C to stop.")
    print("=" * 60)

    threading.Thread(target=sboe_loop, daemon=True).start()
    threading.Thread(target=ap_loop,   daemon=True).start()
    try:
        HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nScraper stopped.")
