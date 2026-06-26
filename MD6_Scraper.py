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

import urllib.request, json, time, threading, re, sys, os

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
PORT              = 8765

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
STATE_FILE = 'tracker_state.json'
stored_state = None

def _load_stored_state():
    global stored_state
    try:
        with open(STATE_FILE) as f:
            stored_state = json.load(f)
        print(f"[State] Loaded saved tracker state from {STATE_FILE}")
    except:
        stored_state = None

def _save_stored_state():
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(stored_state, f)
    except Exception as e:
        print(f"[State] Failed to persist state: {e}")

_load_stored_state()

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


# ── SBOE HELPERS ─────────────────────────────────────────────────────────────

def fetch_url(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
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
        "ts": time.strftime("%I:%M:%S %p"),
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
                    "early": pv(row[early_i]),
                    "ed":    pv(row[ed_i]),
                    "mail":  pv(row[mail_i]),
                }

            if method_data:
                early_tot = sum(v.get("early", 0) for v in method_data.values())
                ed_tot    = sum(v.get("ed",    0) for v in method_data.values())
                mail_tot  = sum(v.get("mail",  0) for v in method_data.values())
                log.append(
                    f"✅ Vote method: {len(method_data)} candidates — "
                    f"Early: {early_tot:,} | Election Day: {ed_tot:,} | Mail: {mail_tot:,} | "
                    f"Total: {early_tot+ed_tot+mail_tot:,}"
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

                # Need Early Voting, Election Day, Mail-In columns
                try:
                    early_i = next(i for i, h in enumerate(headers) if 'early' in h.lower())
                    ed_i    = next(i for i, h in enumerate(headers) if 'election' in h.lower() and 'day' in h.lower())
                    mail_i  = next(i for i, h in enumerate(headers) if 'mail' in h.lower())
                except StopIteration:
                    continue

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
                        'early': parse_vote(row[early_i]) if early_i < len(row) else 0,
                        'ed':    parse_vote(row[ed_i])    if ed_i    < len(row) else 0,
                        'mail':  parse_vote(row[mail_i])  if mail_i  < len(row) else 0,
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
                log.append(f"✅ {county_name}: Early {m['early']:,} | ED {m['ed']:,} | Mail {m['mail']:,}")
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

    ts = time.strftime("%I:%M:%S %p")
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
            page = browser.new_page()
            page.goto(AP_URL, wait_until="domcontentloaded", timeout=30000)
            # Wait for county table to render
            page.wait_for_selector("tr", timeout=15000)

            # Find the Democratic Primary section heading so we only parse Dem rows
            dem_y = 0
            try:
                for sel in ["h2", "h3", "h4", "strong", "b", "div", "span"]:
                    for el in page.query_selector_all(sel):
                        try:
                            if "Democratic Primary" in el.inner_text():
                                box = el.bounding_box()
                                if box:
                                    dem_y = box["y"]
                                    break
                        except:
                            pass
                    if dem_y > 0:
                        break
            except:
                pass

            if dem_y > 0:
                print(f"[AP] Found 'Democratic Primary' section at y={dem_y:.0f} — filtering to Dem rows only")
            else:
                print("[AP] No 'Democratic Primary' heading found — parsing all county rows")

            rows = page.query_selector_all("tr")
            ap_eevp = {}
            ap_total_votes = {}
            debug_logged = False

            for row in rows:
                if dem_y > 0:
                    try:
                        row_box = row.bounding_box()
                        if not row_box or row_box["y"] < dem_y:
                            continue
                    except:
                        pass

                text = row.inner_text().strip()
                if "County" not in text:
                    continue
                parts = [p.strip() for p in text.split("\t")]
                if len(parts) < 4:
                    continue

                county = parts[0].replace(" County", "").strip()

                # First-hit-wins — Democratic rows appear before Republican rows
                if county in ap_eevp:
                    continue

                if not debug_logged:
                    print(f"[AP DEBUG] Raw row for '{county}':")
                    for i, p in enumerate(parts):
                        print(f"  parts[{i}] = {repr(p)}")
                    debug_logged = True

                pct_found   = None
                total_found = None

                for p in reversed(parts[1:]):
                    p_clean = p.replace(",", "").replace("%", "").strip()
                    if not p_clean:
                        continue
                    if "%" in p and pct_found is None:
                        try:
                            v = float(p.replace("%", "").strip())
                            if 0 < v <= 100:
                                pct_found = v
                        except ValueError:
                            pass
                    elif "%" not in p and total_found is None:
                        try:
                            v = int(p.replace(",", "").strip())
                            if v > 0:
                                total_found = v
                        except ValueError:
                            pass
                    if pct_found is not None and total_found is not None:
                        break

                if pct_found is not None:
                    ap_eevp[county] = pct_found
                if total_found is not None:
                    ap_total_votes[county] = total_found

            browser.close()

            if ap_eevp:
                latest["apEevp"] = ap_eevp
                latest["apTotalVotes"] = ap_total_votes
                latest["apTs"] = time.strftime("%I:%M:%S %p")
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
        elif self.path == '/health':
            self._send_json(b'{"ok":true}')
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
                stored_state = json.loads(body)
                _save_stored_state()
                self._send_json(b'{"ok":true}')
            except Exception as e:
                self._send_json(b'{"error":"bad json"}', 400)

        elif self.path == '/config':
            # Tracker config (candidates, priors) — silent, no log
            try:
                latest["config"] = json.loads(body)
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
        except Exception as e:
            print(f"SBOE loop error: {e}")
        time.sleep(SBOE_REFRESH_SECS)


def ap_loop():
    time.sleep(10)  # let SBOE run first
    while True:
        try:
            scrape_ap()
        except Exception as e:
            print(f"AP loop error: {e}")
        time.sleep(AP_REFRESH_SECS)


# ── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  MD-6 Election Night Scraper")
    print(f"  Serving live data → http://localhost:{PORT}")
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
        HTTPServer(("localhost", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nScraper stopped.")
