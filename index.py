import os
import json
import logging
import requests
import time
import re
from urllib.robotparser import RobotFileParser
from flask import Flask, request, render_template, jsonify
from flask_cors import CORS
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin, urldefrag
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

# Cache to store already crawled pages
crawled_pages = {}
# Cache to store robots.txt data
robots_cache = {}


def is_valid_url(url):
    """Check if URL is valid."""
    try:
        parsed = urlparse(url)
        return all([parsed.scheme, parsed.netloc])
    except:
        return False


def get_base_url(url):
    """Extract base URL."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def is_same_domain(url, base_url):
    """Check if URL belongs to the same domain."""
    return urlparse(url).netloc == urlparse(base_url).netloc


def normalize_url(url):
    """Normalize URL by removing fragments and resolving relative paths."""
    url = url.strip()
    # Remove URL fragments
    url, _ = urldefrag(url)
    # Normalize trailing slashes
    if url.endswith('/'):
        url = url[:-1]
    return url


def is_allowed_by_robots(url):
    """Check if URL is allowed by robots.txt."""
    base_url = get_base_url(url)

    if base_url not in robots_cache:
        rp = RobotFileParser()
        robots_url = urljoin(base_url, "/robots.txt")
        try:
            response = requests.get(robots_url, timeout=5)
            if response.status_code == 200:
                rp.parse(response.text.splitlines())
            # No robots.txt or couldn't fetch it, assume everything is allowed
        except:
            # Error fetching robots.txt, assume everything is allowed
            pass

        # Set a reasonable delay if one isn't specified
        crawl_delay = getattr(rp, 'crawl_delay', lambda x: None)('*') or 1
        robots_cache[base_url] = {
            'parser': rp,
            'crawl_delay': min(crawl_delay, 5)  # Cap at 5 seconds max
        }

    return robots_cache[base_url]['parser'].can_fetch("*", url)


def should_process_url(url):
    """Determine if a given URL should be processed or skipped."""
    # Skip URLs with common non-HTML file extensions
    skip_extensions = [
        '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.svg', '.webp',  # Images
        '.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx',  # Documents
        '.zip', '.rar', '.gz', '.tar', '.7z',  # Archives
        '.mp3', '.mp4', '.wav', '.avi', '.mov', '.mkv',  # Media
        '.js', '.css', '.json', '.xml', '.csv',  # Data/code files
        '.ico', '.ttf', '.woff', '.eot'  # Web assets
    ]

    parsed = urlparse(url)
    path = parsed.path.lower()

    # Check extensions
    if any(path.endswith(ext) for ext in skip_extensions):
        return False

    # Skip URLs with query parameters that might cause issues
    if any(param in parsed.query for param in ['logout', 'signout', 'delete']):
        return False

    # Skip common admin/login paths
    skip_paths = ['/admin', '/login', '/signin', '/wp-admin', '/logout', '/cart', '/checkout']
    if any(path.startswith(skip_path) for skip_path in skip_paths):
        return False

    return True


def extract_links(html, base_url):
    """Extract all links from the HTML."""
    soup = BeautifulSoup(html, 'html.parser')
    links = set()  # Use a set to avoid duplicates

    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href']
        # Skip empty or javascript links
        if not href or href.startswith(('javascript:', '#', 'mailto:', 'tel:')):
            continue

        # Construct absolute URL
        absolute_url = urljoin(base_url, href)

        # Normalize URL
        absolute_url = normalize_url(absolute_url)

        # Skip if not valid URL or should not be processed
        if not is_valid_url(absolute_url) or not should_process_url(absolute_url):
            continue

        links.add(absolute_url)

    return list(links)


def extract_meta_tags(url):
    """Extract meta tags from a URL."""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5'
        }

        response = requests.get(url, headers=headers, timeout=10, allow_redirects=True)

        # If redirected, use the final URL
        if response.url != url:
            url = response.url

        response.raise_for_status()

        # Check if content type is HTML
        content_type = response.headers.get('Content-Type', '')
        if 'text/html' not in content_type.lower() and 'application/xhtml+xml' not in content_type.lower():
            return {"success": False, "url": url, "error": "Not an HTML page"}

        # Detect encoding
        encoding = response.encoding

        try:
            html_content = response.text
            soup = BeautifulSoup(html_content, 'html.parser')

            # Extract title
            title = soup.title.string if soup.title else "No Title"

            # Extract meta tags
            meta_tags = soup.find_all('meta')
            meta_data = []
            for tag in meta_tags:
                tag_dict = {}
                for attr, value in tag.attrs.items():
                    # Convert values to strings for JSON serialization
                    tag_dict[attr] = str(value)
                meta_data.append(tag_dict)

            # Extract links for crawling
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
    """Crawl website in parallel using BFS approach."""
    global crawled_pages
    crawled_pages = {}  # Reset the cache for each new crawl

    # Normalize the start URL
    start_url = normalize_url(start_url)
    base_url = get_base_url(start_url)

    # Track URLs by their crawl depth
    urls_by_depth = {0: [start_url]}
    processed_urls = set()

    # Process the start URL first
    logging.info(f"Starting crawl at {start_url}")
    result = extract_meta_tags(start_url)

    if result["success"]:
        crawled_pages[start_url] = {
            "title": result["title"],
            "meta_tags": result["meta_tags"]
        }
        processed_urls.add(start_url)

        # Add discovered links to crawl queue at depth 1
        if 1 not in urls_by_depth:
            urls_by_depth[1] = []

        for link in result["links"]:
            if (link not in processed_urls and is_same_domain(link, base_url)
                    and is_allowed_by_robots(link)):
                urls_by_depth[1].append(link)
    else:
        logging.error(f"Failed to crawl start URL: {result.get('error', 'Unknown error')}")
        return crawled_pages  # Return empty dict if start URL fails

    # BFS crawl by depth
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for depth in range(1, max_depth + 1):
            if depth not in urls_by_depth or not urls_by_depth[depth]:
                continue

            if len(crawled_pages) >= max_pages:
                break

            logging.info(f"Crawling at depth {depth}, URLs to process: {len(urls_by_depth[depth])}")

            # Limit URLs to process based on max_pages
            urls_to_process = urls_by_depth[depth][:max_pages - len(crawled_pages)]

            # Submit all URLs at this depth for processing
            future_to_url = {
                executor.submit(extract_meta_tags, url): url
                for url in urls_to_process
                if url not in processed_urls
            }

            # Process the results as they complete
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

                        # Add links for next depth level
                        if depth + 1 <= max_depth:
                            if depth + 1 not in urls_by_depth:
                                urls_by_depth[depth + 1] = []

                            for link in result["links"]:
                                if (link not in processed_urls and is_same_domain(link, base_url)
                                        and is_allowed_by_robots(link)):
                                    urls_by_depth[depth + 1].append(link)

                        # Respect rate limiting based on robots.txt
                        robot_info = robots_cache.get(base_url, {'crawl_delay': 1})
                        time.sleep(robot_info['crawl_delay'])
                except Exception as e:
                    logging.error(f"Error processing {url}: {str(e)}")

    return crawled_pages


def simplify_meta_tags(meta_tags):
    """Convert meta tags to a simplified dictionary."""
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
    crawled_data = {}  # Initialize empty dictionary

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


if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=5000)