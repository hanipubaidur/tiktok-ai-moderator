# TikTok Live AI Co-Host, Moderator & Live Stream Dashboard

Asisten AI otonom berbasis Python yang berperan sebagai **Co-Host**, **Moderator Otomatis**, sekaligus **Pemutar Musik Request (Pre-Fetch Queue) & Autoplay** untuk siaran langsung (Live) TikTok. Bot ini dibangun dengan arsitektur modular, berjalan secara lokal di PC/Laptop, terhubung ke Discord sebagai pusat kendali host, dilengkapi **Live Stream Dashboard (SSE Real-Time)**, dan menyalurkan output suara maupun musik langsung ke **TikTok Live Studio / OBS** melalui **VB-Audio Virtual Cable**.

---

## Fitur Utama

- **AI Co-Host Kontekstual & Memori Penonton (`viewers_db.json`):**
  - Otomatis membedakan komentar **Host** dan **Penonton** agar AI tidak membalas ucapan host secara canggung, namun tetap mencatat obrolan host sebagai konteks situasi.
  - Memahami alur percakapan sebelumnya dari masing-masing penonton maupun obrolan *room* secara keseluruhan, serta menyimpan profil kunjungan dan fakta penonton secara otomatis ke `viewers_db.json`.
  - **Keseimbangan Topik 50:50 & Gaya Bicara Natural:** Pertanyaan di luar tema *live* tetap dijawab akurat sesuai pengetahuan AI lalu ditutup dengan tawaran halus untuk lanjut membahas topik tersebut atau kembali ke tema utama—tanpa mengarang fakta (*anti-plenger*) dan tidak berlebihan.
- **Multi-Provider LLM Cascade:** Mendukung fallback otomatis berantai dari **Google Gemini** (`gemini-3.5-flash-lite` / `gemini-3.8-flash`) $\rightarrow$ **BetaBotz API** $\rightarrow$ **OpenRouter** (model `:free`) lengkap dengan sistem *cooldown* cerdas saat terjadi limit atau gangguan jaringan.
- **Moderasi Otomatis 3-Strike & Auto-Enforce Ban:** Mendeteksi kata kasar (*badword*) secara instan, memberikan peringatan suara bertingkat (Strike 1 hingga 3), serta mengeksekusi **Mute**, **Kick**, atau **Permanent Ban** secara otomatis maupun manual.
- **Background Pre-Fetch Music Queue & Vibe Autoplay:** Penonton dapat me-request lagu langsung dari kolom komentar TikTok. Sistem mengunduh dan mengonversi hingga 3 lagu berikutnya di latar belakang saat lagu aktif diputar sehingga pergantian lagu berjalan **tanpa jeda tunggu**. Jika antrean kosong, AI otomatis memilihkan lagu berikutnya yang se-vibe dengan lagu terakhir.
- **Live Stream Dashboard (SSE Real-Time):** Dashboard web lokal di `http://127.0.0.1:8765` (mendukung mode *Chroma Key* `/?chroma=1`) yang menampilkan **Antrean Musik (Scrollable + status Pre-Load)**, **Top Like Leaderboard**, **Top Gifter Leaderboard**, serta **Rekap Total Masing-Masing Gift** secara *real-time*.
- **Kontrol Penuh via Discord & Terminal Dashboard:** Dilengkapi panel tombol interaktif Discord, pengiriman *Voice Note* (otomatis ditranskrip dan dibacakan AI ke *live*), sistem eskalasi pertanyaan penting ke host, serta tampilan terminal *full-width* yang bersih.

---

## Struktur Proyek

```text
tiktok-ai-moderator/
├── .env                    # Konfigurasi API key, token Discord, & akun TikTok Host
├── dataset.json            # Konteks live, badword, FAQ, & memori QA host
├── viewers_db.json         # Otomatis dibuat: database kunjungan, catatan, like & gift viewer
├── live_music_queue.txt    # Otomatis dibuat: sinkronisasi teks antrean musik untuk OBS
├── config.py               # Konstanta, state global, deteksi Host, LLM cooldown, & TermUI
├── storage.py              # Manajemen dataset.json, viewers_db.json, riwayat chat & statistik
├── overlay_dashboard.py    # Server HTTP/SSE & antarmuka Live Stream Dashboard
├── ai_moderator.py         # LLM Cascade (Gemini -> BetaBotz -> OpenRouter), STT & TTS
├── music_engine.py         # Audio VB-Cable, Background Pre-Downloader, & Autoplay AI
└── main.py                 # Entry point: event TikTokLive & bot kontrol Discord
```

---

## Cara Kerja Sistem

```text
[TikTok Live Chat & Events] ──► [Python Engine (main.py)] ◄──► [Discord Control Panel]
                                   │        │        │
        (Suara AI TTS)             │        │        │   (Pre-Fetched Audio yt-dlp + FFmpeg)
                                   │        ▼        ▼
                                   │   [CABLE Input (VB-Virtual Cable)]
                                   │        │
                                   │        ▼
                                   │   [CABLE Output (Recording)]
                                   │      ├──► TikTok Live Studio / OBS (Masuk ke Stream)
                                   │      └──► Headset Host (Via "Listen to this device")
                                   │
                                   └──► [Live Dashboard :8765] ──► OBS / Window Capture
```

1. **Penangkapan & Filter Event (`main.py`):** `TikTokLive` memantau komentar, gift, follow, like, dan share secara *real-time*. Komentar dari akun Host langsung ditandai sebagai `[HOST]` dan dicatat ke memori konteks tanpa dibalas AI.
2. **Pemrosesan & Keputusan AI (`ai_moderator.py` & `storage.py`):** Komentar penonton melewati filter *badword* lokal dan pencocokan memori permanen (`dataset.json`). Jika lolos, komentar diproses bersama riwayat obrolan penonton dan profil dari `viewers_db.json` melalui rantai **Gemini $\rightarrow$ BetaBotz $\rightarrow$ OpenRouter** untuk menentukan aksi (`CHAT_AI`, `HYPE`, `WARN`, `DEFLECT`, atau `ESCALATE`).
3. **Pre-Fetch & Mixing Audio Lokal (`music_engine.py`):** Worker latar belakang menyiapkan file `.wav` untuk antrean lagu berikutnya terlebih dahulu. Suara AI diputar pada saluran mixer utama, sementara musik diputar pada saluran terpisah (`Channel 1`) dan dikirim ke **CABLE Input**.
4. **Penyiaran & Live Dashboard (`overlay_dashboard.py`):** Aplikasi *streaming* menangkap **CABLE Output** sebagai sumber audio dan menampilkan **Live Stream Dashboard** (antrean lagu, *Top Like*, dan *Top Gift*) secara langsung.

---

## Persyaratan Sistem (Requirements)

### 1. Spesifikasi Hardware (PC / Laptop)
- **Sistem Operasi:** Windows 10 atau Windows 11 (64-bit). *(Wajib Windows karena konfigurasi device audio SDL2 dan VB-Cable menggunakan driver audio Windows).*
- **Prosesor (CPU):** Minimum Intel Core i3 Gen-8 / AMD Ryzen 3. *(Disarankan **Core i5 / Ryzen 5** ke atas agar proses encoding video live dan konversi audio FFmpeg berjalan mulus).*
- **Memori (RAM):** Minimum **8 GB** *(Disarankan **16 GB** jika menjalankan TikTok Live Studio, Python, dan browser secara bersamaan).*
- **Penyimpanan:** Minimal 2 GB ruang kosong untuk dependensi dan *buffer* sementara audio.
- **Koneksi Internet:** Stabil minimal **10 Mbps (Upload)** untuk menjaga latensi API AI, pre-download audio YouTube, dan kualitas siaran TikTok.

### 2. Software & Akun Pendukung
- **Python 3.10+** *(Disarankan Python 3.11 – 3.14)*.
- **VB-Audio Virtual Cable** ([Download di vb-audio.com](https://vb-audio.com/Cable/)).
- **FFmpeg** *(Dapat diinstal ke System PATH Windows, ditaruh sebagai `ffmpeg.exe` di folder project, atau otomatis menggunakan paket `imageio-ffmpeg` di `requirements.txt`)*.
- **TikTok Live Studio** atau **OBS Studio**.
- **Akun & Bot Discord** untuk panel kontrol host.
- **API Key LLM:** **Google Gemini API Key** (disarankan sebagai utama), serta **BetaBotz API Key** dan/atau **OpenRouter API Key** sebagai AI cadangan.

---

## Langkah Instalasi & Konfigurasi

### Langkah 1: Clone Repository & Install Library Python
Buka terminal (CMD / PowerShell) di folder project, lalu jalankan perintah berikut:

```bash
git clone [https://github.com/hanipubaidur/tiktok-ai-moderator](https://github.com/hanipubaidur/tiktok-ai-moderator)
cd tiktok-ai-moderator

# Buat dan aktifkan virtual environment (disarankan)
python -m venv venv
venv\Scripts\activate

# Install seluruh library yang dibutuhkan
pip install -r requirements.txt
```

### Langkah 2: Install VB-Audio Virtual Cable & FFmpeg
1. Ekstrak file **VB-Audio Virtual Cable**, klik kanan `VBCABLE_Setup_x64.exe` $\rightarrow$ **Run as administrator**, klik **Install Driver**, lalu *restart* PC jika diminta.
2. Install **FFmpeg** melalui terminal Windows (opsional jika sudah memakai `imageio-ffmpeg` dari `requirements.txt`, namun disarankan untuk performa terbaik):
   ```bash
   winget install Gyan.FFmpeg
   ```
   Tutup dan buka ulang terminal, lalu pastikan FFmpeg terbaca dengan mengetik `ffmpeg -version`.

### Langkah 3: Siapkan File `.env` & Kredensial
Ubah nama (*rename*) file **`.env.example`** menjadi **`.env`** (atau buat file `.env` baru), lalu isi variabel berikut:

```env
GEMINI_API_KEY=isi_api_key_gemini_kamu
BETABOTZ_API_KEY=isi_api_key_betabotz_kamu
OPENROUTER_API_KEY=sk-or-v1-isi_api_key_openrouter_kamu

DISCORD_BOT_TOKEN=isi_token_discord_bot_kamu
DISCORD_CHANNEL_ID=isi_channel_id_discord_kamu

TIKTOK_USERNAME=username_tiktok_tanpa_at
HOST_ALIASES=username_tiktok_tanpa_at,nama_panggilan_host
TIKTOK_SESSION_ID=isi_session_id_kamu
TIKTOK_TARGET_IDC=alisg
```

#### A. Mendapatkan API Key LLM (`GEMINI_API_KEY`, `BETABOTZ_API_KEY`, `OPENROUTER_API_KEY`)
1. **Gemini API Key (Utama):** Buka [Google AI Studio](https://aistudio.google.com/apikey), login menggunakan akun Google, klik **Create API key**, lalu salin kodenya ke variabel `GEMINI_API_KEY`.
2. **BetaBotz API Key (Cadangan 1 — Opsional):** Masukkan API key BetaBotz ke `BETABOTZ_API_KEY` (kosongkan jika tidak memakai).
3. **OpenRouter API Key (Cadangan 2 — Opsional):** Buka [OpenRouter Keys](https://openrouter.ai/keys), login, klik **Create Key**, lalu salin ke `OPENROUTER_API_KEY` (bot otomatis merotasi model gratis `:free` bawaan jika dipakai).

#### B. Membuat Bot & Mendapatkan `DISCORD_BOT_TOKEN` (Wajib)
1. Buka [Discord Developer Portal](https://discord.com/developers/applications) dan klik **New Application** (beri nama bebas, misalnya `AI Moderator`).
2. Masuk ke menu **Bot** di panel sebelah kiri:
   - Klik **Reset Token** $\rightarrow$ **Yes, do it!** $\rightarrow$ salin token yang muncul ke variabel `DISCORD_BOT_TOKEN` *(simpan baik-baik karena hanya muncul sekali)*.
   - Scroll ke bagian **Privileged Gateway Intents**, aktifkan (ON) sakelar **Message Content Intent**, lalu klik **Save Changes**.
3. Undang bot ke server Discord pribadi kamu:
   - Masuk ke menu **OAuth2** $\rightarrow$ **URL Generator**.
   - Pada bagian **Scopes**, centang **`bot`**.
   - Pada bagian **Bot Permissions**, centang **`Send Messages`**, **`Embed Links`**, **`Read Message History`**, dan **`Add Reactions`**.
   - Salin link yang muncul di bagian bawah, buka di browser, lalu pilih server Discord kamu dan klik **Authorize**.

#### C. Mendapatkan `DISCORD_CHANNEL_ID` (Wajib)
1. Buka aplikasi Discord $\rightarrow$ klik ikon **User Settings** (gerigi di pojok kiri bawah).
2. Pilih menu **Advanced** $\rightarrow$ aktifkan **Developer Mode**.
3. Kembali ke server Discord kamu, buat satu channel teks khusus (misalnya `#kontrol-live`), klik kanan pada nama channel tersebut $\rightarrow$ pilih **Copy Channel ID**, lalu tempel ke variabel `DISCORD_CHANNEL_ID`.

#### D. Mendapatkan `TIKTOK_USERNAME`, `HOST_ALIASES` & `TIKTOK_SESSION_ID`
1. Isi `TIKTOK_USERNAME` dengan username akun TikTok yang melakukan siaran langsung (tanpa tanda `@`), dan isi `HOST_ALIASES` dengan username maupun nama tampilan host (dipisahkan koma) agar AI tidak membalas chat milik host sendiri.
2. **Untuk Auto Kick/Mute/Ban (`TIKTOK_SESSION_ID` — Opsional):**
   - Buka browser (Chrome / Edge) di PC, kunjungi `https://www.tiktok.com`, dan **login menggunakan akun TikTok yang dipakai untuk Live**.
   - Tekan tombol **`F12`** $\rightarrow$ buka tab **Application** $\rightarrow$ menu **Cookies** $\rightarrow$ klik **`https://www.tiktok.com`**.
   - Cari baris **`sessionid`**, salin nilainya ke variabel `TIKTOK_SESSION_ID`, dan salin nilai **`tt-target-idc`** (misalnya `alisg`) ke `TIKTOK_TARGET_IDC`.

### Langkah 4: Perbarui `dataset.json` & Kustomisasi Suara (Sesuai Channel Kamu)
Repository ini sudah menyertakan file **`dataset.json`** bawaan. Sebelum menjalankan bot, buka dan sesuaikan isinya agar cocok dengan profil siaran kamu:

- **`identitas_ai`**: Ubah `nama`, `kepribadian`, dan `gaya_komunikasi` sesuai karakter AI yang kamu inginkan.
- **`profil_host`**: Ganti `nama_panggilan`, `domisili`, dan `minat_topik` dengan data kamu sendiri.
- **`konteks_live`**: Sesuaikan `topik_bahasan` dan `status_terbaru` *(bisa juga diubah langsung saat live via perintah `!topik` dan `!konteks` di Discord)*.
- **`protokol_moderasi`**: Atur `tindakan_otomatis_strike_3` (`"kick"`, `"mute"`, atau `"ban"`).
- **`daftar_badword` & `faq`**: Tambahkan atau hapus daftar kata terlarang serta pertanyaan yang sering ditanyakan penonton beserta jawabannya.

> **Kustomisasi Prompt & Suara (Opsional):**
> * Untuk mengganti jenis suara TTS, ubah variabel `NEURAL_VOICE` pada file **`config.py`** (`"id-ID-GadisNeural"` untuk suara wanita atau `"id-ID-ArdiNeural"` untuk suara pria).
> * Jika ingin menyesuaikan gaya bicara persona AI lebih lanjut, edit bagian `system_prompt` di dalam fungsi `run_moderator_reasoning()` pada file **`ai_moderator.py`**.

### Langkah 5: Pengaturan Routing Audio & Live Dashboard (Windows & Aplikasi Live)
1. **Windows Default Audio:** Pastikan perangkat *Default Playback* Windows tetap mengarah ke **Headset / Speaker** utama kamu (bukan ke *CABLE Input*) agar suara notifikasi Windows atau Discord tidak ikut masuk ke siaran.
2. **Mendengar Suara AI & Musik di Headset:**
   - Tekan `Win + R`, ketik `mmsys.cpl`, lalu tekan `Enter`.
   - Buka tab **Recording** $\rightarrow$ klik kanan **CABLE Output** $\rightarrow$ **Properties**.
   - Buka tab **Listen** $\rightarrow$ centang **Listen to this device** $\rightarrow$ pilih perangkat **Headset** kamu pada menu dropdown $\rightarrow$ klik **Apply** & **OK**.
3. **Sumber Audio di TikTok Live Studio / OBS Studio:**
   - Tambahkan sumber audio baru (*Audio Input Capture* / *Microphone*).
   - Pilih perangkat **CABLE Output (VB-Audio Virtual Cable)**.
4. **Live Stream Dashboard (Opsional):**
   - **Untuk OBS Studio:** Tambahkan **Browser Source** dan arahkan ke `http://127.0.0.1:8765` (ukuran disarankan `920 x 620`).
   - **Untuk TikTok Live Studio:** Jalankan `!openoverlay` (atau `!openoverlay chroma` untuk latar hijau) di Discord untuk membuka jendela dashboard terpisah, tangkap menggunakan **Window Capture**, dan pastikan jendela tersebut **tidak di-minimize** agar pembaruan *real-time* tetap berjalan.

### Langkah 6: Menjalankan Bot
Mulai siaran langsung di TikTok terlebih dahulu, lalu jalankan bot melalui terminal:

```bash
python main.py
```

---

## Panduan Perintah (Commands)

### Perintah Penonton (Komentar TikTok Live)
- `!play <judul lagu>` / `req <judul lagu>` / `m!p <judul lagu>` — Menambahkan lagu ke antrean pemutar musik (langsung di-download di latar belakang) dan memperbarui patokan *vibes* Autoplay.

### Perintah Host (Channel Kontrol Discord)

#### Konteks Live, Suara & Memori AI
- **Kirim Voice Note:** Otomatis ditranskrip oleh Gemini dan dibacakan oleh AI ke *live* (sekaligus menjawab pertanyaan eskalasi jika ada).
- **Balas Teks Biasa:** Jika ada pertanyaan eskalasi yang menunggu, balasan teks kamu langsung dibacakan AI ke *live* dan disimpan ke memori.
- `!topik <tema baru>` — Mengubah fokus topik obrolan utama AI.
- `!konteks <aktivitas>` — Memperbarui situasi *live* saat ini dan membersihkan memori sementara lama.
- `!say <kalimat>` — Memerintahkan AI membacakan teks secara langsung ke *live* dan memperbarui konteks.
- `!learn <pertanyaan> | <jawaban>` — Melatih AI mengingat fakta permanen baru.
- `!forget <kata kunci>` — Menghapus memori `learned_qa` yang mengandung kata kunci tertentu.
- `!skip` — Melewati pertanyaan eskalasi aktif yang sedang menunggu jawaban.

#### Pemutar Musik & Live Dashboard
- `!play <judul lagu>` / `m!p <judul lagu>` — Memutar lagu atau menambahkannya ke antrean pre-fetch serta menyetel patokan *vibes* Autoplay.
- `!queue` — Menampilkan lagu yang sedang diputar, daftar antrean beserta status `SIAP` / `LOADING`, link dashboard, dan menu *dropdown* untuk menghapus lagu dari antrean.
- `!delqueue <nomor / judul>` *(alias: `!hapuslagu`, `!remqueue`, `!hapusantrean`)* — Menghapus lagu dari antrean berdasarkan nomor urut (`!delqueue 2`, `!delqueue 2 3`, `!delqueue 2-4`) atau judul/nama peminta.
- `!autoplay` (`on` / `off`) — Mengaktifkan atau mematikan pemutaran lagu otomatis yang se-vibe saat antrean kosong.
- `!skipmusic` / `!nextsong` — Melewati lagu yang sedang diputar.
- `!stopmusic` — Menghentikan musik, mengosongkan antrean serta *buffer* pre-fetch, dan menjeda Autoplay.
- `!vol <1-100>` — Mengatur volume musik latar di VB-Cable (default `25`).
- `!openoverlay` / `!overlay` — Membuka jendela **Live Stream Dashboard** (tambahkan `chroma` di belakangnya untuk latar hijau).

#### Moderasi & Sistem
- `!target <nama/@username>` — Menampilkan panel tombol moderasi (`Mute 5m`, `Kick`, `Ban Permanen`, `Reset Strike`) untuk penonton tertentu.
- `!kick` / `!muteuser` / `!ban` / `!unban` `<nama/@username>` — Menindak atau memulihkan status penonton secara langsung.
- `!warnlist` / `!clearwarn` — Melihat atau mereset seluruh catatan peringatan (*strike*) penonton.
- `!banlist` — Melihat daftar akun yang berada dalam daftar *permanent ban*.
- `!addbadword <kata>` / `!delbadword <kata>` — Menambah atau menghapus kata dari filter sensor `daftar_badword`.
- `!mute` / `!unmute` — Membisukan atau mengaktifkan kembali suara AI Moderator.
- `!clear` — Membersihkan layar terminal dan menggambar ulang header dashboard utama.
- `!reconnect` — Memulai ulang koneksi ke server TikTok Live dan mereset statistik sesi.
- `!status` / `!panel` — Menampilkan status sistem terkini atau memunculkan ulang panel kontrol tombol.