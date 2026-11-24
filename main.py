import os
import re
import time
import random
import asyncio
import logging
import importlib
from datetime import datetime
from urllib.parse import urlparse
import httpx
import discord
import pygame

from config import (
    BOOT_TIME,
    OVERLAY_PORT,
    discord_token,
    control_channel_id,
    tiktok_username,
    tiktok_session_id,
    tiktok_target_idc,
    PRIORITY_HOST,
    PRIORITY_GIFT,
    PRIORITY_COMMENT,
    PRIORITY_SOCIAL,
    action_queue,
    queue_counter,
    latest_comment_token,
    seen_comments,
    followed_users,
    shared_users_cooldown,
    welcomed_users_cooldown,
    user_music_cooldown,
    user_warnings,
    last_escalated_question,
    live_session_stats,
    music_pending_items,
    music_state,
    state,
    TermUI,
    get_active_llm_providers_label,
    is_host_account,
)

if tiktok_session_id and not os.getenv("WHITELIST_AUTHENTICATED_SESSION_ID_HOST"):
    os.environ["WHITELIST_AUTHENTICATED_SESSION_ID_HOST"] = "api.eulerstream.com"

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

try:
    from TikTokLive.events import JoinEvent
except ImportError:
    JoinEvent = None

try:
    from TikTokLive.events import LiveEndEvent
except ImportError:
    LiveEndEvent = None

try:
    from TikTokLive.events import ControlEvent
except ImportError:
    ControlEvent = None

WebDefaults = None
for _mod_path in ("TikTokLive.client.web.web_defaults", "TikTokLive.client.web.web_settings"):
    try:
        WebDefaults = getattr(importlib.import_module(_mod_path), "WebDefaults", None)
        if WebDefaults is not None:
            break
    except Exception:
        pass

from storage import (
    load_dataset_dict,
    save_viewers_db,
    load_session_stats,
    save_session_stats,
    sync_live_session_room,
    mark_live_session_ended,
    is_returning_viewer,
    get_clean_nickname,
    get_user_unique_id,
    get_user_numeric_id,
    get_user_sec_uid,
    register_viewer,
    record_user_chat_turn,
    record_room_event_memory,
    record_viewer_like,
    record_viewer_gift,
    resolve_target_viewer,
    is_user_permanently_banned,
    add_banned_user_record,
    remove_banned_user_record,
    normalize_text,
    check_badword_violation,
    find_permanent_memory_answer,
    update_live_topic,
    update_live_context,
    forget_learned_qa,
    add_badword_to_dataset,
    remove_badword_from_dataset,
)
from overlay_dashboard import (
    format_duration_mmss,
    get_live_dashboard_snapshot,
    broadcast_queue_update,
    launch_overlay_app_window,
    start_overlay_services,
    stop_overlay_server,
)
from ai_moderator import (
    are_all_llms_on_cooldown,
    save_learned_qa,
    run_moderator_reasoning,
    transcribe_voice_note,
    speak_to_vbcable,
)
from music_engine import (
    cleanup_stale_temp_files,
    init_vbcable,
    ensure_audio_ready,
    remove_queued_songs,
    remove_user_queued_songs,
    can_user_skip_current_song,
    remove_queued_song_by_uid,
    clear_all_queued_and_buffers,
    ensure_music_workers_running,
    enqueue_song,
    set_music_discord_notifier,
)

load_session_stats()

MUSIC_REQUEST_PREFIXES = ("!play ", "/play ", "play ", "req ", "request ", "m!p ")


class TermUILogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            if any(k in msg.lower() for k in ("heartbeat", "ping", "pong")):
                return
            tag = "ERROR" if record.levelno >= logging.ERROR else "WARN"
            accent = TermUI.RED if record.levelno >= logging.ERROR else TermUI.YELLOW
            TermUI.log(tag, f"[{record.name}] {msg}", accent)
        except Exception:
            pass


def setup_internal_error_logging():
    handler = TermUILogHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))

    lg_tiktok = logging.getLogger("TikTokLive")
    lg_tiktok.setLevel(logging.DEBUG)
    lg_tiktok.handlers.clear()
    lg_tiktok.addHandler(handler)
    lg_tiktok.propagate = False

    for logger_name in ("pyee", "httpx"):
        lg = logging.getLogger(logger_name)
        lg.setLevel(logging.WARNING)
        lg.handlers.clear()
        lg.addHandler(handler)
        lg.propagate = False


setup_internal_error_logging()

tiktok_disconnected_event = asyncio.Event()
worker_task = None
tiktok_task = None
connected_at = None
_viewer_music_warn_cooldown: dict[str, float] = {}
_last_all_cooldown_notice = 0.0


def get_resolved_tiktok_cookies() -> dict[str, str]:
    raw_idc = (tiktok_target_idc or "").strip()
    raw_idc_sign = os.getenv("TIKTOK_TARGET_IDC_SIGN", "").strip()
    raw_ttwid = os.getenv("TIKTOK_TTWID", "").strip()
    raw_ms_token = os.getenv("TIKTOK_MS_TOKEN", "").strip()

    if len(raw_idc) > 20:
        if not raw_idc_sign:
            raw_idc_sign = raw_idc
        clean_idc = "alisg"
    else:
        clean_idc = raw_idc or "alisg"

    cookies = {
        "sessionid": tiktok_session_id,
        "sessionid_ss": tiktok_session_id,
        "tt-target-idc": clean_idc,
    }
    if raw_idc_sign:
        cookies["tt-target-idc-sign"] = raw_idc_sign
    if raw_ttwid:
        cookies["ttwid"] = raw_ttwid
    if raw_ms_token:
        cookies["msToken"] = raw_ms_token
    return cookies


def should_authenticate_ws() -> bool:
    if not tiktok_session_id:
        return False
    ws_pref = os.getenv("TIKTOK_WS_AUTH", "auto").strip().lower()
    return ws_pref in ("true", "1", "yes", "on")


def drain_non_host_actions():
    kept = []
    while not action_queue.empty():
        try:
            item = action_queue.get_nowait()
            action_queue.task_done()
            if item[0] <= PRIORITY_HOST:
                kept.append(item)
        except asyncio.QueueEmpty:
            break
    for item in kept:
        action_queue.put_nowait(item)


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


async def notify_discord_channel(text: str):
    channel = await get_control_channel()
    if channel:
        await channel.send(text)


set_music_discord_notifier(notify_discord_channel)


def _discover_webcast_hosts(idc_region: str, probed_hosts: list[str] | None = None) -> list[str]:
    candidates: list[str] = []
    if probed_hosts:
        candidates.extend(probed_hosts)

    idc_clean = (idc_region or "alisg").strip().lower()
    if idc_clean in ("alisg", "ap-southeast-1", "sg"):
        candidates.extend(
            [
                "https://webcast16-normal-c-alisg.tiktok.com",
                "https://webcast16-normal-alisg.tiktok.com",
                "https://webcast-sg.tiktok.com",
                "https://webcast.tiktok.com",
            ]
        )
    elif idc_clean in ("maliva", "useast1a", "useast2a", "us"):
        candidates.extend(
            [
                f"https://webcast16-normal-c-{idc_clean}.tiktok.com",
                "https://webcast-va.tiktok.com",
                "https://webcast.tiktok.com",
            ]
        )
    else:
        candidates.extend(
            [
                f"https://webcast16-normal-c-{idc_clean}.tiktok.com",
                f"https://webcast-{idc_clean}.tiktok.com",
                "https://webcast.tiktok.com",
                "https://webcast16-normal-c-alisg.tiktok.com",
                "https://webcast-sg.tiktok.com",
            ]
        )

    return list(dict.fromkeys(candidates))


async def _probe_webcast_session(client: httpx.AsyncClient, room_id: int) -> list[str]:
    discovered: list[str] = []
    probe_urls = [
        f"https://webcast.tiktok.com/webcast/room/info/?aid=1988&app_name=tiktok_web&device_platform=web_pc&room_id={room_id}",
        f"https://www.tiktok.com/@{tiktok_username}/live",
    ]
    for url in probe_urls:
        try:
            resp = await client.get(url, follow_redirects=True)
            final_host = f"{resp.url.scheme}://{resp.url.host}"
            if "webcast" in resp.url.host and resp.url.host.endswith("tiktok.com"):
                discovered.append(final_host)
            idc_hdr = resp.headers.get("x-tt-idc") or resp.headers.get("tt-target-idc")
            if idc_hdr and len(idc_hdr) < 20:
                discovered.append(f"https://webcast16-normal-c-{idc_hdr.strip()}.tiktok.com")
        except Exception:
            pass
    return list(dict.fromkeys(discovered))


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
                f"⚠️ Eksekusi API otomatis gagal karena `TIKTOK_SESSION_ID` belum terpasang di `.env`."
            )
        return

    if not room_id or not numeric_uid:
        if channel and not silent:
            await channel.send(
                f"⚠️ Tidak dapat mengeksekusi `{mode.upper()}` untuk **{display_name}** (`@{unique_id}`) "
                f"karena `room_id` atau `numeric_uid` tidak ditemukan."
            )
        return

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": f"https://www.tiktok.com/@{tiktok_username}/live",
        "Origin": "https://www.tiktok.com",
        "Accept": "application/json, text/plain, */*",
    }
    init_cookies = get_resolved_tiktok_cookies()
    idc_region = init_cookies.get("tt-target-idc", "alisg")

    base_params = {
        "aid": 1988,
        "app_name": "tiktok_web",
        "device_platform": "web_pc",
        "cookie_enabled": "true",
        "browser_language": "id-ID",
        "browser_platform": "Win32",
        "browser_name": "Mozilla",
        "browser_version": "5.0",
    }

    async def _post_webcast(client: httpx.AsyncClient, hosts: list[str], path: str, extra_params: dict, form_data: dict) -> dict:
        last_json = {}
        ms_tok = client.cookies.get("msToken") or init_cookies.get("msToken")
        csrf_tok = client.cookies.get("passport_csrf_token")
        req_headers = {}
        if csrf_tok:
            req_headers["x-tt-passport-csrf-token"] = csrf_tok

        path_variants = [path, path.rstrip("/")] if path.endswith("/") else [path, f"{path}/"]

        for host in hosts:
            for p_var in path_variants:
                query_params = {**base_params, **extra_params}
                if ms_tok:
                    query_params["msToken"] = ms_tok
                try:
                    resp = await client.post(
                        f"{host}{p_var}",
                        params=query_params,
                        data=form_data,
                        headers=req_headers,
                    )
                    if resp.status_code != 200:
                        last_json = {"status_code": resp.status_code, "status_msg": f"HTTP {resp.status_code}"}
                        continue
                    data_json = resp.json()
                    last_json = data_json
                    err_txt = str(data_json.get("data", {}).get("prompts") or data_json.get("status_msg") or "")
                    if "url does not match" in err_txt.lower():
                        continue
                    return data_json
                except Exception as req_err:
                    last_json = {"status_code": -1, "status_msg": str(req_err)}
        return last_json

    try:
        jar = httpx.Cookies()
        for c_k, c_v in init_cookies.items():
            jar.set(c_k, c_v, domain=".tiktok.com")

        async with httpx.AsyncClient(headers=headers, cookies=jar, timeout=8.0) as client:
            probed_hosts = await _probe_webcast_session(client, room_id)
            webcast_hosts = _discover_webcast_hosts(idc_region, probed_hosts)

            if mode == "mute":
                mute_fields = {
                    "room_id": room_id,
                    "user_id": numeric_uid,
                    "silence_type": 1,
                    "mute_type": 1,
                    "duration": 300,
                }
                if sec_uid:
                    mute_fields["sec_user_id"] = sec_uid

                res_json = await _post_webcast(
                    client,
                    webcast_hosts,
                    "/webcast/room/mute_user/",
                    mute_fields,
                    mute_fields,
                )
                if res_json.get("status_code") == 0:
                    TermUI.log("AI-MOD", f"MUTE 5m berhasil: {display_name} (@{unique_id})", TermUI.YELLOW)
                    if channel and not silent:
                        await channel.send(
                            f"🔇 **MUTE BERHASIL:** **{display_name}** (`@{unique_id}`) dibisukan 5 menit."
                        )
                    return
                err_msg = res_json.get("data", {}).get("prompts") or res_json.get("status_msg") or "Ditolak server"
                TermUI.log("WARN", f"Mute API gagal ({err_msg}) | resp={res_json}", TermUI.YELLOW)
                if channel and not silent:
                    await channel.send(
                        f"⚠️ Server TikTok menolak `MUTE` untuk **{display_name}** (`{err_msg}`)."
                    )

            elif mode == "kick":
                kick_fields = {
                    "room_id": room_id,
                    "kick_uid": numeric_uid,
                }
                if sec_uid:
                    kick_fields["sec_user_id"] = sec_uid

                res_json = await _post_webcast(
                    client,
                    webcast_hosts,
                    "/webcast/room/kick_user/",
                    kick_fields,
                    kick_fields,
                )
                if res_json.get("status_code") == 0:
                    TermUI.log("AI-MOD", f"KICK berhasil: {display_name} (@{unique_id})", TermUI.RED)
                    if channel and not silent:
                        await channel.send(
                            f"👢 **KICK BERHASIL:** **{display_name}** (`@{unique_id}`) dikeluarkan dari live."
                        )
                    return
                err_msg = res_json.get("data", {}).get("prompts") or res_json.get("status_msg") or "Ditolak server"
                TermUI.log("WARN", f"Kick API gagal ({err_msg}) | resp={res_json}", TermUI.RED)
                if channel and not silent:
                    await channel.send(
                        f"⚠️ Server TikTok menolak `KICK` untuk **{display_name}** (`{err_msg}`)."
                    )

            elif mode == "ban":
                block_payload = {"target_uid": numeric_uid, "op": 1}
                if sec_uid:
                    block_payload["sec_target_uid"] = sec_uid

                block_json = await _post_webcast(
                    client,
                    webcast_hosts,
                    "/webcast/user/relation/update/",
                    block_payload,
                    block_payload,
                )
                block_ok = block_json.get("status_code") == 0

                kick_fields = {
                    "room_id": room_id,
                    "kick_uid": numeric_uid,
                }
                if sec_uid:
                    kick_fields["sec_user_id"] = sec_uid

                kick_json = await _post_webcast(
                    client,
                    webcast_hosts,
                    "/webcast/room/kick_user/",
                    kick_fields,
                    kick_fields,
                )
                kick_ok = kick_json.get("status_code") == 0

                if block_ok or kick_ok:
                    detail = (
                        "Blokir Permanen & Kick"
                        if (block_ok and kick_ok)
                        else ("Blokir Permanen" if block_ok else "Kick + Ban Lokal")
                    )
                    TermUI.log("AI-MOD", f"BAN berhasil: {display_name} (@{unique_id}) -> {detail}", TermUI.RED)
                    if channel and not silent:
                        await channel.send(
                            f"⛔ **PERMANENT BAN BERHASIL:** **{display_name}** (`@{unique_id}`) (`{detail}`)."
                        )
                    return

                err_msg = kick_json.get("data", {}).get("prompts") or block_json.get("status_msg") or "Ditolak server"
                TermUI.log("WARN", f"Ban API gagal ({err_msg}) | kick={kick_json} block={block_json}", TermUI.RED)
                if channel and not silent:
                    await channel.send(
                        f"⛔ **PERMANENT BAN LOKAL AKTIF:** **{display_name}** (`@{unique_id}`) diblokir oleh Mina (`{err_msg}`)."
                    )
    except Exception as e:
        TermUI.log("ERROR", f"Moderation API error: {e}")
        if channel and not silent:
            await channel.send(f"⚠️ Error koneksi saat `{mode.upper()}` untuk **{display_name}**: `{e}`")


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
    resolved_cookies = get_resolved_tiktok_cookies()
    idc_label = resolved_cookies.get("tt-target-idc", "alisg")
    euler_active = bool(os.getenv("EULERSTREAM_API_KEY", "").strip())
    ws_mode = "Authenticated WS" if should_authenticate_ws() else f"EulerSign WS ({'API Key' if euler_active else 'Public'})"
    is_live_conn = state["tiktok_connected"]
    return (
        f"**Status Moderator AI**\n"
        f"- TikTok Live: `{'Terhubung' if is_live_conn else 'Terputus'}` (`{ws_mode}`)\n"
        f"- LLM Cascade: `{get_active_llm_providers_label()}` (Terakhir dipakai: `{state['last_llm_used']}`)\n"
        f"- Kick/Mute/Ban API: `{f'Aktif (IDC: {idc_label})' if tiktok_session_id else 'Nonaktif'}`\n"
        f"- Musik Latar (VB-Cable): `{lagu_aktif}` | Vol: `{int(music_state['volume'] * 100)}%`\n"
        f"- Autoplay Musik: `{autoplay_str}` | Antrean: `{len(music_pending_items)} lagu`\n"
        f"- Live Dashboard: `{music_state['overlay_url']}` (SSE Real-Time)\n"
        f"- Suara AI: `{'MUTE' if state['muted'] else 'AKTIF'}`\n"
        f"- Topik Bahasan Utama: `{topik_live}`\n"
        f"- Konteks Live Terbaru: `{status_live}`\n"
        f"- Total Badword: `{badword_count}` | Learned QA: `{learned_count}` | Banned: `{banned_count}`\n"
        f"- Eskalasi Menunggu: `{aktif_esc}`"
    )


def build_warn_list_embed(title: str = "Daftar Pelanggaran Penonton (Warn List)") -> discord.Embed:
    embed = discord.Embed(
        title=title,
        color=discord.Color.red() if user_warnings else discord.Color.green(),
        timestamp=datetime.now(),
    )
    if not user_warnings:
        embed.description = "Belum ada penonton yang tercatat melanggar aturan room."
        return embed

    sorted_warns = sorted(user_warnings.items(), key=lambda item: item[1]["count"], reverse=True)
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
        timestamp=datetime.now(),
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
            placeholder="Pilih antrean lagu yang ingin dihapus (bisa lebih dari 1)...",
            min_values=1,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if interaction.channel_id != control_channel_id:
            await interaction.followup.send("Perintah hanya aktif di channel kontrol.", ephemeral=True)
            return
        removed_names = []
        for val in self.values:
            rem = remove_queued_song_by_uid(int(val))
            if rem:
                removed_names.append(f"`{rem.get('title')}`")

        if removed_names:
            await interaction.followup.send(f"🗑️ Dihapus dari antrean: {', '.join(removed_names)}")
        else:
            await interaction.followup.send("Lagu sudah diputar atau sudah dihapus.", ephemeral=True)


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
        super().__init__(timeout=None)
        self.numeric_uid = numeric_uid
        self.unique_id = unique_id
        self.display_name = display_name
        self.sec_uid = sec_uid

        safe_uid = (unique_id or "user")[:45]
        self.btn_skip.custom_id = f"vmod:skip:{numeric_uid}:{safe_uid}"
        self.btn_mute.custom_id = f"vmod:mute:{numeric_uid}:{safe_uid}"
        self.btn_kick.custom_id = f"vmod:kick:{numeric_uid}:{safe_uid}"
        self.btn_ban.custom_id = f"vmod:ban:{numeric_uid}:{safe_uid}"
        self.btn_reset_warn.custom_id = f"vmod:reset:{numeric_uid}:{safe_uid}"

        if not show_skip:
            self.remove_item(self.btn_skip)
        if not show_reset_warn:
            self.remove_item(self.btn_reset_warn)

    async def _trigger_mod(self, interaction: discord.Interaction, mode: str, label: str):
        await interaction.response.defer()
        if interaction.channel_id != control_channel_id:
            await interaction.followup.send("Tombol ini hanya aktif di channel kontrol.", ephemeral=True)
            return
        enqueue_action(
            PRIORITY_HOST,
            "MANUAL_MOD",
            (mode, self.numeric_uid, self.unique_id, self.display_name, self.sec_uid),
        )
        await interaction.followup.send(
            f"⚡ Eksekusi **{label}** dikirim untuk **{self.display_name}** (`@{self.unique_id}`)."
        )

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭️", row=0)
    async def btn_skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if interaction.channel_id != control_channel_id:
            return
        last_escalated_question["question"] = None
        button.disabled = True
        button.label = "Skipped"
        await interaction.message.edit(view=self)

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
        await interaction.response.defer()
        if interaction.channel_id != control_channel_id:
            return
        removed = user_warnings.pop(self.unique_id, None)
        button.disabled = True
        button.label = "Strike Direset"
        await interaction.message.edit(view=self)
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

    @discord.ui.button(
        label="Mute Suara AI",
        style=discord.ButtonStyle.secondary,
        emoji="🔇",
        row=0,
        custom_id="mina_cp_toggle_mute",
    )
    async def btn_toggle_mute(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if interaction.channel_id != control_channel_id:
            return
        state["muted"] = not state["muted"]
        self._sync_buttons()
        await interaction.message.edit(view=self)
        status_msg = "Moderator AI di-**MUTE** sementara." if state["muted"] else "Moderator AI kembali **AKTIF**."
        await interaction.followup.send(status_msg, ephemeral=True)

    @discord.ui.button(
        label="Status",
        style=discord.ButtonStyle.primary,
        emoji="📊",
        row=0,
        custom_id="mina_cp_status",
    )
    async def btn_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if interaction.channel_id != control_channel_id:
            return
        await interaction.followup.send(build_status_text())

    @discord.ui.button(
        label="Skip Lagu",
        style=discord.ButtonStyle.secondary,
        emoji="⏩",
        row=0,
        custom_id="mina_cp_skip_music",
    )
    async def btn_skip_music(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if interaction.channel_id != control_channel_id:
            return
        music_state["skip_flag"] = True
        await interaction.followup.send("⏩ Lagu saat ini dilewati.")

    @discord.ui.button(
        label="Autoplay: ON",
        style=discord.ButtonStyle.success,
        emoji="🔁",
        row=1,
        custom_id="mina_cp_toggle_autoplay",
    )
    async def btn_toggle_autoplay(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if interaction.channel_id != control_channel_id:
            return
        music_state["autoplay"] = not music_state["autoplay"]
        broadcast_queue_update()
        self._sync_buttons()
        await interaction.message.edit(view=self)
        msg = "🔁 **Autoplay Musik: AKTIF**." if music_state["autoplay"] else "⏹️ **Autoplay Musik: NONAKTIF**."
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(
        label="Warn List",
        style=discord.ButtonStyle.danger,
        emoji="⚠️",
        row=1,
        custom_id="mina_cp_warnlist",
    )
    async def btn_warnlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if interaction.channel_id != control_channel_id:
            return
        await interaction.followup.send(embed=build_warn_list_embed())

    @discord.ui.button(
        label="Ban List",
        style=discord.ButtonStyle.danger,
        emoji="⛔",
        row=1,
        custom_id="mina_cp_banlist",
    )
    async def btn_banlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if interaction.channel_id != control_channel_id:
            return
        await interaction.followup.send(embed=build_ban_list_embed())


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

    record_room_event_memory(f"{display_name}: {comment} -> Mod (STRIKE {strike_num}/3): {voice_msg}")
    asyncio.create_task(
        send_warn_list_to_discord(
            display_name,
            strike_num,
            target_info=(info["numeric_uid"], unique_id, display_name, info.get("sec_uid", "")),
        )
    )

    await speak_to_vbcable(voice_msg, ensure_audio_ready)

    if strike_num >= 3:
        await execute_tiktok_moderation(
            strike_3_action,
            info["numeric_uid"],
            unique_id,
            display_name,
            info.get("sec_uid", ""),
            reason=f"3x Strike ({reason_label})",
        )


async def send_startup_command_guide():
    channel = await get_control_channel()
    if not channel:
        return

    embed = discord.Embed(
        title="Control Panel AI Moderator Siap",
        description="Gunakan tombol di bawah atau kirim Voice Note / perintah teks untuk mengendalikan live:",
        color=discord.Color.blue(),
        timestamp=datetime.now(),
    )
    embed.add_field(
        name="Tema, Konteks & Suara Langsung",
        value=(
            "- `!topik <tema live>` : Ubah fokus tema utama AI.\n"
            "- `!konteks <status>` : Ubah situasi live saat ini & hapus memori sementara lama.\n"
            "- **Kirim Voice Note:** Ditranskrip & dibacakan AI ke live.\n"
            "- `!say <teks>` : Membacakan teks langsung ke live."
        ),
        inline=False,
    )
    embed.add_field(
        name="Pemutar Musik (Pre-Fetch) & Live Dashboard",
        value=(
            "- **Request Lagu:** `req <judul>`, `play <judul>`, `request <judul>`, `!play <judul>`, atau `m!p <judul>`.\n"
            "- **Kendali Komentar Live:** Ketik `skip` (skip lagu sendiri / bebas untuk host), `!delqueue` / `!hapuslagu` (hapus antrean sendiri).\n"
            "- `!queue` : Cek antrean & munculkan menu hapus lagu.\n"
            "- `!delqueue 2` / `!delqueue 2 3` : Hapus nomor antrean tertentu.\n"
            "- `!autoplay` / `!skipmusic` / `!stopmusic` / `!vol <1-100>`\n"
            f"- `!openoverlay` : Buka Dashboard Live (Musik, Top Like & Gift) di `{music_state['overlay_url']}`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Moderasi & Pengaturan",
        value=(
            "- `!target <nama>` / `!kick` / `!muteuser` / `!ban` / `!unban`\n"
            "- `!learn <tanya> | <jawab>` / `!forget <kata>` / `!addbadword <kata>`\n"
            "- `!reconnect` / `!clear` / `!status` / `!panel`"
        ),
        inline=False,
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
        timestamp=datetime.now(),
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
    global _last_all_cooldown_notice
    TermUI.log("AI-MOD", "AI Head Moderator aktif dan siap menjaga room.")
    while True:
        priority, _, event_type, payload = await action_queue.get()
        try:
            if state["muted"] and priority > PRIORITY_HOST:
                continue

            if event_type == "HOST_SPEAK":
                await speak_to_vbcable(payload[0], ensure_audio_ready)

            elif event_type == "MANUAL_MOD":
                mode, numeric_uid, unique_id, display_name, sec_uid = payload
                if mode == "kick":
                    announce = f"Maaf Kak {display_name}, kamu dikeluarkan dari live oleh moderator."
                elif mode == "mute":
                    announce = f"Kak {display_name} dibisukan sementara dari kolom komentar oleh moderator."
                else:
                    announce = f"Maaf Kak {display_name}, akun kamu diblokir permanen dari live ini oleh moderator."

                await speak_to_vbcable(announce, ensure_audio_ready)
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
                record_room_event_memory(f"Penonton {user_name} mengirim gift {count}x {gift_name}")
                TermUI.log("TIKTOK", f"Gift Shoutout -> {shoutout}", TermUI.YELLOW)
                await speak_to_vbcable(shoutout, ensure_audio_ready)

            elif event_type == "FOLLOW":
                user_name = payload[0]
                templates = [
                    f"Makasih sudah follow Kak {user_name}, selamat bergabung di live ya.",
                    f"Halo Kak {user_name}, terima kasih sudah follow akun ini!",
                    f"Mantap Kak {user_name} sudah follow, yuk ikutan ngobrol di kolom komentar.",
                ]
                shoutout = random.choice(templates)
                TermUI.log("TIKTOK", f"Follow -> {shoutout}")
                await speak_to_vbcable(shoutout, ensure_audio_ready)

            elif event_type == "SHARE":
                user_name = payload[0]
                templates = [
                    f"Terima kasih Kak {user_name} sudah bantu share live-nya!",
                    f"Makasih banyak Kak {user_name} udah bantu bagikan live ini ke teman-teman.",
                ]
                shoutout = random.choice(templates)
                TermUI.log("TIKTOK", f"Share -> {shoutout}")
                await speak_to_vbcable(shoutout, ensure_audio_ready)

            elif event_type == "LIKE":
                user_name = payload[0]
                templates = [
                    f"Makasih Kak {user_name} dan teman-teman yang udah bantu tap-tap layarnya!",
                    f"Terima kasih tap-tap layarnya Kak {user_name}, gas terus ramaikan room-nya!",
                ]
                shoutout = random.choice(templates)
                TermUI.log("TIKTOK", f"Like -> {shoutout}")
                await speak_to_vbcable(shoutout, ensure_audio_ready)

            elif event_type == "JOIN":
                user_name, returning = payload
                if returning:
                    templates = [
                        f"Halo Kak {user_name}, selamat datang kembali di live ya!",
                        f"Eh ada Kak {user_name} mampir lagi, selamat bergabung ya.",
                        f"Halo lagi Kak {user_name}, yuk ikutan ngobrol di kolom komentar.",
                    ]
                else:
                    templates = [
                        f"Halo Kak {user_name}, selamat datang di live, salam kenal ya!",
                        f"Selamat bergabung Kak {user_name}, yuk santai dan ikutan ngobrol di chat.",
                        f"Selamat datang di live Kak {user_name}, kalau mau request lagu tinggal ketik req judul lagu ya!",
                        f"Halo Kak {user_name}, selamat datang, bantu tap-tap layarnya juga ya!",
                    ]
                shoutout = random.choice(templates)
                TermUI.log("TIKTOK", f"Join -> {shoutout}")
                await speak_to_vbcable(shoutout, ensure_audio_ready)

            elif event_type == "COMMENT":
                token_id, numeric_uid, unique_id, user_name, comment, sec_uid = payload[:6]
                enqueued_at = payload[6] if len(payload) > 6 else time.time()

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

                if (latest_comment_token["id"] - token_id) >= 3 or (time.time() - enqueued_at) > 10.0:
                    TermUI.log("WARN", f"Chat basi dilewati: {user_name}: {comment}")
                    continue

                if filter_status == "META_QUESTION":
                    rule_reply = (
                        f"Nggak boleh ya Kak {user_name}, kata kasar otomatis terdeteksi filter room dan kalau dipakai bisa kena ban setelah tiga kali peringatan."
                    )
                    TermUI.log("AI-MOD", f"Aturan Room (0.00s) -> {user_name}: \"{comment}\"")
                    record_room_event_memory(f"{user_name}: {comment} -> Mod (ATURAN): {rule_reply}")
                    await speak_to_vbcable(rule_reply, ensure_audio_ready)
                    continue

                memory_reply = find_permanent_memory_answer(comment)
                if memory_reply:
                    TermUI.log("AI-MOD", f"Memory Hit (0.00s) -> \"{comment}\" => \"{memory_reply}\"", TermUI.CYAN)
                    record_room_event_memory(f"{user_name}: {comment} -> Mod (MEMORY): {memory_reply}")
                    await speak_to_vbcable(memory_reply, ensure_audio_ready)
                    continue

                if are_all_llms_on_cooldown():
                    now_cd = time.time()
                    if now_cd - _last_all_cooldown_notice >= 15.0:
                        _last_all_cooldown_notice = now_cd
                        TermUI.log("WARN", "Semua provider LLM sedang cooldown sementara, chat dilewati agar tidak menumpuk.")
                    continue

                t0 = time.time()
                decision = await asyncio.wait_for(
                    asyncio.to_thread(run_moderator_reasoning, unique_id, user_name, comment),
                    timeout=14.0,
                )
                llm_time = time.time() - t0

                action = decision.get("action", "IGNORE").upper()
                intent = decision.get("intent", "-")
                thought = decision.get("thought", "")
                reply = (decision.get("reply_text") or "").strip()

                TermUI.log("AI-MOD", f"⚡ {llm_time:.2f}s [{state['last_llm_used']}] | {action} ({intent}) | {thought}")

                if action == "IGNORE":
                    continue

                if action == "WARN":
                    await handle_user_strike(numeric_uid, unique_id, user_name, comment, None, sec_uid)
                    continue

                if action == "ESCALATE":
                    last_escalated_question["user"] = user_name
                    last_escalated_question["question"] = comment
                    record_room_event_memory(f"{user_name}: {comment} -> [ESKALASI HOST]")
                    TermUI.log("DISCORD", f"Eskalasi pertanyaan {user_name}: \"{comment}\"", TermUI.YELLOW)
                    await send_discord_escalation(numeric_uid, unique_id, user_name, comment, sec_uid)
                elif reply:
                    record_room_event_memory(f"{user_name}: {comment} -> Mod ({action}): {reply}")
                    TermUI.log("VOICE", f"Mina: \"{reply}\"", TermUI.WHITE)
                    await speak_to_vbcable(reply, ensure_audio_ready)

        except asyncio.TimeoutError:
            TermUI.log("WARN", "Respons LLM melewati batas waktu (14s), lanjut ke chat berikutnya.")
        except Exception as e:
            TermUI.log("WARN", f"Chat dilewati ({e})")
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


def extract_event_msg_id(event: CommentEvent) -> str | None:
    direct_id = getattr(event, "msg_id", None)
    if direct_id:
        return str(direct_id)
    for attr in ("base_message", "common"):
        obj = getattr(event, attr, None)
        if obj is not None:
            nested_id = getattr(obj, "msg_id", None)
            if nested_id:
                return str(nested_id)
    return None


def apply_euler_sign_key() -> bool:
    euler_key = os.getenv("EULERSTREAM_API_KEY", "").strip()
    if not euler_key:
        return False
    if WebDefaults is not None:
        WebDefaults.tiktok_sign_api_key = euler_key
    return True


def _speak_viewer_music_notice(unique_id: str, text: str, cooldown_sec: float = 8.0):
    now = time.time()
    key = unique_id.lower().strip()
    if now - _viewer_music_warn_cooldown.get(key, 0.0) < cooldown_sec:
        return
    _viewer_music_warn_cooldown[key] = now
    enqueue_action(PRIORITY_SOCIAL, "HOST_SPEAK", (text,))


async def _handle_host_live_comment_command(display_name: str, text: str) -> bool:
    clean_raw = re.sub(r"[\u200b-\u200d\ufeff]", "", text)
    clean_raw = re.sub(r"\s+", " ", clean_raw).strip()
    lower = clean_raw.lower()

    if lower.startswith(MUSIC_REQUEST_PREFIXES):
        parts = clean_raw.split(" ", 1)
        song_query = parts[1].strip() if len(parts) > 1 else ""
        if song_query:
            enqueue_song(f"Host ({display_name})", song_query, requester_id="__host__")
            TermUI.log("MUSIC", f"Host request via chat: {song_query}", TermUI.CYAN)
        return True

    if lower in ("skip", "!skip", "skipmusic", "!skipmusic", "nextsong", "!nextsong"):
        if music_state.get("current_item") is not None:
            music_state["skip_flag"] = True
            TermUI.log("MUSIC", f"Host ({display_name}) skip lagu lewat komentar.", TermUI.CYAN)
        return True

    if lower.startswith(("!delqueue", "delqueue", "!hapuslagu", "hapuslagu", "!hapusantrean")):
        parts = clean_raw.split(" ", 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if arg.lower() in ("all", "semua"):
            clear_all_queued_and_buffers()
            TermUI.log("MUSIC", f"Host ({display_name}) mengosongkan antrean musik.", TermUI.CYAN)
        elif arg:
            removed = remove_queued_songs(arg)
            if removed:
                TermUI.log("MUSIC", f"Host menghapus {len(removed)} antrean ({arg}).", TermUI.CYAN)
        return True

    if lower in ("!stopmusic", "stopmusic"):
        music_state["stop_flag"] = True
        music_state["skip_flag"] = True
        music_state["last_seed_query"] = None
        clear_all_queued_and_buffers()
        TermUI.log("MUSIC", f"Host ({display_name}) menghentikan pemutar musik.", TermUI.CYAN)
        return True

    if lower.startswith(("!autoplay", "autoplay")):
        parts = lower.split(" ", 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if arg in ("on", "aktif", "1"):
            music_state["autoplay"] = True
        elif arg in ("off", "mati", "0"):
            music_state["autoplay"] = False
        else:
            music_state["autoplay"] = not music_state["autoplay"]
        broadcast_queue_update()
        TermUI.log("MUSIC", f"Host set Autoplay -> {music_state['autoplay']}", TermUI.CYAN)
        return True

    if lower.startswith(("!vol ", "vol ")):
        parts = lower.split(" ", 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if arg.isdigit():
            vol_num = max(1, min(100, int(arg)))
            music_state["volume"] = vol_num / 100.0
            if music_state["channel"]:
                music_state["channel"].set_volume(music_state["volume"])
            TermUI.log("MUSIC", f"Host set Volume -> {vol_num}%", TermUI.CYAN)
        return True

    if lower.startswith("!topik "):
        arg = clean_raw.split(" ", 1)[1].strip()
        if arg:
            update_live_topic(arg)
        return True

    if lower.startswith("!konteks "):
        arg = clean_raw.split(" ", 1)[1].strip()
        if arg:
            update_live_context(arg)
        return True

    if lower.startswith("!say "):
        arg = clean_raw.split(" ", 1)[1].strip()
        if arg:
            update_live_context(arg)
            enqueue_action(PRIORITY_HOST, "HOST_SPEAK", (arg,))
        return True

    if lower == "!mute":
        state["muted"] = True
        TermUI.log("AI-MOD", "Suara AI di-MUTE oleh Host via komentar.", TermUI.YELLOW)
        return True

    if lower == "!unmute":
        state["muted"] = False
        TermUI.log("AI-MOD", "Suara AI di-UNMUTE oleh Host via komentar.", TermUI.GREEN)
        return True

    if lower.startswith(("!kick ", "!muteuser ", "!ban ")):
        cmd_part, target_arg = clean_raw.split(" ", 1)
        target = resolve_target_viewer(target_arg.strip())
        if target:
            mode = "mute" if cmd_part.lower() == "!muteuser" else cmd_part.lower().lstrip("!")
            enqueue_action(
                PRIORITY_HOST,
                "MANUAL_MOD",
                (mode, target["numeric_uid"], target["unique_id"], target["name"], target.get("sec_uid", "")),
            )
        return True

    if lower.startswith("!unban "):
        target_arg = clean_raw.split(" ", 1)[1].strip()
        if target_arg:
            remove_banned_user_record(target_arg)
        return True

    return False


async def _handle_live_ended_cleanup(source_reason: str):
    had_session = mark_live_session_ended()
    broadcast_queue_update()
    if had_session:
        TermUI.log(
            "TIKTOK",
            f"Sesi Live berakhir ({source_reason}) — Top Like & Top Gifter di-reset otomatis.",
            TermUI.YELLOW,
        )
        channel = await get_control_channel()
        if channel:
            await channel.send(
                f"🏁 **Sesi Live Berakhir** (`{source_reason}`). Papan **Top Like & Top Gifter** telah di-reset otomatis untuk sesi berikutnya."
            )


def create_tiktok_client() -> TikTokLiveClient:
    has_euler_key = apply_euler_sign_key()
    client = TikTokLiveClient(unique_id=tiktok_username)

    cookies = get_resolved_tiktok_cookies()
    idc_label = cookies.get("tt-target-idc", "alisg")

    if should_authenticate_ws():
        if hasattr(client.web, "set_session_id"):
            client.web.set_session_id(cookies["sessionid"])
        if hasattr(client.web, "cookies"):
            for c_key, c_val in cookies.items():
                client.web.cookies.set(c_key, c_val, domain=".tiktok.com")
        session_label = f"Authenticated WS + Mod API | IDC: {idc_label}"
    elif tiktok_session_id:
        sign_tag = "EulerStream API Key" if has_euler_key else "EulerStream Public"
        session_label = f"{sign_tag} WS + Authenticated Mod API | IDC: {idc_label}"
    else:
        session_label = "Public WS (Mod API Nonaktif)"

    @client.on(ConnectEvent)
    async def on_connect(event: ConnectEvent):
        global connected_at
        connected_at = time.time()
        seen_comments.clear()
        state["tiktok_connected"] = True
        state["reconnect_delay"] = 5
        tiktok_disconnected_event.clear()

        room_id = int(getattr(client, "room_id", 0) or 0)
        was_reset = sync_live_session_room(room_id)
        if was_reset:
            TermUI.log("TIKTOK", "Sesi live baru terdeteksi — Leaderboard Top Like & Gift dimulai dari 0.", TermUI.CYAN)

        ensure_audio_ready()
        ensure_worker_running()
        ensure_music_workers_running()
        broadcast_queue_update()
        TermUI.log(
            "TIKTOK",
            f"Terhubung ke Live @{event.unique_id} ({session_label} | filter 1.0s)",
            TermUI.GREEN,
        )

        channel = await get_control_channel()
        if channel:
            await channel.send(
                f"🟢 Terhubung ke TikTok Live **@{event.unique_id}** (`{session_label}`). AI Moderator siap bertugas."
            )

    @client.on(DisconnectEvent)
    async def on_disconnect(event: DisconnectEvent):
        global connected_at
        was_connected = state["tiktok_connected"]
        connected_at = None
        state["tiktok_connected"] = False
        seen_comments.clear()
        drain_non_host_actions()
        save_viewers_db()
        save_session_stats()
        tiktok_disconnected_event.set()
        broadcast_queue_update()
        TermUI.log("WARN", "Koneksi TikTok Live terputus (data Top Like & Gift tetap disimpan).")

        if was_connected:
            channel = await get_control_channel()
            if channel:
                embed = discord.Embed(
                    title="TikTok Live Terputus",
                    description=f"Sesi pemantauan untuk **@{tiktok_username}** terhenti. Auto-reconnect tetap siaga (data Top Like/Gift dipertahankan sampai End Live).",
                    color=discord.Color.dark_red(),
                    timestamp=datetime.now(),
                )
                await channel.send(embed=embed)

    if LiveEndEvent is not None:
        @client.on(LiveEndEvent)
        async def on_live_end(event):
            await _handle_live_ended_cleanup("LiveEndEvent")

    if ControlEvent is not None:
        @client.on(ControlEvent)
        async def on_control_event(event):
            action_code = getattr(event, "action", None)
            if action_code == 3 or str(action_code).upper() in ("STREAM_ENDED", "LIVE_END", "3"):
                await _handle_live_ended_cleanup("ControlEvent: Stream Ended")

    if JoinEvent is not None:
        @client.on(JoinEvent)
        async def on_join(event):
            try:
                now = time.time()
                if connected_at is None or (now - connected_at) < 2.5:
                    return

                user_obj = getattr(event, "user", None) or getattr(event, "user_info", None)
                if not user_obj:
                    return

                unique_id = get_user_unique_id(user_obj)
                numeric_uid = get_user_numeric_id(user_obj)
                if not unique_id or is_host_account(unique_id):
                    return

                if is_user_permanently_banned(unique_id, numeric_uid):
                    return

                returning = is_returning_viewer(unique_id)
                register_viewer(user_obj, is_chat=False)

                uid_key = unique_id.lower().strip()
                if now - welcomed_users_cooldown.get(uid_key, 0.0) < 600.0:
                    return

                if (now - state["last_join_shoutout"]) < 12.0 or action_queue.qsize() >= 3:
                    return

                welcomed_users_cooldown[uid_key] = now
                state["last_join_shoutout"] = now
                display_name = get_clean_nickname(user_obj)
                ensure_worker_running()
                enqueue_action(PRIORITY_SOCIAL, "JOIN", (display_name, returning))
            except Exception as e:
                TermUI.log("ERROR", f"Gagal memproses JoinEvent: {type(e).__name__}: {e}", TermUI.RED)

    @client.on(CommentEvent)
    async def on_comment(event: CommentEvent):
        global connected_at
        try:
            now = time.time()
            if connected_at is None:
                connected_at = now

            user_obj = getattr(event, "user", None) or getattr(event, "user_info", None)
            raw_text = str(getattr(event, "comment", "") or "")
            clean_text = re.sub(r"[\u200b-\u200d\ufeff]", "", raw_text)
            clean_text = re.sub(r"\s+", " ", clean_text).strip()
            if not clean_text:
                return

            msg_ts = extract_comment_timestamp(event)
            if msg_ts > 0 and msg_ts < (BOOT_TIME - 10.0):
                return

            if (now - connected_at) < 1.0:
                return

            unique_id = get_user_unique_id(user_obj)
            numeric_uid = get_user_numeric_id(user_obj)
            display_name = get_clean_nickname(user_obj)
            sec_uid = get_user_sec_uid(user_obj)

            raw_msg_id = extract_event_msg_id(event)
            dedup_key = f"id:{raw_msg_id}" if raw_msg_id else f"fb:{unique_id}:{clean_text}:{int(now * 2)}"
            if dedup_key in seen_comments:
                return
            seen_comments.add(dedup_key)
            if len(seen_comments) > 800:
                seen_comments.clear()

            lower_text = clean_text.lower()

            if is_host_account(unique_id, display_name):
                TermUI.log("HOST", f"{display_name} (@{unique_id}): {clean_text}", TermUI.YELLOW)
                record_room_event_memory(f"[HOST {display_name}]: {clean_text}")
                ensure_worker_running()
                await _handle_host_live_comment_command(display_name, clean_text)
                return

            if is_user_permanently_banned(unique_id, numeric_uid):
                TermUI.log("WARN", f"Auto-Enforce Ban: {display_name} (@{unique_id}) mencoba komentar, diabaikan & kick ulang.")
                asyncio.create_task(
                    execute_tiktok_moderation("kick", numeric_uid, unique_id, display_name, sec_uid, silent=True)
                )
                return

            register_viewer(user_obj, is_chat=True)
            record_user_chat_turn(unique_id, display_name, clean_text)
            ensure_worker_running()
            TermUI.log("CHAT", f"{display_name}: {clean_text}")

            if lower_text.startswith(MUSIC_REQUEST_PREFIXES):
                parts = clean_text.split(" ", 1)
                song_query = parts[1].strip() if len(parts) > 1 else ""
                if not song_query:
                    return

                f_status, matched_bw = check_badword_violation(song_query)
                if f_status == "VIOLATION":
                    await handle_user_strike(numeric_uid, unique_id, display_name, clean_text, matched_bw, sec_uid)
                    return

                if len(music_pending_items) >= 20:
                    TermUI.log("WARN", f"Request {display_name} ditolak: antrean penuh ({len(music_pending_items)}).")
                    return

                last_user_req = user_music_cooldown.get(unique_id, 0.0)
                if now - last_user_req < 5.0:
                    return
                user_music_cooldown[unique_id] = now

                TermUI.log("MUSIC", f"Request masuk dari {display_name}: {song_query}", TermUI.CYAN)
                enqueue_song(display_name, song_query, requester_id=unique_id.lower())
                pos_now = len(music_pending_items)
                if pos_now <= 1 and music_state["current"] is None:
                    ack_msg = f"Oke Kak {display_name}, lagu {song_query} langsung disiapkan ya."
                else:
                    ack_msg = f"Oke Kak {display_name}, lagu {song_query} masuk antrean nomor {pos_now} ya."
                enqueue_action(PRIORITY_SOCIAL, "HOST_SPEAK", (ack_msg,))
                return

            if lower_text in ("skip", "!skip", "skipmusic", "!skipmusic", "nextsong", "!nextsong"):
                allowed, reason_code = can_user_skip_current_song(unique_id.lower(), is_host=False)
                if allowed:
                    music_state["skip_flag"] = True
                    TermUI.log("MUSIC", f"{display_name} (@{unique_id}) skip lagu request miliknya.", TermUI.CYAN)
                    enqueue_action(
                        PRIORITY_SOCIAL,
                        "HOST_SPEAK",
                        (f"Oke Kak {display_name}, lagu request kamu aku skip ya.",),
                    )
                elif reason_code == "NOT_OWNER":
                    TermUI.log("MUSIC", f"Skip ditolak untuk {display_name} (bukan pemilik lagu).", TermUI.YELLOW)
                    _speak_viewer_music_notice(
                        unique_id,
                        f"Maaf Kak {display_name}, kamu cuma bisa skip lagu request kamu sendiri ya.",
                    )
                return

            del_queue_prefixes = ("!delqueue", "delqueue", "!hapuslagu", "hapuslagu", "!cancel", "!batal")
            first_word = lower_text.split(" ", 1)[0]
            if first_word in del_queue_prefixes:
                parts = clean_text.split(" ", 1)
                del_arg = parts[1].strip() if len(parts) > 1 else ""
                removed_items, del_status = remove_user_queued_songs(unique_id.lower(), del_arg)
                if del_status == "OK" and removed_items:
                    if len(removed_items) == 1:
                        t_name = removed_items[0].get("title") or removed_items[0].get("query") or "lagu"
                        ack = f"Oke Kak {display_name}, lagu {t_name} sudah dihapus dari antrean ya."
                    else:
                        ack = f"Oke Kak {display_name}, {len(removed_items)} lagu antrean kamu sudah dihapus ya."
                    TermUI.log("MUSIC", f"{display_name} menghapus antrean miliknya ({len(removed_items)} lagu).", TermUI.CYAN)
                    enqueue_action(PRIORITY_SOCIAL, "HOST_SPEAK", (ack,))
                elif del_status == "NOT_OWNER":
                    _speak_viewer_music_notice(
                        unique_id,
                        f"Maaf Kak {display_name}, kamu hanya bisa menghapus lagu antrean milikmu sendiri ya.",
                    )
                else:
                    _speak_viewer_music_notice(
                        unique_id,
                        f"Kamu belum punya lagu di daftar antrean ya Kak {display_name}.",
                    )
                return

            latest_comment_token["id"] += 1
            enqueue_action(
                PRIORITY_COMMENT,
                "COMMENT",
                (latest_comment_token["id"], numeric_uid, unique_id, display_name, clean_text, sec_uid, now),
            )
        except Exception as e:
            TermUI.log("ERROR", f"Gagal memproses CommentEvent: {type(e).__name__}: {e}", TermUI.RED)

    @client.on(GiftEvent)
    async def on_gift(event: GiftEvent):
        try:
            user_obj = getattr(event, "user", None) or getattr(event, "user_info", None)
            gift_obj = getattr(event, "gift", None)
            streakable = getattr(gift_obj, "streakable", False)
            repeat_end = getattr(event, "repeat_end", True)
            if streakable and not repeat_end:
                return

            display_name = get_clean_nickname(user_obj)
            gift_name = (
                getattr(gift_obj, "name", None)
                or getattr(getattr(gift_obj, "info", None), "name", None)
                or "Gift"
            )
            count = getattr(event, "repeat_count", 1) or 1

            record_viewer_gift(user_obj, gift_name, count)
            broadcast_queue_update()

            ensure_worker_running()
            TermUI.log("TIKTOK", f"Gift: {display_name} mengirim {count}x {gift_name}", TermUI.YELLOW)
            enqueue_action(PRIORITY_GIFT, "GIFT", (display_name, gift_name, count))

            channel = await get_control_channel()
            if channel:
                await channel.send(f"🎁 **Gift Masuk:** `{display_name}` mengirim **{count}x {gift_name}**")
        except Exception as e:
            TermUI.log("ERROR", f"Gagal memproses GiftEvent: {type(e).__name__}: {e}", TermUI.RED)

    @client.on(FollowEvent)
    async def on_follow(event: FollowEvent):
        try:
            user_obj = getattr(event, "user", None) or getattr(event, "user_info", None)
            unique_id = get_user_unique_id(user_obj)
            numeric_uid = get_user_numeric_id(user_obj)
            if is_user_permanently_banned(unique_id, numeric_uid):
                return
            register_viewer(user_obj, is_chat=False)
            if unique_id in followed_users or is_host_account(unique_id):
                return
            followed_users.add(unique_id)

            display_name = get_clean_nickname(user_obj)
            ensure_worker_running()
            enqueue_action(PRIORITY_SOCIAL, "FOLLOW", (display_name,))
        except Exception as e:
            TermUI.log("ERROR", f"Gagal memproses FollowEvent: {type(e).__name__}: {e}", TermUI.RED)

    @client.on(ShareEvent)
    async def on_share(event: ShareEvent):
        try:
            user_obj = getattr(event, "user", None) or getattr(event, "user_info", None)
            unique_id = get_user_unique_id(user_obj)
            numeric_uid = get_user_numeric_id(user_obj)
            if is_user_permanently_banned(unique_id, numeric_uid):
                return
            register_viewer(user_obj, is_chat=False)
            now = time.time()
            if is_host_account(unique_id) or now - shared_users_cooldown.get(unique_id, 0) < 60.0:
                return
            shared_users_cooldown[unique_id] = now

            display_name = get_clean_nickname(user_obj)
            ensure_worker_running()
            enqueue_action(PRIORITY_SOCIAL, "SHARE", (display_name,))
        except Exception as e:
            TermUI.log("ERROR", f"Gagal memproses ShareEvent: {type(e).__name__}: {e}", TermUI.RED)

    @client.on(LikeEvent)
    async def on_like(event: LikeEvent):
        try:
            user_obj = getattr(event, "user", None) or getattr(event, "user_info", None)
            unique_id = get_user_unique_id(user_obj)
            numeric_uid = get_user_numeric_id(user_obj)
            if is_user_permanently_banned(unique_id, numeric_uid):
                return

            like_delta = int(getattr(event, "count", 1) or 1)
            room_total = int(getattr(event, "total", 0) or 0)
            record_viewer_like(user_obj, like_delta, room_total)
            broadcast_queue_update()

            now = time.time()
            if (now - state["last_like_shoutout"]) < 25.0 or action_queue.qsize() >= 2:
                return
            state["last_like_shoutout"] = now

            display_name = get_clean_nickname(user_obj)
            ensure_worker_running()
            enqueue_action(PRIORITY_SOCIAL, "LIKE", (display_name,))
        except Exception as e:
            TermUI.log("ERROR", f"Gagal memproses LikeEvent: {type(e).__name__}: {e}", TermUI.RED)

    return client


tiktok_client = create_tiktok_client()


async def start_tiktok_background():
    global tiktok_client, connected_at
    while True:
        try:
            if not tiktok_username:
                TermUI.log("ERROR", "TIKTOK_USERNAME belum diisi di file .env")
                return

            tiktok_disconnected_event.clear()
            mode_tag = "Authenticated WS" if should_authenticate_ws() else "EulerSign WS + Auth Mod API"
            TermUI.log("TIKTOK", f"Menghubungkan ke TikTok Live (@{tiktok_username}) [{mode_tag}]...")
            start_res = await tiktok_client.start()

            for _ in range(30):
                if state["tiktok_connected"] or tiktok_disconnected_event.is_set():
                    break
                if isinstance(start_res, asyncio.Task) and start_res.done():
                    break
                await asyncio.sleep(0.5)

            if not state["tiktok_connected"] and not (isinstance(start_res, asyncio.Task) and not start_res.done()):
                raise ConnectionError("Tidak menerima konfirmasi ConnectEvent dari TikTok Live.")

            while not tiktok_disconnected_event.is_set():
                if isinstance(start_res, asyncio.Task) and start_res.done():
                    exc = start_res.exception() if not start_res.cancelled() else None
                    if exc:
                        raise exc
                    break
                await asyncio.sleep(2.0)

        except asyncio.CancelledError:
            break
        except Exception as e:
            err_str = str(e)
            err_lower = err_str.lower()
            err_type = type(e).__name__.lower()
            state["tiktok_connected"] = False
            connected_at = None
            delay = state.get("reconnect_delay", 5)

            is_stream_offline = any(
                k in err_lower or k in err_type
                for k in ("offline", "not live", "useroffline", "room not found", "stream ended")
            )
            if is_stream_offline and live_session_stats.get("is_live_active"):
                await _handle_live_ended_cleanup("Host Offline / End Live")

            if "11001" in err_str or "getaddrinfo" in err_str:
                TermUI.log("ERROR", f"DNS gagal resolve server TikTok (cek koneksi/DNS 1.1.1.1). Mencoba ulang dalam {delay}s...")
            elif "503" in err_str or "SIGN_NOT_200" in err_str:
                TermUI.log("ERROR", f"Sign API mengembalikan 503 (pastikan TIKTOK_WS_AUTH=auto). Ulang dalam {delay}s...")
            else:
                TermUI.log("ERROR", f"Koneksi TikTok Live terhenti: {e}. Mencoba ulang dalam {delay}s...")

            channel = await get_control_channel()
            if channel and delay == 5:
                await channel.send(f"⚠️ Koneksi TikTok Live terhenti: `{e}`\n🔄 Mencoba menyambung ulang otomatis...")

        try:
            await tiktok_client.disconnect()
        except Exception:
            pass

        tiktok_client = create_tiktok_client()
        delay = state.get("reconnect_delay", 5)
        await asyncio.sleep(delay)
        state["reconnect_delay"] = min(30, delay + 5)


def restart_tiktok_background():
    global tiktok_task, tiktok_client, connected_at
    state["reconnect_delay"] = 5
    state["tiktok_connected"] = False
    connected_at = None
    seen_comments.clear()
    drain_non_host_actions()
    tiktok_disconnected_event.set()
    if tiktok_task is not None and not tiktok_task.done():
        tiktok_task.cancel()
    tiktok_client = create_tiktok_client()
    tiktok_task = asyncio.create_task(start_tiktok_background())


class ModeratorDiscordBot(discord.Client):
    async def setup_hook(self):
        self.add_view(ControlPanelView())

    async def close(self):
        TermUI.log("SYSTEM", "Menutup terminal & menyimpan database viewer & statistik sesi...")
        save_viewers_db()
        save_session_stats()
        try:
            if tiktok_task is not None and not tiktok_task.done():
                tiktok_task.cancel()
        except Exception:
            pass

        try:
            channel = await get_control_channel()
            if channel:
                embed = discord.Embed(
                    title="Terminal / Sistem AI Moderator Ditutup",
                    description="Program dihentikan oleh host (`Ctrl + C` / Terminal ditutup).",
                    color=discord.Color.dark_grey(),
                    timestamp=datetime.now(),
                )
                await channel.send(embed=embed)
        except Exception as e:
            TermUI.log("ERROR", f"Shutdown notify error: {e}")

        stop_overlay_server()
        try:
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
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return
    await asyncio.sleep(0.05)
    if interaction.response.is_done():
        return

    custom_id = (interaction.data or {}).get("custom_id", "")
    if not custom_id.startswith("vmod:"):
        return

    await interaction.response.defer()
    if interaction.channel_id != control_channel_id:
        return

    parts = custom_id.split(":", 3)
    if len(parts) < 4:
        return

    _, action, num_uid_str, unique_id = parts
    numeric_uid = int(num_uid_str) if num_uid_str.isdigit() else 0
    target = resolve_target_viewer(unique_id)
    display_name = target["name"] if target else unique_id
    sec_uid = target.get("sec_uid", "") if target else ""
    if not numeric_uid and target:
        numeric_uid = target["numeric_uid"]

    if action == "skip":
        last_escalated_question["question"] = None
        await interaction.followup.send("⏭️ Eskalasi dilewati.", ephemeral=True)
    elif action == "reset":
        removed = user_warnings.pop(unique_id, None)
        msg = (
            f"🧹 Catatan peringatan untuk **{display_name}** (`@{unique_id}`) telah dihapus."
            if removed else f"Tidak ada catatan strike aktif untuk `@{unique_id}`."
        )
        await interaction.followup.send(msg)
    elif action in ("mute", "kick", "ban"):
        label_map = {"mute": "MUTE 5 MENIT", "kick": "KICK", "ban": "PERMANENT BAN"}
        enqueue_action(
            PRIORITY_HOST,
            "MANUAL_MOD",
            (action, numeric_uid, unique_id, display_name, sec_uid),
        )
        await interaction.followup.send(
            f"⚡ Eksekusi **{label_map[action]}** dikirim untuk **{display_name}** (`@{unique_id}`)."
        )


@discord_client.event
async def on_ready():
    await start_overlay_services()
    broadcast_queue_update()
    TermUI.log("DISCORD", f"Login sebagai {discord_client.user} | Channel ID: {control_channel_id}", TermUI.GREEN)
    if not state["tiktok_started"]:
        state["tiktok_started"] = True
        ensure_music_workers_running()
        await send_startup_command_guide()
        restart_tiktok_background()


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
        None,
    )

    if audio_attachment:
        await message.add_reaction("🎙️")
        try:
            audio_bytes = await audio_attachment.read()
            transcribed = await asyncio.to_thread(
                transcribe_voice_note,
                audio_bytes,
                audio_attachment.content_type or "audio/ogg",
            )
            if not transcribed or "[TIDAK_JELAS]" in transcribed:
                await message.reply("Suara Voice Note kurang jelas, coba ulangi lagi.")
                return

            if last_escalated_question["question"]:
                q_part = str(last_escalated_question["question"])
                qa_type = await asyncio.to_thread(save_learned_qa, q_part, transcribed, "discord_voice_note")
                record_room_event_memory(f"Host menjawab '{q_part}': {transcribed}")
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
            enqueue_song("Host", arg, requester_id="__host__")
            await message.reply(f"🎵 Ditambahkan ke antrean & langsung di-download di latar belakang: **{arg}**")

        elif cmd in ("!delqueue", "!hapuslagu", "!remqueue", "!hapusantrean"):
            if not arg:
                await message.reply(
                    "Gunakan format: `!delqueue 2`, `!delqueue 2 3`, atau `!delqueue <judul lagu>`"
                )
                return
            removed = remove_queued_songs(arg)
            if removed:
                titles_str = "\n".join(f"- `{r.get('title')}` (Req: {r.get('requester')})" for r in removed)
                await message.reply(f"🗑️ **{len(removed)} lagu** dihapus dari antrean:\n{titles_str}")
            else:
                await message.reply(f"Antrean `{arg}` tidak ditemukan.")

        elif cmd == "!autoplay":
            if arg.lower() in ("on", "aktif", "1"):
                music_state["autoplay"] = True
            elif arg.lower() in ("off", "mati", "0"):
                music_state["autoplay"] = False
            else:
                music_state["autoplay"] = not music_state["autoplay"]

            broadcast_queue_update()
            status_ap = "**AKTIF (ON)**" if music_state["autoplay"] else "**NONAKTIF (OFF)**"
            await message.reply(f"🔁 Autoplay musik sekarang {status_ap}.")

        elif cmd in ("!skipmusic", "!nextsong"):
            music_state["skip_flag"] = True
            await message.reply("⏩ Melewati lagu yang sedang diputar...")

        elif cmd == "!stopmusic":
            music_state["stop_flag"] = True
            music_state["skip_flag"] = True
            music_state["last_seed_query"] = None
            clear_all_queued_and_buffers()
            await message.reply("⏹️ Pemutar musik dihentikan & seluruh antrean dikosongkan.")

        elif cmd == "!vol" and arg.isdigit():
            vol_num = max(1, min(100, int(arg)))
            music_state["volume"] = vol_num / 100.0
            if music_state["channel"]:
                music_state["channel"].set_volume(music_state["volume"])
            await message.reply(f"🔊 Volume musik latar diatur ke **{vol_num}%**.")

        elif cmd == "!queue":
            snap = get_live_dashboard_snapshot()
            np = snap["now_playing"]
            curr = (
                f"{np['title']} (Req: **{np['requester']}**) `[{np['elapsed_str']}/{np['duration_str']}]`"
                if np else "Tidak ada lagu yang sedang diputar"
            )
            if music_pending_items:
                q_lines = "\n".join(
                    f"**#{idx}.** {item.get('title') or item.get('query')} — Req: **{item.get('requester')}** "
                    f"`[{'SIAP' if item.get('status') == 'ready' else 'LOADING'}]`"
                    for idx, item in enumerate(music_pending_items[:15], start=1)
                )
            else:
                q_lines = "Antrean kosong"
            view = QueueManageView(music_pending_items) if music_pending_items else None
            await message.reply(
                f"**Sedang Diputar:** {curr}\n"
                f"**Live Dashboard:** `{music_state['overlay_url']}`\n"
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
                    f"🪟 Dashboard Live dibuka lewat **{browser_used}** (Music Queue, Top Like & Gift).\n"
                    f"Jangan di-minimize jika ditangkap lewat *Window Capture* di TikTok LIVE Studio."
                )
            else:
                await message.reply(f"**Live Dashboard URL:** `{url}` *(tambah `/?chroma=1` untuk green screen)*")

        elif cmd == "!clear":
            TermUI.banner(state["audio_device"], music_state["overlay_url"], get_active_llm_providers_label())
            await message.reply("🧹 Layar terminal dibersihkan.")

        elif cmd == "!panel":
            await send_startup_command_guide()

        elif cmd == "!reconnect":
            try:
                await tiktok_client.disconnect()
            except Exception:
                pass
            restart_tiktok_background()
            await message.reply("🔄 Memulai ulang koneksi ke server TikTok Live (data Top Like & Gift tetap disimpan)...")

        elif cmd == "!topik":
            if not arg:
                curr_topic = load_dataset_dict().get("konteks_live", {}).get("topik_bahasan", "Belum diatur")
                await message.reply(f"Tema utama saat ini: **{curr_topic}**")
                return
            update_live_topic(arg)
            await message.reply(f"Tema utama live diubah menjadi:\n> **{arg}**")

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
                color=discord.Color.gold(),
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
                await message.reply(f"Penonton `{arg}` belum tercatat di sesi live ini.")
                return
            mode = "mute" if cmd == "!muteuser" else cmd.lstrip("!")
            await message.add_reaction("⚡")
            enqueue_action(
                PRIORITY_HOST,
                "MANUAL_MOD",
                (mode, target["numeric_uid"], target["unique_id"], target["name"], target.get("sec_uid", "")),
            )
            await message.reply(f"Memproses `{mode.upper()}` untuk **{target['name']}** (`@{target['unique_id']}`)...")

        elif cmd == "!unban":
            if not arg:
                await message.reply("Gunakan format: `!unban <nama tampilan atau @username>`")
                return
            removed = remove_banned_user_record(arg)
            if removed:
                await message.reply(f"Akun **{removed}** dihapus dari daftar `banned_users` lokal.")
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
            await message.reply(f"Status `konteks_live` diperbarui:\n> {arg}")

        elif cmd == "!learn" and "|" in arg:
            q_part, a_part = [x.strip() for x in arg.split("|", 1)]
            qa_type = await asyncio.to_thread(save_learned_qa, q_part, a_part, "discord_command", "permanen")
            await message.reply(f"Memori **{qa_type}** tersimpan:\n**Tanya:** {q_part}\n**Jawab:** {a_part}")

        elif cmd == "!forget" and arg:
            removed = forget_learned_qa(arg)
            await message.reply(f"Dihapus `{removed}` memori dengan kata kunci `{arg}`.")

        elif cmd == "!addbadword" and arg:
            if add_badword_to_dataset(arg):
                await message.reply(f"Kata `{normalize_text(arg)}` ditambahkan ke `daftar_badword`.")
            else:
                await message.reply(f"Kata `{arg}` sudah ada atau tidak valid.")

        elif cmd == "!delbadword" and arg:
            if remove_badword_from_dataset(arg):
                await message.reply(f"Kata `{normalize_text(arg)}` dihapus dari `daftar_badword`.")
            else:
                await message.reply(f"Kata `{arg}` tidak ditemukan.")

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
        q_part = str(last_escalated_question["question"])
        qa_type = await asyncio.to_thread(save_learned_qa, q_part, content, "discord_text_reply")
        record_room_event_memory(f"Host menjawab '{q_part}': {content}")
        last_escalated_question["question"] = None
        await message.reply(f"Dibacakan & disimpan sebagai memori **{qa_type}**.")
        enqueue_action(PRIORITY_HOST, "HOST_SPEAK", (content,))


if __name__ == "__main__":
    TermUI.clear_screen("MINA AI MODERATOR — Starting...")
    cleanup_stale_temp_files()
    cable_name = init_vbcable()
    TermUI.banner(cable_name, f"http://127.0.0.1:{OVERLAY_PORT}", get_active_llm_providers_label())
    if not discord_token:
        raise RuntimeError("DISCORD_BOT_TOKEN belum diisi di file .env")
    try:
        discord_client.run(discord_token, log_handler=None)
    except KeyboardInterrupt:
        TermUI.log("SYSTEM", "Program dihentikan oleh pengguna.")