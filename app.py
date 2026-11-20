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
        page = fetch_page(url)
    except Exception as e:
        print(f"Gadgets fetch error: {e}")
        return None

    for _, link in candidate_links(page, url):
        if "hubcloud" in link.lower():
            return link

    for _, link in candidate_links(page, url):
        if "hubdrive" in link.lower():
            hc = extract_hubcloud_from_hubdrive(link)
            if hc:
                return hc

    return None


def extract_hubcloud_from_hubdrive(url):
    try:
        page = fetch_page(url)
    except Exception as e:
        print(f"HubDrive fetch error: {e}")
        return None

    for _, link in candidate_links(page, url):
        if "hubcloud" in link.lower():
            return link

    return None


def process_hubcloud(hubcloud_url):
    if not hubcloud_url:
        return None, None

    try:
        page = fetch_page(hubcloud_url)
    except Exception as e:
        print(f"HubCloud fetch error: {e}")
        return None, None

    fsl, pixel = extract_links(page)

    if fsl or pixel:
        return fsl, pixel

    text = normalize_html(page)
    soup = BeautifulSoup(text, "html.parser")

    generate_urls = []
    seen_urls = set()

    def add_url(u, base=hubcloud_url):
        if not u:
            return

        full = urljoin(base, u.strip())
        full = trim_url(full)

        if not full.startswith("http"):
            return

        low = full.lower()

        if any(x in low for x in ("t3apac", "fslv2", "negn6f")):
            return

        if full not in seen_urls:
            seen_urls.add(full)
            generate_urls.append(full)

    for a in soup.find_all("a", href=True):
        label = (
            a.get_text(" ", strip=True)
            + " "
            + (a.get("title") or "")
        ).lower()

        href = a.get("href") or ""

        if (
            ("generate" in label and "download" in label)
            or ("direct" in label and "download" in label)
            or ("download" in label and "link" in label)
        ):
            add_url(href)

    for match in re.findall(r'href=["\']([^"\']+)["\']', text, re.I):
        low = match.lower()
        if "generate" in low or "download" in low or "drive" in low:
            add_url(match)

    for match in re.findall(
        r'(?:location\.href|window\.location|window\.open)\s*=\s*["\']([^"\']+)["\']',
        text,
        re.I,
    ):
        add_url(match)

    for match in re.findall(r'window\.open\s*\(\s*["\']([^"\']+)["\']', text, re.I):
        add_url(match)

    for match in re.findall(r'["\']([^"\']*(?:generate|download|drive)[^"\']*)["\']', text, re.I):
        if match.startswith("/") or match.startswith("http"):
            add_url(match)

    form_pages = []

    for form in soup.find_all("form"):
        action = form.get("action") or hubcloud_url
        action = urljoin(hubcloud_url, action)
        method = (form.get("method") or "get").lower()

        data = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if name:
                data[name] = inp.get("value", "")

        form_label = form.get_text(" ", strip=True).lower()
        action_low = action.lower()

        if (
            "generate" in form_label
            or "download" in form_label
            or "drive" in form_label
            or "generate" in action_low
            or "download" in action_low
            or "drive" in action_low
        ):
            if method == "post":
                form_pages.append(("post", action, data))
            else:
                form_pages.append(("get", action, data))

    for gen_url in generate_urls[:4]:
        try:
            page2 = fetch_page(gen_url)
            fsl, pixel = extract_links(page2)

            if fsl or pixel:
                return fsl, pixel

            for _, next_url in candidate_links(page2, gen_url)[:3]:
                try:
                    page3 = fetch_page(next_url)
                    fsl, pixel = extract_links(page3)

                    if fsl or pixel:
                        return fsl, pixel

                except Exception:
                    pass

        except Exception as e:
            print(f"HubCloud follow error: {e}")

    for method, action, data in form_pages[:3]:
        try:
            if method == "post":
                page2 = post_form(action, data)
            else:
                sep = "&" if "?" in action else "?"
                page2 = fetch_page(action + sep + urlencode(data))

            fsl, pixel = extract_links(page2)

            if fsl or pixel:
                return fsl, pixel

        except Exception as e:
            print(f"HubCloud form error: {e}")

    return None, None


def resolve_candidate(url, depth=0, deadline=None):
    if deadline is None:
        deadline = time.time() + 25

    if depth > 2 or time.time() > deadline:
        return None, None

    low = url.lower()

    try:
        if "hubcloud" in low:
            return process_hubcloud(url)

        if "gadgetsweb" in low:
            hc = extract_hubcloud_from_gadgets(url)
            return process_hubcloud(hc) if hc else (None, None)

        if "hubdrive" in low:
            hc = extract_hubcloud_from_hubdrive(url)
            return process_hubcloud(hc) if hc else (None, None)

        page = fetch_page(url)

        fsl, pixel = extract_links(page)

        if fsl or pixel:
            return fsl, pixel

        hc = find_hubcloud_link(page, url)

        if hc:
            fsl, pixel = resolve_candidate(hc, depth + 1, deadline)
            if fsl or pixel:
                return fsl, pixel

        for _, next_url in candidate_links(page, url)[:3]:
            if next_url == url:
                continue

            fsl, pixel = resolve_candidate(next_url, depth + 1, deadline)

            if fsl or pixel:
                return fsl, pixel

    except Exception as e:
        print(f"resolve_candidate error {url}: {e}")

    return None, None


def get_best_link(movie_url):
    page = fetch_page(movie_url)

    fsl, pixel = extract_links(page)

    if fsl or pixel:
        return fsl, pixel

    deadline = time.time() + 25

    for _, link in candidate_links(page, movie_url)[:8]:
        if link == movie_url:
            continue

        fsl, pixel = resolve_candidate(link, 0, deadline)

        if fsl or pixel:
            return fsl, pixel

    return None, None


@app.route("/")
def home():
    saved_msg = ""

    if request.args.get("saved"):
        saved_msg = '<div class="saved-msg">✅ Movie saved to playlist!</div>'

    saved_html = ""
    saved = get_valid_saved()

    if saved:
        for title in saved:
            title_esc = html_lib.escape(title)
            title_q = quote(title, safe="")
            saved_html += (
                f'<div class="saved-item">'
                f'<span>{title_esc}</span>'
                f'<a href="/remove/{title_q}" class="remove-btn">[Remove]</a>'
                f'</div>'
            )
    else:
        saved_html = '<p class="muted">No saved movies yet.</p>'

    html_page = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Movie Scraper</title>
        <style>
            body {{ font-family: Arial, sans-serif; padding: 20px; background: #121212; color: #fff; text-align: center; }}
            input[type="text"] {{ padding: 10px; width: 80%; max-width: 300px; border-radius: 5px; border: none; }}
            button {{ padding: 10px 20px; background: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; margin-top: 10px; font-weight: bold; }}
            .link {{ margin-top: 30px; display: block; color: #28a745; font-weight: bold; text-decoration: none; }}
            .saved-item {{ background: #1e1e1e; padding: 10px; margin-top: 5px; border-radius: 5px; display: flex; justify-content: space-between; align-items: center; font-size: 14px; }}
            .remove-btn {{ color: #dc3545; text-decoration: none; font-weight: bold; }}
            .saved-msg {{ color: #28a745; margin-bottom: 15px; font-weight: bold; }}
            .muted {{ color: #aaa; font-size: 14px; }}
        </style>
    </head>
    <body>
        <h1>🎬 Movie Scraper</h1>
        {saved_msg}
        <form action="/search" method="get">
            <input type="text" name="q" placeholder="Search for a movie..." required>
            <button type="submit">Search</button>
        </form>
        <h3 style="margin-top:30px;">⭐ Saved Movies</h3>
        {saved_html}
        <a href="/playlist.m3u" class="link">📥 Download M3U Playlist</a>
        <p style="margin-top: 20px; font-size: 12px; color: #666;">Saved movies expire after 3 days.</p>
    </body>
    </html>
    """

    return html_page


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()

    if not query:
        return redirect("/")

    results = []
    seen = set()

    # 1. Typesense API
    merge_results(results, seen, typesense_search(query))

    # 2. WordPress native search fallback
    if len(results) < 3:
        merge_results(results, seen, wp_search(query))

    # 3. Local cache last fallback
    if not results:
        merge_results(results, seen, local_search(query))

    results_html = ""

    for movie in results:
        title = html_lib.escape(movie["title"])
        token = movie.get("id") or encode_token(movie["url"])
        title_q = quote(movie["title"], safe="")

        results_html += f'''
        <div class="movie-card">
            <div class="movie-title">{title}</div>
            <form action="/save" method="post" style="margin:0;">
                <input type="hidden" name="title" value="{title_q}">
                <input type="hidden" name="url" value="{token}">
                <button type="submit" class="btn-save">Save</button>
            </form>
        </div>
        '''

    debug_html = ""

    if not results:
        debug_html = """
        <div class="debug">
            <p>❌ No movies found.</p>
            <p class="muted">Site database me ye movie nahi mili. Spelling check karo ya dusra keyword try karo.</p>
        </div>
        """

    query_esc = html_lib.escape(query)

    html_page = f"""
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
            .debug {{ background: #333; padding: 15px; border-radius: 5px; margin-top: 15px; text-align: left; }}
            .muted {{ font-size: 12px; color: #aaa; }}
        </style>
    </head>
    <body>
        <a href="/">⬅ Back to Home</a>
        <h3>Search Results for: "{query_esc}"</h3>
        {results_html}
        {debug_html}
    </body>
    </html>
    """

    return html_page


@app.route("/save", methods=["POST"])
def save():
    title = unquote(request.form.get("title", "")).strip()
    enc_url = request.form.get("url", "").strip()

    if not title or not enc_url:
        return "Missing data", 400

    url = fix_movie_url(decode_token(enc_url))

    if not url.startswith("http://") and not url.startswith("https://"):
        return "Invalid URL", 400

    saved_movies[title] = {
        "title": title,
        "url": url,
        "saved_at": time.time(),
    }

    return redirect("/?saved=1")

@app.route("/remove/<path:title>")
def remove(title):
    title = unquote(title)
    saved_movies.pop(title, None)
    return redirect("/")


def m3u_text(value):
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()


@app.route("/playlist.m3u")
def playlist():
    movies = fetch_latest_movies()

    m3u_lines = ["#EXTM3U"]
    added_urls = set()

    def add_category(display_name, category_key):
        for movie in movies:
            if movie.get("category") != category_key:
                continue

            title = m3u_text(movie.get("title", ""))
            url = movie.get("url", "").strip()

            if not title or not url:
                continue

            # Sirf 2025 / 2026 movies
            if not re.search(r"\((2025|2026)\)", title):
                continue

            if url in added_urls:
                continue

            added_urls.add(url)
            enc = quote(url, safe="")
            play_url = f"{request.url_root.rstrip('/')}/play?url={enc}"

            m3u_lines.append(f'#EXTINF:-1 group-title="{display_name}",{title}')
            m3u_lines.append(play_url)

    # Folder header channel nahi banega
    add_category("Bollywood Movies", "bollywood-movies")
    add_category("South Hindi Movies", "south-hindi-movies")

    # Saved movies
    for title, data in get_valid_saved().items():
        url = (data.get("url") or "").strip()

        if not url or url in added_urls:
            continue

        added_urls.add(url)
        enc = quote(url, safe="")
        play_url = f"{request.url_root.rstrip('/')}/play?url={enc}"

        m3u_lines.append(f'#EXTINF:-1 group-title="⭐ Saved Movies",{m3u_text(title)}')
        m3u_lines.append(play_url)

    return "\n".join(m3u_lines), 200, {"Content-Type": "audio/x-mpegurl"}


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

        if "Name or service not known" in str(e) or "Max retries" in str(e):
            return "Movie page ka domain resolve nahi hua (mirror dead). Dobara try karo.", 404

        return f"Playback link generate nahi hua: {type(e).__name__}", 404

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
