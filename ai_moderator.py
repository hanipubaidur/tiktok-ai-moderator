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
    gemini_key,
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

gemini_client = (
    genai.Client(
        api_key=gemini_key,
        http_options=types.HttpOptions(timeout=12000),
    )
    if gemini_key
    else None
)

openrouter_client = (
    OpenAI(
        api_key=openrouter_key,
        base_url="https://openrouter.ai/api/v1",
        timeout=12.0,
        max_retries=0,
        default_headers={
            "HTTP-Referer": "https://localhost",
            "X-Title": "Mina AI Moderator",
        },
    )
    if openrouter_key
    else None
)


def _strip_reasoning_tags(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _call_openrouter(
    models: list[str],
    system_prompt: str,
    user_prompt: str,
    json_mode: bool = False,
    temperature: float = 0.65,
    max_tokens: int = 1024,
) -> str:
    if not openrouter_client or is_llm_on_cooldown("openrouter"):
        raise RuntimeError("OpenRouter tidak aktif atau sedang cooldown")

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    for model_name in models:
        cd_key = f"openrouter:{model_name}"
        if is_llm_on_cooldown(cd_key):
            continue
        try:
            kwargs = {
                "model": model_name,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "timeout": 12.0,
            }
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}

            try:
                res = openrouter_client.chat.completions.create(**kwargs)
            except Exception as inner_e:
                if json_mode and "response_format" in str(inner_e).lower():
                    kwargs.pop("response_format", None)
                    res = openrouter_client.chat.completions.create(**kwargs)
                else:
                    raise

            choices = getattr(res, "choices", None)
            if not choices:
                raise ValueError(f"{model_name} choices kosong")

            raw_content = _strip_reasoning_tags(getattr(choices[0].message, "content", None) or "")
            if not raw_content:
                raise ValueError(f"{model_name} konten kosong")

            state["last_llm_used"] = f"OpenRouter ({model_name})"
            return raw_content
        except Exception as e:
            err_l = str(e).lower()
            if any(k in err_l for k in ("402", "insufficient balance", "401", "invalid_api_key")):
                set_llm_cooldown("openrouter", e, "OpenRouter")
                break
            set_llm_cooldown(cd_key, e, f"OpenRouter ({model_name})")

    raise RuntimeError("Semua model OpenRouter gagal")


def _call_betabotz_api(system_prompt: str, user_prompt: str) -> str:
    if not betabotz_key or is_llm_on_cooldown("betabotz"):
        raise RuntimeError("BetaBotz tidak aktif atau sedang cooldown")

    base_url = "https://api.betabotz.eu.org/api/search"
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    def extract_text(payload) -> str:
        if isinstance(payload, dict):
            for key_name in ("result", "message", "data", "answer", "response"):
                val = payload.get(key_name)
                if isinstance(val, str) and val.strip():
                    return _strip_reasoning_tags(val)
                if isinstance(val, dict):
                    nested = val.get("result") or val.get("message") or val.get("text")
                    if isinstance(nested, str) and nested.strip():
                        return _strip_reasoning_tags(nested)
        elif isinstance(payload, str):
            return _strip_reasoning_tags(payload)
        return ""

    attempts = [
        (
            "openai-custom (POST)",
            "POST",
            f"{base_url}/openai-custom",
            {"json": {"apikey": betabotz_key, "message": messages}},
        ),
        (
            "openai-logic (POST)",
            "POST",
            f"{base_url}/openai-logic",
            {
                "json": {
                    "apikey": betabotz_key,
                    "logic": system_prompt or "Kamu adalah Mina, AI Co-Host & Moderator TikTok Live.",
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
                    "logic": (system_prompt or "")[:1200],
                    "text": user_prompt,
                }
            },
        ),
    ]

    with httpx.Client(timeout=12.0, follow_redirects=True) as http:
        for ep_name, method, ep_url, req_kwargs in attempts:
            try:
                resp = http.request(method, ep_url, **req_kwargs)
                resp.raise_for_status()
                raw_text = extract_text(resp.json())

                if not raw_text or ("apikey" in raw_text.lower() and "invalid" in raw_text.lower()):
                    raise ValueError(f"BetaBotz ({ep_name}) respons tidak valid/kosong")

                state["last_llm_used"] = f"BetaBotz ({ep_name})"
                return raw_text
            except Exception as e:
                TermUI.log("WARN", f"BetaBotz ({ep_name}) fallback: {e}")

    set_llm_cooldown("betabotz", RuntimeError("Semua endpoint BetaBotz gagal"), "BetaBotz")
    raise RuntimeError("Semua endpoint BetaBotz gagal")


def call_llm_cascade(
    system_prompt: str,
    user_prompt: str,
    json_mode: bool = False,
    temperature: float = 0.65,
    max_tokens: int = 1024,
) -> str:
    if gemini_client:
        combined_prompt = f"{system_prompt}\n\n{user_prompt}" if system_prompt else user_prompt
        for model_name in ["gemini-3.5-flash-lite", "gemini-3.8-flash"]:
            cd_key = f"gemini:{model_name}"
            if is_llm_on_cooldown(cd_key):
                continue
            try:
                cfg_kwargs = {
                    "temperature": temperature,
                    "max_output_tokens": max_tokens,
                    "http_options": types.HttpOptions(timeout=12000),
                }
                if json_mode:
                    cfg_kwargs["response_mime_type"] = "application/json"

                res = gemini_client.models.generate_content(
                    model=model_name,
                    contents=combined_prompt,
                    config=types.GenerateContentConfig(**cfg_kwargs),
                )
                raw_content = _strip_reasoning_tags(res.text or "")
                if not raw_content:
                    raise ValueError(f"{model_name} konten kosong")
                state["last_llm_used"] = f"Gemini ({model_name})"
                return raw_content
            except Exception as e:
                set_llm_cooldown(cd_key, e, f"Gemini ({model_name})")

    if betabotz_key and not is_llm_on_cooldown("betabotz"):
        try:
            return _call_betabotz_api(system_prompt, user_prompt)
        except Exception:
            pass

    if openrouter_client and not is_llm_on_cooldown("openrouter"):
        or_models = []
        if openrouter_custom_model:
            or_models.append(openrouter_custom_model)
        or_models.extend([
            "deepseek/deepseek-chat-v3-0324:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "qwen/qwen-2.5-72b-instruct:free",
            "google/gemma-3-27b-it:free",
            "openrouter/free",
        ])
        dedup_or = list(dict.fromkeys(or_models))
        try:
            return _call_openrouter(
                dedup_or,
                system_prompt,
                user_prompt,
                json_mode=json_mode,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as e:
            TermUI.log("ERROR", f"OpenRouter gagal: {e}")

    raise RuntimeError("Semua provider LLM gagal merespons.")


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
        raw = call_llm_cascade("", prompt, json_mode=False, temperature=0.1, max_tokens=32)
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
        "memory_note": "",
        "reply_text": cleaned,
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
        "   - reply_text: Bahasa Indonesia lisan yang enak didengar TTS, tanpa emoji, tanpa markdown/simbol, maksimal 3 kalimat (maks 45 kata).\n"
        "   - memory_note: Jika penonton menyebut fakta tentang dirinya (misal asal kota, hobi, kesibukan), tulis ringkas 3-6 kata untuk disimpan di database; jika tidak ada, isi string kosong \"\".\n"
        'Balas JSON murni: {"intent":"...","action":"CHAT_AI|HYPE|WARN|DEFLECT|ESCALATE|IGNORE","thought":"1 kalimat singkat","memory_note":"","reply_text":"..."}'
    )
    user_prompt = f'Penonton {user_name} (@{user_id}): "{comment}"'

    raw_content = call_llm_cascade(
        system_prompt,
        user_prompt,
        json_mode=True,
        temperature=0.65,
        max_tokens=1024,
    )
    decision = parse_agent_json(raw_content)

    mem_note = (decision.get("memory_note") or "").strip()
    if mem_note:
        add_viewer_memory_note(user_id, mem_note)

    return decision


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
    try:
        raw = call_llm_cascade("", prompt, json_mode=False, temperature=0.8, max_tokens=256)
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if lines:
            candidate = lines[0].strip('"\'*-.0123456789 ')
    except Exception as e:
        TermUI.log("WARN", f"Autoplay LLM fallback: {e}")

    if not candidate:
        suffixes = ["acoustic", "cover", "live version", "official audio", "instrumental"]
        candidate = f"{seed_query} {random.choice(suffixes)}"

    return candidate


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
                        types.Part.from_bytes(data=audio_bytes, mime_type=clean_mime),
                    ],
                    config=types.GenerateContentConfig(
                        http_options=types.HttpOptions(timeout=14000),
                    ),
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


async def synthesize_chunk_fast(sentence: str) -> io.BytesIO | None:
    try:
        stream = await asyncio.wait_for(generate_neural_edgetts(sentence), timeout=1.6)
        if stream.getbuffer().nbytes > 0:
            return stream
    except Exception:
        pass
    try:
        return await asyncio.wait_for(asyncio.to_thread(generate_backup_gtts, sentence), timeout=5.0)
    except Exception as e:
        TermUI.log("WARN", f"TTS gagal mensintesis segmen: {e}")
        return None


async def speak_to_vbcable(text: str, ensure_audio_fn=None):
    sentences = split_sentences_for_tts(text)
    if not sentences:
        return

    if ensure_audio_fn:
        ensure_audio_fn()

    t_tts = time.time()
    synth_tasks = [asyncio.create_task(synthesize_chunk_fast(s)) for s in sentences]

    for idx, task in enumerate(synth_tasks):
        audio_stream = await task
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