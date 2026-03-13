import os
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client

# =============================
# 기본 설정
# =============================

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

app = Flask(__name__)
CORS(app)

session = requests.Session()
session.headers.update({
    "User-Agent":
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://gall.dcinside.com/"
})


# =============================
# 유틸
# =============================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def normalize_url(url: str):
    try:
        if "m.dcinside.com/board/" in url:
            m = re.search(r"/board/([^/]+)/(\d+)", url)
            if m:
                return f"https://gall.dcinside.com/mgallery/board/view/?id={m.group(1)}&no={m.group(2)}"
    except Exception:
        pass
    return url


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
    chain = re.sub(r"\s*-\s*->\s*", " -> ", chain)
    chain = re.sub(r"\s+", " ", chain)

    return chain.strip()


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


# =============================
# DB 헬퍼
# =============================

def get_active_run(wave_name: str):
    result = (
        supabase.table("event_runs")
        .select("*")
        .eq("wave_name", wave_name)
        .eq("status", "active")
        .order("started_at", desc=True)
        .limit(1)
        .execute()
    )

    if result.data and len(result.data) > 0:
        return result.data[0]
    return None


def close_all_active_runs(wave_name: str):
    result = (
        supabase.table("event_runs")
        .select("*")
        .eq("wave_name", wave_name)
        .eq("status", "active")
        .execute()
    )

    if not result.data:
        return

    for row in result.data:
        (
            supabase.table("event_runs")
            .update({
                "status": "closed",
                "ended_at": now_iso()
            })
            .eq("id", row["id"])
            .execute()
        )


def get_latest_state_for_wave(run_id: int, wave_number: int):
    result = (
        supabase.table("wave_states")
        .select("*")
        .eq("run_id", run_id)
        .eq("wave_number", wave_number)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if result.data and len(result.data) > 0:
        return result.data[0]
    return None


def get_current_states_for_run(run_id: int):
    """
    병렬 웨이브용:
    run 안의 모든 상태를 최신순으로 가져와서
    wave_number별 최신 1개만 반환
    """
    result = (
        supabase.table("wave_states")
        .select("*")
        .eq("run_id", run_id)
        .order("created_at", desc=True)
        .execute()
    )

    if not result.data:
        return []

    by_wave = {}
    for row in result.data:
        wave_number = row["wave_number"]
        if wave_number not in by_wave:
            by_wave[wave_number] = row

    return sorted(by_wave.values(), key=lambda x: x["wave_number"])


# =============================
# API
# =============================

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "message": "alive"})


@app.route("/crawl", methods=["GET"])
def crawl():
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "no url"}), 400

    try:
        result = crawl_post(url)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/run/start", methods=["POST"])
def run_start():
    """
    새 이벤트 시작
    같은 wave_name의 기존 active run이 있으면 종료 후 새로 시작
    """
    try:
        data = request.get_json(silent=True) or {}
        wave_name = data.get("waveName")

        if not wave_name:
            return jsonify({"error": "waveName is required"}), 400

        close_all_active_runs(wave_name)

        created = (
            supabase.table("event_runs")
            .insert({
                "wave_name": wave_name,
                "status": "active"
            })
            .execute()
        )

        return jsonify({
            "ok": True,
            "message": "new run started",
            "data": created.data
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/run/close", methods=["POST"])
def run_close():
    """
    현재 이벤트 종료
    """
    try:
        data = request.get_json(silent=True) or {}
        wave_name = data.get("waveName")

        if not wave_name:
            return jsonify({"error": "waveName is required"}), 400

        active_run = get_active_run(wave_name)
        if not active_run:
            return jsonify({"error": "no active run"}), 404

        updated = (
            supabase.table("event_runs")
            .update({
                "status": "closed",
                "ended_at": now_iso()
            })
            .eq("id", active_run["id"])
            .execute()
        )

        return jsonify({
            "ok": True,
            "message": "run closed",
            "data": updated.data
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/state/save", methods=["POST"])
def state_save():
    """
    현재 상태 저장
    body:
    {
      "waveName": "[금토일웨이브]",
      "waveNumber": 2,
      "flowText": "A -> B -> C ->",
      "lastPostUrl": "https://..."
    }
    """
    try:
        data = request.get_json(silent=True) or {}

        wave_name = data.get("waveName")
        wave_number = data.get("waveNumber")
        flow_text = data.get("flowText")
        last_post_url = data.get("lastPostUrl", "")

        if not wave_name or wave_number is None or not flow_text:
            return jsonify({
                "error": "waveName, waveNumber, flowText are required"
            }), 400

        active_run = get_active_run(wave_name)
        if not active_run:
            return jsonify({
                "error": "no active run. start a run first"
            }), 404

        saved = (
            supabase.table("wave_states")
            .insert({
                "run_id": active_run["id"],
                "wave_number": int(wave_number),
                "flow_text": flow_text,
                "last_post_url": last_post_url
            })
            .execute()
        )

        return jsonify({
            "ok": True,
            "message": "state saved",
            "data": saved.data
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/state/latest", methods=["GET"])
def state_latest():
    """
    현재 active run 안에서 특정 wave_number의 최신 상태 가져오기
    query:
    ?waveName=[금토일웨이브]&waveNumber=2
    """
    try:
        wave_name = request.args.get("waveName")
        wave_number = request.args.get("waveNumber")

        if not wave_name:
            return jsonify({"error": "waveName is required"}), 400

        if not wave_number:
            return jsonify({"error": "waveNumber is required"}), 400

        active_run = get_active_run(wave_name)
        if not active_run:
            return jsonify({"error": "no active run"}), 404

        latest = get_latest_state_for_wave(active_run["id"], int(wave_number))
        if not latest:
            return jsonify({"error": "no saved state for this wave"}), 404

        return jsonify({
            "ok": True,
            "run": active_run,
            "state": latest
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/state/current", methods=["GET"])
def state_current():
    """
    현재 active run 안의 병렬 웨이브 최신 상태 전체 조회
    query:
    ?waveName=[금토일웨이브]
    """
    try:
        wave_name = request.args.get("waveName")

        if not wave_name:
            return jsonify({"error": "waveName is required"}), 400

        active_run = get_active_run(wave_name)
        if not active_run:
            return jsonify({"error": "no active run"}), 404

        states = get_current_states_for_run(active_run["id"])

        return jsonify({
            "ok": True,
            "run": active_run,
            "states": states
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run()