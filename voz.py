# -*- coding: utf-8 -*-
"""
GUI: Reconocimiento (Vosk, offline) -> Texto -> TTS (edge-tts, gratis)
+ Apartado adicional de Texto->Voz con guardado a MP3 de alta calidad

- Botón para elegir carpeta del modelo Vosk
- Listas de dispositivos de entrada y salida
- Selección de voz y parámetros (rate/volume/gain)
- Transcripción en vivo, cambio de voz con ducking (pausa input al reproducir)
- Nuevo panel: escribir texto, reproducir y guardar MP3 normalizado a 192 kbps
- Manejo de errores, logs en pantalla y archivo
- Requiere: vosk, sounddevice, numpy, edge-tts, pydub, ttkbootstrap, FFmpeg en PATH
"""

import os
import sys
import json
import time
import uuid
import queue
import asyncio
import logging
import threading
import shutil
from datetime import datetime

import numpy as np
import sounddevice as sd
from pydub import AudioSegment
from pydub.effects import normalize  # normalización de loudness

from vosk import Model, KaldiRecognizer
import edge_tts

import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

# UI más bonita
try:
    import ttkbootstrap as tb
    from ttkbootstrap.constants import PRIMARY, SUCCESS, DANGER, WARNING, INFO
    USE_TTKB = True
except Exception:
    import tkinter.ttk as ttk
    USE_TTKB = False


# ======================= UTILIDADES =======================
def ffmpeg_ok() -> bool:
    return shutil.which("ffmpeg") is not None

def normalizar_float32_a_int16(x: np.ndarray) -> bytes:
    x = np.clip(x, -1.0, 1.0)
    return np.int16(x * 32767).tobytes()

def rms(arr: np.ndarray) -> float:
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(arr), dtype=np.float64)))


# ======================= MOTOR DE AUDIO =======================
class VoiceChangerEngine:
    def __init__(self, ui_logger, on_partial, on_final, on_status):
        self.ui_log = ui_logger
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_status = on_status

        self.model = None
        self.recognizer = None
        self.sample_rate = 16000
        self.channels = 1
        self.input_device = None
        self.output_device = None

        self.voice = "es-ES-ElviraNeural"
        self.tts_rate = "+0%"
        self.tts_volume = "+0%"
        self.playback_gain_db = 0.0

        self.keep_mp3 = False
        self.save_wav = True
        self.cache_dir = "tts_cache"
        os.makedirs(self.cache_dir, exist_ok=True)

        self.enable_noise_gate = True
        self.rms_gate_threshold = 0.005

        self.ducking = True  # pausar input mientras reproduce TTS

        self.audio_q = queue.Queue(maxsize=80)
        self.phrase_q = queue.Queue()
        self.stop_event = threading.Event()
        self.stream_in = None

        self.t_asr = None
        self.t_tts = None

        # control de parciales/silencio
        self._last_partial = ""
        self._last_partial_time = 0.0
        self._last_activity_time = time.time()
        self._speech_seen_this_segment = False

        # parámetros fraseo
        self.min_phrase_len = 6
        self.print_partials = True
        self.silence_finalize_sec = 0.8
        self.partial_throttle_sec = 0.25

        # reintentos tts
        self.tts_max_retries = 3
        self.tts_backoff = 1.0

        self.ffmpeg_available = ffmpeg_ok()

    # ---------- carga modelo ----------
    def load_model(self, model_dir: str):
        if not os.path.isdir(model_dir):
            raise RuntimeError(f"No existe la carpeta del modelo: {model_dir}")
        # validación mínima
        expected = ["am", "conf", "graph"]
        if not any(os.path.exists(os.path.join(model_dir, e)) for e in expected):
            self.ui_log(f"[AVISO] La carpeta seleccionada no parece ser un modelo Vosk: {model_dir}")
        self.ui_log("[INFO] Cargando modelo Vosk...")
        self.model = Model(model_dir)
        self.recognizer = KaldiRecognizer(self.model, self.sample_rate)
        self.recognizer.SetWords(True)
        self.ui_log("[OK] Modelo Vosk cargado.")

    # ---------- captura ----------
    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            self.ui_log(f"[AUDIO] {status}")
        if self.enable_noise_gate and rms(indata.ravel()) < self.rms_gate_threshold:
            return
        self.audio_q.put(indata.copy())

    def start_input(self):
        sd.default.samplerate = self.sample_rate
        sd.default.channels = self.channels
        if self.input_device is not None or self.output_device is not None:
            sd.default.device = (self.input_device, self.output_device)
        self.stream_in = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="float32",
            callback=self._audio_callback,
            blocksize=0
        )
        self.stream_in.start()
        self.on_status("🎙️ Grabando…")
        self.ui_log("[OK] Captura iniciada.")

    def stop_input(self):
        if self.stream_in:
            try:
                self.stream_in.stop()
                self.stream_in.close()
            except Exception:
                pass
            self.stream_in = None
            self.ui_log("[OK] Captura detenida.")
            self.on_status("⏸️ Micrófono detenido")

    # ---------- asr ----------
    def asr_worker(self):
        self.ui_log("[OK] Hilo ASR iniciado.")
        while not self.stop_event.is_set():
            try:
                inbuf = self.audio_q.get(timeout=0.2)
            except queue.Empty:
                self._finalize_by_silence()
                continue

            inbytes = normalizar_float32_a_int16(inbuf.ravel())
            if self.recognizer.AcceptWaveform(inbytes):
                res = json.loads(self.recognizer.Result())
                text = (res.get("text") or "").strip()
                if text:
                    self._emit_phrase(text)
                self._reset_partials()
            else:
                partial = json.loads(self.recognizer.PartialResult()).get("partial", "")
                now = time.time()
                if partial:
                    self._speech_seen_this_segment = True
                    self._last_activity_time = now
                    if self.print_partials and partial != self._last_partial and (now - self._last_partial_time) >= self.partial_throttle_sec:
                        self.on_partial(partial)
                        self._last_partial = partial
                        self._last_partial_time = now
                else:
                    self._finalize_by_silence()

        fin = json.loads(self.recognizer.FinalResult()).get("text", "").strip()
        if fin:
            self._emit_phrase(fin)
        self.ui_log("[OK] Hilo ASR detenido.")

    def _reset_partials(self):
        self._last_partial = ""
        self._speech_seen_this_segment = False
        self._last_activity_time = time.time()

    def _finalize_by_silence(self):
        if not self._speech_seen_this_segment:
            return
        if time.time() - self._last_activity_time >= self.silence_finalize_sec:
            fin = json.loads(self.recognizer.FinalResult()).get("text", "").strip()
            if fin:
                self._emit_phrase(fin)
            self._reset_partials()

    def _emit_phrase(self, text: str):
        if len(text) < self.min_phrase_len:
            return
        self.on_final(text)
        self.phrase_q.put(text)

    # ---------- tts (compartido con modulador y panel TTS) ----------
    async def _edge_to_mp3(self, text, mp3_path, voice=None, rate=None, vol=None):
        v = voice or self.voice
        r = rate if rate is not None else self.tts_rate
        vl = vol if vol is not None else self.tts_volume
        comm = edge_tts.Communicate(text, v, rate=r, volume=vl)
        await comm.save(mp3_path)

    def _tts_with_retries(self, text, mp3_path, voice=None, rate=None, vol=None):
        for n in range(1, self.tts_max_retries + 1):
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self._edge_to_mp3(text, mp3_path, voice, rate, vol))
                loop.close()
                return
            except Exception as e:
                self.ui_log(f"[TTS] Reintento {n}: {e}")
                time.sleep(self.tts_backoff * n)
        raise RuntimeError("TTS agotó reintentos")

    def _play_segment(self, seg: AudioSegment):
        if self.playback_gain_db != 0:
            seg = seg + self.playback_gain_db
        seg = seg.set_frame_rate(self.sample_rate).set_channels(1).set_sample_width(2)
        samples = np.array(seg.get_array_of_samples())
        try:
            sd.play(samples, samplerate=seg.frame_rate, blocking=True, device=self.output_device)
        finally:
            sd.stop()

    def tts_worker(self):
        self.ui_log("[OK] Hilo TTS iniciado.")
        while not self.stop_event.is_set():
            try:
                text = self.phrase_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if not text:
                continue

            # ducking
            if self.ducking:
                self.stop_input()

            try:
                uid = uuid.uuid4().hex[:6]
                base = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{uid}"
                mp3_path = os.path.join(self.cache_dir, base + ".mp3")
                wav_path = os.path.join(self.cache_dir, base + ".wav")

                if not self.ffmpeg_available:
                    self.ui_log("[ERROR] FFmpeg no está en PATH. Instálalo para reproducir.")
                    continue

                self._tts_with_retries(text, mp3_path)

                seg = AudioSegment.from_file(mp3_path)
                seg = normalize(seg)  # leve normalización para mejorar consistencia
                if self.save_wav:
                    seg_out = seg.set_frame_rate(self.sample_rate).set_channels(1).set_sample_width(2)
                    seg_out.export(wav_path, format="wav")

                self.on_status("🗣️ Reproduciendo voz cambiada…")
                self._play_segment(seg)
                self.on_status("✅ Listo. Escuchando…")

                if not self.keep_mp3:
                    try:
                        os.remove(mp3_path)
                    except Exception:
                        pass

            except Exception as e:
                self.ui_log(f"[TTS ERROR] {e}")

            finally:
                if self.ducking and not self.stop_event.is_set():
                    try:
                        self.start_input()
                    except Exception as e:
                        self.ui_log(f"[AUDIO] No se pudo reanudar captura: {e}")

        self.ui_log("[OK] Hilo TTS detenido.")

    # ---------- ciclo de vida ----------
    def start(self):
        if self.model is None or self.recognizer is None:
            raise RuntimeError("Modelo Vosk no cargado.")
        self.stop_event.clear()
        self.start_input()
        self.t_asr = threading.Thread(target=self.asr_worker, daemon=True)
        self.t_tts = threading.Thread(target=self.tts_worker, daemon=True)
        self.t_asr.start()
        self.t_tts.start()

    def stop(self):
        self.stop_event.set()
        try:
            self.stop_input()
        except Exception:
            pass
        while not self.audio_q.empty():
            try: self.audio_q.get_nowait()
            except Exception: break
        while not self.phrase_q.empty():
            try: self.phrase_q.get_nowait()
            except Exception: break


# ======================= GUI =======================
class App:
    SPANISH_VOICES = [
        "es-ES-ElviraNeural", "es-ES-AlvaroNeural",
        "es-MX-DaliaNeural", "es-MX-JorgeNeural",
        "es-PE-CamilaNeural", "es-PE-AlexNeural",
        "es-CO-GonzaloNeural", "es-CO-SalomeNeural",
        "es-CL-CatalinaNeural", "es-CL-LorenzoNeural",
        "es-AR-ElenaNeural", "es-AR-TomasNeural",
        "es-US-PalomaNeural", "es-US-AlonsoNeural"
    ]

    def __init__(self, root):
        self.root = root
        self.root.title("Cambio de Voz (Vosk + Edge-TTS) + Texto→Voz con guardado MP3")

        # estilo
        if USE_TTKB:
            style = tb.Style("cosmo")
        else:
            style = None

        container = tb.Frame(root, padding=10) if USE_TTKB else tk.Frame(root)
        container.pack(fill="both", expand=True)

        # ====== fila 1: modelo ======
        fr_model = tb.Labelframe(container, text="Modelo Vosk", padding=10) if USE_TTKB else tk.LabelFrame(container, text="Modelo Vosk", padx=10, pady=10)
        fr_model.pack(fill="x")

        self.model_path_var = tk.StringVar(value="")
        self.model_entry = tb.Entry(fr_model, textvariable=self.model_path_var, width=80) if USE_TTKB else tk.Entry(fr_model, textvariable=self.model_path_var, width=80)
        self.model_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        btn_browse = tb.Button(fr_model, text="Buscar modelo…", command=self.browse_model) if USE_TTKB else tk.Button(fr_model, text="Buscar modelo…", command=self.browse_model)
        btn_browse.pack(side="left")

        # ====== fila 2: dispositivos ======
        fr_dev = tb.Labelframe(container, text="Dispositivos de Audio", padding=10) if USE_TTKB else tk.LabelFrame(container, text="Dispositivos de Audio", padx=10, pady=10)
        fr_dev.pack(fill="x", pady=(8, 0))

        self.input_combo = tb.Combobox(fr_dev, width=50) if USE_TTKB else ttk.Combobox(fr_dev, width=50)
        self.output_combo = tb.Combobox(fr_dev, width=50) if USE_TTKB else ttk.Combobox(fr_dev, width=50)
        btn_refresh = tb.Button(fr_dev, text="Actualizar", command=self.refresh_devices) if USE_TTKB else tk.Button(fr_dev, text="Actualizar", command=self.refresh_devices)

        tk.Label(fr_dev, text="Micrófono:").pack(side="left")
        self.input_combo.pack(side="left", padx=6)
        tk.Label(fr_dev, text="Salida:").pack(side="left", padx=(10,0))
        self.output_combo.pack(side="left", padx=6)
        btn_refresh.pack(side="left", padx=(10,0))

        # ====== fila 3: voz ======
        fr_voice = tb.Labelframe(container, text="Voz y Parámetros", padding=10) if USE_TTKB else tk.LabelFrame(container, text="Voz y Parámetros", padx=10, pady=10)
        fr_voice.pack(fill="x", pady=(8,0))

        self.voice_combo = tb.Combobox(fr_voice, values=self.SPANISH_VOICES, width=30) if USE_TTKB else ttk.Combobox(fr_voice, values=self.SPANISH_VOICES, width=30)
        self.voice_combo.set("es-ES-ElviraNeural")
        self.rate_var = tk.StringVar(value="+0%")
        self.vol_var = tk.StringVar(value="+0%")
        self.gain_var = tk.DoubleVar(value=0.0)

        tk.Label(fr_voice, text="Voz:").pack(side="left")
        self.voice_combo.pack(side="left", padx=6)
        tk.Label(fr_voice, text="Rate:").pack(side="left", padx=(12,0))
        (tb.Entry(fr_voice, textvariable=self.rate_var, width=8) if USE_TTKB else tk.Entry(fr_voice, textvariable=self.rate_var, width=8)).pack(side="left", padx=6)
        tk.Label(fr_voice, text="Vol:").pack(side="left", padx=(12,0))
        (tb.Entry(fr_voice, textvariable=self.vol_var, width=8) if USE_TTKB else tk.Entry(fr_voice, textvariable=self.vol_var, width=8)).pack(side="left", padx=6)
        tk.Label(fr_voice, text="Gain dB:").pack(side="left", padx=(12,0))
        (tb.Entry(fr_voice, textvariable=self.gain_var, width=6) if USE_TTKB else tk.Entry(fr_voice, textvariable=self.gain_var, width=6)).pack(side="left", padx=6)

        # ====== fila 4: controles (modulador/ASR) ======
        fr_ctrl = tb.Frame(container) if USE_TTKB else tk.Frame(container)
        fr_ctrl.pack(fill="x", pady=(8,0))

        self.btn_start = tb.Button(fr_ctrl, text="Iniciar (ASR+TTS)", bootstyle=SUCCESS, command=self.start) if USE_TTKB else tk.Button(fr_ctrl, text="Iniciar (ASR+TTS)", command=self.start)
        self.btn_stop  = tb.Button(fr_ctrl, text="Detener", bootstyle=DANGER, command=self.stop, state="disabled") if USE_TTKB else tk.Button(fr_ctrl, text="Detener", command=self.stop, state="disabled")
        self.btn_test  = tb.Button(fr_ctrl, text="Probar TTS", bootstyle=INFO, command=self.test_tts) if USE_TTKB else tk.Button(fr_ctrl, text="Probar TTS", command=self.test_tts)

        self.btn_start.pack(side="left")
        self.btn_stop.pack(side="left", padx=8)
        self.btn_test.pack(side="left")

        # ====== fila 5: estado ======
        fr_status = tb.Frame(container) if USE_TTKB else tk.Frame(container)
        fr_status.pack(fill="x", pady=(8,0))
        self.status_var = tk.StringVar(value="Listo")
        (tb.Label(fr_status, textvariable=self.status_var) if USE_TTKB else tk.Label(fr_status, textvariable=self.status_var)).pack(side="left")

        # ====== fila 6: Transcripción (modulador) ======
        fr_text = tb.Labelframe(container, text="Transcripción (micro)", padding=8) if USE_TTKB else tk.LabelFrame(container, text="Transcripción (micro)", padx=8, pady=8)
        fr_text.pack(fill="both", expand=True, pady=(8,0))
        self.txt = ScrolledText(fr_text, height=10, wrap="word")
        self.txt.pack(fill="both", expand=True)

        # ====== NUEVO: fila 7: Texto -> Voz (panel dedicado) ======
        fr_tts = tb.Labelframe(container, text="Texto → Voz (TTS independiente)", padding=10) if USE_TTKB else tk.LabelFrame(container, text="Texto → Voz (TTS independiente)", padx=10, pady=10)
        fr_tts.pack(fill="x", pady=(8, 0))

        # caja de texto
        self.tts_text = ScrolledText(fr_tts, height=5, wrap="word")
        self.tts_text.pack(fill="x", expand=True)

        # opciones de guardado
        fr_tts_opts = tb.Frame(fr_tts) if USE_TTKB else tk.Frame(fr_tts)
        fr_tts_opts.pack(fill="x", pady=(6,0))

        self.tts_autosave = tk.BooleanVar(value=True)
        self.tts_outdir = tk.StringVar(value=os.path.abspath("tts_mp3"))
        os.makedirs(self.tts_outdir.get(), exist_ok=True)

        (tb.Checkbutton(fr_tts_opts, text="Guardar automáticamente en MP3 (192 kbps)", variable=self.tts_autosave) 
         if USE_TTKB else tk.Checkbutton(fr_tts_opts, text="Guardar automáticamente en MP3 (192 kbps)", variable=self.tts_autosave)
        ).pack(side="left")

        (tb.Entry(fr_tts_opts, textvariable=self.tts_outdir, width=60) if USE_TTKB else tk.Entry(fr_tts_opts, textvariable=self.tts_outdir, width=60)).pack(side="left", padx=6)
        (tb.Button(fr_tts_opts, text="Elegir carpeta…", command=self.choose_outdir) if USE_TTKB else tk.Button(fr_tts_opts, text="Elegir carpeta…", command=self.choose_outdir)).pack(side="left")

        # botones TTS
        fr_tts_btns = tb.Frame(fr_tts) if USE_TTKB else tk.Frame(fr_tts)
        fr_tts_btns.pack(fill="x", pady=(8,0))

        (tb.Button(fr_tts_btns, text="Generar y Reproducir", bootstyle=PRIMARY, command=self.generate_and_play_tts)
         if USE_TTKB else tk.Button(fr_tts_btns, text="Generar y Reproducir", command=self.generate_and_play_tts)
        ).pack(side="left")

        (tb.Button(fr_tts_btns, text="Guardar MP3 (último)", bootstyle=INFO, command=self.save_last_tts_mp3)
         if USE_TTKB else tk.Button(fr_tts_btns, text="Guardar MP3 (último)", command=self.save_last_tts_mp3)
        ).pack(side="left", padx=8)

        self.last_tts_seg = None  # guardamos último AudioSegment generado para opción "Guardar MP3 (último)"

        # logger
        self._setup_logging()

        # engine compartido
        self.engine = VoiceChangerEngine(
            ui_logger=self.ui_log,
            on_partial=self.on_partial,
            on_final=self.on_final,
            on_status=self.set_status
        )

        # devices iniciales
        self.refresh_devices()

        if not ffmpeg_ok():
            messagebox.showwarning("FFmpeg", "No se encontró FFmpeg en PATH. Pydub lo requiere para reproducir/convertir.")

        # cerrar ordenado
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- logging a GUI y archivo ----------
    def _setup_logging(self):
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            handlers=[logging.FileHandler("voz_gui.log", encoding="utf-8"), logging.StreamHandler(sys.stdout)]
        )

    def ui_log(self, msg):
        logging.info(msg)
        self.txt.insert("end", f"{msg}\n")
        self.txt.see("end")

    # ---------- acciones GUI ----------
    def browse_model(self):
        path = filedialog.askdirectory(title="Selecciona carpeta del modelo Vosk")
        if path:
            self.model_path_var.set(path)

    def refresh_devices(self):
        devices = sd.query_devices()
        in_opts, out_opts = [], []
        for idx, d in enumerate(devices):
            ins = d.get("max_input_channels", 0)
            outs = d.get("max_output_channels", 0)
            label = f"{idx}: {d['name']}  (in:{ins}/out:{outs})"
            if ins > 0:
                in_opts.append(label)
            if outs > 0:
                out_opts.append(label)
        self.input_combo["values"] = in_opts
        self.output_combo["values"] = out_opts
        if in_opts and not self.input_combo.get():
            self.input_combo.set(in_opts[0])
        if out_opts and not self.output_combo.get():
            self.output_combo.set(out_opts[0])

    def parse_device_index(self, combo_value):
        if not combo_value:
            return None
        try:
            idx = int(combo_value.split(":")[0])
            return idx
        except Exception:
            return None

    def set_status(self, text):
        self.status_var.set(text)
        self.root.update_idletasks()

    def on_partial(self, partial):
        self.status_var.set(f"Escuchando… [{partial}]")
        self.root.update_idletasks()

    def on_final(self, text):
        self.txt.insert("end", f"[🎧] {text}\n")
        self.txt.see("end")

    def start(self):
        # aplicar config al engine
        model_dir = self.model_path_var.get().strip()
        if not model_dir:
            messagebox.showerror("Modelo Vosk", "Selecciona la carpeta del modelo Vosk (botón 'Buscar modelo…').")
            return
        try:
            self.engine.load_model(model_dir)
        except Exception as e:
            messagebox.showerror("Modelo Vosk", str(e))
            return

        self.engine.sample_rate = 16000
        self.engine.channels = 1
        self.engine.input_device = self.parse_device_index(self.input_combo.get())
        self.engine.output_device = self.parse_device_index(self.output_combo.get())

        self.engine.voice = self.voice_combo.get().strip() or "es-ES-ElviraNeural"
        self.engine.tts_rate = self.rate_var.get().strip() or "+0%"
        self.engine.tts_volume = self.vol_var.get().strip() or "+0%"
        try:
            self.engine.playback_gain_db = float(self.gain_var.get())
        except Exception:
            self.engine.playback_gain_db = 0.0

        # flags
        self.engine.ducking = True
        self.engine.enable_noise_gate = True
        self.engine.rms_gate_threshold = 0.005
        self.engine.keep_mp3 = False
        self.engine.save_wav = True

        # parámetros fraseo
        self.engine.min_phrase_len = 6
        self.engine.silence_finalize_sec = 0.8
        self.engine.partial_throttle_sec = 0.25

        try:
            self.engine.stop_event.clear()
            self.engine.start()
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
            self.set_status("🎙️ Grabando…")
            self.ui_log("[OK] Inicio completo.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def stop(self):
        try:
            self.engine.stop()
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.set_status("Detenido")
            self.ui_log("[OK] Detenido.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def test_tts(self):
        text = "Esta es una prueba. Estás escuchando mi voz con Edge T T S."
        try:
            threading.Thread(target=self._do_test_tts, args=(text,), daemon=True).start()
        except Exception as e:
            messagebox.showerror("TTS", str(e))

    def _do_test_tts(self, text):
        try:
            voice = self.voice_combo.get().strip() or "es-ES-ElviraNeural"
            rate = self.rate_var.get().strip() or "+0%"
            vol  = self.vol_var.get().strip() or "+0%"
            out_mp3 = os.path.join("tts_cache", f"test_{datetime.now().strftime('%H%M%S')}.mp3")
            os.makedirs("tts_cache", exist_ok=True)

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            comm = edge_tts.Communicate(text, voice, rate=rate, volume=vol)
            loop.run_until_complete(comm.save(out_mp3))
            loop.close()

            if not ffmpeg_ok():
                self.ui_log("[ERROR] FFmpeg no está en PATH.")
                return
            seg = AudioSegment.from_file(out_mp3)
            seg = normalize(seg)
            try:
                gain = float(self.gain_var.get())
            except Exception:
                gain = 0.0
            if gain != 0.0:
                seg = seg + gain
            seg = seg.set_frame_rate(16000).set_channels(1).set_sample_width(2)
            samples = np.array(seg.get_array_of_samples())
            sd.play(samples, samplerate=seg.frame_rate, blocking=True, device=self.engine.output_device)
            sd.stop()
            self.last_tts_seg = seg
            self.ui_log("[OK] TTS de prueba reproducido.")
        except Exception as e:
            self.ui_log(f"[TTS] {e}")

    # ====== NUEVO: helpers TTS dedicado ======
    def choose_outdir(self):
        path = filedialog.askdirectory(title="Selecciona carpeta para guardar MP3")
        if path:
            self.tts_outdir.set(path)
            os.makedirs(path, exist_ok=True)

    def generate_and_play_tts(self):
        text = self.tts_text.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("Texto vacío", "Escribe algo para sintetizar.")
            return
        threading.Thread(target=self._do_generate_and_play_tts, args=(text,), daemon=True).start()

    def _do_generate_and_play_tts(self, text):
        try:
            if not ffmpeg_ok():
                self.ui_log("[ERROR] FFmpeg no está en PATH para convertir/normalizar.")
                return

            # usar los parámetros actuales de la UI
            voice = self.voice_combo.get().strip() or "es-ES-ElviraNeural"
            rate = self.rate_var.get().strip() or "+0%"
            vol  = self.vol_var.get().strip() or "+0%"

            os.makedirs("tts_cache", exist_ok=True)
            base = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
            temp_mp3 = os.path.join("tts_cache", f"{base}.mp3")

            # sintetiza con reintentos del engine
            self.engine._tts_with_retries(text, temp_mp3, voice=voice, rate=rate, vol=vol)

            # lee, normaliza y guarda en memoria
            seg = AudioSegment.from_file(temp_mp3)
            seg = normalize(seg)

            # reproducir con ganancia final
            try:
                gain = float(self.gain_var.get())
            except Exception:
                gain = 0.0
            if gain != 0.0:
                seg = seg + gain

            # reproducir
            seg_play = seg.set_frame_rate(16000).set_channels(1).set_sample_width(2)
            samples = np.array(seg_play.get_array_of_samples())
            sd.play(samples, samplerate=seg_play.frame_rate, blocking=True, device=self.engine.output_device)
            sd.stop()
            self.last_tts_seg = seg  # conservar versión normalizada original

            # guardado automático
            if self.tts_autosave.get():
                outdir = self.tts_outdir.get().strip() or os.path.abspath("tts_mp3")
                os.makedirs(outdir, exist_ok=True)
                safe_snip = (text[:30].replace(" ", "_").replace("/", "_").replace("\\", "_")) if text else "tts"
                outfile = os.path.join(outdir, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_snip}.mp3")
                # export con bitrate alto y sample rate 44100
                seg.export(outfile, format="mp3", bitrate="192k", parameters=["-ar", "44100"])
                self.ui_log(f"[OK] MP3 guardado: {outfile}")
                self.set_status("💾 MP3 guardado")
            else:
                self.set_status("🗣️ Reproducido (no guardado)")
        except Exception as e:
            self.ui_log(f"[TTS TEXTO→VOZ] {e}")
            self.set_status("⚠️ Error en TTS")

    def save_last_tts_mp3(self):
        if self.last_tts_seg is None:
            messagebox.showinfo("Sin audio", "Aún no has generado audio en el panel Texto→Voz.")
            return
        outdir = self.tts_outdir.get().strip() or os.path.abspath("tts_mp3")
        os.makedirs(outdir, exist_ok=True)
        outfile = os.path.join(outdir, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_manual.mp3")
        try:
            self.last_tts_seg.export(outfile, format="mp3", bitrate="192k", parameters=["-ar", "44100"])
            self.ui_log(f"[OK] MP3 guardado manualmente: {outfile}")
            self.set_status("💾 MP3 guardado (manual)")
        except Exception as e:
            messagebox.showerror("Guardar MP3", str(e))

    def on_close(self):
        try:
            self.stop()
        except Exception:
            pass
        self.root.destroy()


def main():
    if USE_TTKB:
        root = tb.Window(themename="cosmo")
    else:
        root = tk.Tk()
    app = App(root)
    root.geometry("980x780")
    root.mainloop()


if __name__ == "__main__":
    main()
