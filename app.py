import base64
from datetime import date
import html as html_lib
import json
import re
import time
import urllib.parse
import logging
from urllib.parse import quote, unquote, urlsplit, urljoin, urlparse
from bs4 import BeautifulSoup
from flask import Flask, Response, redirect, request, render_template
import requests as std_requests

# ================= LOGGING SETUP =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ================= RTDB REST SETUP =================
RTDB_URL = "https://movies-d584a-default-rtdb.firebaseio.com" # Replace with your RTDB URL
RTDB_SECRET = "rs9zuVo5ndVNnyWjmgExnaCEbt91uLU56CKPvgF5" # Replace with your RTDB Secret
RTDB_PATH = "/saved_movies.json"

rtdb_session = std_requests.Session()

# ================= B64 HELPERS =================
def safe_b64decode(data):
    try:
        if not data: return None
        missing_padding = len(data) % 4
        if missing_padding: data += '=' * (4 - missing_padding)
        return base64.urlsafe_b64decode(data).decode('utf-8')
    except Exception: return None

def safe_b64encode(data):
    return base64.urlsafe_b64encode(data.encode()).decode()

# ================= RTDB FUNCTIONS =================
def _key(title): return safe_b64encode(title)

def _rtdb_get():
    try:
        r = rtdb_session.get(RTDB_URL + RTDB_PATH, params={"auth": RTDB_SECRET}, timeout=15)
        return r.json() if r.status_code == 200 else None
    except Exception as e: 
        logger.error(f"RTDB Get Error: {e}")
        return None

def _rtdb_patch(payload):
    try:
        r = rtdb_session.patch(RTDB_URL + RTDB_PATH, params={"auth": RTDB_SECRET}, json=payload, timeout=15)
        return r.status_code == 200
    except Exception as e: 
        logger.error(f"RTDB Patch Error: {e}")
        return False

def _rtdb_delete(key):
    try:
        r = rtdb_session.delete(f"{RTDB_URL}/saved_movies/{key}.json", params={"auth": RTDB_SECRET}, timeout=15)
        return r.status_code == 200
    except Exception as e: 
        logger.error(f"RTDB Delete Error: {e}")
        return False

def load_saved_movies_from_cloud():
    global saved_movies
    data = _rtdb_get()
    if data: saved_movies = data
    return data is not None

# ================= SCRAPER CONFIG =================
BASE_URL = "https://new15.hdhub4u.cl"
# High-quality realistic browser User-Agent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# REGEX DEFINITIONS
REGEX_PIXELDRAIN = re.compile(r'pixeldrain\.(?:dev|com|net|eu\.cc)/(?:u|api/file|l)/([a-zA-Z0-9]+)')
REGEX_R2 = re.compile(r'https?://r2\.cloudflarestorage\.com/[^\s"\'<>]+')

m3u_cache = {"content": None, "expiry": 0}
saved_movies = {}
SAVED_DURATION = 3 * 24 * 60 * 60 

# Using a single session for everything to maximize speed via connection pooling
session = std_requests.Session()
session.headers.update(HEADERS)

# ================= SNIPER LOGIC FUNCTIONS =================
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
        padded = s3 + "=" * ((4 - len(s3) % 4) % 4)
        s4 = base64.b64decode(padded).decode('utf-8')
        data = json.loads(s4)
        hubcloud_b64 = data.get('o')
        if hubcloud_b64: return base64.b64decode(hubcloud_b64).decode('utf-8')
    except Exception: pass
    return None

def get_best_link(movie_soup, base_url):
    candidates = []
    for tag in movie_soup.find_all(['h3', 'h4']):
        a_tag = tag.find('a', href=True)
        if not a_tag: continue
        text = a_tag.get_text(strip=True)
        if "1080p" in text:
            href = a_tag['href']
            if not href.startswith('http'): href = urljoin(base_url, href)
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
                    hb_soup = BeautifulSoup(hb_resp.text, 'lxml')
                    hubdrive_fallback = None
                    for a in hb_soup.find_all('a'):
                        text = a.get_text(strip=True).lower()
                        href = str(a.get('href', '')).lower()
                        if 'hubcloud' in text or 'hubcloud' in href: return a['href']
                        if 'hubdrive' in text or 'hubdrive' in href: hubdrive_fallback = a['href']
                    if hubdrive_fallback: return hubdrive_fallback
                return hubcloud_url
    except Exception as e: logger.error(f"Gadgets Extraction Error: {e}")
    return None

def extract_hubcloud_from_hubdrive(url):
    try:
        resp = session.get(url, timeout=15)
        soup = BeautifulSoup(resp.text, 'lxml')
        for a in soup.find_all('a'):
            text = a.get_text(strip=True).lower()
            href = str(a.get('href', '')).lower()
            if 'hubcloud' in text or 'hubcloud' in href: return a['href']
    except Exception as e: logger.error(f"Hubdrive Extraction Error: {e}")
    return None

def convert_pixeldrain_to_direct(url):
    """ Converts any PixelDrain link to a direct file link for Player. """
    if not url: return None
    url = url.replace("&amp;", "&")
    match = REGEX_PIXELDRAIN.search(url)
    if match: 
        return f"https://cdn.pixeldrain.eu.cc/{match.group(1)}"
    return url

def process_hubcloud(hubcloud_url):
    try:
        resp = session.get(hubcloud_url, timeout=15)
        html_text = resp.text
        soup = BeautifulSoup(html_text, 'lxml')
        gen_btn = soup.find('a', id='download')
        if not gen_btn:
            for a in soup.find_all('a'):
                if 'generate' in a.get_text().lower() and 'download' in a.get_text().lower():
                    gen_btn = a
                    break
        if not gen_btn or not gen_btn.get('href'): return None
        gen_url = urljoin(hubcloud_url, gen_btn['href'])
        gen_resp = session.get(gen_url, timeout=15)
        gen_html = gen_resp.text
        gen_soup = BeautifulSoup(gen_html, 'lxml')
        
        s3_btn = gen_soup.find('a', id='s3')
        if s3_btn and s3_btn.get('href'): return s3_btn['href']
        fsl_btn = gen_soup.find('a', id='fsl')
        if fsl_btn and fsl_btn.get('href'): return fsl_btn['href']
        pxl_btn = gen_soup.find('a', id='pxl-1')
        if pxl_btn:
            pxl_js_match = re.search(r"var pxl\s*=\s*['\"]([^'\"]+)['\"]", gen_html)
            if pxl_js_match: return convert_pixeldrain_to_direct(pxl_js_match.group(1))
            return convert_pixeldrain_to_direct(pxl_btn.get('href'))
        
        all_r2_links = REGEX_R2.findall(gen_html)
        if all_r2_links:
            for link in all_r2_links:
                if 'X-Amz-Expires=28800' in link: return link
            return all_r2_links[0]
        return None
    except Exception as e: logger.error(f"HubCloud Processing Error: {e}")
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
            if now - data.get("saved_at", 0) <= SAVED_DURATION: valid[data["title"]] = data
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
    params = {"q": query, "query_by": "post_title,category,stars,director,imdb_id", "query_by_weights": "4,2,2,2,4", "sort_by": "_text_match:desc,sort_by_date:desc", "per_page": 50, "highlight_fields": "none", "use_cache": "true", "page": 1, "analytics_tag": today}
    try:
        resp = session.get(api_url, params=params, timeout=10)
        if resp.status_code == 200:
            hits = resp.json().get("hits", [])
            results = [{"title": h["document"]["post_title"], "url": fix_movie_url(h["document"]["permalink"])} for h in hits if "document" in h]
            if results: return results
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
                if t_text and url: results.append({"title": t_text, "url": fix_movie_url(url)})
            if results: return results
    except Exception: pass
    return []

# ================= FLASK ROUTES =================
@app.route('/')
def home():
    saved_list = [{"title": t, "title_q": quote(t, safe="")} for t, d in get_valid_saved().items()]
    return render_template('index.html', saved_movies=saved_list)

@app.route('/search')
def search():
    query = request.args.get("q", "").strip()
    mode = request.args.get("mode", "auto")
    if not query: return redirect("/")
    results = typesense_search(query) or wp_search(query)
    formatted = [{"title": m["title"], "enc_url": safe_b64encode(m["url"]), "title_q": quote(m["title"], safe="")} for m in results]
    return render_template('search.html', query=query, mode=mode, results=formatted)

@app.route('/manual')
def manual_links():
    encoded_url = request.args.get('url')
    title = unquote(request.args.get('title', 'Movie Links'))
    movie_url = safe_b64decode(encoded_url)
    if not movie_url: return "Invalid URL", 400
    try:
        resp = session.get(movie_url, timeout=15)
        soup = BeautifulSoup(resp.text, 'lxml')
        links = get_all_links(soup)
        formatted = [{"text": l['text'], "enc_url": safe_b64encode(l['url']), "title_q": quote(f"{title} - {l['text']}", safe="")} for l in links]
        return render_template('manual.html', title=title, links=formatted)
    except Exception as e: return f"Error: {e}", 500

@app.route("/play_direct")
def play_direct():
    """ BROWSER ROUTE: Returns a Viewer Page for PixelDrain so user can download/watch in browser. """
    encoded_url = request.args.get("url")
    target_url = safe_b64decode(encoded_url)
    if not target_url: return "Invalid URL", 400
    try:
        if "greenmotors" in target_url or "gadgetsweb" in target_url: hubcloud = extract_hubcloud_from_gadgets(target_url)
        elif "hubdrive.blog" in target_url: hubcloud = extract_hubcloud_from_hubdrive(target_url)
        else: hubcloud = target_url
        
        if hubcloud:
            if "hubdrive.blog" in hubcloud: hubcloud = extract_hubcloud_from_hubdrive(hubcloud)
            final_link = process_hubcloud(hubcloud)
            if final_link:
                # For Browser: Ensure PixelDrain uses /u/ format for the viewer page
                if "pixeldrain" in final_link:
                    match = REGEX_PIXELDRAIN.search(final_link)
                    if match:
                        return redirect(f"https://pixeldrain.com/u/{match.group(1)}", code=302)
                return redirect(final_link, code=302)
        return "Failed to extract link", 500
    except Exception: return "Error during extraction", 500

@app.route('/save', methods=['POST'])
def save():
    title = unquote(request.form.get("title", "")).strip()
    enc_url = request.form.get("url", "").strip()
    if title and enc_url:
        movie_url = safe_b64decode(enc_url)
        if movie_url:
            if _rtdb_patch({_key(title): {"title": title, "url": movie_url, "saved_at": time.time()}}):
                saved_movies[_key(title)] = {"title": title, "url": movie_url, "saved_at": time.time()}
    return redirect("/")

@app.route('/remove', methods=['POST'])
def remove():
    title = unquote(request.form.get("title", "")).strip()
    if title and _rtdb_delete(_key(title)): saved_movies.pop(_key(title), None)
    return redirect("/")

@app.route('/playlist.m3u')
def get_playlist():
    global m3u_cache
    now = time.time()
    if m3u_cache["content"] and now < m3u_cache["expiry"]: 
        categories_m3u = m3u_cache["content"]
    else:
        categories = [("bollywood-movies", "Bollywood Movies"), ("south-hindi-movies", "South Hindi Movies")]
        m3u_temp = "#EXTM3U\n"
        seen_urls = set()
        for cat_url, cat_name in categories:
            for page in range(1, 10):
                try:
                    resp = session.get(f"{BASE_URL}/category/{cat_url}/page/{page}/", timeout=15)
                    if resp.status_code != 200: continue
                    soup = BeautifulSoup(resp.text, 'lxml')
                    movie_cards = soup.find_all('li', class_='thumb')
                    for card in movie_cards:
                        a_tag = card.find('a', href=True)
                        img_tag = card.find('img', class_='nx-poster')
                        if not a_tag: continue
                        t, h = a_tag.get_text(strip=True), a_tag['href']
                        logo = img_tag['src'] if img_tag and img_tag.get('src') else ""
                        if not t or len(t) < 10: continue
                        full_url = urljoin(BASE_URL, h)
                        if full_url in seen_urls: continue
                        if any(kw in t.lower() for kw in ["season", "ep-", "episode", "series"]): continue
                        seen_urls.add(full_url)
                        m3u_temp += f'#EXTINF:-1 tvg-logo="{logo}" group-title="{cat_name}",{t}\nhttps://{request.host}/play?url={safe_b64encode(full_url)}\n'
                except Exception: pass
        m3u_cache["content"], m3u_cache["expiry"] = m3u_temp, now + 3600
        categories_m3u = m3u_temp

    saved_part = "".join([f'#EXTINF:-1 group-title="⭐ Saved Movies",{t}\nhttps://{request.host}/play?url={safe_b64encode(d["url"])}\n' for t, d in get_valid_saved().items()])
    return Response(categories_m3u + saved_part, mimetype='audio/x-mpegurl')

@app.route('/play')
def play_movie():
    """ PLAYER ROUTE: Returns a Direct File link for PixelDrain so it plays in VLC/Tivimate. """
    encoded_url = request.args.get('url')
    movie_url = safe_b64decode(encoded_url)
    if not movie_url: return "Invalid URL", 400
    try:
        if any(domain in movie_url for domain in ["greenmotors", "gadgetsweb", "hubdrive", "hubcdn", "hubcloud"]): 
            best_link = movie_url
        else:
            movie_resp = session.get(movie_url, timeout=15)
            best_link = get_best_link(BeautifulSoup(movie_resp.text, 'lxml'), movie_url)
            if not best_link: return "No 1080p link found", 404
        
        hubcloud = None
        if "greenmotors" in best_link or "gadgetsweb" in best_link: hubcloud = extract_hubcloud_from_gadgets(best_link)
        elif "hubdrive.blog" in best_link: hubcloud = extract_hubcloud_from_hubdrive(best_link)
        else: hubcloud = best_link
        
        if hubcloud:
            if "hubdrive.blog" in hubcloud: hubcloud = extract_hubcloud_from_hubdrive(hubcloud)
            final_link = process_hubcloud(hubcloud)
            if final_link:
                # For Player: Always convert to direct cdn.pixeldrain.eu.cc/ID format
                if "pixeldrain" in final_link:
                    final_link = convert_pixeldrain_to_direct(final_link)
                return redirect(final_link, code=302)
        return "Failed to extract final link", 500
    except Exception as e: 
        logger.error(f"Play Movie Error: {e}")
        return "Error during extraction", 500

if __name__ == '__main__':
    import os
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, threaded=True)
