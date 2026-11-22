# TikTok Live AI Co-Host, Moderator & Autoplay Music Bot

Asisten AI otonom berbasis Python yang berperan sebagai **Co-Host**, **Moderator Otomatis**, sekaligus **Pemutar Musik Request & Autoplay** untuk siaran langsung (Live) TikTok. Bot ini berjalan secara lokal di PC/Laptop, terhubung ke Discord sebagai pusat kendali host, dilengkapi **Live Queue Overlay (SSE Real-Time)**, dan menyalurkan output suara maupun musik langsung ke **TikTok Live Studio / OBS** melalui **VB-Audio Virtual Cable**.

---

## Fitur Utama

- **AI Co-Host Interaktif:** Membaca komentar, menyambut penonton baru (*follow*, *share*, *like*, dan *gift*), serta menjawab pertanyaan penonton secara natural menggunakan suara neural Indonesia (`edge-tts` dengan cadangan `gTTS`).
- **Moderasi Otomatis 3-Strike & Auto-Enforce Ban:** Mendeteksi kata kasar (*badword*) secara instan, memberikan peringatan suara bertingkat (Strike 1 hingga 3), serta mengeksekusi **Mute**, **Kick**, atau **Permanent Ban** secara otomatis maupun manual.
- **Kontrol Penuh via Discord:** Dilengkapi panel tombol interaktif, pengiriman *Voice Note* (otomatis ditranskrip dan dibacakan AI ke *live*), sistem eskalasi pertanyaan penting ke host, serta fitur *self-learning* untuk menyimpan memori permanen maupun sementara.
- **Request Musik, Manajemen Antrean & Vibe Autoplay:** Penonton dapat me-request lagu langsung dari kolom komentar TikTok. Jika antrean kosong, AI otomatis memilihkan dan memutar lagu berikutnya yang memiliki *vibe* dan genre senada dengan lagu terakhir—dan langsung memotong lagu Autoplay begitu ada request baru masuk.
- **Live Queue Overlay (SSE Real-Time):** Menyediakan tampilan antrean musik *real-time* di `[http://127.0.0.1:8765](http://127.0.0.1:8765)` (mendukung mode *Chroma Key* `/?chroma=1`, tombol hapus antrean cepat, serta sinkronisasi otomatis ke file `live_music_queue.txt`).

---

## Cara Kerja Sistem

```text
[TikTok Live Chat & Events] ──► [Python Engine (main.py)] ◄──► [Discord Control Panel]
                                   │        │        │
        (Suara AI TTS)             │        │        │   (Musik YouTube via yt-dlp + FFmpeg)
                                   │        ▼        ▼
                                   │   [CABLE Input (VB-Virtual Cable)]
                                   │        │
                                   │        ▼
                                   │   [CABLE Output (Recording)]
                                   │      ├──► TikTok Live Studio / OBS (Masuk ke Stream)
                                   │      └──► Headset Host (Via "Listen to this device")
                                   │
                                   └──► [Local Overlay Server :8765] ──► OBS / Window Capture
```

1. **Penangkapan Event:** Library `TikTokLive` memantau komentar, gift, follow, like, dan share secara *real-time*.
2. **Pemrosesan & Keputusan AI:** Komentar terlebih dahulu melewati filter *badword* lokal dan pencocokan memori permanen (`dataset.json`). Jika lolos, komentar diproses oleh **Google Gemini** (`gemini-3.5-flash-lite` / `gemini-3.8-flash`, dengan **OpenRouter** sebagai cadangan) untuk menentukan apakah harus dijawab (`CHAT_AI`), diapresiasi (`HYPE`), ditegur (`WARN`), atau dilaporkan ke host (`ESCALATE`).
3. **Mixing Audio Lokal:** Suara AI diputar pada saluran mixer utama, sementara musik dari YouTube di-stream melalui `yt-dlp` dan `FFmpeg` pada saluran terpisah (`Channel 1`). Keduanya digabung dan dikirim ke perangkat virtual **CABLE Input**.
4. **Penyiaran, Overlay & Pemantauan:** Aplikasi *streaming* (TikTok Live Studio / OBS) menangkap **CABLE Output** sebagai sumber audio tambahan dan menampilkan status antrean lagu secara langsung dari server overlay lokal.

---

## Persyaratan Sistem (Requirements)

### 1. Spesifikasi Hardware (PC / Laptop)
- **Sistem Operasi:** Windows 10 atau Windows 11 (64-bit). *(Wajib Windows karena konfigurasi device audio SDL2 dan VB-Cable menggunakan driver audio Windows).*
- **Prosesor (CPU):** Minimum Intel Core i3 Gen-8 / AMD Ryzen 3. *(Disarankan **Core i5 / Ryzen 5** ke atas agar proses encoding video live dan konversi audio FFmpeg berjalan mulus tanpa patah-patah).*
- **Memori (RAM):** Minimum **8 GB** *(Disarankan **16 GB** jika menjalankan TikTok Live Studio, Python, dan browser secara bersamaan).*
- **Penyimpanan:** Minimal 2 GB ruang kosong untuk dependensi dan *buffer* sementara audio.
- **Koneksi Internet:** Stabil minimal **10 Mbps (Upload)** untuk menjaga latensi API AI, streaming audio YouTube, dan kualitas siaran TikTok.

### 2. Software & Akun Pendukung
- **Python 3.10+** *(Disarankan Python 3.11 – 3.14)*.
- **VB-Audio Virtual Cable** ([Download di vb-audio.com](https://vb-audio.com/Cable/)).
- **FFmpeg** *(Dapat diinstal ke System PATH Windows, ditaruh sebagai `ffmpeg.exe` di folder project, atau otomatis menggunakan paket `imageio-ffmpeg` di `requirements.txt`)*.
- **TikTok Live Studio** atau **OBS Studio**.
- **Akun & Bot Discord** untuk panel kontrol host.
- **Google Gemini API Key** (wajib) dan **OpenRouter API Key** (opsional sebagai AI cadangan).

---

## Langkah Instalasi & Konfigurasi

### Langkah 1: Clone Repository & Install Library Python
Buka terminal (CMD / PowerShell) di folder project, lalu jalankan perintah berikut:

```bash
git clone https://github.com/hanipubaidur/tiktok-ai-moderator
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
Ubah nama (*rename*) file **`.env.example`** menjadi **`.env`**, lalu isi setiap variabel di dalamnya mengikuti panduan di bawah ini:

#### A. Mendapatkan `GEMINI_API_KEY` (Wajib) & `OPENROUTER_API_KEY` (Opsional)
1. **Gemini API Key:** Buka [Google AI Studio](https://aistudio.google.com/apikey), login menggunakan akun Google, klik **Create API key**, lalu salin kodenya ke variabel `GEMINI_API_KEY`.
2. **OpenRouter API Key (Cadangan):** Buka [OpenRouter Keys](https://openrouter.ai/keys), login, klik **Create Key**, lalu salin ke `OPENROUTER_API_KEY` (boleh dikosongkan jika tidak dipakai).

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

#### D. Mendapatkan `TIKTOK_SESSION_ID` & `TIKTOK_TARGET_IDC` (Opsional — Untuk Auto Kick/Mute/Ban)
> Jika dikosongkan, bot tetap berjalan normal tetapi tindakan *kick/ban* hanya akan memunculkan tombol peringatan di Discord untuk kamu klik manual di layar live.

1. Buka browser (Chrome / Edge) di PC, kunjungi [tiktok.com](https://www.tiktok.com), dan **login menggunakan akun TikTok yang dipakai untuk Live**.
2. Tekan tombol **`F12`** (atau klik kanan di halaman $\rightarrow$ **Inspect**) untuk membuka *Developer Tools*.
3. Klik tab **Application** di bagian atas *(jika tidak terlihat, klik ikon panah `>>` di sebelah tab Console/Sources)*.
4. Pada panel sebelah kiri, buka menu **Cookies** $\rightarrow$ klik **`[https://www.tiktok.com](https://www.tiktok.com)`**.
5. Cari baris bernama **`sessionid`** pada tabel di sebelah kanan, klik ganda pada kolom **Value**-nya, lalu salin seluruh teks acak tersebut ke variabel `TIKTOK_SESSION_ID`.
6. *(Opsional)* Cari juga baris **`tt-target-idc`** (misalnya `alisg`) dan salin ke variabel `TIKTOK_TARGET_IDC`.

### Langkah 4: Perbarui `dataset.json` & Prompt AI (Sesuai Channel Kamu)
Repository ini sudah menyertakan file **`dataset.json`** bawaan. Sebelum menjalankan bot, buka dan sesuaikan isinya agar cocok dengan profil siaran kamu:

- **`identitas_ai`**: Ubah `nama`, `kepribadian`, dan `gaya_komunikasi` sesuai karakter AI yang kamu inginkan.
- **`profil_host`**: Ganti `nama_panggilan`, `domisili`, dan `minat_topik` dengan data kamu sendiri.
- **`konteks_live`**: Sesuaikan `topik_bahasan` dan `status_terbaru` *(bisa juga diubah langsung saat live via perintah `!topik` dan `!konteks` di Discord)*.
- **`protokol_moderasi`**: Atur `tindakan_otomatis_strike_3` (`"kick"`, `"mute"`, atau `"ban"`).
- **`daftar_badword` & `faq`**: Tambahkan atau hapus daftar kata terlarang serta pertanyaan yang sering ditanyakan penonton beserta jawabannya.

> **Kustomisasi Prompt & Suara di `main.py` (Opsional):**
> * Jika kamu mengganti nama AI di `dataset.json`, sesuaikan juga kalimat pembuka prompt di dalam fungsi `run_moderator_reasoning()` pada file `main.py` (baris `Kamu adalah Mina...`).
> * Untuk mengganti jenis suara TTS, ubah variabel `NEURAL_VOICE` di bagian atas `main.py` (`"id-ID-GadisNeural"` untuk suara wanita atau `"id-ID-ArdiNeural"` untuk suara pria).

### Langkah 5: Pengaturan Routing Audio & Overlay (Windows & Aplikasi Live)
1. **Windows Default Audio:** Pastikan perangkat *Default Playback* Windows tetap mengarah ke **Headset / Speaker** utama kamu (bukan ke *CABLE Input*) agar suara notifikasi Windows atau Discord tidak ikut masuk ke siaran.
2. **Mendengar Suara AI & Musik di Headset:**
   - Tekan `Win + R`, ketik `mmsys.cpl`, lalu tekan `Enter`.
   - Buka tab **Recording** $\rightarrow$ klik kanan **CABLE Output** $\rightarrow$ **Properties**.
   - Buka tab **Listen** $\rightarrow$ centang **Listen to this device** $\rightarrow$ pilih perangkat **Headset** kamu pada menu dropdown $\rightarrow$ klik **Apply** & **OK**.
3. **Sumber Audio di TikTok Live Studio / OBS Studio:**
   - Tambahkan sumber audio baru (*Audio Input Capture* / *Microphone*).
   - Pilih perangkat **CABLE Output (VB-Audio Virtual Cable)**.
4. **Live Queue Overlay (Opsional):**
   - **Untuk OBS Studio:** Tambahkan **Browser Source** dan arahkan ke `[http://127.0.0.1:8765](http://127.0.0.1:8765)` (ukuran disarankan `460 x 540`).
   - **Untuk TikTok Live Studio:** Jalankan `!openoverlay chroma` di Discord untuk membuka jendela overlay terpisah berlatar hijau, tangkap menggunakan **Window Capture** + aktifkan **Chroma Key**, dan pastikan jendela tersebut **tidak di-minimize** agar pembaruan *real-time* tetap berjalan.

### Langkah 6: Menjalankan Bot
Mulai siaran langsung di TikTok terlebih dahulu, lalu jalankan bot melalui terminal:

```bash
python main.py
```

---

## Panduan Perintah (Commands)

### Perintah Penonton (Komentar TikTok Live)
- `!play <judul lagu>` / `req <judul lagu>` / `m!p <judul lagu>` — Menambahkan lagu ke antrean pemutar musik dan memperbarui patokan *vibes* Autoplay.

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

#### Pemutar Musik & Live Overlay
- `!play <judul lagu>` / `m!p <judul lagu>` — Memutar lagu atau menambahkannya ke antrean serta menyetel patokan *vibes* Autoplay.
- `!queue` — Menampilkan lagu yang sedang diputar, daftar antrean, link overlay, dan menu *dropdown* untuk menghapus lagu dari antrean.
- `!delqueue <nomor / judul>` *(alias: `!hapuslagu`, `!remqueue`, `!hapusantrean`)* — Menghapus lagu dari antrean berdasarkan nomor urut (`!delqueue 2`, `!delqueue 2 3`, `!delqueue 2-4`) atau judul/nama peminta.
- `!autoplay` (`on` / `off`) — Mengaktifkan atau mematikan pemutaran lagu otomatis yang se-vibe saat antrean kosong.
- `!skipmusic` / `!nextsong` — Melewati lagu yang sedang diputar.
- `!stopmusic` — Menghentikan musik, mengosongkan antrean, dan menjeda Autoplay.
- `!vol <1-100>` — Mengatur volume musik latar di VB-Cable (default `25`).
- `!openoverlay` / `!overlay` — Membuka jendela overlay antrean musik (tambahkan `chroma` di belakangnya untuk latar hijau).

#### Moderasi & Sistem
- `!target <nama/@username>` — Menampilkan panel tombol moderasi (`Mute 5m`, `Kick`, `Ban Permanen`, `Reset Strike`) untuk penonton tertentu.
- `!kick` / `!muteuser` / `!ban` / `!unban` `<nama/@username>` — Menindak atau memulihkan status penonton secara langsung.
- `!warnlist` / `!clearwarn` — Melihat atau mereset seluruh catatan peringatan (*strike*) penonton.
- `!banlist` — Melihat daftar akun yang berada dalam daftar *permanent ban*.
- `!addbadword <kata>` / `!delbadword <kata>` — Menambah atau menghapus kata dari filter sensor `daftar_badword`.
- `!mute` / `!unmute` — Membisukan atau mengaktifkan kembali suara AI Moderator.
- `!status` / `!panel` — Menampilkan status sistem terkini atau memunculkan ulang panel kontrol tombol.