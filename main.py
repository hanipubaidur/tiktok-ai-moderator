import io
import os
import re
import json
import time
import random
import shutil
import asyncio
import itertools
import subprocess
import tempfile
import httpx
import discord
import edge_tts
import pygame
import pygame._sdl2.audio as sdl2_audio
import yt_dlp
from collections import deque
from datetime import datetime
from difflib import SequenceMatcher
from gtts import gTTS
from dotenv import load_dotenv
from google import genai
from google.genai import types
from openai import OpenAI
from TikTokLive import TikTokLiveClient
from TikTokLive.events import (
    ConnectEvent,
    DisconnectEvent,
    CommentEvent,
    GiftEvent,
    FollowEvent,
    ShareEvent,
    LikeEvent,
)

os.system("")
load_dotenv()

BOOT_TIME = time.time()
DATASET_FILE = "dataset.json"
LIVE_QUEUE_TXT_FILE = "live_music_queue.txt"
OVERLAY_HOST = "0.0.0.0"
OVERLAY_PORT = 8765

openrouter_key = os.getenv("OPENROUTER_API_KEY")
gemini_key = os.getenv("GEMINI_API_KEY")
discord_token = os.getenv("DISCORD_BOT_TOKEN")
control_channel_id = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
tiktok_username = os.getenv("TIKTOK_USERNAME")
tiktok_session_id = os.getenv("TIKTOK_SESSION_ID", "").strip()
tiktok_target_idc = os.getenv("TIKTOK_TARGET_IDC", "").strip()


class TermUI:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[90m"
    CYAN = "\033[96m"
    BLUE = "\033[94m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    MAGENTA = "\033[95m"
    WHITE = "\033[97m"

    STYLES = {
        "SYSTEM": (CYAN, "SYSTEM "),
        "AUDIO": (BLUE, "AUDIO  "),
        "TIKTOK": (MAGENTA, "TIKTOK "),
        "CHAT": (WHITE, "CHAT   "),
        "AI-MOD": (GREEN, "AI-MOD "),
        "VOICE": (CYAN, "VOICE  "),
        "MUSIC": (MAGENTA, "MUSIC  "),
        "DISCORD": (BLUE, "DISCORD"),
        "OVERLAY": (CYAN, "OVERLAY"),
        "WARN": (YELLOW, "WARN   "),
        "ERROR": (RED, "ERROR  "),
    }

    @classmethod
    def log(cls, tag: str, message: str, accent: str | None = None):
        now_str = datetime.now().strftime("%H:%M:%S")
        color, label = cls.STYLES.get(tag.upper(), (cls.WHITE, tag.upper().ljust(7)[:7]))
        msg_color = accent or cls.RESET
        print(f"{cls.DIM}{now_str}{cls.RESET} {color}{cls.BOLD}[{label}]{cls.RESET} {msg_color}{message}{cls.RESET}")

    @classmethod
    def banner(cls, audio_dev: str, overlay_url: str):
        width = 68
        top = f"{cls.CYAN}┌{'─' * width}┐{cls.RESET}"
        bot = f"{cls.CYAN}└{'─' * width}┘{cls.RESET}"
        mid = f"{cls.CYAN}├{'─' * width}┤{cls.RESET}"

        def row(text: str, color: str = cls.WHITE):
            clean_len = len(re.sub(r"\033\[[0-9;]*m", "", text))
            pad = max(0, width - 2 - clean_len)
            return f"{cls.CYAN}│{cls.RESET} {color}{text}{cls.RESET}{' ' * pad} {cls.CYAN}│{cls.RESET}"

        print("\n" + top)
        print(row("MINA AI CO-HOST, MODERATOR & LIVE MUSIC ENGINE", cls.BOLD + cls.CYAN))
        print(mid)
        print(row(f"Target Live   : @{tiktok_username or '-'}"))
        print(row(f"Audio Output  : {audio_dev}"))
        print(row("LLM Engine    : Gemini 3.5 Flash-Lite (Pipelined Fast TTS)"))
        print(row(f"Live Overlay  : {overlay_url} (SSE Real-Time)"))
        print(row(f"Queue File    : {LIVE_QUEUE_TXT_FILE}"))
        print(bot + "\n")


openrouter_client = OpenAI(
    api_key=openrouter_key,
    base_url="https://openrouter.ai/api/v1"
) if openrouter_key else None

gemini_client = genai.Client(api_key=gemini_key) if gemini_key else None

tiktok_client = TikTokLiveClient(unique_id=tiktok_username)
if tiktok_session_id:
    idc_val = tiktok_target_idc or "alisg"
    TermUI.log("SYSTEM", f"TIKTOK_SESSION_ID siap untuk moderasi lokal (IDC: {idc_val}).")

PRIORITY_HOST = 1
PRIORITY_GIFT = 2
PRIORITY_COMMENT = 3
PRIORITY_SOCIAL = 4

action_queue = asyncio.PriorityQueue()
queue_counter = itertools.count()
song_id_counter = itertools.count(1)
latest_comment_token = {"id": 0}
seen_comments = set()
followed_users = set()
shared_users_cooldown = {}
user_music_cooldown = {}
recent_memory = deque(maxlen=8)
user_warnings = {}
viewer_registry = {}
last_escalated_question = {"user": None, "question": None, "msg_id": None}

music_pending_items: list[dict] = []
music_queue_event = asyncio.Event()
overlay_subscribers: set[asyncio.Queue] = set()
played_music_history = deque(maxlen=30)
music_state = {
    "current": None,
    "current_item": None,
    "is_autoplay_playing": False,
    "started_at": 0.0,
    "duration": 0,
    "volume": 0.25,
    "skip_flag": False,
    "stop_flag": False,
    "channel": None,
    "autoplay": True,
    "last_seed_query": None,
    "last_played_title": None,
    "overlay_url": f"http://127.0.0.1:{OVERLAY_PORT}",
}

state = {
    "muted": False,
    "tiktok_connected": False,
    "tiktok_started": False,
    "last_like_shoutout": 0.0,
}

_dataset_cache = {"mtime": 0.0, "data": None}
worker_task = None
music_worker_task = None
overlay_ticker_task = None
overlay_server = None
connected_at = None
NEURAL_VOICE = "id-ID-GadisNeural"


def init_vbcable() -> str:
    pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=1024)
    devices = sdl2_audio.get_audio_device_names(False)
    pygame.mixer.quit()

    cable_device = next((d for d in devices if "CABLE Input" in d), None)
    if not cable_device:
        raise RuntimeError(f"CABLE Input tidak ditemukan. Device tersedia: {devices}")

    pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=1024, devicename=cable_device)
    music_state["channel"] = pygame.mixer.Channel(1)
    return cable_device


def format_duration_mmss(seconds: int | float) -> str:
    sec = max(0, int(seconds))
    mins, rem = divmod(sec, 60)
    return f"{mins:02d}:{rem:02d}"


def format_wait_minutes(seconds: int | float) -> str:
    sec = max(0, int(seconds))
    if sec < 45:
        return "< 1 mnt"
    mins = round(sec / 60)
    return f"~{max(1, mins)} mnt"


def get_live_queue_snapshot() -> dict:
    now = time.time()
    curr_item = music_state.get("current_item")
    elapsed = 0
    remaining = 0
    duration = 0

    if curr_item and music_state["started_at"] > 0:
        duration = max(1, int(music_state["duration"] or curr_item.get("duration") or 210))
        elapsed = min(duration, max(0, int(now - music_state["started_at"])))
        remaining = max(0, duration - elapsed)

    cumulative_wait = 0 if music_state.get("is_autoplay_playing") else remaining
    top_5 = []
    for idx, item in enumerate(music_pending_items[:5], start=1):
        wait_label = format_wait_minutes(cumulative_wait)
        top_5.append({
            "uid": item.get("uid"),
            "pos": idx,
            "title": item.get("title") or item.get("query") or "-",
            "requester": item.get("requester") or "Viewer",
            "wait_sec": int(cumulative_wait),
            "wait_label": wait_label,
            "duration": int(item.get("duration") or 210),
        })
        cumulative_wait += int(item.get("duration") or 210)

    return {
        "now_playing": {
            "title": curr_item.get("title") if curr_item else None,
            "requester": curr_item.get("requester") if curr_item else None,
            "elapsed": elapsed,
            "duration": duration,
            "elapsed_str": format_duration_mmss(elapsed),
            "duration_str": format_duration_mmss(duration),
            "progress_pct": round((elapsed / duration) * 100, 1) if duration > 0 else 0,
        } if curr_item else None,
        "queue_top5": top_5,
        "total_queued": len(music_pending_items),
        "autoplay": music_state["autoplay"],
        "vibes": music_state["last_seed_query"] or "",
    }


def sync_live_queue_file(snap: dict | None = None):
    try:
        snap = snap or get_live_queue_snapshot()
        lines = []
        np = snap["now_playing"]
        if np:
            lines.append(
                f"🎵 NOW PLAYING: {np['title']} (Req: {np['requester']}) [{np['elapsed_str']}/{np['duration_str']}]"
            )
        else:
            lines.append("🎵 NOW PLAYING: Tidak ada lagu diputar")

        lines.append("")
        lines.append("📋 ANTREAN MUSIK (TOP 5):")
        if snap["queue_top5"]:
            for q in snap["queue_top5"]:
                lines.append(
                    f"#{q['pos']}. {q['title']} | Req: {q['requester']} | Antri {q['wait_label']}"
                )
            extra = snap["total_queued"] - len(snap["queue_top5"])
            if extra > 0:
                lines.append(f"... dan +{extra} lagu lainnya di antrean")
        else:
            ap_note = f"Autoplay ON (Vibes: {snap['vibes']})" if snap["autoplay"] and snap["vibes"] else "Antrean kosong"
            lines.append(f"Belum ada antrean ({ap_note})")

        lines.append("")
        lines.append("Ketik !play <judul lagu> di chat untuk request!")

        with open(LIVE_QUEUE_TXT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass


def broadcast_queue_update():
    snap = get_live_queue_snapshot()
    sync_live_queue_file(snap)
    payload = json.dumps(snap, ensure_ascii=False)
    for sub_q in list(overlay_subscribers):
        try:
            if sub_q.full():
                sub_q.get_nowait()
            sub_q.put_nowait(payload)
        except Exception:
            pass


def remove_queued_songs(spec: str) -> list[dict]:
    if not music_pending_items:
        return []

    raw = spec.strip()
    if not raw:
        return []

    tokens = [t.strip() for t in re.split(r"[\s,]+", raw) if t.strip()]
    target_indices: set[int] = set()
    all_numeric = True

    for tok in tokens:
        if re.fullmatch(r"\d+", tok):
            pos = int(tok)
            if 1 <= pos <= len(music_pending_items):
                target_indices.add(pos - 1)
        elif re.fullmatch(r"\d+-\d+", tok):
            start_s, end_s = tok.split("-", 1)
            a, b = int(start_s), int(end_s)
            if a > b:
                a, b = b, a
            for pos in range(a, b + 1):
                if 1 <= pos <= len(music_pending_items):
                    target_indices.add(pos - 1)
        else:
            all_numeric = False
            break

    removed: list[dict] = []
    if all_numeric:
        if not target_indices:
            return []
        remaining = []
        for idx, item in enumerate(music_pending_items):
            if idx in target_indices:
                removed.append(item)
            else:
                remaining.append(item)
        music_pending_items[:] = remaining
    else:
        kw = raw.lower()
        remaining = []
        for item in music_pending_items:
            title_l = (item.get("title") or "").lower()
            query_l = (item.get("query") or "").lower()
            req_l = (item.get("requester") or "").lower()
            if kw in title_l or kw in query_l or kw in req_l:
                removed.append(item)
            else:
                remaining.append(item)
        music_pending_items[:] = remaining

    if removed:
        broadcast_queue_update()
    return removed


def remove_queued_song_by_uid(uid: int) -> dict | None:
    for idx, item in enumerate(music_pending_items):
        if item.get("uid") == uid:
            removed = music_pending_items.pop(idx)
            broadcast_queue_update()
            return removed
    return None


def launch_overlay_app_window(url: str) -> str | None:
    browser_candidates = [
        shutil.which("msedge"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        shutil.which("chrome"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    exe = next((p for p in browser_candidates if p and os.path.exists(p)), None)
    if not exe:
        return None

    profile_dir = os.path.join(tempfile.gettempdir(), "mina_overlay_browser_profile")
    args = [
        exe,
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        "--window-size=460,540",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-features=CalculateNativeWinOcclusion",
    ]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return os.path.basename(exe)


OVERLAY_HTML = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Live Music Queue Overlay</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    width: 100%;
    height: 100%;
    background: #0b0f19;
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    color: #f8fafc;
    overflow: hidden;
  }
  body.chroma {
    background: #00ff00;
    padding: 12px;
  }
  .card {
    width: 100%;
    height: 100%;
    background: linear-gradient(160deg, rgba(15, 23, 42, 0.97), rgba(10, 14, 26, 0.99));
    border: 2px solid rgba(56, 189, 248, 0.35);
    padding: clamp(14px, 3vh, 24px) clamp(16px, 3vw, 26px);
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    gap: clamp(10px, 2vh, 16px);
  }
  body.chroma .card {
    border-radius: 18px;
  }
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-shrink: 0;
  }
  .badge {
    font-size: clamp(12px, 2.2vh, 16px);
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #38bdf8;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: #22c55e;
    box-shadow: 0 0 10px #22c55e;
    animation: pulse 1.4s infinite;
  }
  .dot.idle { background: #64748b; box-shadow: none; animation: none; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.35} }
  .ap-pill {
    font-size: clamp(11px, 1.9vh, 14px);
    font-weight: 700;
    padding: 4px 12px;
    border-radius: 999px;
    background: rgba(56, 189, 248, 0.16);
    color: #7dd3fc;
    border: 1px solid rgba(56, 189, 248, 0.3);
  }
  .np-box {
    background: rgba(30, 41, 59, 0.82);
    border-radius: 12px;
    padding: clamp(12px, 2.2vh, 18px) clamp(14px, 2.5vw, 20px);
    border-left: 4px solid #38bdf8;
    flex-shrink: 0;
  }
  .np-label {
    font-size: clamp(10px, 1.6vh, 12px);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #38bdf8;
    margin-bottom: 4px;
  }
  .np-title {
    font-size: clamp(16px, 2.9vh, 24px);
    font-weight: 800;
    color: #ffffff;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .np-meta {
    display: flex;
    justify-content: space-between;
    font-size: clamp(12px, 2.1vh, 16px);
    color: #cbd5e1;
    margin-top: 6px;
  }
  .bar-bg {
    width: 100%;
    height: 6px;
    background: rgba(148, 163, 184, 0.22);
    border-radius: 6px;
    margin-top: 10px;
    overflow: hidden;
  }
  .bar-fill {
    height: 100%;
    background: linear-gradient(90deg, #38bdf8, #818cf8);
    width: 0%;
    transition: width 0.35s linear;
  }
  .q-section {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .q-header {
    font-size: clamp(12px, 2.1vh, 15px);
    font-weight: 700;
    color: #cbd5e1;
    letter-spacing: 0.05em;
    margin-bottom: 8px;
    display: flex;
    justify-content: space-between;
    flex-shrink: 0;
  }
  .q-list {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: clamp(6px, 1.3vh, 10px);
  }
  .q-item {
    flex: 1;
    max-height: 76px;
    display: flex;
    align-items: center;
    gap: 12px;
    background: rgba(15, 23, 42, 0.78);
    border: 1px solid rgba(148, 163, 184, 0.16);
    border-radius: 11px;
    padding: 8px 14px;
    transition: border-color 0.2s ease;
  }
  .q-item:hover {
    border-color: rgba(248, 113, 113, 0.5);
  }
  .q-pos {
    min-width: clamp(30px, 4.5vh, 40px);
    height: clamp(30px, 4.5vh, 40px);
    border-radius: 9px;
    background: rgba(56, 189, 248, 0.2);
    color: #38bdf8;
    font-size: clamp(13px, 2.3vh, 18px);
    font-weight: 800;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }
  .q-info { flex: 1; min-width: 0; }
  .q-title {
    font-size: clamp(14px, 2.4vh, 19px);
    font-weight: 700;
    color: #f1f5f9;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .q-req {
    font-size: clamp(11px, 1.9vh, 14px);
    color: #94a3b8;
    margin-top: 2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .q-wait {
    font-size: clamp(11px, 1.9vh, 15px);
    font-weight: 700;
    color: #fbbf24;
    background: rgba(251, 191, 36, 0.14);
    border: 1px solid rgba(251, 191, 36, 0.28);
    padding: 5px 10px;
    border-radius: 8px;
    white-space: nowrap;
    flex-shrink: 0;
  }
  .q-del {
    display: none;
    background: rgba(239, 68, 68, 0.22);
    color: #fca5a5;
    border: 1px solid rgba(239, 68, 68, 0.45);
    border-radius: 7px;
    padding: 5px 9px;
    font-size: 12px;
    font-weight: 800;
    cursor: pointer;
    flex-shrink: 0;
  }
  .q-item:hover .q-del {
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .q-del:hover {
    background: rgba(239, 68, 68, 0.45);
    color: #ffffff;
  }
  .empty {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: clamp(13px, 2.2vh, 17px);
    color: #64748b;
    text-align: center;
    border: 1px dashed rgba(148, 163, 184, 0.2);
    border-radius: 11px;
    padding: 12px;
  }
  .footer {
    padding-top: 8px;
    border-top: 1px solid rgba(148, 163, 184, 0.18);
    font-size: clamp(12px, 2vh, 15px);
    color: #94a3b8;
    text-align: center;
    flex-shrink: 0;
  }
  .footer span { color: #38bdf8; font-weight: 800; }
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <div class="badge"><div id="dot" class="dot idle"></div> LIVE MUSIC REQUEST</div>
    <div id="apPill" class="ap-pill">AUTOPLAY: ON</div>
  </div>
  <div class="np-box">
    <div class="np-label">Sedang Diputar</div>
    <div id="npTitle" class="np-title">Menunggu request lagu...</div>
    <div class="np-meta">
      <span id="npReq">Ketik !play &lt;judul&gt;</span>
      <span id="npTime">00:00 / 00:00</span>
    </div>
    <div class="bar-bg"><div id="npBar" class="bar-fill"></div></div>
  </div>
  <div class="q-section">
    <div class="q-header">
      <span>ANTREAN BERIKUTNYA (TOP 5)</span>
      <span id="qCount">0 Lagu</span>
    </div>
    <div id="qList" class="q-list">
      <div class="empty">Antrean kosong</div>
    </div>
  </div>
  <div class="footer">Ketik <span>!play &lt;judul lagu&gt;</span> di kolom komentar untuk request</div>
</div>
<script>
if (new URLSearchParams(location.search).get('chroma') === '1') {
  document.body.classList.add('chroma');
}

async function removeQueueItem(uid) {
  try {
    await fetch('/api/remove', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ uid: uid })
    });
  } catch (e) {}
}

function renderQueue(data) {
  const dot = document.getElementById('dot');
  const apPill = document.getElementById('apPill');
  const npTitle = document.getElementById('npTitle');
  const npReq = document.getElementById('npReq');
  const npTime = document.getElementById('npTime');
  const npBar = document.getElementById('npBar');
  const qList = document.getElementById('qList');
  const qCount = document.getElementById('qCount');

  apPill.textContent = data.autoplay ? 'AUTOPLAY: ON' : 'AUTOPLAY: OFF';

  if (data.now_playing) {
    dot.classList.remove('idle');
    npTitle.textContent = data.now_playing.title;
    npReq.textContent = 'Req by: ' + data.now_playing.requester;
    npTime.textContent = data.now_playing.elapsed_str + ' / ' + data.now_playing.duration_str;
    npBar.style.width = data.now_playing.progress_pct + '%';
  } else {
    dot.classList.add('idle');
    npTitle.textContent = 'Tidak ada lagu yang diputar';
    npReq.textContent = 'Siap menerima request';
    npTime.textContent = '00:00 / 00:00';
    npBar.style.width = '0%';
  }

  qCount.textContent = data.total_queued + ' Antrean';
  qList.innerHTML = '';

  if (!data.queue_top5 || data.queue_top5.length === 0) {
    const emptyDiv = document.createElement('div');
    emptyDiv.className = 'empty';
    emptyDiv.textContent = data.autoplay && data.vibes
      ? 'Antrean kosong — Autoplay siap memilih lagu se-vibe'
      : 'Belum ada antrean lagu';
    qList.appendChild(emptyDiv);
    return;
  }

  data.queue_top5.forEach(item => {
    const row = document.createElement('div');
    row.className = 'q-item';

    const pos = document.createElement('div');
    pos.className = 'q-pos';
    pos.textContent = '#' + item.pos;

    const info = document.createElement('div');
    info.className = 'q-info';

    const title = document.createElement('div');
    title.className = 'q-title';
    title.textContent = item.title;

    const req = document.createElement('div');
    req.className = 'q-req';
    req.textContent = 'Req by: ' + item.requester;

    info.appendChild(title);
    info.appendChild(req);

    const wait = document.createElement('div');
    wait.className = 'q-wait';
    wait.textContent = item.wait_label;

    const delBtn = document.createElement('button');
    delBtn.className = 'q-del';
    delBtn.title = 'Hapus dari antrean';
    delBtn.textContent = '✕';
    delBtn.onclick = () => removeQueueItem(item.uid);

    row.appendChild(pos);
    row.appendChild(info);
    row.appendChild(wait);
    row.appendChild(delBtn);
    qList.appendChild(row);
  });
}

function connectSSE() {
  const es = new EventSource('/api/events');
  es.onmessage = (ev) => {
    try {
      renderQueue(JSON.parse(ev.data));
    } catch (e) {}
  };
  es.onerror = () => {
    es.close();
    setTimeout(connectSSE, 1500);
  };
}

connectSSE();
</script>
</body>
</html>"""


async def handle_overlay_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    sub_q: asyncio.Queue | None = None
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        if not request_line:
            writer.close()
            return

        parts = request_line.decode("utf-8", errors="ignore").split()
        method = parts[0].upper() if len(parts) > 0 else "GET"
        path = parts[1] if len(parts) > 1 else "/"

        content_length = 0
        while True:
            header_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            if not header_line or header_line == b"\r\n":
                break
            if header_line.lower().startswith(b"content-length:"):
                try:
                    content_length = int(header_line.split(b":", 1)[1].strip())
                except ValueError:
                    content_length = 0

        if path.startswith("/api/events"):
            header = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/event-stream; charset=utf-8\r\n"
                "Cache-Control: no-cache, no-transform\r\n"
                "Connection: keep-alive\r\n"
                "Access-Control-Allow-Origin: *\r\n\r\n"
            ).encode("utf-8")
            writer.write(header)
            await writer.drain()

            sub_q = asyncio.Queue(maxsize=10)
            overlay_subscribers.add(sub_q)

            init_payload = json.dumps(get_live_queue_snapshot(), ensure_ascii=False)
            writer.write(f"data: {init_payload}\n\n".encode("utf-8"))
            await writer.drain()

            while True:
                try:
                    msg = await asyncio.wait_for(sub_q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    msg = json.dumps(get_live_queue_snapshot(), ensure_ascii=False)
                writer.write(f"data: {msg}\n\n".encode("utf-8"))
                await writer.drain()

        elif path.startswith("/api/remove") and method == "POST":
            raw_body = b""
            if content_length > 0:
                raw_body = await asyncio.wait_for(reader.readexactly(content_length), timeout=2.0)
            removed_title = None
            try:
                payload = json.loads(raw_body.decode("utf-8", errors="ignore"))
                if "uid" in payload:
                    rem = remove_queued_song_by_uid(int(payload["uid"]))
                    if rem:
                        removed_title = rem.get("title")
                elif "spec" in payload:
                    rems = remove_queued_songs(str(payload["spec"]))
                    if rems:
                        removed_title = ", ".join(r.get("title", "-") for r in rems)
            except Exception:
                pass

            body = json.dumps({"ok": bool(removed_title), "removed": removed_title}, ensure_ascii=False).encode("utf-8")
            header = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("utf-8")
            writer.write(header + body)
            await writer.drain()

        elif path.startswith("/api/queue"):
            body = json.dumps(get_live_queue_snapshot(), ensure_ascii=False).encode("utf-8")
            header = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("utf-8")
            writer.write(header + body)
            await writer.drain()

        else:
            body = OVERLAY_HTML.encode("utf-8")
            header = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("utf-8")
            writer.write(header + body)
            await writer.drain()

    except Exception:
        pass
    finally:
        if sub_q is not None:
            overlay_subscribers.discard(sub_q)
        try:
            writer.close()
        except Exception:
            pass


async def overlay_file_ticker():
    while True:
        sync_live_queue_file()
        await asyncio.sleep(1.5)


async def start_overlay_services():
    global overlay_server, overlay_ticker_task
    if overlay_ticker_task is None or overlay_ticker_task.done():
        overlay_ticker_task = asyncio.create_task(overlay_file_ticker())

    if overlay_server is not None:
        return

    for port in range(OVERLAY_PORT, OVERLAY_PORT + 5):
        try:
            overlay_server = await asyncio.start_server(handle_overlay_http, OVERLAY_HOST, port)
            music_state["overlay_url"] = f"http://127.0.0.1:{port}"
            TermUI.log("OVERLAY", f"Live Queue Overlay SSE aktif di {music_state['overlay_url']}")
            return
        except OSError:
            continue


def get_clean_nickname(user_obj) -> str:
    raw_nick = getattr(user_obj, "nickname", "") or ""
    clean_nick = re.sub(r"[^\w\s]", "", raw_nick).strip()
    return clean_nick if clean_nick else "Kak"


def get_user_unique_id(user_obj) -> str:
    return str(getattr(user_obj, "unique_id", None) or get_clean_nickname(user_obj))


def get_user_numeric_id(user_obj) -> int:
    uid = getattr(user_obj, "id", 0) or getattr(user_obj, "user_id", 0) or 0
    try:
        return int(uid)
    except (ValueError, TypeError):
        return 0


def get_user_sec_uid(user_obj) -> str:
    return str(getattr(user_obj, "sec_uid", "") or "")


def register_viewer(user_obj):
    if not user_obj:
        return
    unique_id = get_user_unique_id(user_obj)
    numeric_uid = get_user_numeric_id(user_obj)
    display_name = get_clean_nickname(user_obj)
    sec_uid = get_user_sec_uid(user_obj)

    key = unique_id.lower().strip()
    existing = viewer_registry.get(key, {})
    viewer_registry[key] = {
        "unique_id": unique_id,
        "numeric_uid": numeric_uid or existing.get("numeric_uid", 0),
        "name": display_name,
        "sec_uid": sec_uid or existing.get("sec_uid", ""),
        "last_seen": datetime.now().strftime("%H:%M"),
    }


def resolve_target_viewer(query: str) -> dict | None:
    cleaned = query.strip().lstrip("@").lower()
    if not cleaned:
        return None

    if cleaned in viewer_registry:
        return viewer_registry[cleaned]

    for info in viewer_registry.values():
        if info["name"].lower() == cleaned:
            return info

    for info in viewer_registry.values():
        if cleaned in info["unique_id"].lower() or cleaned in info["name"].lower():
            return info

    for uid, warn_info in user_warnings.items():
        if uid.lower() == cleaned or warn_info["name"].lower() == cleaned:
            return {
                "unique_id": uid,
                "numeric_uid": warn_info.get("numeric_uid", 0),
                "name": warn_info["name"],
                "sec_uid": warn_info.get("sec_uid", ""),
            }

    if cleaned.isdigit():
        return {
            "unique_id": cleaned,
            "numeric_uid": int(cleaned),
            "name": f"User_{cleaned}",
            "sec_uid": "",
        }

    return None


def load_dataset_dict() -> dict:
    if not os.path.exists(DATASET_FILE):
        return {
            "konteks_live": {},
            "daftar_badword": [],
            "banned_users": [],
            "faq": [],
            "learned_qa": [],
        }
    try:
        mtime = os.path.getmtime(DATASET_FILE)
        if _dataset_cache["data"] is not None and _dataset_cache["mtime"] == mtime:
            return _dataset_cache["data"]
        with open(DATASET_FILE, "r", encoding="utf-8") as f:
            parsed = json.load(f)
        _dataset_cache["mtime"] = mtime
        _dataset_cache["data"] = parsed
        return parsed
    except Exception:
        with open(DATASET_FILE, "r", encoding="utf-8") as f:
            return json.load(f)


def write_dataset_dict(data: dict):
    with open(DATASET_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _dataset_cache["data"] = data
    _dataset_cache["mtime"] = os.path.getmtime(DATASET_FILE)


def is_user_permanently_banned(unique_id: str, numeric_uid: int = 0) -> bool:
    data = load_dataset_dict()
    for entry in data.get("banned_users", []):
        if unique_id and entry.get("unique_id", "").lower() == unique_id.lower():
            return True
        if numeric_uid and entry.get("numeric_uid", 0) == numeric_uid:
            return True
    return False


def add_banned_user_record(numeric_uid: int, unique_id: str, display_name: str, reason: str = "Banned oleh Moderator"):
    data = load_dataset_dict()
    if "banned_users" not in data:
        data["banned_users"] = []

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    for entry in data["banned_users"]:
        if entry.get("unique_id", "").lower() == unique_id.lower():
            entry["numeric_uid"] = numeric_uid or entry.get("numeric_uid", 0)
            entry["name"] = display_name
            entry["reason"] = reason
            entry["banned_at"] = now_str
            write_dataset_dict(data)
            return

    data["banned_users"].append({
        "unique_id": unique_id,
        "numeric_uid": numeric_uid,
        "name": display_name,
        "reason": reason,
        "banned_at": now_str,
    })
    write_dataset_dict(data)


def remove_banned_user_record(query: str) -> str | None:
    data = load_dataset_dict()
    banned_list = data.get("banned_users", [])
    if not banned_list:
        return None

    cleaned = query.strip().lstrip("@").lower()
    remaining = []
    removed_name = None
    for entry in banned_list:
        if (
            entry.get("unique_id", "").lower() == cleaned
            or entry.get("name", "").lower() == cleaned
            or str(entry.get("numeric_uid", "")) == cleaned
        ):
            removed_name = f"{entry.get('name')} (@{entry.get('unique_id')})"
        else:
            remaining.append(entry)

    if removed_name:
        data["banned_users"] = remaining
        write_dataset_dict(data)
    return removed_name


def normalize_text(text: str) -> str:
    cleaned = re.sub(r"[^\w\s]", "", text.lower())
    return " ".join(cleaned.split())


def is_meta_rule_question(comment: str) -> bool:
    lower_c = comment.lower().strip()
    meta_patterns = (
        r"\bboleh\b.*\b(kasar|badword|toxic|ngomong|bilang|sebut)\b",
        r"\b(kalau|kalo|misal|misalnya|andaikata)\b.*\b(bilang|ngomong|sebut|ketik)\b",
        r"\b(apa arti|arti kata|maksud kata|maksudnya)\b",
        r"\b(kena ban|di ban|diban|di kick|dikick|di mute|dimute|melanggar)\b.*\?",
    )
    if any(re.search(pat, lower_c) for pat in meta_patterns):
        return True
    if "?" in lower_c and any(k in lower_c for k in ("boleh", "aturan", "peraturan", "filter", "kena warn")):
        return True
    return False


def check_badword_violation(comment: str) -> tuple[str, str | None]:
    data = load_dataset_dict()
    badwords = data.get("daftar_badword", [])
    if not badwords:
        return ("CLEAN", None)

    norm_comment = normalize_text(comment)
    if not norm_comment:
        return ("CLEAN", None)

    matched_word = None
    for bw in badwords:
        clean_bw = normalize_text(bw)
        if not clean_bw:
            continue
        if re.search(rf"\b{re.escape(clean_bw)}\b", norm_comment):
            matched_word = bw
            break

    if not matched_word:
        if is_meta_rule_question(comment):
            return ("META_QUESTION", None)
        return ("CLEAN", None)

    if is_meta_rule_question(comment):
        return ("META_QUESTION", matched_word)

    return ("VIOLATION", matched_word)


def find_permanent_memory_answer(comment: str) -> str | None:
    data = load_dataset_dict()
    norm_comment = normalize_text(comment)
    if not norm_comment:
        return None

    permanent_learned = [
        item for item in reversed(data.get("learned_qa", []))
        if item.get("tipe") == "permanen"
    ]
    candidates = permanent_learned + data.get("faq", [])
    best_answer = None
    best_score = 0.0

    for item in candidates:
        q_text = item.get("tanya", "")
        a_text = item.get("jawab", "").strip()
        if not q_text or not a_text:
            continue

        norm_q = normalize_text(q_text)
        if norm_comment == norm_q:
            return a_text

        score = SequenceMatcher(None, norm_comment, norm_q).ratio()
        if score > best_score:
            best_score = score
            best_answer = a_text

    if best_score >= 0.85:
        return best_answer
    return None


def classify_qa_durability(question: str, answer: str) -> str:
    temp_keywords = (
        "lagi", "sedang", "sekarang", "bentar", "testing", "tes", "coba",
        "makan", "minum", "tidur", "otw", "main", "mabar", "off", "brb", "afk"
    )
    combined = f"{question} {answer}".lower()
    if any(word in combined.split() for word in temp_keywords):
        return "sementara"

    prompt = (
        f'Pertanyaan: "{question}"\nJawaban Host: "{answer}"\n'
        'Apakah informasi jawaban ini bersifat PERMANEN (fakta tetap seperti nama, domisili, spek alat, aturan) '
        'atau SEMENTARA (kondisi/aktivitas saat ini yang bisa berubah dalam beberapa menit/jam seperti sedang testing, lagi makan, lagi main game)? '
        'Balas HANYA satu kata: PERMANEN atau SEMENTARA.'
    )

    if gemini_client:
        try:
            res = gemini_client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=prompt
            )
            ans = (res.text or "").strip().upper()
            if "SEMENTARA" in ans:
                return "sementara"
            if "PERMANEN" in ans:
                return "permanen"
        except Exception:
            pass

    return "sementara"


def save_learned_qa(question: str, answer: str, source: str = "discord_host", force_type: str | None = None):
    data = load_dataset_dict()
    if "learned_qa" not in data:
        data["learned_qa"] = []

    qa_type = force_type or classify_qa_durability(question, answer)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    norm_new = normalize_text(question)

    for item in data["learned_qa"]:
        if normalize_text(item.get("tanya", "")) == norm_new:
            item["tanya"] = question
            item["jawab"] = answer
            item["tipe"] = qa_type
            item["updated_at"] = now_str
            break
    else:
        data["learned_qa"].append({
            "tanya": question,
            "jawab": answer,
            "tipe": qa_type,
            "sumber": source,
            "created_at": now_str
        })

    if qa_type == "sementara":
        if not isinstance(data.get("konteks_live"), dict):
            data["konteks_live"] = {}
        data["konteks_live"]["status_terbaru"] = f"{question} -> {answer}"
        data["konteks_live"]["diperbarui_pada"] = now_str

    write_dataset_dict(data)
    TermUI.log("AI-MOD", f"Self-Learning ({qa_type.upper()}): '{question}' -> '{answer}'")
    return qa_type


def update_live_topic(new_topic: str):
    data = load_dataset_dict()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    if not isinstance(data.get("konteks_live"), dict):
        data["konteks_live"] = {}
    if not isinstance(data.get("profil_host"), dict):
        data["profil_host"] = {}

    data["konteks_live"]["topik_bahasan"] = new_topic
    data["konteks_live"]["diperbarui_pada"] = now_str
    data["profil_host"]["minat_topik"] = new_topic

    write_dataset_dict(data)
    recent_memory.append(f"[UPDATE TOPIK HOST ({now_str})]: {new_topic}")
    TermUI.log("AI-MOD", f"Topik bahasan diperbarui: {new_topic}")


def update_live_context(new_status: str):
    data = load_dataset_dict()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    if not isinstance(data.get("konteks_live"), dict):
        data["konteks_live"] = {}

    data["konteks_live"]["status_terbaru"] = new_status
    data["konteks_live"]["diperbarui_pada"] = now_str

    if "learned_qa" in data:
        data["learned_qa"] = [
            item for item in data["learned_qa"]
            if item.get("tipe") == "permanen"
        ]

    write_dataset_dict(data)
    recent_memory.append(f"[UPDATE KONTEKS HOST ({now_str})]: {new_status}")
    TermUI.log("AI-MOD", f"Konteks live diperbarui: {new_status}")


def forget_learned_qa(keyword: str) -> int:
    data = load_dataset_dict()
    if "learned_qa" not in data:
        return 0
    before = len(data["learned_qa"])
    kw = keyword.lower().strip()
    data["learned_qa"] = [
        item for item in data["learned_qa"]
        if kw not in item.get("tanya", "").lower() and kw not in item.get("jawab", "").lower()
    ]
    removed = before - len(data["learned_qa"])
    if removed > 0:
        write_dataset_dict(data)
    return removed


def add_badword_to_dataset(word: str) -> bool:
    data = load_dataset_dict()
    if "daftar_badword" not in data:
        data["daftar_badword"] = []
    clean_w = normalize_text(word)
    if not clean_w:
        return False
    existing = {normalize_text(w) for w in data["daftar_badword"]}
    if clean_w in existing:
        return False
    data["daftar_badword"].append(clean_w)
    write_dataset_dict(data)
    return True


def remove_badword_from_dataset(word: str) -> bool:
    data = load_dataset_dict()
    if "daftar_badword" not in data:
        return False
    clean_w = normalize_text(word)
    before = len(data["daftar_badword"])
    data["daftar_badword"] = [
        w for w in data["daftar_badword"]
        if normalize_text(w) != clean_w
    ]
    if len(data["daftar_badword"]) < before:
        write_dataset_dict(data)
        return True
    return False


def parse_agent_json(raw_text: str) -> dict:
    cleaned = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()
    if not cleaned:
        raise ValueError("Respons LLM kosong setelah pembersihan")

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(cleaned[start:end + 1])
            if isinstance(parsed, dict) and "action" in parsed:
                return parsed
        except json.JSONDecodeError:
            pass

    return {
        "intent": "obrolan_umum",
        "action": "CHAT_AI",
        "thought": "Ekstraksi teks langsung",
        "reply_text": cleaned
    }


def run_moderator_reasoning(user_id: str, user_name: str, comment: str) -> dict:
    data = load_dataset_dict()
    filtered_data = {
        k: v for k, v in data.items()
        if k not in ("daftar_badword", "banned_users", "kapabilitas_berpikir")
    }
    dataset_str = json.dumps(filtered_data, ensure_ascii=False, separators=(",", ":"))
    history_str = "\n".join(recent_memory) if recent_memory else "-"
    warning_count = user_warnings.get(user_id, {}).get("count", 0)
    waktu_sekarang = datetime.now().strftime("%H:%M WIB")

    konteks_live = data.get("konteks_live", {})
    topik_aktif = konteks_live.get("topik_bahasan") or data.get("profil_host", {}).get("minat_topik") or "Obrolan santai"
    status_aktif = konteks_live.get("status_terbaru") or "Live santai"

    system_prompt = (
        f"Kamu adalah Mina, AI Co-Host & Moderator TikTok Live yang cerdas, ekspresif, dan cepat tanggap.\n"
        f"Waktu: {waktu_sekarang} | Topik: {topik_aktif} | Situasi: {status_aktif} | Warn {user_name}: {warning_count}/3\n"
        f"Data: {dataset_str}\n"
        f"Riwayat: {history_str}\n"
        "Aturan:\n"
        "- Jika penonton bertanya soal aturan secara hipotetis (misal 'boleh kasar ga?'), pilih CHAT_AI (jangan WARN) dan jelaskan santai.\n"
        "- Aksi: CHAT_AI (obrolan/tanya jawab/diskusi), HYPE (sambutan/support), WARN (penghinaan/SARA/spam/promosi ilegal nyata), DEFLECT (tangkis info privat host), ESCALATE (kendala teknis live/pertanyaan real-time wajib host), IGNORE (ketikan acak 1-2 huruf tanpa makna).\n"
        "- reply_text: Bahasa Indonesia lisan natural tanpa emoji/simbol markdown. Singkat padat (1-3 kalimat, maks 40 kata).\n"
        'Balas JSON murni: {"intent":"...","action":"CHAT_AI|HYPE|WARN|DEFLECT|ESCALATE|IGNORE","thought":"1 kalimat singkat","reply_text":"..."}'
    )
    user_prompt = f'{user_name}: "{comment}"'

    if gemini_client:
        for model_name in ["gemini-3.5-flash-lite", "gemini-3.8-flash"]:
            try:
                res = gemini_client.models.generate_content(
                    model=model_name,
                    contents=f"{system_prompt}\n{user_prompt}",
                    config=types.GenerateContentConfig(
                        temperature=0.6,
                        max_output_tokens=1024,
                        response_mime_type="application/json",
                    ),
                )
                raw_content = (res.text or "").strip()
                if not raw_content:
                    raise ValueError(f"{model_name} konten kosong")
                return parse_agent_json(raw_content)
            except Exception as e:
                TermUI.log("WARN", f"Gemini ({model_name}) fallback: {e}")

    if openrouter_client:
        try:
            res = openrouter_client.chat.completions.create(
                model="openrouter/free",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.6,
                max_tokens=1024
            )
            raw_content = (res.choices[0].message.content or "").strip()
            if not raw_content:
                raise ValueError("openrouter/free konten kosong")
            return parse_agent_json(raw_content)
        except Exception as e:
            TermUI.log("ERROR", f"OpenRouter gagal: {e}")

    raise RuntimeError("Semua provider LLM gagal merespons.")


def transcribe_voice_note(audio_bytes: bytes, mime_type: str) -> str:
    if not gemini_client:
        raise RuntimeError("GEMINI_API_KEY belum terpasang untuk transkripsi Voice Note.")

    clean_mime = mime_type.split(";")[0].strip() or "audio/ogg"
    prompt = (
        "Transkripsikan rekaman suara bahasa Indonesia ini menjadi satu paragraf kalimat yang natural dan rapi. "
        "Jangan tambahkan tanda kutip, label pembicara, atau penjelasan apa pun. "
        "Jika suara tidak terdengar atau kosong, balas persis dengan: [TIDAK_JELAS]"
    )

    for model_name in ["gemini-3.5-flash-lite", "gemini-3.8-flash"]:
        for attempt in range(2):
            try:
                res = gemini_client.models.generate_content(
                    model=model_name,
                    contents=[
                        prompt,
                        types.Part.from_bytes(data=audio_bytes, mime_type=clean_mime)
                    ]
                )
                return (res.text or "").strip()
            except Exception as e:
                TermUI.log("WARN", f"STT {model_name} percobaan {attempt + 1} gagal: {e}")
                time.sleep(0.6)

    raise RuntimeError("Gagal mentranskripsi Voice Note karena server Gemini sibuk.")


def normalize_for_speech(text: str) -> str:
    cleaned = text.replace("\n", ", ").replace("/", " atau ").replace("*", "")
    return " ".join(cleaned.split())


def split_sentences_for_tts(text: str) -> list[str]:
    speech_text = normalize_for_speech(text)
    if not speech_text:
        return []
    raw_parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", speech_text) if p.strip()]
    if len(raw_parts) <= 1:
        return raw_parts

    merged = []
    for part in raw_parts:
        if merged and len(part) < 14:
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged


async def generate_neural_edgetts(text: str) -> io.BytesIO:
    communicate = edge_tts.Communicate(text, NEURAL_VOICE, rate="+15%", pitch="+2Hz")
    audio_stream = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_stream.write(chunk["data"])
    audio_stream.seek(0)
    return audio_stream


def generate_backup_gtts(text: str) -> io.BytesIO:
    tts = gTTS(text=text, lang="id", slow=False)
    audio_stream = io.BytesIO()
    tts.write_to_fp(audio_stream)
    audio_stream.seek(0)
    return audio_stream


async def synthesize_chunk_fast(sentence: str) -> io.BytesIO:
    try:
        stream = await asyncio.wait_for(generate_neural_edgetts(sentence), timeout=1.35)
        if stream.getbuffer().nbytes > 0:
            return stream
    except Exception:
        pass
    return await asyncio.to_thread(generate_backup_gtts, sentence)


async def speak_to_vbcable(text: str):
    sentences = split_sentences_for_tts(text)
    if not sentences:
        return

    t_tts = time.time()
    synth_tasks = [asyncio.create_task(synthesize_chunk_fast(s)) for s in sentences]

    for idx, task in enumerate(synth_tasks):
        audio_stream = await task
        if idx == 0:
            TermUI.log("VOICE", f"Suara mulai diputar (TTFA: {time.time() - t_tts:.2f}s | {len(sentences)} segmen)")
        pygame.mixer.music.load(audio_stream, "mp3")
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            await asyncio.sleep(0.02)
        pygame.mixer.music.unload()


def generate_autoplay_next_song(seed_query: str, last_played: str | None, history: list[str]) -> str:
    history_text = ", ".join(history[-15:]) if history else "Belum ada"
    last_info = f' (baru saja memutar: "{last_played}")' if last_played else ""

    prompt = (
        f'Lagu acuan terakhir dari pengguna: "{seed_query}"{last_info}.\n'
        f'Daftar lagu yang SUDAH diputar (JANGAN pilih lagu yang sama atau mirip judulnya dengan daftar ini): [{history_text}].\n\n'
        'Tugas: Rekomendasikan SATU lagu tunggal (bukan full album, kompilasi, atau playlist 1 jam) yang memiliki vibes, genre, tempo, dan karakter musisi yang sangat dekat dengan lagu acuan di atas. '
        'Jika lagu acuan adalah musisi indie/rock/pop Indonesia (misalnya .Feast, Hindia, Perunggu, Lomba Sihir, Reality Club, Nadin Amizah, Pamungkas, Sal Priadi, Tulus, dan sejenisnya), pilih lagu lain dari musisi tersebut atau musisi Indonesia lain dengan vibes yang sama. '
        'Jika lagu acuan berbahasa Inggris/Jepang/instrumen/lofi, sesuaikan dengan artis dan vibes yang sejenis.\n'
        'Balas HANYA dengan format: Judul Lagu - Nama Artis (tanpa tanda kutip, tanpa nomor, tanpa penjelasan).'
    )

    candidate = ""
    if gemini_client:
        for model_name in ["gemini-3.5-flash-lite", "gemini-3.8-flash"]:
            try:
                res = gemini_client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                raw = (res.text or "").strip()
                raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
                first_line = raw.splitlines()[0].strip().strip('"\'*-.0123456789 ')
                if first_line:
                    candidate = first_line
                    break
            except Exception as e:
                TermUI.log("WARN", f"Autoplay LLM {model_name} gagal: {e}")

    if not candidate and openrouter_client:
        try:
            res = openrouter_client.chat.completions.create(
                model="openrouter/free",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.8,
                max_tokens=512
            )
            raw = (res.choices[0].message.content or "").strip()
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
            first_line = raw.splitlines()[0].strip().strip('"\'*-.0123456789 ')
            if first_line:
                candidate = first_line
        except Exception as e:
            TermUI.log("ERROR", f"Autoplay OpenRouter error: {e}")

    if not candidate:
        candidate = f"{seed_query} official audio"

    return candidate


def find_ffmpeg_executable() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found

    local_candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe"),
        os.path.join(os.getcwd(), "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ]
    for path in local_candidates:
        if os.path.exists(path):
            return path

    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass

    raise FileNotFoundError(
        "ffmpeg.exe tidak ditemukan! Taruh ffmpeg.exe di folder yang sama dengan main.py atau jalankan: pip install imageio-ffmpeg"
    )


def fetch_ytdlp_metadata(song_query: str) -> dict:
    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(song_query, download=False)
        if "entries" in info and info["entries"]:
            info = info["entries"][0]
        raw_dur = int(info.get("duration") or 210)
        capped_dur = min(420, max(15, raw_dur))
        return {
            "title": info.get("title") or song_query,
            "duration": capped_dur,
            "stream_url": info.get("url") or "",
            "http_headers": info.get("http_headers") or {},
            "resolved_at": time.time(),
        }


async def resolve_song_metadata_async(song_item: dict):
    try:
        meta = await asyncio.to_thread(fetch_ytdlp_metadata, song_item["query"])
        song_item["title"] = meta["title"]
        song_item["duration"] = meta["duration"]
        song_item["stream_url"] = meta["stream_url"]
        song_item["http_headers"] = meta.get("http_headers") or {}
        song_item["resolved_at"] = meta["resolved_at"]
        broadcast_queue_update()
    except Exception:
        pass


def prepare_song_wav(song_item: dict) -> tuple[str, int, str]:
    ffmpeg_bin = find_ffmpeg_executable()
    song_query = song_item["query"]
    stream_url = song_item.get("stream_url") or ""
    title = song_item.get("title") or song_query
    duration = int(song_item.get("duration") or 210)
    http_headers = song_item.get("http_headers") or {}
    resolved_at = float(song_item.get("resolved_at") or 0.0)

    if not stream_url or (time.time() - resolved_at) > 600:
        meta = fetch_ytdlp_metadata(song_query)
        stream_url = meta["stream_url"]
        title = meta["title"]
        duration = meta["duration"]
        http_headers = meta.get("http_headers") or {}

    tmp_fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="mina_bgm_")
    os.close(tmp_fd)

    cmd = [ffmpeg_bin, "-y"]
    if http_headers:
        header_str = "".join(f"{k}: {v}\r\n" for k, v in http_headers.items())
        cmd.extend(["-headers", header_str])

    cmd.extend([
        "-i",
        stream_url,
        "-t",
        "420",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-f",
        "wav",
        wav_path,
    ])
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=45)
    return title, duration, wav_path


async def music_player_worker():
    TermUI.log("MUSIC", "Pemutar musik latar VB-Cable + Autoplay & SSE Overlay aktif.")
    broadcast_queue_update()
    while True:
        is_autoplay_track = False
        song_item = None

        if music_pending_items:
            music_state["stop_flag"] = False
            song_item = music_pending_items.pop(0)
            if not music_pending_items:
                music_queue_event.clear()
            broadcast_queue_update()
        else:
            music_queue_event.clear()
            try:
                await asyncio.wait_for(music_queue_event.wait(), timeout=1.5)
                continue
            except asyncio.TimeoutError:
                if (
                    not music_state["autoplay"]
                    or music_state["stop_flag"]
                    or not music_state["last_seed_query"]
                ):
                    continue

                seed_now = music_state["last_seed_query"]
                last_now = music_state["last_played_title"]
                TermUI.log("MUSIC", f"Autoplay mencari lagu se-vibe dengan '{seed_now}'...")
                next_song = await asyncio.to_thread(
                    generate_autoplay_next_song,
                    seed_now,
                    last_now,
                    list(played_music_history),
                )

                if music_pending_items or music_state["stop_flag"] or not music_state["autoplay"]:
                    continue

                song_item = {
                    "uid": next(song_id_counter),
                    "query": next_song,
                    "title": next_song,
                    "requester": "Autoplay AI",
                    "duration": 210,
                    "stream_url": "",
                    "http_headers": {},
                    "resolved_at": 0.0,
                }
                is_autoplay_track = True

        requester_name = song_item["requester"]
        song_query = song_item["query"]
        wav_path = None
        try:
            if music_state["stop_flag"]:
                music_state["stop_flag"] = False
                continue

            TermUI.log("MUSIC", f"Memuat audio: {song_query} (Req: {requester_name})...")
            title, duration, wav_path = await asyncio.to_thread(prepare_song_wav, song_item)

            if is_autoplay_track and music_pending_items:
                TermUI.log("MUSIC", "Request baru masuk saat Autoplay memuat, langsung mendahulukan request...")
                continue

            if music_state["stop_flag"]:
                music_state["stop_flag"] = False
                continue

            song_item["title"] = title
            song_item["duration"] = duration
            played_music_history.append(song_query)
            if title.lower() != song_query.lower():
                played_music_history.append(title)
            music_state["last_played_title"] = title

            sound = pygame.mixer.Sound(wav_path)
            actual_dur = int(sound.get_length()) or duration
            song_item["duration"] = actual_dur

            ch = music_state["channel"] or pygame.mixer.Channel(1)
            music_state["channel"] = ch
            music_state["current"] = f"{title} ({requester_name})"
            music_state["current_item"] = song_item
            music_state["is_autoplay_playing"] = is_autoplay_track
            music_state["started_at"] = time.time()
            music_state["duration"] = actual_dur
            music_state["skip_flag"] = False
            broadcast_queue_update()

            ch.set_volume(music_state["volume"])
            ch.play(sound)
            TermUI.log(
                "MUSIC",
                f"Playing: {title} [{format_duration_mmss(actual_dur)}] | Vol: {int(music_state['volume'] * 100)}%",
                TermUI.GREEN,
            )

            channel_dc = await get_control_channel()
            if channel_dc:
                if is_autoplay_track:
                    await channel_dc.send(
                        f"🎶 **Autoplay Memutar:** `{title}` *(Vibes: {music_state['last_seed_query']})*"
                    )
                else:
                    await channel_dc.send(
                        f"🎵 **Memutar Lagu:** `{title}` (Request oleh **{requester_name}**)"
                    )

            while ch.get_busy():
                if is_autoplay_track and music_pending_items:
                    music_state["skip_flag"] = True
                if music_state["skip_flag"] or music_state["stop_flag"]:
                    ch.stop()
                    break
                ch.set_volume(music_state["volume"])
                await asyncio.sleep(0.2)

        except Exception as e:
            TermUI.log("ERROR", f"Gagal memutar '{song_query}': {e}")
            played_music_history.append(song_query)
            channel_dc = await get_control_channel()
            if channel_dc and not is_autoplay_track:
                await channel_dc.send(f"⚠️ Gagal memuat lagu `{song_query}`: `{e}`")
            await asyncio.sleep(1.0)
        finally:
            music_state["current"] = None
            music_state["current_item"] = None
            music_state["is_autoplay_playing"] = False
            music_state["started_at"] = 0.0
            music_state["duration"] = 0
            music_state["skip_flag"] = False
            broadcast_queue_update()
            if wav_path and os.path.exists(wav_path):
                try:
                    os.remove(wav_path)
                except Exception:
                    pass


def ensure_music_worker_running():
    global music_worker_task
    if music_worker_task is None or music_worker_task.done():
        music_worker_task = asyncio.create_task(music_player_worker())


def enqueue_song(requester_name: str, song_query: str):
    ensure_music_worker_running()
    music_state["stop_flag"] = False
    music_state["last_seed_query"] = song_query
    song_item = {
        "uid": next(song_id_counter),
        "query": song_query,
        "title": song_query,
        "requester": requester_name,
        "duration": 210,
        "stream_url": "",
        "http_headers": {},
        "resolved_at": 0.0,
    }
    music_pending_items.append(song_item)
    if music_state.get("is_autoplay_playing"):
        music_state["skip_flag"] = True
        TermUI.log("MUSIC", "Memotong lagu Autoplay demi memutar request baru...", TermUI.CYAN)
    music_queue_event.set()
    broadcast_queue_update()
    asyncio.create_task(resolve_song_metadata_async(song_item))


async def execute_tiktok_moderation(
    mode: str,
    numeric_uid: int,
    unique_id: str,
    display_name: str,
    sec_uid: str = "",
    reason: str = "Pelanggaran aturan live",
    silent: bool = False,
):
    channel = await get_control_channel()
    room_id = getattr(tiktok_client, "room_id", 0) or 0
    mode = mode.lower().strip()

    if mode == "ban":
        add_banned_user_record(numeric_uid, unique_id, display_name, reason)

    if not tiktok_session_id:
        if channel and not silent:
            await channel.send(
                f"🚨 **AKSI `{mode.upper()}` UNTUK {display_name} (`@{unique_id}`):**\n"
                f"⚠️ Eksekusi API otomatis dilewati karena `TIKTOK_SESSION_ID` belum terpasang di `.env`. "
                f"Silakan lakukan `{mode}` secara manual di layar TikTok Live."
            )
        return

    if not room_id or not numeric_uid:
        if channel and not silent:
            await channel.send(
                f"⚠️ Tidak dapat mengeksekusi `{mode.upper()}` ke API TikTok untuk **{display_name}** (`@{unique_id}`) "
                f"karena `room_id` atau `numeric_uid` tidak ditemukan."
            )
        return

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": f"https://www.tiktok.com/@{tiktok_username}/live",
        "Origin": "https://www.tiktok.com",
    }
    cookies = {"sessionid": tiktok_session_id}
    if tiktok_target_idc:
        cookies["tt-target-idc"] = tiktok_target_idc

    kick_url = "https://webcast.tiktok.com/webcast/room/kick_user/"
    mute_url = "https://webcast.tiktok.com/webcast/room/mute_user/"
    block_url = "https://webcast.tiktok.com/webcast/user/relation/update/"

    try:
        async with httpx.AsyncClient(headers=headers, cookies=cookies, timeout=8.0) as client:
            if mode == "mute":
                resp = await client.post(
                    mute_url,
                    params={"aid": 1988, "room_id": room_id, "user_id": numeric_uid, "mute_type": 1, "duration": 300},
                    data={"room_id": room_id, "user_id": numeric_uid, "mute_type": 1, "duration": 300},
                )
                res_json = resp.json() if resp.status_code == 200 else {}
                if res_json.get("status_code") == 0:
                    TermUI.log("AI-MOD", f"MUTE 5m berhasil: {display_name} (@{unique_id})", TermUI.YELLOW)
                    if channel and not silent:
                        await channel.send(
                            f"🔇 **MUTE BERHASIL:** **{display_name}** (`@{unique_id}`) dibisukan dari komentar selama 5 menit."
                        )
                    return
                err_msg = res_json.get("data", {}).get("prompts") or res_json.get("status_msg") or f"HTTP {resp.status_code}"
                if channel and not silent:
                    await channel.send(f"⚠️ Server TikTok menolak `MUTE` untuk **{display_name}** (`{err_msg}`). Silakan mute manual.")

            elif mode == "kick":
                resp = await client.post(
                    kick_url,
                    params={"aid": 1988, "room_id": room_id, "kick_uid": numeric_uid},
                    data={"room_id": room_id, "kick_uid": numeric_uid},
                )
                res_json = resp.json() if resp.status_code == 200 else {}
                if res_json.get("status_code") == 0:
                    TermUI.log("AI-MOD", f"KICK berhasil: {display_name} (@{unique_id})", TermUI.RED)
                    if channel and not silent:
                        await channel.send(
                            f"👢 **KICK BERHASIL:** **{display_name}** (`@{unique_id}`) telah dikeluarkan dari TikTok Live."
                        )
                    return
                err_msg = res_json.get("data", {}).get("prompts") or res_json.get("status_msg") or f"HTTP {resp.status_code}"
                if channel and not silent:
                    await channel.send(f"⚠️ Server TikTok menolak `KICK` untuk **{display_name}** (`{err_msg}`). Silakan kick manual.")

            elif mode == "ban":
                block_payload = {"target_uid": numeric_uid, "op": 1}
                if sec_uid:
                    block_payload["sec_target_uid"] = sec_uid

                block_resp = await client.post(
                    block_url,
                    params={"aid": 1988, **block_payload},
                    data=block_payload,
                )
                block_json = block_resp.json() if block_resp.status_code == 200 else {}
                block_ok = block_json.get("status_code") == 0

                kick_resp = await client.post(
                    kick_url,
                    params={"aid": 1988, "room_id": room_id, "kick_uid": numeric_uid},
                    data={"room_id": room_id, "kick_uid": numeric_uid},
                )
                kick_json = kick_resp.json() if kick_resp.status_code == 200 else {}
                kick_ok = kick_json.get("status_code") == 0

                if block_ok or kick_ok:
                    detail = "Blokir Permanen & Kick Room" if (block_ok and kick_ok) else ("Blokir Permanen" if block_ok else "Kick Room + Ban Lokal Aktif")
                    TermUI.log("AI-MOD", f"BAN berhasil: {display_name} (@{unique_id}) -> {detail}", TermUI.RED)
                    if channel and not silent:
                        await channel.send(
                            f"⛔ **PERMANENT BAN BERHASIL:** **{display_name}** (`@{unique_id}`) "
                            f"telah diblokir (`{detail}`) dan dikunci di daftar `banned_users`."
                        )
                    return

                err_msg = kick_json.get("data", {}).get("prompts") or block_json.get("status_msg") or f"HTTP {kick_resp.status_code}"
                if channel and not silent:
                    await channel.send(
                        f"⛔ **PERMANENT BAN TERCATAT (LOKAL):** **{display_name}** (`@{unique_id}`)\n"
                        f"⚠️ Server TikTok menolak blokir API (`{err_msg}`). Akun sudah dikunci di `banned_users` lokal, silakan tap blokir permanen di layar live."
                    )

    except Exception as e:
        TermUI.log("ERROR", f"Moderation API error: {e}")
        if channel and not silent:
            await channel.send(f"⚠️ Error koneksi saat eksekusi `{mode.upper()}` untuk **{display_name}**: `{e}`")


def build_status_text() -> str:
    data = load_dataset_dict()
    learned_count = len(data.get("learned_qa", []))
    badword_count = len(data.get("daftar_badword", []))
    banned_count = len(data.get("banned_users", []))
    konteks = data.get("konteks_live", {})
    topik_live = konteks.get("topik_bahasan", "Umum")
    status_live = konteks.get("status_terbaru", "Default")
    aktif_esc = last_escalated_question["question"] or "Tidak ada"
    lagu_aktif = music_state["current"] or "Tidak ada"
    seed_vibes = music_state["last_seed_query"] or "Belum ada"
    autoplay_str = f"ON (Vibes: {seed_vibes})" if music_state["autoplay"] else "OFF"
    idc_label = tiktok_target_idc or "alisg"
    return (
        f"**Status Moderator AI**\n"
        f"- TikTok Live: `{'Terhubung' if state['tiktok_connected'] else 'Terputus'}`\n"
        f"- Sesi Kick/Mute/Ban API: `{f'Aktif (IDC: {idc_label})' if tiktok_session_id else 'Mode Alert Manual'}`\n"
        f"- Musik Latar (VB-Cable): `{lagu_aktif}` | Vol: `{int(music_state['volume'] * 100)}%`\n"
        f"- Autoplay Musik: `{autoplay_str}` | Antrean: `{len(music_pending_items)} lagu`\n"
        f"- Live Queue Overlay: `{music_state['overlay_url']}` (SSE Real-Time)\n"
        f"- Suara AI: `{'MUTE' if state['muted'] else 'AKTIF'}`\n"
        f"- Topik Bahasan Utama: `{topik_live}`\n"
        f"- Konteks Live Terbaru: `{status_live}`\n"
        f"- Total Badword: `{badword_count}` | Learned QA: `{learned_count}` | Banned: `{banned_count}`\n"
        f"- Total Akun Terkena Warn: `{len(user_warnings)}`\n"
        f"- Eskalasi Menunggu: `{aktif_esc}`"
    )


def build_warn_list_embed(title: str = "Daftar Pelanggaran Penonton (Warn List)") -> discord.Embed:
    embed = discord.Embed(
        title=title,
        color=discord.Color.red() if user_warnings else discord.Color.green(),
        timestamp=datetime.now()
    )
    if not user_warnings:
        embed.description = "Belum ada penonton yang tercatat melanggar aturan room."
        return embed

    sorted_warns = sorted(
        user_warnings.items(),
        key=lambda item: item[1]["count"],
        reverse=True
    )
    lines = []
    for idx, (uid, info) in enumerate(sorted_warns, start=1):
        last_reason = info["reasons"][-1] if info["reasons"] else "-"
        status_tag = f" [{info['punished']}]" if info.get("punished") else ""
        lines.append(
            f"**{idx}. {info['name']}** (`@{uid}`) — **{info['count']}/3 Strike**{status_tag}\n"
            f"   └ Pelanggaran terakhir ({info['last_time']}): *\"{last_reason}\"*"
        )

    embed.description = "\n".join(lines[:15])
    embed.set_footer(text=f"Total pelanggar tercatat: {len(user_warnings)} akun")
    return embed


def build_ban_list_embed() -> discord.Embed:
    data = load_dataset_dict()
    banned_list = data.get("banned_users", [])
    embed = discord.Embed(
        title="Daftar Akun Permanent Ban",
        color=discord.Color.dark_red() if banned_list else discord.Color.green(),
        timestamp=datetime.now()
    )
    if not banned_list:
        embed.description = "Belum ada akun di daftar permanent ban."
        return embed

    lines = []
    for idx, entry in enumerate(banned_list[-15:], start=1):
        lines.append(
            f"**{idx}. {entry.get('name')}** (`@{entry.get('unique_id')}`)\n"
            f"   └ Alasan ({entry.get('banned_at', '-')}): *{entry.get('reason', '-')}*"
        )
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Total akun ter-ban permanen: {len(banned_list)}")
    return embed


class QueueDeleteSelect(discord.ui.Select):
    def __init__(self, items: list[dict]):
        options = []
        for idx, item in enumerate(items[:15], start=1):
            label = f"#{idx}. {(item.get('title') or item.get('query') or '-')[:75]}"
            desc = f"Req: {item.get('requester') or 'Viewer'}"[:90]
            options.append(
                discord.SelectOption(
                    label=label,
                    description=desc,
                    value=str(item.get("uid")),
                    emoji="🗑️",
                )
            )
        super().__init__(
            placeholder="Pilih antrean lagu yang ingin dihapus (bisa pilih lebih dari 1)...",
            min_values=1,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.channel_id != control_channel_id:
            return
        removed_names = []
        for val in self.values:
            rem = remove_queued_song_by_uid(int(val))
            if rem:
                removed_names.append(f"`{rem.get('title')}`")

        if removed_names:
            await interaction.response.send_message(
                f"🗑️ Dihapus dari antrean: {', '.join(removed_names)}"
            )
        else:
            await interaction.response.send_message(
                "Lagu yang dipilih sudah diputar atau sudah dihapus sebelumnya.",
                ephemeral=True,
            )


class QueueManageView(discord.ui.View):
    def __init__(self, items: list[dict]):
        super().__init__(timeout=600)
        if items:
            self.add_item(QueueDeleteSelect(items))


class ViewerModerationView(discord.ui.View):
    def __init__(
        self,
        numeric_uid: int,
        unique_id: str,
        display_name: str,
        sec_uid: str = "",
        show_skip: bool = False,
        show_reset_warn: bool = False,
    ):
        super().__init__(timeout=7200)
        self.numeric_uid = numeric_uid
        self.unique_id = unique_id
        self.display_name = display_name
        self.sec_uid = sec_uid

        if not show_skip:
            self.remove_item(self.btn_skip)
        if not show_reset_warn:
            self.remove_item(self.btn_reset_warn)

    async def _trigger_mod(self, interaction: discord.Interaction, mode: str, label: str):
        if interaction.channel_id != control_channel_id:
            return
        enqueue_action(
            PRIORITY_HOST,
            "MANUAL_MOD",
            (mode, self.numeric_uid, self.unique_id, self.display_name, self.sec_uid),
        )
        await interaction.response.send_message(
            f"⚡ Eksekusi **{label}** dikirim untuk **{self.display_name}** (`@{self.unique_id}`)."
        )

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭️", row=0)
    async def btn_skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.channel_id != control_channel_id:
            return
        last_escalated_question["question"] = None
        button.disabled = True
        button.label = "Skipped"
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Mute 5m", style=discord.ButtonStyle.primary, emoji="🔇", row=0)
    async def btn_mute(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._trigger_mod(interaction, "mute", "MUTE 5 MENIT")

    @discord.ui.button(label="Kick", style=discord.ButtonStyle.danger, emoji="👢", row=0)
    async def btn_kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._trigger_mod(interaction, "kick", "KICK")

    @discord.ui.button(label="Ban Permanen", style=discord.ButtonStyle.danger, emoji="⛔", row=0)
    async def btn_ban(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._trigger_mod(interaction, "ban", "PERMANENT BAN")

    @discord.ui.button(label="Reset Strike", style=discord.ButtonStyle.success, emoji="🧹", row=1)
    async def btn_reset_warn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.channel_id != control_channel_id:
            return
        removed = user_warnings.pop(self.unique_id, None)
        button.disabled = True
        button.label = "Strike Direset"
        await interaction.response.edit_message(view=self)
        if removed:
            await interaction.followup.send(
                f"🧹 Catatan peringatan untuk **{self.display_name}** (`@{self.unique_id}`) telah dihapus."
            )


class ControlPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self._sync_buttons()

    def _sync_buttons(self):
        if state["muted"]:
            self.btn_toggle_mute.label = "Unmute Suara AI"
            self.btn_toggle_mute.style = discord.ButtonStyle.success
            self.btn_toggle_mute.emoji = "🔊"
        else:
            self.btn_toggle_mute.label = "Mute Suara AI"
            self.btn_toggle_mute.style = discord.ButtonStyle.secondary
            self.btn_toggle_mute.emoji = "🔇"

        if music_state["autoplay"]:
            self.btn_toggle_autoplay.label = "Autoplay: ON"
            self.btn_toggle_autoplay.style = discord.ButtonStyle.success
            self.btn_toggle_autoplay.emoji = "🔁"
        else:
            self.btn_toggle_autoplay.label = "Autoplay: OFF"
            self.btn_toggle_autoplay.style = discord.ButtonStyle.secondary
            self.btn_toggle_autoplay.emoji = "⏹️"

    @discord.ui.button(label="Mute Suara AI", style=discord.ButtonStyle.secondary, emoji="🔇", row=0)
    async def btn_toggle_mute(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.channel_id != control_channel_id:
            return
        state["muted"] = not state["muted"]
        self._sync_buttons()
        await interaction.response.edit_message(view=self)
        status_msg = "Moderator AI di-**MUTE** sementara." if state["muted"] else "Moderator AI kembali **AKTIF**."
        await interaction.followup.send(status_msg, ephemeral=True)

    @discord.ui.button(label="Status", style=discord.ButtonStyle.primary, emoji="📊", row=0)
    async def btn_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.channel_id != control_channel_id:
            return
        await interaction.response.send_message(build_status_text())

    @discord.ui.button(label="Skip Lagu", style=discord.ButtonStyle.secondary, emoji="⏩", row=0)
    async def btn_skip_music(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.channel_id != control_channel_id:
            return
        music_state["skip_flag"] = True
        await interaction.response.send_message("⏩ Lagu saat ini dilewati.")

    @discord.ui.button(label="Autoplay: ON", style=discord.ButtonStyle.success, emoji="🔁", row=1)
    async def btn_toggle_autoplay(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.channel_id != control_channel_id:
            return
        music_state["autoplay"] = not music_state["autoplay"]
        broadcast_queue_update()
        self._sync_buttons()
        await interaction.response.edit_message(view=self)
        msg = "🔁 **Autoplay Musik: AKTIF** (otomatis memutar lagu se-vibe saat antrean habis)." if music_state["autoplay"] else "⏹️ **Autoplay Musik: NONAKTIF**."
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Warn List", style=discord.ButtonStyle.danger, emoji="⚠️", row=1)
    async def btn_warnlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.channel_id != control_channel_id:
            return
        await interaction.response.send_message(embed=build_warn_list_embed())

    @discord.ui.button(label="Ban List", style=discord.ButtonStyle.danger, emoji="⛔", row=1)
    async def btn_banlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.channel_id != control_channel_id:
            return
        await interaction.response.send_message(embed=build_ban_list_embed())


async def handle_user_strike(
    numeric_uid: int,
    unique_id: str,
    display_name: str,
    comment: str,
    matched_bw: str | None = None,
    sec_uid: str = "",
):
    now_str = datetime.now().strftime("%H:%M")
    if unique_id not in user_warnings:
        user_warnings[unique_id] = {
            "name": display_name,
            "numeric_uid": numeric_uid,
            "sec_uid": sec_uid,
            "count": 0,
            "reasons": [],
            "last_time": now_str,
            "punished": "",
        }

    info = user_warnings[unique_id]
    info["name"] = display_name
    if numeric_uid:
        info["numeric_uid"] = numeric_uid
    if sec_uid:
        info["sec_uid"] = sec_uid
    info["count"] += 1
    reason_label = f"{comment} [kata: {matched_bw}]" if matched_bw else comment
    info["reasons"].append(reason_label)
    info["last_time"] = now_str

    strike_num = info["count"]
    data = load_dataset_dict()
    strike_3_action = data.get("protokol_moderasi", {}).get("tindakan_otomatis_strike_3", "ban").lower()
    if strike_3_action not in ("kick", "mute", "ban"):
        strike_3_action = "ban"

    TermUI.log("WARN", f"Strike {strike_num}/3 -> {display_name} (@{unique_id}) | {reason_label}", TermUI.RED)

    if strike_num == 1:
        voice_msg = f"Peringatan pertama buat Kak {display_name}, tolong jaga ketikan di kolom chat ya, kata kasar dilarang di sini."
    elif strike_num == 2:
        voice_msg = f"Peringatan kedua buat Kak {display_name}! Satu kali lagi melanggar aturan room, akun kamu otomatis aku tindak tegas."
    else:
        info["punished"] = strike_3_action.upper()
        if strike_3_action == "ban":
            voice_msg = f"Sudah tiga kali melanggar aturan room, maaf Kak {display_name}, akun kamu aku blokir permanen dari live ini."
        elif strike_3_action == "mute":
            voice_msg = f"Sudah tiga kali melanggar aturan room, Kak {display_name} aku bisukan sementara dari kolom komentar."
        else:
            voice_msg = f"Sudah tiga kali melanggar aturan room, maaf Kak {display_name}, akun kamu aku keluarkan dari live sekarang."

    recent_memory.append(f"{display_name}: {comment} -> Mod (STRIKE {strike_num}/3): {voice_msg}")
    asyncio.create_task(
        send_warn_list_to_discord(
            display_name,
            strike_num,
            target_info=(info["numeric_uid"], unique_id, display_name, info.get("sec_uid", "")),
        )
    )

    await speak_to_vbcable(voice_msg)

    if strike_num >= 3:
        await execute_tiktok_moderation(
            strike_3_action,
            info["numeric_uid"],
            unique_id,
            display_name,
            info.get("sec_uid", ""),
            reason=f"3x Strike ({reason_label})",
        )


async def get_control_channel():
    if not control_channel_id:
        return None
    channel = discord_client.get_channel(control_channel_id)
    if channel is None:
        try:
            channel = await discord_client.fetch_channel(control_channel_id)
        except Exception as e:
            TermUI.log("ERROR", f"Gagal mengambil channel Discord {control_channel_id}: {e}")
            return None
    return channel


async def send_startup_command_guide():
    channel = await get_control_channel()
    if not channel:
        return

    embed = discord.Embed(
        title="Control Panel AI Moderator Siap",
        description="Gunakan tombol di bawah atau kirim Voice Note / perintah teks untuk mengendalikan live:",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    embed.add_field(
        name="Tema, Konteks & Suara Langsung",
        value=(
            "- `!topik <tema live>` : Ubah fokus topik bahasan AI.\n"
            "- `!konteks <status>` : Ubah situasi live saat ini & hapus memori sementara lama.\n"
            "- **Kirim Voice Note:** Ditranskrip & dibacakan AI ke live (sekaligus menjawab eskalasi jika ada).\n"
            "- `!say <teks>` : Membacakan teks langsung ke live."
        ),
        inline=False
    )
    embed.add_field(
        name="Pemutar Musik, Antrean & Overlay Live",
        value=(
            "- **Request Lagu:** `!play <judul>`, `req <judul>`, atau `m!p <judul>`.\n"
            "- `!queue` : Cek antrean & munculkan menu hapus lagu.\n"
            "- `!delqueue 2` / `!delqueue 2 3` : Hapus antrean nomor tertentu.\n"
            "- `!autoplay` / `!skipmusic` / `!stopmusic` / `!vol <1-100>`\n"
            f"- `!openoverlay` : Buka jendela overlay anti-pause (`{music_state['overlay_url']}`)"
        ),
        inline=False
    )
    embed.add_field(
        name="Moderasi & Pengaturan",
        value=(
            "- `!target <nama>` : Munculkan tombol aksi (Mute/Kick/Ban) untuk penonton tertentu.\n"
            "- `!kick <nama>` / `!muteuser <nama>` / `!ban <nama>` / `!unban <nama>`\n"
            "- `!learn <tanya> | <jawab>` / `!forget <kata>` / `!addbadword <kata>`"
        ),
        inline=False
    )
    await channel.send(embed=embed, view=ControlPanelView())


async def send_discord_escalation(
    numeric_uid: int,
    unique_id: str,
    user_name: str,
    comment: str,
    sec_uid: str = "",
):
    channel = await get_control_channel()
    if not channel:
        return

    embed = discord.Embed(
        title="Eskalasi Komentar TikTok Live",
        description=f"**Penonton:** **{user_name}** (`@{unique_id}`)\n**Komentar:** {comment}",
        color=discord.Color.orange(),
        timestamp=datetime.now()
    )
    embed.set_footer(text="Balas dengan teks / Voice Note untuk menjawab, atau klik tombol tindakan cepat di bawah")
    view = ViewerModerationView(
        numeric_uid=numeric_uid,
        unique_id=unique_id,
        display_name=user_name,
        sec_uid=sec_uid,
        show_skip=True,
        show_reset_warn=False,
    )
    sent_msg = await channel.send(embed=embed, view=view)
    last_escalated_question["msg_id"] = sent_msg.id


async def send_warn_list_to_discord(
    trigger_user: str | None = None,
    strike_num: int = 1,
    target_info: tuple | None = None,
):
    channel = await get_control_channel()
    if not channel:
        return
    title = (
        f"Update Warn List — Strike {strike_num}/3 untuk {trigger_user}"
        if trigger_user else "Daftar Pelanggaran Penonton (Warn List)"
    )
    view = None
    if target_info:
        t_num_uid, t_uid, t_name, t_sec_uid = target_info
        view = ViewerModerationView(
            numeric_uid=t_num_uid,
            unique_id=t_uid,
            display_name=t_name,
            sec_uid=t_sec_uid,
            show_skip=False,
            show_reset_warn=True,
        )
    await channel.send(embed=build_warn_list_embed(title), view=view)


def enqueue_action(priority: int, event_type: str, payload: tuple):
    if state["muted"] and priority > PRIORITY_HOST:
        return
    action_queue.put_nowait((priority, next(queue_counter), event_type, payload))


async def queue_worker():
    TermUI.log("AI-MOD", "AI Head Moderator aktif dan siap menjaga room.")
    while True:
        priority, _, event_type, payload = await action_queue.get()
        try:
            if state["muted"] and priority > PRIORITY_HOST:
                continue

            if event_type == "HOST_SPEAK":
                text_to_speak = payload[0]
                await speak_to_vbcable(text_to_speak)

            elif event_type == "MANUAL_MOD":
                mode, numeric_uid, unique_id, display_name, sec_uid = payload
                if mode == "kick":
                    announce = f"Maaf Kak {display_name}, kamu dikeluarkan dari live oleh moderator."
                elif mode == "mute":
                    announce = f"Kak {display_name} dibisukan sementara dari kolom komentar oleh moderator."
                else:
                    announce = f"Maaf Kak {display_name}, akun kamu diblokir permanen dari live ini oleh moderator."

                await speak_to_vbcable(announce)
                await execute_tiktok_moderation(
                    mode,
                    numeric_uid,
                    unique_id,
                    display_name,
                    sec_uid,
                    reason=f"Aksi moderasi !{mode} oleh Host",
                )

            elif event_type == "GIFT":
                user_name, gift_name, count = payload
                if count > 1:
                    shoutout = f"Wah makasih banyak Kak {user_name} udah ngirim {count} {gift_name}! Sehat dan lancar terus rezekinya ya."
                else:
                    shoutout = f"Makasih banyak Kak {user_name} buat {gift_name}-nya! Keren banget support-nya."
                recent_memory.append(f"Penonton {user_name} mengirim gift {count}x {gift_name}")
                TermUI.log("TIKTOK", f"Gift Shoutout -> {shoutout}", TermUI.YELLOW)
                await speak_to_vbcable(shoutout)

            elif event_type == "FOLLOW":
                user_name = payload[0]
                templates = [
                    f"Makasih sudah follow Kak {user_name}, selamat bergabung di live ya.",
                    f"Welcome Kak {user_name}, terima kasih sudah follow akun ini!",
                    f"Mantap Kak {user_name} sudah follow, yuk ikutan ngobrol di kolom komentar."
                ]
                shoutout = random.choice(templates)
                TermUI.log("TIKTOK", f"Follow -> {shoutout}")
                await speak_to_vbcable(shoutout)

            elif event_type == "SHARE":
                user_name = payload[0]
                templates = [
                    f"Terima kasih Kak {user_name} sudah bantu share live-nya!",
                    f"Makasih banyak Kak {user_name} udah bantu bagikan live ini ke teman-teman."
                ]
                shoutout = random.choice(templates)
                TermUI.log("TIKTOK", f"Share -> {shoutout}")
                await speak_to_vbcable(shoutout)

            elif event_type == "LIKE":
                user_name = payload[0]
                templates = [
                    f"Makasih Kak {user_name} dan teman-teman yang udah bantu tap-tap layarnya!",
                    f"Terima kasih tap-tap layarnya Kak {user_name}, gas terus ramaikan room-nya!"
                ]
                shoutout = random.choice(templates)
                TermUI.log("TIKTOK", f"Like -> {shoutout}")
                await speak_to_vbcable(shoutout)

            elif event_type == "COMMENT":
                token_id, numeric_uid, unique_id, user_name, comment, sec_uid = payload

                if is_user_permanently_banned(unique_id, numeric_uid):
                    TermUI.log("WARN", f"Auto-Enforce Ban: {user_name} (@{unique_id}) mencoba komentar, kick ulang.")
                    asyncio.create_task(
                        execute_tiktok_moderation("kick", numeric_uid, unique_id, user_name, sec_uid, silent=True)
                    )
                    continue

                filter_status, matched_bw = check_badword_violation(comment)
                if filter_status == "VIOLATION":
                    await handle_user_strike(numeric_uid, unique_id, user_name, comment, matched_bw, sec_uid)
                    continue

                if (latest_comment_token["id"] - token_id) >= 3:
                    continue

                if filter_status == "META_QUESTION":
                    rule_reply = (
                        f"Nggak boleh ya Kak {user_name}, kata kasar otomatis terdeteksi filter room dan kalau dipakai bisa kena ban setelah tiga kali peringatan."
                    )
                    TermUI.log("AI-MOD", f"Aturan Room (0.00s) -> {user_name}: \"{comment}\"")
                    recent_memory.append(f"{user_name}: {comment} -> Mod (ATURAN): {rule_reply}")
                    await speak_to_vbcable(rule_reply)
                    continue

                memory_reply = find_permanent_memory_answer(comment)
                if memory_reply:
                    TermUI.log("AI-MOD", f"Memory Hit (0.00s) -> \"{comment}\" => \"{memory_reply}\"", TermUI.CYAN)
                    recent_memory.append(f"{user_name}: {comment} -> Mod (MEMORY): {memory_reply}")
                    await speak_to_vbcable(memory_reply)
                    continue

                t0 = time.time()
                decision = await asyncio.to_thread(run_moderator_reasoning, unique_id, user_name, comment)
                llm_time = time.time() - t0

                action = decision.get("action", "IGNORE").upper()
                intent = decision.get("intent", "-")
                thought = decision.get("thought", "")
                reply = (decision.get("reply_text") or "").strip()

                TermUI.log("AI-MOD", f"⚡ {llm_time:.2f}s | {action} ({intent}) | {thought}")

                if action == "IGNORE":
                    continue

                if action == "WARN":
                    await handle_user_strike(numeric_uid, unique_id, user_name, comment, None, sec_uid)
                    continue

                if action == "ESCALATE":
                    last_escalated_question["user"] = user_name
                    last_escalated_question["question"] = comment
                    recent_memory.append(f"{user_name}: {comment} -> [ESKALASI HOST]")
                    TermUI.log("DISCORD", f"Eskalasi pertanyaan {user_name}: \"{comment}\"", TermUI.YELLOW)
                    await send_discord_escalation(numeric_uid, unique_id, user_name, comment, sec_uid)
                elif reply:
                    recent_memory.append(f"{user_name}: {comment} -> Mod ({action}): {reply}")
                    TermUI.log("VOICE", f"Mina: \"{reply}\"", TermUI.WHITE)
                    await speak_to_vbcable(reply)

        except Exception as e:
            TermUI.log("ERROR", f"Worker error: {e}")
        finally:
            action_queue.task_done()


def ensure_worker_running():
    global worker_task
    if worker_task is None or worker_task.done():
        worker_task = asyncio.create_task(queue_worker())


def extract_comment_timestamp(event: CommentEvent) -> float:
    for attr in ("base_message", "common"):
        obj = getattr(event, attr, None)
        if obj is not None:
            ts = getattr(obj, "create_time", 0) or 0
            if ts > 1e12:
                return ts / 1000.0
            if ts > 1e9:
                return float(ts)
    return 0.0


@tiktok_client.on(ConnectEvent)
async def on_connect(event: ConnectEvent):
    global connected_at
    connected_at = time.time()
    state["tiktok_connected"] = True
    ensure_worker_running()
    ensure_music_worker_running()
    TermUI.log("TIKTOK", f"Terhubung ke Live @{event.unique_id} (filter riwayat chat aktif 2.5s)", TermUI.GREEN)

    channel = await get_control_channel()
    if channel:
        await channel.send(f"🟢 Terhubung ke TikTok Live **@{event.unique_id}**. AI Moderator siap bertugas.")


@tiktok_client.on(DisconnectEvent)
async def on_disconnect(event: DisconnectEvent):
    was_connected = state["tiktok_connected"]
    state["tiktok_connected"] = False
    TermUI.log("WARN", "Koneksi TikTok Live berakhir / terputus.")

    if was_connected:
        channel = await get_control_channel()
        if channel:
            embed = discord.Embed(
                title="TikTok Live Berakhir / Terputus",
                description=f"Sesi pemantauan untuk **@{tiktok_username}** telah berhenti.",
                color=discord.Color.dark_red(),
                timestamp=datetime.now()
            )
            await channel.send(embed=embed)


@tiktok_client.on(CommentEvent)
async def on_comment(event: CommentEvent):
    global connected_at
    now = time.time()
    if connected_at is None:
        connected_at = now

    register_viewer(event.user)

    text = str(getattr(event, "comment", "") or "").strip()
    if len(text) < 2:
        return

    msg_ts = extract_comment_timestamp(event)
    if msg_ts > 0 and msg_ts < (BOOT_TIME - 15.0):
        return

    if (now - connected_at) < 2.5:
        return

    unique_id = get_user_unique_id(event.user)
    numeric_uid = get_user_numeric_id(event.user)
    display_name = get_clean_nickname(event.user)
    sec_uid = get_user_sec_uid(event.user)

    msg_id = getattr(event, "msg_id", None) or f"{unique_id}:{text}:{int(now // 5)}"
    if msg_id in seen_comments:
        return
    seen_comments.add(msg_id)
    if len(seen_comments) > 500:
        seen_comments.clear()

    ensure_worker_running()
    TermUI.log("CHAT", f"{display_name}: {text}")

    if is_user_permanently_banned(unique_id, numeric_uid):
        TermUI.log("WARN", f"Auto-Enforce Ban: {display_name} (@{unique_id}) mencoba komentar, kick ulang.")
        asyncio.create_task(
            execute_tiktok_moderation("kick", numeric_uid, unique_id, display_name, sec_uid, silent=True)
        )
        return

    lower_text = text.lower()
    if lower_text.startswith(("!play ", "req ", "m!p ")):
        song_query = text.split(" ", 1)[1].strip()
        if not song_query:
            return

        f_status, matched_bw = check_badword_violation(song_query)
        if f_status == "VIOLATION":
            await handle_user_strike(numeric_uid, unique_id, display_name, text, matched_bw, sec_uid)
            return

        if len(music_pending_items) >= 12:
            TermUI.log("WARN", f"Request {display_name} ditolak: antrean penuh ({len(music_pending_items)}).")
            return

        last_user_req = user_music_cooldown.get(unique_id, 0.0)
        if now - last_user_req < 5.0:
            return
        user_music_cooldown[unique_id] = now

        TermUI.log("MUSIC", f"Request masuk dari {display_name}: {song_query}", TermUI.CYAN)
        enqueue_song(display_name, song_query)
        pos_now = len(music_pending_items)
        if pos_now <= 1 and music_state["current"] is None:
            ack_msg = f"Oke Kak {display_name}, lagu {song_query} langsung diputar ya."
        else:
            ack_msg = f"Oke Kak {display_name}, lagu {song_query} masuk antrean nomor {pos_now} ya."
        enqueue_action(PRIORITY_SOCIAL, "HOST_SPEAK", (ack_msg,))
        return

    latest_comment_token["id"] += 1
    enqueue_action(
        PRIORITY_COMMENT,
        "COMMENT",
        (latest_comment_token["id"], numeric_uid, unique_id, display_name, text, sec_uid)
    )


@tiktok_client.on(GiftEvent)
async def on_gift(event: GiftEvent):
    register_viewer(event.user)
    gift_obj = getattr(event, "gift", None)
    streakable = getattr(gift_obj, "streakable", False)
    repeat_end = getattr(event, "repeat_end", True)
    if streakable and not repeat_end:
        return

    display_name = get_clean_nickname(event.user)
    gift_name = (
        getattr(gift_obj, "name", None)
        or getattr(getattr(gift_obj, "info", None), "name", None)
        or "Gift"
    )
    count = getattr(event, "repeat_count", 1) or 1

    ensure_worker_running()
    TermUI.log("TIKTOK", f"Gift: {display_name} mengirim {count}x {gift_name}", TermUI.YELLOW)
    enqueue_action(PRIORITY_GIFT, "GIFT", (display_name, gift_name, count))

    channel = await get_control_channel()
    if channel:
        await channel.send(f"🎁 **Gift Masuk:** `{display_name}` mengirim **{count}x {gift_name}**")


@tiktok_client.on(FollowEvent)
async def on_follow(event: FollowEvent):
    register_viewer(event.user)
    unique_id = get_user_unique_id(event.user)
    if unique_id in followed_users:
        return
    followed_users.add(unique_id)

    display_name = get_clean_nickname(event.user)
    ensure_worker_running()
    enqueue_action(PRIORITY_SOCIAL, "FOLLOW", (display_name,))


@tiktok_client.on(ShareEvent)
async def on_share(event: ShareEvent):
    register_viewer(event.user)
    now = time.time()
    unique_id = get_user_unique_id(event.user)
    if now - shared_users_cooldown.get(unique_id, 0) < 60.0:
        return
    shared_users_cooldown[unique_id] = now

    display_name = get_clean_nickname(event.user)
    ensure_worker_running()
    enqueue_action(PRIORITY_SOCIAL, "SHARE", (display_name,))


@tiktok_client.on(LikeEvent)
async def on_like(event: LikeEvent):
    register_viewer(event.user)
    now = time.time()
    if now - state["last_like_shoutout"] < 25.0:
        return
    state["last_like_shoutout"] = now

    display_name = get_clean_nickname(event.user)
    ensure_worker_running()
    enqueue_action(PRIORITY_SOCIAL, "LIKE", (display_name,))


async def start_tiktok_background():
    try:
        TermUI.log("TIKTOK", "Menghubungkan ke server TikTok Live...")
        await tiktok_client.start()
    except Exception as e:
        state["tiktok_connected"] = False
        TermUI.log("ERROR", f"Koneksi TikTok Live terhenti: {e}")
        channel = await get_control_channel()
        if channel:
            await channel.send(f"⚠️ Koneksi TikTok Live terhenti: `{e}`")


class ModeratorDiscordBot(discord.Client):
    async def close(self):
        TermUI.log("SYSTEM", "Menutup terminal & mematikan AI Moderator...")
        try:
            channel = await get_control_channel()
            if channel:
                embed = discord.Embed(
                    title="Terminal / Sistem AI Moderator Ditutup",
                    description="Program dihentikan oleh host (`Ctrl + C` / Terminal ditutup).",
                    color=discord.Color.dark_grey(),
                    timestamp=datetime.now()
                )
                await channel.send(embed=embed)
        except Exception as e:
            TermUI.log("ERROR", f"Shutdown notify error: {e}")

        try:
            if overlay_server:
                overlay_server.close()
        except Exception:
            pass

        try:
            if tiktok_client.connected:
                await tiktok_client.disconnect()
        except Exception:
            pass

        try:
            pygame.mixer.quit()
        except Exception:
            pass

        await super().close()


intents = discord.Intents.default()
intents.message_content = True
discord_client = ModeratorDiscordBot(intents=intents)


@discord_client.event
async def on_ready():
    await start_overlay_services()
    TermUI.log("DISCORD", f"Login sebagai {discord_client.user} | Channel ID: {control_channel_id}", TermUI.GREEN)
    if not state["tiktok_started"]:
        state["tiktok_started"] = True
        ensure_music_worker_running()
        await send_startup_command_guide()
        asyncio.create_task(start_tiktok_background())


@discord_client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.channel.id != control_channel_id:
        return

    content = message.content.strip()

    audio_attachment = next(
        (
            a for a in message.attachments
            if (a.content_type and "audio" in a.content_type)
            or a.filename.lower().endswith((".ogg", ".mp3", ".wav", ".m4a"))
        ),
        None
    )

    if audio_attachment:
        await message.add_reaction("🎙️")
        try:
            audio_bytes = await audio_attachment.read()
            transcribed = await asyncio.to_thread(
                transcribe_voice_note,
                audio_bytes,
                audio_attachment.content_type or "audio/ogg"
            )
            if not transcribed or "[TIDAK_JELAS]" in transcribed:
                await message.reply("Suara Voice Note kurang jelas, coba ulangi lagi.")
                return

            if last_escalated_question["question"]:
                q_part = last_escalated_question["question"]
                qa_type = await asyncio.to_thread(save_learned_qa, q_part, transcribed, "discord_voice_note")
                recent_memory.append(f"Host menjawab '{q_part}': {transcribed}")
                last_escalated_question["question"] = None
                await message.reply(f"**Dibacakan & Disimpan ({qa_type}):** {transcribed}")
            else:
                update_live_context(transcribed)
                await message.reply(f"**Dibacakan & Konteks Live Diperbarui:** {transcribed}")

            enqueue_action(PRIORITY_HOST, "HOST_SPEAK", (transcribed,))
        except Exception as e:
            await message.reply(f"Gagal memproses Voice Note: `{e}`")
        return

    if content.startswith(("!", "m!p ")):
        parts = content.split(" ", 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("!play", "m!p") and arg:
            enqueue_song("Host", arg)
            await message.reply(f"🎵 Ditambahkan ke antrean musik VB-Cable: **{arg}** *(Autoplay akan mengikuti vibes lagu ini)*")

        elif cmd in ("!delqueue", "!hapuslagu", "!remqueue", "!hapusantrean"):
            if not arg:
                await message.reply(
                    "Gunakan format:\n"
                    "- `!delqueue 2` (hapus antrean nomor 2)\n"
                    "- `!delqueue 2 3` atau `!delqueue 2, 3` (hapus antrean nomor 2 dan 3)\n"
                    "- `!delqueue <judul lagu>` (hapus berdasarkan judul)"
                )
                return
            removed = remove_queued_songs(arg)
            if removed:
                titles_str = "\n".join(f"- `{r.get('title')}` (Req: {r.get('requester')})" for r in removed)
                await message.reply(f"🗑️ **{len(removed)} lagu** dihapus dari antrean:\n{titles_str}")
            else:
                await message.reply(f"Antrean `{arg}` tidak ditemukan. Ketik `!queue` untuk melihat nomor antrean saat ini.")

        elif cmd == "!autoplay":
            if arg.lower() in ("on", "aktif", "1"):
                music_state["autoplay"] = True
            elif arg.lower() in ("off", "mati", "0"):
                music_state["autoplay"] = False
            else:
                music_state["autoplay"] = not music_state["autoplay"]

            broadcast_queue_update()
            status_ap = "**AKTIF (ON)**" if music_state["autoplay"] else "**NONAKTIF (OFF)**"
            seed_info = f" (patokan vibes saat ini: `{music_state['last_seed_query']}`)" if music_state["last_seed_query"] else ""
            await message.reply(f"🔁 Autoplay musik sekarang {status_ap}{seed_info}.")

        elif cmd in ("!skipmusic", "!nextsong"):
            music_state["skip_flag"] = True
            await message.reply("⏩ Melewati lagu yang sedang diputar...")

        elif cmd == "!stopmusic":
            music_state["stop_flag"] = True
            music_state["skip_flag"] = True
            music_state["last_seed_query"] = None
            music_pending_items.clear()
            music_queue_event.clear()
            broadcast_queue_update()
            await message.reply("⏹️ Pemutar musik dihentikan, antrean dikosongkan, dan Autoplay dijeda sampai ada request lagu baru.")

        elif cmd == "!vol" and arg.isdigit():
            vol_num = max(1, min(100, int(arg)))
            music_state["volume"] = vol_num / 100.0
            if music_state["channel"]:
                music_state["channel"].set_volume(music_state["volume"])
            await message.reply(f"🔊 Volume musik latar diatur ke **{vol_num}%**.")

        elif cmd == "!queue":
            snap = get_live_queue_snapshot()
            np = snap["now_playing"]
            curr = (
                f"{np['title']} (Req: **{np['requester']}**) `[{np['elapsed_str']}/{np['duration_str']}]`"
                if np else "Tidak ada lagu yang sedang diputar"
            )
            if music_pending_items:
                q_lines = "\n".join(
                    f"**#{idx}.** {item.get('title') or item.get('query')} — Req: **{item.get('requester')}**"
                    for idx, item in enumerate(music_pending_items[:15], start=1)
                )
            else:
                q_lines = "Antrean kosong"
            ap_status = f"ON (Vibes: {music_state['last_seed_query'] or '-'})" if music_state["autoplay"] else "OFF"
            view = QueueManageView(music_pending_items) if music_pending_items else None
            await message.reply(
                f"**Sedang Diputar:** {curr}\n"
                f"**Autoplay:** `{ap_status}` | **Overlay Live:** `{music_state['overlay_url']}`\n"
                f"**Daftar Antrean ({len(music_pending_items)} lagu):**\n{q_lines}",
                view=view,
            )

        elif cmd in ("!overlay", "!openoverlay"):
            url = music_state["overlay_url"]
            if arg.lower() == "chroma":
                url = f"{url}/?chroma=1"
            browser_used = await asyncio.to_thread(launch_overlay_app_window, url)
            if browser_used:
                await message.reply(
                    f"🪟 Jendela Overlay dibuka lewat **{browser_used}** (mode anti-pause).\n"
                    f"Pastikan jendela tersebut **tidak di-minimize** saat ditangkap lewat *Window Capture* di TikTok LIVE Studio."
                )
            else:
                await message.reply(
                    f"**Live Queue Overlay (SSE Real-Time):**\n"
                    f"- **URL Browser:** `{url}` *(tambah `/?chroma=1` untuk background hijau)*\n"
                    f"- **Text Source File:** `{os.path.abspath(LIVE_QUEUE_TXT_FILE)}`"
                )

        elif cmd == "!panel":
            await send_startup_command_guide()

        elif cmd == "!topik":
            if not arg:
                curr_topic = load_dataset_dict().get("konteks_live", {}).get("topik_bahasan", "Belum diatur")
                await message.reply(f"Topik bahasan saat ini: **{curr_topic}**\nGunakan `!topik <tema baru>` untuk mengubahnya.")
                return
            update_live_topic(arg)
            await message.reply(f"Fokus topik bahasan live diubah menjadi:\n> **{arg}**")

        elif cmd == "!target":
            if not arg:
                await message.reply("Gunakan format: `!target <nama tampilan atau @username>`")
                return
            target = resolve_target_viewer(arg)
            if not target:
                await message.reply(f"Penonton `{arg}` belum tercatat berinteraksi di sesi live ini.")
                return
            embed = discord.Embed(
                title=f"Panel Tindakan: {target['name']}",
                description=f"**Username:** `@{target['unique_id']}`\n**Numeric ID:** `{target['numeric_uid']}`",
                color=discord.Color.gold()
            )
            view = ViewerModerationView(
                numeric_uid=target["numeric_uid"],
                unique_id=target["unique_id"],
                display_name=target["name"],
                sec_uid=target.get("sec_uid", ""),
                show_skip=False,
                show_reset_warn=target["unique_id"] in user_warnings,
            )
            await message.reply(embed=embed, view=view)

        elif cmd in ("!kick", "!muteuser", "!ban"):
            if not arg:
                await message.reply(f"Gunakan format: `{cmd} <nama tampilan atau @username>`")
                return

            target = resolve_target_viewer(arg)
            if not target:
                await message.reply(
                    f"Penonton `{arg}` belum tercatat berinteraksi di sesi live ini. "
                    f"Pastikan nama/username sesuai atau masukkan ID numerik akun."
                )
                return

            mode = "mute" if cmd == "!muteuser" else cmd.lstrip("!")
            await message.add_reaction("⚡")
            enqueue_action(
                PRIORITY_HOST,
                "MANUAL_MOD",
                (
                    mode,
                    target["numeric_uid"],
                    target["unique_id"],
                    target["name"],
                    target.get("sec_uid", ""),
                ),
            )
            await message.reply(
                f"Memproses `{mode.upper()}` untuk **{target['name']}** (`@{target['unique_id']}`)..."
            )

        elif cmd == "!unban":
            if not arg:
                await message.reply("Gunakan format: `!unban <nama tampilan atau @username>`")
                return
            removed = remove_banned_user_record(arg)
            if removed:
                await message.reply(f"Akun **{removed}** telah dihapus dari daftar `banned_users` lokal.")
            else:
                await message.reply(f"Akun `{arg}` tidak ditemukan di daftar `banned_users`.")

        elif cmd == "!banlist":
            await message.reply(embed=build_ban_list_embed())

        elif cmd == "!say" and arg:
            update_live_context(arg)
            await message.add_reaction("🗣️")
            enqueue_action(PRIORITY_HOST, "HOST_SPEAK", (arg,))

        elif cmd == "!konteks" and arg:
            update_live_context(arg)
            await message.reply(f"Status `konteks_live` diperbarui & memori sementara lama dibersihkan:\n> {arg}")

        elif cmd == "!learn" and "|" in arg:
            q_part, a_part = [x.strip() for x in arg.split("|", 1)]
            qa_type = await asyncio.to_thread(save_learned_qa, q_part, a_part, "discord_command", "permanen")
            await message.reply(f"Memori **{qa_type}** tersimpan:\n**Tanya:** {q_part}\n**Jawab:** {a_part}")

        elif cmd == "!forget" and arg:
            removed = forget_learned_qa(arg)
            await message.reply(f"Dihapus `{removed}` memori yang mengandung kata `{arg}`.")

        elif cmd == "!addbadword" and arg:
            if add_badword_to_dataset(arg):
                await message.reply(f"Kata `{normalize_text(arg)}` berhasil ditambahkan ke `daftar_badword`.")
            else:
                await message.reply(f"Kata `{arg}` sudah ada di daftar atau tidak valid.")

        elif cmd == "!delbadword" and arg:
            if remove_badword_from_dataset(arg):
                await message.reply(f"Kata `{normalize_text(arg)}` dihapus dari `daftar_badword`.")
            else:
                await message.reply(f"Kata `{arg}` tidak ditemukan di `daftar_badword`.")

        elif cmd == "!warnlist":
            await message.reply(embed=build_warn_list_embed())

        elif cmd == "!clearwarn":
            user_warnings.clear()
            await message.reply("Seluruh catatan peringatan (`warn list`) telah direset.")

        elif cmd == "!mute":
            state["muted"] = True
            await message.reply("Moderator AI di-**MUTE** sementara.")

        elif cmd == "!unmute":
            state["muted"] = False
            await message.reply("Moderator AI kembali **AKTIF** menjaga chat.")

        elif cmd == "!skip":
            last_escalated_question["question"] = None
            await message.reply("Eskalasi aktif dilewati.")

        elif cmd == "!status":
            await message.reply(build_status_text())
        return

    if last_escalated_question["question"] and content:
        q_part = last_escalated_question["question"]
        qa_type = await asyncio.to_thread(save_learned_qa, q_part, content, "discord_text_reply")
        recent_memory.append(f"Host menjawab '{q_part}': {content}")
        last_escalated_question["question"] = None
        await message.reply(f"Dibacakan & disimpan sebagai memori **{qa_type}**.")
        enqueue_action(PRIORITY_HOST, "HOST_SPEAK", (content,))


if __name__ == "__main__":
    cable_name = init_vbcable()
    TermUI.banner(cable_name, f"http://127.0.0.1:{OVERLAY_PORT}")
    if not discord_token:
        raise RuntimeError("DISCORD_BOT_TOKEN belum diisi di file .env")
    discord_client.run(discord_token, log_handler=None)