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


def canon_name(s: str):
    s = s.strip()
    s = re.sub(r"\s+", "", s)
    return s


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

    # 이미 깨진 A - -> B 복원
    chain = re.sub(r"\s*-\s*->\s*", " -> ", chain)

    # 단독 > 만 -> 로 변환
    chain = re.sub(r"(?<!-)\s*>\s*", " -> ", chain)

    # 화살표 주변 공백 통일
    chain = re.sub(r"\s*->\s*", " -> ", chain)
    chain = re.sub(r"\s+", " ", chain)

    return chain.strip()


def parse_flow_text(flow_text: str):
    if not flow_text:
        return []

    chain = flow_text.replace("\u00a0", " ")
    chain = re.sub(r"\s*-\s*->\s*", " -> ", chain)
    chain = re.sub(r"(?<!-)\s*>\s*", " -> ", chain)
    chain = re.sub(r"\s*->\s*", " -> ", chain)

    parts = [seg.strip() for seg in chain.split("->")]
    parts = [p for p in parts if p]

    cleaned = []
    for p in parts:
        p = re.sub(r"^\-+\s*", "", p)
        p = re.sub(r"\s*\-+$", "", p)
        if p:
            cleaned.append(p)

    return cleaned


def extract_writer_id_from_post_url(url: str):
    if not url:
        return None

    try:
        normalized = normalize_url(url)
        r = session.get(normalized, timeout=10)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")

        # 1) 게시글 페이지의 작성자 영역 찾기
        writer_el = (
            soup.select_one(".gall_writer")
            or soup.select_one("span.ub-writer")
            or soup.select_one("span.gall_writer")
            or soup.select_one("div.gall_writer")
        )

        # 2) 작성자 영역 자체의 data-* 속성
        if writer_el:
            writer_id = (
                (writer_el.get("data-uid") or "").strip()
                or (writer_el.get("data-userid") or "").strip()
                or (writer_el.get("data-nickid") or "").strip()
            )
            if writer_id:
                return writer_id

            # 3) 작성자 영역 내부 자식 요소의 data-* 속성
            child = (
                writer_el.find(attrs={"data-uid": True})
                or writer_el.find(attrs={"data-userid": True})
                or writer_el.find(attrs={"data-nickid": True})
            )
            if child:
                writer_id = (
                    (child.get("data-uid") or "").strip()
                    or (child.get("data-userid") or "").strip()
                    or (child.get("data-nickid") or "").strip()
                )
                if writer_id:
                    return writer_id

        # 4) gallog 링크 href에서 찾기
        gallog_link = soup.find("a", href=re.compile(r"gallog\.dcinside\.com"))
        if gallog_link:
            href = gallog_link.get("href", "")
            m = re.search(r"gallog\.dcinside\.com/?([A-Za-z0-9_]+)", href)
            if m:
                return m.group(1).strip()

        # 5) onclick fallback
        gallog_link = soup.find("a", onclick=re.compile(r"gallog\.dcinside\.com"))
        if gallog_link:
            raw = gallog_link.get("onclick", "")
            m = re.search(r"gallog\.dcinside\.com/?([A-Za-z0-9_]+)", raw)
            if m:
                return m.group(1).strip()

    except Exception as e:
        print("[WARN] extract_writer_id_from_post_url failed:", url, str(e))
        return None

    return None


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


def get_current_states_for_run(run_id: int):
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


def build_board_summaries(states):
    waves = {}
    for row in states:
        row["participants_list"] = parse_flow_text(row.get("flow_text", ""))
        wave_num = row["wave_number"]
        waves.setdefault(wave_num, []).append(row)

    summaries = []

    for wave_num, recs in waves.items():
        valid_recs = [r for r in recs if r["participants_list"]]
        if not valid_recs:
            continue

        main_rec = max(
            valid_recs,
            key=lambda r: (len(r["participants_list"]), r.get("created_at", ""))
        )

        final_chain = main_rec["participants_list"]
        final_canon = [canon_name(p) for p in final_chain]
        final_pos = {c: i for i, c in enumerate(final_canon)}

        post_info_by_canon = {}

        for rec in valid_recs:
            plist = rec["participants_list"]
            if len(plist) < 2:
                continue

            source_raw = plist[-2]
            source_c = canon_name(source_raw)

            if source_c not in final_pos:
                continue

            cand = {
                "url": rec.get("last_post_url"),
                "writer_id": rec.get("last_post_writer_id"),
                "score_len": len(plist),
                "score_time": rec.get("created_at", "")
            }

            prev = post_info_by_canon.get(source_c)
            if not prev:
                post_info_by_canon[source_c] = cand
            else:
                if (cand["score_len"], cand["score_time"]) > (prev["score_len"], prev["score_time"]):
                    post_info_by_canon[source_c] = cand

        summaries.append({
            "wave": wave_num,
            "final_chain": final_chain,
            "post_info_by_canon": post_info_by_canon
        })

    summaries.sort(key=lambda x: x["wave"])
    return summaries


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

        last_post_writer_id = None
        if last_post_url:
            last_post_writer_id = extract_writer_id_from_post_url(last_post_url)

        saved = (
            supabase.table("wave_states")
            .insert({
                "run_id": active_run["id"],
                "wave_number": int(wave_number),
                "flow_text": flow_text,
                "last_post_url": last_post_url,
                "last_post_writer_id": last_post_writer_id
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


@app.route("/state/current", methods=["GET"])
def state_current():
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


@app.route("/state/board", methods=["GET"])
def state_board():
    try:
        wave_name = request.args.get("waveName")

        if not wave_name:
            return jsonify({"error": "waveName is required"}), 400

        active_run = get_active_run(wave_name)
        if not active_run:
            return jsonify({"error": "no active run"}), 404

        result = (
            supabase.table("wave_states")
            .select("*")
            .eq("run_id", active_run["id"])
            .order("created_at", desc=False)
            .execute()
        )

        states = result.data or []
        summaries = build_board_summaries(states)

        return jsonify({
            "ok": True,
            "run": active_run,
            "summaries": summaries
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run()