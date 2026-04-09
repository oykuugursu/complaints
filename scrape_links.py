"""
scrape_links.py  (v24 — universal scraper, all fixes applied)
=======================================================================

CHANGES vs v23:
  [F1]  COMPANY NAME PREFIX STRIPPING — strips leading nav words like
        "About", "Home", "Welcome to", "Introducing" from page title
        before using as company name.

  [F2]  COMPANY NAME CONFIRMATION BEFORE SEARCHES — after auto-detecting
        the company name, pauses in the console and asks the user to
        confirm or correct it BEFORE Layer 1 and Layer 2 run. This way
        all external searches use the correct name from the start.
        (Replaces the AHK post-search confirmation approach.)

  [F3]  UNIVERSAL OPENCORPORATES — removed hardcoded jurisdiction="in"
        (India). Now searches globally by default. Jurisdiction is read
        back from the matched result and used for officer lookup.

  [F4]  UNIVERSAL EXTERNAL DATABASES — removed India-only steps 8 and 9
        (tradeindia, iphex-india, pharmexcil, zaubacorp, tofler,
        quickcompany, thecompanycheck). Replaced with universal sources:
        Kompass, Europages, D&B Hoovers, Bureau van Dijk / Orbis,
        and global pharma DB PharmaCompass.

  [F5]  DDG 202 / RATE-LIMIT FIX — switched primary DDG search to the
        JSON API endpoint which is far less likely to serve CAPTCHA
        challenges. HTML scrape kept as fallback. Added retry with
        exponential backoff on 202/429, and longer randomised delays
        between queries.

  [F6]  All previous v23 fixes retained (E1-E7, D1-D6, C1-C15, F1-F4).
"""

import os, re, time, hashlib, random
from urllib.parse import urljoin, urlparse, urlunparse, quote_plus, unquote
from collections import deque, Counter
import subprocess

import requests
from bs4 import BeautifulSoup

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
# PATHS & SETTINGS
# ─────────────────────────────────────────────────────────────────────────────
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
INPUT_FILE  = os.path.join(SCRIPT_DIR, "scrape_url_input.txt")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "scrape_ai_ready.txt")

MAX_PAGES          = 200
MAX_CHARS_PER_PAGE = 8000
DELAY_SELENIUM     = 2.5
SCROLL_PAUSE       = 0.8

# GPT-4o context ~128k tokens = ~512k chars. Target 380k to leave room for response.
CHATGPT_CHAR_LIMIT = 380_000

BROAD_DDG_RESULTS       = 20
BROAD_DDG_SNIPPET_CHARS = 2500
TARGETED_DDG_RESULTS    = 5
TARGETED_SNIPPET_CHARS  = 2500
BROAD_FETCH_PER_QUERY   = 8

# ── OpenCorporates free API ───────────────────────────────────────────────────
OC_API_BASE = "https://api.opencorporates.com/v0.4"

# Domains we never try to fully fetch (paywalls / bot blocks)
SKIP_FETCH_DOMAINS = {
    "linkedin.com", "bloomberg.com", "wsj.com", "ft.com",
    "reuters.com", "businesswire.com", "prnewswire.com",
    "twitter.com", "x.com", "facebook.com", "youtube.com",
}

# Known valuable database domains — universal edition
HIGH_VALUE_DOMAINS = {
    "crunchbase.com", "opencorporates.com",
    "kompass.com", "europages.com",
    "dnb.com", "hoovers.com", "orbis.bvdinfo.com",
    "pharmacompass.com",
    "rocketreach.co",
    "sec.gov", "finance.yahoo.com",
    "espacenet.com", "patents.google.com",
    "trademo.com", "chemdmart.com",
    # India DBs kept in high-value list so snippets are not dropped by budget
    "zaubacorp.com", "tofler.in", "quickcompany.in", "thecompanycheck.com",
    "gmpfinder.com", "tradeindia.com", "iphex-india.com", "pharmexcil.com",
}

# ── [E1] Social media patterns ───────────────────────────────────────────────
SOCIAL_PATTERNS = {
    "LinkedIn Link":  re.compile(
        r'linkedin\.com/company/([\w\-\.%]+)', re.I),
    "YouTube Link":   re.compile(
        r'youtube\.com/(?:@[\w\-\.]+|channel/[\w\-]+|c/[\w\-]+|user/[\w\-]+)', re.I),
    "Facebook Link":  re.compile(
        r'facebook\.com/([\w\.\-]+(?:/[\w\.\-]+)?)', re.I),
    "Twitter Link":   re.compile(
        r'(?:twitter|x)\.com/([\w\-]+)', re.I),
    "Instagram Link": re.compile(
        r'instagram\.com/([\w\.\-]+)', re.I),
}

SOCIAL_SKIP_SLUGS = {
    "sharer", "share", "intent", "login", "signup", "join",
    "home", "feed", "notifications", "messaging", "search",
    "in", "pub", "jobs", "company", "pages", "groups",
    "watch", "results", "playlist", "shorts", "hashtag",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

DDG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://duckduckgo.com/",
}

SKIP_EXTENSIONS = {
    ".css",".js",".json",".xml",".png",".jpg",".jpeg",".gif",".webp",
    ".svg",".ico",".bmp",".woff",".woff2",".ttf",".eot",".otf",
    ".pdf",".zip",".gz",".tar",".rar",".mp4",".mp3",".avi",".mov",
    ".wmv",".webm",".csv",".xls",".xlsx",".doc",".docx",".map",
}

SKIP_PATH_SEGMENTS = {
    "_next/static","_next/image","wp-content/uploads",
    "wp-content/themes","wp-content/plugins","wp-includes",
    "static/css","static/js","static/media",
    "assets/css","assets/js","assets/fonts","assets/images",
    "cdn-cgi","node_modules","/__nextjs",
}

PRIORITY_SLUGS = {
    "team","about","about-us","people","leadership","management",
    "pipeline","research","publications","services","contact","contact-us",
    "founders","investors","news","platform","technology","history",
}

JS_SHELL_SIGNALS = [
    "wix.com/","_wix_","wixsite","wixstatic",
    "squarespace.com","squarespace-cdn",
    "webflow.com","webflow.io",
    "__NEXT_DATA__","gatsby-chunk","react-root",
    'id="root"',"id='root'",'id="app"',"id='app'",
]

_NOISE_RE = re.compile(
    r'^(home|about us?|contact us?|menu|navigation|search|login|sign in|sign up|'
    r'log in|register|accept cookies?|privacy policy|terms of (use|service)|'
    r'sitemap|skip to (main )?content|follow us|share this|back to top|'
    r'all rights reserved|copyright \d{4}|subscribe|newsletter|loading\.\.\.|'
    r'read more|learn more|click here|tweet|facebook|linkedin|instagram|'
    r'youtube|twitter|pinterest|accept|reject all|close|ok|cancel|'
    r'submit|send message|next|previous|scroll down|toggle menu|'
    r'expand|collapse)$',
    re.IGNORECASE
)

_INSTRUCTIONS_OVERHEAD = 8000
_HEADER_OVERHEAD       = 2000

# ── [F1] Company name prefix stripping ───────────────────────────────────────
_TITLE_PREFIX_RE = re.compile(
    r'^(?:about\s+us[:\s]*|about|home|welcome\s+to|introducing|meet|'
    r'this\s+is)\s+',
    re.IGNORECASE
)
_NAV_WORDS = {"home", "index", "about", "main", "welcome"}

# ── [E2] Company name suffix stripping ───────────────────────────────────────
_SUFFIX_RE = re.compile(
    r'\s*[,\.]?\s*(?:'
    r'private limited|pvt\.?\s*ltd\.?|pvt ltd|'
    r'public limited|'
    r'limited liability company|llc|'
    r'incorporated|inc\.?|'
    r'limited|ltd\.?|'
    r'gmbh|ag|sa|sas|srl|bv|nv|oy|ab|as|'
    r'co\.\s*ltd\.?|co\.,?\s*ltd\.?|'
    r'llp|lp|plc|pty\.?\s*ltd\.?'
    r')\s*$',
    re.IGNORECASE
)

def strip_suffix(name):
    return _SUFFIX_RE.sub("", name).strip()

def name_variants(company_name):
    short = strip_suffix(company_name)
    if short and short.lower() != company_name.lower():
        return [company_name, short]
    return [company_name]


# ─────────────────────────────────────────────────────────────────────────────
# CLIPBOARD  (Windows)
# ─────────────────────────────────────────────────────────────────────────────

def copy_to_clipboard(text):
    try:
        proc = subprocess.run(
            ["powershell", "-Command", "Set-Clipboard -Value $input"],
            input=text, text=True, encoding="utf-8",
            capture_output=True, timeout=20,
        )
        if proc.returncode == 0:
            print("  Copied to clipboard.", flush=True)
            return
    except Exception:
        pass
    try:
        tmp = os.path.join(SCRIPT_DIR, "_clip_tmp.txt")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.system(f'type "{tmp}" | clip')
        try:
            os.remove(tmp)
        except Exception:
            pass
        print("  Copied to clipboard (via clip.exe).", flush=True)
    except Exception as e:
        print(f"  Could not copy to clipboard: {e}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# URL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def normalize(url):
    p    = urlparse(url)
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", "", ""))

def base_domain(url):
    return urlparse(url).netloc.lower().lstrip("www.")

def same_site(url, domain):
    h = urlparse(url).netloc.lower()
    return h == domain or h == "www." + domain or h.lstrip("www.") == domain

def should_skip_url(url):
    parsed = urlparse(url)
    path   = parsed.path.lower()
    _, ext = os.path.splitext(path)
    if ext in SKIP_EXTENSIONS:
        return True
    for seg in SKIP_PATH_SEGMENTS:
        if seg in path:
            return True
    return False

def should_skip_fetch(url):
    host = urlparse(url).netloc.lower().lstrip("www.")
    return any(host == d or host.endswith("." + d) for d in SKIP_FETCH_DOMAINS)

def is_high_value(url):
    host = urlparse(url).netloc.lower().lstrip("www.")
    return any(host == d or host.endswith("." + d) for d in HIGH_VALUE_DOMAINS)

def snippet_priority(snippet):
    if is_high_value(snippet["url"]):
        return 2
    if snippet.get("text"):
        return 1
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# [E1] SOCIAL LINK EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def _is_valid_social_slug(label, slug):
    slug_clean = slug.strip("/").split("/")[0].lower()
    if slug_clean in SOCIAL_SKIP_SLUGS:
        return False
    if label == "Twitter Link" and slug_clean in {"twitter", "x", "share", "intent"}:
        return False
    if label == "Facebook Link" and slug_clean in {"sharer", "share", "dialog"}:
        return False
    return True

def extract_social_links_from_html(html, base_url):
    found = {}
    raw   = html if isinstance(html, str) else html.decode("utf-8", errors="ignore")
    soup  = BeautifulSoup(raw, "html.parser")
    for tag in soup.find_all(href=True):
        href = tag["href"].strip()
        for label, pattern in SOCIAL_PATTERNS.items():
            if label in found:
                continue
            m = pattern.search(href)
            if m:
                slug = m.group(0).split("/", 1)[-1] if "/" in m.group(0) else m.group(1) if m.lastindex else ""
                if not _is_valid_social_slug(label, slug):
                    continue
                full = href if href.startswith("http") else "https://www." + m.group(0)
                full = full.split("?")[0].rstrip("/")
                found[label] = full
    for label, pattern in SOCIAL_PATTERNS.items():
        if label in found:
            continue
        for m in pattern.finditer(raw):
            matched = m.group(0)
            slug    = matched.split("/", 1)[-1] if "/" in matched else ""
            if not _is_valid_social_slug(label, slug):
                continue
            full = ("https://www." + matched).split("?")[0].rstrip("/")
            found[label] = full
            break
    return found

def collect_site_social_links(pages_html):
    merged = {}
    for url, html in pages_html:
        links = extract_social_links_from_html(html, url)
        for label, value in links.items():
            if label not in merged:
                merged[label] = value
                print(f"  [social] Found {label}: {value}  (from {url[:60]})", flush=True)
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# SELENIUM
# ─────────────────────────────────────────────────────────────────────────────

def make_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--log-level=3")
    opts.add_argument(f'user-agent={HEADERS["User-Agent"]}')
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)

def selenium_fetch(driver, url):
    driver.get(url)
    time.sleep(DELAY_SELENIUM)
    last_h = driver.execute_script("return document.body.scrollHeight")
    for _ in range(6):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(SCROLL_PAUSE)
        new_h = driver.execute_script("return document.body.scrollHeight")
        if new_h == last_h:
            break
        last_h = new_h
    return driver.page_source, driver.title or url


# ─────────────────────────────────────────────────────────────────────────────
# JS SHELL DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def is_js_shell(html, body_text):
    for sig in JS_SHELL_SIGNALS:
        if sig in html:
            return True
    if len(html) > 5000 and len(body_text) < 800 and len(body_text) / len(html) < 0.05:
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# NOISE STRIPPING & GLOBAL DEDUPLICATION
# ─────────────────────────────────────────────────────────────────────────────

def strip_noise_lines(text):
    lines   = text.splitlines()
    cleaned = []
    for ln in lines:
        s = ln.strip()
        if not s:
            cleaned.append("")
            continue
        if len(s) < 2:
            continue
        if len(s) <= 60 and _NOISE_RE.match(s):
            continue
        if re.match(r'^https?://\S+$', s):
            continue
        if re.match(r'^[\W_]+$', s):
            continue
        cleaned.append(ln)
    result, blank_run = [], 0
    for ln in cleaned:
        if not ln.strip():
            blank_run += 1
            if blank_run <= 1:
                result.append(ln)
        else:
            blank_run = 0
            result.append(ln)
    return "\n".join(result)

def build_global_noise(pages):
    if len(pages) < 4:
        return set()
    threshold   = max(6, len(pages) // 2)
    line_counts = Counter()
    for p in pages:
        seen = set()
        for ln in p["text"].splitlines():
            norm = ln.strip().lower()
            if len(norm) < 15 or norm in seen:
                continue
            line_counts[norm] += 1
            seen.add(norm)
    return {ln for ln, cnt in line_counts.items() if cnt >= threshold}

def apply_global_dedup(pages):
    noise = build_global_noise(pages)
    if noise:
        print(f"  [dedup] Suppressing {len(noise)} repeated nav/footer lines.", flush=True)
    deduped = []
    for p in pages:
        lines   = p["text"].splitlines()
        cleaned = [ln for ln in lines if ln.strip().lower() not in noise]
        result, blank_run = [], 0
        for ln in cleaned:
            if not ln.strip():
                blank_run += 1
                if blank_run <= 1:
                    result.append(ln)
            else:
                blank_run = 0
                result.append(ln)
        p2         = dict(p)
        p2["text"] = "\n".join(result)
        deduped.append(p2)
    return deduped

def compress_snippet_text(text, max_chars=1500):
    FILLER = re.compile(
        r'\b(click here|read more|learn more|all rights reserved|cookie policy|'
        r'privacy policy|terms of use|follow us|subscribe to|newsletter|'
        r'loading\.\.\.|enable javascript|your browser|we use cookies|'
        r'accept cookies|gdpr|data protection|manage preferences|'
        r'powered by|built with|designed by)\b',
        re.IGNORECASE
    )
    lines = text.splitlines()
    good  = []
    for ln in lines:
        s = ln.strip()
        if not s or len(s) < 25:
            continue
        if _NOISE_RE.match(s) and len(s) <= 60:
            continue
        if FILLER.search(s):
            continue
        good.append(s)
    deduped, prev = [], None
    for ln in good:
        if ln != prev:
            deduped.append(ln)
        prev = ln
    return "\n".join(deduped)[:max_chars]


# ─────────────────────────────────────────────────────────────────────────────
# [F5] DUCKDUCKGO SEARCH — JSON API primary, HTML scrape fallback
# ─────────────────────────────────────────────────────────────────────────────

_ddg_fail_streak = 0  # tracks consecutive 202/429 hits for escalating backoff

def ddg_search(query, num_results=8):
    """
    [F5] Primary: DDG Instant Answer JSON API (rarely rate-limited).
    Fallback: DDG HTML scrape if JSON returns no results.
    """
    global _ddg_fail_streak

    results = _ddg_json_search(query, num_results)
    if results:
        _ddg_fail_streak = 0
        return results

    # Fallback to HTML scrape
    results = _ddg_html_search(query, num_results)
    if results:
        _ddg_fail_streak = 0
    return results


def _ddg_json_search(query, num_results=8):
    """
    DDG Instant Answer JSON API.
    Returns RelatedTopics links — less comprehensive than HTML but
    almost never triggers CAPTCHA / 202 responses.
    """
    try:
        params = {
            "q":             query,
            "format":        "json",
            "no_html":       "1",
            "skip_disambig": "1",
        }
        time.sleep(random.uniform(1.0, 2.0))
        r = requests.get(
            "https://api.duckduckgo.com/",
            params=params,
            headers=HEADERS,
            timeout=15,
        )
        if r.status_code != 200:
            return []

        data    = r.json()
        results = []

        for topic in data.get("RelatedTopics", []):
            if len(results) >= num_results:
                break
            if "Topics" in topic:
                for sub in topic["Topics"]:
                    if len(results) >= num_results:
                        break
                    href = sub.get("FirstURL", "")
                    text = sub.get("Text", "")
                    if href and text:
                        results.append({"title": text[:80], "url": href, "snippet": text})
            else:
                href = topic.get("FirstURL", "")
                text = topic.get("Text", "")
                if href and text:
                    results.append({"title": text[:80], "url": href, "snippet": text})

        return results

    except Exception:
        return []


def _ddg_html_search(query, num_results=8, _retry=0):
    """
    [F5] DDG HTML scrape with exponential backoff on 202/429.
    Delays: base 2-4s + 3s per failure streak. Retries up to 3 times.
    Backoff per retry: 5s → 15s → 45s.
    """
    global _ddg_fail_streak
    results = []
    try:
        url   = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        delay = random.uniform(2.0, 4.0) + (_ddg_fail_streak * 3.0)
        time.sleep(delay)

        r = requests.get(url, headers=DDG_HEADERS, timeout=15)

        if r.status_code in (202, 429):
            _ddg_fail_streak += 1
            backoff = min(5 * (3 ** _retry), 120)
            print(
                f"    [ddg] HTTP {r.status_code} (streak {_ddg_fail_streak})"
                f" — waiting {backoff}s before retry {_retry+1}/3: {query[:50]}",
                flush=True,
            )
            if _retry < 3:
                time.sleep(backoff)
                return _ddg_html_search(query, num_results, _retry + 1)
            else:
                print(f"    [ddg] Giving up after 3 retries: {query[:60]}", flush=True)
                return results

        if r.status_code != 200:
            print(f"    [ddg] HTTP {r.status_code}: {query[:60]}", flush=True)
            return results

        _ddg_fail_streak = 0
        soup = BeautifulSoup(r.text, "html.parser")
        for result in soup.select(".result"):
            a = result.select_one(".result__a")
            if not a:
                continue
            href = a.get("href", "")
            if "uddg=" in href:
                m = re.search(r'uddg=([^&]+)', href)
                if m:
                    href = unquote(m.group(1))
            if not href.startswith("http"):
                continue
            title = a.get_text(strip=True)
            stag  = result.select_one(".result__snippet")
            snip  = stag.get_text(strip=True) if stag else ""
            if href and title:
                results.append({"title": title, "url": href, "snippet": snip})
            if len(results) >= num_results:
                break

        if not results:
            print(f"    [ddg] 0 results: {query[:60]}", flush=True)

    except Exception as e:
        print(f"    [ddg] error: {e}", flush=True)
    return results


def fetch_page_text(url, max_chars=2500):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.content, "html.parser")
        for tag in soup(["script","style","nav","footer","header","aside"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
        return compress_snippet_text(text, max_chars=max_chars)
    except Exception:
        return ""

def _source_label(url):
    host = urlparse(url).netloc.lower().lstrip("www.")
    for known in [
        "crunchbase","opencorporates","sec.gov","yahoo","bloomberg",
        "reuters","kompass","europages","dnb","hoovers","orbis",
        "pharmacompass","rocketreach","trademo","chemdmart",
    ]:
        if known in host:
            return known.replace(".","_").title()
    parts = host.split(".")
    return parts[-2].title() if len(parts) >= 2 else host


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-EXTRACT FIELDS FROM PAGE TEXT
# ─────────────────────────────────────────────────────────────────────────────

def extract_fields_from_text(page_text, src, findings):
    if "Year Founded" not in findings:
        m = re.search(r'[Ff]ounded[:\s]+(\d{4})', page_text)
        if not m:
            m = re.search(
                r'(?:incorporated|incorporation date|date of incorporation|'
                r'cr[ée]{1,2}e?\s+en|fond[ée]{1,2}e?\s+en)'
                r'[:\s]+(?:\d{1,2}[\-/]\d{1,2}[\-/])?(\d{4})',
                page_text, re.IGNORECASE)
        if m:
            yr  = re.search(r'\b(20\d{2}|19\d{2})\b', m.group(1))
            val = (yr.group(1) if yr else m.group(1)).strip()
            findings["Year Founded (external)"] = f"{val} (* {src})"
            print(f"          → Year Founded: {val}", flush=True)

    if "Address (raw)" not in findings:
        m = re.search(
            r'(?:headquarter|HQ|registered address|address|located(?:\s+at)?|'
            r'si[eè]ge social|adresse)[:\s]+'
            r'([^\n]{15,150})',
            page_text, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            findings["Address (raw)"] = f"{val} (* {src})"
            print(f"          → Address: {val}", flush=True)

    found_names = extract_all_founders(page_text)
    if found_names:
        existing = findings.get("Founders (external)", "")
        ex_list  = [n.strip()
                    for n in re.sub(r'\(\* [^)]+\)', '', existing).split("|")
                    if n.strip()]
        added = False
        for n in found_names:
            if n not in ex_list:
                ex_list.append(n)
                added = True
        if added:
            findings["Founders (external)"] = (
                " | ".join(ex_list) + f" (* {src})")
            print(f"          → Founders: {ex_list}", flush=True)

    if "Funding (external)" not in findings:
        m = re.search(
            r'\$[\d\.]+ ?[MmBb]illion|\$[\d,]+[ \t]+(?:million|billion|[Mm]|[Bb])',
            page_text)
        if m:
            findings["Funding (external)"] = f"{m.group(0)} (* {src})"

    if "CIN" not in findings:
        m = re.search(r'\b([UL]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6})\b', page_text)
        if m:
            findings["CIN"] = f"{m.group(1)} (* {src})"
            print(f"          → CIN: {m.group(1)}", flush=True)

    dirs = re.findall(
        r'(?:Director|DIN)[:\s]+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        page_text)
    if dirs and "Officers (India DB)" not in findings:
        findings["Officers (India DB)"] = (
            " | ".join(dict.fromkeys(dirs[:10])) + f" (* {src})")
        print(f"          → Directors: {dirs[:5]}", flush=True)


def extract_all_founders(text):
    NAME  = r'[A-Z][a-z]+(?:[ -][A-Z][a-z]+)+'
    names = []
    for chunk_m in re.finditer(
            r'founded\s+by\s+(.+?)(?:\s+in\b|\s+at\b|\s+with\b|\.|\Z)',
            text, re.IGNORECASE):
        for p in re.split(r'\s*,\s*|\s+and\s+', chunk_m.group(1).strip()):
            p = p.strip()
            if re.match(r'^' + NAME + r'$', p) and p not in names:
                names.append(p)
    m = re.search(
        r'(?:Drs?\.\s*)(' + NAME + r')'
        r'(?:\s+and\s+(' + NAME + r'))?'
        r'\s+(?:are\s+the\s+)?founders?', text)
    if m:
        for g in [m.group(1), m.group(2)]:
            if g and g not in names:
                names.append(g)
    for m3 in re.finditer(r'(' + NAME + r').{0,30}?(?:co-?founder|founder)', text):
        n = m3.group(1)
        if n not in names:
            names.append(n)
    cleaned = []
    for n in names:
        n = re.sub(r'^(?:Dr|Drs|Prof|Mr|Ms|Mrs)\.?\s+', '', n)
        if len(n.split()) >= 2:
            cleaned.append(n.strip())
    return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# [F3] OPENCORPORATES FREE API — universal, no hardcoded jurisdiction
# ─────────────────────────────────────────────────────────────────────────────

def opencorporates_api_search(company_name, jurisdiction=None):
    """
    [F3] Search OpenCorporates globally by default.
    Pass jurisdiction only if you already know it (e.g. "fr", "us_de").
    Jurisdiction is read from the result, not assumed.
    """
    for name_try in name_variants(company_name):
        try:
            params = {"q": name_try, "format": "json"}
            if jurisdiction:
                params["jurisdiction_code"] = jurisdiction

            r = requests.get(
                f"{OC_API_BASE}/companies/search",
                params=params,
                headers={"User-Agent": HEADERS["User-Agent"]},
                timeout=15,
            )
            if r.status_code == 429:
                print("    [OC API] Rate limited — waiting 30s...", flush=True)
                time.sleep(30)
                r = requests.get(
                    f"{OC_API_BASE}/companies/search",
                    params=params,
                    timeout=15,
                )
            if r.status_code != 200:
                print(f"    [OC API] HTTP {r.status_code} for '{name_try}'", flush=True)
                continue

            data      = r.json()
            companies = data.get("results", {}).get("companies", [])
            if not companies:
                continue

            name_lower = name_try.lower()
            for item in companies:
                co   = item.get("company", {})
                co_n = co.get("name", "").lower()
                if name_lower.split()[0] in co_n:
                    print(
                        f"    [OC API] Matched: {co.get('name')} "
                        f"({co.get('jurisdiction_code')}/{co.get('company_number')})",
                        flush=True,
                    )
                    return co

            co = companies[0].get("company", {})
            print(f"    [OC API] Top result (fuzzy): {co.get('name')}", flush=True)
            return co

        except Exception as e:
            print(f"    [OC API] error for '{name_try}': {e}", flush=True)

    return None


def opencorporates_api_officers(company_number, jurisdiction):
    """[F3] Fetch officers. Jurisdiction comes from the search result."""
    try:
        url = f"{OC_API_BASE}/companies/{jurisdiction}/{company_number}/officers"
        r   = requests.get(
            url,
            headers={"User-Agent": HEADERS["User-Agent"]},
            timeout=15,
        )
        if r.status_code == 429:
            time.sleep(30)
            r = requests.get(url, timeout=15)
        if r.status_code != 200:
            print(f"    [OC API officers] HTTP {r.status_code}", flush=True)
            return []

        data     = r.json()
        officers = data.get("results", {}).get("officers", [])
        result   = []
        for o in officers:
            off = o.get("officer", {})
            result.append({
                "name":       off.get("name", "").title(),
                "position":   off.get("position", ""),
                "start_date": off.get("start_date", ""),
                "end_date":   off.get("end_date", ""),
            })
        return result
    except Exception as e:
        print(f"    [OC API officers] error: {e}", flush=True)
        return []


def research_opencorporates(company_name, findings):
    """[F3] Full OC API research — global search, jurisdiction from result."""
    print("  [OC API] Searching OpenCorporates (global)...", flush=True)
    co = opencorporates_api_search(company_name, jurisdiction=None)
    if not co:
        print("        → Not found on OpenCorporates API.", flush=True)
        return

    SRC = "OpenCorporates"
    cn  = co.get("company_number", "")
    jur = co.get("jurisdiction_code", "")   # [F3] from result, never hardcoded

    if not jur:
        print("        → No jurisdiction in result, skipping officer lookup.", flush=True)
        return

    findings["OpenCorporates Link"] = \
        f"https://opencorporates.com/companies/{jur}/{cn}"
    print(f"        → OC Link: {findings['OpenCorporates Link']}", flush=True)

    if co.get("name") and "Legal Name" not in findings:
        findings["Legal Name"] = f"{co['name']} (* {SRC})"
        print(f"        → Legal Name: {co['name']}", flush=True)

    if co.get("company_type"):
        findings["Company Type (OC)"] = f"{co['company_type']} (* {SRC})"
        print(f"        → Company Type: {co['company_type']}", flush=True)

    inc_date = co.get("incorporation_date", "") or ""
    if inc_date:
        yr = re.search(r'\d{4}', inc_date)
        if yr:
            findings["Incorporation Date (OC)"] = f"{inc_date} (* {SRC})"
            print(f"        → Incorporation Date: {inc_date}", flush=True)

    if cn:
        officers = opencorporates_api_officers(cn, jur)
        if officers:
            founders = [o for o in officers
                        if o["start_date"] and o["start_date"] == inc_date
                        and not o.get("end_date")]
            if founders:
                fnames = " | ".join(o["name"] for o in founders)
                findings["Founders (OC — appointed on incorporation date)"] = \
                    f"{fnames} (* {SRC})"
                print(f"        → Founder candidates: {fnames}", flush=True)

            current = [o for o in officers if not o.get("end_date")]
            if current:
                ofs = " | ".join(
                    f"{o['name']} ({o['position']})" for o in current)
                findings["Officers (OpenCorporates)"] = f"{ofs} (* {SRC})"
                print(f"        → {len(current)} current officers", flush=True)
        else:
            print("        → No officers returned by API.", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# LAYER 1 — BROAD SEARCH
# ─────────────────────────────────────────────────────────────────────────────

def broad_research(company_name, company_website):
    print(f"\n{'='*60}", flush=True)
    print(f"  LAYER 1 — BROAD SEARCH: {company_name}", flush=True)
    print(f"{'='*60}", flush=True)

    findings       = {}
    snippets       = []
    seen_urls      = set()
    fetched_urls   = set()
    company_domain = base_domain(company_website)
    variants       = name_variants(company_name)

    broad_queries = []
    for v in variants:
        broad_queries.append(f'"{v}"')
        broad_queries.append(f'"{v}" pharmaceutical')
        broad_queries.append(f'"{v}" company profile')

    for qi, query in enumerate(broad_queries, 1):
        print(f"\n  [broad {qi}/{len(broad_queries)}] {query}", flush=True)
        results = ddg_search(query, num_results=BROAD_DDG_RESULTS)
        print(f"    → {len(results)} results", flush=True)
        fetched_this_query = 0

        for r in results:
            url  = r["url"]
            host = urlparse(url).netloc.lower().lstrip("www.")
            if host == company_domain or host.endswith("." + company_domain):
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)

            entry = {
                "query":   query,
                "title":   r["title"],
                "url":     url,
                "snippet": r["snippet"],
                "text":    "",
                "layer":   "broad",
            }

            max_fetches = (BROAD_FETCH_PER_QUERY * 2
                           if is_high_value(url) else BROAD_FETCH_PER_QUERY)
            if fetched_this_query < max_fetches and not should_skip_fetch(url):
                print(f"      fetch: {url[:80]}", flush=True)
                raw = fetch_page_text(url, max_chars=BROAD_DDG_SNIPPET_CHARS)
                if raw:
                    entry["text"] = raw
                    fetched_urls.add(url)
                    fetched_this_query += 1
                    extract_fields_from_text(raw, _source_label(url), findings)

            snippets.append(entry)

    print(
        f"\n  Broad search done: {len(snippets)} snippets, "
        f"{len(fetched_urls)} pages fetched, "
        f"{len(findings)} fields extracted.",
        flush=True,
    )
    return findings, snippets, fetched_urls


# ─────────────────────────────────────────────────────────────────────────────
# LAYER 2 — TARGETED RESEARCH — universal edition [F4]
# ─────────────────────────────────────────────────────────────────────────────

def targeted_research(company_name, company_website, already_fetched=None,
                      site_social_links=None):
    print(f"\n{'='*60}", flush=True)
    print(f"  LAYER 2 — TARGETED RESEARCH: {company_name}", flush=True)
    print(f"{'='*60}", flush=True)

    already_fetched   = already_fetched   or set()
    site_social_links = site_social_links or {}
    findings          = {}
    snippets          = []

    for label, url in site_social_links.items():
        findings[label] = url
        print(f"  [social — from site] {label}: {url}", flush=True)

    variants = name_variants(company_name)

    def _add_snippet(r, layer="targeted"):
        snippets.append({
            "query":   "",
            "title":   r["title"],
            "url":     r["url"],
            "snippet": r["snippet"],
            "text":    "",
            "layer":   layer,
        })
        return snippets[-1]

    def _fetch_if_new(url, max_chars=TARGETED_SNIPPET_CHARS):
        if url in already_fetched or should_skip_fetch(url):
            return ""
        print(f"        fetching: {url[:80]}", flush=True)
        text = fetch_page_text(url, max_chars=max_chars)
        if text:
            already_fetched.add(url)
        return text

    def _ddg_multi(site_pattern, num=TARGETED_DDG_RESULTS):
        seen  = set()
        all_r = []
        for v in variants:
            query = f'"{v}" {site_pattern}'
            print(f"        query: {query}", flush=True)
            for r in ddg_search(query, num_results=num):
                if r["url"] not in seen:
                    seen.add(r["url"])
                    all_r.append(r)
        return all_r

    def _name_in(text):
        tl = text.lower()
        return any(v.lower().split()[0] in tl for v in variants)

    # ── 1. LinkedIn ──────────────────────────────────────────────────────────
    print("\n  [1/11] LinkedIn...", flush=True)
    if "LinkedIn Link" in findings:
        print(f"        → Already found on site: {findings['LinkedIn Link']}", flush=True)
    else:
        res      = _ddg_multi("site:linkedin.com/company")
        found_li = False
        for r in res:
            if "linkedin.com/company" not in r["url"]:
                continue
            if _name_in(r["title"] + " " + r["snippet"]):
                findings["LinkedIn Link"] = r["url"]
                print(f"        → {r['url']}", flush=True)
                yr = re.search(r'[Ff]ounded[:\s]+(\d{4})', r["snippet"])
                if yr and "Year Founded (external)" not in findings:
                    findings["Year Founded (external)"] = \
                        f"{yr.group(1)} (* LinkedIn snippet)"
                _add_snippet(r)
                found_li = True
                break
        if not found_li:
            print("        → Not found.", flush=True)

    # ── 2. YouTube ───────────────────────────────────────────────────────────
    print("\n  [2/11] YouTube...", flush=True)
    if "YouTube Link" in findings:
        print(f"        → Already found on site: {findings['YouTube Link']}", flush=True)
    else:
        yt_queries = []
        for v in variants:
            yt_queries.append(f'"{v}" site:youtube.com/@')
            yt_queries.append(f'"{v}" official channel site:youtube.com')
        found_yt = False
        seen_yt  = set()
        for q in yt_queries:
            print(f"        query: {q}", flush=True)
            res = ddg_search(q, num_results=4)
            for r in res:
                if r["url"] in seen_yt:
                    continue
                seen_yt.add(r["url"])
                if "youtube.com" not in r["url"]:
                    continue
                if not any(x in r["url"] for x in ["/@", "/channel/", "/user/", "/c/"]):
                    continue
                if _name_in(r["title"] + " " + r["snippet"]):
                    findings["YouTube Link"] = r["url"]
                    print(f"        → {r['url']}", flush=True)
                    _add_snippet(r)
                    found_yt = True
                    break
            if found_yt:
                break
        if not found_yt:
            print("        → Not found.", flush=True)

    # ── 3. Crunchbase ─────────────────────────────────────────────────────────
    print("\n  [3/11] Crunchbase...", flush=True)
    res      = _ddg_multi("site:crunchbase.com/organization")
    found_cb = False
    for r in res:
        if "crunchbase.com" not in r["url"] or "/organization/" not in r["url"]:
            continue
        cb_text = _fetch_if_new(r["url"], max_chars=4000)
        if cb_text and not _name_in(cb_text):
            print(f"        → Crunchbase mismatch, skipping.", flush=True)
            continue
        findings["Crunchbase Link"] = r["url"]
        print(f"        → {r['url']}", flush=True)
        if cb_text:
            yr = re.search(r'[Ff]ounded[:\s]+(\d{4})', cb_text)
            if yr and "Year Founded (external)" not in findings:
                findings["Year Founded (external)"] = f"{yr.group(1)} (* Crunchbase)"
        e = _add_snippet(r)
        e["text"] = cb_text or ""
        found_cb = True
        break
    if not found_cb:
        print("        → Not found.", flush=True)

    # ── 4. OpenCorporates API (global) ────────────────────────────────────────
    print("\n  [4/11] OpenCorporates API...", flush=True)
    research_opencorporates(company_name, findings)

    # ── 5. SEC EDGAR ─────────────────────────────────────────────────────────
    print("\n  [5/11] SEC EDGAR...", flush=True)
    found_sec = False
    for v in variants:
        res = ddg_search(f'"{v}" site:sec.gov', num_results=TARGETED_DDG_RESULTS)
        for r in res:
            if "sec.gov" in r["url"]:
                findings["Financial Filings Link"] = r["url"]
                print(f"        → {r['url']}", flush=True)
                _add_snippet(r)
                found_sec = True
                break
        if found_sec:
            break
    if not found_sec:
        print("        → Not found.", flush=True)

    # ── 6. Yahoo Finance ─────────────────────────────────────────────────────
    print("\n  [6/11] Yahoo Finance...", flush=True)
    found_yf = False
    for v in variants:
        res = ddg_search(
            f'"{v}" stock ticker site:finance.yahoo.com',
            num_results=TARGETED_DDG_RESULTS,
        )
        for r in res:
            if "finance.yahoo.com/quote/" in r["url"]:
                findings["Investor Link"] = r["url"]
                ticker = r["url"].split("/quote/")[-1].split("/")[0].split("?")[0]
                findings["Ticker Symbol"] = f"{ticker} (* Yahoo Finance)"
                print(f"        → {r['url']} | Ticker: {ticker}", flush=True)
                _add_snippet(r)
                found_yf = True
                break
        if found_yf:
            break
    if not found_yf:
        print("        → Not found.", flush=True)

    # ── 7. Patents (Espacenet / Google Patents) ───────────────────────────────
    print("\n  [7/11] Patents...", flush=True)
    found_ep = False
    for v in variants:
        for q in [
            f'"{v}" site:worldwide.espacenet.com',
            f'"{v}" applicant site:patents.google.com',
        ]:
            res = ddg_search(q, num_results=TARGETED_DDG_RESULTS)
            for r in res:
                if "espacenet.com" in r["url"] or "patents.google.com" in r["url"]:
                    if _name_in(r["title"] + " " + r["snippet"]):
                        findings["Patents Link"] = r["url"]
                        print(f"        → {r['url']}", flush=True)
                        _add_snippet(r)
                        found_ep = True
                        break
            if found_ep:
                break
        if found_ep:
            break
    if not found_ep:
        print("        → Not found.", flush=True)

    # ── 8. [F4] Universal business directories ────────────────────────────────
    print("\n  [8/11] Universal business directories (Kompass / Europages / D&B)...",
          flush=True)
    univ_sites = [
        "site:kompass.com",
        "site:europages.com",
        "site:dnb.com",
        "site:hoovers.com",
    ]
    univ_found = False
    for db_site in univ_sites:
        if univ_found:
            break
        for v in variants:
            query = f'"{v}" {db_site}'
            print(f"        query: {query}", flush=True)
            res   = ddg_search(query, num_results=3)
            for r in res:
                db_domain = db_site.replace("site:", "")
                if db_domain not in r["url"]:
                    continue
                if not _name_in(r["title"] + " " + r["snippet"]):
                    continue
                print(f"        → {r['url']}", flush=True)
                e   = _add_snippet(r)
                txt = _fetch_if_new(r["url"])
                if txt:
                    e["text"] = txt
                    extract_fields_from_text(txt, _source_label(r["url"]), findings)
                univ_found = True
                break
            if univ_found:
                break
    if not univ_found:
        print("        → Not found.", flush=True)

    # ── 9. [F4] Global pharma / trade databases ───────────────────────────────
    print("\n  [9/11] Global pharma / trade databases (PharmaCompass / Trademo)...",
          flush=True)
    pharma_sites = [
        "site:pharmacompass.com",
        "site:trademo.com",
        "site:chemdmart.com",
    ]
    pharma_found = False
    for db_site in pharma_sites:
        if pharma_found:
            break
        for v in variants:
            query = f'"{v}" {db_site}'
            print(f"        query: {query}", flush=True)
            res   = ddg_search(query, num_results=2)
            for r in res:
                if _name_in(r["title"] + " " + r["snippet"]):
                    print(f"        → {r['url']}", flush=True)
                    e   = _add_snippet(r)
                    txt = _fetch_if_new(r["url"])
                    if txt:
                        e["text"] = txt
                        extract_fields_from_text(txt, _source_label(r["url"]), findings)
                    pharma_found = True
                    break
            if pharma_found:
                break
    if not pharma_found:
        print("        → Not found.", flush=True)

    # ── 10. RocketReach ──────────────────────────────────────────────────────
    print("\n  [10/11] RocketReach...", flush=True)
    found_rr = False
    for v in variants:
        res = ddg_search(
            f'"{v}" site:rocketreach.co',
            num_results=TARGETED_DDG_RESULTS,
        )
        for r in res:
            if "rocketreach.co" not in r["url"]:
                continue
            if _name_in(r["title"] + " " + r["snippet"]):
                print(f"        → {r['url']}", flush=True)
                e   = _add_snippet(r)
                txt = _fetch_if_new(r["url"], max_chars=3000)
                if txt:
                    e["text"] = txt
                    found_names = extract_all_founders(txt)
                    if found_names and "Founders (external)" not in findings:
                        findings["Founders (external)"] = (
                            " | ".join(found_names) + " (* RocketReach)")
                found_rr = True
                break
        if found_rr:
            break
    if not found_rr:
        print("        → Not found.", flush=True)

    # ── 11. [F4] Bureau van Dijk / Orbis ─────────────────────────────────────
    print("\n  [11/11] Bureau van Dijk / Orbis...", flush=True)
    found_bvd = False
    for v in variants:
        for q in [
            f'"{v}" site:orbis.bvdinfo.com',
            f'"{v}" bureau van dijk company profile',
        ]:
            print(f"        query: {q}", flush=True)
            res = ddg_search(q, num_results=TARGETED_DDG_RESULTS)
            for r in res:
                if _name_in(r["title"] + " " + r["snippet"]):
                    print(f"        → {r['url']}", flush=True)
                    e   = _add_snippet(r)
                    txt = _fetch_if_new(r["url"], max_chars=3000)
                    if txt:
                        e["text"] = txt
                        extract_fields_from_text(txt, _source_label(r["url"]), findings)
                    found_bvd = True
                    break
            if found_bvd:
                break
        if found_bvd:
            break
    if not found_bvd:
        print("        → Not found.", flush=True)

    found_keys = list(findings.keys())
    print(f"\n  Targeted research done: {len(findings)} fields found.", flush=True)
    if findings:
        print(f"  Fields: {', '.join(found_keys)}", flush=True)
    else:
        print("  [WARNING] Targeted research found 0 fields.", flush=True)

    return findings, snippets


# ─────────────────────────────────────────────────────────────────────────────
# SITE CRAWL
# ─────────────────────────────────────────────────────────────────────────────

def extract_links(html, base_url, domain):
    soup  = BeautifulSoup(html, "html.parser")
    found = set()
    for tag in soup.find_all(href=True):
        val = tag["href"].strip()
        if not val or val.startswith(("#","mailto:","tel:","javascript:")):
            continue
        url = urljoin(base_url, val)
        if same_site(url, domain) and urlparse(url).scheme in ("http","https"):
            if not should_skip_url(url):
                found.add(normalize(url))
    for tag in soup.find_all(onclick=True):
        for m in re.findall(
                r"""(?:href|location(?:\.href)?)\s*[=:]\s*['"]([^'"]+)['"]""",
                tag["onclick"]):
            url = urljoin(base_url, m)
            if same_site(url, domain) and urlparse(url).scheme in ("http","https"):
                if not should_skip_url(url):
                    found.add(normalize(url))
    return found


def get_sitemap_urls(base_url, domain):
    urls   = set()
    parsed = urlparse(base_url)
    roots  = [f"{parsed.scheme}://{parsed.netloc}"]
    if not parsed.netloc.lower().startswith("www."):
        roots.append(f"{parsed.scheme}://www.{domain}")

    candidates = []
    for root in roots:
        try:
            r = requests.get(f"{root}/robots.txt", headers=HEADERS, timeout=8)
            if r.status_code == 200:
                for line in r.text.splitlines():
                    if line.lower().startswith("sitemap:"):
                        candidates.append(line.split(":",1)[1].strip())
        except Exception:
            pass
        candidates += [
            f"{root}/sitemap.xml", f"{root}/sitemap_index.xml",
            f"{root}/sitemap-index.xml", f"{root}/sitemaps.xml",
            f"{root}/wp-sitemap.xml", f"{root}/sitemap/sitemap.xml",
        ]

    visited_sm = set()

    def parse_sitemap(sm_url, depth=0):
        if depth > 4 or sm_url in visited_sm:
            return
        visited_sm.add(sm_url)
        try:
            r = requests.get(sm_url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                return
            try:
                soup = BeautifulSoup(r.content, "lxml-xml")
            except Exception:
                import xml.etree.ElementTree as ET
                root_el = ET.fromstring(r.content)
                ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
                for loc_el in root_el.findall('.//sm:loc', ns):
                    u = normalize(loc_el.text.strip())
                    if same_site(u, domain) and not should_skip_url(u):
                        urls.add(u)
                return
            for loc in soup.find_all("sitemap"):
                child = loc.find("loc")
                if child:
                    parse_sitemap(child.text.strip(), depth + 1)
            for loc in soup.find_all("url"):
                child = loc.find("loc")
                if child:
                    u = normalize(child.text.strip())
                    if same_site(u, domain) and not should_skip_url(u):
                        urls.add(u)
        except Exception:
            pass

    for sm in candidates:
        parse_sitemap(sm)
    print(f"  [sitemap] {len(urls)} URLs found", flush=True)
    return urls


def extract_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script","style","nav","footer","header",
                     "aside","form","noscript","iframe","svg",
                     "button","meta","link","figure","figcaption"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    text = (text.encode("utf-8","ignore").decode("utf-8")
            .replace("\ufeff","").replace("\u200b","").replace("\xa0"," "))
    lines, prev = [], None
    for ln in text.splitlines():
        s = ln.strip()
        if len(s) <= 3:
            continue
        if s != prev:
            lines.append(s)
        prev = s
    return strip_noise_lines("\n".join(lines))


def fetch_page(url, driver, js_domains):
    netloc = urlparse(url).netloc.lower()

    if netloc not in js_domains:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
            r.raise_for_status()
            if "text/html" not in r.headers.get("Content-Type",""):
                return None, None, None
            html      = r.content.decode(r.apparent_encoding or "utf-8", errors="ignore")
            soup      = BeautifulSoup(html, "html.parser")
            body_text = (soup.find("body") or soup).get_text(strip=True)

            if len(body_text) <= 300:
                js_domains.add(netloc)
            elif is_js_shell(html, body_text):
                js_domains.add(netloc)
            else:
                title   = soup.title.string.strip() if soup.title else url
                soft404 = ["website not found","page not found","404",
                           "not registered","does not exist","coming soon"]
                if any(s in title.lower() or s in body_text[:300].lower()
                       for s in soft404):
                    return None, None, None
                return html, title, html

        except (requests.exceptions.ConnectionError,
                requests.exceptions.SSLError):
            js_domains.add(netloc)
        except Exception:
            return None, None, None

    if driver:
        try:
            html, title = selenium_fetch(driver, url)
            js_domains.add(netloc)
            soup      = BeautifulSoup(html, "html.parser")
            body_text = (soup.find("body") or soup).get_text(strip=True)
            if len(body_text) < 100:
                return None, None, None
            return html, title, html
        except Exception:
            pass

    return None, None, None


def content_hash(text):
    return hashlib.md5(text.encode("utf-8","ignore")).hexdigest()


def scrape(start_url):
    domain          = base_domain(start_url)
    visited         = set()
    pages           = []
    pages_html_raw  = []
    seen_hashes     = set()
    seen_urls       = set()
    js_domains      = set()

    print(f"\n{'='*60}", flush=True)
    print(f"  Scraping: {start_url}", flush=True)
    print(f"  Domain:   {domain}", flush=True)
    print(f"{'='*60}\n", flush=True)

    sitemap_urls = get_sitemap_urls(start_url, domain)
    queue        = deque([normalize(start_url)])
    for u in sitemap_urls:
        if u not in queue:
            queue.append(u)

    print(f"  Crawling | {len(queue)} URLs in queue\n", flush=True)
    driver = make_driver() if SELENIUM_AVAILABLE else None

    try:
        while queue and len(visited) < MAX_PAGES:
            url = normalize(queue.popleft())
            if url in visited or should_skip_url(url):
                visited.add(url)
                continue
            visited.add(url)
            print(f"  [{len(visited):3d}/{MAX_PAGES}]  {url[:90]}", flush=True)

            html, title, raw_html = fetch_page(url, driver, js_domains)
            if not html:
                print(f"    → skipped", flush=True)
                continue

            if raw_html:
                pages_html_raw.append((url, raw_html))

            text = extract_text(html)
            if not text or len(text) < 50:
                continue

            h = content_hash(text)
            if h in seen_hashes:
                print(f"    → duplicate content", flush=True)
                continue
            seen_hashes.add(h)

            norm_url = normalize(url)
            if norm_url in seen_urls:
                continue
            seen_urls.add(norm_url)

            pages.append({"url": url, "title": title, "text": text})
            print(f"    → saved [{len(pages):2d}]  \"{title[:60]}\"", flush=True)

            new_links = extract_links(html, url, domain)
            added     = 0
            for link in new_links:
                if link not in visited and link not in queue:
                    queue.append(link)
                    added += 1
            if added:
                print(f"    → +{added} links queued (queue: {len(queue)})", flush=True)

    finally:
        if driver:
            driver.quit()

    print(f"\n  Done: {len(pages)} pages from {len(visited)} visited\n", flush=True)

    print("  Scanning site pages for social media links...", flush=True)
    site_social_links = collect_site_social_links(pages_html_raw)
    if not site_social_links:
        print("  [social] No social links found in site HTML.", flush=True)

    return pages, site_social_links


# ─────────────────────────────────────────────────────────────────────────────
# SMART TRUNCATION
# ─────────────────────────────────────────────────────────────────────────────

def truncate_smart(page, base_limit=MAX_CHARS_PER_PAGE):
    slug  = urlparse(page["url"]).path.strip("/").split("/")[-1].lower()
    limit = base_limit if slug in PRIORITY_SLUGS else base_limit // 2
    text  = page["text"]
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[...truncated]"


# ─────────────────────────────────────────────────────────────────────────────
# BUDGET ENFORCEMENT
# ─────────────────────────────────────────────────────────────────────────────

def _estimate_size(pages, snippets, research_findings, broad_findings):
    def page_budget(p):
        slug = urlparse(p["url"]).path.strip("/").split("/")[-1].lower()
        lim  = MAX_CHARS_PER_PAGE if slug in PRIORITY_SLUGS else MAX_CHARS_PER_PAGE // 2
        return min(len(p["text"]), lim)

    page_chars     = sum(page_budget(p) for p in pages)
    snippet_chars  = sum(
        len(s["title"]) + len(s["url"]) + len(s["snippet"]) + len(s.get("text","")) + 80
        for s in snippets
    )
    findings_chars = sum(
        len(k) + len(str(v)) + 10
        for d in [research_findings, broad_findings]
        for k, v in d.items()
    )
    return page_chars + snippet_chars + findings_chars + _INSTRUCTIONS_OVERHEAD + _HEADER_OVERHEAD


def enforce_budget(pages, snippets, research_findings, broad_findings,
                   char_limit=CHATGPT_CHAR_LIMIT):
    trim_log = []

    def over():
        return _estimate_size(pages, snippets, research_findings, broad_findings) > char_limit

    if over():
        for s in snippets:
            if s.get("text") and len(s["text"]) > 800:
                old = len(s["text"])
                s["text"] = compress_snippet_text(s["text"], max_chars=800)
                if len(s["text"]) < old:
                    trim_log.append(
                        f"Compressed snippet text {old}→{len(s['text'])}: "
                        f"{s['url'][:50]}"
                    )
        if not over():
            _log_budget_trims(trim_log)
            return pages, snippets

    if over():
        for s in sorted(snippets, key=snippet_priority):
            if s.get("text") and not is_high_value(s["url"]):
                trim_log.append(f"Dropped page-text: {s['url'][:55]}")
                s["text"] = ""
                if not over():
                    break

    if over():
        for s in snippets:
            if s.get("text"):
                trim_log.append(f"Dropped HV page-text: {s['url'][:55]}")
                s["text"] = ""
                if not over():
                    break

    if over():
        removable = [s for s in snippets
                     if not is_high_value(s["url"]) and s["layer"] == "broad"]
        for s in removable:
            if len(snippets) <= 5:
                break
            snippets.remove(s)
            trim_log.append(f"Removed snippet: {s['url'][:55]}")
            if not over():
                break

    if over():
        for p in pages:
            slug = urlparse(p["url"]).path.strip("/").split("/")[-1].lower()
            if slug not in PRIORITY_SLUGS and len(p["text"]) > 2000:
                old_len   = len(p["text"])
                p["text"] = p["text"][:2000] + "\n[...trimmed for size]"
                trim_log.append(f"Trimmed page {old_len}→2000: {p['url'][:50]}")
                if not over():
                    break

    _log_budget_trims(trim_log)
    return pages, snippets


def _log_budget_trims(trim_log):
    if trim_log:
        print(f"\n  [budget] {len(trim_log)} trims applied:", flush=True)
        for msg in trim_log[:10]:
            print(f"    - {msg}", flush=True)
        if len(trim_log) > 10:
            print(f"    ... and {len(trim_log)-10} more", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# POST-PROCESSOR
# ─────────────────────────────────────────────────────────────────────────────

def strip_markdown_links(text):
    text = re.sub(r'\[([^\]]*)\]\((https?://[^\)]+)\)', r'\2', text)
    text = re.sub(r'\[(https?://[^\]]+)\]\((https?://[^\)]+)\)', r'\2', text)
    text = re.sub(r'<(https?://[^>]+)>', r'\1', text)
    return text


# ─────────────────────────────────────────────────────────────────────────────
# PROMPT BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_prompt(pages, start_url, research_findings,
                 broad_findings=None, snippets=None):
    lines          = []
    broad_findings = broad_findings or {}
    snippets       = snippets       or []

    broad_snips    = [s for s in snippets if s.get("layer") == "broad"]
    targeted_snips = [s for s in snippets if s.get("layer") != "broad"]

    lines.append(f"WEBSITE: {start_url}")
    lines.append(f"PAGES SCRAPED: {len(pages)}")
    lines.append("")
    lines.append("SCRAPED LINKS — all pages found:")
    for i, p in enumerate(pages, 1):
        lines.append(f"  [{i:02d}] {p['url']}  |  {p['title']}")
    lines.append("")
    lines.append("=" * 60)

    for i, p in enumerate(pages, 1):
        lines.append(f"\n[PAGE {i:02d}]  [TIER 1 — OFFICIAL WEBSITE]")
        lines.append(f"URL: {p['url']}")
        lines.append(f"TITLE: {p['title']}")
        lines.append("---")
        lines.append(truncate_smart(p))

    lines.append("\n" + "=" * 60)

    if broad_snips:
        lines.append("")
        lines.append("BROAD WEB SEARCH RESULTS  [lowest priority — Tier 3/4/5]")
        lines.append("(Wide-net DDG search — use only for fields not found in website or targeted DBs)")
        lines.append("All values carry (* SourceName) and require verification against Tiers 1-2.")
        lines.append("")
        for i, s in enumerate(broad_snips, 1):
            lines.append(f"  [BROAD {i:02d}] {s['title']}")
            lines.append(f"  URL: {s['url']}")
            if s["snippet"]:
                lines.append(f"  SNIPPET: {s['snippet'][:300]}")
            if s.get("text"):
                lines.append(f"  PAGE TEXT:")
                for ln in s["text"].splitlines()[:60]:
                    lines.append(f"    {ln}")
            lines.append("")
        lines.append("=" * 60)

    if targeted_snips:
        lines.append("")
        lines.append("TARGETED DATABASE SEARCH RESULTS  [Tier 3/4 — use when website is silent]")
        lines.append("(LinkedIn, YouTube, Crunchbase, OpenCorporates API, SEC, Yahoo Finance,")
        lines.append(" Espacenet, Kompass, Europages, D&B, PharmaCompass, RocketReach,")
        lines.append(" Bureau van Dijk / Orbis, Trademo, Chemdmart)")
        lines.append("All external values carry (* SourceName). Apply UNIVERSAL SOURCE PRIORITY.")
        lines.append("")
        for i, s in enumerate(targeted_snips, 1):
            lines.append(f"  [TARGET {i:02d}] {s['title']}")
            lines.append(f"  URL: {s['url']}")
            if s["snippet"]:
                lines.append(f"  SNIPPET: {s['snippet'][:300]}")
            if s.get("text"):
                lines.append(f"  PAGE TEXT:")
                for ln in s["text"].splitlines()[:60]:
                    lines.append(f"    {ln}")
            lines.append("")
        lines.append("=" * 60)

    all_findings = {**broad_findings, **research_findings}
    if all_findings:
        lines.append("")
        lines.append("PRE-RESEARCHED STRUCTURED FINDINGS")
        lines.append("(Auto-extracted fields — apply UNIVERSAL SOURCE PRIORITY rules)")
        lines.append("")
        lines.append("  TRUST TIERS FOR VALUES IN THIS SECTION:")
        lines.append("  [SITE]  → Tier 1/2: from company's own HTML — treat as authoritative")
        lines.append("  (* OC)  → Tier 3/4: OpenCorporates API — reliable for legal fields")
        lines.append("  (* Src) → Tier 3/4/5: other external source — verify against website")
        lines.append("  Fields labelled 'Founders (OC — appointed on incorporation date)'")
        lines.append("  are founder CANDIDATES — use additive rule (see ADDITIVE FIELDS).")
        lines.append("")
        for k, v in all_findings.items():
            lines.append(f"  {k}: {v}")
        lines.append("")
        lines.append("=" * 60)

    lines.append("\n" + "=" * 60)

    lines.append(r"""
INSTRUCTIONS
============

You are a pharmaceutical/biotech company database researcher.
You have been given FOUR data sources — use ALL of them:

  1. SCRAPED WEBSITE PAGES     — the company's own official website content
  2. PRE-RESEARCHED STRUCTURED FINDINGS — auto-extracted and labelled facts
  3. TARGETED DATABASE RESULTS — LinkedIn, YouTube, Crunchbase, OC API, SEC, etc.
  4. BROAD WEB SEARCH RESULTS  — wide-net DDG search, catches any source

Do NOT invent any data. Only use what is explicitly present in the sources above.

══════════════════════════════════════════════════════
CRITICAL URL FORMAT RULE — APPLIES TO EVERY CELL
══════════════════════════════════════════════════════

  ALWAYS write URLs as bare plain text. NEVER use markdown link syntax.

  CORRECT:   https://example.com/about
  WRONG:     [About](https://example.com/about)
  WRONG:     [https://example.com](https://example.com)

══════════════════════════════════════════════════════
UNIVERSAL SOURCE PRIORITY — APPLIES TO EVERY FIELD
══════════════════════════════════════════════════════

Follow this decision tree for EVERY field, without exception:

  TIER 1 — Official website (SCRAPED WEBSITE PAGES):
    If the website explicitly states a value for a field → use it as the final
    answer. Do not override it with any external source, even if they disagree.
    Mark nothing — website data needs no verification tag.

  TIER 2 — Structured findings labelled [SITE]:
    Values in PRE-RESEARCHED STRUCTURED FINDINGS labelled "[SITE]" were
    extracted directly from the company's own HTML (e.g. social media links
    found in footer/contact page). Treat exactly like Tier 1 — authoritative,
    no tag needed.

  TIER 3 — Single external source, website is silent on this field:
    Use the external value but append:  (unverified * SourceName)

  TIER 4 — Multiple external sources agree, website is silent:
    Use the value and append:  (verified * Source1, Source2)

  TIER 5 — External sources disagree with each other, website is silent:
    Report all of them:  CONFLICT: Source1 says [X] / Source2 says [Y]

  TIER 6 — External source contradicts the website:
    Keep the website value. Add a note in the same cell:
    NOTE: [SourceName] says [conflicting value]
    Do NOT replace the website value. Do NOT silently drop the conflict.

ADDITIVE FIELDS — the one exception to "website wins, skip external":
  Some fields are lists where external sources can ADD new entries that the
  website omits — they do not contradict the website, they extend it.
  Additive fields are:  Founders | Management Team | Certifications | Business Segments
  For these fields:
    → Start with everything found on the website.
    → Then add any additional entries from external sources that are NOT already
      present, tagging each addition with (unverified * SourceName).
    → Never remove a website-sourced entry because an external source omits it.
    → Never duplicate an entry that already appears under a different phrasing.

EXAMPLE — Founders (additive):
  Website lists: Jane Smith (founder)
  OC API adds:   John Doe (director on incorporation date)
  Result:        Jane Smith<br>John Doe (unverified * OpenCorporates)

EXAMPLE — Year Founded (non-additive, website wins):
  Website says: 2018   |   OC records say: 2022
  Result:       2018   NOTE: OpenCorporates incorporation date is 2022

══════════════════════════════════════════════════════
STEP 1 — EXTRACT FROM SCRAPED WEBSITE CONTENT
══════════════════════════════════════════════════════

─── LINK ASSIGNMENT ───
  Drug programs, pipeline, preclinical/clinical work  → Pipeline Link
  Published research papers / journal articles         → Publications Link
  Company history, founding story, milestones          → History Link
  Team member names and bios                           → Management Team Link
  Company mission, what it does, its approach          → Profile Link
  Approved therapies or products on market             → Products Link
  Delivery platform or core scientific technology      → Technology Link
  Investor relations, funding, financial data          → Investor Link
  Open job positions                                   → Jobs Link
  Contact info, address, email form                    → Contacts Link
  Paid services for external clients                   → Services Link
  News articles or press releases                      → News Link
  FAQs / Terms / Privacy / Legal                       → OMIT

  PIPELINE WARNING: "/services" slug ≠ Services Link. "/platform" slug ≠
  Technology Link. Services Link ONLY if page explicitly describes paid
  services offered to external clients.

─── PEOPLE — READ EVERY LINE, LIST EVERYBODY ───
  Go through EVERY team/about/people page LINE BY LINE.
  List EVERY person found: founders, executives, board members, advisors.
  Do NOT stop after a few names. Do NOT summarize or group.
  If a page ends with [...truncated] or [...trimmed] → add:
    NOTE: page was truncated — more people may exist

  Founders field: full name only, no titles, separated by <br>.
    Include anyone explicitly called "founder" or "co-founder" on the website.
    Then apply additive rule: append external founder candidates not already
    listed, tagged (unverified * SourceName).
    Priority for external founders:
      1. "Founders (OC — appointed on incorporation date)" from structured findings
      2. "Founders (external)" from structured findings
      3. "Officers (OpenCorporates)" — only if appointment date matches incorporation date

  Management Team field: Full Name – Role, one per line using <br>.
    Strip honorifics (Mr., Dr., Prof.) from name only. Keep role as written.
    Apply additive rule: add external directors/officers not already on website,
    tagged (unverified * SourceName).

─── COMPANY NAME ───
  Use the legal entity name with its corporate suffix (Pvt. Ltd., Inc., LLC, SAS…).
  If the suffix is not confirmed on the website: append (suffix unconfirmed).
  If "Legal Name" appears in structured findings and includes a legal suffix
  that the website does not show → use it and mark (unverified * SourceName).

─── COMPANY SUMMARY ─── (minimum 4 full sentences)
  • No specific drug/compound/target/gene/technology names
  • No year founded, founder names, or location
  • Third person only — never "we", "our", "us"
  • No promotional adjectives: leading, pioneering, innovative, groundbreaking
  • Must cover: business sector, company type, therapeutic areas, disease focus

─── CERTIFICATIONS ───
  Scan ALL scraped pages AND all web research sections.
  Look for: ISO, WHO-GMP, GMP, CDSCO, FDA, CE Mark, EMA, TGA, ANVISA, NAFDAC,
  and any other country-specific regulatory or quality certification.
  List each on its own line using <br>.
  Apply additive rule: add certifications found only in external sources,
  tagged (unverified * SourceName).

─── BUSINESS MODEL — select all that apply ───
  drug delivery | pharma/bio | pharmaceutical services | generic |
  specialty pharma | agricultural | biosimilar | biobetter | chemicals |
  Consumer/OTC products | diagnostics | medical devices | lab equipment |
  Research/Reagent Supplies | pharma equipment | pharma excipients |
  research institution | incubator | venture capital | veterinary

─── MAJOR BUSINESS MODEL — select all that apply ───
  Same options as above, plus: pharma/bio diversified

  CLASSIFICATION RULES (apply in order — stop at first match):
  [1]  generic: company manufactures or exports finished formulations of
       existing generic drugs → YES for generic.
       GUARD: if the website contains NO mention of NME development, novel
       biologic research, clinical trials of proprietary compounds, or drug
       discovery programs → do NOT add pharma/bio.
  [2]  pharma/bio: website explicitly mentions developing or licensing a New
       Molecular Entity (NME) or novel biologic → YES
  [3]  specialty pharma: new formulations, drug delivery systems, or new
       indications for already-marketed drugs → YES
  [4]  biosimilar / biobetter: explicitly developing biosimilars or improved
       biologics → YES
  [5]  drug delivery: owns AND uses/licenses a proprietary delivery platform → YES
  [6]  diagnostics: regulated IVD products, clinical assays, disease detection → YES
  [7]  pharmaceutical services: CRO, CMO, regulatory consulting, analytical
       testing offered to external clients as a primary business → YES
  [8]  chemicals: pharma APIs or intermediates as the primary product line → YES
  [9]  veterinary: products explicitly for animal health → YES
  [10] AI/digital: AI for drug discovery or AI-based imaging/diagnostics → YES

─── US STATE (only if company is in USA) ───
  Full format: e.g. CA-California, NY-New York, TX-Texas

══════════════════════════════════════════════════════
STEP 2 — APPLY STRUCTURED FINDINGS & WEB RESEARCH
══════════════════════════════════════════════════════

  Apply the UNIVERSAL SOURCE PRIORITY rules to every field.
  Additional formatting rules:

  CIN (Corporate Identification Number, India):
    Include if found in any source. Format: the raw CIN value + source tag
    if from external only.

  Address field:
    Take the full address string and place EVERYTHING except the country name
    into Address. Place only the country name into Country.

    Example:
      Raw: "6, rue Solférino 78000 Versailles, France"
      Address: 6, rue Solférino 78000 Versailles
      Country: France

    Do NOT split into separate City / Postal Code / State fields.
    The ONLY thing removed is the country name itself.

  Links (Profile, Products, Jobs, LinkedIn, YouTube, etc.):
    Include a link only if you have confirmed it exists and is relevant.
    [SITE]-labelled social links → use as-is, no tag needed.
    DDG-found social links → only use if no [SITE] version exists AND the
    company name (or a clear keyword from it) appears in the URL slug or
    page title/snippet. Tag as (unverified * DDG).

══════════════════════════════════════════════════════
OUTPUT FORMAT
══════════════════════════════════════════════════════

  URLs: plain text only — NO markdown. See CRITICAL URL FORMAT RULE above.

  Single markdown table, two columns: | Field | Value |
    • First row = Company Name, second row = Company Summary
    • Omit any row where no value was found
    • Multi-value fields use <br> as separator (not newlines, not bullets):
        Founders | Management Team | Business Model | Major Business Model |
        Business Segments | Certifications | Scraped Links
    • US State: full format e.g. CA-California

  Output NOTHING outside the table — no preamble, no footnotes, no comments.

FIELD ORDER:
Company Name | Company Summary | Year Founded | Founders |
Business Model | Major Business Model | Business Segments | Certifications |
Private or Public | Ticker Symbol | CIN |
Company Website | E-Mail | Phone | Fax | Address | US State | Country |
Management Team |
Profile Link | History Link | Technology Link | Pipeline Link |
Products Link | Services Link | API Products Link | News Link | Investor Link |
Presentation Link | Financial Filings Link | Jobs Link | Contacts Link |
Management Team Link | Locations Link | Portfolio Companies Link |
Publications Link | Partners Link |
LinkedIn Link | YouTube Link | Patents Link | Crunchbase Link |
Scraped Links
""")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# [F2] COMPANY NAME CONFIRMATION — console prompt BEFORE searches run
# ─────────────────────────────────────────────────────────────────────────────

def confirm_company_name(detected_name):
    """
    [F2] Shows the auto-detected name in the console and lets the user
    correct it BEFORE any external search queries are built and fired.
    Press Enter alone to accept the detected name as-is.
    Handles non-interactive (piped) mode gracefully.
    """
    print(f"\n{'='*60}", flush=True)
    print(f"  COMPANY NAME DETECTED: \"{detected_name}\"", flush=True)
    print(f"  Press Enter to confirm, or type the correct name:", flush=True)
    print(f"{'='*60}", flush=True)
    try:
        user_input = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        user_input = ""
    if user_input:
        print(f"  [name] Corrected to: \"{user_input}\"", flush=True)
        return user_input
    print(f"  [name] Confirmed: \"{detected_name}\"", flush=True)
    return detected_name


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if not os.path.exists(INPUT_FILE):
        print(f"Input file not found: {INPUT_FILE}")
        return
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        start_url = f.read().strip()
    if not start_url:
        print("Input file is empty.")
        return

    print("=" * 60, flush=True)
    print(" Scraper Tool - Starting...", flush=True)
    print("=" * 60, flush=True)

    # 1. Crawl company website
    pages, site_social_links = scrape(start_url)
    if not pages:
        print("No pages scraped.")
        return

    # 2. [F1] Company name detection with prefix stripping
    homepage  = next(
        (p for p in pages if normalize(p["url"]) == normalize(start_url)),
        pages[0],
    )
    raw_title = homepage["title"]

    # Strip pipe/dash suffixes: "Topiqual | Home" → "Topiqual"
    stripped = re.sub(
        r'\s*[|\-–—·•]\s*(.+)$',
        lambda m: "" if len(m.group(1).split()) >= 3 else m.group(0),
        raw_title,
    ).strip()
    company_name = stripped if stripped else raw_title.strip()

    # Strip leading nav prefixes: "About topiqual" → "topiqual"
    company_name = _TITLE_PREFIX_RE.sub("", company_name).strip()

    # Fallback to domain name if result is empty or a bare nav word
    if not company_name or company_name.lower() in _NAV_WORDS:
        company_name = base_domain(start_url).split(".")[0].title()

    # 3. [F2] Confirm with user BEFORE searches run
    company_name = confirm_company_name(company_name)

    print(f"\n  Company name: \"{company_name}\"", flush=True)
    print(f"  Name variants: {name_variants(company_name)}", flush=True)

    # 4. Global dedup on site pages
    before = sum(len(p["text"]) for p in pages)
    pages  = apply_global_dedup(pages)
    after  = sum(len(p["text"]) for p in pages)
    if before > after:
        print(
            f"  [compression] Saved {before-after:,} chars via deduplication.",
            flush=True,
        )

    # 5. Layer 1: broad search
    broad_findings, broad_snippets, fetched_urls = broad_research(
        company_name, start_url)

    # 6. Layer 2: targeted research
    targeted_findings, targeted_snippets = targeted_research(
        company_name, start_url,
        already_fetched=fetched_urls,
        site_social_links=site_social_links,
    )

    # 7. Merge
    all_snippets = broad_snippets + targeted_snippets
    all_findings = {**broad_findings, **targeted_findings}

    # 8. Budget enforcement
    pages, all_snippets = enforce_budget(
        pages, all_snippets, targeted_findings, broad_findings,
        char_limit=CHATGPT_CHAR_LIMIT,
    )

    # 9. Build prompt
    prompt = build_prompt(
        pages, start_url, targeted_findings, broad_findings, all_snippets)
    prompt = strip_markdown_links(prompt)

    # 10. Save
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(prompt)

    chars  = len(prompt)
    tokens = chars // 4
    fits   = chars <= CHATGPT_CHAR_LIMIT

    print(f"\n{'='*60}", flush=True)
    print(f"  RESEARCH SUMMARY", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"  Site pages        : {len(pages)}", flush=True)
    print(f"  Site social links : {site_social_links}", flush=True)
    broad_count    = len([s for s in all_snippets if s.get("layer") == "broad"])
    targeted_count = len([s for s in all_snippets if s.get("layer") != "broad"])
    print(f"  Broad snippets    : {broad_count}", flush=True)
    print(f"  Targeted snippets : {targeted_count}", flush=True)
    print(f"  Fields found      : {len(all_findings)}", flush=True)
    if all_findings:
        for k, v in all_findings.items():
            print(f"    {k}: {str(v)[:80]}", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"  Saved  : {OUTPUT_FILE}", flush=True)
    print(f"  Size   : {chars:,} chars (~{tokens:,} tokens)", flush=True)
    print(
        f"  Limit  : {CHATGPT_CHAR_LIMIT:,} chars  →  "
        f"{'FITS ✓' if fits else f'OVER by {chars-CHATGPT_CHAR_LIMIT:,} chars ✗'}",
        flush=True,
    )
    print("", flush=True)
    copy_to_clipboard(prompt)
    print(f"{'='*60}\n", flush=True)
    print("  Done. Paste into ChatGPT — it's in your clipboard.\n", flush=True)


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 3 and sys.argv[1] == "--clean":
        target = sys.argv[2]
        if not os.path.exists(target):
            print(f"File not found: {target}")
            sys.exit(1)
        with open(target, "r", encoding="utf-8") as f:
            raw = f.read()
        cleaned  = strip_markdown_links(raw)
        out_path = target.replace(".txt", "_clean.txt")
        if out_path == target:
            out_path = target + "_clean.txt"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(cleaned)
        print(f"Cleaned → {out_path}")
    else:
        main()
