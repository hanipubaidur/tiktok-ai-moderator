import os
import re
import time
import random
import shutil
import asyncio
import tempfile
import threading
import subprocess
from typing import Callable, Awaitable
import pygame
import pygame._sdl2.audio as sdl2_audio
import yt_dlp

from config import (
    TermUI,
    song_id_counter,
    music_pending_items,
    music_queue_event,
    music_prefetch_event,
    played_music_history,
    music_state,
    state,
)
from overlay_dashboard import (
    format_duration_mmss,
    broadcast_queue_update,
    register_queue_callbacks,
)
from ai_moderator import generate_autoplay_next_song

_music_worker_task = None
_prefetch_worker_task = None
_notify_discord_fn: Callable[[str], Awaitable[None]] | None = None
_download_lock = threading.Lock()


def set_music_discord_notifier(fn: Callable[[str], Awaitable[None]]):
    global _notify_discord_fn
    _notify_discord_fn = fn


async def _notify_discord(text: str):
    if _notify_discord_fn:
        try:
            await _notify_discord_fn(text)
        except Exception:
            pass


def cleanup_stale_temp_files():
    tmp_dir = tempfile.gettempdir()
    try:
        for fname in os.listdir(tmp_dir):
            if fname.startswith(("mina_raw_", "mina_bgm_")):
                try:
                    os.remove(os.path.join(tmp_dir, fname))
                except Exception:
                    pass
    except Exception:
        pass


def remove_wav_file(path: str | None):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass


def init_vbcable() -> str:
    if pygame.mixer.get_init():
        pygame.mixer.quit()
    pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=1024)
    devices = sdl2_audio.get_audio_device_names(False)
    pygame.mixer.quit()

    cable_device = next((d for d in devices if "CABLE Input" in d), None)
    if not cable_device:
        raise RuntimeError(f"CABLE Input tidak ditemukan. Device tersedia: {devices}")

    pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=1024, devicename=cable_device)
    music_state["channel"] = pygame.mixer.Channel(1)
    state["audio_device"] = cable_device
    return cable_device


def ensure_audio_ready():
    try:
        if not pygame.mixer.get_init():
            init_vbcable()
        elif music_state["channel"] is None:
            music_state["channel"] = pygame.mixer.Channel(1)
    except Exception as e:
        TermUI.log("ERROR", f"Gagal memulihkan audio VB-Cable: {e}")


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
        "ffmpeg.exe tidak ditemukan! Taruh ffmpeg.exe di folder proyek atau jalankan: pip install imageio-ffmpeg"
    )


def prepare_song_wav(song_item: dict, deadline_ts: float | None = None) -> tuple[str, int, str]:
    ffmpeg_bin = find_ffmpeg_executable()
    song_query = song_item["query"]
    target_input = song_item.get("webpage_url") or song_query
    hard_deadline = deadline_ts or (time.time() + 58.0)

    def _progress_hook(d: dict):
        if music_state.get("stop_flag") or time.time() > hard_deadline:
            raise yt_dlp.utils.DownloadCancelled("Batas waktu unduhan lagu tercapai atau dihentikan.")

    tmp_dir = tempfile.gettempdir()
    uid_tag = f"{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
    raw_template = os.path.join(tmp_dir, f"mina_raw_{uid_tag}.%(ext)s")
    wav_path = os.path.join(tmp_dir, f"mina_bgm_{uid_tag}.wav")

    ydl_opts = {
        "format": "bestaudio[ext=m4a][abr<=160]/bestaudio[abr<=160]/bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": raw_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "default_search": "ytsearch1",
        "socket_timeout": 15,
        "retries": 2,
        "fragment_retries": 2,
        "http_chunk_size": 2097152,
        "progress_hooks": [_progress_hook],
    }

    with _download_lock:
        if music_state.get("stop_flag") or time.time() > hard_deadline:
            raise TimeoutError("Antrean unduhan melewati batas waktu.")

        downloaded_path = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(target_input, download=True)
                if "entries" in info and info["entries"]:
                    info = info["entries"][0]
                title = info.get("title") or song_item.get("title") or song_query
                raw_dur = int(info.get("duration") or song_item.get("duration") or 210)
                duration = min(420, max(15, raw_dur))
                downloaded_path = ydl.prepare_filename(info)

            if not downloaded_path or not os.path.exists(downloaded_path):
                prefix = f"mina_raw_{uid_tag}."
                for fname in os.listdir(tmp_dir):
                    if fname.startswith(prefix):
                        downloaded_path = os.path.join(tmp_dir, fname)
                        break

            if not downloaded_path or not os.path.exists(downloaded_path):
                raise FileNotFoundError("File audio mentah gagal diunduh oleh yt-dlp.")

            cmd = [
                ffmpeg_bin,
                "-y",
                "-i",
                downloaded_path,
                "-t",
                "420",
                "-ar",
                "44100",
                "-ac",
                "2",
                "-f",
                "wav",
                wav_path,
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=25)
            return title, duration, wav_path
        finally:
            if downloaded_path and os.path.exists(downloaded_path):
                try:
                    os.remove(downloaded_path)
                except Exception:
                    pass
            prefix = f"mina_raw_{uid_tag}."
            try:
                for fname in os.listdir(tmp_dir):
                    if fname.startswith(prefix):
                        os.remove(os.path.join(tmp_dir, fname))
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

    for item in removed:
        remove_wav_file(item.get("wav_path"))

    if removed:
        broadcast_queue_update()
    return removed


def remove_user_queued_songs(requester_id: str, spec: str = "") -> tuple[list[dict], str]:
    """
    Menghapus antrean lagu khusus milik requester_id tertentu.
    Mengembalikan (daftar_lagu_terhapus, status_kode):
    - status_kode: "OK", "EMPTY_USER_QUEUE", atau "NOT_OWNER"
    """
    owner_key = (requester_id or "").strip().lower()
    if not owner_key or not music_pending_items:
        return [], "EMPTY_USER_QUEUE"

    raw = (spec or "").strip()
    user_indices = [
        idx for idx, item in enumerate(music_pending_items)
        if (item.get("requester_id") or "").strip().lower() == owner_key
    ]

    if not raw:
        if not user_indices:
            return [], "EMPTY_USER_QUEUE"
        target_idx = user_indices[-1]
        removed = [music_pending_items.pop(target_idx)]
        remove_wav_file(removed[0].get("wav_path"))
        broadcast_queue_update()
        return removed, "OK"

    if raw.lower() in ("all", "semua"):
        if not user_indices:
            return [], "EMPTY_USER_QUEUE"
        removed = []
        remaining = []
        for idx, item in enumerate(music_pending_items):
            if idx in user_indices:
                removed.append(item)
            else:
                remaining.append(item)
        music_pending_items[:] = remaining
        for item in removed:
            remove_wav_file(item.get("wav_path"))
        broadcast_queue_update()
        return removed, "OK"

    if re.fullmatch(r"\d+", raw):
        pos = int(raw)
        if 1 <= pos <= len(music_pending_items):
            target_item = music_pending_items[pos - 1]
            if (target_item.get("requester_id") or "").strip().lower() != owner_key:
                return [], "NOT_OWNER"
            removed = [music_pending_items.pop(pos - 1)]
            remove_wav_file(removed[0].get("wav_path"))
            broadcast_queue_update()
            return removed, "OK"
        return [], "EMPTY_USER_QUEUE"

    kw = raw.lower()
    matched_other = False
    removed = []
    remaining = []
    for item in music_pending_items:
        title_l = (item.get("title") or "").lower()
        query_l = (item.get("query") or "").lower()
        is_match = kw in title_l or kw in query_l
        if is_match:
            if (item.get("requester_id") or "").strip().lower() == owner_key:
                removed.append(item)
            else:
                matched_other = True
                remaining.append(item)
        else:
            remaining.append(item)

    if removed:
        music_pending_items[:] = remaining
        for item in removed:
            remove_wav_file(item.get("wav_path"))
        broadcast_queue_update()
        return removed, "OK"

    if matched_other:
        return [], "NOT_OWNER"
    return [], "EMPTY_USER_QUEUE"


def can_user_skip_current_song(requester_id: str, is_host: bool = False) -> tuple[bool, str]:
    curr = music_state.get("current_item")
    if curr is None:
        return False, "NO_SONG"
    if is_host:
        return True, "OK"
    owner_key = (requester_id or "").strip().lower()
    curr_owner = (curr.get("requester_id") or "").strip().lower()
    if owner_key and curr_owner and owner_key == curr_owner:
        return True, "OK"
    return False, "NOT_OWNER"


def remove_queued_song_by_uid(uid: int) -> dict | None:
    for idx, item in enumerate(music_pending_items):
        if item.get("uid") == uid:
            removed = music_pending_items.pop(idx)
            remove_wav_file(removed.get("wav_path"))
            broadcast_queue_update()
            return removed
    return None


register_queue_callbacks(remove_queued_song_by_uid, remove_queued_songs)


def clear_all_queued_and_buffers():
    for item in music_pending_items:
        remove_wav_file(item.get("wav_path"))
    music_pending_items.clear()
    ap_buf = music_state.get("autoplay_prefetched")
    if ap_buf:
        remove_wav_file(ap_buf.get("wav_path"))
        music_state["autoplay_prefetched"] = None
    music_queue_event.clear()
    broadcast_queue_update()


async def music_prefetch_worker():
    while True:
        did_work = False

        for item in list(music_pending_items[:3]):
            if item.get("status") == "queued":
                did_work = True
                item["status"] = "downloading"
                broadcast_queue_update()
                TermUI.log("MUSIC", f"Pre-download antrean: {item['query']} (Req: {item['requester']})...")
                try:
                    deadline = time.time() + 58.0
                    title, duration, wav_path = await asyncio.wait_for(
                        asyncio.to_thread(prepare_song_wav, item, deadline),
                        timeout=65.0,
                    )
                    if item not in music_pending_items:
                        remove_wav_file(wav_path)
                        continue
                    item["title"] = title
                    item["duration"] = duration
                    item["wav_path"] = wav_path
                    item["status"] = "ready"
                    TermUI.log("MUSIC", f"Siap putar (Pre-fetched): {title}", TermUI.CYAN)
                except Exception as e:
                    err_txt = str(e) or type(e).__name__
                    item["status"] = "failed"
                    item["error"] = err_txt
                    TermUI.log("WARN", f"Pre-download gagal ({item['query']}): {err_txt}")
                finally:
                    ev = item.get("ready_event")
                    if isinstance(ev, asyncio.Event):
                        ev.set()
                    broadcast_queue_update()
                break

        if (
            not did_work
            and not music_pending_items
            and music_state["autoplay"]
            and not music_state["stop_flag"]
            and music_state["last_seed_query"]
            and music_state["current_item"] is not None
            and music_state.get("autoplay_prefetched") is None
            and (time.time() - music_state["started_at"]) >= 10.0
        ):
            did_work = True
            seed_now = music_state["last_seed_query"]
            last_now = music_state["last_played_title"]
            try:
                next_song = await asyncio.wait_for(
                    asyncio.to_thread(
                        generate_autoplay_next_song,
                        seed_now,
                        last_now,
                        list(played_music_history),
                    ),
                    timeout=12.0,
                )
                if not music_pending_items and music_state["autoplay"] and not music_state["stop_flag"]:
                    ap_item = {
                        "uid": next(song_id_counter),
                        "query": next_song,
                        "title": next_song,
                        "requester": "Autoplay AI",
                        "requester_id": "__autoplay__",
                        "duration": 210,
                        "webpage_url": "",
                        "status": "downloading",
                        "wav_path": None,
                        "ready_event": asyncio.Event(),
                    }
                    deadline = time.time() + 58.0
                    title, duration, wav_path = await asyncio.wait_for(
                        asyncio.to_thread(prepare_song_wav, ap_item, deadline),
                        timeout=65.0,
                    )
                    if music_state["stop_flag"] or not music_state["autoplay"]:
                        remove_wav_file(wav_path)
                    else:
                        ap_item["title"] = title
                        ap_item["duration"] = duration
                        ap_item["wav_path"] = wav_path
                        ap_item["status"] = "ready"
                        ap_item["ready_event"].set()
                        music_state["autoplay_prefetched"] = ap_item
                        TermUI.log("MUSIC", f"Autoplay berikutnya sudah siap: {title}", TermUI.CYAN)
            except Exception:
                await asyncio.sleep(4.0)

        if not did_work:
            music_prefetch_event.clear()
            try:
                await asyncio.wait_for(music_prefetch_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass


async def music_player_worker():
    TermUI.log("MUSIC", "Pemutar musik latar VB-Cable + Pre-Fetch Queue & Autoplay aktif.")
    broadcast_queue_update()
    autoplay_fail_streak = 0

    while True:
        is_autoplay_track = False
        song_item = None

        if music_pending_items:
            music_state["stop_flag"] = False
            autoplay_fail_streak = 0
            song_item = music_pending_items.pop(0)
            if not music_pending_items:
                music_queue_event.clear()
            music_prefetch_event.set()
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

                if autoplay_fail_streak >= 3:
                    await asyncio.sleep(15.0)
                    autoplay_fail_streak = 0
                    continue

                prefetched_ap = music_state.get("autoplay_prefetched")
                music_state["autoplay_prefetched"] = None

                if prefetched_ap and prefetched_ap.get("wav_path") and os.path.exists(prefetched_ap["wav_path"]):
                    song_item = prefetched_ap
                    is_autoplay_track = True
                else:
                    seed_now = music_state["last_seed_query"]
                    last_now = music_state["last_played_title"]
                    TermUI.log("MUSIC", f"Autoplay mencari lagu se-vibe dengan '{seed_now}'...")
                    try:
                        next_song = await asyncio.wait_for(
                            asyncio.to_thread(
                                generate_autoplay_next_song,
                                seed_now,
                                last_now,
                                list(played_music_history),
                            ),
                            timeout=12.0,
                        )
                    except asyncio.TimeoutError:
                        next_song = f"{seed_now} official audio"

                    if music_pending_items or music_state["stop_flag"] or not music_state["autoplay"]:
                        continue

                    song_item = {
                        "uid": next(song_id_counter),
                        "query": next_song,
                        "title": next_song,
                        "requester": "Autoplay AI",
                        "requester_id": "__autoplay__",
                        "duration": 210,
                        "webpage_url": "",
                        "status": "queued",
                        "wav_path": None,
                        "ready_event": asyncio.Event(),
                    }
                    is_autoplay_track = True

        requester_name = song_item["requester"]
        song_query = song_item["query"]
        wav_path = None
        try:
            if music_state["stop_flag"]:
                music_state["stop_flag"] = False
                remove_wav_file(song_item.get("wav_path"))
                continue

            if song_item.get("status") == "downloading":
                ev = song_item.get("ready_event")
                if isinstance(ev, asyncio.Event):
                    await asyncio.wait_for(ev.wait(), timeout=66.0)

            if song_item.get("status") == "ready" and song_item.get("wav_path") and os.path.exists(song_item["wav_path"]):
                title = song_item["title"]
                duration = song_item["duration"]
                wav_path = song_item["wav_path"]
            elif song_item.get("status") == "failed":
                raise RuntimeError(song_item.get("error") or "Gagal mengunduh lagu")
            else:
                TermUI.log("MUSIC", f"Memuat audio: {song_query} (Req: {requester_name})...")
                deadline = time.time() + 58.0
                title, duration, wav_path = await asyncio.wait_for(
                    asyncio.to_thread(prepare_song_wav, song_item, deadline),
                    timeout=65.0,
                )

            if music_state["stop_flag"]:
                music_state["stop_flag"] = False
                remove_wav_file(wav_path)
                continue

            autoplay_fail_streak = 0
            ensure_audio_ready()
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
            music_prefetch_event.set()
            broadcast_queue_update()

            ch.set_volume(music_state["volume"])
            ch.play(sound)
            TermUI.log(
                "MUSIC",
                f"Playing: {title} [{format_duration_mmss(actual_dur)}] | Vol: {int(music_state['volume'] * 100)}%",
                TermUI.GREEN,
            )

            if is_autoplay_track:
                await _notify_discord(
                    f"🎶 **Autoplay Memutar:** `{title}` *(Vibes: {music_state['last_seed_query']})*"
                )
            else:
                await _notify_discord(
                    f"🎵 **Memutar Lagu:** `{title}` (Request oleh **{requester_name}**)"
                )

            play_wall_start = time.time()
            while ch.get_busy():
                if music_state["skip_flag"] or music_state["stop_flag"]:
                    ch.stop()
                    break
                if (time.time() - play_wall_start) > (actual_dur + 5.0):
                    ch.stop()
                    break
                ch.set_volume(music_state["volume"])
                await asyncio.sleep(0.2)

        except Exception as e:
            err_detail = str(e) or type(e).__name__
            TermUI.log("ERROR", f"Gagal memutar '{song_query}': {err_detail}")
            played_music_history.append(song_query)
            if is_autoplay_track:
                autoplay_fail_streak += 1
                await asyncio.sleep(min(30.0, 5.0 * autoplay_fail_streak))
            else:
                await _notify_discord(f"⚠️ Gagal memuat lagu `{song_query}`: `{err_detail}`")
                await asyncio.sleep(1.0)
        finally:
            music_state["current"] = None
            music_state["current_item"] = None
            music_state["is_autoplay_playing"] = False
            music_state["started_at"] = 0.0
            music_state["duration"] = 0
            music_state["skip_flag"] = False
            broadcast_queue_update()
            remove_wav_file(wav_path)


def ensure_music_workers_running():
    global _music_worker_task, _prefetch_worker_task
    if _music_worker_task is None or _music_worker_task.done():
        _music_worker_task = asyncio.create_task(music_player_worker())
    if _prefetch_worker_task is None or _prefetch_worker_task.done():
        _prefetch_worker_task = asyncio.create_task(music_prefetch_worker())


def enqueue_song(requester_name: str, song_query: str, requester_id: str = ""):
    ensure_music_workers_running()
    music_state["stop_flag"] = False
    music_state["last_seed_query"] = song_query
    owner_key = (requester_id or requester_name).strip().lower()
    song_item = {
        "uid": next(song_id_counter),
        "query": song_query,
        "title": song_query,
        "requester": requester_name,
        "requester_id": owner_key,
        "duration": 210,
        "webpage_url": "",
        "status": "queued",
        "wav_path": None,
        "ready_event": asyncio.Event(),
    }
    music_pending_items.append(song_item)
    music_queue_event.set()
    music_prefetch_event.set()
    broadcast_queue_update()