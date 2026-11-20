import os
import re
import time
import base64
import urllib.parse
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, redirect, render_template_string

app = Flask(__name__)

BASE_URL = "https://new15.hdhub4u.cl"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

session = requests.Session()
session.headers.update(HEADERS)

# Caches
main_cache = {
    "data": [],
    "last_update": 0
}
CACHE_DURATION = 3600  # 1 hour

saved_movies = {}
SAVED_DURATION = 3 * 24 * 60 * 60  # 3 days

# Helper functions
def clean_url(url):
    if url.startswith('/'):
        url = BASE_URL + url
    return url

def extract_movie_data(movie_url):
    try:
        resp = session.get(movie_url, timeout=15)
        html_text = resp.text.replace('&amp;', '&').replace('\\u0026', '&').replace('\u0026', '&')
        
        fsl_link = None
        pixel_link = None
        
        # 1. Look for FSL links
        fsl_matches = re.findall(r'https://[^\s"\'<>]+r2\.cloudflarestorage\.com[^\s"\'<>]+', html_text)
        for fsl in fsl_matches:
            if 't3apac' in fsl or 'fslv2' in fsl or 'negn6f' in fsl:
                continue
            if 'X-Amz-Expires=28800' in fsl:
                fsl_link = fsl
                break
                
        # 2. Look for Pixel links
        pixel_matches = re.findall(r'https://pixeldrain\.(?:dev|com)/[^\s"\'<>]+', html_text)
        for pix in pixel_matches:
            if 'negn6f' in pix:
                continue
            pix_id = pix.rstrip('/').split('/')[-1].split('?')[0]
            if pix_id and pix_id not in ['api', 'file']:
                pixel_link = f"https://pixeldrain.dev/api/file/{pix_id}?download"
                break
                
        # 3. Deep search for Base64 encoded links
        if not fsl_link or not pixel_link:
            b64_matches = re.findall(r'[A-Za-z0-9+/=_-]{40,}', html_text)
            for b64 in b64_matches:
                try:
                    padded = b64 + '=' * (-len(b64) % 4)
                    for func in [base64.b64decode, base64.urlsafe_b64decode]:
                        try:
                            decoded = func(padded).decode('utf-8', errors='ignore')
                            if 'http' in decoded:
                                if 'r2.cloudflarestorage.com' in decoded and 'X-Amz-Expires=28800' in decoded:
                                    if 't3apac' not in decoded and 'fslv2' not in decoded and 'negn6f' not in decoded:
                                        match = re.search(r'https://[^\s"\'<>]+', decoded)
                                        if match: fsl_link = match.group(0)
                                elif 'pixeldrain' in decoded:
                                    match = re.search(r'https://pixeldrain\.[^\s"\'<>]+', decoded)
                                    if match:
                                        pix_url = match.group(0)
                                        pix_id = pix_url.rstrip('/').split('/')[-1].split('?')[0]
                                        if pix_id not in ['api', 'file']:
                                            pixel_link = f"https://pixeldrain.dev/api/file/{pix_id}?download"
                            if fsl_link and pixel_link:
                                break
                        except:
                            pass
                except:
                    pass
                
        return fsl_link, pixel_link
    except Exception:
        return None, None

def parse_movie_tag(a_tag):
    title = a_tag.get_text(strip=True)
    href = a_tag.get('href', '')
    if not href:
        return None
    href = clean_url(href)
    
    if href.rstrip('/') == BASE_URL.rstrip('/'):
        return None
        
    exclude_kw = ["season", "ep-", "episode", "series", "s01", "s02", "how to download", "telegram", "play", "home", "homepage", "back to", "go to"]
    if any(kw in title.lower() for kw in exclude_kw):
        return None
        
    if 'category' in href or 'search' in href or 'how-to-download' in href or '/play/' in href:
        return None
        
    return {
        'title': title,
        'url': href,
        'id': base64.urlsafe_b64encode(href.encode()).decode()
    }

@app.route('/')
def home():
    saved_msg = ""
    if request.args.get('saved'):
        saved_msg = '<div style="color: #28a745; margin-bottom: 15px; font-weight: bold;">✅ Movie saved to playlist!</div>'
        
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
        <a href="/playlist.m3u" class="link">📥 Download M3U Playlist</a>
    </body>
    </html>
    """
    return render_template_string(html)

@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    if not query:
        return redirect('/')
    
    search_url = f"{BASE_URL}/search.html?q={urllib.parse.quote_plus(query)}"
    try:
        resp = session.get(search_url, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        seen = set()
        results_html = ""
        
        exclude_kw = [
            "season", "ep-", "episode", "series", "s01", "s02", 
            "how to download", "telegram", "play", "home", "homepage", 
            "back to", "next", "previous", "join", "contact", "dmca", 
            "privacy", "about", "go to"
        ]
        
        for a_tag in soup.find_all('a', href=True):
            title = a_tag.get_text(strip=True)
            href = a_tag.get('href', '')
            
            if not href or not title or len(title) < 3: 
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
            
        if not results_html:
            results_html = f"""
            <div style="background:#333; padding:15px; border-radius:5px;">
                <p>❌ No movies matched the filter.</p>
                <p><b>Status Code:</b> {resp.status_code}</p>
                <p><b>Debug HTML Preview:</b></p>
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

@app.route('/playlist.m3u')
def playlist():
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
            
    now = time.time()
    if now - main_cache["last_update"] > CACHE_DURATION or not main_cache["data"]:
        movies = []
        seen = set()
        for cat in ['bollywood-movies', 'south-hindi-movies']:
            for page in range(1, 4):
                url = f"{BASE_URL}/category/{cat}/" if page == 1 else f"{BASE_URL}/category/{cat}/page/{page}/"
                try:
                    resp = session.get(url, timeout=15)
                    soup = BeautifulSoup(resp.text, 'html.parser')
                    for a in soup.find_all('a', href=True):
                        movie = parse_movie_tag(a)
                        if movie and movie['url'] not in seen:
                            if '2025' in movie['title'] or '2026' in movie['title']:
                                seen.add(movie['url'])
                                movies.append(movie)
                except:
                    pass
                
        main_cache["data"] = movies
        main_cache["last_update"] = now
        
    bolly = [m for m in main_cache["data"] if "bollywood" in m['url'].lower()]
    south = [m for m in main_cache["data"] if "south-hindi" in m['url'].lower()]
    
    add_category("Bollywood Movies", bolly)
    add_category("South Hindi Movies", south)
    
    saved_valid = {}
    now = time.time()
    for title, m_data in list(saved_movies.items()):
        if now - m_data['saved_at'] > SAVED_DURATION:
            del saved_movies[title]
        else:
            saved_valid[title] = m_data
            
    if saved_valid:
        m3u_lines.append('#EXTINF:-1 group-title="⭐ Saved Movies",⭐ Saved Movies')
        for title, m_data in saved_valid.items():
            enc_url = base64.urlsafe_b64encode(m_data['url'].encode()).decode()
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
        
    fsl, pixel = extract_movie_data(movie_url)
    
    if fsl:
        return redirect(fsl)
    elif pixel:
        return redirect(pixel)
    else:
        return "No direct links found for this movie.", 404

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
