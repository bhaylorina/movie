import os
import re
import time
import base64
import codecs
import html as html_lib
from datetime import date
from urllib.parse import urljoin, urlsplit, quote, unquote, urlencode

from flask import Flask, request, redirect
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
import requests as std_requests

app = Flask(__name__)

BASE_URL = "https://new15.hdhub4u.cl"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

SITE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
}

API_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
}

# Site scraping / playback ke liye curl_cffi
site_session = cffi_requests.Session(impersonate="chrome110")
site_session.headers.update(SITE_HEADERS)

# Search API ke liye normal requests
api_session = std_requests.Session()
api_session.headers.update(API_HEADERS)

# Site pages fallback ke liye normal requests
std_site_session = std_requests.Session()
std_site_session.headers.update(SITE_HEADERS)

main_cache = {
    "data": [],
    "last_update": 0
}
CACHE_DURATION = 0  # 1 hour

saved_movies = {}
SAVED_DURATION = 3 * 24 * 60 * 60  # 3 days


def trim_url(url):
    if not url:
        return ""
    url = url.strip().strip('"\'')
    return url.rstrip('.,);]}')


def clean_url(url):
    url = (url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE_URL + url
    return url


def normalize_html(text):
    if not text:
        return ""
    return (
        text.replace("&amp;", "&")
        .replace("\\u0026", "&")
        .replace("\u0026", "&")
        .replace("\\/", "/")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )


def rot13(text):
    return codecs.decode(text, "rot_13")


def decrypt_token(token):
    if not token:
        return ""
    token = unquote(str(token))
    padded = token + "=" * (-len(token) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            decoded = decoder(padded.encode("ascii")).decode("utf-8", errors="ignore")
            if decoded:
                return decoded
        except Exception:
            pass
    return token


def encode_token(url):
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def decode_token(token):
    if not token:
        return ""

    token = unquote(str(token))

    if token.startswith("http://") or token.startswith("https://"):
        return token

    padded = token + "=" * (-len(token) % 4)

    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        if decoded.startswith("http://") or decoded.startswith("https://"):
            return decoded
    except Exception:
        pass

    return token


def convert_pixeldrain_to_direct(url):
    if not url:
        return None

    url = normalize_html(url)
    url = trim_url(url)

    if "pixeldrain" not in url.lower():
        return None

    if "negn6f" in url.lower():
        return None

    if "/api/file/" in url:
        if "download" not in url:
            return url + ("&" if "?" in url else "?") + "download"
        return url

    parts = url.rstrip("/").split("/")
    pix_id = parts[-1].split("?")[0]

    if not pix_id or pix_id in ("api", "file", "u", "d"):
        return None

    return f"https://pixeldrain.dev/api/file/{pix_id}?download"


def extract_links(html_text):
    if not html_text:
        return None, None

    text = normalize_html(html_text)

    # 1. FSL 28800 preferred
    for match in re.findall(r'https://[^\s"\'<>]+', text):
        url = trim_url(match)
        low = url.lower()

        if "r2.cloudflarestorage.com" in low and "28800" in url:
            if "t3apac" not in low and "fslv2" not in low and "negn6f" not in low:
                return url, None

    # 2. PixelDrain fallback
    for match in re.findall(r'https://pixeldrain\.(?:dev|com)/[^\s"\'<>]+', text):
        pix = convert_pixeldrain_to_direct(match)
        if pix:
            return None, pix

    # 3. Base64 encoded links
    for b64 in re.findall(r'[A-Za-z0-9+/=_-]{40,}', text):
        padded = b64 + "=" * (-len(b64) % 4)

        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                decoded = decoder(padded).decode("utf-8", errors="ignore")
            except Exception:
                continue

            if not decoded:
                continue

            decoded = normalize_html(decoded)

            for match in re.findall(r'https://[^\s"\'<>]+', decoded):
                url = trim_url(match)
                low = url.lower()

                if "r2.cloudflarestorage.com" in low and "28800" in url:
                    if "t3apac" not in low and "fslv2" not in low and "negn6f" not in low:
                        return url, None

            for match in re.findall(r'https://pixeldrain\.(?:dev|com)/[^\s"\'<>]+', decoded):
                pix = convert_pixeldrain_to_direct(match)
                if pix:
                    return None, pix

    return None, None


def parse_movie_tag(a_tag):
    title = a_tag.get_text(" ", strip=True)
    if not title:
        title = (a_tag.get("title") or "").strip()

    href = (a_tag.get("href") or "").strip()

    if not href or not title or len(title) < 3:
        return None

    href = clean_url(href)
    low_title = re.sub(r"\s+", " ", title.lower()).strip()
    low_href = href.lower()

    garbage_exact = {
        "play",
        "home",
        "homepage",
        "go to homepage 🏠",
        "back to home",
        "next",
        "previous",
        "join telegram",
        "contact us",
        "dmca",
        "privacy policy",
        "about us",
        "menu",
    }

    if low_title in garbage_exact:
        return None

    garbage_contains = [
        "how to download",
        "telegram",
        "episode",
        "season",
        "s01",
        "s02",
        "series",
    ]

    if any(k in low_title for k in garbage_contains):
        return None

    bad_href = [
        "/category/",
        "/search",
        "how-to-download",
        "/play/",
        "/page/",
        "/tag/",
        "/author/",
        "wp-login",
        "#",
    ]

    if any(k in low_href for k in bad_href):
        return None

    if href.rstrip("/") == BASE_URL.rstrip("/"):
        return None

    if not href.startswith(BASE_URL):
        return None

    return {
        "title": title,
        "url": href,
        "id": encode_token(href),
    }


def fetch_latest_movies():
    now = time.time()

    if main_cache["data"] and (now - main_cache["last_update"] < CACHE_DURATION):
        return main_cache["data"]

    print("Refreshing movie cache...")

    movies = []
    seen = set()
    categories = ["bollywood-movies", "south-hindi-movies"]

    for cat in categories:
        for page in range(1, 10):
            if page == 1:
                url = f"{BASE_URL}/category/{cat}/"
            else:
                url = f"{BASE_URL}/category/{cat}/page/{page}/"

            try:
                resp = site_session.get(url, timeout=15)

                if resp.status_code == 404:
                    break

                if resp.status_code != 200:
                    print(f"Category fetch failed: {url} -> {resp.status_code}")
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")
                found_on_page = 0

                for a_tag in soup.find_all("a", href=True):
                    movie = parse_movie_tag(a_tag)
                    if not movie:
                        continue

                    if movie["url"] in seen:
                        continue

                    seen.add(movie["url"])
                    movie["category"] = cat
                    movies.append(movie)
                    found_on_page += 1

                print(f"{url} -> {found_on_page} movies")

            except Exception as e:
                print(f"Error fetching {url}: {e}")
                continue

    if movies:
        main_cache["data"] = movies
        main_cache["last_update"] = now
        print(f"Cache refreshed with {len(movies)} movies.")

    return main_cache["data"]


def is_garbage_title(title):
    low = re.sub(r"\s+", " ", (title or "").lower()).strip()

    if len(low) < 2:
        return True

    exact = {
        "play",
        "home",
        "homepage",
        "go to homepage 🏠",
        "back to home",
        "next",
        "previous",
        "contact us",
        "dmca",
        "privacy policy",
        "about us",
        "menu",
    }

    if low in exact:
        return True

    if "how to download" in low or "telegram" in low:
        return True

    return False


def fix_movie_url(url):
    """Kisi bhi hdhub4u mirror domain ko current BASE_URL par le aao."""
    if not url:
        return ""

    url = url.strip()
    base = urlsplit(BASE_URL)

    if url.startswith("//"):
        url = "https:" + url

    # Agar relative path hai (e.g. /movie-name/)
    if not url.startswith("http://") and not url.startswith("https://"):
        if url.startswith("/"):
            return f"{base.scheme}://{base.netloc}{url}"
        return f"{base.scheme}://{base.netloc}/{url}"

    try:
        parsed = urlsplit(url)
    except Exception:
        return url

    host = (parsed.netloc or "").lower()

    # Agar URL kisi bhi hdhub4u mirror ka hai, toh use BASE_URL se replace karo
    if "hdhub4u" in host or "hdhub" in host:
        path = parsed.path or "/"
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{base.scheme}://{base.netloc}{path}{query}"

    return url

def normalize_search_url(permalink):
    permalink = html_lib.unescape(str(permalink or "")).strip()

    if not permalink:
        return ""

    if permalink.startswith("//"):
        permalink = "https:" + permalink

    if not permalink.startswith("http://") and not permalink.startswith("https://"):
        permalink = BASE_URL + (permalink if permalink.startswith("/") else "/" + permalink)

    return fix_movie_url(permalink)


def parse_typesense_hits(hits):
    results = []
    seen = set()

    for hit in hits:
        doc = hit.get("document") or {}

        title = html_lib.unescape(str(doc.get("post_title") or "")).strip()
        permalink = doc.get("permalink") or ""

        if not title or not permalink:
            continue

        if is_garbage_title(title):
            continue

        url = normalize_search_url(permalink)

        if not url or url in seen:
            continue

        seen.add(url)

        results.append({
            "title": title,
            "url": url,
            "id": encode_token(url),
        })

    return results


def typesense_search(query):
    today = date.today().isoformat()
    api_url = "https://search.pingora.fyi/collections/post/documents/search"

    params = {
        "q": query,
        "query_by": "post_title,category,stars,director,imdb_id",
        "query_by_weights": "4,2,2,2,4",
        "sort_by": "_text_match:desc,sort_by_date:desc",
        "per_page": 50,
        "highlight_fields": "none",
        "use_cache": "true",
        "page": 1,
        "analytics_tag": today,
    }

    clients = [
        (api_session, "requests"),
        (site_session, "curl_cffi"),
    ]

    for client, name in clients:
        try:
            resp = client.get(api_url, params=params, timeout=12)
            print(f"Typesense {name}: {resp.status_code}")

            if resp.status_code != 200:
                print(resp.text[:200])
                continue

            data = resp.json()
            hits = data.get("hits", []) if isinstance(data, dict) else []

            print(f"Typesense {name} hits: {len(hits)}")

            if not hits:
                continue

            parsed = parse_typesense_hits(hits)

            if parsed:
                return parsed

        except Exception as e:
            print(f"Typesense {name} error: {e}")

    return []


def wp_search(query):
    wp_url = f"{BASE_URL}/wp-json/wp/v2/search"
    wp_params = {
        "search": query,
        "per_page": 50,
    }

    clients = [
        (api_session, "requests"),
        (site_session, "curl_cffi"),
    ]

    for client, name in clients:
        try:
            resp = client.get(wp_url, params=wp_params, timeout=12)
            print(f"WP {name}: {resp.status_code}")

            if resp.status_code != 200:
                continue

            data = resp.json()

            if not isinstance(data, list):
                continue

            results = []
            seen = set()

            for item in data:
                title = item.get("title", "")

                if isinstance(title, dict):
                    title = title.get("rendered", "")

                title = html_lib.unescape(str(title)).strip()
                url = item.get("url") or item.get("link") or ""
                url = normalize_search_url(url)

                if not title or not url:
                    continue

                if is_garbage_title(title):
                    continue

                if url in seen:
                    continue

                seen.add(url)

                results.append({
                    "title": title,
                    "url": url,
                    "id": encode_token(url),
                })

            print(f"WP {name} hits: {len(results)}")

            if results:
                return results

        except Exception as e:
            print(f"WP {name} error: {e}")

    return []


def local_search(query):
    q = query.lower()
    results = []
    seen = set()

    for movie in fetch_latest_movies():
        if q in movie["title"].lower() and movie["url"] not in seen:
            seen.add(movie["url"])
            results.append({
                "title": movie["title"],
                "url": movie["url"],
                "id": movie.get("id") or encode_token(movie["url"]),
            })

    return results


def merge_results(results, seen, new_results):
    for movie in new_results:
        url = movie.get("url") or ""

        if not url or url in seen:
            continue

        seen.add(url)

        if not movie.get("id"):
            movie["id"] = encode_token(url)

        results.append(movie)


def get_valid_saved():
    now = time.time()
    valid = {}

    for title, data in list(saved_movies.items()):
        if now - data.get("saved_at", 0) <= SAVED_DURATION:
            valid[title] = data
        else:
            del saved_movies[title]

    return valid


def fetch_page(url, timeout=15):
    last_exc = None

    for sess in (site_session, std_site_session):
        try:
            resp = sess.get(url, timeout=timeout, allow_redirects=True)

            if resp.status_code == 200 and resp.text:
                return resp.text

            last_exc = RuntimeError(f"HTTP {resp.status_code} for {url}")
            print(f"fetch_page {url} -> {resp.status_code}")

        except Exception as e:
            last_exc = e
            print(f"fetch_page error {url}: {e}")

    if last_exc:
        raise last_exc

    raise RuntimeError("fetch_page failed")


def post_form(url, data, timeout=15):
    for sess in (site_session, std_site_session):
        try:
            resp = sess.post(url, data=data, timeout=timeout, allow_redirects=True)

            if resp.status_code == 200 and resp.text:
                return resp.text

        except Exception as e:
            print(f"post_form error {url}: {e}")

    return ""


def candidate_links(html_text, base_url):
    if not html_text:
        return []

    text = normalize_html(html_text)
    found = {}
    soup = BeautifulSoup(text, "html.parser")

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()

        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue

        full = urljoin(base_url, href)
        full = trim_url(full)

        if not full.startswith("http"):
            continue

        low = full.lower()

        if any(x in low for x in ("t3apac", "fslv2", "negn6f")):
            continue

        label = (
            a.get_text(" ", strip=True)
            + " "
            + (a.get("title") or "")
        ).lower()

        score = 0

        if "1080p" in label:
            score += 30
        elif "720p" in label:
            score += 20
        elif "480p" in label:
            score += 10

        if "generate" in label and "download" in label:
            score += 35

        if "direct" in label and "download" in label:
            score += 25

        if "download" in label:
            score += 10

        if "hubcloud" in low:
            score += 30

        if "gadgetsweb" in low:
            score += 20

        if "hubdrive" in low:
            score += 20

        if "hubcloud" in label or "hubdrive" in label or "gadgets" in label:
            score += 10

        if score > 0:
            found[full] = max(found.get(full, 0), score)

    for match in re.findall(r'https?://[^\s"\'<>]+', text):
        full = trim_url(match)
        low = full.lower()

        if any(x in low for x in ("t3apac", "fslv2", "negn6f")):
            continue

        if any(k in low for k in ("hubcloud", "gadgetsweb", "hubdrive")):
            found[full] = max(found.get(full, 0), 15)

    for b64 in re.findall(r'[A-Za-z0-9+/=_-]{40,}', text):
        padded = b64 + "=" * (-len(b64) % 4)

        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                decoded = decoder(padded).decode("utf-8", errors="ignore")
            except Exception:
                continue

            low_dec = decoded.lower()

            if not any(k in low_dec for k in ("hubcloud", "gadgetsweb", "hubdrive")):
                continue

            for match in re.findall(r'https?://[^\s"\'<>]+', decoded):
                full = trim_url(match)
                if full.startswith("http"):
                    found[full] = max(found.get(full, 0), 25)

    return sorted(((score, url) for url, score in found.items()), key=lambda x: x[0], reverse=True)


def find_hubcloud_link(html_text, base_url):
    for _, link in candidate_links(html_text, base_url):
        low = link.lower()
        if "hubcloud" in low or "gadgetsweb" in low or "hubdrive" in low:
            return link

    return None


def extract_hubcloud_from_gadgets(url):
    try:
        resp = session.get(url, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for a in soup.find_all('a', href=True):
            if 'hubcloud' in a['href'].lower():
                return a['href']
        return None
    except:
        return None

def extract_hubcloud_from_hubdrive(url):
    try:
        resp = session.get(url, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for a in soup.find_all('a', href=True):
            h = a['href']
            if 'hubcloud' in h.lower():
                return h
        return None
    except:
        return None

def process_hubcloud(hubcloud_url):
    try:
        resp = session.get(hubcloud_url, timeout=15)
        html_text = resp.text.replace('&amp;', '&').replace('\\u0026', '&').replace('\u0026', '&')
        
        fsl, pixel = extract_links(html_text)
        if fsl or pixel:
            return fsl, pixel
            
        soup = BeautifulSoup(html_text, 'html.parser')
        generate_url = None
        for a in soup.find_all('a', href=True):
            text = a.get_text(strip=True).lower()
            if 'generate' in text and 'download' in text:
                generate_url = a['href']
                break
            elif 'direct' in text and 'download' in text:
                generate_url = a['href']
                break
            elif 'download' in text and 'link' in text:
                generate_url = a['href']
                break
        
        if generate_url:
            generate_url = urljoin(hubcloud_url, generate_url)
            resp2 = session.get(generate_url, timeout=15)
            html_text2 = resp2.text.replace('&amp;', '&').replace('\\u0026', '&').replace('\u0026', '&')
            fsl, pixel = extract_links(html_text2)
            if fsl or pixel:
                return fsl, pixel
                
        return None, None
    except:
        return None, None

def get_best_link(url):
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        download_links = []
        
        # 1. Link extraction from movie page (1080p/720p/480p)
        for a in soup.find_all('a', href=True):
            text = a.get_text(strip=True).lower()
            if '1080p' in text and ('download' in text or 'hubcloud' in text or 'gadgets' in text or 'drive' in text or 'greenmotors' in text):
                download_links.append(a['href'])
            elif '720p' in text and ('download' in text or 'hubcloud' in text or 'gadgets' in text or 'drive' in text or 'greenmotors' in text):
                download_links.append(a['href'])
            elif '480p' in text and ('download' in text or 'hubcloud' in text or 'gadgets' in text or 'drive' in text or 'greenmotors' in text):
                download_links.append(a['href'])
        
        if not download_links:
            for a in soup.find_all('a', href=True):
                href = a['href']
                if any(x in href.lower() for x in ['hubcloud', 'gadgets', 'drive', 'greenmotors']):
                    download_links.append(href)

        # 2. Process the found links directly
        if download_links:
            for dl in download_links:
                dl_low = dl.lower()
                if 'hubcloud' in dl_low:
                    fsl, pixel = process_hubcloud(dl)
                    if fsl or pixel: return fsl, pixel
                elif 'gadgets' in dl_low:
                    hc = extract_hubcloud_from_gadgets(dl)
                    if hc:
                        fsl, pixel = process_hubcloud(hc)
                        if fsl or pixel: return fsl, pixel
                elif 'hubdrive' in dl_low:
                    hc = extract_hubcloud_from_hubdrive(dl)
                    if hc:
                        fsl, pixel = process_hubcloud(hc)
                        if fsl or pixel: return fsl, pixel
                else:
                    # Handle greenmotors and generic pages
                    resp2 = session.get(dl, timeout=15)
                    html_text = resp2.text
                    
                    fsl, pixel = extract_links(html_text)
                    if fsl or pixel: return fsl, pixel
                    
                    soup2 = BeautifulSoup(html_text, 'html.parser')
                    hc = None
                    for a2 in soup2.find_all('a', href=True):
                        if 'hubcloud' in a2['href'].lower():
                            hc = a2['href']
                            break
                    
                    if hc:
                        fsl, pixel = process_hubcloud(hc)
                        if fsl or pixel: return fsl, pixel
                    
                    # Handle greenmotors CLICK TO CONTINUE form
                    form = soup2.find('form')
                    if form and form.get('action'):
                        action_url = urljoin(dl, form.get('action'))
                        data = {i.get('name'): i.get('value', '') for i in form.find_all('input') if i.get('name')}
                        resp3 = session.post(action_url, data=data, timeout=15)
                        
                        fsl, pixel = extract_links(resp3.text)
                        if fsl or pixel: return fsl, pixel
                        
                        soup3 = BeautifulSoup(resp3.text, 'html.parser')
                        for a3 in soup3.find_all('a', href=True):
                            if 'hubcloud' in a3['href'].lower():
                                fsl, pixel = process_hubcloud(a3['href'])
                                if fsl or pixel: return fsl, pixel

        # 3. Fallback check on movie page
        html_text = resp.text
        return extract_links(html_text)
    except Exception as e:
        print(f"Error in get_best_link: {e}")
        return None, None

@app.route("/play")
def play():
    raw = request.args.get("url", "").strip()

    if not raw:
        return "Missing URL", 400

    movie_url = fix_movie_url(decode_token(raw))

    if not movie_url.startswith("http://") and not movie_url.startswith("https://"):
        return "Invalid URL", 400

    try:
        fsl, pixel = get_best_link(movie_url)

        if fsl:
            return redirect(fsl)

        if pixel:
            return redirect(pixel)

        return "No valid FSL 28800 or PixelDrain link found.", 404

    except Exception as e:
        print(f"Play error for {movie_url}: {e}")
        return f"Playback link generate nahi hua: {type(e).__name__}", 404

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
