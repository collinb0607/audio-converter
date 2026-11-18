#!/usr/bin/env python3
"""
Audio Converter - a cross-platform (Windows / macOS / Linux) tool that pulls the
sound out of videos, or converts audio files, to MP3 or WAV, in batches, with
optional loudness normalization.

Requirements:
    Python 3.8+ (with Tkinter, included in the python.org installers)
    imageio-ffmpeg -> bundles an FFmpeg binary (no separate install needed)
    tkinterdnd2    -> drag and drop (optional: the app works without it)
    The app offers to install them on first run. Manual install, using the Python that runs the app:
        python -m pip install imageio-ffmpeg tkinterdnd2

Run:
    python audio_converter.py
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

VIDEO_EXTS = "mp4 mov m4v avi mkv webm wmv flv mpg mpeg 3gp ts"
AUDIO_EXTS = "mp3 wav m4a aac flac ogg opus wma aif aiff"
MEDIA_EXTS = {"." + e for e in (VIDEO_EXTS + " " + AUDIO_EXTS).split()}
FILE_TYPES = [
    ("Video and audio files", " ".join("*." + e for e in (VIDEO_EXTS + " " + AUDIO_EXTS).split())),
    ("All files", "*.*"),
]

# Output formats. Each quality choice maps to FFmpeg encoder arguments.
FORMATS = {
    "MP3": dict(ext="mp3", muxer="mp3", qualities={
        "High — about 190 kbps (recommended)": ["-c:a", "libmp3lame", "-q:a", "2"],  # LAME VBR V2
        "Highest — 320 kbps": ["-c:a", "libmp3lame", "-b:a", "320k"],
        "Smaller — 128 kbps": ["-c:a", "libmp3lame", "-b:a", "128k"],
    }, about="Plays everywhere. Small files with no audible loss at the High setting."),
    "WAV": dict(ext="wav", muxer="wav", qualities={
        "16-bit (CD quality)": ["-c:a", "pcm_s16le"],
        "24-bit (for editing)": ["-c:a", "pcm_s24le"],
    }, about="Uncompressed, for editing or burning to CD. About 10 MB per minute."),
}
MP3_MAX_RATE = 48000  # MP3 can't store higher sample rates

# Loudness targets in LUFS (loudness units: how loud it SOUNDS, not just its peaks).
LOUDNESS = {
    "Music / streaming — −14 LUFS": -14.0,     # Spotify, YouTube
    "Podcast / voice — −16 LUFS": -16.0,       # Apple Podcasts and most podcast hosts
    "Broadcast (EBU R128) — −23 LUFS": -23.0,  # TV and radio standard
}
# True-peak ceiling in dBTP. MP3 encoding itself adds peak overshoot (more at
# lower bitrates), so lossy output gets extra headroom, and the finished MP3 is
# measured and turned down if it still ends up above MAX_PEAK.
TRUE_PEAK = {"MP3": -1.5, "WAV": -1.0}
MAX_PEAK = -1.0
AUDIO_TRACKS = ["First track", "Mix all tracks"]


# --------------------------------------------------------------------------- #
#  FFmpeg helpers
# --------------------------------------------------------------------------- #
# Hide the console window that would otherwise flash up on Windows.
_POPEN_FLAGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def ffmpeg_check(path):
    """Return (usable, version_string). 'Usable' means it has the MP3 encoder
    and the loudnorm filter. Old copies bundled with other software often don't."""
    try:
        ver = subprocess.run([path, "-version"], capture_output=True, text=True,
                             errors="replace", timeout=15, **_POPEN_FLAGS).stdout
        enc = subprocess.run([path, "-hide_banner", "-encoders"], capture_output=True, text=True,
                             errors="replace", timeout=15, **_POPEN_FLAGS).stdout
        flt = subprocess.run([path, "-hide_banner", "-filters"], capture_output=True, text=True,
                             errors="replace", timeout=15, **_POPEN_FLAGS).stdout
    except (OSError, subprocess.SubprocessError):
        return False, "couldn't run"
    m = re.search(r"version\s+(\S+)", ver)
    return ("libmp3lame" in enc and "loudnorm" in flt), (m[1] if m else "unknown")


def find_ffmpeg():
    """Return (path, description) of a usable FFmpeg, or (None, reason).

    Prefers the copy bundled by imageio-ffmpeg (known-good, recent), and only
    accepts one from PATH if it can make MP3s and normalize loudness.
    """
    try:
        import importlib
        importlib.invalidate_caches()  # see packages installed since startup
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        ok, ver = ffmpeg_check(path)
        if ok:
            return path, f"FFmpeg {ver} (bundled)"
    except Exception:
        pass
    path = shutil.which("ffmpeg")
    if path:
        ok, ver = ffmpeg_check(path)
        if ok:
            return path, f"FFmpeg {ver} ({path})"
        return None, f"The FFmpeg found at {path} (version {ver}) is too old or can't make MP3s."
    return None, "FFmpeg isn't installed."


def install_requirements():
    """pip-install the dependency into *this* Python interpreter.

    Using sys.executable guarantees it lands in the same Python that's running
    the app — the usual cause of 'I installed it but it's not found' is having
    several Pythons, where `pip` and double-clicking the script use different ones.
    """
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):  # pythonw has no console; use python.exe for pip
        exe = exe[:-len("pythonw.exe")] + "python.exe"
    r = subprocess.run([exe, "-m", "pip", "install", "--upgrade", "imageio-ffmpeg", "tkinterdnd2"],
                       capture_output=True, text=True, errors="replace", **_POPEN_FLAGS)
    add_user_site()
    return r.returncode == 0, (r.stderr or r.stdout).strip().splitlines()[-3:]


def add_user_site():
    """Make packages pip just put in the per-user folder importable right away.

    When Python's own folder isn't writable (e.g. installed under Program
    Files), pip installs to the user folder instead (%APPDATA%\\Python\\...
    on Windows, ~/Library/Python/... on macOS). Python only adds that folder
    to sys.path at startup if it already existed, so on a first install it
    isn't searched until the app is restarted.
    """
    import site
    path = site.getusersitepackages() if site.ENABLE_USER_SITE else None  # off in venvs
    if path and os.path.isdir(path) and path not in sys.path:
        site.addsitedir(path)


def probe_media(ffmpeg, path):
    """Return a dict (duration, tracks, rate, channels, codec, error) by parsing `ffmpeg -i`.

    This only reads the file header, so it's instant even for huge videos.
    'tracks' is the number of audio streams (OBS can record several).
    """
    out = subprocess.run([ffmpeg, "-hide_banner", "-i", path],
                         capture_output=True, text=True, errors="replace",
                         **_POPEN_FLAGS).stderr
    info = dict(duration=None, tracks=0, rate=None, channels=None, codec=None, error=None)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if m:
        info["duration"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    audio = re.findall(r"Stream #\S+.*?: Audio: ([^\n]+)", out)
    info["tracks"] = len(audio)
    if audio:
        first = audio[0]
        info["codec"] = re.match(r"\s*(\w+)", first)[1]
        m = re.search(r"(\d+) Hz", first)
        info["rate"] = int(m[1]) if m else None
        m = re.search(r"Hz,\s*([^,]+)", first)
        info["channels"] = m[1].strip() if m else None
    if not info["duration"] and not audio:
        info["error"] = explain_probe_error(out)
    elif not audio:
        info["error"] = "No sound in this file."
    return info


def explain_probe_error(ffmpeg_output):
    """Turn FFmpeg's error output into a short, human-readable reason."""
    text = ffmpeg_output.lower()
    if "moov atom not found" in text or "invalid data found" in text:
        return "The file looks incomplete or damaged."
    if "permission denied" in text:
        return "Permission denied — the file may be open in another program."
    if "no such file" in text:
        return "File not found."
    lines = [l.strip() for l in ffmpeg_output.strip().splitlines() if l.strip()]
    return lines[-1] if lines else "FFmpeg returned no information."


def audio_graph(info, mix_all, extra=None):
    """FFmpeg filter graph that picks the audio track(s) and optionally adds a filter.

    'Mix all tracks' combines e.g. OBS's game + mic tracks into one. normalize=0
    keeps each track at its own volume (amix otherwise lowers them all).
    """
    if mix_all and info["tracks"] > 1:
        ins = "".join(f"[0:a:{i}]" for i in range(info["tracks"]))
        chain = f"{ins}amix=inputs={info['tracks']}:normalize=0"
    else:
        chain = "[0:a:0]anull"
    if extra:
        chain += "," + extra
    return chain + "[a]"


def loudnorm_filter(target, peak, measured=None):
    """EBU R128 loudness normalization (FFmpeg's loudnorm), in two passes.

    Pass 1 only measures. Pass 2 feeds those measurements back so loudnorm can
    apply ONE steady gain change ('linear'), which sounds natural — a single
    pass has to adjust as it goes, like an automatic volume knob.
    """
    f = f"loudnorm=I={target}:TP={peak}"
    if measured is None:
        return f + ":LRA=11:print_format=json"
    # Allow the file's own loudness range, so loudnorm doesn't squash dynamics to fit 11 LU.
    lra = max(11.0, min(50.0, float(measured["input_lra"])))
    return (f + f":LRA={lra:g}:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}:linear=true:print_format=json")


def parse_loudnorm(stderr_text):
    """Pull loudnorm's JSON report out of FFmpeg's log."""
    m = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr_text)
    return json.loads(m[-1]) if m else None


def output_rate(fmt, info):
    """Keep the source's sample rate. MP3 tops out at 48 kHz, and loudnorm works
    at 192 kHz internally, so the rate must always be set explicitly."""
    rate = info["rate"] or 48000
    return min(rate, MP3_MAX_RATE) if fmt == "MP3" else rate


# ---- small formatting helpers ---------------------------------------------- #
def fmt_time(secs):
    m, s = divmod(int(round(secs)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(n):
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.0f} KB"


class EncodeError(Exception):
    pass


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- #
#  GUI
# --------------------------------------------------------------------------- #
class ConverterApp:
    def __init__(self, root):
        self.root = root
        root.title("Audio Converter")
        root.minsize(640, 0)
        self.ffmpeg, self.ffmpeg_desc = find_ffmpeg()
        self.proc = None
        self.cancelled = False
        self.running = False
        self.items = {}  # tree row id -> dict(path, info, status)
        self.ui_queue = queue.Queue()
        self.probe_queue = queue.Queue()  # files waiting to be read, one background reader
        self._pump_ui()
        threading.Thread(target=self._probe_worker, daemon=True).start()
        # Show unexpected errors in a dialog. By default Tk prints them to a
        # console, which is invisible when the script is double-clicked.
        root.report_callback_exception = self._report_error
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        home = os.path.expanduser("~")
        music = os.path.join(home, "Music")
        self.v = {
            "format": tk.StringVar(value="MP3"),
            "quality": tk.StringVar(),
            "track": tk.StringVar(value=AUDIO_TRACKS[0]),
            "normalize": tk.BooleanVar(value=False),
            "loudness": tk.StringVar(value=next(iter(LOUDNESS))),
            "where": tk.StringVar(value="same"),
            "out_dir": tk.StringVar(value=music if os.path.isdir(music) else home),
            "reveal": tk.BooleanVar(value=True),
        }
        self._build_ui()
        self.v["format"].trace_add("write", lambda *_: self._on_format())
        self.v["normalize"].trace_add("write", lambda *_: self._on_normalize())
        self.v["where"].trace_add("write", lambda *_: self._on_where())
        self._on_format()
        self._on_normalize()
        self._on_where()
        self.dnd = enable_drag_and_drop(root, self._on_drop, self._on_drag_enter, self._update_list_label)
        self._update_list_label()

        if self.ffmpeg:
            self.status.config(text=f"Ready — using {self.ffmpeg_desc}.")
        else:
            # Defer so the main window is drawn before the dialog appears.
            self.root.after(200, self.offer_install)

    # ---- dependency installer ------------------------------------------ #
    def offer_install(self):
        if getattr(sys, "frozen", False):
            # The downloadable app bundles FFmpeg, and sys.executable is the app
            # itself, so "pip install" would just launch another copy of it.
            messagebox.showerror("Missing FFmpeg", f"{self.ffmpeg_desc}\n\n"
                                 "This copy of Audio Converter is incomplete. Please download it again.")
            return
        if not messagebox.askyesno(
                "Install FFmpeg?",
                f"Audio Converter needs FFmpeg — {self.ffmpeg_desc}\n\n"
                "Install it now? (about 30 MB download)\n\n"
                f"This runs pip for the Python running this app:\n{sys.executable}"):
            self.status.config(text="FFmpeg missing — nothing can be converted until it's installed.")
            return
        self._set_busy(True)
        self.cancel_btn.config(state="disabled")
        self.status.config(text="Installing FFmpeg… this can take a minute.")

        def work():
            try:
                ok, tail = install_requirements()
            except Exception as e:
                ok, tail = False, [str(e)]
            self.ui(lambda: self._install_done(ok, tail))
        threading.Thread(target=work, daemon=True).start()

    def _install_done(self, ok, tail):
        self._set_busy(False)
        self.ffmpeg, self.ffmpeg_desc = find_ffmpeg()
        if ok and self.ffmpeg:
            self.status.config(text=f"Installed! Using {self.ffmpeg_desc}.")
            if not self.dnd:  # tkinterdnd2 was just installed too
                self.dnd = enable_drag_and_drop(self.root, self._on_drop, self._on_drag_enter,
                                                self._update_list_label)
                self._update_list_label()
            for iid, item in self.items.items():  # re-read files added before FFmpeg existed
                item["info"] = None
                self._set_row(iid, status="Reading…")
                self.probe_queue.put((iid, item["path"]))
        else:
            self.status.config(text="Install failed — see the error message.")
            messagebox.showerror(
                "Install failed",
                "Couldn't install automatically:\n\n" + "\n".join(tail) +
                "\n\nTry running this in a terminal:\n"
                f'"{sys.executable}" -m pip install imageio-ffmpeg tkinterdnd2')

    # ---- layout --------------------------------------------------------- #
    def _build_ui(self):
        pad = {"padx": 6, "pady": 3}
        app_header(self.root, "Audio Converter", "Videos and audio to MP3 or WAV, in batches · free, private, no limits", "♪").pack(fill="x")
        main = ttk.Frame(self.root, padding=(12, 10))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)

        f = ttk.LabelFrame(main, text="1. Files (videos or audio)", padding=8)
        f.grid(row=0, column=0, sticky="nsew", **pad)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        btns = ttk.Frame(f)
        btns.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.edit_btns = [
            ttk.Button(btns, text="Add files…", command=self.add_files),
            ttk.Button(btns, text="Add folder…", command=self.add_folder),
            ttk.Button(btns, text="Remove", command=self.remove_selected),
            ttk.Button(btns, text="Clear", command=self.clear),
        ]
        for i, b in enumerate(self.edit_btns):
            b.grid(row=0, column=i, padx=(0, 6))
        cols = ("length", "audio", "status")
        self.tree = ttk.Treeview(f, columns=cols, height=8, selectmode="extended")
        self.tree.heading("#0", text="File")
        self.tree.heading("length", text="Length")
        self.tree.heading("audio", text="Sound")
        self.tree.heading("status", text="Status")
        self.tree.column("#0", width=250, stretch=True)
        self.tree.column("length", width=60, anchor="e", stretch=False)
        self.tree.column("audio", width=150, stretch=False)
        self.tree.column("status", width=150, stretch=False)
        self.tree.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(f, orient="vertical", command=self.tree.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.list_lbl = ttk.Label(f, text="Add files, or a folder (files directly inside it).",
                                  foreground=MUTED)
        self.list_lbl.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        f = ttk.LabelFrame(main, text="2. Convert to", padding=8)
        f.grid(row=1, column=0, sticky="ew", **pad)
        ttk.Label(f, text="Format").grid(row=0, column=0, sticky="w", **pad)
        ttk.Combobox(f, values=list(FORMATS), textvariable=self.v["format"],
                     state="readonly", width=8).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(f, text="Quality").grid(row=0, column=2, sticky="w", **pad)
        self.quality_cb = ttk.Combobox(f, textvariable=self.v["quality"], state="readonly", width=40)
        self.quality_cb.grid(row=0, column=3, sticky="w", **pad)
        self.format_lbl = ttk.Label(f, text="", foreground=MUTED)
        self.format_lbl.grid(row=1, column=0, columnspan=4, sticky="w", **pad)
        ttk.Label(f, text="Audio track").grid(row=2, column=0, sticky="w", **pad)
        ttk.Combobox(f, values=AUDIO_TRACKS, textvariable=self.v["track"],
                     state="readonly", width=14).grid(row=2, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(f, text="Only matters for videos with several tracks (e.g. OBS game + mic).",
                  foreground=MUTED).grid(row=3, column=0, columnspan=4, sticky="w", **pad)

        f = ttk.LabelFrame(main, text="3. Volume", padding=8)
        f.grid(row=2, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)  # the long note's extra width goes here, not between checkbox and list
        ttk.Checkbutton(f, text="Normalize loudness to", variable=self.v["normalize"]).grid(
            row=0, column=0, sticky="w", **pad)
        self.loud_cb = ttk.Combobox(f, values=list(LOUDNESS), textvariable=self.v["loudness"],
                                    state="readonly", width=32)
        self.loud_cb.grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(f, text="Makes every file sound equally loud, and keeps peaks from clipping. "
                          "Off = the sound is left exactly as it is.",
                  foreground=MUTED, wraplength=580).grid(row=1, column=0, columnspan=2, sticky="w", **pad)

        f = ttk.LabelFrame(main, text="4. Save to", padding=8)
        f.grid(row=3, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        ttk.Radiobutton(f, text="Same folder as each file", value="same",
                        variable=self.v["where"]).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(f, text="This folder:", value="folder",
                        variable=self.v["where"]).grid(row=1, column=0, sticky="w")
        self.dir_e = ttk.Entry(f, textvariable=self.v["out_dir"])
        self.dir_e.grid(row=1, column=1, sticky="ew", padx=6)
        self.dir_btn = ttk.Button(f, text="Choose…", command=self.pick_folder)
        self.dir_btn.grid(row=1, column=2)
        ttk.Checkbutton(f, text="Show the results in their folder when done",
                        variable=self.v["reveal"]).grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        f = ttk.Frame(main)
        f.grid(row=4, column=0, sticky="ew", **pad)
        f.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(f, mode="determinate", maximum=1000)
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.cancel_btn = ttk.Button(f, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=1)
        self.go_btn = PillButton(f, "Convert", self.start)
        self.go_btn.grid(row=0, column=2, padx=(6, 0))
        foot = ttk.Frame(main)  # the status line, with the support link on its right
        foot.grid(row=5, column=0, sticky="ew", **pad)
        foot.columnconfigure(0, weight=1)
        self.status = ttk.Label(foot, text="Ready.", wraplength=450)
        self.status.grid(row=0, column=0, sticky="w")
        support_link(foot).grid(row=0, column=1, sticky="ne", padx=(12, 0))

    # ---- small helpers -------------------------------------------------- #
    def _on_format(self):
        fmt = FORMATS[self.v["format"].get()]
        names = list(fmt["qualities"])
        self.quality_cb.config(values=names)
        if self.v["quality"].get() not in names:
            self.v["quality"].set(names[0])
        self.format_lbl.config(text=fmt["about"])

    def _on_normalize(self):
        self.loud_cb.config(state="readonly" if self.v["normalize"].get() else "disabled")

    def _on_where(self):
        state = "normal" if self.v["where"].get() == "folder" else "disabled"
        self.dir_e.config(state=state)
        self.dir_btn.config(state=state)

    def pick_folder(self):
        d = filedialog.askdirectory(title="Choose output folder", initialdir=self.v["out_dir"].get())
        if d:
            self.v["out_dir"].set(d)

    def ui(self, fn):
        """Run fn on the main (UI) thread. Safe to call from any thread.

        Tkinter isn't thread-safe: touching widgets, or even calling
        root.after(), from a worker thread works on some systems and silently
        does nothing on others. So workers only put callbacks in a queue, and
        the UI thread drains it every 50 ms in _pump_ui().
        """
        self.ui_queue.put(fn)

    def _pump_ui(self):
        # Reschedule FIRST: if a queued callback raises, the pump must keep
        # running, or every later message (status, progress, errors) is lost.
        self.root.after(50, self._pump_ui)
        while True:
            try:
                fn = self.ui_queue.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception:
                self._report_error(*sys.exc_info())

    # ---- the file list -------------------------------------------------- #
    def add_files(self):
        self._add_paths(filedialog.askopenfilenames(title="Choose videos or audio files",
                                                    filetypes=FILE_TYPES))

    def add_folder(self):
        d = filedialog.askdirectory(title="Choose a folder of videos or audio files")
        if not d:
            return
        paths = self._folder_media(d)
        if not paths:
            messagebox.showinfo("No files found", "That folder has no video or audio files directly "
                                "inside it (subfolders aren't searched).")
        self._add_paths(paths)

    def _folder_media(self, d):
        return sorted(os.path.join(d, n) for n in os.listdir(d)
                      if os.path.splitext(n)[1].lower() in MEDIA_EXTS
                      and os.path.isfile(os.path.join(d, n)))

    def _on_drag_enter(self):
        if not self.running:
            self.list_lbl.config(text="Drop to add these files.", foreground="")

    def _on_drop(self, paths):
        """Files or folders dropped on the window (a folder adds the files directly inside it)."""
        self._update_list_label()
        if self.running:
            self.status.config(text="Wait for this batch to finish before adding more files.")
            return
        found, skipped, empty = [], 0, []
        for p in paths:
            if os.path.isdir(p):
                media = self._folder_media(p)
                found += media
                if not media:
                    empty.append(os.path.basename(p) or p)
            elif os.path.splitext(p)[1].lower() in MEDIA_EXTS:
                found.append(p)
            else:
                skipped += 1
        self._add_paths(found)
        notes = []
        if skipped:
            notes.append(f"{skipped} file{'s' * (skipped != 1)} skipped (not a video or audio file)")
        if empty:
            notes.append("no video or audio files directly inside " + ", ".join(empty) +
                         " (subfolders aren't searched)")
        if notes:
            text = "; ".join(notes)
            self.status.config(text=text[0].upper() + text[1:] + ".")

    def _add_paths(self, paths):
        known = {os.path.normcase(os.path.abspath(i["path"])) for i in self.items.values()}
        for p in paths:
            key = os.path.normcase(os.path.abspath(p))
            if key in known:
                continue  # already in the list
            known.add(key)
            iid = self.tree.insert("", "end", text=os.path.basename(p),
                                   values=("", "", "Reading…" if self.ffmpeg else "Needs FFmpeg"))
            self.items[iid] = dict(path=p, info=None)
            if self.ffmpeg:
                self.probe_queue.put((iid, p))
        self._update_list_label()

    def _probe_worker(self):
        """Background thread: reads each added file's details, one at a time."""
        while True:
            iid, path = self.probe_queue.get()
            try:
                info = probe_media(self.ffmpeg, path)
            except OSError as e:
                info = dict(duration=None, tracks=0, rate=None, channels=None, codec=None,
                            error=f"Couldn't run FFmpeg: {e}")
            self.ui(lambda i=iid, inf=info: self._probed(i, inf))

    def _probed(self, iid, info):
        if iid not in self.items:  # removed while it was being read
            return
        self.items[iid]["info"] = info
        sound = ""
        if info["tracks"]:
            rate = f"{info['rate'] / 1000:g} kHz" if info["rate"] else ""
            sound = " ".join(x for x in (info["codec"], rate, info["channels"]) if x)
            if info["tracks"] > 1:
                sound = f"{info['tracks']} tracks: " + sound
        self._set_row(iid, length=fmt_time(info["duration"]) if info["duration"] else "",
                      audio=sound, status=info["error"] or "Ready")
        self._update_list_label()

    def _set_row(self, iid, **cols):
        if self.tree.exists(iid):
            for col, val in cols.items():
                self.tree.set(iid, col, val)

    def remove_selected(self):
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.items.pop(iid, None)
        self._update_list_label()

    def clear(self):
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self._update_list_label()

    def _update_list_label(self):
        n = len(self.items)
        ok = sum(1 for i in self.items.values() if i["info"] and not i["info"]["error"])
        total = sum(i["info"]["duration"] or 0 for i in self.items.values()
                    if i["info"] and not i["info"]["error"])
        empty = ("Drag files or folders here, or use Add files." if getattr(self, "dnd", False)
                 else "Add files, or a folder (files directly inside it).")
        self.list_lbl.config(text=empty if not n else
                             f"{n} file{'s' * (n != 1)}, {ok} with sound, {fmt_time(total)} in total.",
                             foreground=MUTED)

    # ---- output names --------------------------------------------------- #
    def _plan_outputs(self, todo):
        """Decide each file's output path. Never the source file itself, and never
        two inputs writing the same output (e.g. clip.mp4 and clip.mov → clip.mp3)."""
        ext = FORMATS[self.v["format"].get()]["ext"]
        tag = " (normalized)" if self.v["normalize"].get() else " (converted)"
        taken, plans = set(), []
        for iid, item in todo:
            folder = (os.path.dirname(item["path"]) if self.v["where"].get() == "same"
                      else self.v["out_dir"].get().strip())
            stem = os.path.splitext(os.path.basename(item["path"]))[0]
            out = os.path.join(folder, f"{stem}.{ext}")
            if os.path.normcase(os.path.abspath(out)) == os.path.normcase(os.path.abspath(item["path"])):
                out = os.path.join(folder, f"{stem}{tag}.{ext}")
            base, n = os.path.splitext(out)[0], 2
            while os.path.normcase(os.path.abspath(out)) in taken:
                out = f"{base} ({n}).{ext}"
                n += 1
            taken.add(os.path.normcase(os.path.abspath(out)))
            plans.append((iid, out))
        return plans

    # ---- converting ----------------------------------------------------- #
    def start(self):
        if not self.ffmpeg:
            self.offer_install()
            return
        if not self.items:
            messagebox.showwarning("No files", "Add some videos or audio files first.")
            return
        if any(i["info"] is None for i in self.items.values()):
            messagebox.showinfo("Still reading files", "Still reading the files' details. "
                                "Try again in a moment.")
            return
        if self.v["where"].get() == "folder" and not os.path.isdir(self.v["out_dir"].get().strip()):
            messagebox.showwarning("Check settings", "The output folder doesn't exist.")
            return
        todo = [(iid, it) for iid, it in self.items.items() if not it["info"]["error"]]
        if not todo:
            messagebox.showwarning("Nothing to convert", "None of these files has any sound.")
            return
        plans = self._plan_outputs(todo)
        existing = [out for _, out in plans if os.path.exists(out)]
        skip = set()
        if existing:
            names = "\n".join(os.path.basename(p) for p in existing[:5])
            more = f"\n…and {len(existing) - 5} more" if len(existing) > 5 else ""
            answer = messagebox.askyesnocancel(
                "Files already exist",
                f"{len(existing)} of the output files already exist:\n\n{names}{more}\n\n"
                "Replace them?\n\nYes = replace   No = skip those files   Cancel = stop")
            if answer is None:
                return
            if answer is False:
                skip = set(existing)
        jobs = [(iid, self.items[iid]["path"], self.items[iid]["info"], out)
                for iid, out in plans if out not in skip]
        for iid, out in plans:
            self._set_row(iid, status="Skipped (exists)" if out in skip else "Waiting")
        if not jobs:
            self.status.config(text="Nothing to do: every output file already exists.")
            return
        settings = dict(fmt=self.v["format"].get(), quality=self.v["quality"].get(),
                        mix_all=self.v["track"].get() == "Mix all tracks",
                        target=LOUDNESS[self.v["loudness"].get()] if self.v["normalize"].get() else None)
        self.cancelled = False
        self._set_busy(True)
        # Run FFmpeg off the UI thread so the window stays responsive.
        threading.Thread(target=self._worker, args=(jobs, settings, len(skip)), daemon=True).start()

    def _set_busy(self, busy):
        self.running = busy
        self.go_btn.config(state="disabled" if busy else "normal")
        self.cancel_btn.config(state="normal" if busy else "disabled")
        for b in self.edit_btns:
            b.config(state="disabled" if busy else "normal")
        if busy:
            self.progress.config(value=0)

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _run(self, cmd, duration, on_progress):
        """Run one FFmpeg command, reporting 0..1 progress. Returns FFmpeg's log text.

        -progress pipe:1 makes FFmpeg print 'out_time_us=…' lines as it goes.
        The log goes to a temp file rather than a pipe, so a chatty FFmpeg can
        never fill the pipe and stall while we're reading progress.
        """
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as log:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log,
                                         stdin=subprocess.DEVNULL, text=True, errors="replace",
                                         **_POPEN_FLAGS)
            last = 0.0
            for line in self.proc.stdout:
                m = re.match(r"out_time_us=(\d+)", line)
                if m and duration:
                    frac = min(1.0, int(m[1]) / 1e6 / duration)
                    if frac - last >= 0.01:
                        last = frac
                        on_progress(frac)
            self.proc.wait()
            log.seek(0)
            text = log.read()
        if self.cancelled:
            raise Cancelled()
        if self.proc.returncode != 0:
            lines = [l for l in text.strip().splitlines() if l.strip()]
            raise EncodeError("\n".join(lines[-3:]) or f"FFmpeg exited with code {self.proc.returncode}.")
        return text

    def _convert_one(self, src, info, out, s, on_progress):
        """Convert one file (two FFmpeg runs when normalizing: measure, then apply).

        Writes to a temporary '.part' file and renames it at the end, so a cancel
        or a crash never leaves half a file, or damages one being replaced.
        """
        fmt = FORMATS[s["fmt"]]
        base = [self.ffmpeg, "-y", "-hide_banner", "-nostats", "-progress", "pipe:1", "-i", src]
        extra, note = None, ""
        if s["target"] is not None:
            peak = TRUE_PEAK[s["fmt"]]
            measure_cmd = base + ["-filter_complex",
                                  audio_graph(info, s["mix_all"], loudnorm_filter(s["target"], peak)),
                                  "-map", "[a]", "-f", "null", os.devnull]
            measured = parse_loudnorm(self._run(measure_cmd, info["duration"], lambda f: on_progress(f / 2)))
            if not measured or measured["input_i"] in ("-inf", "inf") or float(measured["input_i"]) < -70:
                note = "silent — volume left as is"
            else:
                extra = loudnorm_filter(s["target"], peak, measured)
        cmd = base + ["-filter_complex", audio_graph(info, s["mix_all"], extra), "-map", "[a]",
                      "-map_metadata", "0", "-ar", str(output_rate(s["fmt"], info))]
        if s["fmt"] == "MP3":
            cmd += ["-ac", "1" if info["channels"] == "mono" else "2", "-id3v2_version", "3"]
        cmd += fmt["qualities"][s["quality"]]
        part = out + ".part"
        done = 0.5 if s["target"] is not None else 0.0
        try:
            self._run(cmd + ["-f", fmt["muxer"], part], info["duration"],
                      lambda f: on_progress(done + f * (1 - done)))
            if extra and s["fmt"] == "MP3":
                # Check the peaks of the finished MP3, and redo it quieter if the
                # encoder pushed them over the ceiling.
                on_progress(1.0, "Checking peaks…")
                peak = self._true_peak(part, info["duration"])
                if peak is not None and peak > MAX_PEAK:
                    cut = peak - MAX_PEAK + 0.1
                    i = cmd.index("-filter_complex") + 1
                    cmd[i] = audio_graph(info, s["mix_all"], f"{extra},volume=-{cut:.2f}dB")
                    on_progress(0.0, "Lowering peaks")
                    self._run(cmd + ["-f", fmt["muxer"], part], info["duration"],
                              lambda f: on_progress(f, "Lowering peaks"))
            os.replace(part, out)
        finally:
            if os.path.exists(part):
                os.remove(part)
        return note

    def _true_peak(self, path, duration):
        """True peak of a file in dBTP (loudnorm measures it), or None."""
        cmd = [self.ffmpeg, "-hide_banner", "-nostats", "-progress", "pipe:1", "-f", "mp3", "-i", path,
               "-af", "loudnorm=print_format=json", "-f", "null", os.devnull]
        j = parse_loudnorm(self._run(cmd, duration, lambda f: None))
        try:
            return float(j["input_tp"])
        except (TypeError, KeyError, ValueError):
            return None

    def _worker(self, jobs, s, skipped):
        n = len(jobs)
        results = dict(done=0, failed=0, cancelled=0)
        first_out = None
        for k, (iid, src, info, out) in enumerate(jobs):
            if self.cancelled:
                self.ui(lambda i=iid: self._set_row(i, status="Canceled"))
                results["cancelled"] += 1
                continue
            name = os.path.basename(src)

            def on_progress(frac, stage="Converting", i=iid, k=k, name=name):
                # Stages after the main encode ("Checking peaks…", "Lowering peaks")
                # get their own label, so a long file never sits at "100%" looking stuck.
                text = stage if stage.endswith("…") else f"{stage} {frac * 100:.0f}%"
                overall = (k + (frac if stage == "Converting" else 1.0)) / n * 1000
                self.ui(lambda: (self._set_row(i, status=text),
                                 self.progress.config(value=overall),
                                 self.status.config(text=f"File {k + 1} of {n}: {name}")))
            on_progress(0.0)
            try:
                note = self._convert_one(src, info, out, s, on_progress)
            except Cancelled:
                self.ui(lambda i=iid: self._set_row(i, status="Canceled"))
                results["cancelled"] += 1
                continue
            except EncodeError as e:
                log_error(f"{src}\n{e}")
                self.ui(lambda i=iid, msg=str(e).splitlines()[-1]: self._set_row(i, status=f"Failed: {msg}"))
                results["failed"] += 1
                continue
            except Exception as e:  # keep going with the other files
                log_error(f"{src}\n{type(e).__name__}: {e}")
                self.ui(lambda i=iid, e=e: self._set_row(i, status=f"Failed: {e}"))
                results["failed"] += 1
                continue
            results["done"] += 1
            first_out = first_out or out
            size = os.path.getsize(out)
            status = f"Done — {fmt_size(size)}" + (f" ({note})" if note else "")
            self.ui(lambda i=iid, st=status: self._set_row(i, status=st))
        self.ui(lambda: self._finish(results, skipped, first_out))

    def _finish(self, r, skipped, first_out):
        self._set_busy(False)
        self.progress.config(value=1000 if r["done"] else 0)
        parts = [f"{r['done']} converted"]
        if skipped:
            parts.append(f"{skipped} skipped (already existed)")
        if r["failed"]:
            parts.append(f"{r['failed']} failed — see the list")
        if r["cancelled"]:
            parts.append(f"{r['cancelled']} canceled")
        self.status.config(text=("Done! " if not (r["failed"] or r["cancelled"]) else "") +
                           ", ".join(parts) + ".")
        if first_out and self.v["reveal"].get() and not self.cancelled:
            reveal_file(first_out)

    def _report_error(self, exc, val, tb):
        import traceback
        self._set_busy(False)
        detail = "".join(traceback.format_exception(exc, val, tb))
        log_error(detail)
        self.status.config(text=f"Unexpected error: {val}")
        messagebox.showerror("Unexpected error", detail[-1500:])

    def on_close(self):
        self.cancel()
        self.root.destroy()


def enable_drag_and_drop(root, on_drop, on_enter=None, on_leave=None):
    """Accept files dragged onto the window, using tkinterdnd2 if it's installed.
    Returns False when it isn't (or its native part can't load): the Browse
    buttons still work, so drag and drop is optional.

    on_drop gets the list of dropped paths. Every widget is registered, so a
    drop works anywhere in the window.
    """
    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        TkinterDnD.require(root)
    except Exception:
        return False

    def drop(event):
        # The paths come as a Tcl list: names with spaces are in {braces}.
        on_drop(list(root.tk.splitlist(event.data)))
        return event.action  # "copy": the files stay where they are

    def hook(fn):
        def handler(event):
            if fn:
                fn()
            return event.action
        return handler

    widgets, todo = [], list(root.winfo_children())
    while todo:
        w = todo.pop()
        widgets.append(w)
        todo += w.winfo_children()
    for w in widgets:
        w.drop_target_register(DND_FILES)
        w.dnd_bind("<<Drop>>", drop)
        w.dnd_bind("<<DropEnter>>", hook(on_enter))
        w.dnd_bind("<<DropLeave>>", hook(on_leave))
    return True


LOG_PATH = os.path.join(tempfile.gettempdir(), "audio_converter_errors.log")


def log_error(text):
    """Append an error to a log file. Errors can otherwise vanish when the
    script runs without a console (double-clicked / pythonw)."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{text}\n")
    except OSError:
        pass


def reveal_file(path):
    """Open the file's folder with the file selected."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except Exception:
        pass


def self_test():
    """Used by the release build: exit code 0 if FFmpeg and drag and drop both
    work in the packaged app, so a missing piece fails the build."""
    if not find_ffmpeg()[0]:
        return 1
    try:
        root = tk.Tk()
        root.withdraw()
        from tkinterdnd2 import TkinterDnD
        TkinterDnD.require(root)
        root.destroy()
    except Exception as e:
        log_error(f"Self-test failed: {type(e).__name__}: {e}")
        return 2
    return 0


SUPPORT_URL = "https://ko-fi.com/collinbrooksdev"


def support_link(parent, bg=None, font=None):
    """A small "Support this project" link that opens the Ko-fi page in the browser."""
    import webbrowser
    kw = {"bg": bg} if bg else {}
    link = tk.Label(parent, text="\u2665 Support this project", fg="#c8364f", cursor="hand2",
                    font=font or ("", 9, "underline"), **kw)
    link.bind("<Button-1>", lambda e: webbrowser.open(SUPPORT_URL))
    return link


# --------------------------------------------------------------------------- #
#  The look (shared by the Offline Toolkit apps): white cards, a dark purple
#  header fading to blue, blue actions. Only colors and fonts: no behavior.
# --------------------------------------------------------------------------- #
THEME = dict(
    bg="#ffffff", card="#ffffff", border="#e2ddf0", text="#1c1433", muted="#6e6787", error="#c8364f",
    purple="#2b1655", purple2="#4a2a8a", blue="#2f7cf6", blue_hover="#1d67dc", blue_soft="#e7f0ff",
    blue_text="#1756c4", lavender_line="#7b5cd6", disabled="#c9c4d8", tint="#f5f1ff")
MUTED, ERROR = THEME["muted"], THEME["error"]


def ui_family():
    try:
        import tkinter.font as tkfont
        families = set(tkfont.families())
    except tk.TclError:
        return "TkDefaultFont"
    for name in ("Segoe UI", "SF Pro Text", "Helvetica Neue", "Inter", "Cantarell", "DejaVu Sans"):
        if name in families:
            return name
    return "TkDefaultFont"


def _indicator_image(root, kind, on, enabled, n=18, k=4):
    """A checkbox (a blue box with a white tick) or radio button (a blue ring and dot),
    drawn pixel by pixel with smooth edges (no image library needed)."""
    t = THEME
    blue = t["blue"] if enabled else t["disabled"]
    edge = "#b9b0d6" if enabled else t["disabled"]

    def rgb(h):
        return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))

    def seg_dist(px, py, ax, ay, bx, by):
        dx, dy = bx - ax, by - ay
        u = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return ((px - ax - u * dx) ** 2 + (py - ay - u * dy) ** 2) ** 0.5

    def in_round_rect(x, y, r=4.6, lo=0.9, hi=n - 0.9):
        cx, cy = min(max(x, lo + r), hi - r), min(max(y, lo + r), hi - r)
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r

    def colour_at(x, y):
        """The color of one sample point, or None where it's see-through."""
        c = n / 2
        d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
        if kind == "check":
            if not in_round_rect(x, y):
                return None
            if on:
                tick = min(seg_dist(x, y, 4.8, 9.4, 7.8, 12.4), seg_dist(x, y, 7.8, 12.4, 13.4, 5.9))
                return "#ffffff" if tick <= 1.15 else blue
            return edge if not in_round_rect(x, y, 3.4, 2.3, n - 2.3) else "#ffffff"
        if d > n / 2 - 0.9:
            return None
        if on:
            return blue if d > n / 2 - 2.5 or d < 3.6 else "#ffffff"
        return edge if d > n / 2 - 2.3 else "#ffffff"

    img = tk.PhotoImage(master=root, width=n, height=n)
    for py in range(n):
        row = []
        for px in range(n):
            acc, hits = [0, 0, 0], 0
            for sy in range(k):
                for sx in range(k):
                    col = colour_at(px + (sx + 0.5) / k, py + (sy + 0.5) / k)
                    if col is not None:
                        r, g, b = rgb(col)
                        acc[0] += r
                        acc[1] += g
                        acc[2] += b
                        hits += 1
            if hits == 0:
                row.append(None)
                continue
            # Blend with white for partly covered edge pixels (the widgets sit on white).
            a = hits / (k * k)
            r, g, b = (round(acc[i] / hits * a + 255 * (1 - a)) for i in range(3))
            row.append("#%02x%02x%02x" % (r, g, b))
        for px, col in enumerate(row):
            if col is not None:
                img.put(col, (px, py))
    return img


def apply_theme(root):
    """ttk's "clam" theme, recoloured (the native Windows and macOS themes ignore colors)."""
    import tkinter.font as tkfont
    t, fam = THEME, ui_family()
    root.configure(bg=t["bg"])
    root.option_add("*Background", t["bg"])  # plain Tk widgets (pictures, links) sit on white too
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        try:
            tkfont.nametofont(name).configure(family=fam, size=10)
        except tk.TclError:
            pass
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=t["bg"], foreground=t["text"], font=(fam, 10), bordercolor=t["border"],
                focuscolor=t["blue"], lightcolor=t["card"], darkcolor=t["border"], troughcolor="#ece8f7")
    s.map(".", foreground=[("disabled", t["disabled"])])
    s.map("TLabel", background=[("disabled", t["card"])])  # clam grays a disabled label's background
    s.configure("TLabelframe", background=t["card"], bordercolor=t["border"], lightcolor=t["border"],
                darkcolor=t["border"], relief="solid", borderwidth=1)
    s.configure("TLabelframe.Label", background=t["card"], foreground=t["purple2"], font=(fam, 11, "bold"))
    for kind in ("TCheckbutton", "TRadiobutton"):
        s.configure(kind, background=t["card"], foreground=t["text"])
        s.map(kind, background=[("active", t["card"])], foreground=[("disabled", t["disabled"])])
    s.configure("TCombobox", fieldbackground="#ffffff", background="#ffffff", foreground=t["text"],
                arrowcolor=t["purple2"], bordercolor=t["border"], padding=(6, 3), selectbackground="#ffffff",
                selectforeground=t["text"])
    s.map("TCombobox", fieldbackground=[("readonly", "#ffffff"), ("disabled", "#f6f4fb")],
          foreground=[("disabled", t["disabled"])], arrowcolor=[("disabled", t["disabled"])],
          bordercolor=[("focus", t["blue"]), ("hover", t["lavender_line"])])
    s.configure("TEntry", fieldbackground="#ffffff", bordercolor=t["border"], padding=(6, 3))
    s.map("TEntry", bordercolor=[("focus", t["blue"])], fieldbackground=[("disabled", "#f6f4fb")])
    s.configure("TSpinbox", fieldbackground="#ffffff", bordercolor=t["border"], arrowcolor=t["purple2"], padding=(6, 3))
    s.configure("TButton", background="#ffffff", foreground=t["purple2"], bordercolor=t["border"],
                padding=(12, 5), font=(fam, 10, "bold"), relief="flat")
    s.map("TButton", background=[("disabled", t["card"]), ("pressed", t["tint"]), ("active", "#f7f4ff")],
          foreground=[("disabled", t["disabled"])], bordercolor=[("active", t["lavender_line"])])
    s.configure("Treeview", background="#ffffff", fieldbackground="#ffffff", foreground=t["text"],
                rowheight=26, font=(fam, 10))
    s.map("Treeview", background=[("selected", t["blue_soft"])], foreground=[("selected", t["blue_text"])])
    s.configure("Treeview.Heading", background="#f6f4fb", foreground=t["muted"], font=(fam, 9, "bold"),
                relief="flat", padding=(6, 4))
    s.map("Treeview.Heading", background=[("active", "#efeaf9")])
    s.configure("Horizontal.TProgressbar", troughcolor="#ece8f7", background=t["blue"], bordercolor="#ece8f7",
                lightcolor=t["blue"], darkcolor=t["blue"])
    for orient in ("Vertical", "Horizontal"):
        s.configure(f"{orient}.TScrollbar", background="#ddd6ee", troughcolor="#f6f4fb", bordercolor="#f6f4fb",
                    arrowcolor=t["muted"], lightcolor="#ddd6ee", darkcolor="#ddd6ee", gripcount=0)
    s.configure("Horizontal.TScale", background=t["blue"], troughcolor="#ece8f7", bordercolor=t["border"])
    root.option_add("*TCombobox*Listbox.background", "#ffffff")
    root.option_add("*TCombobox*Listbox.foreground", t["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", t["blue"])
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    # Checkboxes and radio buttons: drawn ticks and dots (clam draws a tick as an X).
    imgs = root._indicator_images = {(kind, on, en): _indicator_image(root, kind, on, en)
                                     for kind in ("check", "radio") for on in (False, True) for en in (False, True)}
    for kind, widget in (("check", "Checkbutton"), ("radio", "Radiobutton")):
        name = f"Toolkit.{kind}.indicator"
        try:
            s.element_create(name, "image", imgs[(kind, False, True)],
                             ("disabled", "selected", imgs[(kind, True, False)]),
                             ("disabled", imgs[(kind, False, False)]),
                             ("selected", imgs[(kind, True, True)]), padding=(0, 0, 6, 0), sticky="")
        except tk.TclError:
            pass
        s.layout(f"T{widget}", [(f"{widget}.padding", {"sticky": "nswe", "children": [
            (name, {"side": "left", "sticky": ""}),
            (f"{widget}.focus", {"side": "left", "sticky": "w", "children": [
                (f"{widget}.label", {"sticky": "nswe"})]})]})])
    return fam


class PillButton(tk.Canvas):
    """A rounded blue button for the app's main action. Accepts .config(state=..., text=...)
    like a ttk.Button, so the rest of the app drives it the same way."""

    def __init__(self, parent, text, command, height=36, padx=24):
        import tkinter.font as tkfont
        self._font = tkfont.Font(family=ui_family(), size=11, weight="bold")
        self._padx = padx
        self._bh = height
        self._bw = self._font.measure(text) + 2 * padx
        super().__init__(parent, width=self._bw, height=height, bg=THEME["card"], highlightthickness=0, bd=0,
                         cursor="hand2")
        self._text, self._command, self._enabled, self._hover = text, command, True, False
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._click)
        self._draw()

    def _draw(self):
        self.delete("all")
        t = THEME
        fill = t["disabled"] if not self._enabled else t["blue_hover"] if self._hover else t["blue"]
        r, w, h = self._bh // 2, self._bw, self._bh
        pts = [1 + r, 1, w - 1 - r, 1, w - 1, 1, w - 1, 1 + r, w - 1, h - 1 - r, w - 1, h - 1, w - 1 - r, h - 1,
               1 + r, h - 1, 1, h - 1, 1, h - 1 - r, 1, 1 + r, 1, 1]
        self.create_polygon(pts, smooth=True, fill=fill, outline=fill)
        self.create_text(w / 2, h / 2, text=self._text, fill="#ffffff", font=self._font)

    def _set_hover(self, on):
        self._hover = on
        self._draw()

    def _click(self, event):
        if self._enabled and 0 <= event.x <= self._bw and 0 <= event.y <= self._bh:
            self._command()

    def configure(self, cnf=None, **kw):
        if "state" in kw:
            self._enabled = kw.pop("state") != "disabled"
            super().configure(cursor="hand2" if self._enabled else "")
        if "text" in kw:
            self._text = kw.pop("text")
            self._bw = self._font.measure(self._text) + 2 * self._padx
            super().configure(width=self._bw)
        if "command" in kw:
            self._command = kw.pop("command")
        if cnf or kw:
            super().configure(cnf, **kw)
        self._draw()

    config = configure

    def cget(self, key):
        if key == "state":
            return "normal" if self._enabled else "disabled"
        if key == "text":
            return self._text
        return super().cget(key)

    def invoke(self):
        if self._enabled:
            self._command()


def app_header(parent, title, tagline, logo):
    """The app's bar: dark purple fading to blue, with its logo, name and what it does."""
    t, fam = THEME, ui_family()
    c = tk.Canvas(parent, height=60, highlightthickness=0, bd=0, bg=t["purple"])

    def draw(event=None):
        c.delete("all")
        w = max(c.winfo_width(), 300)
        a, b = (0x2b, 0x16, 0x55), (0x2f, 0x5c, 0xd6)
        steps = 48
        for i in range(steps):
            k = (i / (steps - 1)) ** 2.2  # mostly purple, turning blue on the right
            col = "#%02x%02x%02x" % tuple(round(a[j] + (b[j] - a[j]) * k) for j in range(3))
            c.create_rectangle(i * w / steps, 0, (i + 1) * w / steps + 1, 60, fill=col, outline=col)
        if not hasattr(c, "_logo"):  # the app's own icon
            c._logo = tk.PhotoImage(master=c, data=HEADER_ICON)
        c.create_image(35, 30, image=c._logo)
        c.create_text(68, 21, text=title, anchor="w", fill="#ffffff", font=(fam, 15, "bold"))
        c.create_text(69, 42, text=tagline, anchor="w", fill="#cfc6ff", font=(fam, 9))
    c.bind("<Configure>", draw)
    return c


# The app's icon (64 px PNG), for the window and the taskbar. The .exe and the Mac app get
# theirs from assets/ when they're built.
APP_ICON = (
    "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"
)

# The same icon, small, for the app's header bar.
HEADER_ICON = (
    "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"
)


def set_app_icon(root):
    try:
        if sys.platform == "win32":
            # Its own taskbar button and icon, not Python's, when run as a script.
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("OfflineToolkit.AudioConverter")
        root._app_icon = tk.PhotoImage(master=root, data=APP_ICON)
        root.iconphoto(True, root._app_icon)
    except Exception:
        pass  # just the default icon


def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    root = tk.Tk()
    apply_theme(root)
    set_app_icon(root)
    ConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
