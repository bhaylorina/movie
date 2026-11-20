import os
import re
import time
import base64
import urllib.parse
import datetime
from urllib.parse import urlparse
from flask import Flask, request, redirect, render_template_string
from bs4 import BeautifulSoup
import requests

app = Flask(__name__)

BASE_URL = "https://new15.hdhub4u.cl"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

session = requests.Session()
session.headers.update(HEADERS)

# Cache
main_cache = {
    "data": [],
    "last_update": 0
}
CACHE_DURATION = 3600  # 1 hour

saved_movies = {}
SAVED_DURATION = 3 * 24 * 60 * 60  # 3 days

def clean_url(url):
    if url.startswith('/'):
        return BASE_URL + url
    return url

def parse_movie_tag(a_tag):
    title = a_tag.get_text(strip=True)
    href = a_tag.get('href', '')
    if not href or not title:
        return None
    href = clean_url(href)
    if href.rstrip('/') == BASE_URL.rstrip('/') or len(title) < 3:
        return None
        
    exclude_kw = [
        "season", "ep-", "episode", "series", "s01", "s02",
        "how to download", "telegram", "play", "home", "homepage",
        "back to", "go to", "next", "previous", "join", "contact",
        "dmca", "privacy", "about", "menu"
    ]
    if any(kw in title.lower() for kw in exclude_kw):
        return None
    if 'category' in href or 'search' in href or 'how-to-download' in href or '/play/' in href or '/page/' in href:
        return None
        
    return {
        'title': title,
        'url': href,
        'id': base64.urlsafe_b64encode(href.encode()).decode()
    }

def fetch_latest_movies():
    now = time.time()
    if main_cache["data"] and (now - main_cache["last_update"] < CACHE_DURATION):
        return main_cache["data"]

    print("Refreshing movie cache...")
    movies = []
    seen = set()
    categories = ['bollywood-movies', 'south-hindi-movies']
    
    for cat in categories:
        for page in range(1, 4):  # 3 pages tak fetch karenge
            url = f"{BASE_URL}/category/{cat}/" if page == 1 else f"{BASE_URL}/category/{cat}/page/{page}/"
            try:
                resp = session.get(url, timeout=15)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, 'html.parser')
                for a_tag in soup.find_all('a', href=True):
                    movie = parse_movie_tag(a_tag)
                    if not movie:
                        continue
                    if movie['url'] in seen:
                        continue
                    seen.add(movie['url'])
                    movie['category'] = cat
                    movies.append(movie)
            except Exception as e:
                print(f"Error fetching {url}: {e}")
                continue

    if movies:
        main_cache["data"] = movies
        main_cache["last_update"] = now
        print(f"Cache refreshed with {len(movies)} movies.")
    return main_cache["data"]

def extract_links(html_text):
    for match in re.findall(r'https://[^\s"\'<>]+', html_text):
        if 'r2.cloudflarestorage.com' in match and 'X-Amz-Expires=28800' in match:
            if 't3apac' not in match and 'fslv2' not in match and 'negn6f' not in match:
                return match, None

    for match in re.findall(r'https://pixeldrain\.(?:dev|com)/[^\s"\'<>]+', html_text):
        if 'negn6f' in match:
            continue
        pix_id = match.rstrip('/').split('/')[-1].split('?')[0]
        if pix_id and pix_id not in ['api', 'file']:
            return None, f"https://pixeldrain.dev/api/file/{pix_id}?download"

    b64_matches = re.findall(r'[A-Za-z0-9+/=_-]{40,}', html_text)
    for b64 in b64_matches:
        try:
            padded = b64 + '=' * (-len(b64) % 4)
            for func in [base64.b64decode, base64.urlsafe_b64decode]:
                try:
                    decoded = func(padded).decode('utf-8', errors='ignore')
                    if 'r2.cloudflarestorage.com' in decoded and 'X-Amz-Expires=28800' in decoded:
                        if 't3apac' not in decoded and 'fslv2' not in decoded and 'negn6f' not in decoded:
                            match = re.search(r'https://[^\s"\'<>]+', decoded)
                            if match:
                                return match.group(0), None
                    if 'pixeldrain' in decoded:
                        match = re.search(r'https://pixeldrain\.[^\s"\'<>]+', decoded)
                        if match:
                            pix_url = match.group(0)
                            pix_id = pix_url.rstrip('/').split('/')[-1].split('?')[0]
                            if pix_id not in ['api', 'file']:
                                return None, f"https://pixeldrain.dev/api/file/{pix_id}?download"
                except:
                    pass
        except:
            pass
    return None, None

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

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Movie Scraper</title>
        <style>
            body {{ font-family: Arial; padding: 20px; background: #121212; color: #fff; text-align: center; }}
            input[type="text"] {{ padding: 10px; width: 80%; max-width: 300px; border-radius: 5px; border: none; }}
            button {{ padding: 10px 20px; background: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; margin-top: 10px; font-weight: bold; }}
            .link {{ margin-top: 30px; display: block; color: #28a745; font-weight: bold; text-decoration: none; }}
            .saved-item {{ background: #1e1e1e; padding: 10px; margin-top: 5px; border-radius: 5px; display: flex; justify-content: space-between; align-items: center; font-size: 14px; }}
            .remove-btn {{ color: #dc3545; text-decoration: none; font-weight: bold; }}
        </style>
    </head>
    <body>
        <h1>🎬 Movie Scraper</h1>
        {saved_msg}
        <form action="/search" method="get">
            <input type="text" name="q" placeholder="Search for a movie..." required>
            <br>
            <button type="submit">Search</button>
        </form>
        <h3 style="margin-top:30px;">⭐ Saved Movies</h3>
        {saved_html}
        <a href="/playlist.m3u" class="link">📥 Download M3U Playlist</a>
        <p style="margin-top: 20px; font-size: 12px; color: #666;">Saved movies expire after 3 days.</p>
    </body>
    </html>
    """
    return render_template_string(html)

@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    if not query:
        return redirect('/')
        
    results = []
    seen = set()
    
    # Website ka asli API endpoint (Typesense)
    today = datetime.date.today().isoformat()
    api_url = "https://search.pingora.fyi/collections/post/documents/search"
    params = {
        'q': query,
        'query_by': 'post_title,category,stars,director,imdb_id',
        'query_by_weights': '4,2,2,2,4',
        'sort_by': 'sort_by_date:desc',
        'limit': 30,
        'highlight_fields': 'none',
        'use_cache': 'true',
        'page': 1,
        'analytics_tag': today
    }
    
    try:
        resp = session.get(api_url, params=params, timeout=15)
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
                
                results.append({
                    'title': title,
                    'url': path,
                    'id': base64.urlsafe_b64encode(path.encode()).decode()
                })
    except Exception as e:
        print(f"API Error: {e}")
        
    # Fallback (local cache)
    if not results:
        movies = fetch_latest_movies()
        query_lower = query.lower()
        for m in movies:
            if query_lower in m['title'].lower():
                if m['url'] not in seen:
                    seen.add(m['url'])
                    results.append(m)

    results_html = ""
    for movie in results:
        enc_url = movie['id']
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
        debug_html = f"""
        <div style="background:#333; padding:15px; border-radius:5px; margin-top:15px; text-align:left;">
            <p>❌ Yeh movie nahi mili.</p>
            <p style="font-size:12px; color:#aaa;">Try searching a different keyword. Make sure spelling is correct.</p>
        </div>
        """
    else:
        debug_html = ""

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
        {debug_html}
    </body>
    </html>
    """
    return render_template_string(html)

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

@app.route('/playlist.m3u')
def playlist():
    fetch_latest_movies()
    m3u_lines = ["#EXTM3U"]
    
    def add_category(name, movies):
        if not movies:
            return
        m3u_lines.append(f'#EXTINF:-1 group-title="{name}",{name}')
        for m in movies:
            enc_url = m['id']
            play_url = f"{request.host_url}play?url={enc_url}"
            m3u_lines.append(f'#EXTINF:-1,{m["title"]}')
            m3u_lines.append(play_url)
            
    # 2025 aur 2026 wali movies filter
    bolly = [m for m in main_cache["data"] if m.get('category') == 'bollywood-movies' and ('2025' in m['title'] or '2026' in m['title'])]
    south = [m for m in main_cache["data"] if m.get('category') == 'south-hindi-movies' and ('2025' in m['title'] or '2026' in m['title'])]
    
    add_category("Bollywood Movies", bolly)
    add_category("South Hindi Movies", south)

    now = time.time()
    safe_saved = {}
    for title, data in list(saved_movies.items()):
        if now - data['saved_at'] > SAVED_DURATION:
            del saved_movies[title]
        else:
            safe_saved[title] = data
            
    if safe_saved:
        m3u_lines.append('#EXTINF:-1 group-title="⭐ Saved Movies",⭐ Saved Movies')
        for title, data in safe_saved.items():
            enc_url = base64.urlsafe_b64encode(data['url'].encode()).decode()
            play_url = f"{request.host_url}play?url={enc_url}"
            m3u_lines.append(f'#EXTINF:-1,{title}')
            m3u_lines.append(play_url)
            
    return "\n".join(m3u_lines), 200, {'Content-Type': 'audio/x-mpegurl'}

@app.route('/play')
def play():
    enc_url = request.args.get('url')
    if not enc_url:
        return "Missing URL", 400
    try:
        movie_url = base64.urlsafe_b64decode(enc_url.encode()).decode()
    except:
        return "Invalid URL", 400

    try:
        resp = session.get(movie_url, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        download_page_url = None
        
        for a_tag in soup.find_all('a', href=True):
            text = a_tag.get_text(strip=True).lower()
            if 'generate' in text and 'download' in text:
                download_page_url = a_tag['href']
                break
            if 'download' in text and 'link' in text:
                download_page_url = a_tag['href']
                break
                
        if not download_page_url:
            html_text = resp.text.replace('&amp;', '&').replace('\\u0026', '&').replace('\u0026', '&')
            fsl, pixel = extract_links(html_text)
            if fsl:
                return redirect(fsl)
            elif pixel:
                return redirect(pixel)
            return "Movie page par direct link nahi mila.", 404

        if not download_page_url.startswith('http'):
            download_page_url = BASE_URL + download_page_url
            
        resp2 = session.get(download_page_url, timeout=15)
        html_text = resp2.text.replace('&amp;', '&').replace('\\u0026', '&').replace('\u0026', '&')
        
        fsl, pixel = extract_links(html_text)
        if fsl:
            return redirect(fsl)
        elif pixel:
            return redirect(pixel)
        else:
            return "No valid FSL or PixelDrain links found on download page.", 404

    except Exception as e:
        return f"Error: {e}", 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
