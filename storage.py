import os
import re
import time
import json
from collections import deque
from datetime import datetime
from difflib import SequenceMatcher
from config import (
    DATASET_FILE,
    VIEWERS_DB_FILE,
    SESSION_STATS_FILE,
    HOST_IDENTIFIERS,
    tiktok_username,
    TermUI,
    room_chat_history,
    user_chat_threads,
    user_warnings,
    viewer_registry,
    live_session_stats,
)

_dataset_cache = {"mtime": 0.0, "data": None}
_viewers_cache: dict[str, dict] | None = None
_session_counted_viewers: set[str] = set()


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

        for alias in parsed.get("host_aliases", []):
            if isinstance(alias, str) and alias.strip():
                HOST_IDENTIFIERS.add(alias.strip().lstrip("@").lower())
        return parsed
    except Exception:
        with open(DATASET_FILE, "r", encoding="utf-8") as f:
            return json.load(f)


def write_dataset_dict(data: dict):
    with open(DATASET_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _dataset_cache["data"] = data
    _dataset_cache["mtime"] = os.path.getmtime(DATASET_FILE)


def load_viewers_db() -> dict[str, dict]:
    global _viewers_cache
    if _viewers_cache is not None:
        return _viewers_cache
    if not os.path.exists(VIEWERS_DB_FILE):
        _viewers_cache = {}
        return _viewers_cache
    try:
        with open(VIEWERS_DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _viewers_cache = data if isinstance(data, dict) else {}
    except Exception:
        _viewers_cache = {}
    return _viewers_cache


def save_viewers_db():
    db = load_viewers_db()
    try:
        with open(VIEWERS_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
    except Exception as e:
        TermUI.log("WARN", f"Gagal menyimpan {VIEWERS_DB_FILE}: {e}")


def save_session_stats():
    live_session_stats["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tmp_path = f"{SESSION_STATS_FILE}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(live_session_stats, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, SESSION_STATS_FILE)
    except Exception as e:
        TermUI.log("WARN", f"Gagal menyimpan {SESSION_STATS_FILE}: {e}")


def load_session_stats():
    live_session_stats.setdefault("live_started_at", 0.0)
    if not os.path.exists(SESSION_STATS_FILE):
        return
    try:
        with open(SESSION_STATS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            return

        saved_host = str(saved.get("host_username") or "").strip().lower()
        curr_host = (tiktok_username or "").strip().lower()
        if saved_host and curr_host and saved_host != curr_host:
            reset_session_stats()
            return

        live_session_stats["room_id"] = int(saved.get("room_id") or 0)
        live_session_stats["host_username"] = tiktok_username
        live_session_stats["is_live_active"] = bool(saved.get("is_live_active", False))
        live_session_stats["live_started_at"] = float(saved.get("live_started_at") or 0.0)
        live_session_stats["updated_at"] = str(saved.get("updated_at") or "")
        live_session_stats["total_likes"] = int(saved.get("total_likes") or 0)
        live_session_stats["user_likes"] = (
            saved.get("user_likes") if isinstance(saved.get("user_likes"), dict) else {}
        )
        live_session_stats["user_gifts"] = (
            saved.get("user_gifts") if isinstance(saved.get("user_gifts"), dict) else {}
        )
        live_session_stats["gift_totals"] = (
            saved.get("gift_totals") if isinstance(saved.get("gift_totals"), dict) else {}
        )
    except Exception as e:
        TermUI.log("WARN", f"Gagal memuat {SESSION_STATS_FILE}: {e}")


def reset_session_stats():
    live_session_stats["room_id"] = 0
    live_session_stats["host_username"] = tiktok_username
    live_session_stats["is_live_active"] = False
    live_session_stats["live_started_at"] = 0.0
    live_session_stats["total_likes"] = 0
    live_session_stats["user_likes"].clear()
    live_session_stats["user_gifts"].clear()
    live_session_stats["gift_totals"].clear()
    _session_counted_viewers.clear()
    save_session_stats()


def sync_live_session_room(room_id: int) -> bool:
    """
    Sinkronisasi room_id saat ConnectEvent.
    Mempertahankan total durasi live & leaderboard saat reconnect ke room yang sama.
    Mengembalikan True jika sesi live baru terdeteksi dan stats di-reset.
    """
    was_reset = False
    prev_room_id = int(live_session_stats.get("room_id") or 0)
    was_active = bool(live_session_stats.get("is_live_active", False))
    prev_started_at = float(live_session_stats.get("live_started_at") or 0.0)
    has_existing_data = bool(
        live_session_stats["total_likes"] > 0
        or live_session_stats["user_likes"]
        or live_session_stats["user_gifts"]
    )

    if not was_active and has_existing_data:
        reset_session_stats()
        was_reset = True
    elif room_id > 0 and prev_room_id > 0 and room_id != prev_room_id:
        reset_session_stats()
        was_reset = True

    if room_id > 0:
        live_session_stats["room_id"] = room_id
    live_session_stats["host_username"] = tiktok_username
    live_session_stats["is_live_active"] = True

    if was_reset or not was_active or prev_started_at <= 0.0:
        live_session_stats["live_started_at"] = time.time()

    save_session_stats()
    return was_reset


def mark_live_session_ended() -> bool:
    """
    Dipanggil hanya saat live benar-benar berakhir (End Live).
    Membersihkan durasi sesi & leaderboard dan mengembalikan True jika memang ada sesi yang diakhiri.
    """
    had_session = bool(
        live_session_stats.get("is_live_active")
        or float(live_session_stats.get("live_started_at") or 0.0) > 0.0
        or live_session_stats["total_likes"] > 0
        or live_session_stats["user_likes"]
        or live_session_stats["user_gifts"]
    )
    reset_session_stats()
    return had_session


def get_clean_nickname(user_obj) -> str:
    raw_nick = getattr(user_obj, "nickname", "") or ""
    clean_nick = re.sub(r"[^\w\s]", "", raw_nick).strip()
    return clean_nick if clean_nick else "Kak"


def get_user_unique_id(user_obj) -> str:
    return str(
        getattr(user_obj, "unique_id", None)
        or getattr(user_obj, "display_id", None)
        or get_clean_nickname(user_obj)
    )


def get_user_numeric_id(user_obj) -> int:
    uid = getattr(user_obj, "id", 0) or getattr(user_obj, "user_id", 0) or 0
    try:
        return int(uid)
    except (ValueError, TypeError):
        return 0


def get_user_sec_uid(user_obj) -> str:
    return str(getattr(user_obj, "sec_uid", "") or "")


def register_viewer(user_obj, is_chat: bool = False):
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

    db = load_viewers_db()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    profile = db.get(key)
    if not profile:
        profile = {
            "unique_id": unique_id,
            "numeric_uid": numeric_uid,
            "name": display_name,
            "visit_count": 1,
            "chat_count": 0,
            "total_likes": 0,
            "total_gift_items": 0,
            "gifts": {},
            "notes": [],
            "first_seen": now_str,
            "last_seen": now_str,
        }
        db[key] = profile
        _session_counted_viewers.add(key)
    else:
        profile["name"] = display_name
        if numeric_uid:
            profile["numeric_uid"] = numeric_uid
        if key not in _session_counted_viewers:
            profile["visit_count"] = int(profile.get("visit_count", 0)) + 1
            _session_counted_viewers.add(key)
        profile["last_seen"] = now_str

    if is_chat:
        profile["chat_count"] = int(profile.get("chat_count", 0)) + 1
        if profile["chat_count"] % 5 == 0:
            save_viewers_db()


def is_returning_viewer(unique_id: str) -> bool:
    key = (unique_id or "").lower().strip()
    db = load_viewers_db()
    profile = db.get(key)
    if not profile:
        return False
    return int(profile.get("visit_count", 1)) >= 2 or int(profile.get("chat_count", 0)) >= 1


def add_viewer_memory_note(unique_id: str, note: str):
    clean_note = (note or "").strip()
    if not clean_note or len(clean_note) < 3:
        return
    key = unique_id.lower().strip()
    db = load_viewers_db()
    profile = db.get(key)
    if not profile:
        return
    notes: list[str] = profile.setdefault("notes", [])
    norm_new = normalize_text(clean_note)
    if any(normalize_text(n) == norm_new for n in notes):
        return
    notes.append(clean_note[:100])
    if len(notes) > 6:
        notes.pop(0)
    save_viewers_db()


def get_viewer_memory_summary(unique_id: str, display_name: str) -> str:
    key = unique_id.lower().strip()
    db = load_viewers_db()
    profile = db.get(key)
    if not profile:
        return f"{display_name} (Viewer baru di room)"

    visits = int(profile.get("visit_count", 1))
    chats = int(profile.get("chat_count", 0))
    likes = int(profile.get("total_likes", 0))
    gift_items = int(profile.get("total_gift_items", 0))
    notes = profile.get("notes", [])

    tier = "Viewer Baru"
    if visits >= 4 or chats >= 25 or gift_items >= 5:
        tier = "Viewer Langganan / Akrab"
    elif visits >= 2 or chats >= 6:
        tier = "Viewer Sering Mampir"

    parts = [f"Status: {tier} ({visits}x sesi live, {chats} chat"]
    if likes > 0:
        parts.append(f"{likes} like")
    if gift_items > 0:
        parts.append(f"{gift_items} gift")
    summary = ", ".join(parts) + ")"
    if notes:
        summary += f" | Fakta viewer ini: {'; '.join(notes)}"
    return summary


def record_user_chat_turn(unique_id: str, display_name: str, text: str):
    key = unique_id.lower().strip()
    if key not in user_chat_threads:
        user_chat_threads[key] = deque(maxlen=6)
    now_str = datetime.now().strftime("%H:%M")
    user_chat_threads[key].append(f"[{now_str}] {display_name}: {text}")


def get_user_recent_thread(unique_id: str) -> str:
    key = unique_id.lower().strip()
    thread = user_chat_threads.get(key)
    if not thread:
        return "-"
    return " | ".join(thread)


def record_room_event_memory(line: str):
    now_str = datetime.now().strftime("%H:%M")
    room_chat_history.append(f"[{now_str}] {line}")


def record_viewer_like(user_obj, delta_count: int = 1, room_total: int = 0):
    if not user_obj:
        return
    register_viewer(user_obj, is_chat=False)
    unique_id = get_user_unique_id(user_obj)
    display_name = get_clean_nickname(user_obj)
    key = unique_id.lower().strip()

    inc = max(1, int(delta_count or 1))
    entry = live_session_stats["user_likes"].setdefault(
        key,
        {"unique_id": unique_id, "name": display_name, "count": 0},
    )
    entry["name"] = display_name
    entry["count"] += inc

    if room_total > live_session_stats["total_likes"]:
        live_session_stats["total_likes"] = room_total
    else:
        live_session_stats["total_likes"] += inc

    db = load_viewers_db()
    if key in db:
        db[key]["total_likes"] = int(db[key].get("total_likes", 0)) + inc

    save_session_stats()


def record_viewer_gift(user_obj, gift_name: str, count: int = 1):
    if not user_obj:
        return
    register_viewer(user_obj, is_chat=False)
    unique_id = get_user_unique_id(user_obj)
    display_name = get_clean_nickname(user_obj)
    key = unique_id.lower().strip()
    qty = max(1, int(count or 1))
    clean_gift = (gift_name or "Gift").strip()

    g_entry = live_session_stats["user_gifts"].setdefault(
        key,
        {
            "unique_id": unique_id,
            "name": display_name,
            "total_items": 0,
            "gifts": {},
        },
    )
    g_entry["name"] = display_name
    g_entry["total_items"] += qty
    g_entry["gifts"][clean_gift] = int(g_entry["gifts"].get(clean_gift, 0)) + qty

    live_session_stats["gift_totals"][clean_gift] = (
        int(live_session_stats["gift_totals"].get(clean_gift, 0)) + qty
    )

    db = load_viewers_db()
    if key in db:
        db[key]["total_gift_items"] = int(db[key].get("total_gift_items", 0)) + qty
        user_db_gifts = db[key].setdefault("gifts", {})
        user_db_gifts[clean_gift] = int(user_db_gifts.get(clean_gift, 0)) + qty
        save_viewers_db()

    save_session_stats()


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
    record_room_event_memory(f"[UPDATE TOPIK HOST]: {new_topic}")
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
    record_room_event_memory(f"[UPDATE KONTEKS HOST]: {new_status}")
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