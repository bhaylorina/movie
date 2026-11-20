import re
import time
import json
import base64
import urllib.parse
import hashlib
from bs4 import BeautifulSoup
from curl_cffi import requests
from flask import Flask, request, redirect, Response, render_template_string

app = Flask(__name__)

BASE_URL = "https://new15.hdhub4u.cl"

session = requests.Session(impersonate="chrome110")
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
})

# ================= CACHE SYSTEM =================
# Main Playlist Cache (1 ghante ke liye)
main_cache = {"playlist": None, "timestamp": 0}

# Saved Movies Cache (3 din ke liye)
# Structure: { "url_hash": {"title": "...", "url": "...", "timestamp": 12345} }
saved_movies = {}
CACHE_EXPIRY_3_DAYS = 3 * 24 * 60 * 60 # seconds

def cleanup_saved_movies():
    current_time = time.time()
    expired = [k for k, v in saved_movies.items() if current_time - v['timestamp'] > CACHE_EXPIRY_3_DAYS]
    for k in expired:
        del saved_movies[k]

# ================= HELPER FUNCTIONS =================
def rot13(s):
    res = []
    for char in s:
        if 'a' <= char <= 'z':
            res.append(chr((ord(char) - ord('a') + 13) % 26 + ord('a')))
        elif 'A' <= char <= 'Z':
            res.append(chr((ord(char) - ord('A') + 13) % 26 + ord('A')))
        else:
            res.append(char)
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
    except Exception:
        pass
    return None

def get_best_link(movie_soup, base_url):
    candidates = []
    for a_tag in movie_soup.find_all('a', href=True):
        text = a_tag.get_text(strip=True)
        if "1080p" in text:
            href = a_tag['href']
            if not href.startswith('http'):
                href = urllib.parse.urljoin(base_url, href)
            size_match = re.search(r'(\d+(?:\.\d+)?)\s*(GB|MB)', text, re.I)
            size_gb = 0.0
            if size_match:
                val = float(size_match.group(1))
                unit = size_match.group(2).upper()
                size_gb = val / 1024 if unit == 'MB' else val
            if size_gb > 0:
                candidates.append({'href': href, 'text': text, 'size': size_gb})
            
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
                        if 'hubcloud' in text or 'hubcloud' in href:
                            return a['href']
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
            if 'hubcloud' in text or 'hubcloud' in href:
                return a['href']
    except Exception: pass
    return None

def convert_pixeldrain_to_direct(url):
    url = url.replace('pixeldrain.com', 'pixeldrain.dev')
    if "pixeldrain.dev/u/" in url:
        file_id = url.split("/u/")[-1].split("?")[0]
        return f"https://pixeldrain.dev/api/file/{file_id}?download"
    return url

def process_hubcloud(hubcloud_url):
    try:
        resp = session.get(hubcloud_url, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        gen_btn = soup.find('a', id='download') or soup.find('a', string=re.compile(r'Generate Direct Download Link', re.I))
        if not gen_btn or not gen_btn.get('href'): return None
            
        gen_url = gen_btn['href']
        if not gen_url.startswith('http'): gen_url = urllib.parse.urljoin(hubcloud_url, gen_url)
            
        gen_resp = session.get(gen_url, timeout=15)
        html_text = gen_resp.text.replace('&amp;', '&').replace('\\u0026', '&').replace('\u0026', '&')
        gen_soup = BeautifulSoup(html_text, 'html.parser')
        
        # PRIORITY 1: FSL SERVER (28800)
        for a in gen_soup.find_all('a'):
            text = a.get_text(strip=True).lower()
            href = a.get('href', '')
            if 'fsl server' in text and 'fslv2' not in text:
                if href and 'http' in href: return href
        
        all_r2_links = re.findall(r'https?://[a-zA-Z0-9._-]+\.r2\.cloudflarestorage\.com/[^\s"\'<>]+', html_text)
        valid_r2_links = [l for l in all_r2_links if 't3apac' not in l.lower() and 'fslv2' not in l.lower()]
        
        if valid_r2_links:
            for link in valid_r2_links:
                if 'X-Amz-Expires=28800' in link: return link
            return valid_r2_links[0]

        # PRIORITY 2: PIXELSERVER (Deep Search Fallback)
        found_links = set()
        for match in re.finditer(r'pixeldrain\.(?:dev|com)/u/([a-zA-Z0-9]+)', html_text): found_links.add(match.group(1))
        for a in gen_soup.find_all('a'):
            for attr in ['data-url', 'data-link', 'data-href', 'data-src']:
                val = a.get(attr)
                if val:
                    for m in re.finditer(r'pixeldrain\.(?:dev|com)/u/([a-zA-Z0-9]+)', val): found_links.add(m.group(1))
        for match in re.finditer(r'[a-zA-Z0-9+/]{20,}={0,2}', html_text):
            b64_str = match.group(0)
            try:
                decoded = base64.b64decode(b64_str + "=" * ((4 - len(b64_str) % 4) % 4)).decode('utf-8')
                for m in re.finditer(r'pixeldrain\.(?:dev|com)/u/([a-zA-Z0-9]+)', decoded): found_links.add(m.group(1))
            except: pass
        for match in re.finditer(r"atob\(['\"]([^'\"]+)['\"]\)", html_text):
            b64_str = match.group(1)
            try:
                decoded = base64.b64decode(b64_str + "=" * ((4 - len(b64_str) % 4) % 4)).decode('utf-8')
                for m in re.finditer(r'pixeldrain\.(?:dev|com)/u/([a-zA-Z0-9]+)', decoded): found_links.add(m.group(1))
            except: pass

        real_ids = [id for id in found_links if id != 'negn6f']
        if real_ids:
            return convert_pixeldrain_to_direct(f"https://pixeldrain.dev/u/{real_ids[0]}")
        elif found_links:
            return convert_pixeldrain_to_direct(f"https://pixeldrain.dev/u/{list(found_links)[0]}")
        return None
    except Exception: return None

def parse_movie_tag(a_tag):
    title, href = a_tag.get_text(strip=True), a_tag.get('href')
    if not href or not title or len(title) < 10: return None
    if "(2026)" not in title and "(2025)" not in title: return None
    if not href.startswith('http'): href = BASE_URL + href
    if BASE_URL not in href: return None
    if '/category/' in href or '/page/' in href or 'how-to-download' in href: return None
    exclude = ["season", "ep-", "episode", "series", "s01", "s02", "how to download", "telegram", "play"]
    if any(kw in title.lower() for kw in exclude): return None
    return title, href

# ================= FLASK ROUTES =================

@app.route('/')
def home():
    # UI Design with Search Box and Saved Movies List
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>HDHub4u Scraper</title>
        <style>
            body { font-family: Arial; padding: 20px; background: #121212; color: #fff; }
            input[type="text"] { padding: 10px; width: 70%; max-width: 400px; border-radius: 5px; border: none; }
            button { padding: 10px 15px; background: #28a745; color: white; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; }
            .movie-card { background: #1e1e1e; padding: 15px; margin-top: 10px; border-radius: 5px; border-left: 4px solid #007bff; display: flex; justify-content: space-between; align-items: center; }
            .movie-title { font-size: 14px; font-weight: bold; }
            .btn-save { background: #007bff; }
            .btn-save:hover { background: #0056b3; }
            .saved-list { margin-top: 30px; border-top: 1px solid #333; padding-top: 20px; }
            .saved-item { color: #aaa; font-size: 14px; margin-bottom: 5px; }
            a { color: #28a745; text-decoration: none; }
        </style>
    </head>
    <body>
        <h2>🎬 HDHub4u Scraper</h2>
        <p>Main Playlist URL: <a href="/playlist.m3u">/playlist.m3u</a></p>
        
        <h3>🔍 Search Movie</h3>
        <form action="/search" method="get">
            <input type="text" name="q" placeholder="E.g. Param Sundari" required>
            <button type="submit">Search</button>
        </form>
        <br>
        
        <div class="saved-list">
            <h3>⭐ Saved Movies (3 Days Expiry)</h3>
    """
    
    cleanup_saved_movies()
    if not saved_movies:
        html += "<p style='color: #777;'>No movies saved yet. Search and click 'Save'.</p>"
    else:
        for k, v in saved_movies.items():
            time_left = int((CACHE_EXPIRY_3_DAYS - (time.time() - v['timestamp'])) / 3600)
            html += f"<div class='saved-item'>✅ {v['title']} <i>(Expires in {time_left}h)</i></div>"
            
    html += """
        </div>
    </body>
    </html>
    """
    return render_template_string(html)

@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    if not query: return redirect('/')
    
    # Space ki jagah + use karne ke liye quote_plus kiya
    search_url = f"{BASE_URL}/search.html?q={urllib.parse.quote_plus(query)}"
    print(f"[*] Searching: {search_url}")
    
    try:
        resp = session.get(search_url, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        seen = set()
        results_html = ""
        
        # Search page ke liye relaxed filter
        for a_tag in soup.find_all('a', href=True):
            title = a_tag.get_text(strip=True)
            href = a_tag.get('href')
            
            # Basic validation (Search me title chota ho sakta hai, isliye len > 5)
            if not href or not title or len(title) < 5: 
                continue
                
            if not href.startswith('http'): 
                href = BASE_URL + href
                
            if BASE_URL not in href: 
                continue
                
            # Category/Page/Home links ko ignore karo
            if '/category/' in href or '/page/' in href or 'search.html' in href or 'how-to-download' in href: 
                continue
                
            exclude = ["season", "ep-", "episode", "series", "s01", "s02", "how to download", "telegram", "play"]
            if any(kw in title.lower() for kw in exclude): 
                continue
            
            if href in seen: 
                continue
            seen.add(href)
            
            enc_url = base64.urlsafe_b64encode(href.encode()).decode()
            
            results_html += f"""
            <div class="movie-card">
                <div class="movie-title">{title}</div>
                <form action="/save" method="post" style="margin:0;">
                    <input type="hidden" name="title" value="{urllib.parse.quote(title)}">
                    <input type="hidden" name="url" value="{enc_url}">
                    <button type="submit" class="btn-save">Save</button>
                </form>
            </div>
            """
            
        # 🛑 DEBUG MAGIC: Agar list khali hai toh HTML show karo
        if not results_html:
            results_html = f"""
            <div style="background:#333; padding:15px; border-radius:5px;">
                <p>❌ No movies matched the filter.</p>
                <p><b>Status Code:</b> {resp.status_code}</p>
                <p><b>Debug HTML Preview (Screenshot bhej dena iska):</b></p>
                <textarea style="width:100%; height:200px; background:#000; color:#0f0; font-size:12px;">{resp.text[:3000]}</textarea>
            </div>
            """
            
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Search Results</title>
            <style>
                body {{ font-family: Arial; padding: 20px; background: #121212; color: #fff; }}
                .movie-card {{ background: #1e1e1e; padding: 15px; margin-top: 10px; border-radius: 5px; border-left: 4px solid #007bff; display: flex; justify-content: space-between; align-items: center; }}
                .movie-title {{ font-size: 14px; font-weight: bold; max-width: 70%; }}
                button {{ padding: 8px 15px; background: #28a745; color: white; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; }}
                a {{ color: #ddd; text-decoration: none; font-size: 14px; }}
            </style>
        </head>
        <body>
            <a href="/">⬅ Back to Home</a>
            <h3>Search Results for: "{query}"</h3>
            {results_html}
        </body>
        </html>
        """
        return render_template_string(html)
    except Exception as e:
        return f"Error: {e}"

@app.route('/save', methods=['POST'])
def save_movie():
    title = urllib.parse.unquote(request.form.get('title'))
    encoded_url = urllib.parse.unquote(request.form.get('url'))
    
    if title and encoded_url:
        # Save to global cache
        saved_movies[encoded_url] = {
            "title": title,
            "url": encoded_url,
            "timestamp": time.time()
        }
        # Clear main playlist cache so the new movie appears immediately
        main_cache["playlist"] = None
        main_cache["timestamp"] = 0
        
    return redirect('/')

@app.route('/playlist.m3u')
def get_playlist():
    global main_cache
    cleanup_saved_movies()
    
    # 1. Main Playlist (Cached for 1 hour)
    if main_cache["playlist"] and (time.time() - main_cache["timestamp"] < 3600):
        m3u = main_cache["playlist"]
    else:
        categories = [("bollywood-movies", "Bollywood Movies"), ("south-hindi-movies", "South Hindi Movies")]
        m3u = "#EXTM3U\n"
        seen_urls = set()
        
        for cat_url, cat_name in categories:
            try:
                url = f"{BASE_URL}/category/{cat_url}/page/1/"
                resp = session.get(url, timeout=15)
                soup = BeautifulSoup(resp.text, 'html.parser')
                for a_tag in soup.find_all('a', href=True):
                    res = parse_movie_tag(a_tag)
                    if not res: continue
                    title, href = res
                    if href in seen_urls: continue
                    seen_urls.add(href)
                    
                    encoded_url = base64.urlsafe_b64encode(href.encode()).decode()
                    m3u += f'#EXTINF:-1 group-title="{cat_name}",{title}\n'
                    m3u += f'https://{request.host}/play?url={encoded_url}\n'
            except Exception as e:
                print(f"Main Fetch Error: {e}")
                
        main_cache["playlist"] = m3u
        main_cache["timestamp"] = time.time()

    # 2. Append Saved Movies (Always fresh from memory)
    m3u += '\n#EXTINF:-1 group-title="⭐ Saved Movies",=========================\n'
    m3u += 'http://127.0.0.1/placeholder\n\n' # Separator
    
    if saved_movies:
        for k, v in saved_movies.items():
            time_left = int((CACHE_EXPIRY_3_DAYS - (time.time() - v['timestamp'])) / 3600)
            m3u += f'#EXTINF:-1 group-title="⭐ Saved Movies",{v["title"]} ({time_left}h left)\n'
            m3u += f'https://{request.host}/play?url={v["url"]}\n'
    else:
        m3u += '#EXTINF:-1 group-title="⭐ Saved Movies",(No saved movies)\nhttp://127.0.0.1/placeholder\n'

    return Response(m3u, mimetype='audio/x-mpegurl')

@app.route('/play')
def play_movie():
    encoded_url = request.args.get('url')
    if not encoded_url: return "No URL", 400
    try:
        movie_url = base64.urlsafe_b64decode(encoded_url.encode()).decode()
    except: return "Invalid URL", 400

    try:
        movie_resp = session.get(movie_url, timeout=15)
        movie_soup = BeautifulSoup(movie_resp.text, 'html.parser')
        best_link = get_best_link(movie_soup, movie_url)
        if not best_link: return "No 1080p link", 404

        hubcloud = extract_hubcloud_from_gadgets(best_link) if "greenmotors" in best_link or "gadgetsweb" in best_link else (extract_hubcloud_from_hubdrive(best_link) if "hubdrive.blog" in best_link else best_link)

        if hubcloud:
            final_link = process_hubcloud(hubcloud)
            if final_link: return redirect(final_link, code=302)
        return "Extraction Failed", 500
    except Exception as e: return f"Error: {e}", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
