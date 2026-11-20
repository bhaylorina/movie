import re
import time
import json
import base64
import urllib.parse
import datetime
from urllib.parse import urlparse
import requests as std_requests  # Search API ke liye normal requests
from bs4 import BeautifulSoup
from curl_cffi import requests
from flask import Flask, request, redirect, Response, render_template_string

app = Flask(__name__)

BASE_URL = "https://new15.hdhub4u.cl"

# === AAPKA ORIGINAL CURL_CFFI SESSION (No changes) ===
session = requests.Session(impersonate="chrome110")
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
})

cache_data = {"m3u": None, "timestamp": 0}
saved_movies = {}

# === AAPKE ORIGINAL EXTRACTION FUNCTIONS (No changes) ===
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
            
    if not candidates:
        return None

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
                    print(f"    [🔄] Detected HBLinks page, extracting HubCloud...")
                    hb_resp = session.get(hubcloud_url, timeout=15)
                    hb_soup = BeautifulSoup(hb_resp.text, 'html.parser')
                    for a in hb_soup.find_all('a'):
                        text = a.get_text(strip=True).lower()
                        href = str(a.get('href', '')).lower()
                        if 'hubcloud' in text or 'hubcloud' in href:
                            return a['href']
                    return None
                return hubcloud_url
    except Exception:
        pass
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
    except Exception:
        pass
    return None

def convert_pixeldrain_to_direct(url):
    url = url.replace('pixeldrain.com', 'pixeldrain.dev')
    if "pixeldrain.dev/u/" in url:
        file_id = url.split("/u/")[-1].split("?")[0]
        direct_url = f"https://pixeldrain.dev/api/file/{file_id}?download"
        return direct_url
    return url

def process_hubcloud(hubcloud_url):
    try:
        print(f"    [☁️] Processing HubCloud...")
        resp = session.get(hubcloud_url, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        gen_btn = soup.find('a', id='download')
        if not gen_btn:
            for a in soup.find_all('a'):
                text = a.get_text(strip=True).lower()
                if 'generate' in text and 'download' in text:
                    gen_btn = a
                    break
        
        if not gen_btn or not gen_btn.get('href'):
            return None
            
        gen_url = gen_btn['href']
        if not gen_url.startswith('http'):
            gen_url = urllib.parse.urljoin(hubcloud_url, gen_url)
            
        print(f"      [*] Download list page par jaa rahe hain...")
        gen_resp = session.get(gen_url, timeout=15)
        
        html_text = gen_resp.text.replace('&amp;', '&').replace('\\u0026', '&').replace('\u0026', '&')
        gen_soup = BeautifulSoup(html_text, 'html.parser')
        
        # PRIORITY 1: FSL SERVER (28800)
        for a in gen_soup.find_all('a'):
            text = a.get_text(strip=True).lower()
            href = a.get('href', '')
            if 'fsl server' in text and 'fslv2' not in text:
                if href and 'http' in href:
                    print(f"      [✅] FSL Server Link mila (Direct Button)!")
                    return href
        
        # Fallback Regex for FSL
        all_r2_links = re.findall(r'https?://[a-zA-Z0-9._-]+\.r2\.cloudflarestorage\.com/[^\s"\'<>]+', html_text)
        valid_r2_links = [l for l in all_r2_links if 't3apac' not in l.lower() and 'fslv2' not in l.lower()]
        
        if valid_r2_links:
            for link in valid_r2_links:
                if 'X-Amz-Expires=28800' in link:
                    print(f"      [✅] FSL Server Link mila (Regex 28800)!")
                    return link
            print(f"      [⚠️] FSL Server Link mila (Regex Fallback)!")
            return valid_r2_links[0]

        # PRIORITY 2: PIXELSERVER
        print(f"      [!] FSL nahi mila, PixelServer ke liye Deep Search shuru...")
        found_links = set()
        
        for match in re.finditer(r'pixeldrain\.(?:dev|com)/u/([a-zA-Z0-9]+)', html_text):
            found_links.add(match.group(1))
            
        for a in gen_soup.find_all('a'):
            for attr in ['data-url', 'data-link', 'data-href', 'data-src']:
                val = a.get(attr)
                if val:
                    for m in re.finditer(r'pixeldrain\.(?:dev|com)/u/([a-zA-Z0-9]+)', val):
                        found_links.add(m.group(1))
                        
        for match in re.finditer(r'[a-zA-Z0-9+/]{20,}={0,2}', html_text):
            b64_str = match.group(0)
            try:
                decoded = base64.b64decode(b64_str + "=" * ((4 - len(b64_str) % 4) % 4)).decode('utf-8')
                for m in re.finditer(r'pixeldrain\.(?:dev|com)/u/([a-zA-Z0-9]+)', decoded):
                    found_links.add(m.group(1))
            except: pass
            
        for match in re.finditer(r"atob\(['\"]([^'\"]+)['\"]\)", html_text):
            b64_str = match.group(1)
            try:
                decoded = base64.b64decode(b64_str + "=" * ((4 - len(b64_str) % 4) % 4)).decode('utf-8')
                for m in re.finditer(r'pixeldrain\.(?:dev|com)/u/([a-zA-Z0-9]+)', decoded):
                    found_links.add(m.group(1))
            except: pass
            
        parsed_url = urllib.parse.urlparse(gen_url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        for key, values in query_params.items():
            for val in values:
                try:
                    decoded = base64.b64decode(val + "=" * ((4 - len(val) % 4) % 4)).decode('utf-8')
                    for m in re.finditer(r'pixeldrain\.(?:dev|com)/u/([a-zA-Z0-9]+)', decoded):
                        found_links.add(m.group(1))
                except: pass
                for m in re.finditer(r'pixeldrain\.(?:dev|com)/u/([a-zA-Z0-9]+)', val):
                    found_links.add(m.group(1))

        real_ids = [id for id in found_links if id != 'negn6f']
        
        target_url = None
        if real_ids:
            target_url = f"https://pixeldrain.dev/u/{real_ids[0]}"
            print(f"      [✅] Real Pixel Link mila (Deep Search): {target_url}")
        elif found_links:
            target_url = f"https://pixeldrain.dev/u/{list(found_links)[0]}"
            print(f"      [⚠️] Sirf placeholder link mila: {target_url}")
        else:
            print("      [❌] Page par koi bhi asli PixelDrain link nahi mila.")
            return None

        if target_url:
            final_direct_pixel = convert_pixeldrain_to_direct(target_url)
            print(f"      [🔄] Pixel Link Direct API me convert ho gaya: {final_direct_pixel}")
            return final_direct_pixel

        return None
                
    except Exception as e:
        print(f"    [❌] HubCloud Error: {e}")
    return None


# ================= WEB UI & SEARCH =================

@app.route('/')
def home():
    saved_msg = ""
    if request.args.get('saved'):
        saved_msg = '<div style="color: #28a745; margin-bottom: 15px; font-weight: bold;">✅ Movie saved to playlist!</div>'
        
    saved_list = list(saved_movies.keys())
    saved_html = ""
    if saved_list:
        for t in saved_list:
            saved_html += f'<div class="saved-item"><span>{t}</span><a href="/remove/{urllib.parse.quote(t)}" class="remove-btn">[Remove]</a></div>'
    else:
        saved_html = '<p style="color:#aaa;font-size:14px;">No saved movies yet.</p>'

    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Movie Scraper</title>
        <style>
            body { font-family: Arial; padding: 20px; background: #121212; color: #fff; text-align: center; }
            input[type="text"] { padding: 10px; width: 80%; max-width: 300px; border-radius: 5px; border: none; }
            button { padding: 10px 20px; background: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; margin-top: 10px; font-weight: bold; }
            .link { margin-top: 30px; display: block; color: #28a745; font-weight: bold; text-decoration: none; }
            .saved-item { background: #1e1e1e; padding: 10px; margin-top: 5px; border-radius: 5px; display: flex; justify-content: space-between; align-items: center; font-size: 14px; max-width: 400px; margin-left: auto; margin-right: auto; }
            .remove-btn { color: #dc3545; text-decoration: none; font-weight: bold; }
        </style>
    </head>
    <body>
        <h1>🎬 Movie Scraper</h1>
        {{ saved_msg|safe }}
        <form action="/search" method="get">
            <input type="text" name="q" placeholder="Search for a movie..." required>
            <br>
            <button type="submit">Search</button>
        </form>
        
        <h3 style="margin-top:30px;">⭐ Saved Movies</h3>
        {{ saved_html|safe }}
        
        <a href="/playlist.m3u" class="link">📥 Download M3U Playlist</a>
        <p style="margin-top: 20px; font-size: 12px; color: #666;">Saved movies expire after 3 days.</p>
    </body>
    </html>
    ''', saved_msg=saved_msg, saved_html=saved_html)

@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    if not query:
        return redirect('/')
        
    results = []
    seen = set()
    
    # 100% API Search (Koi website scraping nahi)
    try:
        today = datetime.date.today().isoformat()
        api_url = "https://search.pingora.fyi/collections/post/documents/search"
        
        # Exact parameters jo site ke JS me use hote hain
        params = {
            'q': query,
            'query_by': 'post_title,category,stars,director,imdb_id',
            'query_by_weights': '4,2,2,2,4',
            'sort_by': 'sort_by_date:desc',
            'per_page': 50,
            'highlight_fields': 'none',
            'use_cache': 'true',
            'page': 1,
            'analytics_tag': today
        }
        
        # Cloudflare se bachne ke liye normal requests use kar rahe hain
        resp = std_requests.get(api_url, params=params, timeout=15)
        
        if resp.status_code == 200:
            data = resp.json()
            for hit in data.get('hits', []):
                doc = hit.get('document', {})
                title = doc.get('post_title', '')
                permalink = doc.get('permalink', '')
                
                if not title or not permalink: 
                    continue
                
                parsed = urlparse(permalink)
                path = parsed.path if parsed.scheme else permalink
                if not path.startswith('http'):
                    path = BASE_URL + path
                
                if path in seen: 
                    continue
                seen.add(path)
                results.append({'title': title, 'url': path})
    except Exception as e:
        print(f"API Search Error: {e}")

    # Generate Results HTML
    results_html = ""
    for movie in results:
        enc_url = base64.urlsafe_b64encode(movie['url'].encode()).decode()
        results_html += f"""
        <div class="movie-card">
            <div class="movie-title">{movie['title']}</div>
            <form action="/save" method="post" style="margin:0;">
                <input type="hidden" name="title" value="{urllib.parse.quote(movie['title'])}">
                <input type="hidden" name="url" value="{enc_url}">
                <button type="submit" class="btn-save">Save</button>
            </form>
        </div>
        """
        
    if not results_html:
        results_html = "<p>❌ No movies found.</p>"
        
    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Search Results</title>
        <style>
            body { font-family: Arial; padding: 20px; background: #121212; color: #fff; }
            .movie-card { background: #1e1e1e; padding: 15px; margin-top: 10px; border-radius: 5px; border-left: 4px solid #007bff; display: flex; justify-content: space-between; align-items: center; }
            .movie-title { font-size: 14px; font-weight: bold; max-width: 70%; }
            button { padding: 8px 15px; background: #28a745; color: white; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; }
            a { color: #ddd; text-decoration: none; font-size: 14px; }
        </style>
    </head>
    <body>
        <a href="/">⬅ Back to Home</a>
        <h3>Search Results for: "{{ query }}"</h3>
        {{ results_html|safe }}
    </body>
    </html>
    ''', query=query, results_html=results_html)

@app.route('/save', methods=['POST'])
def save():
    title = urllib.parse.unquote(request.form.get('title', ''))
    enc_url = request.form.get('url', '')
    if not title or not enc_url:
        return "Missing data", 400
    try:
        url = base64.urlsafe_b64decode(enc_url.encode()).decode()
    except:
        return "Invalid encoding", 400
    saved_movies[title] = {
        'title': title,
        'url': url,
        'saved_at': time.time()
    }
    return redirect('/?saved=1')

@app.route('/remove/<path:title>')
def remove(title):
    if title in saved_movies:
        del saved_movies[title]
    return redirect('/')

# ================= ORIGINAL PLAYLIST & PLAY (No changes) =================

@app.route('/playlist.m3u')
def get_playlist():
    global cache_data
    if cache_data["m3u"] and (time.time() - cache_data["timestamp"] < 0):
        base_m3u = cache_data["m3u"]
    else:
        categories = [
            ("bollywood-movies", "Bollywood Movies"),
            ("south-hindi-movies", "South Hindi Movies")
        ]
        
        base_m3u = "#EXTM3U\n"
        seen_urls = set() 
        total_movies = 0
        
        for cat_url, cat_name in categories:
            for page in range(1, 10):
                try:
                    url = f"{BASE_URL}/category/{cat_url}/page/{page}/"
                    resp = session.get(url, timeout=15)
                    
                    if resp.status_code != 200:
                        continue
                        
                    soup = BeautifulSoup(resp.text, 'html.parser')
                    
                    for a_tag in soup.find_all('a', href=True):
                        title = a_tag.get_text(strip=True)
                        href = a_tag['href']
                        
                        if not title or len(title) < 10: 
                            continue
                            
                        if "(2026)" not in title and "(2025)" not in title: 
                            continue
                            
                        if not href.startswith('http'):
                            href = BASE_URL + href
                        
                        if BASE_URL not in href:
                            continue
                            
                        if '/category/' in href or '/page/' in href or 'how-to-download' in href:
                            continue
                            
                        exclude = ["season", "ep-", "episode", "series", "s01", "s02", "how to download", "telegram", "play"]
                        if any(kw in title.lower() for kw in exclude): 
                            continue
                        
                        if href in seen_urls:
                            continue
                        
                        seen_urls.add(href)
                        total_movies += 1
                        
                        encoded_url = base64.urlsafe_b64encode(href.encode()).decode()
                        
                        base_m3u += f'#EXTINF:-1 group-title="{cat_name}",{title}\n'
                        base_m3u += f'https://{request.host}/play?url={encoded_url}\n'
                        
                except Exception as e:
                    print(f"[!] Error fetching {cat_name} page {page}: {e}")

        print(f"[+] Total movies added to playlist: {total_movies}")
        
        cache_data["m3u"] = base_m3u
        cache_data["timestamp"] = time.time()

    # Saved Movies ko append karna (Bina structure bigaade)
    final_m3u = base_m3u
    safe_saved = {}
    now = time.time()
    for title, data in list(saved_movies.items()):
        if now - data['saved_at'] > 3 * 24 * 60 * 60:
            del saved_movies[title]
        else:
            safe_saved[title] = data

    for title, data in safe_saved.items():
        encoded_url = base64.urlsafe_b64encode(data['url'].encode()).decode()
        final_m3u += f'#EXTINF:-1 group-title="⭐ Saved Movies",{title}\n'
        final_m3u += f'https://{request.host}/play?url={encoded_url}\n'

    return Response(final_m3u, mimetype='audio/x-mpegurl')

@app.route('/play')
def play_movie():
    encoded_url = request.args.get('url')
    if not encoded_url:
        return "No URL provided", 400

    try:
        movie_url = base64.urlsafe_b64decode(encoded_url.encode()).decode()
    except Exception:
        return "Invalid URL encoding", 400

    try:
        movie_resp = session.get(movie_url, timeout=15)
        movie_soup = BeautifulSoup(movie_resp.text, 'html.parser')
        
        best_link = get_best_link(movie_soup, movie_url)
        if not best_link:
            return "No 1080p link found for this movie", 404

        hubcloud = None
        if "greenmotors" in best_link or "gadgetsweb" in best_link:
            hubcloud = extract_hubcloud_from_gadgets(best_link)
        elif "hubdrive.blog" in best_link:
            hubcloud = extract_hubcloud_from_hubdrive(best_link)
        else:
            hubcloud = best_link

        if hubcloud:
            final_link = process_hubcloud(hubcloud)
            if final_link:
                return redirect(final_link, code=302)
                
        return "Failed to extract final HubCloud link.", 500
        
    except Exception as e:
        return f"Error during extraction: {str(e)}", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
