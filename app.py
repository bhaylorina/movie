import re
import time
import json
import base64
import urllib.parse
from bs4 import BeautifulSoup
from curl_cffi import requests
from flask import Flask, request, redirect, Response

app = Flask(__name__)

BASE_URL = "https://new15.hdhub4u.cl"

# curl_cffi session
session = requests.Session(impersonate="chrome110")
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
})

# Simple In-Memory Cache
cache_data = {"m3u": None, "timestamp": 0}

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

def process_hubcloud(hubcloud_url):
    try:
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
            
        gen_resp = session.get(gen_url, timeout=15)
        
        # \u0026 FIX for 400 Error
        html_text = gen_resp.text.replace('&amp;', '&').replace('\\u0026', '&').replace('\u0026', '&')
        gen_soup = BeautifulSoup(html_text, 'html.parser')
        
        # FSL SERVER (28800) LOGIC
        for a in gen_soup.find_all('a'):
            text = a.get_text(strip=True).lower()
            href = a.get('href', '')
            
            if 'fsl server' in text and 'fslv2' not in text:
                if href and 'http' in href:
                    return href
        
        # Fallback Regex
        all_r2_links = re.findall(r'https?://[a-zA-Z0-9._-]+\.r2\.cloudflarestorage\.com/[^\s"\'<>]+', html_text)
        valid_links = [l for l in all_r2_links if 't3apac' not in l.lower() and 'fslv2' not in l.lower()]
        
        if valid_links:
            for link in valid_links:
                if 'X-Amz-Expires=28800' in link:
                    return link
            return valid_links[0]
            
    except Exception:
        pass
    return None

# ================= FLASK ROUTES =================

@app.route('/')
def home():
    return "<h1>HDHub4u Scraper is Live!</h1><p>Use /playlist.m3u to load in your player.</p>"

@app.route('/playlist.m3u')
def get_playlist():
    global cache_data
    # 1 hour cache
    if cache_data["m3u"] and (time.time() - cache_data["timestamp"] < 0):
        return Response(cache_data["m3u"], mimetype='audio/x-mpegurl')

    categories = [
        ("bollywood-movies", "Bollywood Movies"),
        ("south-hindi-movies", "South Hindi Movies")
    ]
    
    m3u = "#EXTM3U\n"
    seen_urls = set() 
    total_movies = 0
    
    for cat_url, cat_name in categories:
        # Fetch first 2 pages of each category
        for page in range(1, 10):
            try:
                url = f"{BASE_URL}/category/{cat_url}/page/{page}/"
                print(f"[*] Fetching: {url}") # Debug log
                resp = session.get(url, timeout=15)
                
                if resp.status_code != 200:
                    print(f"[!] Failed to load {url}, Status: {resp.status_code}")
                    continue
                    
                soup = BeautifulSoup(resp.text, 'html.parser')
                
                # 🛑 FIX: Direct <a> tags par loop chalayenge
                for a_tag in soup.find_all('a', href=True):
                    title = a_tag.get_text(strip=True)
                    href = a_tag['href']
                    
                    # 1. Basic Filter (Title chota nahi hona chahiye, 2026 hona chahiye)
                    if not title or len(title) < 15: 
                        continue
                    if "(2026)" not in title: 
                        continue
                        
                    # 2. URL Filter (Sirf hdhub4u ki links leni hain)
                    if not href.startswith('http'):
                        href = BASE_URL + href
                    
                    if BASE_URL not in href:
                        continue
                        
                    # 3. Garbage Filter (Category page, pagination ya play button ko block karo)
                    if '/category/' in href or '/page/' in href or 'how-to-download' in href:
                        continue
                        
                    exclude = ["season", "ep-", "episode", "series", "s01", "s02", "how to download", "telegram", "play"]
                    if any(kw in title.lower() for kw in exclude): 
                        continue
                    
                    # 4. Duplicate Filter
                    if href in seen_urls:
                        continue
                    
                    seen_urls.add(href)
                    total_movies += 1
                    
                    encoded_url = base64.urlsafe_b64encode(href.encode()).decode()
                    
                    m3u += f'#EXTINF:-1 group-title="{cat_name}",{title}\n'
                    m3u += f'https://{request.host}/play?url={encoded_url}\n'
                    
            except Exception as e:
                print(f"[!] Error fetching {cat_name} page {page}: {e}")

    print(f"[+] Total movies added to playlist: {total_movies}")
    
    cache_data["m3u"] = m3u
    cache_data["timestamp"] = time.time()
    
    return Response(m3u, mimetype='audio/x-mpegurl')
@app.route('/play')
def play_movie():
    encoded_url = request.args.get('url')
    if not encoded_url:
        return "No URL provided", 400

    try:
        # Decode the URL
        movie_url = base64.urlsafe_b64decode(encoded_url.encode()).decode()
    except Exception:
        return "Invalid URL encoding", 400

    # Now run the real extraction
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
                # Redirect player to the final FSL link
                return redirect(final_link, code=302)
                
        return "Failed to extract final HubCloud link.", 500
        
    except Exception as e:
        return f"Error during extraction: {str(e)}", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
