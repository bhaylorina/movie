import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import logging
import re
import time
from urllib.parse import quote, unquote, urljoin, urlparse, urlsplit, urlunparse
import html as html_mod

from bs4 import BeautifulSoup
from flask import (
    Flask, Response, redirect, render_template, render_template_string, 
    request, url_for, flash
)
import requests as std_requests

# ================= LOGGING SETUP =================
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = "change-this-ripal-flix-secret"

# ================= RTDB REST SETUP =================
RTDB_URL = "https://movies-d584a-default-rtdb.firebaseio.com".rstrip("/")
RTDB_SECRET = "rs9zuVo5ndVNnyWjmgExnaCEbt91uLU56CKPvgF5"
RTDB_PATH = "/saved_movies.json"

# ⚡ Persistent Session for Firebase
rtdb_session = std_requests.Session()

# ================= B64 HELPERS =================
def safe_b64decode(data):
    try:
        if not data:
            return None
        missing_padding = len(data) % 4
        if missing_padding:
            data += "=" * (4 - missing_padding)
        return base64.urlsafe_b64decode(data).decode("utf-8")
    except Exception:
        return None

def safe_b64encode(data):
    return base64.urlsafe_b64encode(data.encode()).decode()

# ================= RTDB FUNCTIONS =================
def _key(title):
    return safe_b64encode(title)

def _rtdb_get():
    try:
        r = rtdb_session.get(
            RTDB_URL + RTDB_PATH, params={"auth": RTDB_SECRET}, timeout=10
        )
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        logger.error(f"RTDB Get Error: {e}")
        return None

def _rtdb_patch(payload):
    try:
        r = rtdb_session.patch(
            RTDB_URL + RTDB_PATH,
            params={"auth": RTDB_SECRET},
            json=payload,
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        logger.error(f"RTDB Patch Error: {e}")
        return False

def _rtdb_delete(key):
    try:
        r = rtdb_session.delete(
            f"{RTDB_URL}/saved_movies/{key}.json",
            params={"auth": RTDB_SECRET},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        logger.error(f"RTDB Delete Error: {e}")
        return False

def get_valid_saved():
    data = _rtdb_get()
    if not data:
        return {}
    now = time.time()
    valid = {}
    expired = []
    for key, val in data.items():
        if isinstance(val, dict) and "saved_at" in val and "title" in val:
            if now - val.get("saved_at", 0) <= 3 * 24 * 60 * 60: # 3 Days
                valid[val["title"]] = val
            else:
                expired.append(key)
        else:
            expired.append(key)
    if expired:
        _rtdb_patch({k: None for k in expired})
    return valid

# ================= SCRAPER CONFIG =================
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

session = std_requests.Session()
session.headers.update(HEADERS)

# ================= HDHUB4U CONFIG =================
BASE_URL = "https://new15.hdhub4u.cl"
REGEX_GAME_DRIVE = re.compile(
    r"https?://(?:pixeldra\.in|pixeldrain\.(?:com|net|dev|eu\.cc))/(?:u|api/file|l)/([a-zA-Z0-9]+)"
)
REGEX_R2 = re.compile(r'https?://[^\s"\'<>]+\.r2\.cloudflarestorage\.com/[^\s"\'<>]+')
REGEX_SERIES_EXCLUDE = re.compile(
    r"(?:\bseason\b|\bs\d{1,2}\b|\bep(?:isode)?[\s.-]*\d+|\bweb[\s-]*series\b|\bseries\b|\ball\s*episodes\b|how\s*to\s*download|telegram)",
    re.I,
)
m3u_cache = {"content": None, "expiry": 0}

# ================= 10XFLIX CONFIG =================
SEARCH_API = "https://10xflix.beer/m/wp-json/wp/v2/posts"

# ================= SHARED HELPERS =================
def rot13(s):
    res = []
    for char in s:
        if "a" <= char <= "z":
            res.append(chr((ord(char) - ord("a") + 13) % 26 + ord("a")))
        elif "A" <= char <= "Z":
            res.append(chr((ord(char) - ord("A") + 13) % 26 + ord("A")))
        else:
            res.append(char)
    return "".join(res)

def decrypt_token(token):
    try:
        s1 = base64.b64decode(token).decode("utf-8")
        s2 = base64.b64decode(s1).decode("utf-8")
        s3 = rot13(s2)
        padded = s3 + "=" * ((4 - len(s3) % 4) % 4)
        s4 = base64.b64decode(padded).decode("utf-8")
        data = json.loads(s4)
        hubcloud_b64 = data.get("o")
        if hubcloud_b64:
            return base64.b64decode(hubcloud_b64).decode("utf-8")
    except Exception:
        pass
    return None

def convert_pixeldrain_to_direct(url):
    if not url:
        return None
    url = url.replace("&amp;", "&")
    match = REGEX_GAME_DRIVE.search(url)
    if match:
        file_id = match.group(1)
        direct_url = f"https://cdn.pixeldrain.eu.cc/{file_id}"
        logger.info(f"[🔄] Converted PixelDrain: {direct_url}")
        return direct_url
    return url

def clean_html_text(value):
    if value is None: return ""
    if isinstance(value, dict):
        value = value.get("rendered") or value.get("raw") or ""
    if not isinstance(value, str): value = str(value)
    return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)

def extract_title(post_title):
    if isinstance(post_title, dict):
        return clean_html_text(post_title.get("rendered") or post_title.get("raw") or "")
    return clean_html_text(post_title)

# ============================================================
# HDHUB4U LOGIC
# ============================================================
def get_best_link(movie_soup, base_url):
    candidates = []
    for a_tag in movie_soup.find_all("a", href=True):
        text = a_tag.get_text(strip=True)
        if "1080p" in text:
            href = a_tag["href"]
            if not href.startswith("http"):
                href = urljoin(base_url, href)
            size_match = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB)", text, re.I)
            size_gb = 0.0
            if size_match:
                val = float(size_match.group(1))
                unit = size_match.group(2).upper()
                size_gb = val / 1024 if unit == "MB" else val
            if size_gb > 0:
                candidates.append({"href": href, "text": text, "size": size_gb})
    if not candidates:
        return None

    def sort_key(c):
        if 2.0 <= c["size"] <= 6.0: return 1
        if c["size"] > 6.0: return 2
        return 3

    candidates.sort(key=sort_key)
    return candidates[0]["href"]

def get_all_links(movie_soup):
    links = []
    for a_tag in movie_soup.find_all("a", href=True):
        href = a_tag["href"]
        text = a_tag.get_text(strip=True)
        if any(domain in href for domain in ["greenmotors", "gadgetsweb", "hubdrive", "hubcdn", "hubcloud"]):
            if text and len(text) > 2 and "WATCH" not in text.upper() and "PLAYER" not in text.upper():
                links.append({"text": text, "url": href})
    return links

def extract_hubcloud_from_gadgets(url):
    try:
        resp = session.get(url, timeout=12)
        token_match = re.search(r"s\('o','([^']+)'", resp.text)
        if token_match:
            hubcloud_url = decrypt_token(token_match.group(1))
            if hubcloud_url:
                if "hblinks" in hubcloud_url:
                    hb_resp = session.get(hubcloud_url, timeout=12)
                    hb_soup = BeautifulSoup(hb_resp.text, "lxml")
                    hubdrive_fallback = None
                    for a in hb_soup.find_all("a"):
                        text = a.get_text(strip=True).lower()
                        href = str(a.get("href", "")).lower()
                        if "hubcloud" in text or "hubcloud" in href:
                            return a["href"]
                        if "hubdrive" in text or "hubdrive" in href:
                            hubdrive_fallback = a["href"]
                    if hubdrive_fallback:
                        return hubdrive_fallback
                return hubcloud_url
    except Exception:
        pass
    return None

def extract_hubcloud_from_hubdrive(url):
    try:
        resp = session.get(url, timeout=12)
        soup = BeautifulSoup(resp.text, "lxml")
        for a in soup.find_all("a"):
            text = a.get_text(strip=True).lower()
            href = str(a.get("href", "")).lower()
            if "hubcloud" in text or "hubcloud" in href:
                return a["href"]
    except Exception:
        pass
    return None

def process_hubcloud(hubcloud_url):
    try:
        resp = session.get(hubcloud_url, timeout=12)
        soup = BeautifulSoup(resp.text, "lxml")
        gen_btn = soup.find("a", id="download")
        if not gen_btn:
            for a in soup.find_all("a"):
                if "generate" in a.get_text().lower() and "download" in a.get_text().lower():
                    gen_btn = a
                    break
        if not gen_btn or not gen_btn.get("href"):
            return None
        gen_url = urljoin(hubcloud_url, gen_btn["href"])
        gen_resp = session.get(gen_url, timeout=12)
        gen_html = gen_resp.text.replace("&amp;", "&")
        gen_soup = BeautifulSoup(gen_html, "lxml")

        s3_btn = gen_soup.find("a", id="s3")
        if s3_btn and s3_btn.get("href"): return s3_btn["href"]
        fsl_btn = gen_soup.find("a", id="fsl")
        if fsl_btn and fsl_btn.get("href"): return fsl_btn["href"]

        for a in gen_soup.find_all("a", href=True):
            text = a.get_text(strip=True).lower()
            if "fsl server" in text and "fslv2" not in text:
                if "http" in a["href"]: return a["href"]

        all_r2_links = REGEX_R2.findall(gen_html)
        valid_r2 = [l for l in all_r2_links if "t3apac" not in l.lower() and "fslv2" not in l.lower()]
        if valid_r2: return valid_r2[0]

        pxl_btn = gen_soup.find("a", id="pxl-1")
        if pxl_btn:
            pxl_js_match = re.search(r"var pxl\s*=\s*['\"]([^'\"]+)['\"]", gen_html)
            if pxl_js_match and "negn6f" not in pxl_js_match.group(1):
                return convert_pixeldrain_to_direct(pxl_js_match.group(1))
            if pxl_btn.get("href") and "negn6f" not in pxl_btn.get("href"):
                return convert_pixeldrain_to_direct(pxl_btn.get("href"))

        for match in REGEX_GAME_DRIVE.finditer(gen_html):
            if match.group(1) != "negn6f":
                return convert_pixeldrain_to_direct(match.group(0))

        return None
    except Exception as e:
        logger.error(f"HubCloud Error: {e}")
    return None

def resolve_hdhub_final_link(target_url):
    if "greenmotors" in target_url or "gadgetsweb" in target_url:
        hubcloud = extract_hubcloud_from_gadgets(target_url)
    elif "hubdrive.blog" in target_url:
        hubcloud = extract_hubcloud_from_hubdrive(target_url)
    else:
        hubcloud = target_url

    if hubcloud:
        if "hubdrive.blog" in hubcloud:
            hubcloud = extract_hubcloud_from_hubdrive(hubcloud)
        if hubcloud:
            final_link = process_hubcloud(hubcloud)
            if final_link: return final_link
    return None

def fix_movie_url(url):
    if not url: return ""
    url = url.strip()
    base = urlsplit(BASE_URL)
    if url.startswith("//"): url = "https:" + url
    if not url.startswith("http"): return f"{base.scheme}://{base.netloc}/{url.lstrip('/')}"
    if "hdhub4u" in url.lower():
        parsed = urlsplit(url)
        return f"{base.scheme}://{base.netloc}{parsed.path}"
    return url

def typesense_search(query):
    api_url = "https://search.pingora.fyi/collections/post/documents/search"
    params = {
        "q": query, "query_by": "post_title,category,stars,director,imdb_id",
        "query_by_weights": "4,2,2,2,4", "sort_by": "_text_match:desc", "per_page": 50,
    }
    api_headers = {"Origin": BASE_URL, "Referer": BASE_URL + "/"}
    try:
        resp = session.get(api_url, params=params, headers=api_headers, timeout=8)
        if resp.status_code == 200:
            hits = resp.json().get("hits", [])
            return [{"title": h["document"]["post_title"], "url": fix_movie_url(h["document"]["permalink"])} for h in hits if "document" in h]
    except Exception: pass
    return []

def wp_search(query):
    wp_url = f"{BASE_URL}/wp-json/wp/v2/search"
    try:
        resp = session.get(wp_url, params={"search": query, "per_page": 50}, timeout=10)
        if resp.status_code == 200:
            results = []
            for item in resp.json():
                title = item.get("title", {})
                t_text = title.get("rendered", "") if isinstance(title, dict) else title
                url = item.get("url", "")
                if t_text and url:
                    results.append({"title": t_text, "url": fix_movie_url(url)})
            return results
    except Exception: pass
    return []

def _fetch_category_page(args):
    cat_url, cat_name, page, host = args
    items = []
    try:
        resp = session.get(f"{BASE_URL}/category/{cat_url}/page/{page}/", timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "lxml")
            for card in soup.find_all("li", class_="thumb"):
                a_tag = card.find("a", href=True)
                img_tag = card.find("img", class_="nx-poster")
                if not a_tag: continue
                t, h = a_tag.get_text(strip=True), a_tag["href"]
                if not t or len(t) < 5: continue
                if REGEX_SERIES_EXCLUDE.search(t) or REGEX_SERIES_EXCLUDE.search(h): continue
                logo = img_tag["src"] if img_tag and img_tag.get("src") else ""
                full_url = urljoin(BASE_URL, h)
                items.append((full_url, logo, cat_name, t))
    except Exception: pass
    return items


# ============================================================
# 10XFLIX LOGIC
# ============================================================
def search_flix_movies(query):
    params = {"search": query, "per_page": 100, "_embed": 1}
    response = session.get(SEARCH_API, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list): raise RuntimeError("Unexpected WordPress API response")
    results = []
    for post in data:
        title = extract_title(post.get("title"))
        page_url = post.get("url") or post.get("link") or ""
        if page_url:
            results.append({"title": title or page_url, "url": page_url})
    return results

def choose_flix_quality_link(page_html, page_url):
    soup = BeautifulSoup(page_html, "html.parser")
    candidates = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"): continue
        href = urljoin(page_url, href)
        text = a.get_text(" ", strip=True).lower()
        classes = " ".join(a.get("class", [])).lower()
        quality = None
        if "1080" in text or "wo" in classes: quality = "1080p"
        elif "720" in text or "hsl" in classes: quality = "720p"
        elif "480" in text or "sdl" in classes: quality = "480p"
        if quality: candidates.append({"quality": quality, "href": href, "text": text})
    for wanted in ("1080p", "720p", "480p"):
        for item in candidates:
            if item["quality"] == wanted: return item["href"], wanted
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True).lower()
        href = a.get("href", "").strip()
        if "download" in text and href.startswith("http"): return urljoin(page_url, href), "unknown"
    raise RuntimeError("No 1080p/720p download link found on movie page")

def unlock_mobilejsr(url):
    local_session = std_requests.Session()
    headers = {"User-Agent": UA, "Referer": url, "Origin": "https://mobilejsr.com"}
    response = local_session.get(url, headers=headers, timeout=30, allow_redirects=True)
    if response.status_code == 403: raise RuntimeError("Blocked by Cloudflare (403 Forbidden).")
    if response.status_code >= 400: raise RuntimeError(f"mobilejsr HTTP {response.status_code}")
    soup = BeautifulSoup(response.text, "html.parser")
    csrf_input = soup.find("input", {"name": "_csrf"})
    if not csrf_input: raise RuntimeError("Error: Could not find _csrf token on mobilejsr page.")
    payload = {"_csrf": csrf_input.get("value")}
    post_response = local_session.post(url, data=payload, headers=headers, timeout=30, allow_redirects=True)
    if post_response.status_code >= 400: raise RuntimeError(f"mobilejsr unlock HTTP {post_response.status_code}")
    unlocked_soup = BeautifulSoup(post_response.text, "html.parser")
    links = []
    container = unlocked_soup.find("section", class_="content-wrapper")
    if container:
        for a_tag in container.find_all("a", href=True):
            link = a_tag["href"]
            if link.startswith("http") and "is.gd" not in link and "dmus.in" not in link:
                links.append(link)
    links = list(dict.fromkeys(links))
    if not links: raise RuntimeError("No unlocked links found after POST")
    return links

def pick_hubcloud_link(links):
    for link in links:
        if "hubcloud" in link.lower(): return link
    for link in links:
        if "hub" in link.lower(): return link
    return links[0] if links else None

def normalize_hubcloud_link(url):
    parsed = urlparse(url)
    if "hubcloud" in parsed.netloc.lower():
        parsed = parsed._replace(scheme="https", netloc="hubcloud.mov")
        return urlunparse(parsed)
    return url

def extract_flix_pixel_link(hubcloud_url):
    hubcloud_url = normalize_hubcloud_link(hubcloud_url)
    try:
        resp = session.get(hubcloud_url, timeout=12)
        soup = BeautifulSoup(resp.text, "lxml")
        gen_btn = soup.find("a", id="download")
        if not gen_btn:
            for a in soup.find_all("a"):
                if "generate" in a.get_text().lower() and "download" in a.get_text().lower():
                    gen_btn = a
                    break
        if not gen_btn or not gen_btn.get("href"): raise RuntimeError("Generate button not found")
        gen_url = urljoin(hubcloud_url, gen_btn["href"])
        gen_resp = session.get(gen_url, timeout=12)
        gen_html = gen_resp.text.replace("&amp;", "&")
        gen_soup = BeautifulSoup(gen_html, "lxml")

        pxl_btn = gen_soup.find("a", id="pxl-1")
        if pxl_btn:
            pxl_js_match = re.search(r"var pxl\s*=\s*['\"]([^'\"]+)['\"]", gen_html)
            if pxl_js_match and "negn6f" not in pxl_js_match.group(1):
                return convert_pixeldrain_to_direct(pxl_js_match.group(1))
            if pxl_btn.get("href") and "negn6f" not in pxl_btn.get("href"):
                return convert_pixeldrain_to_direct(pxl_btn.get("href"))
        for match in REGEX_GAME_DRIVE.finditer(gen_html):
            if match.group(1) != "negn6f":
                return convert_pixeldrain_to_direct(match.group(0))
        raise RuntimeError("Pixel link not found on HubCloud page.")
    except Exception as e:
        raise RuntimeError(f"HubCloud extraction failed: {e}")

def resolve_flix_movie(post_url, title):
    if not post_url: raise RuntimeError("Missing post_url")
    page_response = session.get(post_url, timeout=30, allow_redirects=True)
    if page_response.status_code >= 400: raise RuntimeError(f"Movie page HTTP {page_response.status_code}")
    mobile_link, quality = choose_flix_quality_link(page_response.text, page_response.url)
    unlocked_links = unlock_mobilejsr(mobile_link)
    hubcloud_link = pick_hubcloud_link(unlocked_links)
    if not hubcloud_link: raise RuntimeError("No HubCloud link found after unlocking mobilejsr")
    final_link = extract_flix_pixel_link(hubcloud_link)
    return {
        "title": clean_html_text(title) or post_url,
        "quality": quality,
        "post_url": post_url,
        "download_url": final_link,
        "stream_url": final_link,
    }


# ============================================================
# FLASK ROUTES (HDHUB4U)
# ============================================================

@app.route("/")
def home():
    saved_list = [{"title": t, "title_q": quote(t, safe="")} for t, d in get_valid_saved().items()]
    return render_template("index.html", saved_movies=saved_list)

@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    if not query: return redirect("/")
    results = typesense_search(query) or wp_search(query)
    formatted = [{"title": m["title"], "enc_url": safe_b64encode(m["url"]), "title_q": quote(m["title"], safe="")} for m in results]
    return render_template("search.html", query=query, results=formatted)

@app.route("/manual")
def manual_links():
    encoded_url = request.args.get("url")
    title = unquote(request.args.get("title", "Movie Links"))
    movie_url = safe_b64decode(encoded_url)
    if not movie_url: return "Invalid URL", 400
    try:
        resp = session.get(movie_url, timeout=12)
        soup = BeautifulSoup(resp.text, "lxml")
        links = get_all_links(soup)
        formatted = [{"text": l["text"], "enc_url": safe_b64encode(l["url"]), "title_q": quote(f"{title} - {l['text']}", safe="")} for l in links]
        return render_template("manual.html", title=title, links=formatted)
    except Exception as e:
        return f"Error: {e}", 500

@app.route("/play_direct")
def play_direct():
    encoded_url = request.args.get("url")
    if not encoded_url: return "No URL provided", 400
    target_url = safe_b64decode(encoded_url)
    if not target_url: return "Invalid URL", 400
    try:
        final_link = resolve_hdhub_final_link(target_url)
        if final_link:
            if "cdn.pixeldrain.eu.cc/" in final_link:
                try:
                    r = session.get(final_link, allow_redirects=False, timeout=10)
                    loc = r.headers.get("Location", "")
                    if "/api/file/" in loc:
                        viewer_on_cdn = loc.replace("/api/file/", "/u/")
                        return redirect(viewer_on_cdn, code=302)
                except Exception as e: logger.error(f"CDN resolve error: {e}")
            return redirect(final_link, code=302)
        return "Failed to extract final link from this specific URL.", 500
    except Exception as e:
        import traceback
        return f"Error during extraction: {str(e)}\n{traceback.format_exc()}", 500

@app.route("/save", methods=["POST"])
def save():
    title = unquote(request.form.get("title", "")).strip()
    enc_url = request.form.get("url", "").strip()
    if title and enc_url:
        movie_url = safe_b64decode(enc_url)
        if movie_url:
            _rtdb_patch({_key(title): {"title": title, "url": movie_url, "saved_at": time.time()}})
    return redirect("/")

@app.route("/remove", methods=["POST"])
def remove():
    title = unquote(request.form.get("title", "")).strip()
    if title: _rtdb_delete(_key(title))
    return redirect("/")

@app.route("/playlist.m3u")
def get_playlist():
    global m3u_cache
    now = time.time()
    if m3u_cache["content"] and now < m3u_cache["expiry"]:
        categories_m3u = m3u_cache["content"]
    else:
        categories = [("bollywood-movies", "Bollywood Movies"), ("south-hindi-movies", "South Hindi Movies")]
        tasks = [(cat_url, cat_name, page, request.host) for cat_url, cat_name in categories for page in range(1, 5)]
        m3u_temp = "#EXTM3U\n"
        seen_urls = set()
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = executor.map(_fetch_category_page, tasks)
            for page_items in results:
                for full_url, logo, cat_name, t in page_items:
                    if full_url in seen_urls: continue
                    seen_urls.add(full_url)
                    m3u_temp += f'#EXTINF:-1 tvg-logo="{logo}" group-title="{cat_name}",{t}\nhttps://{request.host}/play?url={safe_b64encode(full_url)}\n'
        m3u_cache["content"], m3u_cache["expiry"] = m3u_temp, now + 3600
        categories_m3u = m3u_temp
    saved = get_valid_saved()
    saved_part = "".join([f'#EXTINF:-1 group-title="⭐ Saved Movies",{t}\nhttps://{request.host}/play?url={safe_b64encode(d["url"])}\n' for t, d in saved.items()])
    return Response(categories_m3u + saved_part, mimetype="audio/x-mpegurl")

@app.route("/play")
def play_movie():
    encoded_url = request.args.get("url")
    movie_url = safe_b64decode(encoded_url)
    if not movie_url: return "Invalid URL", 400
    try:
        if any(domain in movie_url for domain in ["greenmotors", "gadgetsweb", "hubdrive", "hubcdn", "hubcloud"]):
            best_link = movie_url
        else:
            movie_resp = session.get(movie_url, timeout=12)
            best_link = get_best_link(BeautifulSoup(movie_resp.text, "lxml"), movie_url)
            if not best_link: return "No 1080p link found", 404
        final_link = resolve_hdhub_final_link(best_link)
        if final_link: return redirect(final_link, code=302)
        return "Failed to extract final link", 500
    except Exception as e:
        logger.error(f"Play Error: {e}")
        return "Error during extraction", 500

# ============================================================
# FLASK ROUTES (FLIX SECTION / 10XFLIX)
# ============================================================

FLIX_BASE_STYLE = """
<style>
  body { font-family: Arial, Helvetica, sans-serif; background: #111; color: #fff; padding: 20px; text-align: center; }
  .save-item { background: #222; padding: 10px 12px; margin: 8px auto; border-radius: 6px; display: flex; justify-content: space-between; align-items: center; gap: 12px; max-width: 900px; text-align: left; }
  .movie-title { flex: 1; }
  .actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  input, select { padding: 10px; border-radius: 5px; border: none; font-size: 15px; }
  .btn, .btn-search, .btn-save { padding: 10px 16px; background: #007bff; color: #fff; border: none; border-radius: 5px; cursor: pointer; text-decoration: none; display: inline-block; font-size: 14px; }
  .btn-play { background: #28a745; }
  .btn-save { background: #ffc107; color: #111; }
  .btn-remove { background: none; border: none; color: #ff5555; cursor: pointer; padding: 0; }
  .download { color: #28a745; font-weight: bold; font-size: 18px; text-decoration: none; }
  .flash { margin: 10px auto; max-width: 900px; padding: 10px; border-radius: 5px; background: #333; }
  .flash.error { background: #6b1f1f; }
  .flash.success { background: #1f6b35; }
  small { color: #aaa; }
  a { color: #4da3ff; }
  @media (max-width: 640px) { .save-item { flex-direction: column; align-items: stretch; } .actions { justify-content: flex-end; } }
</style>
"""

FLIX_INDEX_HTML = """
<!DOCTYPE html>
<html>
<head><meta name="viewport" content="width=device-width, initial-scale=1"><title>Flix Scraper</title>{{ base_style|safe }}</head>
<body>
    <h1>FLIX SCRAPER (10xFlix)</h1>
    <p><a href="/" style="color: #4da3ff;">← Back to HDHub4U</a></p>
    <form action="/flix/search" method="get">
        <input type="text" name="q" value="{{ q or '' }}" placeholder="Search movie..." required style="width:60%; max-width:300px;">
        <button type="submit" class="btn-search">Search</button>
    </form>
    {% with messages = get_flashed_messages(with_categories=true) %}{% if messages %}{% for category, message in messages %}<div class="flash {{ category }}">{{ message }}</div>{% endfor %}{% endif %}{% endwith %}
    <h3 style="margin-top:40px;">Saved Movies</h3>
    {% if saved_movies %}
        {% for movie in saved_movies %}
        <div class="save-item">
            <span>{{ movie.title }} {% if movie.quality %}<small>({{ movie.quality }})</small>{% endif %}</span>
            <span class="actions">
                <a class="btn btn-play" href="/flix/play?post_url={{ movie.post_url }}&title={{ movie.title }}" target="_blank" rel="noreferrer">Play</a>
                <form action="/flix/remove" method="post" style="margin:0;display:inline;">
                    <input type="hidden" name="key" value="{{ movie.key }}">
                    <button type="submit" class="btn-remove">Remove</button>
                </form>
            </span>
        </div>
        {% endfor %}
    {% else %}<p style="color:#888;">No saved movies yet.</p>{% endif %}
    <p style="margin-top:30px;"><a href="/flix/playlist.m3u" class="download">Download Flix M3U Playlist</a></p>
</body>
</html>
"""

FLIX_RESULTS_HTML = """
<!DOCTYPE html>
<html>
<head><meta name="viewport" content="width=device-width, initial-scale=1"><title>Search Results</title>{{ base_style|safe }}</head>
<body>
    <h1>Flix Search Results</h1>
    <p>Query: <strong>{{ q }}</strong></p>
    {% with messages = get_flashed_messages(with_categories=true) %}{% if messages %}{% for category, message in messages %}<div class="flash {{ category }}">{{ message }}</div>{% endfor %}{% endif %}{% endwith %}
    {% if results %}
        {% for r in results %}
        <div class="save-item">
            <div class="movie-title">{{ r.title }}</div>
            <div class="actions">
                <a class="btn btn-play" href="/flix/play?post_url={{ r.url }}&title={{ r.title }}&q={{ q }}" target="_blank" rel="noreferrer">Play</a>
                <form action="/flix/save" method="post" style="margin:0;display:inline;">
                    <input type="hidden" name="post_url" value="{{ r.url }}">
                    <input type="hidden" name="title" value="{{ r.title }}">
                    <input type="hidden" name="q" value="{{ q }}">
                    <button type="submit" class="btn-save">Save</button>
                </form>
            </div>
        </div>
        {% endfor %}
    {% else %}<p>No results found.</p>{% endif %}
    <p><a href="/flix">Back to Flix Home</a></p>
</body>
</html>
"""

FLIX_ERROR_HTML = """
<!DOCTYPE html>
<html>
<head><meta name="viewport" content="width=device-width, initial-scale=1"><title>Extraction Error</title>{{ base_style|safe }}</head>
<body>
    <h1>Extraction failed</h1>
    <div class="flash error" style="white-space: pre-wrap; text-align: left; font-family: monospace;">{{ error }}</div>
    <p><a href="/flix">Back to Flix Home</a></p>
</body>
</html>
"""

def make_flix_movie_key(title):
    if not title: title = "unknown"
    return base64.b64encode(title.encode('utf-8')).decode('utf-8')

@app.route("/flix")
def flix_index():
    q = request.args.get("q", "")
    try:
        all_saved = get_valid_saved()
        saved_movies = [{"key": _key(t), "title": val.get("title"), "quality": "", "post_url": val.get("url"), "download_url": val.get("url")} for t, val in all_saved.items()]
    except Exception as e:
        flash(f"Could not load saved movies: {e}", "error")
        saved_movies = []
    return render_template_string(FLIX_INDEX_HTML, base_style=FLIX_BASE_STYLE, saved_movies=saved_movies, q=q)

@app.route("/flix/search")
def flix_search():
    q = request.args.get("q", "").strip()
    if not q: return redirect(url_for("flix_index"))
    try:
        results = search_flix_movies(q)
    except Exception as e:
        flash(f"Search failed: {e}", "error")
        results = []
    return render_template_string(FLIX_RESULTS_HTML, base_style=FLIX_BASE_STYLE, q=q, results=results)

@app.route("/flix/play")
def flix_play_movie():
    post_url = request.args.get("post_url", "").strip()
    title = request.args.get("title", "").strip()
    try:
        key = make_flix_movie_key(title)
        cached = _rtdb_get() or {}
        cached_entry = cached.get(key) if key in cached else None
        
        final_link = None
        current_time = time.time()
        
        if cached_entry and isinstance(cached_entry, dict) and "saved_at" in cached_entry:
            if current_time - cached_entry["saved_at"] <= 3 * 24 * 60 * 60:
                final_link = cached_entry.get("url")
            else:
                if cached_entry.get("url"): post_url = cached_entry.get("url")
                _rtdb_delete(key)

        if not final_link:
            movie = resolve_flix_movie(post_url, title)
            final_link = movie["download_url"]
            _rtdb_patch({key: {"title": title, "url": final_link, "saved_at": time.time()}})

        if "cdn.pixeldrain.eu.cc/" in final_link:
            try:
                r = session.get(final_link, allow_redirects=False, timeout=10)
                loc = r.headers.get("Location", "")
                if "/api/file/" in loc:
                    viewer_on_cdn = loc.replace("/api/file/", "/u/")
                    return redirect(viewer_on_cdn, code=302)
            except Exception as e: logger.error(f"CDN resolve error: {e}")

        return redirect(final_link, code=302)
    except Exception as e:
        import traceback
        return render_template_string(FLIX_ERROR_HTML, base_style=FLIX_BASE_STYLE, error=f"{str(e)}\n\n{traceback.format_exc()}"), 502

@app.route("/flix/save", methods=["POST"])
def flix_save_movie():
    post_url = request.form.get("post_url", "").strip()
    title = request.form.get("title", "").strip()
    q = request.form.get("q", "").strip()
    try:
        movie = resolve_flix_movie(post_url, title)
        _rtdb_patch({make_flix_movie_key(title): {"title": title, "url": movie["download_url"], "saved_at": time.time()}})
        flash(f"Saved: {movie['title']} ({movie.get('quality')})", "success")
    except Exception as e:
        flash(f"Save failed: {e}", "error")
    if q: return redirect(url_for("flix_search", q=q))
    return redirect(url_for("flix_index"))

@app.route("/flix/remove", methods=["POST"])
def flix_remove_movie():
    key = request.form.get("key", "").strip()
    try:
        _rtdb_delete(key)
        flash("Removed.", "success")
    except Exception as e:
        flash(f"Remove failed: {e}", "error")
    return redirect(url_for("flix_index"))

@app.route("/flix/playlist.m3u")
def flix_playlist():
    try:
        all_saved = get_valid_saved()
        movies = [{"title": val.get("title"), "url": val.get("url")} for t, val in all_saved.items()]
    except Exception:
        movies = []
    lines = ["#EXTM3U"]
    for movie in movies:
        title = (movie.get("title") or "Unknown").replace('"', "'")
        title = re.sub(r"\s+", " ", title).strip()
        url = movie.get("url")
        if not url: continue
        lines.append(f'#EXTINF:-1 tvg-name="{title}" group-title="Flix",{title}')
        lines.append(url)
    body = "\n".join(lines) + "\n"
    return Response(body, mimetype="audio/x-mpegurl", headers={"Content-Disposition": "attachment; filename=flix_ripal.m3u"})

# ============================================================
# MAIN APP RUNNER
# ============================================================
if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)
