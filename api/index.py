import json
import logging
import requests
import time
from urllib.robotparser import RobotFileParser
from flask import Flask, request, render_template, jsonify
from flask_cors import CORS
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin, urldefrag
from concurrent.futures import ThreadPoolExecutor

from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__, template_folder='../templates')
app.wsgi_app = ProxyFix(app.wsgi_app)
CORS(app)
logging.basicConfig(level=logging.INFO)

crawled_pages = {}
robots_cache = {}

def is_valid_url(url):
    try:
        parsed = urlparse(url)
        return all([parsed.scheme, parsed.netloc])
    except:
        return False

def get_base_url(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"

def is_same_domain(url, base_url):
    return urlparse(url).netloc == urlparse(base_url).netloc

def normalize_url(url):
    url = url.strip()
    url, _ = urldefrag(url)
    if url.endswith('/'):
        url = url[:-1]
    return url

def is_allowed_by_robots(url):
    base_url = get_base_url(url)

    if base_url not in robots_cache:
        rp = RobotFileParser()
        robots_url = urljoin(base_url, "/robots.txt")
        try:
            response = requests.get(robots_url, timeout=5)
            if response.status_code == 200:
                rp.parse(response.text.splitlines())
        except:
            pass

        crawl_delay = getattr(rp, 'crawl_delay', lambda x: None)('*') or 1
        robots_cache[base_url] = {
            'parser': rp,
            'crawl_delay': min(crawl_delay, 5)
        }

    return robots_cache[base_url]['parser'].can_fetch("*", url)

def should_process_url(url):
    skip_extensions = [
        '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.svg', '.webp',
        '.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx',
        '.zip', '.rar', '.gz', '.tar', '.7z',
        '.mp3', '.mp4', '.wav', '.avi', '.mov', '.mkv',
        '.js', '.css', '.json', '.xml', '.csv',
        '.ico', '.ttf', '.woff', '.eot'
    ]

    parsed = urlparse(url)
    path = parsed.path.lower()

    if any(path.endswith(ext) for ext in skip_extensions):
        return False

    if any(param in parsed.query for param in ['logout', 'signout', 'delete']):
        return False

    skip_paths = ['/admin', '/login', '/signin', '/wp-admin', '/logout', '/cart', '/checkout']
    if any(path.startswith(skip_path) for skip_path in skip_paths):
        return False

    return True

def extract_links(html, base_url):
    soup = BeautifulSoup(html, 'html.parser')
    links = set()

    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href']
        if not href or href.startswith(('javascript:', '#', 'mailto:', 'tel:')):
            continue
        absolute_url = urljoin(base_url, href)
        absolute_url = normalize_url(absolute_url)
        if not is_valid_url(absolute_url) or not should_process_url(absolute_url):
            continue
        links.add(absolute_url)

    return list(links)

def extract_meta_tags(url):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5'
        }

        response = requests.get(url, headers=headers, timeout=10, allow_redirects=True)

        if response.url != url:
            url = response.url

        response.raise_for_status()

        content_type = response.headers.get('Content-Type', '')
        if 'text/html' not in content_type.lower() and 'application/xhtml+xml' not in content_type.lower():
            return {"success": False, "url": url, "error": "Not an HTML page"}

        encoding = response.encoding

        try:
            html_content = response.text
            soup = BeautifulSoup(html_content, 'html.parser')
            title = soup.title.string if soup.title else "No Title"
            meta_tags = soup.find_all('meta')
            meta_data = []
            for tag in meta_tags:
                tag_dict = {}
                for attr, value in tag.attrs.items():
                    tag_dict[attr] = str(value)
                meta_data.append(tag_dict)
            links = extract_links(html_content, url)

            return {
                "success": True,
                "url": url,
                "title": title,
                "meta_tags": meta_data,
                "links": links
            }
        except Exception as e:
            return {"success": False, "url": url, "error": f"Error parsing HTML: {str(e)}"}

    except requests.exceptions.RequestException as e:
        return {"success": False, "url": url, "error": f"Error fetching URL: {str(e)}"}
    except Exception as e:
        return {"success": False, "url": url, "error": f"An error occurred: {str(e)}"}

def parallel_crawl_website(start_url, max_pages=10, max_depth=2, max_workers=5):
    global crawled_pages
    crawled_pages = {}

    start_url = normalize_url(start_url)
    base_url = get_base_url(start_url)

    urls_by_depth = {0: [start_url]}
    processed_urls = set()

    logging.info(f"Starting crawl at {start_url}")
    result = extract_meta_tags(start_url)

    if result["success"]:
        crawled_pages[start_url] = {
            "title": result["title"],
            "meta_tags": result["meta_tags"]
        }
        processed_urls.add(start_url)

        if 1 not in urls_by_depth:
            urls_by_depth[1] = []

        for link in result["links"]:
            if (link not in processed_urls and is_same_domain(link, base_url)
                    and is_allowed_by_robots(link)):
                urls_by_depth[1].append(link)
    else:
        logging.error(f"Failed to crawl start URL: {result.get('error', 'Unknown error')}")
        return crawled_pages

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for depth in range(1, max_depth + 1):
            if depth not in urls_by_depth or not urls_by_depth[depth]:
                continue

            if len(crawled_pages) >= max_pages:
                break

            logging.info(f"Crawling at depth {depth}, URLs to process: {len(urls_by_depth[depth])}")
            urls_to_process = urls_by_depth[depth][:max_pages - len(crawled_pages)]

            future_to_url = {
                executor.submit(extract_meta_tags, url): url
                for url in urls_to_process
                if url not in processed_urls
            }

            for future in future_to_url:
                if len(crawled_pages) >= max_pages:
                    break

                url = future_to_url[future]
                processed_urls.add(url)

                try:
                    result = future.result()
                    if result["success"]:
                        crawled_pages[url] = {
                            "title": result["title"],
                            "meta_tags": result["meta_tags"]
                        }

                        if depth + 1 <= max_depth:
                            if depth + 1 not in urls_by_depth:
                                urls_by_depth[depth + 1] = []

                            for link in result["links"]:
                                if (link not in processed_urls and is_same_domain(link, base_url)
                                        and is_allowed_by_robots(link)):
                                    urls_by_depth[depth + 1].append(link)

                        robot_info = robots_cache.get(base_url, {'crawl_delay': 1})
                        time.sleep(robot_info['crawl_delay'])
                except Exception as e:
                    logging.error(f"Error processing {url}: {str(e)}")

    return crawled_pages

def simplify_meta_tags(meta_tags):
    simplified = {}
    for tag in meta_tags:
        name = tag.get('name') or tag.get('property') or tag.get('http-equiv')
        content = tag.get('content')
        if name and content:
            simplified[name] = content
    return simplified

@app.route('/', methods=['GET', 'POST'])
def index():
    result = None
    url = None
    crawl_status = None
    crawled_data = {}

    if request.method == 'POST':
        url = request.form.get('url')
        max_pages = int(request.form.get('max_pages', 10))
        max_depth = int(request.form.get('max_depth', 2))

        if url and is_valid_url(url):
            try:
                crawl_status = "Crawling website... This may take a few minutes."
                crawled_data = parallel_crawl_website(url, max_pages, max_depth)
                result = json.dumps(crawled_data, indent=2)
                crawl_status = f"Crawled {len(crawled_data)} pages."
            except Exception as e:
                result = json.dumps({"error": f"Error during crawl: {str(e)}"}, indent=2)
                crawl_status = "Crawl failed."
                crawled_data = {}
        else:
            result = json.dumps({"error": "Invalid URL"}, indent=2)
            crawl_status = "Invalid URL."
            crawled_data = {}

    return render_template('index.html', result=result, url=url, crawl_status=crawl_status, crawled_pages=crawled_data)

@app.route('/api/crawl', methods=['GET'])
def api_crawl():
    url = request.args.get('url')
    max_pages = int(request.args.get('max_pages', 10))
    max_depth = int(request.args.get('max_depth', 2))
    simplified = request.args.get('simplified', 'false').lower() == 'true'

    if not url or not is_valid_url(url):
        return jsonify({"error": "A valid URL parameter is required"}), 400

    try:
        crawled_data = parallel_crawl_website(url, max_pages, max_depth)

        if simplified:
            simplified_data = {}
            for page_url, page_data in crawled_data.items():
                simplified_data[page_url] = {
                    "title": page_data["title"],
                    "meta_tags": simplify_meta_tags(page_data["meta_tags"])
                }
            return jsonify(simplified_data)

        return jsonify(crawled_data)
    except Exception as e:
        return jsonify({"error": f"Error during crawl: {str(e)}"}), 500

@app.route('/api/extract', methods=['GET'])
def api_extract():
    url = request.args.get('url')
    simplified = request.args.get('simplified', 'false').lower() == 'true'

    if not url or not is_valid_url(url):
        return jsonify({"error": "A valid URL parameter is required"}), 400

    meta_tags_result = extract_meta_tags(url)

    if not meta_tags_result["success"]:
        return jsonify({"error": meta_tags_result["error"]}), 500

    meta_tags = meta_tags_result["meta_tags"]
    if simplified:
        return jsonify(simplify_meta_tags(meta_tags))
    return jsonify(meta_tags)


def handler(environ, start_response):
    return app(environ, start_response)