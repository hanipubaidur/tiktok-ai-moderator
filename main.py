import time
import random
import asyncio
from datetime import datetime
import httpx
import discord
import pygame
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

from config import (
    BOOT_TIME,
    OVERLAY_PORT,
    LIVE_QUEUE_TXT_FILE,
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
    user_music_cooldown,
    user_warnings,
    last_escalated_question,
    music_pending_items,
    music_state,
    state,
    TermUI,
    get_active_llm_providers_label,
    is_host_account,
)
from storage import (
    load_dataset_dict,
    save_viewers_db,
    get_clean_nickname,
    get_user_unique_id,
    get_user_numeric_id,
    get_user_sec_uid,
    register_viewer,
    record_user_chat_turn,
    record_room_event_memory,
    record_viewer_like,
    record_viewer_gift,
    reset_session_stats,
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
    remove_queued_song_by_uid,
    clear_all_queued_and_buffers,
    ensure_music_workers_running,
    enqueue_song,
    set_music_discord_notifier,
)

tiktok_disconnected_event = asyncio.Event()
worker_task = None
tiktok_task = None
connected_at = None


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
                f"⚠️ Eksekusi API otomatis dilewati karena `TIKTOK_SESSION_ID` belum terpasang di `.env`."
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
                            f"🔇 **MUTE BERHASIL:** **{display_name}** (`@{unique_id}`) dibisukan 5 menit."
                        )
                    return
                err_msg = res_json.get("data", {}).get("prompts") or res_json.get("status_msg") or f"HTTP {resp.status_code}"
                if channel and not silent:
                    await channel.send(f"⚠️ Server TikTok menolak `MUTE` untuk **{display_name}** (`{err_msg}`).")

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
                            f"👢 **KICK BERHASIL:** **{display_name}** (`@{unique_id}`) dikeluarkan dari live."
                        )
                    return
                err_msg = res_json.get("data", {}).get("prompts") or res_json.get("status_msg") or f"HTTP {resp.status_code}"
                if channel and not silent:
                    await channel.send(f"⚠️ Server TikTok menolak `KICK` untuk **{display_name}** (`{err_msg}`).")

            elif mode == "ban":
                block_payload = {"target_uid": numeric_uid, "op": 1}
                if sec_uid:
                    block_payload["sec_target_uid"] = sec_uid

                block_resp = await client.post(block_url, params={"aid": 1988, **block_payload}, data=block_payload)
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
                    detail = "Blokir Permanen & Kick" if (block_ok and kick_ok) else ("Blokir Permanen" if block_ok else "Kick + Ban Lokal")
                    TermUI.log("AI-MOD", f"BAN berhasil: {display_name} (@{unique_id}) -> {detail}", TermUI.RED)
                    if channel and not silent:
                        await channel.send(
                            f"⛔ **PERMANENT BAN BERHASIL:** **{display_name}** (`@{unique_id}`) (`{detail}`)."
                        )
                    return

                err_msg = kick_json.get("data", {}).get("prompts") or block_json.get("status_msg") or f"HTTP {kick_resp.status_code}"
                if channel and not silent:
                    await channel.send(
                        f"⛔ **PERMANENT BAN LOKAL:** **{display_name}** (`@{unique_id}`) (`{err_msg}`)."
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
    idc_label = tiktok_target_idc or "alisg"
    is_live_conn = state["tiktok_connected"]
    return (
        f"**Status Moderator AI**\n"
        f"- TikTok Live: `{'Terhubung' if is_live_conn else 'Terputus'}`\n"
        f"- LLM Cascade: `{get_active_llm_providers_label()}` (Terakhir dipakai: `{state['last_llm_used']}`)\n"
        f"- Sesi Kick/Mute/Ban API: `{f'Aktif (IDC: {idc_label})' if tiktok_session_id else 'Mode Alert Manual'}`\n"
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
        if interaction.channel_id != control_channel_id:
            return
        removed_names = []
        for val in self.values:
            rem = remove_queued_song_by_uid(int(val))
            if rem:
                removed_names.append(f"`{rem.get('title')}`")

        if removed_names:
            await interaction.response.send_message(f"🗑️ Dihapus dari antrean: {', '.join(removed_names)}")
        else:
            await interaction.response.send_message("Lagu sudah diputar atau sudah dihapus.", ephemeral=True)


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
        msg = "🔁 **Autoplay Musik: AKTIF**." if music_state["autoplay"] else "⏹️ **Autoplay Musik: NONAKTIF**."
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
            "- **Request Lagu:** `!play <judul>`, `req <judul>`, atau `m!p <judul>`.\n"
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
                    f"Welcome Kak {user_name}, terima kasih sudah follow akun ini!",
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

                if (latest_comment_token["id"] - token_id) >= 8 or (time.time() - enqueued_at) > 30.0:
                    TermUI.log("WARN", f"Chat dilewati karena antrean padat: {user_name}: {comment}")
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

                t0 = time.time()
                decision = await asyncio.wait_for(
                    asyncio.to_thread(run_moderator_reasoning, unique_id, user_name, comment),
                    timeout=20.0,
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
            TermUI.log("WARN", "Respons LLM melewati batas waktu (20s), lanjut ke chat berikutnya.")
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


def create_tiktok_client() -> TikTokLiveClient:
    client = TikTokLiveClient(unique_id=tiktok_username)

    @client.on(ConnectEvent)
    async def on_connect(event: ConnectEvent):
        global connected_at
        connected_at = time.time()
        seen_comments.clear()
        state["tiktok_connected"] = True
        state["reconnect_delay"] = 5
        tiktok_disconnected_event.clear()
        ensure_audio_ready()
        ensure_worker_running()
        ensure_music_workers_running()
        broadcast_queue_update()
        TermUI.log("TIKTOK", f"Terhubung ke Live @{event.unique_id} (filter riwayat chat aktif 1.0s)", TermUI.GREEN)

        channel = await get_control_channel()
        if channel:
            await channel.send(f"🟢 Terhubung ke TikTok Live **@{event.unique_id}**. AI Moderator siap bertugas.")

    @client.on(DisconnectEvent)
    async def on_disconnect(event: DisconnectEvent):
        global connected_at
        was_connected = state["tiktok_connected"]
        connected_at = None
        state["tiktok_connected"] = False
        seen_comments.clear()
        drain_non_host_actions()
        save_viewers_db()
        tiktok_disconnected_event.set()
        broadcast_queue_update()
        TermUI.log("WARN", "Koneksi TikTok Live berakhir / terputus.")

        if was_connected:
            channel = await get_control_channel()
            if channel:
                embed = discord.Embed(
                    title="TikTok Live Berakhir / Terputus",
                    description=f"Sesi pemantauan untuk **@{tiktok_username}** terhenti. Auto-reconnect tetap siaga.",
                    color=discord.Color.dark_red(),
                    timestamp=datetime.now(),
                )
                await channel.send(embed=embed)

    @client.on(CommentEvent)
    async def on_comment(event: CommentEvent):
        global connected_at
        now = time.time()
        if connected_at is None:
            connected_at = now

        user_obj = getattr(event, "user", None) or getattr(event, "user_info", None)
        text = str(getattr(event, "comment", "") or "").strip()
        if not text:
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
        dedup_key = f"id:{raw_msg_id}" if raw_msg_id else f"fb:{unique_id}:{text}:{int(now * 2)}"
        if dedup_key in seen_comments:
            return
        seen_comments.add(dedup_key)
        if len(seen_comments) > 800:
            seen_comments.clear()

        lower_text = text.lower()

        if is_host_account(unique_id, display_name):
            TermUI.log("HOST", f"{display_name} (@{unique_id}): {text}", TermUI.YELLOW)
            record_room_event_memory(f"[HOST {display_name}]: {text}")
            if lower_text.startswith(("!play ", "req ", "m!p ")):
                song_query = text.split(" ", 1)[1].strip()
                if song_query:
                    enqueue_song(f"Host ({display_name})", song_query)
            return

        register_viewer(user_obj, is_chat=True)
        record_user_chat_turn(unique_id, display_name, text)
        ensure_worker_running()
        TermUI.log("CHAT", f"{display_name}: {text}")

        if is_user_permanently_banned(unique_id, numeric_uid):
            TermUI.log("WARN", f"Auto-Enforce Ban: {display_name} (@{unique_id}) mencoba komentar, kick ulang.")
            asyncio.create_task(
                execute_tiktok_moderation("kick", numeric_uid, unique_id, display_name, sec_uid, silent=True)
            )
            return

        if lower_text.startswith(("!play ", "req ", "m!p ")):
            song_query = text.split(" ", 1)[1].strip()
            if not song_query:
                return

            f_status, matched_bw = check_badword_violation(song_query)
            if f_status == "VIOLATION":
                await handle_user_strike(numeric_uid, unique_id, display_name, text, matched_bw, sec_uid)
                return

            if len(music_pending_items) >= 20:
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
                ack_msg = f"Oke Kak {display_name}, lagu {song_query} langsung disiapkan ya."
            else:
                ack_msg = f"Oke Kak {display_name}, lagu {song_query} masuk antrean nomor {pos_now} dan langsung di-download di belakang layar ya."
            enqueue_action(PRIORITY_SOCIAL, "HOST_SPEAK", (ack_msg,))
            return

        latest_comment_token["id"] += 1
        enqueue_action(
            PRIORITY_COMMENT,
            "COMMENT",
            (latest_comment_token["id"], numeric_uid, unique_id, display_name, text, sec_uid, now),
        )

    @client.on(GiftEvent)
    async def on_gift(event: GiftEvent):
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

    @client.on(FollowEvent)
    async def on_follow(event: FollowEvent):
        user_obj = getattr(event, "user", None) or getattr(event, "user_info", None)
        register_viewer(user_obj, is_chat=False)
        unique_id = get_user_unique_id(user_obj)
        if unique_id in followed_users or is_host_account(unique_id):
            return
        followed_users.add(unique_id)

        display_name = get_clean_nickname(user_obj)
        ensure_worker_running()
        enqueue_action(PRIORITY_SOCIAL, "FOLLOW", (display_name,))

    @client.on(ShareEvent)
    async def on_share(event: ShareEvent):
        user_obj = getattr(event, "user", None) or getattr(event, "user_info", None)
        register_viewer(user_obj, is_chat=False)
        now = time.time()
        unique_id = get_user_unique_id(user_obj)
        if is_host_account(unique_id) or now - shared_users_cooldown.get(unique_id, 0) < 60.0:
            return
        shared_users_cooldown[unique_id] = now

        display_name = get_clean_nickname(user_obj)
        ensure_worker_running()
        enqueue_action(PRIORITY_SOCIAL, "SHARE", (display_name,))

    @client.on(LikeEvent)
    async def on_like(event: LikeEvent):
        user_obj = getattr(event, "user", None) or getattr(event, "user_info", None)
        like_delta = int(getattr(event, "count", 1) or 1)
        room_total = int(getattr(event, "total", 0) or 0)
        record_viewer_like(user_obj, like_delta, room_total)
        broadcast_queue_update()

        now = time.time()
        if now - state["last_like_shoutout"] < 25.0:
            return
        state["last_like_shoutout"] = now

        display_name = get_clean_nickname(user_obj)
        ensure_worker_running()
        enqueue_action(PRIORITY_SOCIAL, "LIKE", (display_name,))

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
            TermUI.log("TIKTOK", f"Menghubungkan ke server TikTok Live (@{tiktok_username})...")
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
            state["tiktok_connected"] = False
            connected_at = None
            delay = state.get("reconnect_delay", 5)
            if "11001" in err_str or "getaddrinfo" in err_str:
                TermUI.log("ERROR", f"DNS gagal resolve server TikTok (cek koneksi/DNS 1.1.1.1). Mencoba ulang dalam {delay}s...")
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
    async def close(self):
        TermUI.log("SYSTEM", "Menutup terminal & menyimpan database viewer...")
        save_viewers_db()
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
async def on_ready():
    await start_overlay_services()
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
            enqueue_song("Host", arg)
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
            reset_session_stats()
            restart_tiktok_background()
            await message.reply("🔄 Memulai ulang koneksi ke server TikTok Live...")

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