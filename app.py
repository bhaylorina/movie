import re
import time
import json
import base64
import urllib.parse
import html as html_lib
from datetime import date
from urllib.parse import quote, unquote, urlsplit
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
import requests as std_requests
from flask import Flask, request, redirect, Response

app = Flask(__name__)

BASE_URL = "https://new15.hdhub4u.cl"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

session = cffi_requests.Session(impersonate="chrome110")
session.headers.update({
    'User-Agent': UA,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
})

api_session = std_requests.Session()
api_session.headers.update({
    "User-Agent": UA, "Accept": "application/json, text/plain, */*",
    "Origin": BASE_URL, "Referer": BASE_URL + "/",
})

cache_data = {"m3u": None, "timestamp": 0}
saved_movies = {}
SAVED_DURATION = 3 * 24 * 60 * 60

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

def extract_hubcloud_from_gadgets(url):
    try:
        resp = session.get(url, timeout=15)
        token_match = re.search(r"s\('o','([^']+)'", resp.text)
        if token_match:
            hubcloud_url = decrypt_token(token_match.group(1))
            if hubcloud_url:
                if "hblinks" in hubcloud_url:
                    hb_resp = session.get(hubcloud_url, timeout=15)
                    hb_soup = BeautifulSoup(hb_resp.text, 'html.parser')
                    for a in hb_soup.find_all('a'):
                        text = a.get_text(strip=True).lower()
                        href = str(a.get('href', '')).lower()
                        if 'hubcloud' in text or 'hubcloud' in href: return a['href']
                    return None
                return hubcloud_url
    except Exception: pass
    return None

def extract_hubcloud_from_hubdrive(url):
    try:
        resp = session.get(url, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for a in soup.find_all('a'):
            text = a.get_text(strip=True).lower()
            href = str(a.get('href', '')).lower()
            if 'hubcloud' in text or 'hubcloud' in href: return a['href']
    except Exception: pass
    return None

def convert_pixeldrain_to_direct(url):
    if not url: return None
    match = re.search(r'https?://(?:pixeldra\.in|pixeldrain\.com|pixeldrain\.net|pixeldrain\.dev)/(?:u|api/file|l)/([^?\s]+)', url)
    if match:
        return f"https://cdn.pixeldrain.eu.cc/{match.group(1)}"
    return url

# 🛑 नया: लिंक चेक करने का फंक्शन (HEAD Request)
def is_link_alive(url):
    if not url: return False
    try:
        # timeout 5s रखा है ताकि ज्यादा इंतज़ार न करना पड़े
        resp = session.head(url, timeout=5, allow_redirects=True)
        # 200 (OK) या 206 (Partial Content) होने पर लिंक सही है
        if resp.status_code in [200, 206]:
            return True
        print(f"      [❌] Link Dead ({resp.status_code}): {url[:50]}...")
        return False
    except Exception as e:
        print(f"      [❌] Link Check Failed: {e}")
        return False

# 🛑 अपडेटेड: यह अब एक लिस्ट (List) रिटर्न करेगा (FSL + Pixel सभी लिंक)
def get_all_hubcloud_links(hubcloud_url):
    all_links = []
    try:
        resp = session.get(hubcloud_url, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        gen_btn = soup.find('a', id='download')
        if not gen_btn:
            for a in soup.find_all('a'):
                if 'generate' in a.get_text(strip=True).lower() and 'download' in a.get_text(strip=True).lower():
                    gen_btn = a; break
        
        if not gen_btn or not gen_btn.get('href'): return []
            
        gen_url = gen_btn['href']
        if not gen_url.startswith('http'): gen_url = urllib.parse.urljoin(hubcloud_url, gen_url)
            
        gen_resp = session.get(gen_url, timeout=15)
        html_text = gen_resp.text.replace('&amp;', '&').replace('\\u0026', '&').replace('\u0026', '&')
        gen_soup = BeautifulSoup(html_text, 'html.parser')
        
        # 1. FSL Server (Direct Button)
        for a in gen_soup.find_all('a'):
            text = a.get_text(strip=True).lower()
            href = a.get('href', '')
            if 'fsl server' in text and 'fslv2' not in text and href.startswith('http'):
                all_links.append(href)
        
        # 2. FSL Server (Regex fallback)
        all_r2_links = re.findall(r'https?://[a-zA-Z0-9._-]+\.r2\.cloudflarestorage\.com/[^\s"\'<>]+', html_text)
        for l in all_r2_links:
            if 't3apac' not in l.lower() and 'fslv2' not in l.lower():
                all_links.append(l)

        # 3. PixelServer (Deep Search)
        found_links = set()
        for match in re.finditer(r'pixeldrain\.(?:dev|com)/u/([a-zA-Z0-9]+)', html_text): found_links.add(match.group(1))
        for a in gen_soup.find_all('a'):
            for attr in ['data-url', 'data-link', 'data-href', 'data-src']:
                val = a.get(attr)
                if val:
                    for m in re.finditer(r'pixeldrain\.(?:dev|com)/u/([a-zA-Z0-9]+)', val): found_links.add(m.group(1))
        
        parsed_url = urllib.parse.urlparse(gen_url)
        for key, values in urllib.parse.parse_qs(parsed_url.query).items():
            for val in values:
                for m in re.finditer(r'pixeldrain\.(?:dev|com)/u/([a-zA-Z0-9]+)', val): found_links.add(m.group(1))

        real_ids = [id for id in found_links if id != 'negn6f']
        for rid in real_ids:
            pix_url = convert_pixeldrain_to_direct(f"https://pixeldrain.dev/u/{rid}")
            if pix_url: all_links.append(pix_url)

        # डुप्लीकेट हटाएं लेकिन ऑर्डर बनाए रखें (FSL पहले, फिर Pixel)
        final_links = []
        for link in all_links:
            if link not in final_links: final_links.append(link)
            
        return final_links
    except Exception as e:
        print(f"HubCloud Error: {e}")
        return []

# ================= सर्च और सेव फंक्शन्स (पहले जैसे ही हैं) =================
def get_valid_saved():
    now = time.time()
    valid = {}
    for title, data in list(saved_movies.items()):
        if now - data.get("saved_at", 0) <= SAVED_DURATION: valid[title] = data
        else: del saved_movies[title]
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
    api_url = "https://search.pingora.fyi/collections/post/documents/search"
    params = {"q": query, "query_by": "post_title,category,stars,director,imdb_id", "query_by_weights": "4,2,2,2,4", "sort_by": "_text_match:desc,sort_by_date:desc", "per_page": 50, "highlight_fields": "none", "use_cache": "true", "page": 1, "analytics_tag": date.today().isoformat()}
    for client in (api_session, session):
        try:
            resp = client.get(api_url, params=params, timeout=10)
            if resp.status_code == 200:
                hits = resp.json().get("hits", [])
                results = [{"title": hit.get("document", {}).get("post_title", ""), "url": fix_movie_url(hit.get("document", {}).get("permalink", ""))} for hit in hits if hit.get("document", {}).get("permalink")]
                if results: return results
        except Exception: pass
    return []

def wp_search(query):
    wp_url = f"{BASE_URL}/wp-json/wp/v2/search"
    for client in (api_session, session):
        try:
            resp = client.get(wp_url, params={"search": query, "per_page": 50}, timeout=10)
            if resp.status_code == 200:
                results = [{"title": item.get("title", {}).get("rendered", ""), "url": fix_movie_url(item.get("url", ""))} for item in resp.json() if item.get("url")]
                if results: return results
        except Exception: pass
    return []

# ================= FLASK ROUTES =================
@app.route('/')
def home():
    saved_html = "".join([f'<div style="background:#222; padding:10px; margin:5px 0; border-radius:5px; display:flex; justify-content:space-between;"><span>{html_lib.escape(title)}</span><a href="/remove/{quote(title, safe="")}" style="color:red; text-decoration:none;">Remove</a></div>' for title in get_valid_saved()])
    if not saved_html: saved_html = "<p style='color:#888;'>No saved movies yet.</p>"
    return f"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>HDHub Scraper</title></head>
    <body style="font-family: Arial; background:#111; color:#fff; padding:20px; text-align:center;">
        <h1>🎬 HDHub4u Scraper</h1>
        <form action="/search" method="get"><input type="text" name="q" placeholder="Search movie..." required style="padding:10px; width:70%; max-width:300px; border-radius:5px; border:none;"><button type="submit" style="padding:10px 20px; background:#007bff; color:#fff; border:none; border-radius:5px;">Search</button></form>
        <h3 style="margin-top:40px;">⭐ Saved Movies</h3>{saved_html}<br><br>
        <a href="/playlist.m3u" style="color:#28a745; font-weight:bold; font-size:18px; text-decoration:none;">📥 Download M3U Playlist</a>
    </body></html>"""

@app.route('/search')
def search():
    query = request.args.get("q", "").strip()
    if not query: return redirect("/")
    results = typesense_search(query) or wp_search(query)
    results_html = "".join([f'''<div style="background:#222; padding:15px; margin:10px 0; border-radius:5px; display:flex; justify-content:space-between; align-items:center;"><span style="max-width:70%; font-size:14px;">{html_lib.escape(movie["title"])}</span><form action="/save" method="post" style="margin:0;"><input type="hidden" name="title" value="{quote(movie["title"], safe="")}"><input type="hidden" name="url" value="{base64.urlsafe_b64encode(movie["url"].encode()).decode()}"><button type="submit" style="padding:8px 15px; background:#28a745; color:#fff; border:none; border-radius:5px; cursor:pointer;">Save</button></form></div>''' for movie in results])
    if not results_html: results_html = "<p style='color:#aaa;'>No movies found. Try different spelling.</p>"
    return f"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>Search Results</title></head><body style="font-family: Arial; background:#111; color:#fff; padding:20px;"><a href="/" style="color:#fff; text-decoration:none;">⬅ Back</a><h3>Results for: "{html_lib.escape(query)}"</h3>{results_html}</body></html>"""

@app.route('/save', methods=['POST'])
def save():
    title = unquote(request.form.get("title", "")).strip()
    enc_url = request.form.get("url", "").strip()
    if title and enc_url:
        try:
            saved_movies[title] = {"title": title, "url": base64.urlsafe_b64decode(enc_url.encode()).decode(), "saved_at": time.time()}
        except Exception: pass
    return redirect("/")

@app.route('/remove/<path:title>')
def remove(title):
    saved_movies.pop(unquote(title), None)
    return redirect("/")

@app.route('/playlist.m3u')
def get_playlist():
    global cache_data
    if cache_data["m3u"] and (time.time() - cache_data["timestamp"] < 3600):
        saved_part = "".join([f'#EXTINF:-1 group-title="⭐ Saved Movies",{title}\nhttps://{request.host}/play?url={base64.urlsafe_b64encode(data["url"].encode()).decode()}\n' for title, data in get_valid_saved().items()])
        return Response(cache_data["m3u"] + saved_part, mimetype='audio/x-mpegurl')

    categories = [("bollywood-movies", "Bollywood Movies"), ("south-hindi-movies", "South Hindi Movies")]
    m3u, seen_urls = "#EXTM3U\n", set() 
    
    for cat_url, cat_name in categories:
        for page in range(1, 10):
            try:
                resp = session.get(f"{BASE_URL}/category/{cat_url}/page/{page}/", timeout=15)
                if resp.status_code != 200: continue
                soup = BeautifulSoup(resp.text, 'html.parser')
                for a_tag in soup.find_all('a', href=True):
                    title, href = a_tag.get_text(strip=True), a_tag['href']
                    if not title or len(title) < 10 or ("(2026)" not in title and "(2025)" not in title): continue
                    if not href.startswith('http'): href = BASE_URL + href
                    if BASE_URL not in href or '/category/' in href or '/page/' in href or 'how-to-download' in href: continue
                    if any(kw in title.lower() for kw in ["season", "ep-", "episode", "series", "s01", "s02", "how to download", "telegram", "play"]): continue
                    if href in seen_urls: continue
                    seen_urls.add(href)
                    m3u += f'#EXTINF:-1 group-title="{cat_name}",{title}\nhttps://{request.host}/play?url={base64.urlsafe_b64encode(href.encode()).decode()}\n'
            except Exception: pass

    m3u += "".join([f'#EXTINF:-1 group-title="⭐ Saved Movies",{title}\nhttps://{request.host}/play?url={base64.urlsafe_b64encode(data["url"].encode()).decode()}\n' for title, data in get_valid_saved().items()])
    cache_data["m3u"], cache_data["timestamp"] = m3u, time.time()
    return Response(m3u, mimetype='audio/x-mpegurl')

# 🛑 सबसे बड़ा अपडेट: अब यह हर लिंक को चेक करेगा
@app.route('/play')
def play_movie():
    encoded_url = request.args.get('url')
    if not encoded_url: return "No URL provided", 400

    try:
        movie_url = base64.urlsafe_b64decode(encoded_url.encode()).decode()
    except Exception:
        return "Invalid URL encoding", 400

    try:
        movie_resp = session.get(movie_url, timeout=15)
        movie_soup = BeautifulSoup(movie_resp.text, 'html.parser')
        
        best_link = get_best_link(movie_soup, movie_url)
        if not best_link: return "No 1080p link found for this movie", 404

        hubcloud = None
        if "greenmotors" in best_link or "gadgetsweb" in best_link: hubcloud = extract_hubcloud_from_gadgets(best_link)
        elif "hubdrive.blog" in best_link: hubcloud = extract_hubcloud_from_hubdrive(best_link)
        else: hubcloud = best_link

        if hubcloud:
            # सारे लिंक्स की लिस्ट निकालें (FSL पहले, फिर Pixel)
            all_links = get_all_hubcloud_links(hubcloud)
            
            print(f"  [*] Testing {len(all_links)} links for playback...")
            for link in all_links:
                print(f"    [🔍] Checking: {link[:60]}...")
                # 🛑 जादू यहाँ है: प्लेयर को भेजने से पहले लिंक चेक करो
                if is_link_alive(link):
                    print(f"    [✅] Link is ALIVE! Redirecting player...")
                    return redirect(link, code=302)
                else:
                    print(f"    [❌] Link is DEAD. Trying next fallback...")
            
            return "All extracted links are dead (404/451).", 404
                
        return "Failed to extract final HubCloud link.", 500
        
    except Exception as e:
        return f"Error during extraction: {str(e)}", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000, threaded=True)
