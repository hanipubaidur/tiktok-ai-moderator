import os
import re
import sys
import time
import shutil
import asyncio
import itertools
from collections import deque
from datetime import datetime
from dotenv import load_dotenv

os.system("")
load_dotenv()

BOOT_TIME = time.time()
DATASET_FILE = "dataset.json"
VIEWERS_DB_FILE = "viewers_db.json"
LIVE_QUEUE_TXT_FILE = "live_music_queue.txt"
OVERLAY_HOST = "0.0.0.0"
OVERLAY_PORT = 8765
NEURAL_VOICE = "id-ID-GadisNeural"

gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
betabotz_key = os.getenv("BETABOTZ_API_KEY", "").strip()
openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()
openrouter_custom_model = os.getenv("OPENROUTER_MODEL", "").strip()

discord_token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
control_channel_id = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
tiktok_username = (os.getenv("TIKTOK_USERNAME") or "").strip().lstrip("@")
tiktok_session_id = os.getenv("TIKTOK_SESSION_ID", "").strip()
tiktok_target_idc = os.getenv("TIKTOK_TARGET_IDC", "").strip()

raw_aliases = os.getenv("HOST_ALIASES", "")
HOST_IDENTIFIERS: set[str] = {
    x.strip().lstrip("@").lower()
    for x in raw_aliases.split(",")
    if x.strip()
}
if tiktok_username:
    HOST_IDENTIFIERS.add(tiktok_username.lower())

PRIORITY_HOST = 1
PRIORITY_GIFT = 2
PRIORITY_COMMENT = 3
PRIORITY_SOCIAL = 4

action_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
queue_counter = itertools.count()
song_id_counter = itertools.count(1)
latest_comment_token = {"id": 0}

seen_comments: set[str] = set()
followed_users: set[str] = set()
shared_users_cooldown: dict[str, float] = {}
user_music_cooldown: dict[str, float] = {}

room_chat_history: deque[str] = deque(maxlen=16)
user_chat_threads: dict[str, deque[str]] = {}
user_warnings: dict[str, dict] = {}
viewer_registry: dict[str, dict] = {}
last_escalated_question: dict[str, str | int | None] = {
    "user": None,
    "question": None,
    "msg_id": None,
}

live_session_stats = {
    "total_likes": 0,
    "user_likes": {},
    "user_gifts": {},
    "gift_totals": {},
}

music_pending_items: list[dict] = []
music_queue_event = asyncio.Event()
music_prefetch_event = asyncio.Event()
overlay_subscribers: set[asyncio.Queue] = set()
played_music_history: deque[str] = deque(maxlen=30)

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
    "autoplay_prefetched": None,
}

state = {
    "muted": False,
    "tiktok_connected": False,
    "tiktok_started": False,
    "last_like_shoutout": 0.0,
    "reconnect_delay": 5,
    "last_llm_used": "-",
    "audio_device": "-",
}

llm_cooldowns: dict[str, float] = {}


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
        "HOST": (YELLOW, "HOST   "),
        "CHAT": (WHITE, "CHAT   "),
        "AI-MOD": (GREEN, "AI-MOD "),
        "VOICE": (CYAN, "VOICE  "),
        "MUSIC": (MAGENTA, "MUSIC  "),
        "DISCORD": (BLUE, "DISCORD"),
        "OVERLAY": (CYAN, "OVERLAY"),
        "WARN": (YELLOW, "WARN   "),
        "ERROR": (RED, "ERROR  "),
    }

    @staticmethod
    def _visible_len(text: str) -> int:
        return len(re.sub(r"\033\[[0-9;]*m", "", text))

    @classmethod
    def _fit_text(cls, text: str, max_width: int) -> str:
        if cls._visible_len(text) <= max_width:
            return text
        plain = re.sub(r"\033\[[0-9;]*m", "", text)
        return plain[: max(0, max_width - 1)] + "…"

    @classmethod
    def clear_screen(cls, title: str | None = None):
        os.system("cls" if os.name == "nt" else "clear")
        sys.stdout.write("\033[2J\033[3J\033[H")
        if title:
            sys.stdout.write(f"\033]0;{title}\007")
        sys.stdout.flush()

    @classmethod
    def log(cls, tag: str, message: str, accent: str | None = None):
        now_str = datetime.now().strftime("%H:%M:%S")
        color, label = cls.STYLES.get(tag.upper(), (cls.WHITE, tag.upper().ljust(7)[:7]))
        msg_color = accent or cls.RESET
        print(
            f" {cls.DIM}{now_str}{cls.RESET} {cls.DIM}│{cls.RESET} "
            f"{color}{cls.BOLD}{label}{cls.RESET} {cls.DIM}│{cls.RESET} "
            f"{msg_color}{message}{cls.RESET}"
        )

    @classmethod
    def banner(cls, audio_dev: str, overlay_url: str, active_providers: str):
        target_label = f"@{tiktok_username}" if tiktok_username else "Belum diatur"
        cls.clear_screen(f"MINA AI MODERATOR — {target_label}")

        term_cols = shutil.get_terminal_size((114, 30)).columns
        box_w = max(86, term_cols - 2)
        inner_w = box_w - 2
        left_w = inner_w // 2
        right_w = inner_w - left_w - 1

        c = cls.CYAN
        r = cls.RESET
        b = cls.BOLD
        d = cls.DIM
        w = cls.WHITE
        g = cls.GREEN

        top_border = f" {c}╭{'─' * inner_w}╮{r}"
        split_top = f" {c}├{'─' * left_w}┬{'─' * right_w}┤{r}"
        split_bot = f" {c}├{'─' * left_w}┴{'─' * right_w}┤{r}"
        bot_border = f" {c}╰{'─' * inner_w}╯{r}"

        def full_row(left_text: str, right_text: str = "") -> str:
            l_fit = cls._fit_text(left_text, inner_w - 2)
            r_fit = cls._fit_text(right_text, inner_w - cls._visible_len(l_fit) - 3)
            space = max(1, inner_w - 2 - cls._visible_len(l_fit) - cls._visible_len(r_fit))
            return f" {c}│{r} {l_fit}{' ' * space}{r_fit} {c}│{r}"

        def dual_row(left_text: str, right_text: str) -> str:
            l_fit = cls._fit_text(left_text, left_w - 2)
            r_fit = cls._fit_text(right_text, right_w - 2)
            l_pad = max(0, left_w - 2 - cls._visible_len(l_fit))
            r_pad = max(0, right_w - 2 - cls._visible_len(r_fit))
            return f" {c}│{r} {l_fit}{' ' * l_pad} {c}│{r} {r_fit}{' ' * r_pad} {c}│{r}"

        mod_mode = (
            f"{g}API Auto-Mod Aktif{r}"
            if tiktok_session_id
            else f"{cls.YELLOW}Alert Mode (Non-Session){r}"
        )
        boot_clock = datetime.now().strftime("%d %b %Y • %H:%M WIB")

        print("")
        print(top_border)
        print(
            full_row(
                f"{b}{c}◈ MINA AI CO-HOST, MODERATOR & LIVE ENGINE{r}",
                f"{g}● READY{r}  {d}│{r}  {w}{boot_clock}{r}",
            )
        )
        print(split_top)
        print(
            dual_row(
                f"{d}Target Live  :{r} {b}{cls.MAGENTA}{target_label}{r}",
                f"{d}LLM Cascade  :{r} {w}{active_providers}{r}",
            )
        )
        print(
            dual_row(
                f"{d}Audio Output :{r} {w}{audio_dev}{r}",
                f"{d}Moderasi     :{r} {mod_mode}",
            )
        )
        print(
            dual_row(
                f"{d}Dashboard    :{r} {c}{overlay_url}{r}",
                f"{d}Music Engine :{r} {g}Pre-Fetch Queue + Autoplay{r}",
            )
        )
        print(split_bot)
        print(
            full_row(
                f"{d}Controls:{r} {w}!panel{r} {d}(Discord UI){r}  •  "
                f"{w}!openoverlay{r} {d}(Dashboard){r}  •  "
                f"{w}!clear{r} {d}(Reset Layar){r}  •  {w}Ctrl+C{r} {d}(Stop){r}"
            )
        )
        print(bot_border)

        section_title = " LIVE ACTIVITY STREAM "
        side_len = max(4, (box_w - len(section_title)) // 2)
        rem_len = max(4, box_w - len(section_title) - side_len)
        print(f"\n {d}{'─' * side_len}{c}{b}{section_title}{r}{d}{'─' * rem_len}{r}\n")


def get_active_llm_providers_label() -> str:
    names = []
    if gemini_key:
        names.append("Gemini")
    if betabotz_key:
        names.append("BetaBotz")
    if openrouter_key:
        names.append("OpenRouter")
    return " -> ".join(names) if names else "Tidak ada API Key terpasang"


def is_llm_on_cooldown(key: str) -> bool:
    until = llm_cooldowns.get(key, 0.0)
    if until <= 0:
        return False
    if time.time() >= until:
        llm_cooldowns.pop(key, None)
        return False
    return True


def set_llm_cooldown(key: str, error_obj: Exception, label: str):
    err_str = str(error_obj).lower()
    if any(code in err_str for code in ("402", "insufficient balance", "401", "invalid_api_key", "unauthorized")):
        seconds = 1800.0
    elif any(code in err_str for code in ("503", "unavailable", "429", "rate limit", "quota", "timeout", "timed out")):
        seconds = 60.0
    else:
        seconds = 25.0

    already = is_llm_on_cooldown(key)
    llm_cooldowns[key] = time.time() + seconds
    if not already:
        TermUI.log("WARN", f"{label} fallback (cooldown {int(seconds)}s): {error_obj}")


def is_host_account(unique_id: str, display_name: str = "") -> bool:
    uid_clean = (unique_id or "").strip().lstrip("@").lower()
    name_clean = (display_name or "").strip().lower()
    if uid_clean and uid_clean in HOST_IDENTIFIERS:
        return True
    if name_clean and name_clean in HOST_IDENTIFIERS:
        return True
    return False