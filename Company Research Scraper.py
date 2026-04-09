"""
Company Research Scraper (Brave Search edition)
================================================
WHAT IT DOES:
  1. Crawls the company website (Selenium + requests, sitemap, JS detection)
  2. Layer 1 — broad Brave search: finds general info about the company
  3. Layer 2 — targeted Brave search: LinkedIn, Crunchbase, Kompass, OC API,
               SEC, Yahoo Finance, Patents, PharmaCompass, RocketReach, BvD
  4. Auto-extracts fields (founded, address, founders, CIN, directors) from pages
  5. Builds one giant prompt with ALL scraped data + full field instructions
  6. Saves to .txt file (upload to ChatGPT or Claude) + copies to clipboard

SETUP (one time only):
  1. Install Python: https://python.org  (tick "Add to PATH" during install)
  2. Run in terminal:
         pip install requests beautifulsoup4 selenium webdriver-manager lxml
  3. Get free Brave Search API key:
         Go to https://api.search.brave.com
         Sign up → subscribe to "Data for Search" FREE plan (2000 queries/month)
         Copy your API key and paste it below
  4. Create urls.txt in the same folder — one company URL per line:
         https://www.topiqual.com
         https://www.anothercompany.com
  5. Run:
         python company_scraper.py
  6. Each company gets a .txt file saved in the same folder
     Upload it to ChatGPT or Claude → get your filled table instantly
"""

import os
import re
import time
import hashlib
import random
import subprocess
from urllib.parse import urljoin, urlparse, urlunparse
from collections import deque, Counter

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
# CONFIG — edit these two lines
# ─────────────────────────────────────────────────────────────────────────────

BRAVE_API_KEY = "paste_your_brave_api_key_here"   # get free at api.search.brave.com
INPUT_FILE    = "urls.txt"                         # one company URL per line

# ─────────────────────────────────────────────────────────────────────────────
# SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

MAX_PAGES          = 200
MAX_CHARS_PER_PAGE = 8000
DELAY_SELENIUM     = 2.5
SCROLL_PAUSE       = 0.8
CHATGPT_CHAR_LIMIT = 380_000

BROAD_RESULTS          = 10
BROAD_SNIPPET_CHARS    = 2500
TARGETED_RESULTS       = 5
TARGETED_SNIPPET_CHARS = 2500
BROAD_FETCH_PER_QUERY  = 8

OC_API_BASE = "https://api.opencorporates.com/v0.4"

SKIP_FETCH_DOMAINS = {
    "linkedin.com", "bloomberg.com", "wsj.com", "ft.com",
    "reuters.com", "businesswire.com", "prnewswire.com",
    "twitter.com", "x.com", "facebook.com", "youtube.com",
}

HIGH_VALUE_DOMAINS = {
    "crunchbase.com", "opencorporates.com",
    "kompass.com", "europages.com",
    "dnb.com", "hoovers.com", "orbis.bvdinfo.com",
    "pharmacompass.com", "rocketreach.co",
    "sec.gov", "finance.yahoo.com",
    "espacenet.com", "patents.google.com",
    "trademo.com", "chemdmart.com",
    "zaubacorp.com", "tofler.in", "quickcompany.in", "thecompanycheck.com",
    "gmpfinder.com", "tradeindia.com",
}

SOCIAL_PATTERNS = {
    "LinkedIn Link":  re.compile(r'linkedin\.com/company/([\w\-\.%]+)', re.I),
    "YouTube Link":   re.compile(r'youtube\.com/(?:@[\w\-\.]+|channel/[\w\-]+|c/[\w\-]+|user/[\w\-]+)', re.I),
    "Facebook Link":  re.compile(r'facebook\.com/([\w\.\-]+(?:/[\w\.\-]+)?)', re.I),
    "Twitter Link":   re.compile(r'(?:twitter|x)\.com/([\w\-]+)', re.I),
    "Instagram Link": re.compile(r'instagram\.com/([\w\.\-]+)', re.I),
}

SOCIAL_SKIP_SLUGS = {
    "sharer","share","intent","login","signup","join","home","feed",
    "notifications","messaging","search","in","pub","jobs","company",
    "pages","groups","watch","results","playlist","shorts","hashtag",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

SKIP_EXTENSIONS = {
    ".css",".js",".json",".xml",".png",".jpg",".jpeg",".gif",".webp",
    ".svg",".ico",".bmp",".woff",".woff2",".ttf",".eot",".otf",
    ".pdf",".zip",".gz",".tar",".rar",".mp4",".mp3",".avi",".mov",
    ".wmv",".webm",".csv",".xls",".xlsx",".doc",".docx",".map",
}

SKIP_PATH_SEGMENTS = {
    "_next/static","_next/image","wp-content/uploads","wp-content/themes",
    "wp-content/plugins","wp-includes","static/css","static/js","static/media",
    "assets/css","assets/js","assets/fonts","assets/images","cdn-cgi","node_modules",
}

PRIORITY_SLUGS = {
    "team","about","about-us","people","leadership","management",
    "pipeline","research","publications","services","contact","contact-us",
    "founders","investors","news","platform","technology","history",
}

JS_SHELL_SIGNALS = [
    "wix.com/","_wix_","wixsite","wixstatic","squarespace.com","squarespace-cdn",
    "webflow.com","webflow.io","__NEXT_DATA__","gatsby-chunk","react-root",
    'id="root"',"id='root'",'id="app"',"id='app'",
]

_NOISE_RE = re.compile(
    r'^(home|about us?|contact us?|menu|navigation|search|login|sign in|sign up|'
    r'log in|register|accept cookies?|privacy policy|terms of (use|service)|'
    r'sitemap|skip to (main )?content|follow us|share this|back to top|'
    r'all rights reserved|copyright \d{4}|subscribe|newsletter|loading\.\.\.|'
    r'read more|learn more|click here|tweet|facebook|linkedin|instagram|'
    r'youtube|twitter|pinterest|accept|reject all|close|ok|cancel|'
    r'submit|send message|next|previous|scroll down|toggle menu|expand|collapse)$',
    re.IGNORECASE
)

_TITLE_PREFIX_RE = re.compile(
    r'^(?:about\s+us[:\s]*|about|home|welcome\s+to|introducing|meet|this\s+is)\s+',
    re.IGNORECASE
)
_NAV_WORDS = {"home", "index", "about", "main", "welcome"}

_SUFFIX_RE = re.compile(
    r'\s*[,\.]?\s*(?:private limited|pvt\.?\s*ltd\.?|pvt ltd|public limited|'
    r'limited liability company|llc|incorporated|inc\.?|limited|ltd\.?|'
    r'gmbh|ag|sa|sas|srl|bv|nv|oy|ab|as|co\.\s*ltd\.?|llp|lp|plc|pty\.?\s*ltd\.?)\s*$',
    re.IGNORECASE
)

_INSTRUCTIONS_OVERHEAD = 8000
_HEADER_OVERHEAD       = 2000


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def strip_suffix(name):
    return _SUFFIX_RE.sub("", name).strip()

def name_variants(company_name):
    short = strip_suffix(company_name)
    if short and short.lower() != company_name.lower():
        return [company_name, short]
    return [company_name]

def normalize(url):
    p = urlparse(url)
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", "", ""))

def base_domain(url):
    return urlparse(url).netloc.lower().lstrip("www.")

def same_site(url, domain):
    h = urlparse(url).netloc.lower()
    return h == domain or h == "www." + domain or h.lstrip("www.") == domain

def should_skip_url(url):
    parsed = urlparse(url)
    path = parsed.path.lower()
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

def _source_label(url):
    host = urlparse(url).netloc.lower().lstrip("www.")
    for known in ["crunchbase","opencorporates","sec.gov","yahoo","bloomberg",
                  "reuters","kompass","europages","dnb","hoovers","orbis",
                  "pharmacompass","rocketreach","trademo","chemdmart"]:
        if known in host:
            return known.replace(".","_").title()
    parts = host.split(".")
    return parts[-2].title() if len(parts) >= 2 else host

def content_hash(text):
    return hashlib.md5(text.encode("utf-8", "ignore")).hexdigest()

def strip_markdown_links(text):
    text = re.sub(r'\[([^\]]*)\]\((https?://[^\)]+)\)', r'\2', text)
    text = re.sub(r'\[(https?://[^\]]+)\]\((https?://[^\)]+)\)', r'\2', text)
    text = re.sub(r'<(https?://[^>]+)>', r'\1', text)
    return text


# ─────────────────────────────────────────────────────────────────────────────
# CLIPBOARD
# ─────────────────────────────────────────────────────────────────────────────

def copy_to_clipboard(text):
    try:
        proc = subprocess.run(
            ["powershell", "-Command", "Set-Clipboard -Value $input"],
            input=text, text=True, encoding="utf-8", capture_output=True, timeout=20,
        )
        if proc.returncode == 0:
            print("  Copied to clipboard.", flush=True)
            return
    except Exception:
        pass
    try:
        tmp = "_clip_tmp.txt"
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
# BRAVE SEARCH — replaces DDG entirely
# ─────────────────────────────────────────────────────────────────────────────

def brave_search(query, num_results=10):
    """Brave Search API — free plan: 2000 queries/month, no rate limit issues."""
    try:
        time.sleep(random.uniform(0.3, 0.8))
        r = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": BRAVE_API_KEY,
            },
            params={"q": query, "count": min(num_results, 20), "search_lang": "en", "safesearch": "off"},
            timeout=15,
        )
        if r.status_code == 429:
            print("    [brave] Rate limited — waiting 60s...", flush=True)
            time.sleep(60)
            return brave_search(query, num_results)
        if r.status_code != 200:
            print(f"    [brave] HTTP {r.status_code}: {query[:60]}", flush=True)
            return []
        data = r.json()
        results = []
        for item in data.get("web", {}).get("results", []):
            results.append({
                "title":   item.get("title", ""),
                "url":     item.get("url", ""),
                "snippet": item.get("description", ""),
            })
            if len(results) >= num_results:
                break
        if not results:
            print(f"    [brave] 0 results: {query[:60]}", flush=True)
        return results
    except Exception as e:
        print(f"    [brave] error: {e}", flush=True)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# SOCIAL LINK EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def _is_valid_social_slug(label, slug):
    slug_clean = slug.strip("/").split("/")[0].lower()
    if slug_clean in SOCIAL_SKIP_SLUGS:
        return False
    if label == "Twitter Link" and slug_clean in {"twitter","x","share","intent"}:
        return False
    if label == "Facebook Link" and slug_clean in {"sharer","share","dialog"}:
        return False
    return True

def extract_social_links_from_html(html, base_url):
    found = {}
    raw = html if isinstance(html, str) else html.decode("utf-8", errors="ignore")
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup.find_all(href=True):
        href = tag["href"].strip()
        for label, pattern in SOCIAL_PATTERNS.items():
            if label in found:
                continue
            m = pattern.search(href)
            if m:
                slug = m.group(0).split("/",1)[-1] if "/" in m.group(0) else (m.group(1) if m.lastindex else "")
                if not _is_valid_social_slug(label, slug):
                    continue
                full = href if href.startswith("http") else "https://www." + m.group(0)
                found[label] = full.split("?")[0].rstrip("/")
    for label, pattern in SOCIAL_PATTERNS.items():
        if label in found:
            continue
        for m in pattern.finditer(raw):
            matched = m.group(0)
            slug = matched.split("/",1)[-1] if "/" in matched else ""
            if not _is_valid_social_slug(label, slug):
                continue
            found[label] = ("https://www." + matched).split("?")[0].rstrip("/")
            break
    return found

def collect_site_social_links(pages_html):
    merged = {}
    for url, html in pages_html:
        for label, value in extract_social_links_from_html(html, url).items():
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
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)

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

def is_js_shell(html, body_text):
    for sig in JS_SHELL_SIGNALS:
        if sig in html:
            return True
    if len(html) > 5000 and len(body_text) < 800 and len(body_text) / len(html) < 0.05:
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# NOISE STRIPPING & DEDUP
# ─────────────────────────────────────────────────────────────────────────────

def strip_noise_lines(text):
    lines = text.splitlines()
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
    threshold = max(6, len(pages) // 2)
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
        lines = p["text"].splitlines()
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
        p2 = dict(p)
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
    good = []
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
# PAGE FETCHER
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# FIELD AUTO-EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def extract_all_founders(text):
    NAME = r'[A-Z][a-z]+(?:[ -][A-Z][a-z]+)+'
    names = []
    for chunk_m in re.finditer(
            r'founded\s+by\s+(.+?)(?:\s+in\b|\s+at\b|\s+with\b|\.|\Z)', text, re.IGNORECASE):
        for p in re.split(r'\s*,\s*|\s+and\s+', chunk_m.group(1).strip()):
            p = p.strip()
            if re.match(r'^' + NAME + r'$', p) and p not in names:
                names.append(p)
    m = re.search(
        r'(?:Drs?\.\s*)(' + NAME + r')(?:\s+and\s+(' + NAME + r'))?\s+(?:are\s+the\s+)?founders?', text)
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
            yr = re.search(r'\b(20\d{2}|19\d{2})\b', m.group(1))
            val = (yr.group(1) if yr else m.group(1)).strip()
            findings["Year Founded (external)"] = f"{val} (* {src})"
            print(f"          → Year Founded: {val}", flush=True)

    if "Address (raw)" not in findings:
        m = re.search(
            r'(?:headquarter|HQ|registered address|address|located(?:\s+at)?|'
            r'si[eè]ge social|adresse)[:\s]+([^\n]{15,150})',
            page_text, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            findings["Address (raw)"] = f"{val} (* {src})"
            print(f"          → Address: {val}", flush=True)

    found_names = extract_all_founders(page_text)
    if found_names:
        existing = findings.get("Founders (external)", "")
        ex_list = [n.strip() for n in re.sub(r'\(\* [^)]+\)', '', existing).split("|") if n.strip()]
        added = False
        for n in found_names:
            if n not in ex_list:
                ex_list.append(n)
                added = True
        if added:
            findings["Founders (external)"] = " | ".join(ex_list) + f" (* {src})"
            print(f"          → Founders: {ex_list}", flush=True)

    if "Funding (external)" not in findings:
        m = re.search(r'\$[\d\.]+ ?[MmBb]illion|\$[\d,]+[ \t]+(?:million|billion|[Mm]|[Bb])', page_text)
        if m:
            findings["Funding (external)"] = f"{m.group(0)} (* {src})"

    if "CIN" not in findings:
        m = re.search(r'\b([UL]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6})\b', page_text)
        if m:
            findings["CIN"] = f"{m.group(1)} (* {src})"
            print(f"          → CIN: {m.group(1)}", flush=True)

    dirs = re.findall(r'(?:Director|DIN)[:\s]+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)', page_text)
    if dirs and "Officers (India DB)" not in findings:
        findings["Officers (India DB)"] = " | ".join(dict.fromkeys(dirs[:10])) + f" (* {src})"
        print(f"          → Directors: {dirs[:5]}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# OPENCORPORATES API
# ─────────────────────────────────────────────────────────────────────────────

def opencorporates_api_search(company_name, jurisdiction=None):
    for name_try in name_variants(company_name):
        try:
            params = {"q": name_try, "format": "json"}
            if jurisdiction:
                params["jurisdiction_code"] = jurisdiction
            r = requests.get(
                f"{OC_API_BASE}/companies/search", params=params,
                headers={"User-Agent": HEADERS["User-Agent"]}, timeout=15,
            )
            if r.status_code == 429:
                print("    [OC API] Rate limited — waiting 30s...", flush=True)
                time.sleep(30)
                r = requests.get(f"{OC_API_BASE}/companies/search", params=params, timeout=15)
            if r.status_code != 200:
                print(f"    [OC API] HTTP {r.status_code} for '{name_try}'", flush=True)
                continue
            data = r.json()
            companies = data.get("results", {}).get("companies", [])
            if not companies:
                continue
            name_lower = name_try.lower()
            for item in companies:
                co = item.get("company", {})
                if name_lower.split()[0] in co.get("name", "").lower():
                    print(f"    [OC API] Matched: {co.get('name')} ({co.get('jurisdiction_code')}/{co.get('company_number')})", flush=True)
                    return co
            co = companies[0].get("company", {})
            print(f"    [OC API] Top result (fuzzy): {co.get('name')}", flush=True)
            return co
        except Exception as e:
            print(f"    [OC API] error for '{name_try}': {e}", flush=True)
    return None

def opencorporates_api_officers(company_number, jurisdiction):
    try:
        url = f"{OC_API_BASE}/companies/{jurisdiction}/{company_number}/officers"
        r = requests.get(url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=15)
        if r.status_code == 429:
            time.sleep(30)
            r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return []
        officers = r.json().get("results", {}).get("officers", [])
        return [{"name": o.get("officer",{}).get("name","").title(),
                 "position": o.get("officer",{}).get("position",""),
                 "start_date": o.get("officer",{}).get("start_date",""),
                 "end_date": o.get("officer",{}).get("end_date","")} for o in officers]
    except Exception as e:
        print(f"    [OC API officers] error: {e}", flush=True)
        return []

def research_opencorporates(company_name, findings):
    print("  [OC API] Searching OpenCorporates (global)...", flush=True)
    co = opencorporates_api_search(company_name)
    if not co:
        print("        → Not found on OpenCorporates API.", flush=True)
        return
    SRC = "OpenCorporates"
    cn  = co.get("company_number", "")
    jur = co.get("jurisdiction_code", "")
    if not jur:
        print("        → No jurisdiction in result.", flush=True)
        return
    findings["OpenCorporates Link"] = f"https://opencorporates.com/companies/{jur}/{cn}"
    print(f"        → OC Link: {findings['OpenCorporates Link']}", flush=True)
    if co.get("name") and "Legal Name" not in findings:
        findings["Legal Name"] = f"{co['name']} (* {SRC})"
        print(f"        → Legal Name: {co['name']}", flush=True)
    if co.get("company_type"):
        findings["Company Type (OC)"] = f"{co['company_type']} (* {SRC})"
    inc_date = co.get("incorporation_date", "") or ""
    if inc_date:
        findings["Incorporation Date (OC)"] = f"{inc_date} (* {SRC})"
        print(f"        → Incorporation Date: {inc_date}", flush=True)
    if cn:
        officers = opencorporates_api_officers(cn, jur)
        if officers:
            founders = [o for o in officers if o["start_date"] == inc_date and not o.get("end_date")]
            if founders:
                findings["Founders (OC — appointed on incorporation date)"] = \
                    " | ".join(o["name"] for o in founders) + f" (* {SRC})"
                print(f"        → Founder candidates: {findings['Founders (OC — appointed on incorporation date)']}", flush=True)
            current = [o for o in officers if not o.get("end_date")]
            if current:
                findings["Officers (OpenCorporates)"] = \
                    " | ".join(f"{o['name']} ({o['position']})" for o in current) + f" (* {SRC})"
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

    findings = {}
    snippets = []
    seen_urls = set()
    fetched_urls = set()
    company_domain = base_domain(company_website)
    variants = name_variants(company_name)

    broad_queries = []
    for v in variants:
        broad_queries.append(f'"{v}"')
        broad_queries.append(f'"{v}" pharmaceutical')
        broad_queries.append(f'"{v}" company profile')

    for qi, query in enumerate(broad_queries, 1):
        print(f"\n  [broad {qi}/{len(broad_queries)}] {query}", flush=True)
        results = brave_search(query, num_results=BROAD_RESULTS)
        print(f"    → {len(results)} results", flush=True)
        fetched_this_query = 0

        for r in results:
            url = r["url"]
            host = urlparse(url).netloc.lower().lstrip("www.")
            if host == company_domain or host.endswith("." + company_domain):
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)

            entry = {"query": query, "title": r["title"], "url": url,
                     "snippet": r["snippet"], "text": "", "layer": "broad"}

            max_fetches = BROAD_FETCH_PER_QUERY * 2 if is_high_value(url) else BROAD_FETCH_PER_QUERY
            if fetched_this_query < max_fetches and not should_skip_fetch(url):
                print(f"      fetch: {url[:80]}", flush=True)
                raw = fetch_page_text(url, max_chars=BROAD_SNIPPET_CHARS)
                if raw:
                    entry["text"] = raw
                    fetched_urls.add(url)
                    fetched_this_query += 1
                    extract_fields_from_text(raw, _source_label(url), findings)

            snippets.append(entry)

    print(f"\n  Broad search done: {len(snippets)} snippets, {len(fetched_urls)} pages fetched, {len(findings)} fields extracted.", flush=True)
    return findings, snippets, fetched_urls


# ─────────────────────────────────────────────────────────────────────────────
# LAYER 2 — TARGETED RESEARCH
# ─────────────────────────────────────────────────────────────────────────────

def targeted_research(company_name, company_website, already_fetched=None, site_social_links=None):
    print(f"\n{'='*60}", flush=True)
    print(f"  LAYER 2 — TARGETED RESEARCH: {company_name}", flush=True)
    print(f"{'='*60}", flush=True)

    already_fetched   = already_fetched   or set()
    site_social_links = site_social_links or {}
    findings = {}
    snippets = []

    for label, url in site_social_links.items():
        findings[label] = url
        print(f"  [social — from site] {label}: {url}", flush=True)

    variants = name_variants(company_name)

    def _add_snippet(r, layer="targeted"):
        snippets.append({"query": "", "title": r["title"], "url": r["url"],
                         "snippet": r["snippet"], "text": "", "layer": layer})
        return snippets[-1]

    def _fetch_if_new(url, max_chars=TARGETED_SNIPPET_CHARS):
        if url in already_fetched or should_skip_fetch(url):
            return ""
        print(f"        fetching: {url[:80]}", flush=True)
        text = fetch_page_text(url, max_chars=max_chars)
        if text:
            already_fetched.add(url)
        return text

    def _brave_multi(site_pattern, num=TARGETED_RESULTS):
        seen = set()
        all_r = []
        for v in variants:
            query = f'"{v}" {site_pattern}'
            print(f"        query: {query}", flush=True)
            for r in brave_search(query, num_results=num):
                if r["url"] not in seen:
                    seen.add(r["url"])
                    all_r.append(r)
        return all_r

    def _name_in(text):
        tl = text.lower()
        return any(v.lower().split()[0] in tl for v in variants)

    # 1. LinkedIn
    print("\n  [1/11] LinkedIn...", flush=True)
    if "LinkedIn Link" in findings:
        print(f"        → Already found on site: {findings['LinkedIn Link']}", flush=True)
    else:
        found_li = False
        for r in _brave_multi("site:linkedin.com/company"):
            if "linkedin.com/company" not in r["url"]:
                continue
            if _name_in(r["title"] + " " + r["snippet"]):
                findings["LinkedIn Link"] = r["url"]
                print(f"        → {r['url']}", flush=True)
                yr = re.search(r'[Ff]ounded[:\s]+(\d{4})', r["snippet"])
                if yr and "Year Founded (external)" not in findings:
                    findings["Year Founded (external)"] = f"{yr.group(1)} (* LinkedIn snippet)"
                _add_snippet(r)
                found_li = True
                break
        if not found_li:
            print("        → Not found.", flush=True)

    # 2. YouTube
    print("\n  [2/11] YouTube...", flush=True)
    if "YouTube Link" in findings:
        print(f"        → Already found on site: {findings['YouTube Link']}", flush=True)
    else:
        found_yt = False
        seen_yt = set()
        for v in variants:
            for q in [f'"{v}" site:youtube.com/@', f'"{v}" official channel site:youtube.com']:
                print(f"        query: {q}", flush=True)
                for r in brave_search(q, num_results=4):
                    if r["url"] in seen_yt or "youtube.com" not in r["url"]:
                        continue
                    seen_yt.add(r["url"])
                    if not any(x in r["url"] for x in ["/@","/channel/","/user/","/c/"]):
                        continue
                    if _name_in(r["title"] + " " + r["snippet"]):
                        findings["YouTube Link"] = r["url"]
                        print(f"        → {r['url']}", flush=True)
                        _add_snippet(r)
                        found_yt = True
                        break
                if found_yt:
                    break
            if found_yt:
                break
        if not found_yt:
            print("        → Not found.", flush=True)

    # 3. Crunchbase
    print("\n  [3/11] Crunchbase...", flush=True)
    found_cb = False
    for r in _brave_multi("site:crunchbase.com/organization"):
        if "crunchbase.com" not in r["url"] or "/organization/" not in r["url"]:
            continue
        cb_text = _fetch_if_new(r["url"], max_chars=4000)
        if cb_text and not _name_in(cb_text):
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

    # 4. OpenCorporates API
    print("\n  [4/11] OpenCorporates API...", flush=True)
    research_opencorporates(company_name, findings)

    # 5. SEC EDGAR
    print("\n  [5/11] SEC EDGAR...", flush=True)
    found_sec = False
    for v in variants:
        for r in brave_search(f'"{v}" site:sec.gov', num_results=TARGETED_RESULTS):
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

    # 6. Yahoo Finance
    print("\n  [6/11] Yahoo Finance...", flush=True)
    found_yf = False
    for v in variants:
        for r in brave_search(f'"{v}" stock ticker site:finance.yahoo.com', num_results=TARGETED_RESULTS):
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

    # 7. Patents
    print("\n  [7/11] Patents...", flush=True)
    found_ep = False
    for v in variants:
        for q in [f'"{v}" site:worldwide.espacenet.com', f'"{v}" applicant site:patents.google.com']:
            for r in brave_search(q, num_results=TARGETED_RESULTS):
                if ("espacenet.com" in r["url"] or "patents.google.com" in r["url"]) and _name_in(r["title"] + " " + r["snippet"]):
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

    # 8. Kompass / Europages / D&B
    print("\n  [8/11] Kompass / Europages / D&B...", flush=True)
    univ_found = False
    for db_site in ["site:kompass.com","site:europages.com","site:dnb.com","site:hoovers.com"]:
        if univ_found:
            break
        for v in variants:
            query = f'"{v}" {db_site}'
            print(f"        query: {query}", flush=True)
            for r in brave_search(query, num_results=3):
                if db_site.replace("site:","") not in r["url"]:
                    continue
                if not _name_in(r["title"] + " " + r["snippet"]):
                    continue
                print(f"        → {r['url']}", flush=True)
                e = _add_snippet(r)
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

    # 9. PharmaCompass / Trademo
    print("\n  [9/11] PharmaCompass / Trademo...", flush=True)
    pharma_found = False
    for db_site in ["site:pharmacompass.com","site:trademo.com","site:chemdmart.com"]:
        if pharma_found:
            break
        for v in variants:
            query = f'"{v}" {db_site}'
            print(f"        query: {query}", flush=True)
            for r in brave_search(query, num_results=2):
                if _name_in(r["title"] + " " + r["snippet"]):
                    print(f"        → {r['url']}", flush=True)
                    e = _add_snippet(r)
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

    # 10. RocketReach
    print("\n  [10/11] RocketReach...", flush=True)
    found_rr = False
    for v in variants:
        for r in brave_search(f'"{v}" site:rocketreach.co', num_results=TARGETED_RESULTS):
            if "rocketreach.co" not in r["url"]:
                continue
            if _name_in(r["title"] + " " + r["snippet"]):
                print(f"        → {r['url']}", flush=True)
                e = _add_snippet(r)
                txt = _fetch_if_new(r["url"], max_chars=3000)
                if txt:
                    e["text"] = txt
                    found_names = extract_all_founders(txt)
                    if found_names and "Founders (external)" not in findings:
                        findings["Founders (external)"] = " | ".join(found_names) + " (* RocketReach)"
                found_rr = True
                break
        if found_rr:
            break
    if not found_rr:
        print("        → Not found.", flush=True)

    # 11. Bureau van Dijk / Orbis
    print("\n  [11/11] Bureau van Dijk / Orbis...", flush=True)
    found_bvd = False
    for v in variants:
        for q in [f'"{v}" site:orbis.bvdinfo.com', f'"{v}" bureau van dijk company profile']:
            print(f"        query: {q}", flush=True)
            for r in brave_search(q, num_results=TARGETED_RESULTS):
                if _name_in(r["title"] + " " + r["snippet"]):
                    print(f"        → {r['url']}", flush=True)
                    e = _add_snippet(r)
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

    print(f"\n  Targeted research done: {len(findings)} fields found.", flush=True)
    if findings:
        print(f"  Fields: {', '.join(findings.keys())}", flush=True)
    else:
        print("  [WARNING] Targeted research found 0 fields.", flush=True)

    return findings, snippets


# ─────────────────────────────────────────────────────────────────────────────
# SITE CRAWL
# ─────────────────────────────────────────────────────────────────────────────

def extract_links(html, base_url, domain):
    soup = BeautifulSoup(html, "html.parser")
    found = set()
    for tag in soup.find_all(href=True):
        val = tag["href"].strip()
        if not val or val.startswith(("#","mailto:","tel:","javascript:")):
            continue
        url = urljoin(base_url, val)
        if same_site(url, domain) and urlparse(url).scheme in ("http","https"):
            if not should_skip_url(url):
                found.add(normalize(url))
    return found

def get_sitemap_urls(base_url, domain):
    urls = set()
    parsed = urlparse(base_url)
    roots = [f"{parsed.scheme}://{parsed.netloc}"]
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
        candidates += [f"{root}/sitemap.xml", f"{root}/sitemap_index.xml",
                       f"{root}/sitemap-index.xml", f"{root}/sitemaps.xml", f"{root}/wp-sitemap.xml"]
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
    for tag in soup(["script","style","nav","footer","header","aside","form",
                     "noscript","iframe","svg","button","meta","link","figure","figcaption"]):
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
            html = r.content.decode(r.apparent_encoding or "utf-8", errors="ignore")
            soup = BeautifulSoup(html, "html.parser")
            body_text = (soup.find("body") or soup).get_text(strip=True)
            if len(body_text) <= 300 or is_js_shell(html, body_text):
                js_domains.add(netloc)
            else:
                title = soup.title.string.strip() if soup.title else url
                soft404 = ["website not found","page not found","404","not registered","does not exist","coming soon"]
                if any(s in title.lower() or s in body_text[:300].lower() for s in soft404):
                    return None, None, None
                return html, title, html
        except (requests.exceptions.ConnectionError, requests.exceptions.SSLError):
            js_domains.add(netloc)
        except Exception:
            return None, None, None
    if driver:
        try:
            html, title = selenium_fetch(driver, url)
            js_domains.add(netloc)
            soup = BeautifulSoup(html, "html.parser")
            body_text = (soup.find("body") or soup).get_text(strip=True)
            if len(body_text) < 100:
                return None, None, None
            return html, title, html
        except Exception:
            pass
    return None, None, None

def scrape(start_url):
    domain = base_domain(start_url)
    visited = set()
    pages = []
    pages_html_raw = []
    seen_hashes = set()
    seen_urls = set()
    js_domains = set()

    print(f"\n{'='*60}", flush=True)
    print(f"  Scraping: {start_url}", flush=True)
    print(f"  Domain:   {domain}", flush=True)
    print(f"{'='*60}\n", flush=True)

    sitemap_urls = get_sitemap_urls(start_url, domain)
    queue = deque([normalize(start_url)])
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
            added = 0
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
# BUDGET ENFORCEMENT
# ─────────────────────────────────────────────────────────────────────────────

def truncate_smart(page, base_limit=MAX_CHARS_PER_PAGE):
    slug = urlparse(page["url"]).path.strip("/").split("/")[-1].lower()
    limit = base_limit if slug in PRIORITY_SLUGS else base_limit // 2
    text = page["text"]
    return text if len(text) <= limit else text[:limit] + "\n[...truncated]"

def _estimate_size(pages, snippets, research_findings, broad_findings):
    def page_budget(p):
        slug = urlparse(p["url"]).path.strip("/").split("/")[-1].lower()
        lim = MAX_CHARS_PER_PAGE if slug in PRIORITY_SLUGS else MAX_CHARS_PER_PAGE // 2
        return min(len(p["text"]), lim)
    return (sum(page_budget(p) for p in pages)
            + sum(len(s["title"])+len(s["url"])+len(s["snippet"])+len(s.get("text",""))+80 for s in snippets)
            + sum(len(k)+len(str(v))+10 for d in [research_findings, broad_findings] for k,v in d.items())
            + _INSTRUCTIONS_OVERHEAD + _HEADER_OVERHEAD)

def enforce_budget(pages, snippets, research_findings, broad_findings, char_limit=CHATGPT_CHAR_LIMIT):
    trim_log = []
    def over():
        return _estimate_size(pages, snippets, research_findings, broad_findings) > char_limit
    if over():
        for s in snippets:
            if s.get("text") and len(s["text"]) > 800:
                old = len(s["text"])
                s["text"] = compress_snippet_text(s["text"], max_chars=800)
                if len(s["text"]) < old:
                    trim_log.append(f"Compressed: {s['url'][:50]}")
    if over():
        for s in sorted(snippets, key=snippet_priority):
            if s.get("text") and not is_high_value(s["url"]):
                s["text"] = ""
                trim_log.append(f"Dropped text: {s['url'][:55]}")
                if not over():
                    break
    if over():
        for p in pages:
            slug = urlparse(p["url"]).path.strip("/").split("/")[-1].lower()
            if slug not in PRIORITY_SLUGS and len(p["text"]) > 2000:
                p["text"] = p["text"][:2000] + "\n[...trimmed]"
                trim_log.append(f"Trimmed page: {p['url'][:50]}")
                if not over():
                    break
    if trim_log:
        print(f"\n  [budget] {len(trim_log)} trims applied.", flush=True)
    return pages, snippets


# ─────────────────────────────────────────────────────────────────────────────
# PROMPT BUILDER — full original logic, all fields, all instructions
# ─────────────────────────────────────────────────────────────────────────────

def build_prompt(pages, start_url, research_findings, broad_findings=None, snippets=None):
    lines = []
    broad_findings = broad_findings or {}
    snippets = snippets or []
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
        lines.append("(Wide-net search — use only for fields not found in website or targeted DBs)")
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
        lines.append(" Espacenet, Kompass, Europages, D&B, PharmaCompass, RocketReach, BvD)")
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
        lines.append("  TRUST TIERS:")
        lines.append("  [SITE]  → Tier 1/2: from company's own HTML — authoritative")
        lines.append("  (* OC)  → Tier 3/4: OpenCorporates API — reliable for legal fields")
        lines.append("  (* Src) → Tier 3/4/5: other external source — verify against website")
        lines.append("")
        for k, v in all_findings.items():
            lines.append(f"  {k}: {v}")
        lines.append("")
        lines.append("=" * 60)

    lines.append("""
INSTRUCTIONS
============

You are a pharmaceutical/biotech company database researcher.
You have been given FOUR data sources — use ALL of them:

  1. SCRAPED WEBSITE PAGES              — the company's own official website
  2. PRE-RESEARCHED STRUCTURED FINDINGS — auto-extracted and labelled facts
  3. TARGETED DATABASE RESULTS          — LinkedIn, Crunchbase, OC API, SEC, etc.
  4. BROAD WEB SEARCH RESULTS           — wide-net search, catches any source

Do NOT invent any data. Only use what is explicitly present in the sources above.

══════════════════════════════════════════════════════
CRITICAL URL FORMAT RULE
══════════════════════════════════════════════════════
  ALWAYS write URLs as bare plain text. NEVER use markdown link syntax.
  CORRECT:  https://example.com/about
  WRONG:    [About](https://example.com/about)

══════════════════════════════════════════════════════
UNIVERSAL SOURCE PRIORITY
══════════════════════════════════════════════════════
  TIER 1 — Official website value → use as final answer, no tag needed.
  TIER 2 — Structured findings labelled [SITE] → treat as Tier 1.
  TIER 3 — Single external source, website silent → use + (unverified * SourceName)
  TIER 4 — Multiple external sources agree, website silent → (verified * Source1, Source2)
  TIER 5 — External sources disagree, website silent → CONFLICT: Source1 says X / Source2 says Y
  TIER 6 — External contradicts website → keep website value, add NOTE: [Source] says [value]

ADDITIVE FIELDS — external sources ADD entries, never override:
  Founders | Management Team | Certifications | Business Segments
  Start with website data. Add external entries tagged (unverified * SourceName).

══════════════════════════════════════════════════════
EXTRACTION RULES
══════════════════════════════════════════════════════

LINK ASSIGNMENT:
  Drug programs / pipeline / clinical work  → Pipeline Link
  Research papers / journal articles         → Publications Link
  Company history / founding story           → History Link
  Team bios                                  → Management Team Link
  Company mission / what it does             → Profile Link
  Approved products on market                → Products Link
  Delivery platform / core technology        → Technology Link
  Investor relations / funding               → Investor Link
  Job positions                              → Jobs Link
  Contact info / address / email form        → Contacts Link
  Paid services for external clients         → Services Link
  News / press releases                      → News Link

PEOPLE — read every line of every team/about page, list everyone:
  Founders: full name only, no titles, separated by <br>
  Management Team: Full Name – Role, one per line using <br>
  If page ends with [...truncated]: add NOTE: page was truncated — more people may exist

COMPANY NAME: use legal entity name with corporate suffix.
  If suffix unconfirmed on website → append (suffix unconfirmed)

COMPANY SUMMARY (minimum 4 sentences):
  Third person only. No "we/our/us". No promotional words (leading, pioneering, innovative).
  No year, founders, or location. Cover: sector, company type, therapeutic areas, disease focus.

CERTIFICATIONS: scan all pages for ISO, GMP, WHO-GMP, FDA, CE Mark, EMA, TGA, etc.

BUSINESS MODEL — select all that apply:
  drug delivery | pharma/bio | pharmaceutical services | generic | specialty pharma |
  agricultural | biosimilar | biobetter | chemicals | Consumer/OTC products |
  diagnostics | medical devices | lab equipment | Research/Reagent Supplies |
  pharma equipment | pharma excipients | research institution | incubator |
  venture capital | veterinary

MAJOR BUSINESS MODEL — same options plus: pharma/bio diversified

ADDRESS: put everything except country name into Address field.
  Country field gets only the country name.
  Example — Raw: "6 rue Solférino 78000 Versailles, France"
            Address: 6 rue Solférino 78000 Versailles
            Country: France

══════════════════════════════════════════════════════
OUTPUT FORMAT
══════════════════════════════════════════════════════
  Single markdown table, two columns: | Field | Value |
  First row = Company Name, second row = Company Summary
  Omit any row where no value was found
  Multi-value fields separated by <br>:
    Founders | Management Team | Business Model | Major Business Model |
    Business Segments | Certifications | Scraped Links
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
# COMPANY NAME CONFIRMATION
# ─────────────────────────────────────────────────────────────────────────────

def confirm_company_name(detected_name):
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
        print(f"\nERROR: {INPUT_FILE} not found.")
        print(f"Create a file called {INPUT_FILE} in the same folder.")
        print("Put one company URL per line.")
        return

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    if not urls:
        print(f"ERROR: {INPUT_FILE} is empty.")
        return

    print(f"\nFound {len(urls)} company URL(s) to research.\n", flush=True)

    for url_index, start_url in enumerate(urls, 1):
        print(f"\n{'#'*60}", flush=True)
        print(f"  COMPANY {url_index}/{len(urls)}: {start_url}", flush=True)
        print(f"{'#'*60}", flush=True)

        # 1. Crawl website
        pages, site_social_links = scrape(start_url)
        if not pages:
            print("  No pages scraped — skipping.", flush=True)
            continue

        # 2. Detect company name
        homepage = next((p for p in pages if normalize(p["url"]) == normalize(start_url)), pages[0])
        raw_title = homepage["title"]
        stripped = re.sub(
            r'\s*[|\-–—·•]\s*(.+)$',
            lambda m: "" if len(m.group(1).split()) >= 3 else m.group(0),
            raw_title,
        ).strip()
        company_name = stripped if stripped else raw_title.strip()
        company_name = _TITLE_PREFIX_RE.sub("", company_name).strip()
        if not company_name or company_name.lower() in _NAV_WORDS:
            company_name = base_domain(start_url).split(".")[0].title()

        # 3. Confirm name with user
        company_name = confirm_company_name(company_name)
        print(f"\n  Company name: \"{company_name}\"", flush=True)
        print(f"  Name variants: {name_variants(company_name)}", flush=True)

        # 4. Global dedup
        pages = apply_global_dedup(pages)

        # 5. Layer 1 — broad search
        broad_findings, broad_snippets, fetched_urls = broad_research(company_name, start_url)

        # 6. Layer 2 — targeted research
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
        prompt = build_prompt(pages, start_url, targeted_findings, broad_findings, all_snippets)
        prompt = strip_markdown_links(prompt)

        # 10. Save to named file
        safe_name = re.sub(r'[^\w\-]', '_', company_name.lower())
        out_file = f"{safe_name}_research.txt"
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(prompt)

        # 11. Copy to clipboard
        copy_to_clipboard(prompt)

        chars = len(prompt)
        fits  = chars <= CHATGPT_CHAR_LIMIT
        print(f"\n{'='*60}", flush=True)
        print(f"  DONE: {company_name}", flush=True)
        print(f"  File  : {out_file}  ← upload this to ChatGPT or Claude", flush=True)
        print(f"  Clipboard: ready to paste", flush=True)
        print(f"  Size  : {chars:,} chars (~{chars//4:,} tokens)", flush=True)
        print(f"  Limit : {CHATGPT_CHAR_LIMIT:,} chars → {'FITS ✓' if fits else f'OVER by {chars-CHATGPT_CHAR_LIMIT:,} chars ✗'}", flush=True)
        print(f"{'='*60}", flush=True)

        if url_index < len(urls):
            print(f"\n  Waiting 5s before next company...", flush=True)
            time.sleep(5)

    print("\n  All done. Upload the .txt file(s) to ChatGPT or Claude.\n", flush=True)


if __name__ == "__main__":
    main()
