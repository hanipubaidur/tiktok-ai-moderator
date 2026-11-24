import os
import io
import re
import json
import time
import random
import asyncio
from datetime import datetime
import httpx
import edge_tts
import pygame
from gtts import gTTS
from google import genai
from google.genai import types
from openai import OpenAI

from config import (
    NEURAL_VOICE,
    gemini_key_list,
    betabotz_key,
    openrouter_key,
    openrouter_custom_model,
    tiktok_username,
    state,
    room_chat_history,
    user_warnings,
    TermUI,
    is_llm_on_cooldown,
    set_llm_cooldown,
)
from storage import (
    load_dataset_dict,
    write_dataset_dict,
    normalize_text,
    get_viewer_memory_summary,
    get_user_recent_thread,
    add_viewer_memory_note,
)

GEMINI_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
]

_openrouter_free_cache: dict = {
    "models": [],
    "fetched_at": 0.0,
}


def _apply_gemini_cooldown(cd_key: str, err: Exception, label: str):
    err_low = str(err).lower()
    if "404" in err_low or "not_found" in err_low:
        cd_sec = 3600.0
    else:
        cd_sec = 10.0
    set_llm_cooldown(cd_key, err, label, override_seconds=cd_sec)


def _build_gemini_clients() -> list[genai.Client]:
    return [
        genai.Client(
            api_key=k,
            http_options=types.HttpOptions(timeout=10000),
        )
        for k in gemini_key_list
    ]


gemini_clients = _build_gemini_clients()

openrouter_client = (
    OpenAI(
        api_key=openrouter_key,
        base_url="https://openrouter.ai/api/v1",
        timeout=8.0,
        max_retries=0,
        default_headers={
            "HTTP-Referer": "https://localhost",
            "X-Title": "Mina AI Moderator",
        },
    )
    if openrouter_key
    else None
)


def are_all_llms_on_cooldown() -> bool:
    gemini_ready = any(
        not is_llm_on_cooldown(f"gemini:k{k_idx}:{m}")
        for m in GEMINI_MODELS
        for k_idx in range(1, len(gemini_clients) + 1)
    )
    if gemini_ready:
        return False
    if betabotz_key and not is_llm_on_cooldown("betabotz"):
        return False
    if openrouter_client and not is_llm_on_cooldown("openrouter"):
        return False
    return True


def _strip_reasoning_tags(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _sanitize_plain_reply(text: str, max_sentences: int = 2) -> str:
    cleaned = _strip_reasoning_tags(text)
    cleaned = re.sub(r"[*`#_~>]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", cleaned) if p.strip()]
    shortened = " ".join(parts[:max_sentences]).strip()
    if len(shortened) > 220:
        shortened = shortened[:217].rsplit(" ", 1)[0] + "."
    return shortened


def _is_provider_error_text(text: str) -> bool:
    if not text:
        return True
    low = text.lower()
    error_markers = (
        "unlimitedai.chat",
        "layanan ai saat ini dibatasi",
        "pembatasan kuota",
        "silakan coba lagi nanti",
        "beban tinggi atau pembatasan",
        "rate limit exceeded",
        "too many requests",
        "service unavailable",
        "error code:",
        "apikey",
        "invalid api key",
    )
    return any(m in low for m in error_markers)


def _is_valid_song_candidate(text: str) -> bool:
    if not text or _is_provider_error_text(text):
        return False
    low = text.lower()
    blocked_phrases = (
        "halo!",
        "halo,",
        "saya adalah",
        "membantu anda",
        "sebagai ai",
        "rekomendasi lagu",
        "berikut adalah",
        "tentu,",
        "maaf,",
    )
    if any(b in low for b in blocked_phrases):
        return False
    if len(text) < 4 or len(text) > 75 or len(text.split()) > 10:
        return False
    return True


def _get_live_openrouter_free_models() -> list[str]:
    now = time.time()
    if _openrouter_free_cache["models"] and (now - _openrouter_free_cache["fetched_at"]) < 1800:
        return _openrouter_free_cache["models"]

    discovered = []
    try:
        with httpx.Client(timeout=5.0) as http:
            resp = http.get("https://openrouter.ai/api/v1/models")
            if resp.status_code == 200:
                items = resp.json().get("data", [])
                for item in items:
                    mid = str(item.get("id") or "")
                    pricing = item.get("pricing") or {}
                    is_zero_price = (
                        str(pricing.get("prompt", "1")) == "0"
                        and str(pricing.get("completion", "1")) == "0"
                    )
                    if (mid.endswith(":free") or is_zero_price) and mid != "openrouter/free":
                        discovered.append(mid)
    except Exception:
        pass

    preferred_keywords = ("gemini", "llama", "qwen", "deepseek", "gemma", "mistral")
    discovered.sort(
        key=lambda m: next((i for i, kw in enumerate(preferred_keywords) if kw in m.lower()), 99)
    )
    final_list = discovered[:5] + ["openrouter/free"]
    _openrouter_free_cache["models"] = final_list
    _openrouter_free_cache["fetched_at"] = now
    return final_list


def _call_openrouter(
    models: list[str],
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.65,
    max_tokens: int = 800,
) -> str:
    if not openrouter_client or is_llm_on_cooldown("openrouter"):
        raise RuntimeError("OpenRouter tidak aktif atau sedang cooldown")

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    effective_max_tokens = max(max_tokens, 800)

    for model_name in models:
        cd_key = f"openrouter:{model_name}"
        if is_llm_on_cooldown(cd_key):
            continue
        try:
            res = openrouter_client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=temperature,
                max_tokens=effective_max_tokens,
                timeout=8.0,
            )

            choices = getattr(res, "choices", None)
            if not choices:
                raise ValueError(f"{model_name} choices kosong")

            msg_obj = choices[0].message
            raw_content = _strip_reasoning_tags(getattr(msg_obj, "content", None) or "")

            if not raw_content:
                reasoning_alt = (
                    getattr(msg_obj, "reasoning", None)
                    or getattr(msg_obj, "reasoning_content", None)
                    or ""
                )
                if reasoning_alt:
                    m_json = re.search(r"\{[^{}]*\"action\"[^{}]*\}", reasoning_alt, flags=re.DOTALL)
                    if m_json:
                        raw_content = m_json.group(0)

            if not raw_content or _is_provider_error_text(raw_content):
                raise ValueError(f"{model_name} konten kosong/error")

            state["last_llm_used"] = f"OpenRouter ({model_name})"
            return raw_content
        except Exception as e:
            err_l = str(e).lower()
            if "free-models-per-day" in err_l or "daily limit" in err_l:
                set_llm_cooldown("openrouter", e, "OpenRouter (Daily Limit)", override_seconds=3600.0)
                break
            if any(k in err_l for k in ("402", "insufficient balance", "401", "invalid_api_key")):
                set_llm_cooldown("openrouter", e, "OpenRouter", override_seconds=1800.0)
                break
            set_llm_cooldown(cd_key, e, f"OpenRouter ({model_name})", override_seconds=15.0)

    raise RuntimeError("Semua model OpenRouter gagal")


def _call_betabotz_api(system_prompt: str, user_prompt: str, music_mode: bool = False) -> str:
    if not betabotz_key or is_llm_on_cooldown("betabotz"):
        raise RuntimeError("BetaBotz tidak aktif atau sedang cooldown")

    base_url = "https://api.betabotz.eu.org/api/search"
    compact_logic = (system_prompt or "Kamu adalah Mina, AI Co-Host TikTok Live. Jawab singkat maksimal 2 kalimat tanpa markdown.")[:1000]

    def extract_text(payload) -> str:
        if isinstance(payload, dict):
            for key_name in ("result", "message", "data", "answer", "response", "content"):
                val = payload.get(key_name)
                if isinstance(val, str) and val.strip():
                    return _strip_reasoning_tags(val)
                if isinstance(val, dict):
                    nested = (
                        val.get("result")
                        or val.get("message")
                        or val.get("text")
                        or val.get("content")
                    )
                    if isinstance(nested, str) and nested.strip():
                        return _strip_reasoning_tags(nested)
        elif isinstance(payload, str):
            return _strip_reasoning_tags(payload)
        return ""

    attempts = [
        (
            "openai-logic (POST)",
            "POST",
            f"{base_url}/openai-logic",
            {
                "json": {
                    "apikey": betabotz_key,
                    "logic": compact_logic,
                    "text": user_prompt,
                }
            },
        ),
        (
            "openai-logic (GET)",
            "GET",
            f"{base_url}/openai-logic",
            {
                "params": {
                    "apikey": betabotz_key,
                    "logic": compact_logic[:650],
                    "text": user_prompt[:400],
                }
            },
        ),
    ]

    with httpx.Client(timeout=5.5, follow_redirects=True) as http:
        for ep_name, method, ep_url, req_kwargs in attempts:
            try:
                resp = http.request(method, ep_url, **req_kwargs)
                resp.raise_for_status()
                raw_text = extract_text(resp.json())

                if _is_provider_error_text(raw_text):
                    raise ValueError(f"BetaBotz ({ep_name}) mengembalikan pesan limit/error server")

                if music_mode:
                    first_line = raw_text.splitlines()[0].strip('"\'*-.0123456789 ')
                    if not _is_valid_song_candidate(first_line):
                        raise ValueError(f"BetaBotz ({ep_name}) bukan judul lagu valid")
                    raw_text = first_line

                state["last_llm_used"] = f"BetaBotz ({ep_name})"
                return raw_text
            except Exception as e:
                TermUI.log("WARN", f"BetaBotz ({ep_name}) fallback: {e}")

    set_llm_cooldown("betabotz", RuntimeError("Semua endpoint BetaBotz gagal/limit"), "BetaBotz", override_seconds=45.0)
    raise RuntimeError("Semua endpoint BetaBotz gagal")


def call_llm_cascade(
    system_prompt: str,
    user_prompt: str,
    json_mode: bool = False,
    temperature: float = 0.65,
    max_tokens: int = 768,
) -> str:
    # 1. Utama: Gemini Cascade (cooldown 10s untuk error sementara, 3600s untuk 404)
    if gemini_clients:
        combined_prompt = f"{system_prompt}\n\n{user_prompt}" if system_prompt else user_prompt
        for model_name in GEMINI_MODELS:
            for key_idx, client in enumerate(gemini_clients, start=1):
                cd_key = f"gemini:k{key_idx}:{model_name}"
                if is_llm_on_cooldown(cd_key):
                    continue

                key_tag = f"Key#{key_idx} " if len(gemini_clients) > 1 else ""
                try:
                    cfg_kwargs = {
                        "temperature": temperature,
                        "max_output_tokens": max_tokens,
                    }
                    if json_mode:
                        cfg_kwargs["response_mime_type"] = "application/json"

                    res = client.models.generate_content(
                        model=model_name,
                        contents=combined_prompt,
                        config=types.GenerateContentConfig(**cfg_kwargs),
                    )
                    raw_content = _strip_reasoning_tags(res.text or "")
                    if not raw_content or _is_provider_error_text(raw_content):
                        raise ValueError(f"{model_name} konten kosong")
                    state["last_llm_used"] = f"Gemini {key_tag}({model_name})"
                    return raw_content
                except Exception as e:
                    _apply_gemini_cooldown(cd_key, e, f"Gemini {key_tag}({model_name})")

    # 2. Cadangan pertama: BetaBotz
    if betabotz_key and not is_llm_on_cooldown("betabotz"):
        try:
            data = load_dataset_dict()
            host_name = data.get("profil_host", {}).get("nama_panggilan") or "Bang Hanif"
            topik_aktif = data.get("konteks_live", {}).get("topik_bahasan") or "Obrolan santai"
            short_sys = (
                f"Kamu adalah Mina, AI Co-Host TikTok Live milik {host_name} (@{tiktok_username}). "
                f"Topik live: {topik_aktif}. Jawab komentar penonton secara santai dan sopan (1-2 kalimat), "
                "panggil nama tampilannya tanpa simbol @ atau markdown."
            )
            return _call_betabotz_api(short_sys, user_prompt, music_mode=False)
        except Exception:
            pass

    # 3. Cadangan kedua: OpenRouter
    if openrouter_client and not is_llm_on_cooldown("openrouter"):
        or_models = []
        if openrouter_custom_model:
            or_models.append(openrouter_custom_model)
        or_models.extend(_get_live_openrouter_free_models())
        dedup_or = list(dict.fromkeys(or_models))
        try:
            return _call_openrouter(
                dedup_or,
                system_prompt,
                user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as e:
            TermUI.log("ERROR", f"OpenRouter gagal: {e}")

    raise RuntimeError("Semua provider LLM sedang limit/sibuk.")


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

    try:
        raw = call_llm_cascade("", prompt, json_mode=False, temperature=0.1, max_tokens=64)
        ans = raw.strip().upper()
        if "SEMENTARA" in ans:
            return "sementara"
        if "PERMANEN" in ans:
            return "permanen"
    except Exception:
        pass

    return "sementara"


def save_learned_qa(question: str, answer: str, source: str = "discord_host", force_type: str | None = None) -> str:
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
            "created_at": now_str,
        })

    if qa_type == "sementara":
        if not isinstance(data.get("konteks_live"), dict):
            data["konteks_live"] = {}
        data["konteks_live"]["status_terbaru"] = f"{question} -> {answer}"
        data["konteks_live"]["diperbarui_pada"] = now_str

    write_dataset_dict(data)
    TermUI.log("AI-MOD", f"Self-Learning ({qa_type.upper()}): '{question}' -> '{answer}'")
    return qa_type


def parse_agent_json(raw_text: str) -> dict:
    cleaned = _strip_reasoning_tags(raw_text)
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE).strip()
    if not cleaned or _is_provider_error_text(cleaned):
        raise ValueError("Respons LLM kosong atau pesan error provider")

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(cleaned[start:end + 1])
            if isinstance(parsed, dict) and "action" in parsed:
                if parsed.get("reply_text"):
                    rep = _sanitize_plain_reply(str(parsed["reply_text"]), max_sentences=3)
                    if _is_provider_error_text(rep):
                        return {"action": "IGNORE", "intent": "error", "thought": "Blocked error text", "reply_text": ""}
                    parsed["reply_text"] = rep
                return parsed
        except json.JSONDecodeError:
            pass

    short_reply = _sanitize_plain_reply(cleaned, max_sentences=2)
    if _is_provider_error_text(short_reply):
        return {"action": "IGNORE", "intent": "error", "thought": "Blocked error text", "reply_text": ""}
    return {
        "intent": "obrolan_umum",
        "action": "CHAT_AI" if short_reply else "IGNORE",
        "thought": "Ekstraksi teks singkat",
        "memory_note": "",
        "reply_text": short_reply,
    }


def run_moderator_reasoning(user_id: str, user_name: str, comment: str) -> dict:
    data = load_dataset_dict()
    filtered_data = {
        k: v for k, v in data.items()
        if k not in ("daftar_badword", "banned_users", "kapabilitas_berpikir", "host_aliases")
    }
    dataset_str = json.dumps(filtered_data, ensure_ascii=False, separators=(",", ":"))
    room_history_str = "\n".join(room_chat_history) if room_chat_history else "-"
    user_thread_str = get_user_recent_thread(user_id)
    viewer_profile_str = get_viewer_memory_summary(user_id, user_name)
    warning_count = user_warnings.get(user_id, {}).get("count", 0)
    waktu_sekarang = datetime.now().strftime("%H:%M WIB")

    konteks_live = data.get("konteks_live", {})
    profil_host = data.get("profil_host", {})
    host_name = profil_host.get("nama_panggilan") or "Bang Hanif"
    topik_aktif = konteks_live.get("topik_bahasan") or profil_host.get("minat_topik") or "Obrolan santai"
    status_aktif = konteks_live.get("status_terbaru") or "Live santai"

    system_prompt = (
        f"Kamu adalah Mina, AI Co-Host & Moderator TikTok Live yang mendampingi Host ({host_name} / @{tiktok_username}).\n"
        f"Waktu: {waktu_sekarang} | Tema Utama Live: {topik_aktif} | Situasi Saat Ini: {status_aktif}\n"
        f"Profil Penonton Aktif: {viewer_profile_str} | Strike: {warning_count}/3\n"
        f"Chat Terakhir dari {user_name}: {user_thread_str}\n"
        f"Alur Obrolan Room Terbaru:\n{room_history_str}\n"
        f"Knowledge Base Host: {dataset_str}\n\n"
        "Pedoman Utama:\n"
        "1. PAHAM KONTEKS: Baca 'Chat Terakhir dari user' dan 'Alur Obrolan Room' sebelum menjawab agar paham maksud kata ganti atau lanjutan kalimat viewer yang terpotong-potong.\n"
        "2. KESEIMBANGAN 50:50 (TEMA VS UMUM): Tema utama hanyalah jangkar suasana. Jika penonton bertanya atau membahas hal di luar tema live (pengetahuan umum, teknologi, tips, otomotif, hiburan, dll.), TETAP LAYANI dan jawab dengan akurat memakai pengetahuan AI. Di akhir jawaban di luar tema, tawarkan dengan santai apakah mau lanjut bahas topik itu atau kembali ke tema utama live.\n"
        "3. KARAKTER NATURAL (ANTI-PLENGER & ANTI-SOK ASIK): Bicara santai, jelas, sopan, nyambung, dan tidak lebay/cringe. Jangan mengarang fakta pribadi host jika tidak ada di Knowledge Base (jika menyangkut kondisi live/pribadi host yang tidak diketahui, pilih ESCALATE). Sesuaikan keakraban berdasarkan status kunjungan penonton.\n"
        "4. ATURAN & AKSI:\n"
        "   - CHAT_AI: Tanya jawab, diskusi sesuai tema maupun di luar tema, sapaan.\n"
        "   - HYPE: Apresiasi support/semangat penonton.\n"
        "   - WARN: Penghinaan/SARA/spam/promosi ilegal nyata (jika hanya bertanya soal aturan secara hipotetis, pilih CHAT_AI).\n"
        "   - DEFLECT: Menangkis pertanyaan informasi sensitif/privat host dengan sopan.\n"
        "   - ESCALATE: Kendala teknis stream atau pertanyaan real-time yang hanya bisa dijawab langsung oleh Host.\n"
        "   - IGNORE: Ketikan acak tanpa makna sama sekali.\n"
        "5. FORMAT OUTPUT:\n"
        "   - reply_text: Bahasa Indonesia lisan yang enak didengar TTS, tanpa emoji, tanpa simbol @, tanpa markdown, maksimal 3 kalimat (maks 45 kata).\n"
        "   - memory_note: Jika penonton menyebut fakta tentang dirinya (misal asal kota, hobi, kesibukan), tulis ringkas 3-6 kata untuk disimpan di database; jika tidak ada, isi string kosong \"\".\n"
        'Balas JSON murni: {"intent":"...","action":"CHAT_AI|HYPE|WARN|DEFLECT|ESCALATE|IGNORE","thought":"1 kalimat singkat","memory_note":"","reply_text":"..."}'
    )
    user_prompt = f'Penonton {user_name} (@{user_id}): "{comment}"'

    raw_content = call_llm_cascade(
        system_prompt,
        user_prompt,
        json_mode=True,
        temperature=0.65,
        max_tokens=768,
    )
    decision = parse_agent_json(raw_content)

    mem_note = (decision.get("memory_note") or "").strip()
    if mem_note:
        add_viewer_memory_note(user_id, mem_note)

    return decision


def generate_autoplay_next_song(seed_query: str, last_played: str | None, history: list[str]) -> str:
    history_text = ", ".join(history[-10:]) if history else "Belum ada"
    last_info = f' (baru diputar: "{last_played}")' if last_played else ""

    logic_prompt = (
        "Output ONLY 1 song in exact format: Song Title - Artist Name. "
        "No greeting, no intro, no explanation, no numbering."
    )
    user_prompt = (
        f'Give 1 song similar in vibe to "{seed_query}"{last_info}, '
        f'not in [{history_text}]. Format: Song Title - Artist Name'
    )

    candidate = ""
    if betabotz_key and not is_llm_on_cooldown("betabotz"):
        try:
            raw = _call_betabotz_api(logic_prompt, user_prompt, music_mode=True)
            if _is_valid_song_candidate(raw):
                candidate = raw
        except Exception as e:
            TermUI.log("WARN", f"Autoplay BetaBotz fallback ke variasi lokal: {e}")

    if not candidate:
        suffixes = ["acoustic", "cover", "live version", "official audio", "lofi"]
        candidate = f"{seed_query} {random.choice(suffixes)}"

    return candidate


def transcribe_voice_note(audio_bytes: bytes, mime_type: str) -> str:
    if not gemini_clients:
        raise RuntimeError("GEMINI_API_KEY belum terpasang untuk transkripsi Voice Note.")

    clean_mime = mime_type.split(";")[0].strip() or "audio/ogg"
    prompt = (
        "Transkripsikan rekaman suara bahasa Indonesia ini menjadi satu paragraf kalimat yang natural dan rapi. "
        "Jangan tambahkan tanda kutip, label pembicara, atau penjelasan apa pun. "
        "Jika suara tidak terdengar atau kosong, balas persis dengan: [TIDAK_JELAS]"
    )

    for model_name in GEMINI_MODELS:
        for key_idx, client in enumerate(gemini_clients, start=1):
            cd_key = f"gemini:k{key_idx}:{model_name}"
            if is_llm_on_cooldown(cd_key):
                continue
            try:
                res = client.models.generate_content(
                    model=model_name,
                    contents=[
                        prompt,
                        types.Part.from_bytes(data=audio_bytes, mime_type=clean_mime),
                    ],
                )
                return (res.text or "").strip()
            except Exception as e:
                _apply_gemini_cooldown(cd_key, e, f"STT {model_name}")

    raise RuntimeError("Gagal mentranskripsi Voice Note karena kuota/server Gemini sibuk.")


def normalize_for_speech(text: str) -> str:
    cleaned = text.replace("\n", ", ").replace("/", " atau ").replace("*", "").replace("@", "")
    cleaned = re.sub(r"[^\w\s.,!?'-]", " ", cleaned)
    return " ".join(cleaned.split())


def split_sentences_for_tts(text: str) -> list[str]:
    speech_text = normalize_for_speech(text)
    if not speech_text:
        return []
    raw_parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", speech_text) if re.search(r"[a-zA-Z0-9]", p)]
    if len(raw_parts) <= 1:
        return raw_parts[:3]

    merged = []
    for part in raw_parts:
        if merged and len(part) < 14:
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged[:3]


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


async def synthesize_chunk_fast(sentence: str) -> io.BytesIO | None:
    if not re.search(r"[a-zA-Z0-9]", sentence):
        return None

    try:
        stream = await asyncio.wait_for(generate_neural_edgetts(sentence), timeout=5.0)
        if stream.getbuffer().nbytes > 0:
            return stream
    except Exception:
        pass

    try:
        return await asyncio.wait_for(asyncio.to_thread(generate_backup_gtts, sentence), timeout=7.0)
    except Exception as e:
        err_detail = str(e) or type(e).__name__
        TermUI.log("WARN", f"TTS gagal mensintesis segmen: {err_detail}")
        return None


async def speak_to_vbcable(text: str, ensure_audio_fn=None):
    sentences = split_sentences_for_tts(text)
    if not sentences:
        return

    if ensure_audio_fn:
        ensure_audio_fn()

    t_tts = time.time()
    for idx, sentence in enumerate(sentences):
        audio_stream = await synthesize_chunk_fast(sentence)
        if audio_stream is None or audio_stream.getbuffer().nbytes == 0:
            continue
        if idx == 0:
            TermUI.log("VOICE", f"Suara mulai diputar (TTFA: {time.time() - t_tts:.2f}s | {len(sentences)} segmen)")
        pygame.mixer.music.load(audio_stream, "mp3")
        pygame.mixer.music.play()
        play_started = time.time()
        while pygame.mixer.music.get_busy():
            if (time.time() - play_started) > 25.0:
                pygame.mixer.music.stop()
                break
            await asyncio.sleep(0.02)
        pygame.mixer.music.unload()