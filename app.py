import re
import requests
from flask import Flask, request, jsonify
from bs4 import BeautifulSoup
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

session = requests.Session()
session.headers.update({
    "User-Agent":
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://gall.dcinside.com/"
})


# -----------------------------
# URL 변환 (모바일 → PC)
# -----------------------------
def normalize_url(url: str):
    try:
        if "m.dcinside.com/board/" in url:
            m = re.search(r"/board/([^/]+)/(\d+)", url)
            if m:
                return f"https://gall.dcinside.com/mgallery/board/view/?id={m.group(1)}&no={m.group(2)}"
    except:
        pass
    return url


# -----------------------------
# 웨이브 번호 추출
# -----------------------------
def extract_wave(text):
    patterns = [
        r"(?:웨이브|[Ww]ave)\s*(\d+)",
        r"(\d+)\s*(?:웨이브|[Ww]ave)"
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return ""


# -----------------------------
# 흐름도 추출
# -----------------------------
def extract_flow(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    chain_lines = []
    started = False

    for line in lines:
        if "->" in line or ">" in line:
            chain_lines.append(line)
            started = True
        elif started:
            break

    if not chain_lines:
        return ""

    chain = " ".join(chain_lines)
    chain = re.sub(r"\s*>\s*", " -> ", chain)
    chain = re.sub(r"\s*->\s*", " -> ", chain)

    return chain.strip()


# -----------------------------
# 글 크롤링
# -----------------------------
def crawl_post(url):
    url = normalize_url(url)

    r = session.get(url, timeout=10)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    body = soup.select_one("div.write_div")
    if not body:
        body = soup

    text = body.get_text("\n", strip=True)

    wave = extract_wave(text)
    flow = extract_flow(text)

    return {
        "waveNumber": wave,
        "flowLine": flow
    }


# -----------------------------
# API
# -----------------------------
@app.route("/crawl")
def crawl():
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "no url"})

    try:
        result = crawl_post(url)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)})


# Render용
if __name__ == "__main__":
    app.run()