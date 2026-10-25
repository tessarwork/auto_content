from __future__ import annotations

import asyncio
import concurrent.futures
import edge_tts
import io
import json
from os.path import exists
import queue
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, jsonify, request, send_file
from dotenv import load_dotenv
from PIL import Image

from tools.google_credentials import get_genai_client
from tools.audio.gemini_tts import GeminiTTS
from tools.graphics.google_imagen import GoogleImagen
from tools.graphics.pixabay_image import PixabayImage
from tools.graphics.diagram_gen import DiagramGen
from tools.video.video_compose import VideoCompose

load_dotenv()

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
PROJECTS_DIR = BASE_DIR / "projects"

JOBS: dict[str, dict] = {}


def get_audio_duration(audio_path: Path) -> float:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        text=True,
    ).strip()

    return round(float(out), 2)


def run_with_timeout(fn, timeout_sec: float = 15.0):
    """Jalankan fungsi di daemon thread dan cut seketika jika melebihi timeout tanpa blocking."""
    q = queue.Queue()

    def worker():
        try:
            res = fn()
            q.put((True, res))
        except Exception as exc:
            q.put((False, exc))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    try:
        success, val = q.get(timeout=timeout_sec)
        if success:
            return val
        raise val
    except queue.Empty:
        raise TimeoutError(f"Proses melebihi batas waktu {timeout_sec} detik.")


def check_model_health_from_google(model_name: str, timeout_sec: float = 8.0) -> dict:
    """Query langsung ke endpoint resmi Google Models API (client.models.get) untuk status asli dari server Google."""
    print(
        f"\n[GOOGLE HEALTH CHECK] Mengambil status resmi model '{model_name}' langsung dari server Google..."
    )
    client = get_genai_client()
    t0 = time.time()

    try:
        model_info = run_with_timeout(lambda: client.models.get(model=model_name), timeout_sec=timeout_sec)
        latency = round(time.time() - t0, 2)
        res = {
            "healthy": True,
            "name": getattr(model_info, "name", model_name),
            "display_name": getattr(model_info, "display_name", model_name),
            "input_token_limit": getattr(model_info, "input_token_limit", None),
            "output_token_limit": getattr(model_info, "output_token_limit", None),
            "latency_seconds": latency,
        }
        print(f"[GOOGLE SERVER RESPONSE] Status: SEHAT (HTTP 200 OK)")
        print(f"  -> Model Resmi : {res['display_name']} ({res['name']})")
        print(
            f"  -> Token Limit : Input {res['input_token_limit']} | Output {res['output_token_limit']}"
        )
        print(f"  -> Waktu Respon: {latency} detik")
        return res
    except TimeoutError:
        print(
            f"[GOOGLE SERVER RESPONSE] Status: SAKIT / TIMEOUT (Server Google tidak membalas > {timeout_sec}s)"
        )
        print(f"  -> ACTION: LANGSUNG DI-CUT! Tidak melanjutkan eksekusi.")
        return {
            "healthy": False,
            "error": f"Server Google tidak merespons melebihi {timeout_sec} detik",
        }
    except Exception as exc:
        print(
            f"[GOOGLE SERVER RESPONSE] Status: SAKIT / ERROR ({type(exc).__name__})"
        )
        print(f"  -> Pesan Asli Server Google: {exc}")
        print(f"  -> ACTION: LANGSUNG DI-CUT! Tidak melanjutkan eksekusi.")
        return {"healthy": False, "error": str(exc)}


def check_tts_synthesis_health(
    model_name: str = "gemini-3.8-flash-tts",
    voice_id: str = "Kore",
    timeout_sec: float = 8.0,
) -> dict:
    """Test ping sintesis suara nyata ke Google Interactions API tanpa blocking."""
    print(
        f"\n[GOOGLE TTS PROBE] Menguji sintesis suara nyata ke Google Interactions API ('{model_name}')..."
    )
    tts = GeminiTTS()
    t0 = time.time()
    dummy_wav = BASE_DIR / ".tts_probe.wav"

    def _probe():
        return tts.execute(
            {
                "model_id": model_name,
                "voice_id": voice_id,
                "text": "Tes koneksi suara.",
                "output_path": str(dummy_wav),
            }
        )

    try:
        result = run_with_timeout(_probe, timeout_sec=timeout_sec)
        latency = round(time.time() - t0, 2)
        if dummy_wav.exists():
            dummy_wav.unlink(missing_ok=True)
        if result.success:
            print(
                f"[GOOGLE TTS PROBE] Status: SEHAT & BERHASIL SINTESIS (Waktu Respon: {latency}s)"
            )
            return {"healthy": True, "latency_seconds": latency}
        else:
            print(f"[GOOGLE TTS PROBE] Status: SAKIT / REJECTED DARI SERVER GOOGLE")
            print(f"  -> Balasan Error Google: {result.error}")
            return {"healthy": False, "error": result.error}
    except TimeoutError:
        print(
            f"[GOOGLE TTS PROBE] Status: SAKIT / SERVER GOOGLE MACET (> {timeout_sec}s tanpa balasan)"
        )
        return {
            "healthy": False,
            "error": f"Server Google TTS macet > {timeout_sec} detik tanpa balasan",
        }
    except Exception as exc:
        print(f"[GOOGLE TTS PROBE] Status: SAKIT / ERROR ({type(exc).__name__})")
        print(f"  -> Balasan Error Google: {exc}")
        return {"healthy": False, "error": str(exc)}


def generate_plan_with_gemini(topic: str) -> dict:
    text_model = "gemini-3.5-flash-lite"
    status = check_model_health_from_google(text_model, timeout_sec=8.0)
    if not status["healthy"]:
        raise RuntimeError(
            f"Model Text '{text_model}' sedang SAKIT / TIDAK TERSEDIA: {status.get('error')}. Proses langsung di-cut."
        )

    client = get_genai_client()
    prompt = f"""

    Kamu adalah sutradara video pendek vertikal (TikTok/Reels/Shorts) Bahasa Indonesia.
    Buatkan rencana video edukasi durasi ~35-40 detik tentang topik berikut:
    TOPIK: "{topic}"
    
    Kembalikan HANYA JSON valid dengan struktur persis seperti ini:
    {{
      "title": "Judul video singkat & memancing rasa penasaran",
      "caption": "Caption sosial media 2 kalimat untuk TikTok/Reels/Shorts",
      "hashtags": ["#AI", "#FaktaUnik", "#Teknologi", "#Edukasi"],
      "sections": [
        {{"id": "s1", "text": "Kalimat hook pembuka 12-15 kata...", "style": "Curious, punchy Indonesian storyteller"}},
        {{"id": "s2", "text": "Kalimat fakta/studi ilmiah 12-15 kata...", "style": "Authoritative scientific explainer"}},
        {{"id": "s3", "text": "Kalimat penjelasan cara kerja/analogi 12-15 kata...", "style": "Vivid rhythmic analogy"}},
        {{"id": "s4", "text": "Kalimat perbandingan/klimaks 12-15 kata...", "style": "Dramatic contrast"}},
        {{"id": "s5", "text": "Kalimat kesimpulan & pertanyaan penutup 12-15 kata...", "style": "Warm engaging conclusion"}}
      ],
      "image_prompts": {{
        "scene1": "Vertical 9:16 flat vector illustration, dark slate background #0F172A, bold neon purple #7C3AED and cyan #06B6D4 colors, [deskripsi visual pembuka dalam Bahasa Inggris], no text",
        "scene3": "Vertical 9:16 flat vector illustration, dark slate background #0F172A, [deskripsi visual analogi utama dalam Bahasa Inggris], no text",
        "scene5": "Vertical 9:16 flat vector illustration, dark slate background #0F172A, warm golden #F59E0B highlight, [deskripsi visual klimaks/solusi dalam Bahasa Inggris], no text"
      }},
      "stock_queries": {{
        "scene1": "2-3 kata kunci pencarian bahasa Inggris sederhana untuk scene pembuka (misal: cat in box, programming code, brain idea)",
        "scene3": "2-3 kata kunci pencarian bahasa Inggris sederhana untuk scene analogi (misal: chef cooking, computer chip, running track)",
        "scene5": "2-3 kata kunci pencarian bahasa Inggris sederhana untuk scene kesimpulan (misal: happy cat, supercomputer, technology future)"
      }},
      "visual_cards": {{
        "scene1_stat": "KATA KUNCI SINGKAT (maks 8 karakter, dipakai jika fallback)",
        "scene1_subtitle": "Subjudul singkat penjelas hook (maks 10 kata)",
        "scene2_title": "ISTILAH KUNCI (maks 3 kata huruf kapital)",
        "scene2_body": "Ringkasan fakta/riset 1 kalimat (maks 18 kata)",
        "scene3_diagram_title": "JUDUL DIAGRAM ALUR (dipakai jika fallback)",
        "scene3_boxes": ["Tahap 1", "Tahap 2", "Tahap 3"],
        "scene4_title": "JUDUL PERBANDINGAN",
        "scene4_left_label": "Kondisi A (maks 3 kata)",
        "scene4_left_value": "Hasil A (maks 3 kata)",
        "scene4_right_label": "Kondisi B (maks 3 kata)",
        "scene4_right_value": "Hasil B (maks 3 kata)",
        "scene5_diagram_title": "JUDUL DIAGRAM DAMPAK (dipakai jika fallback)",
        "scene5_boxes": ["Sebab", "Proses", "Dampak"],
        "scene6_cta_title": "Pertanyaan singkat ke penonton? (maks 6 kata)",
        "scene6_cta_subtitle": "Ajak penonton tulis di komentar! (maks 7 kata)"
      }}
    }}
    """

    print(f"[Tahap 1] Menyusun naskah via '{text_model}' (timeout 25 detik)...")

    def _call_gemini():
        response = client.models.generate_content(
            model=text_model,
            contents=prompt,
            config={"response_mime_type": "application/json"},
        )
        return json.loads(response.text)

    try:
        plan = run_with_timeout(_call_gemini, timeout_sec=25.0)
        print(f"[Tahap 1] Naskah berhasil disusun via '{text_model}'.")
        return plan
    except TimeoutError:
        print(
            f"[Tahap 1] Pembuatan naskah via '{text_model}' SAKIT / TIMEOUT (> 25s). LANGSUNG CUT!"
        )
        raise TimeoutError(
            f"Pembuatan naskah via '{text_model}' timeout melebihi batas 25 detik. Langsung di-cut."
        )
    except Exception as err:
        print(
            f"[Tahap 1] Pembuatan naskah via '{text_model}' ERROR ({err}). LANGSUNG CUT!"
        )
        raise RuntimeError(f"Pembuatan naskah via '{text_model}' gagal: {err}")


def generate_edge_tts(text: str, output_path: Path, voice: str = "id-ID-GadisNeural") -> bool:
    """Fallback suara via Microsoft Edge Neural TTS jika Gemini TTS sedang macet."""
    async def _run():
        temp_mp3 = output_path.with_suffix(".mp3")
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(temp_mp3))
        # Convert ke WAV 44.1kHz mono PCM
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(temp_mp3), "-ar", "44100", "-ac", "1", str(output_path)],
            check=True,
            capture_output=True,
            timeout=15,
        )
        temp_mp3.unlink(missing_ok=True)
        return output_path.exists()

    try:
        return asyncio.run(_run())
    except Exception as err:
        print(f"[Edge-TTS Error] {err}")
        return False


def fetch_pixabay_vertical_image(query: str, output_path: Path) -> bool:
    """Cari dan download foto/ilustrasi vertikal 9:16 resolusi 1080x1920 dari Pixabay."""
    pix = PixabayImage()
    # 1. Coba foto vertikal terlebih dahulu
    res = pix.execute(
        {
            "query": query,
            "orientation": "vertical",
            "image_type": "photo",
            "output_path": str(output_path),
        }
    )
    # 2. Kalau belum dapat foto vertikal, coba ilustrasi vertikal
    if not res.success or not output_path.exists():
        res = pix.execute(
            {
                "query": query,
                "orientation": "vertical",
                "image_type": "illustration",
                "output_path": str(output_path),
            }
        )
    # 3. Kalau masih belum dapat, coba pencarian umum (all)
    if not res.success or not output_path.exists():
        res = pix.execute(
            {
                "query": query,
                "image_type": "all",
                "output_path": str(output_path),
            }
        )

    if res.success and output_path.exists():
        try:
            # Crop tengah pas 9:16 dan resize ke 1080x1920 Full HD
            im = Image.open(output_path).convert("RGB")
            w, h = im.size
            target_ratio = 9 / 16
            current_ratio = w / h
            if current_ratio > target_ratio:
                new_w = int(h * target_ratio)
                left = (w - new_w) // 2
                im = im.crop((left, 0, left + new_w, h))
            else:
                new_h = int(w / target_ratio)
                top = (h - new_h) // 2
                im = im.crop((0, top, w, top + new_h))
            im_final = im.resize((1080, 1920), Image.Resampling.LANCZOS)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            im_final.save(output_path, format="PNG")
            return True
        except Exception as err:
            print(f"[Pixabay Resize Error] {err}")
            return False
    return False


def generate_visual_assets(
    plan: dict,
    images_dir: Path,
    image_model: str = "gemini-2.5-flash-image",
) -> dict[str, Path | None]:
    imagen = GoogleImagen()
    prompts = plan.get("image_prompts", {})
    stock_queries = plan.get("stock_queries", {})
    cards = plan["visual_cards"]

    generated_paths: dict[str, Path | None] = {
        "scene1": None,
        "scene3": None,
        "scene5": None,
    }

    # --- OPSI 1: GoogleImagen (Jika Akun Gemini Paid Tier) ---
    s1_path = images_dir / "scene-1.png"
    if prompts.get("scene1"):
        res1 = imagen.execute(
            {
                "prompt": prompts["scene1"],
                "model": image_model,
                "aspect_ratio": "9:16",
                "output_path": str(s1_path),
            }
        )
        if res1.success and s1_path.exists():
            generated_paths["scene1"] = s1_path
            for key, filename in (("scene3", "scene-3.png"), ("scene5", "scene-5.png")):
                out_path = images_dir / filename
                res = imagen.execute(
                    {
                        "prompt": prompts[key],
                        "model": image_model,
                        "aspect_ratio": "9:16",
                        "output_path": str(out_path),
                    }
                )
                if res.success and out_path.exists():
                    generated_paths[key] = out_path
            return generated_paths

    # --- OPSI 2: Pixabay API (Stok Foto & Ilustrasi Vertikal HD Gratis) ---
    print("[Visual] Mengambil stok foto/vektor vertikal Full HD via Pixabay API...")
    for key, filename in (
        ("scene1", "scene-1.png"),
        ("scene3", "scene-3.png"),
        ("scene5", "scene-5.png"),
    ):
        out_path = images_dir / filename
        query = stock_queries.get(key) or (prompts.get(key, "technology")[:50])
        ok = fetch_pixabay_vertical_image(query, out_path)
        if ok and out_path.exists():
            print(
                f"  -> Pixabay '{query}' berhasil diunduh & di-resize 1080x1920: {filename}"
            )
            generated_paths[key] = out_path
        else:
            print(
                f"  -> Pixabay '{query}' tidak ditemukan atau gagal, lanjut ke fallback."
            )

    # --- OPSI 3: Fallback ke DiagramGen Lokal jika ada scene visual yang kosong ---
    dg = DiagramGen()
    if generated_paths["scene3"] is None:
        print("[Fallback] Membuat diagram alur untuk Scene 3...")
        d3_path = images_dir / "scene-3-diagram.png"
        dg.execute(
            {
                "diagram_type": "boxes",
                "title": cards["scene3_diagram_title"],
                "theme": "dark",
                "width": 1080,
                "height": 1920,
                "boxes": [
                    {"label": cards["scene3_boxes"][0], "color": "#0891B2"},
                    {"label": cards["scene3_boxes"][1], "color": "#7C3AED"},
                    {"label": cards["scene3_boxes"][2], "color": "#DB2777"},
                ],
                "connections": [{"from": 0, "to": 1}, {"from": 1, "to": 2}],
                "output_path": str(d3_path),
            }
        )
        generated_paths["scene3"] = d3_path

    if generated_paths["scene5"] is None:
        print("[Fallback] Membuat diagram dampak untuk Scene 5...")
        d5_path = images_dir / "scene-5-diagram.png"
        dg.execute(
            {
                "diagram_type": "boxes",
                "title": cards["scene5_diagram_title"],
                "theme": "dark",
                "width": 1080,
                "height": 1920,
                "boxes": [
                    {"label": cards["scene5_boxes"][0], "color": "#7C3AED"},
                    {"label": cards["scene5_boxes"][1], "color": "#DB2777"},
                    {"label": cards["scene5_boxes"][2], "color": "#D97706"},
                ],
                "connections": [{"from": 0, "to": 1}, {"from": 1, "to": 2}],
                "output_path": str(d5_path),
            }
        )
        generated_paths["scene5"] = d5_path

    return generated_paths


def run_video_pipeline(
    job_id: str,
    topic: str,
    voice_id: str = "Kore",
    image_model: str = "gemini-2.5-flash-image",
):
    try:
        JOBS[job_id]["status"] = "running"
        JOBS[job_id]["step"] = (
            "1/4 - Menyusun naskah & prompt gambar via Gemini 2.5 Flash..."
        )

        slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:30]
        project_id = f"{slug}-{job_id[:6]}"
        project_dir = PROJECTS_DIR / project_id
        audio_dir = project_dir / "assets" / "audio"
        images_dir = project_dir / "assets" / "images"
        music_dir = project_dir / "assets" / "music"
        renders_dir = project_dir / "renders"
        artifacts_dir = project_dir / "artifacts"

        for d in (audio_dir, images_dir, music_dir, renders_dir, artifacts_dir):
            d.mkdir(parents=True, exist_ok=True)

        # TAHAP 1: Buat Naskah & Prompt Gambar dari Topik
        plan = generate_plan_with_gemini(topic)
        (artifacts_dir / "auto_plan.json").write_text(
            json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # TAHAP 2A: Generate 5 Suara Voiceover (Gemini TTS dengan Auto-Fallback ke Edge-TTS)
        JOBS[job_id]["step"] = "2/4 - Membuat 5 file voiceover..."
        tts_model = "gemini-3.8-flash-tts"
        use_edge_fallback = False

        tts_meta = check_model_health_from_google(tts_model, timeout_sec=8.0)
        if not tts_meta["healthy"]:
            print(f"[TTS FALLBACK] Model '{tts_model}' tidak sehat di Google ({tts_meta.get('error')}). Beralih ke edge-tts (id-ID-GadisNeural)...")
            use_edge_fallback = True
        else:
            tts_probe = check_tts_synthesis_health(tts_model, voice_id=voice_id, timeout_sec=8.0)
            if not tts_probe["healthy"]:
                print(f"[TTS FALLBACK] Probe sintesis Google TTS macet > 8s. Otomatis beralih ke edge-tts (id-ID-GadisNeural)...")
                use_edge_fallback = True

        tts = GeminiTTS()
        section_timings = []
        current_time = 0.0
        wav_paths = []

        for idx, sec in enumerate(plan["sections"]):
            sec_id = sec["id"]
            wav_path = audio_dir / f"{sec_id}.wav"
            tts_ok = False

            if not use_edge_fallback:
                print(f"[TTS {sec_id}] Request suara via Gemini TTS (timeout 15 detik)...")

                def _gen_tts(s_sec=sec, s_path=wav_path):
                    return tts.execute(
                        {
                            "model_id": tts_model,
                            "voice_id": voice_id,
                            "text": s_sec["text"],
                            "style": s_sec.get("style", "Energetic Indonesian narrator"),
                            "output_path": str(s_path),
                        }
                    )

                try:
                    res = run_with_timeout(_gen_tts, timeout_sec=15.0)
                    if res.success and wav_path.exists():
                        tts_ok = True
                        print(f"[TTS {sec_id}] Gemini TTS SEHAT & Sukses.")
                    else:
                        print(f"[TTS {sec_id}] Gemini TTS ditolak Google ({res.error}). Fallback ke edge-tts...")
                except TimeoutError:
                    print(f"[TTS {sec_id}] Gemini TTS SAKIT / TIMEOUT (> 15s)! Fallback ke edge-tts...")
                except Exception as exc:
                    print(f"[TTS {sec_id}] Gemini TTS ERROR ({exc})! Fallback ke edge-tts...")

            # Jika Gemini TTS gagal atau sejak awal sedang sakit -> gunakan edge-tts
            if not tts_ok:
                print(f"[TTS {sec_id}] Membuat suara via edge-tts (id-ID-GadisNeural)...")
                edge_ok = generate_edge_tts(sec["text"], wav_path, voice="id-ID-GadisNeural")
                if not edge_ok or not wav_path.exists():
                    raise RuntimeError(f"Gagal membuat audio pada {sec_id} baik via Gemini TTS maupun edge-tts.")
                print(f"[TTS {sec_id}] Edge-TTS Sukses.")

            dur = get_audio_duration(wav_path)
            section_timings.append(
                (
                    sec_id,
                    sec["text"],
                    round(current_time, 2),
                    round(current_time + dur, 2),
                )
            )
            current_time += dur
            wav_paths.append(wav_path)
            if idx < len(plan["sections"]) - 1:
                time.sleep(2)

        total_narration_dur = round(current_time, 2)

        # TAHAP 2B: Generate 3 Gambar AI via GoogleImagen (+ Fallback)
        JOBS[job_id]["step"] = (
            "2b/4 - Membuat gambar ilustrasi 9:16 via Gemini Image..."
        )
        visual_paths = generate_visual_assets(plan, images_dir, image_model=image_model)

        # TAHAP 2C: Buat Musik Latar & Gabungkan Audio Master
        total_video_dur = round(total_narration_dur + 1.0, 2)
        bgm_path = music_dir / "background_music.mp3"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                (
                    f"aevalsrc='0.08*sin(2*PI*110*t)+0.05*sin(2*PI*164.81*t)+0.04*sin(2*PI*220*t)'"
                    f":s=44100:d={int(total_video_dur) + 2}"
                ),
                "-af",
                f"lowpass=f=900,afade=t=in:st=0:d=1.5,afade=t=out:st={total_narration_dur}:d=2",
                "-c:a",
                "libmp3lame",
                "-q:a",
                "4",
                str(bgm_path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )

        concat_wav = audio_dir / "narration_full.wav"
        inputs_ff = []
        for p in wav_paths:
            inputs_ff.extend(["-i", str(p)])
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                *inputs_ff,
                "-filter_complex",
                "[0:a][1:a][2:a][3:a][4:a]concat=n=5:v=0:a=1,apad=pad_dur=1.0[aout]",
                "-map",
                "[aout]",
                "-t",
                str(total_video_dur),
                str(concat_wav),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )

        mixed_master = audio_dir / "mixed_master.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(concat_wav),
                "-i",
                str(bgm_path),
                "-filter_complex",
                "[0:a]volume=1.0[n];[1:a]volume=0.10[m];[n][m]amix=inputs=2:duration=first[out]",
                "-map",
                "[out]",
                "-ac",
                "2",
                "-ar",
                "44100",
                str(mixed_master),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )

        # TAHAP 3: Susun Timeline Cuts & Subtitle Per Kata
        JOBS[job_id]["step"] = "3/4 - Menyusun timeline & subtitle per kata..."
        remotion_captions = []
        for _, text, s_start, s_end in section_timings:
            words = text.split()
            usable = max(0.5, (s_end - s_start) - 0.2)
            step = usable / max(1, len(words))
            for i, w in enumerate(words):
                w_start = s_start + 0.1 + i * step
                w_end = min(s_end - 0.05, w_start + step * 0.92)
                remotion_captions.append(
                    {
                        "word": w,
                        "startMs": int(round(w_start * 1000)),
                        "endMs": int(round(w_end * 1000)),
                    }
                )

        cards = plan["visual_cards"]
        s5_start, s5_end = section_timings[4][2], section_timings[4][3]
        s5_mid = round((s5_start + s5_end) / 2, 2)

        if visual_paths["scene1"] is not None:
            cut_1 = {
                "id": "cut-1",
                "source": str(visual_paths["scene1"].resolve()),
                "in_seconds": section_timings[0][2],
                "out_seconds": section_timings[0][3],
                "animation": "zoom-in",
                "backgroundColor": "#0F172A",
            }
        else:
            cut_1 = {
                "id": "cut-1",
                "type": "stat_card",
                "source": "",
                "in_seconds": section_timings[0][2],
                "out_seconds": section_timings[0][3],
                "stat": cards["scene1_stat"],
                "subtitle": cards["scene1_subtitle"],
                "accentColor": "#7C3AED",
                "backgroundColor": "#0F172A",
            }

        edit_decisions = {
            "version": "1.0",
            "renderer_family": "explainer-data",
            "render_runtime": "remotion",
            "composition_mode": "templated",
            "playbook": "flat-motion-graphics",
            "captions": remotion_captions,
            "cuts": [
                cut_1,
                {
                    "id": "cut-2",
                    "type": "callout",
                    "source": "",
                    "in_seconds": section_timings[1][2],
                    "out_seconds": section_timings[1][3],
                    "callout_type": "warning",
                    "title": cards["scene2_title"],
                    "text": cards["scene2_body"],
                    "accentColor": "#EC4899",
                    "backgroundColor": "#1E293B",
                },
                {
                    "id": "cut-3",
                    "source": str(visual_paths["scene3"].resolve()),
                    "in_seconds": section_timings[2][2],
                    "out_seconds": section_timings[2][3],
                    "animation": "ken-burns-slow-zoom",
                    "backgroundColor": "#0F172A",
                },
                {
                    "id": "cut-4",
                    "type": "comparison",
                    "source": "",
                    "in_seconds": section_timings[3][2],
                    "out_seconds": section_timings[3][3],
                    "title": cards["scene4_title"],
                    "leftLabel": cards["scene4_left_label"],
                    "leftValue": cards["scene4_left_value"],
                    "rightLabel": cards["scene4_right_label"],
                    "rightValue": cards["scene4_right_value"],
                    "backgroundColor": "#0F172A",
                    "cardBackgroundColor": "#1E293B",
                },
                {
                    "id": "cut-5",
                    "source": str(visual_paths["scene5"].resolve()),
                    "in_seconds": s5_start,
                    "out_seconds": s5_mid,
                    "animation": "zoom-in",
                    "backgroundColor": "#0F172A",
                },
                {
                    "id": "cut-6",
                    "type": "hero_title",
                    "source": "",
                    "in_seconds": s5_mid,
                    "out_seconds": s5_end,
                    "text": cards["scene6_cta_title"],
                    "heroSubtitle": cards["scene6_cta_subtitle"],
                    "accentColor": "#22D3EE",
                    "backgroundColor": "#0F172A",
                },
            ],
            "metadata": {
                "playbook": "flat-motion-graphics",
                "target_duration_seconds": total_narration_dur,
                "proposal_render_runtime": "remotion",
            },
        }

        asset_manifest = {
            "version": "1.0",
            "assets": [
                {
                    "id": f"vis-{k}",
                    "type": "image",
                    "path": str(v),
                    "source_tool": "google_imagen",
                    "scene_id": k,
                }
                for k, v in visual_paths.items()
                if v is not None
            ],
        }

        # TAHAP 4: Render Video MP4 via Remotion (Lokal)
        JOBS[job_id]["step"] = "4/4 - Merender video 9:16 di Remotion (~4-5 menit)..."
        output_mp4 = renders_dir / "final.mp4"
        vc = VideoCompose()
        print("[Tahap 4] Merender video 9:16 di Remotion secara lokal...")

        render_res = vc.execute(
            {
                "operation": "render",
                "edit_decisions": edit_decisions,
                "asset_manifest": asset_manifest,
                "profile": "tiktok",
                "output_path": str(output_mp4),
                "audio_path": str(mixed_master),
            }
        )

        if not render_res.success:
            raise RuntimeError(f"Remotion render gagal: {render_res.error}")

        JOBS[job_id].update(
            {
                "status": "completed",
                "step": "Selesai!",
                "project_id": project_id,
                "visual_mode": "pixabay"
                if visual_paths["scene1"]
                else "remotion_fallback",
                "title": plan["title"],
                "caption": plan["caption"],
                "hashtags": plan["hashtags"],
                "video_path": str(output_mp4.resolve()),
                "download_url": f"http://127.0.0.1:5005/api/download/{job_id}",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    except Exception as exc:
        JOBS[job_id].update(
            {
                "status": "failed",
                "error": str(exc),
            }
        )


@app.route("/api/generate", methods=["POST"])
def start_generation():
    data = request.get_json(force=True) or {}
    topic = (data.get("topic") or "").strip()
    voice_id = data.get("voice_id", "Kore")
    image_model = data.get("image_model", "gemini-2.5-flash-image")
    if not topic:
        return jsonify({"error": "Field 'topic' wajib diisi"}), 400

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "job_id": job_id,
        "topic": topic,
        "status": "queued",
        "step": "Menunggu antrean...",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    thread = threading.Thread(
        target=run_video_pipeline,
        args=(job_id, topic, voice_id, image_model),
        daemon=True,
    )
    thread.start()

    return jsonify(
        {
            "message": "Produksi video dimulai di background",
            "job_id": job_id,
            "status_url": f"http://127.0.0.1:5005/api/status/{job_id}",
        }
    ), 202


@app.route("/api/status/<job_id>", methods=["GET"])
def check_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan"}), 404
    return jsonify(job)


@app.route("/api/download/<job_id>", methods=["GET"])
def download_video(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "completed":
        return jsonify({"error": "Video belum selesai atau job tidak ditemukan"}), 404
    return send_file(job["video_path"], mimetype="video/mp4", as_attachment=True)


if __name__ == "__main__":
    print("🚀 OpenMontage Gemini Automation API berjalan di http://127.0.0.1:5005")
    app.run(host="0.0.0.0", port=5005, debug=False)
