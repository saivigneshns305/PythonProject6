from flask import Flask, request, render_template, jsonify
from flask_cors import CORS
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import requests
import json
import logging
import os

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

def is_valid_url(url):
    parsed = urlparse(url)
    return all([parsed.scheme, parsed.netloc])

def extract_meta_tags(url):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 11.0; Win64; x64) Chrome/100.0.4896.127 Chromium/100.0.4896.127'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        meta_tags = soup.find_all('meta')

        meta_data = []
        for tag in meta_tags:
            tag_dict = {}
            for attr, value in tag.attrs.items():
                tag_dict[attr] = value
            meta_data.append(tag_dict)
        return meta_data

    except requests.exceptions.RequestException as e:
        return {"error": f"Error fetching URL: {e}"}
    except Exception as e:
        return {"error": f"An error occurred: {e}"}

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
    if request.method == 'POST':
        url = request.form.get('url')
        if url and is_valid_url(url):
            meta_tags = extract_meta_tags(url)
            result = json.dumps(meta_tags, indent=2)
        else:
            result = json.dumps({"error": "Invalid URL"}, indent=2)
    return render_template('index.html', result=result, url=url)

@app.route('/api/extract', methods=['GET'])
def api_extract():
    url = request.args.get('url')
    simplified = request.args.get('simplified', 'false').lower() == 'true'

    if not url or not is_valid_url(url):
        return jsonify({"error": "A valid URL parameter is required"}), 400

    meta_tags = extract_meta_tags(url)
    if isinstance(meta_tags, dict) and 'error' in meta_tags:
        return jsonify(meta_tags), 500

    if simplified:
        return jsonify(simplify_meta_tags(meta_tags))
    return jsonify(meta_tags)


def handler(environ, start_response):
    return app(environ, start_response)
