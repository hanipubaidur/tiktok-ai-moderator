import os
import json
import time
import shutil
import asyncio
import tempfile
import subprocess
from typing import Callable
from config import (
    OVERLAY_HOST,
    OVERLAY_PORT,
    LIVE_QUEUE_TXT_FILE,
    TermUI,
    music_pending_items,
    music_state,
    overlay_subscribers,
    live_session_stats,
)

_remove_uid_callback: Callable[[int], dict | None] | None = None
_remove_spec_callback: Callable[[str], list[dict]] | None = None
_overlay_server = None
_overlay_ticker_task = None


def register_queue_callbacks(
    remove_by_uid_fn: Callable[[int], dict | None],
    remove_by_spec_fn: Callable[[str], list[dict]],
):
    global _remove_uid_callback, _remove_spec_callback
    _remove_uid_callback = remove_by_uid_fn
    _remove_spec_callback = remove_by_spec_fn


def format_duration_mmss(seconds: int | float) -> str:
    sec = max(0, int(seconds))
    mins, rem = divmod(sec, 60)
    return f"{mins:02d}:{rem:02d}"


def format_live_duration(seconds: int | float) -> str:
    sec = max(0, int(seconds))
    hours, rem = divmod(sec, 3600)
    mins, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def format_wait_minutes(seconds: int | float) -> str:
    sec = max(0, int(seconds))
    if sec < 45:
        return "< 1 mnt"
    mins = round(sec / 60)
    return f"~{max(1, mins)} mnt"


def get_live_dashboard_snapshot() -> dict:
    now = time.time()
    curr_item = music_state.get("current_item")
    elapsed = 0
    remaining = 0
    duration = 0

    if curr_item and music_state["started_at"] > 0:
        duration = max(1, int(music_state["duration"] or curr_item.get("duration") or 210))
        elapsed = min(duration, max(0, int(now - music_state["started_at"])))
        remaining = max(0, duration - elapsed)

    cumulative_wait = remaining
    queue_all = []
    for idx, item in enumerate(music_pending_items, start=1):
        wait_label = format_wait_minutes(cumulative_wait)
        queue_all.append({
            "uid": item.get("uid"),
            "pos": idx,
            "title": item.get("title") or item.get("query") or "-",
            "requester": item.get("requester") or "Viewer",
            "status": item.get("status") or "queued",
            "wait_sec": int(cumulative_wait),
            "wait_label": wait_label,
            "duration": int(item.get("duration") or 210),
        })
        cumulative_wait += int(item.get("duration") or 210)

    top_likers = sorted(
        live_session_stats["user_likes"].values(),
        key=lambda x: x["count"],
        reverse=True,
    )[:10]

    top_gifters = []
    sorted_gifters = sorted(
        live_session_stats["user_gifts"].values(),
        key=lambda x: x["total_items"],
        reverse=True,
    )[:10]
    for g in sorted_gifters:
        g_breakdown = ", ".join(
            f"{cnt}x {gname}"
            for gname, cnt in sorted(g["gifts"].items(), key=lambda kv: kv[1], reverse=True)
        )
        top_gifters.append({
            "name": g["name"],
            "unique_id": g["unique_id"],
            "total_items": g["total_items"],
            "breakdown": g_breakdown,
        })

    gift_recap = [
        {"gift_name": gname, "count": cnt}
        for gname, cnt in sorted(
            live_session_stats["gift_totals"].items(),
            key=lambda kv: kv[1],
            reverse=True,
        )
    ]

    is_live_active = bool(live_session_stats.get("is_live_active", False))
    live_started_at = float(live_session_stats.get("live_started_at") or 0.0)
    live_elapsed_sec = max(0, int(now - live_started_at)) if (is_live_active and live_started_at > 0) else 0

    return {
        "live_session": {
            "is_active": is_live_active,
            "started_at": live_started_at,
            "elapsed_sec": live_elapsed_sec,
            "duration_str": format_live_duration(live_elapsed_sec),
        },
        "now_playing": {
            "title": curr_item.get("title") if curr_item else None,
            "requester": curr_item.get("requester") if curr_item else None,
            "elapsed": elapsed,
            "duration": duration,
            "elapsed_str": format_duration_mmss(elapsed),
            "duration_str": format_duration_mmss(duration),
            "progress_pct": round((elapsed / duration) * 100, 1) if duration > 0 else 0,
        } if curr_item else None,
        "queue_all": queue_all,
        "total_queued": len(music_pending_items),
        "autoplay": music_state["autoplay"],
        "vibes": music_state["last_seed_query"] or "",
        "stats": {
            "total_likes": live_session_stats["total_likes"],
            "top_likers": top_likers,
            "top_gifters": top_gifters,
            "gift_recap": gift_recap,
        },
    }


def sync_live_queue_file(snap: dict | None = None):
    try:
        snap = snap or get_live_dashboard_snapshot()
        lines = []
        ls = snap.get("live_session") or {}
        if ls.get("is_active"):
            lines.append(f"🔴 DURASI LIVE: {ls.get('duration_str', '00:00')}")
            lines.append("")

        np = snap["now_playing"]
        if np:
            lines.append(
                f"🎵 NOW PLAYING: {np['title']} (Req: {np['requester']}) [{np['elapsed_str']}/{np['duration_str']}]"
            )
        else:
            lines.append("🎵 NOW PLAYING: Tidak ada lagu diputar")

        lines.append("")
        lines.append(f"📋 ANTREAN MUSIK ({snap['total_queued']} LAGU):")
        if snap["queue_all"]:
            for q in snap["queue_all"][:8]:
                ready_tag = " [SIAP]" if q.get("status") == "ready" else ""
                lines.append(
                    f"#{q['pos']}. {q['title']} | Req: {q['requester']} | {q['wait_label']}{ready_tag}"
                )
            extra = snap["total_queued"] - 8
            if extra > 0:
                lines.append(f"... dan +{extra} lagu lainnya di antrean")
        else:
            ap_note = f"Autoplay ON (Vibes: {snap['vibes']})" if snap["autoplay"] and snap["vibes"] else "Antrean kosong"
            lines.append(f"Belum ada antrean ({ap_note})")

        lines.append("")
        lines.append("Ketik req <judul lagu> atau !play <judul lagu> di chat untuk request!")

        with open(LIVE_QUEUE_TXT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass


def broadcast_queue_update():
    snap = get_live_dashboard_snapshot()
    sync_live_queue_file(snap)
    payload = json.dumps(snap, ensure_ascii=False)
    for sub_q in list(overlay_subscribers):
        try:
            if sub_q.full():
                sub_q.get_nowait()
            sub_q.put_nowait(payload)
        except Exception:
            pass


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
        "--window-size=920,620",
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
<title>Mina Live Stream Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    width: 100%;
    height: 100%;
    background: #090d16;
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    color: #f8fafc;
    overflow: hidden;
  }
  body.chroma { background: #00ff00; padding: 12px; }
  .dashboard {
    width: 100%;
    height: 100%;
    display: grid;
    grid-template-columns: 1.25fr 1fr;
    gap: 14px;
    padding: 14px;
    background: #090d16;
  }
  body.chroma .dashboard {
    border-radius: 18px;
    border: 2px solid rgba(56, 189, 248, 0.4);
  }
  @media (max-width: 720px) {
    .dashboard { grid-template-columns: 1fr; grid-template-rows: 1.2fr 1fr; }
  }
  .panel {
    background: linear-gradient(165deg, rgba(15, 23, 42, 0.96), rgba(10, 15, 28, 0.99));
    border: 1px solid rgba(56, 189, 248, 0.25);
    border-radius: 14px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    min-height: 0;
    gap: 12px;
  }
  .right-col {
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-height: 0;
  }
  .sub-panel {
    flex: 1;
    background: linear-gradient(165deg, rgba(15, 23, 42, 0.96), rgba(10, 15, 28, 0.99));
    border: 1px solid rgba(148, 163, 184, 0.2);
    border-radius: 14px;
    padding: 13px 15px;
    display: flex;
    flex-direction: column;
    min-height: 0;
    gap: 8px;
  }
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    flex-shrink: 0;
  }
  .header-pills {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }
  .badge {
    font-size: 13px;
    font-weight: 800;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: #38bdf8;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .badge.pink { color: #f472b6; }
  .badge.amber { color: #fbbf24; }
  .dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: #22c55e;
    box-shadow: 0 0 10px #22c55e;
    animation: pulse 1.4s infinite;
  }
  .dot.idle { background: #64748b; box-shadow: none; animation: none; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.35} }
  .pill {
    font-size: 11px;
    font-weight: 700;
    padding: 4px 10px;
    border-radius: 999px;
    background: rgba(56, 189, 248, 0.15);
    color: #7dd3fc;
    border: 1px solid rgba(56, 189, 248, 0.28);
  }
  .pill.pink {
    background: rgba(244, 114, 182, 0.15);
    color: #f9a8d4;
    border-color: rgba(244, 114, 182, 0.3);
  }
  .pill.live-timer {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: rgba(239, 68, 68, 0.16);
    color: #fca5a5;
    border-color: rgba(239, 68, 68, 0.38);
    font-variant-numeric: tabular-nums;
  }
  .pill.live-timer.offline {
    background: rgba(100, 116, 139, 0.16);
    color: #94a3b8;
    border-color: rgba(148, 163, 184, 0.24);
  }
  .live-rec-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #ef4444;
    box-shadow: 0 0 8px #ef4444;
    animation: pulse 1.3s infinite;
  }
  .pill.live-timer.offline .live-rec-dot {
    background: #64748b;
    box-shadow: none;
    animation: none;
  }
  .np-box {
    background: rgba(30, 41, 59, 0.85);
    border-radius: 11px;
    padding: 13px 15px;
    border-left: 4px solid #38bdf8;
    flex-shrink: 0;
  }
  .np-label {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #38bdf8;
    margin-bottom: 3px;
  }
  .np-title {
    font-size: 18px;
    font-weight: 800;
    color: #ffffff;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .np-meta {
    display: flex;
    justify-content: space-between;
    font-size: 13px;
    color: #cbd5e1;
    margin-top: 5px;
  }
  .bar-bg {
    width: 100%;
    height: 6px;
    background: rgba(148, 163, 184, 0.22);
    border-radius: 6px;
    margin-top: 9px;
    overflow: hidden;
  }
  .bar-fill {
    height: 100%;
    background: linear-gradient(90deg, #38bdf8, #818cf8);
    width: 0%;
    transition: width 0.35s linear;
  }
  .sec-head {
    font-size: 12px;
    font-weight: 700;
    color: #cbd5e1;
    letter-spacing: 0.05em;
    display: flex;
    justify-content: space-between;
    flex-shrink: 0;
  }
  .scroll-list {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-right: 4px;
  }
  .scroll-list::-webkit-scrollbar { width: 6px; }
  .scroll-list::-webkit-scrollbar-track { background: rgba(15, 23, 42, 0.5); border-radius: 6px; }
  .scroll-list::-webkit-scrollbar-thumb { background: rgba(56, 189, 248, 0.35); border-radius: 6px; }
  .scroll-list::-webkit-scrollbar-thumb:hover { background: rgba(56, 189, 248, 0.65); }

  .q-item {
    display: flex;
    align-items: center;
    gap: 10px;
    background: rgba(15, 23, 42, 0.85);
    border: 1px solid rgba(148, 163, 184, 0.16);
    border-radius: 10px;
    padding: 9px 12px;
    flex-shrink: 0;
  }
  .q-item:hover { border-color: rgba(248, 113, 113, 0.45); }
  .q-pos {
    min-width: 32px;
    height: 32px;
    border-radius: 8px;
    background: rgba(56, 189, 248, 0.2);
    color: #38bdf8;
    font-size: 13px;
    font-weight: 800;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }
  .q-info { flex: 1; min-width: 0; }
  .q-title {
    font-size: 14px;
    font-weight: 700;
    color: #f1f5f9;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .q-req {
    font-size: 12px;
    color: #94a3b8;
    margin-top: 2px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .tag-ready {
    font-size: 10px;
    font-weight: 800;
    padding: 1px 6px;
    border-radius: 4px;
    background: rgba(34, 197, 94, 0.2);
    color: #4ade80;
  }
  .tag-dl {
    font-size: 10px;
    font-weight: 800;
    padding: 1px 6px;
    border-radius: 4px;
    background: rgba(251, 191, 36, 0.2);
    color: #fbbf24;
  }
  .q-wait {
    font-size: 12px;
    font-weight: 700;
    color: #fbbf24;
    background: rgba(251, 191, 36, 0.13);
    border: 1px solid rgba(251, 191, 36, 0.25);
    padding: 4px 8px;
    border-radius: 7px;
    white-space: nowrap;
    flex-shrink: 0;
  }
  .q-del {
    display: none;
    background: rgba(239, 68, 68, 0.22);
    color: #fca5a5;
    border: 1px solid rgba(239, 68, 68, 0.45);
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 12px;
    font-weight: 800;
    cursor: pointer;
    flex-shrink: 0;
  }
  .q-item:hover .q-del { display: inline-flex; }
  .q-del:hover { background: rgba(239, 68, 68, 0.5); color: #fff; }

  .stat-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    background: rgba(15, 23, 42, 0.8);
    border: 1px solid rgba(148, 163, 184, 0.14);
    border-radius: 9px;
    padding: 7px 11px;
    flex-shrink: 0;
  }
  .stat-left { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1; }
  .rank {
    min-width: 24px;
    height: 24px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 800;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(148, 163, 184, 0.18);
    color: #e2e8f0;
    flex-shrink: 0;
  }
  .rank.r1 { background: rgba(250, 204, 21, 0.25); color: #fde047; }
  .rank.r2 { background: rgba(203, 213, 225, 0.25); color: #f1f5f9; }
  .rank.r3 { background: rgba(251, 146, 60, 0.25); color: #fdba74; }
  .stat-name {
    font-size: 13px;
    font-weight: 700;
    color: #f8fafc;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .stat-sub {
    font-size: 11px;
    color: #94a3b8;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .stat-val {
    font-size: 12px;
    font-weight: 800;
    padding: 3px 9px;
    border-radius: 7px;
    background: rgba(244, 114, 182, 0.15);
    color: #f472b6;
    white-space: nowrap;
    flex-shrink: 0;
  }
  .stat-val.amber {
    background: rgba(251, 191, 36, 0.15);
    color: #fbbf24;
  }
  .recap-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    padding-top: 4px;
    border-top: 1px dashed rgba(148, 163, 184, 0.2);
    flex-shrink: 0;
  }
  .chip {
    font-size: 11px;
    font-weight: 700;
    padding: 3px 9px;
    border-radius: 7px;
    background: rgba(56, 189, 248, 0.12);
    border: 1px solid rgba(56, 189, 248, 0.25);
    color: #bae6fd;
  }
  .empty {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 13px;
    color: #64748b;
    text-align: center;
    border: 1px dashed rgba(148, 163, 184, 0.2);
    border-radius: 10px;
    padding: 12px;
  }
  .footer {
    padding-top: 6px;
    border-top: 1px solid rgba(148, 163, 184, 0.16);
    font-size: 12px;
    color: #94a3b8;
    text-align: center;
    flex-shrink: 0;
  }
  .footer span { color: #38bdf8; font-weight: 800; }
</style>
</head>
<body>
<div class="dashboard">
  <div class="panel">
    <div class="header">
      <div class="badge"><div id="dot" class="dot idle"></div> LIVE MUSIC QUEUE</div>
      <div class="header-pills">
        <div id="liveTimerPill" class="pill live-timer offline" title="Total durasi sesi live (tetap jalan saat reconnect, reset saat End Live)">
          <span class="live-rec-dot"></span>
          <span id="liveTimerText">LIVE 00:00</span>
        </div>
        <div id="apPill" class="pill">AUTOPLAY: ON</div>
      </div>
    </div>
    <div class="np-box">
      <div class="np-label">Sedang Diputar</div>
      <div id="npTitle" class="np-title">Menunggu request lagu...</div>
      <div class="np-meta">
        <span id="npReq">Ketik req &lt;judul&gt; atau !play &lt;judul&gt;</span>
        <span id="npTime">00:00 / 00:00</span>
      </div>
      <div class="bar-bg"><div id="npBar" class="bar-fill"></div></div>
    </div>
    <div class="sec-head">
      <span>DAFTAR ANTREAN (SCROLLABLE)</span>
      <span id="qCount">0 Lagu</span>
    </div>
    <div id="qList" class="scroll-list">
      <div class="empty">Antrean kosong</div>
    </div>
    <div class="footer">Ketik <span>req &lt;judul&gt;</span> / <span>!play &lt;judul&gt;</span> • <span>skip</span> • <span>!delqueue</span> di komentar</div>
  </div>

  <div class="right-col">
    <div class="sub-panel">
      <div class="header">
        <div class="badge pink">TOP LIKE LEADERBOARD</div>
        <div id="totalLikesPill" class="pill pink">Total: 0 Like</div>
      </div>
      <div id="likeList" class="scroll-list">
        <div class="empty">Belum ada data tap-tap layar</div>
      </div>
    </div>

    <div class="sub-panel">
      <div class="header">
        <div class="badge amber">TOP GIFTER & REKAP GIFT</div>
        <div id="totalGiftTypesPill" class="pill">0 Jenis Gift</div>
      </div>
      <div id="giftList" class="scroll-list">
        <div class="empty">Belum ada gift masuk di sesi ini</div>
      </div>
      <div id="giftRecapChips" class="recap-chips" style="display:none;"></div>
    </div>
  </div>
</div>

<script>
if (new URLSearchParams(location.search).get('chroma') === '1') {
  document.body.classList.add('chroma');
}

let liveSessionState = {
  isActive: false,
  startedAt: 0
};

function formatLiveClock(totalSec) {
  const s = Math.max(0, Math.floor(totalSec));
  const hrs = Math.floor(s / 3600);
  const mins = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return hrs > 0 ? `${pad(hrs)}:${pad(mins)}:${pad(secs)}` : `${pad(mins)}:${pad(secs)}`;
}

function updateLiveDurationUI() {
  const pill = document.getElementById('liveTimerPill');
  const text = document.getElementById('liveTimerText');
  if (!pill || !text) return;

  if (liveSessionState.isActive && liveSessionState.startedAt > 0) {
    const elapsed = (Date.now() / 1000) - liveSessionState.startedAt;
    pill.classList.remove('offline');
    text.textContent = 'LIVE ' + formatLiveClock(elapsed);
  } else {
    pill.classList.add('offline');
    text.textContent = 'LIVE 00:00';
  }
}

setInterval(updateLiveDurationUI, 1000);

async function removeQueueItem(uid) {
  try {
    await fetch('/api/remove', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ uid: uid })
    });
  } catch (e) {}
}

function renderDashboard(data) {
  if (data.live_session) {
    liveSessionState.isActive = Boolean(data.live_session.is_active);
    liveSessionState.startedAt = Number(data.live_session.started_at || 0);
    updateLiveDurationUI();
  }

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

  const items = data.queue_all || [];
  if (items.length === 0) {
    const emptyDiv = document.createElement('div');
    emptyDiv.className = 'empty';
    emptyDiv.textContent = data.autoplay && data.vibes
      ? 'Antrean kosong — Autoplay siap memilih lagu se-vibe'
      : 'Belum ada antrean lagu';
    qList.appendChild(emptyDiv);
  } else {
    items.forEach(item => {
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
      const reqSpan = document.createElement('span');
      reqSpan.textContent = 'Req: ' + item.requester;
      req.appendChild(reqSpan);

      if (item.status === 'ready') {
        const badge = document.createElement('span');
        badge.className = 'tag-ready';
        badge.textContent = 'SIAP PUTAR';
        req.appendChild(badge);
      } else if (item.status === 'downloading') {
        const badge = document.createElement('span');
        badge.className = 'tag-dl';
        badge.textContent = 'PRE-LOADING...';
        req.appendChild(badge);
      }

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

  const stats = data.stats || {};
  document.getElementById('totalLikesPill').textContent = 'Total: ' + (stats.total_likes || 0).toLocaleString('id-ID') + ' Like';
  const likeList = document.getElementById('likeList');
  likeList.innerHTML = '';
  const likers = stats.top_likers || [];
  if (likers.length === 0) {
    const emptyL = document.createElement('div');
    emptyL.className = 'empty';
    emptyL.textContent = 'Belum ada data tap-tap layar';
    likeList.appendChild(emptyL);
  } else {
    likers.forEach((lk, idx) => {
      const r = document.createElement('div');
      r.className = 'stat-row';
      const rankClass = idx === 0 ? 'r1' : (idx === 1 ? 'r2' : (idx === 2 ? 'r3' : ''));
      r.innerHTML = `
        <div class="stat-left">
          <div class="rank ${rankClass}">#${idx + 1}</div>
          <div style="min-width:0">
            <div class="stat-name">${lk.name}</div>
            <div class="stat-sub">@${lk.unique_id}</div>
          </div>
        </div>
        <div class="stat-val">${lk.count.toLocaleString('id-ID')} Like</div>
      `;
      likeList.appendChild(r);
    });
  }

  const giftList = document.getElementById('giftList');
  giftList.innerHTML = '';
  const gifters = stats.top_gifters || [];
  const recap = stats.gift_recap || [];
  document.getElementById('totalGiftTypesPill').textContent = recap.length + ' Jenis Gift';

  if (gifters.length === 0) {
    const emptyG = document.createElement('div');
    emptyG.className = 'empty';
    emptyG.textContent = 'Belum ada gift masuk di sesi ini';
    giftList.appendChild(emptyG);
  } else {
    gifters.forEach((gf, idx) => {
      const r = document.createElement('div');
      r.className = 'stat-row';
      const rankClass = idx === 0 ? 'r1' : (idx === 1 ? 'r2' : (idx === 2 ? 'r3' : ''));
      r.innerHTML = `
        <div class="stat-left">
          <div class="rank ${rankClass}">#${idx + 1}</div>
          <div style="min-width:0">
            <div class="stat-name">${gf.name}</div>
            <div class="stat-sub">${gf.breakdown}</div>
          </div>
        </div>
        <div class="stat-val amber">${gf.total_items}x Gift</div>
      `;
      giftList.appendChild(r);
    });
  }

  const recapEl = document.getElementById('giftRecapChips');
  if (recap.length > 0) {
    recapEl.style.display = 'flex';
    recapEl.innerHTML = recap.map(c => `<div class="chip">${c.gift_name}: <b>${c.count}x</b></div>`).join('');
  } else {
    recapEl.style.display = 'none';
  }
}

async function hydrateInitialSnapshot() {
  try {
    const res = await fetch('/api/queue', { cache: 'no-store' });
    if (res.ok) {
      const data = await res.json();
      renderDashboard(data);
    }
  } catch (e) {}
}

function connectSSE() {
  const es = new EventSource('/api/events');
  es.onmessage = (ev) => {
    try { renderDashboard(JSON.parse(ev.data)); } catch (e) {}
  };
  es.onerror = () => {
    es.close();
    setTimeout(connectSSE, 1500);
  };
}

hydrateInitialSnapshot();
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

            init_payload = json.dumps(get_live_dashboard_snapshot(), ensure_ascii=False)
            writer.write(f"data: {init_payload}\n\n".encode("utf-8"))
            await writer.drain()

            while True:
                try:
                    msg = await asyncio.wait_for(sub_q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    msg = json.dumps(get_live_dashboard_snapshot(), ensure_ascii=False)
                writer.write(f"data: {msg}\n\n".encode("utf-8"))
                await writer.drain()

        elif path.startswith("/api/remove") and method == "POST":
            raw_body = b""
            if content_length > 0:
                raw_body = await asyncio.wait_for(reader.readexactly(content_length), timeout=2.0)
            removed_title = None
            try:
                payload = json.loads(raw_body.decode("utf-8", errors="ignore"))
                if "uid" in payload and _remove_uid_callback:
                    rem = _remove_uid_callback(int(payload["uid"]))
                    if rem:
                        removed_title = rem.get("title")
                elif "spec" in payload and _remove_spec_callback:
                    rems = _remove_spec_callback(str(payload["spec"]))
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
            body = json.dumps(get_live_dashboard_snapshot(), ensure_ascii=False).encode("utf-8")
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
    global _overlay_server, _overlay_ticker_task
    if _overlay_ticker_task is None or _overlay_ticker_task.done():
        _overlay_ticker_task = asyncio.create_task(overlay_file_ticker())

    if _overlay_server is not None:
        return

    for port in range(OVERLAY_PORT, OVERLAY_PORT + 5):
        try:
            _overlay_server = await asyncio.start_server(handle_overlay_http, OVERLAY_HOST, port)
            music_state["overlay_url"] = f"http://127.0.0.1:{port}"
            TermUI.log("OVERLAY", f"Live Stream Dashboard aktif di {music_state['overlay_url']}")
            return
        except OSError:
            continue


def stop_overlay_server():
    if _overlay_server:
        try:
            _overlay_server.close()
        except Exception:
            pass