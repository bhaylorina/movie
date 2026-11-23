import base64
from datetime import date
import html as html_lib
import json
import re
import time
import urllib.parse
import logging
from urllib.parse import quote, unquote, urlsplit
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
from flask import Flask, Response, redirect, request, render_template
import requests as std_requests

# ================= LOGGING SETUP =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ================= RTDB REST SETUP (Hardcoded as requested) =================
RTDB_URL = "remove"
RTDB_SECRET = "removegF5"
RTDB_PATH = "/saved_movies.json"

rtdb_session = std_requests.Session()

# ================= SECURITY: Domain Validation =================
# Only redirect to these domains to prevent Open Redirect attacks
TRUSTED_DOMAINS = [
    "pixeldrain.com", "pixeldrain.dev", "pixeldrain.net", "pixeldrain.eu.cc",
    "r2.cloudflarestorage.com", "fsl-server.com", "hubcloud.com", "hubdrive.blog"
]

def is_safe_url(url):
    if not url: return False
    return any(domain in url.lower() for domain in TRUSTED_DOMAINS)

# ================= B64 HELPERS (Optimization: Removed manual padding everywhere) =================
def safe_b64decode(data):
    """Handles padding automatically and decodes base64."""
    try:
        if not data: return None
        # Add padding if missing
        missing_padding = len(data) % 4
        if missing_padding:
            data += '=' * (4 - missing_padding)
        return base64.urlsafe_b64decode(data).decode('utf-8')
    except Exception as e:
        logger.error(f"B64 Decode Error: {e}")
        return None

def safe_b64encode(data):
    """Encodes string to urlsafe base64."""
    return base64.urlsafe_b64encode(data.encode()).decode()

# ================= RTDB FUNCTIONS =================
def _key(title):
    return safe_b64encode(title)

def _rtdb_get():
    try:
        r = rtdb_session.get(RTDB_URL + RTDB_PATH, params={"auth": RTDB_SECRET}, timeout=20)
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, dict) else {}
        logger.error(f"RTDB GET status {r.status_code}")
        return None
    except Exception as e:
        logger.error(f"RTDB GET error: {e}")
        return None

def _rtdb_patch(payload):
    try:
        r = rtdb_session.patch(RTDB_URL + RTDB_PATH, params={"auth": RTDB_SECRET}, json=payload, timeout=20)
        if r.status_code == 200:
            return True
        logger.error(f"RTDB PATCH status {r.status_code}")
        return False
    except Exception as e:
        logger.error(f"RTDB PATCH error: {e}")
        return False

def _rtdb_delete(key):
    try:
        r = rtdb_session.delete(f"{RTDB_URL}/saved_movies/{key}.json", params={"auth": RTDB_SECRET}, timeout=20)
        if r.status_code == 200:
            return True
        logger.error(f"RTDB DELETE status {r.status_code}")
        return False
    except Exception as e:
        logger.error(f"RTDB DELETE error: {e}")
        return False

def load_saved_movies_from_cloud():
    global saved_movies
    data = _rtdb_get()
    if data is None:
        return False
    saved_movies = data
    return True

# ================= SCRAPER CONFIG =================
BASE_URL = "https://new15.hdhub4u.cl"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

REGEX_PIXELDRAIN = re.compile(r'pixeldrain\.(?:dev|com)/u/([a-zA-Z0-9]+)')
REGEX_R2 = re.compile(r'https?://[a-zA-Z0-9._-]+\.r2\.cloudflarestorage\.com/[^\s"\'<>]+')
REGEX_BASE64_ATOB = re.compile(r"atob\(['\"]([^'\"]+)['\"]\)")
REGEX_BASE64_LONG = re.compile(r'[a-zA-Z0-9+/]{20,}={0,2}')
REGEX_GAME_DRIVE = re.compile(r'https?://(?:pixeldra\.in|pixeldrain\.com|pixeldrain\.net|pixeldrain\.dev)/(?:u|api/file|l)/([^?\s]+)')

# M3U Cache (Optimization: 1 hour for categories)
m3u_cache = {"content": None, "expiry": 0}
saved_movies = {}
SAVED_DURATION = 3 * 24 * 60 * 60 

session = cffi_requests.Session(impersonate="chrome110")
session.headers.update({'User-Agent': UA})

api_session = std_requests.Session()
api_session.headers.update({
    "User-Agent": UA,
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
})

# ================= CORE LOGIC (UNCHANGED) =================
def rot13(s):
    res = []
    for char in s:
        if 'a' <= char <= 'z': res.append(chr((ord(char) - ord('a') + 13) % 26 + ord('a')))
        elif 'A' <= char <= 'Z': res.append(chr((ord(char) - ord('A') + 13) % 26 + ord('A')))
        else: res.append(char)
    return "".join(res)

def decrypt_token(token):
    try:
        s1 = base64.b64decode(token).decode('utf-8')
        s2 = base64.b64decode(s1).decode('utf-8')
        s3 = rot13(s2)
        s4 = base64.b64decode(s3 + "=" * ((4 - len(s3) % 4) % 4)).decode('utf-8')
        data = json.loads(s4)
        hubcloud_b64 = data.get('o')
        if hubcloud_b64:
            return base64.b64decode(hubcloud_b64).decode('utf-8')
    except Exception: pass
    return None

def get_best_link(movie_soup, base_url):
    candidates = []
    for a_tag in movie_soup.find_all('a', href=True):
        text = a_tag.get_text(strip=True)
        if "1080p" in text:
            href = a_tag['href']
            if not href.startswith('http'): href = urllib.parse.urljoin(base_url, href)
            size_match = re.search(r'(\d+(?:\.\d+)?)\s*(GB|MB)', text, re.I)
            size_gb = 0.0
            if size_match:
                val = float(size_match.group(1))
                unit = size_match.group(2).upper()
                size_gb = val / 1024 if unit == 'MB' else val
            if size_gb > 0: candidates.append({'href': href, 'text': text, 'size': size_gb})
    if not candidates: return None
    def sort_key(c):
        if 2.0 <= c['size'] <= 6.0: return 1
        if c['size'] > 6.0: return 2
        return 3
    candidates.sort(key=sort_key)
    return candidates[0]['href']

def get_all_links(movie_soup):
    links = []
    for a_tag in movie_soup.find_all('a', href=True):
        href = a_tag['href']
        text = a_tag.get_text(strip=True)
        if any(domain in href for domain in ["greenmotors", "gadgetsweb", "hubdrive", "hubcdn", "hubcloud"]):
            if text and len(text) > 2 and "WATCH" not in text.upper() and "PLAYER" not in text.upper():
                links.append({"text": text, "url": href})
    return links

def extract_hubcloud_from_gadgets(url):
    try:
        resp = session.get(url, timeout=15)
        token_match = re.search(r"s\('o','([^']+)'", resp.text)
        if token_match:
            hubcloud_url = decrypt_token(token_match.group(1))
            if hubcloud_url:
                if "hblinks" in hubcloud_url:
                    hb_resp = session.get(hubcloud_url, timeout=15)
                    # Optimization: Changed to 'lxml'
                    hb_soup = BeautifulSoup(hb_resp.text, 'lxml')
                    hubdrive_fallback = None
                    for a in hb_soup.find_all('a'):
                        text = a.get_text(strip=True).lower()
                        href = str(a.get('href', '')).lower()
                        if 'hubcloud' in text or 'hubcloud' in href: return a['href']
                        if 'hubdrive' in text or 'hubdrive' in href: hubdrive_fallback = a['href']
                    return hubdrive_fallback or None
                return hubcloud_url
    except Exception as e: logger.error(f"Gadgets Error: {e}")
    return None

def extract_hubcloud_from_hubdrive(url):
    try:
        resp = session.get(url, timeout=15)
        # Optimization: Changed to 'lxml'
        soup = BeautifulSoup(resp.text, 'lxml')
        for a in soup.find_all('a'):
            text = a.get_text(strip=True).lower()
            href = str(a.get('href', '')).lower()
            if 'hubcloud' in text or 'hubcloud' in href: return a['href']
    except Exception: pass
    return None

def convert_pixeldrain_to_direct(url):
    if not url: return None
    url = url.replace("&amp;", "&")
    match = REGEX_GAME_DRIVE.search(url)
    if match:
        return f"https://cdn.pixeldrain.eu.cc/{match.group(1)}"
    return url

def process_hubcloud(hubcloud_url):
    try:
        resp = session.get(hubcloud_url, timeout=15)
        # Optimization: Changed to 'lxml'
        soup = BeautifulSoup(resp.text, 'lxml')
        gen_btn = soup.find('a', id='download')
        if not gen_btn:
            for a in soup.find_all('a'):
                if 'generate' in a.get_text().lower() and 'download' in a.get_text().lower():
                    gen_btn = a
                    break
        if not gen_btn or not gen_btn.get('href'): return None
        
        gen_url = urllib.parse.urljoin(hubcloud_url, gen_btn['href'])
        gen_resp = session.get(gen_url, timeout=15)
        html_text = gen_resp.text.replace('&amp;', '&').replace('\\u0026', '&').replace('\u0026', '&')
        # Optimization: Changed to 'lxml'
        gen_soup = BeautifulSoup(html_text, 'lxml')
        
        for a in gen_soup.find_all('a'):
            text = a.get_text(strip=True).lower()
            href = a.get('href', '')
            if 'fsl server' in text and 'fslv2' not in text:
                if href and 'http' in href: return href
        
        all_r2_links = REGEX_R2.findall(html_text)
        valid_r2_links = [l for l in all_r2_links if 't3apac' not in l.lower() and 'fslv2' not in l.lower()]
        if valid_r2_links:
            for link in valid_r2_links:
                if 'X-Amz-Expires=28800' in link: return link
            return valid_r2_links[0]
            
        found_links = set()
        for match in REGEX_PIXELDRAIN.finditer(html_text): found_links.add(match.group(1))
        
        # B64 checks using new helper
        for match in REGEX_BASE64_LONG.finditer(html_text):
            decoded = safe_b64decode(match.group(0))
            if decoded:
                for m in REGEX_PIXELDRAIN.finditer(decoded): found_links.add(m.group(1))
        
        for match in REGEX_BASE64_ATOB.finditer(html_text):
            decoded = safe_b64decode(match.group(1))
            if decoded:
                for m in REGEX_PIXELDRAIN.finditer(decoded): found_links.add(m.group(1))

        if found_links:
            return convert_pixeldrain_to_direct(f"https://pixeldrain.dev/u/{list(found_links)[0]}")
            
        return None
    except Exception as e:
        logger.error(f"HubCloud Error: {e}")
    return None

# ================= SEARCH & SAVED =================
def get_valid_saved():
    global saved_movies
    load_saved_movies_from_cloud()
    now = time.time()
    valid = {}
    expired = []
    for key, data in saved_movies.items():
        if isinstance(data, dict) and "saved_at" in data and "title" in data:
            if now - data.get("saved_at", 0) <= SAVED_DURATION:
                valid[data["title"]] = data
            else: expired.append(key)
        else: expired.append(key)
    if expired:
        _rtdb_patch({k: None for k in expired})
        for k in expired: saved_movies.pop(k, None)
    return valid

def fix_movie_url(url):
    if not url: return ""
    url = url.strip()
    base = urlsplit(BASE_URL)
    if url.startswith("//"): url = "https:" + url
    if not url.startswith("http"): return f"{base.scheme}://{base.netloc}/{url.lstrip('/')}"
    if "hdhub4u" in url.lower() or "hdhub" in url.lower():
        parsed = urlsplit(url)
        return f"{base.scheme}://{base.netloc}{parsed.path}"
    return url

def typesense_search(query):
    today = date.today().isoformat()
    api_url = "https://search.pingora.fyi/collections/post/documents/search"
    params = {
        "q": query, "query_by": "post_title,category,stars,director,imdb_id",
        "query_by_weights": "4,2,2,2,4", "sort_by": "_text_match:desc,sort_by_date:desc",
        "per_page": 50, "highlight_fields": "none", "use_cache": "true", "page": 1, "analytics_tag": today
    }
    for client in (api_session, session):
        try:
            resp = client.get(api_url, params=params, timeout=10)
            if resp.status_code == 200:
                hits = resp.json().get("hits", [])
                results = [{"title": h["document"]["post_title"], "url": fix_movie_url(h["document"]["permalink"])} for h in hits if "document" in h]
                if results: return results
        except Exception as e: logger.error(f"Typesense error: {e}")
    return []

def wp_search(query):
    wp_url = f"{BASE_URL}/wp-json/wp/v2/search"
    for client in (api_session, session):
        try:
            resp = client.get(wp_url, params={"search": query, "per_page": 50}, timeout=10)
            if resp.status_code == 200:
                results = []
                for item in resp.json():
                    title = item.get("title", {})
                    title_text = title.get("rendered", "") if isinstance(title, dict) else title
                    url = item.get("url", "")
                    if title_text and url: results.append({"title": title_text, "url": fix_movie_url(url)})
                if results: return results
        except Exception as e: logger.error(f"WP error: {e}")
    return []

# ================= FLASK ROUTES (Optimized with Jinja2) =================
@app.route('/')
def home():
    saved_list = []
    for title, data in get_valid_saved().items():
        saved_list.append({"title": title, "title_q": quote(title, safe="")})
    return render_template('index.html', saved_movies=saved_list)

@app.route('/search')
def search():
    query = request.args.get("q", "").strip()
    mode = request.args.get("mode", "auto")
    if not query: return redirect("/")
    results = typesense_search(query) or wp_search(query)
    
    formatted_results = []
    for movie in results:
        formatted_results.append({
            "title": movie["title"],
            "enc_url": safe_b64encode(movie["url"]),
            "title_q": quote(movie["title"], safe="")
        })
    return render_template('search.html', query=query, mode=mode, results=formatted_results)

@app.route('/manual')
def manual_links():
    encoded_url = request.args.get('url')
    title = unquote(request.args.get('title', 'Movie Links'))
    if not encoded_url: return "No URL provided", 400
    
    movie_url = safe_b64decode(encoded_url)
    if not movie_url: return "Invalid URL", 400

    try:
        resp = session.get(movie_url, timeout=15)
        # Optimization: Changed to 'lxml'
        soup = BeautifulSoup(resp.text, 'lxml')
        links = get_all_links(soup)

        formatted_links = []
        for link in links:
            full_save_title = f"{title} - {link['text']}"
            formatted_links.append({
                "text": link['text'],
                "enc_url": safe_b64encode(link['url']),
                "title_q": quote(full_save_title, safe="")
            })
        return render_template('manual.html', title=title, links=formatted_links)
    except Exception as e:
        logger.error(f"Manual extraction error: {e}")
        return f"Error: {e}", 500

@app.route("/play_direct")
def play_direct():
    encoded_url = request.args.get("url")
    target_url = safe_b64decode(encoded_url)
    if not target_url: return "Invalid URL", 400

    try:
        if "greenmotors" in target_url or "gadgetsweb" in target_url: hubcloud = extract_hubcloud_from_gadgets(target_url)
        elif "hubdrive.blog" in target_url: hubcloud = extract_hubcloud_from_hubdrive(target_url)
        else: hubcloud = target_url

        if hubcloud:
            if "hubdrive.blog" in hubcloud: hubcloud = extract_hubcloud_from_hubdrive(hubcloud)
            if hubcloud:
                final_link = process_hubcloud(hubcloud)
                # Security: Domain Validation
                if final_link and is_safe_url(final_link):
                    if "cdn.pixeldrain.eu.cc/" in final_link:
                        try:
                            r = session.get(final_link, allow_redirects=False, timeout=10)
                            loc = r.headers.get("Location", "")
                            if "/api/file/" in loc: return redirect(loc.replace("/api/file/", "/u/"), code=302)
                        except Exception: pass
                    return redirect(final_link, code=302)
        return "Failed to extract final link.", 500
    except Exception as e:
        logger.error(f"Play direct error: {e}")
        return "Error during extraction", 500

@app.route('/save', methods=['POST'])
def save():
    title = unquote(request.form.get("title", "")).strip()
    enc_url = request.form.get("url", "").strip()
    if title and enc_url:
        movie_url = safe_b64decode(enc_url)
        if movie_url:
            entry = {"title": title, "url": movie_url, "saved_at": time.time()}
            if _rtdb_patch({_key(title): entry}):
                saved_movies[_key(title)] = entry
    return redirect("/")

@app.route('/remove', methods=['POST'])
def remove():
    title = unquote(request.form.get("title", "")).strip()
    if title:
        if _rtdb_delete(_key(title)):
            saved_movies.pop(_key(title), None)
    return redirect("/")

@app.route('/playlist.m3u')
def get_playlist():
    global m3u_cache
    now = time.time()
    
    # Optimization: Category Cache for 1 hour
    if m3u_cache["content"] and now < m3u_cache["expiry"]:
        categories_m3u = m3u_cache["content"]
    else:
        categories = [("bollywood-movies", "Bollywood Movies"), ("south-hindi-movies", "South Hindi Movies")]
        m3u_temp = "#EXTM3U\n"
        seen_urls = set()
        for cat_url, cat_name in categories:
            for page in range(1, 10):
                try:
                    url = f"{BASE_URL}/category/{cat_url}/page/{page}/"
                    resp = session.get(url, timeout=15)
                    if resp.status_code != 200: continue
                    # Optimization: Changed to 'lxml'
                    soup = BeautifulSoup(resp.text, 'lxml')
                    for a_tag in soup.find_all('a', href=True):
                        title = a_tag.get_text(strip=True)
                        href = a_tag['href']
                        if not title or len(title) < 10 or not href.startswith('http') or BASE_URL not in href: continue
                        if any(kw in title.lower() for kw in ["season", "ep-", "episode", "series", "s01", "s02"]): continue
                        if href in seen_urls: continue
                        seen_urls.add(href)
                        m3u_temp += f'#EXTINF:-1 group-title="{cat_name}",{title}\n'
                        m3u_temp += f'https://{request.host}/play?url={safe_b64encode(href)}\n'
                except Exception as e: logger.error(f"M3U Error {cat_name} p{page}: {e}")
        
        m3u_cache["content"] = m3u_temp
        m3u_cache["expiry"] = now + 3600 # 1 hour
        categories_m3u = m3u_temp

    # Optimization: Saved Movies are ALWAYS fetched live from RTDB (Instant)
    saved_part = ""
    for title, data in get_valid_saved().items():
        saved_part += f'#EXTINF:-1 group-title="⭐ Saved Movies",{title}\n'
        saved_part += f'https://{request.host}/play?url={safe_b64encode(data["url"])}\n'
        
    return Response(categories_m3u + saved_part, mimetype='audio/x-mpegurl')

@app.route('/play')
def play_movie():
    encoded_url = request.args.get('url')
    movie_url = safe_b64decode(encoded_url)
    if not movie_url: return "Invalid URL", 400
        
    try:
        if any(domain in movie_url for domain in ["greenmotors", "gadgetsweb", "hubdrive", "hubcdn", "hubcloud"]):
            best_link = movie_url
        else:
            movie_resp = session.get(movie_url, timeout=15)
            # Optimization: Changed to 'lxml'
            movie_soup = BeautifulSoup(movie_resp.text, 'lxml')
            best_link = get_best_link(movie_soup, movie_url)
            if not best_link: return "No 1080p link found", 404

        hubcloud = None
        if "greenmotors" in best_link or "gadgetsweb" in best_link: hubcloud = extract_hubcloud_from_gadgets(best_link)
        elif "hubdrive.blog" in best_link: hubcloud = extract_hubcloud_from_hubdrive(best_link)
        else: hubcloud = best_link
            
        if hubcloud:
            if "hubdrive.blog" in hubcloud: hubcloud = extract_hubcloud_from_hubdrive(hubcloud)
            if hubcloud:
                final_link = process_hubcloud(hubcloud)
                # Security: Domain Validation
                if final_link and is_safe_url(final_link):
                    return redirect(final_link, code=302)
                    
        return "Failed to extract final link.", 500
    except Exception as e:
        logger.error(f"Play error: {e}")
        return "Error during extraction", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000, threaded=True)
