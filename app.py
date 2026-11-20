import os
import re
import time
import base64
import urllib.parse
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
    if href.rstrip('/') == BASE_URL.rstrip('/'):
        return None
    exclude_kw = [
        "season", "ep-", "episode", "series", "s01", "s02",
        "how to download", "telegram", "play", "home", "homepage",
        "back to", "go to", "next", "previous", "join", "contact",
        "dmca", "privacy", "about"
    ]
    if any(kw in title.lower() for kw in exclude_kw):
        return None
    if 'category' in href or 'search' in href or 'how-to-download' in href or '/play/' in href:
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
        for page in range(1, 3):  # first 2 pages
            url = f"{BASE_URL}/category/{cat}/" if page == 1 else f"{BASE_URL}/category/{cat}/page/{page}/"
            try:
                resp = session.get(url, timeout=12)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, 'html.parser')
                for a_tag in soup.find_all('a', href=True):
                    movie = parse_movie_tag(a_tag)
                    if not movie:
                        continue
                    if not ('2025' in movie['title'] or '2026' in movie['title']):
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
    else:
        print("No movies found, keeping old cache.")
    return main_cache["data"]

def extract_links(html_text):
    # Direct FSL
    for match in re.findall(r'https://[^\s"\'<>]+', html_text):
        if 'r2.cloudflarestorage.com' in match and 'X-Amz-Expires=28800' in match:
            if 't3apac' not in match and 'fslv2' not in match and 'negn6f' not in match:
                return match, None

    # Direct Pixel
    for match in re.findall(r'https://pixeldrain\.(?:dev|com)/[^\s"\'<>]+', html_text):
        if 'negn6f' in match:
            continue
        pix_id = match.rstrip('/').split('/')[-1].split('?')[0]
        if pix_id and pix_id not in ['api', 'file']:
            return None, f"https://pixeldrain.dev/api/file/{pix_id}?download"

    # Deep search: FSL
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
                except:
                    pass
        except:
            pass

    # Deep search: Pixel
    for b64 in b64_matches:
        try:
            padded = b64 + '=' * (-len(b64) % 4)
            for func in [base64.b64decode, base64.urlsafe_b64decode]:
                try:
                    decoded = func(padded).decode('utf-8', errors='ignore')
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
    movies = fetch_latest_movies()
    query_lower = query.lower()
    results = [m for m in movies if query_lower in m['title'].lower()]
    debug_html = ""

    if not results:
        # Fallback to site search
        search_url = f"{BASE_URL}/search.html?q={urllib.parse.quote_plus(query)}"
        try:
            resp = session.get(search_url, timeout=12)
            soup = BeautifulSoup(resp.text, 'html.parser')
            seen_urls = set()
            exclude_kw = [
                "season", "ep-", "episode", "series", "s01", "s02",
                "how to download", "telegram", "play", "home", "homepage",
                "back to", "go to", "next", "previous", "join", "contact",
                "dmca", "privacy", "about"
            ]
            for a_tag in soup.find_all('a', href=True):
                title = a_tag.get_text(strip=True)
                href = a_tag.get('href', '')
                if not href or not title or len(title) < 5:
                    continue
                if href.rstrip('/') == BASE_URL.rstrip('/'):
                    continue
                if not href.startswith('http'):
                    href = BASE_URL + href
                if BASE_URL not in href:
                    continue
                if '/category/' in href or '/page/' in href or 'search.html' in href or 'how-to-download' in href:
                    continue
                if any(kw in title.lower() for kw in exclude_kw):
                    continue
                if href in seen_urls:
                    continue
                seen_urls.add(href)
                results.append({
                    'title': title,
                    'url': href,
                    'id': base64.urlsafe_b64encode(href.encode()).decode()
                })
            if not results:
                debug_html = f"""
                <div style="background:#333; padding:15px; border-radius:5px; margin-top:15px; text-align:left;">
                    <p>❌ No movies found.</p>
                    <p><b>Local cache size:</b> {len(movies)}</p>
                    <p><b>Status Code:</b> {resp.status_code}</p>
                    <p><b>Debug HTML (first 1200 chars):</b></p>
                    <textarea style="width:100%; height:120px; background:#000; color:#0f0; font-size:11px;">{resp.text[:1200]}</textarea>
                </div>
                """
        except Exception as e:
            debug_html = f"<p style='color:red;'>Search fallback error: {e}</p>"

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
    if not results_html and not debug_html:
        debug_html = "<p>❌ No movies found for this search.</p>"

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
            enc_url = base64.urlsafe_b64encode(m['url'].encode()).decode()
            play_url = f"{request.host_url}play?url={enc_url}"
            m3u_lines.append(f'#EXTINF:-1,{m["title"]}')
            m3u_lines.append(play_url)
    bolly = [m for m in main_cache["data"] if m.get('category') == 'bollywood-movies']
    south = [m for m in main_cache["data"] if m.get('category') == 'south-hindi-movies']
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
            return "Could not find download page link or direct links.", 404

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
