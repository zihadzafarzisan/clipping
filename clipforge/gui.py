from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .config import DATA, Settings
from .media import validate_source
from .pipeline import Pipeline, safe_error, validate_options
from .publish import check_facebook, check_tiktok, check_youtube, connect_tiktok, connect_youtube
from .store import Store

BG = "#0e1420"
PANEL = "#182131"
TEXT = "#eef3fa"
MUTED = "#a6b4c8"
ACCENT = "#76efbc"


def open_path(path):
    path = Path(path).resolve()
    if not path.exists():
        raise ValueError("This job has not created output yet.")
    if os.name == "nt":
        os.startfile(str(path))
    else:
        webbrowser.open(path.as_uri())


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ClipForge · Stream to shorts")
        self.geometry("1180x860")
        self.minsize(1040, 780)
        self.configure(bg=BG)
        self.settings = Settings()
        self.store = Store()
        self.store.recover()
        self.pipeline = Pipeline(self.store, self.settings)
        self.events = queue.Queue()
        self.last_signature = None
        self.clip_map = {}
        self.style_ui()
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=30, pady=(25, 15))
        tk.Label(header, text="CLIPFORGE", font=("Segoe UI", 13, "bold"), fg=ACCENT, bg=BG).pack(anchor="w")
        tk.Label(header, text="One link. Your next shorts.", font=("Segoe UI", 25, "bold"), fg=TEXT, bg=BG).pack(anchor="w", pady=(7, 4))
        tk.Label(header, text="Find spoken highlights, add timed subtitles, and publish to connected accounts.",
                 font=("Segoe UI", 11), fg=MUTED, bg=BG).pack(anchor="w")
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=30, pady=(0, 22))
        self.create_tab = ttk.Frame(self.tabs, padding=20)
        self.library_tab = ttk.Frame(self.tabs, padding=20)
        self.settings_tab = ttk.Frame(self.tabs, padding=20)
        self.tabs.add(self.create_tab, text="  Create shorts  ")
        self.tabs.add(self.library_tab, text="  Clip library  ")
        self.tabs.add(self.settings_tab, text="  Settings & accounts  ")
        self.build_create()
        self.build_library()
        self.build_settings()
        self.pipeline.start()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(300, self.refresh)

    def style_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        self.option_add("*Font", ("Segoe UI", 10))
        style.configure(".", background=PANEL, foreground=TEXT, fieldbackground=BG, bordercolor="#334158")
        style.configure("TFrame", background=PANEL)
        style.configure("TLabel", font=("Segoe UI", 10), background=PANEL, foreground=TEXT)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(12, 10))
        style.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", TEXT)])
        style.configure("TButton", padding=(13, 8), background="#29384f", foreground=TEXT)
        style.map("TButton", background=[("active", "#3b506f")])
        style.configure("Accent.TButton", background=ACCENT, foreground=BG, font=("Segoe UI", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#a0ffd5")], foreground=[("active", BG)])
        style.configure("TEntry", padding=8, fieldbackground=BG, foreground=TEXT)
        style.configure("TSpinbox", padding=7, fieldbackground=BG, foreground=TEXT, arrowsize=15)
        style.configure("TCombobox", padding=7, fieldbackground=BG, foreground=TEXT, arrowsize=15)
        style.map("TCombobox", fieldbackground=[("readonly", BG)], foreground=[("readonly", TEXT)])
        style.configure("TCheckbutton", background=PANEL, foreground=TEXT)
        style.configure("Treeview", background=BG, fieldbackground=BG, foreground=TEXT, rowheight=31, borderwidth=0)
        style.configure("Treeview.Heading", background="#263348", foreground=MUTED, font=("Segoe UI", 10, "bold"), padding=7)
        style.map("Treeview", background=[("selected", "#324864")], foreground=[("selected", TEXT)])
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=BG, borderwidth=0)

    def build_create(self):
        frame = self.create_tab
        ttk.Label(frame, text="STREAM OR VIDEO LINK", style="Muted.TLabel").pack(anchor="w", pady=(0, 7))
        line = ttk.Frame(frame)
        line.pack(fill="x")
        self.source = tk.StringVar(value=self.settings.get("sample_link"))
        ttk.Entry(line, textvariable=self.source).pack(side="left", fill="x", expand=True, padx=(0, 10))
        ttk.Button(line, text="Choose file", command=self.choose_file).pack(side="right")
        options = ttk.Frame(frame)
        options.pack(fill="x", pady=18)
        self.option_vars = {}
        for col, (key, label, default, low, high) in enumerate([
            ("start_minutes", "Start at minute · recordings", "0", 0, 1440),
            ("minutes", "Capture window · minutes", "10", 1, 120),
            ("clip_count", "Maximum shorts", "3", 1, 10),
            ("clip_seconds", "Maximum short · seconds", "40", 15, 60),
        ]):
            box = ttk.Frame(options)
            box.grid(row=0, column=col, sticky="ew", padx=(0, 14) if col < 3 else 0)
            options.columnconfigure(col, weight=1)
            ttk.Label(box, text=label, style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
            var = tk.StringVar(value=default)
            self.option_vars[key] = var
            ttk.Spinbox(box, from_=low, to=high, textvariable=var, width=12).pack(fill="x")
        row = ttk.Frame(frame)
        row.pack(fill="x")
        self.mode = tk.StringVar(value="Local · speech and audio")
        self.layout = tk.StringVar(value="Full gameplay · blurred background")
        for name, variable, choices in (
            ("Highlight selection", self.mode, ["Local · speech and audio", "AI · OpenAI API key required"]),
            ("Vertical framing", self.layout, ["Full gameplay · blurred background", "Fill frame · center crop"]),
        ):
            box = ttk.Frame(row)
            box.pack(side="left", fill="x", expand=True, padx=(0, 12))
            ttk.Label(box, text=name, style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
            ttk.Combobox(box, textvariable=variable, values=choices, state="readonly").pack(fill="x")
        publish_row = ttk.Frame(frame)
        publish_row.pack(fill="x", pady=(14, 7))
        self.engagement_only = tk.BooleanVar(value=True)
        ttk.Checkbutton(publish_row, text="Only download high-engagement clips (Most Replayed heatmap / audio peaks)",
                        variable=self.engagement_only).pack(anchor="w", pady=(0, 8))
        dest_row = ttk.Frame(frame)
        dest_row.pack(fill="x", pady=(0, 6))
        ttk.Label(dest_row, text="POST AUTOMATICALLY", style="Muted.TLabel").pack(side="left", padx=(0, 20))
        self.youtube = tk.BooleanVar(value=False)
        self.facebook = tk.BooleanVar(value=False)
        self.tiktok = tk.BooleanVar(value=False)
        ttk.Checkbutton(dest_row, text="YouTube Shorts", variable=self.youtube).pack(side="left", padx=(0, 18))
        ttk.Checkbutton(dest_row, text="Facebook Reels", variable=self.facebook).pack(side="left", padx=(0, 18))
        ttk.Checkbutton(dest_row, text="TikTok", variable=self.tiktok).pack(side="left")
        ttk.Label(frame, text="TikTok also exports local MP4 + caption files as a backup in the job's tiktok folder.", style="Muted.TLabel").pack(anchor="w")
        action = ttk.Frame(frame)
        action.pack(fill="x", pady=(14, 16))
        ttk.Button(action, text="Create shorts", style="Accent.TButton", command=self.create_job).pack(side="left")
        ttk.Label(action, text="Selected accounts upload after rendering. Leave unchecked to export only.", style="Muted.TLabel").pack(side="left", padx=16)
        ttk.Separator(frame).pack(fill="x", pady=(0, 12))
        ttk.Label(frame, text="JOBS", style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        columns = ("status", "window", "message")
        self.jobs_tree = ttk.Treeview(frame, columns=columns, show="headings", height=4, selectmode="browse")
        for key, title, width in (("status", "Status", 110), ("window", "Window", 120), ("message", "Progress", 650)):
            self.jobs_tree.heading(key, text=title)
            self.jobs_tree.column(key, width=width, stretch=key == "message")
        self.jobs_tree.pack(fill="both", expand=True)
        self.jobs_tree.bind("<<TreeviewSelect>>", lambda event: self.show_job())
        self.progress = ttk.Progressbar(frame, maximum=1)
        self.progress.pack(fill="x", pady=(10, 5))
        self.job_message = tk.StringVar(value="Ready. The first transcription downloads a local speech model.")
        ttk.Label(frame, textvariable=self.job_message, style="Muted.TLabel", wraplength=980).pack(anchor="w")
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(10, 0))
        for text, command in (("Open output folder", self.open_job), ("Cancel selected", self.cancel_selected), ("Resume selected", self.resume_selected)):
            ttk.Button(actions, text=text, command=command).pack(side="left", padx=(0, 9))

    def build_library(self):
        ttk.Label(self.library_tab, text="FINISHED CLIPS", style="Muted.TLabel").pack(anchor="w", pady=(0, 12))
        self.clips_tree = ttk.Treeview(self.library_tab, columns=("title", "seconds", "yt", "fb", "tt"), show="headings", height=10)
        for key, text, width in (("title", "Title", 450), ("seconds", "Length", 70), ("yt", "YouTube", 110), ("fb", "Facebook", 110), ("tt", "TikTok", 110)):
            self.clips_tree.heading(key, text=text)
            self.clips_tree.column(key, width=width, stretch=key == "title")
        self.clips_tree.pack(fill="both", expand=True)
        self.clips_tree.bind("<<TreeviewSelect>>", lambda event: self.show_clip())
        self.clips_tree.bind("<Double-1>", lambda event: self.preview_clip())
        self.clip_details = tk.Text(self.library_tab, height=8, bg=BG, fg=TEXT, font=("Segoe UI", 10),
                                    relief="flat", wrap="word", padx=12, pady=12, state="disabled")
        self.clip_details.pack(fill="x", pady=15)
        controls = ttk.Frame(self.library_tab)
        controls.pack(fill="x")
        ttk.Button(controls, text="Preview selected", command=self.preview_clip).pack(side="left", padx=(0, 10))
        ttk.Button(controls, text="Open clip folder", command=self.open_clip_folder).pack(side="left", padx=(0, 10))
        ttk.Button(controls, text="Open TikTok upload", command=self.open_tiktok).pack(side="left", padx=(0, 10))
        ttk.Button(controls, text="Copy caption", command=self.copy_caption).pack(side="left")
        ttk.Label(self.library_tab, text="Uploads that need checking are never automatically repeated, to avoid duplicate posts.", style="Muted.TLabel").pack(anchor="w", pady=14)

    def build_settings(self):
        frame = self.settings_tab
        self.setting_vars, self.secret_vars = {}, {}
        ttk.Label(frame, text="TRANSCRIPTION & PUBLISHING ACCOUNTS", style="Muted.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        entries = [
            ("whisper_model", "Speech model", ["tiny", "base", "small", "medium", "large-v3-turbo"], True),
            ("language", "Speech language (blank=auto, hinglish, hi-en)", ["", "hinglish", "hi-en", "hi", "en", "bn"], False),
            ("subtitle_prompt", "Subtitle prompt (optional vocabulary hint)", None, False),
            ("highlight_model", "AI highlight model (optional)", None, False),
            ("youtube_client_file", "Google OAuth Desktop client JSON", None, False),
            ("youtube_privacy", "YouTube visibility", ["public", "unlisted", "private"], True),
            ("facebook_page_id", "Facebook Page ID", None, False),
            ("facebook_api_version", "Facebook API version", None, False),
            ("tiktok_client_key", "TikTok Client Key", None, False),
            ("tiktok_post_mode", "TikTok post mode (inbox=draft, direct=publish)", ["inbox", "direct"], True),
            ("tiktok_privacy", "TikTok privacy", ["SELF_ONLY", "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR"], True),
        ]
        for row, (key, label, values, readonly) in enumerate(entries, 1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 14))
            var = tk.StringVar(value=self.settings.get(key))
            self.setting_vars[key] = var
            if values is not None:
                state = "readonly" if readonly else "normal"
                control = ttk.Combobox(frame, textvariable=var, values=values, state=state)
            else:
                control = ttk.Entry(frame, textvariable=var)
            control.grid(row=row, column=1, sticky="ew", pady=3)
            if key == "youtube_client_file":
                ttk.Button(frame, text="Browse", command=self.choose_google_client).grid(row=row, column=2, padx=8)
        row = len(entries) + 1
        secret_entries = (
            ("openai_api_key", "OpenAI API key (AI mode only)"),
            ("facebook_page_token", "Facebook Page access token"),
            ("tiktok_client_secret", "TikTok Client Secret"),
            ("tiktok_access_token", "TikTok access token (or use Connect TikTok)"),
        )
        for key, label in secret_entries:
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 14))
            var = tk.StringVar(value=self.settings.secret(key))
            self.secret_vars[key] = var
            ttk.Entry(frame, textvariable=var, show="•").grid(row=row, column=1, sticky="ew", pady=3)
            row += 1
        frame.columnconfigure(1, weight=1)
        controls = ttk.Frame(frame)
        controls.grid(row=row, column=0, columnspan=3, sticky="w", pady=10)
        ttk.Button(controls, text="Save settings", style="Accent.TButton", command=self.save_settings).pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="Connect YouTube", command=self.connect_google).pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="Check YouTube", command=self.check_google).pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="Check Facebook", command=self.check_meta).pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="Connect TikTok", command=self.connect_tt).pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="Check TikTok", command=self.check_tt).pack(side="left")
        self.account_status = tk.StringVar(value=self.connection_status())
        ttk.Label(frame, textvariable=self.account_status, wraplength=950, style="Muted.TLabel").grid(row=row+1, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Label(frame, text="Speech runs locally. Use 'hinglish' for Romanized Hindi+English, 'hi-en' for Devanagari+English, or blank for auto.\n"
                  "Credentials are encrypted with Windows DPAPI. TikTok supports Content Posting API (inbox draft or direct) plus local export.",
                  wraplength=950, style="Muted.TLabel").grid(row=row+2, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Button(frame, text="Open setup guide", command=lambda: open_path(Path(__file__).resolve().parent.parent / "README.md")).grid(row=row+3, column=0, sticky="w")

    def connection_status(self):
        yt = "connected" if self.settings.secret("youtube_credentials") else "not connected"
        fb = "token saved; verify access" if self.settings.secret("facebook_page_token") else "not connected"
        tt = "connected" if (self.settings.secret("tiktok_access_token") or self.settings.secret("tiktok_refresh_token")) else "export only"
        return f"YouTube: {yt}   ·   Facebook: {fb}   ·   TikTok: {tt}"

    def choose_file(self):
        path = filedialog.askopenfilename(filetypes=[("Video", "*.mp4 *.mkv *.mov *.webm *.ts"), ("All files", "*.*")])
        if path:
            self.source.set(path)

    def choose_google_client(self):
        path = filedialog.askopenfilename(filetypes=[("Google client JSON", "*.json")])
        if path:
            self.setting_vars["youtube_client_file"].set(path)

    def save_settings(self, notify=True):
        values = {key: variable.get().strip() for key, variable in self.setting_vars.items()}
        secrets = {key: variable.get().strip() for key, variable in self.secret_vars.items()}
        self.settings.save(values, secrets)
        self.account_status.set(self.connection_status())
        if notify:
            messagebox.showinfo("Settings saved", "Settings saved on this PC.")

    def background(self, operation, success):
        def work():
            try:
                result = operation()
                self.events.put(("success", success if result is None else f"{success}: {result}"))
            except Exception as error:
                self.events.put(("error", safe_error(error)))
        threading.Thread(target=work, daemon=True).start()

    def connect_google(self):
        self.save_settings(notify=False)
        self.account_status.set("Complete the YouTube authorization in your browser.")
        self.background(lambda: connect_youtube(self.settings), "YouTube connected")

    def check_google(self):
        self.save_settings(notify=False)
        self.account_status.set("Checking YouTube channel access…")
        self.background(lambda: check_youtube(self.settings), "YouTube channel accessible")

    def check_meta(self):
        self.save_settings(notify=False)
        self.account_status.set("Checking Facebook Page access…")
        self.background(lambda: check_facebook(self.settings), "Facebook Page accessible")

    def connect_tt(self):
        self.save_settings(notify=False)
        self.account_status.set("Complete the TikTok authorization in your browser.")
        self.background(lambda: connect_tiktok(self.settings), "TikTok connected")

    def check_tt(self):
        self.save_settings(notify=False)
        self.account_status.set("Checking TikTok creator account…")
        self.background(lambda: check_tiktok(self.settings), "TikTok account accessible")

    def create_job(self):
        try:
            source = self.source.get().strip()
            validate_source(source)
            options = {key: variable.get() for key, variable in self.option_vars.items()}
            options.update(layout="fit" if self.layout.get().startswith("Full") else "crop",
                highlight_mode="local" if self.mode.get().startswith("Local") else "ai",
                engagement_only=bool(self.engagement_only.get()),
                destinations=(["youtube"] if self.youtube.get() else [])
                + (["facebook"] if self.facebook.get() else [])
                + (["tiktok"] if self.tiktok.get() else []))
            options = validate_options(options)
            self.pipeline.validate_connections(options)
            job_id = self.store.create(source, options)
            self.job_message.set(f"Queued job {job_id}")
            self.refresh_jobs()
            self.jobs_tree.selection_set(job_id)
        except Exception as error:
            messagebox.showerror("Cannot start job", safe_error(error))

    def selected_job(self):
        selected = self.jobs_tree.selection()
        return self.store.job(selected[0]) if selected else None

    def show_job(self):
        job = self.selected_job()
        if job:
            self.progress["value"] = job["progress"]
            self.job_message.set(job["message"])

    def open_job(self):
        job = self.selected_job()
        if job:
            try:
                open_path(DATA / "jobs" / job["id"])
            except Exception as error:
                messagebox.showinfo("Output folder", str(error))

    def cancel_selected(self):
        job = self.selected_job()
        if job:
            self.pipeline.cancel_job(job["id"])

    def resume_selected(self):
        job = self.selected_job()
        if not job or job["status"] not in ("failed", "cancelled", "interrupted"):
            return
        self.store.update(job["id"], "queued", "Resuming from saved progress.")

    def selected_clip(self):
        selected = self.clips_tree.selection()
        return self.clip_map.get(selected[0]) if selected else None

    def show_clip(self):
        clip = self.selected_clip()
        if not clip:
            return
        details = clip["description"] + "\n\n" + "\n".join(
            f"{d['platform'].title()}: {d['status']} — {d['message']}" + (f"\n{d['url']}" if d["url"] else "")
            for d in clip["deliveries"])
        self.clip_details.configure(state="normal")
        self.clip_details.delete("1.0", "end")
        self.clip_details.insert("1.0", details)
        self.clip_details.configure(state="disabled")

    def preview_clip(self):
        clip = self.selected_clip()
        if clip:
            open_path(clip["path"])

    def open_clip_folder(self):
        clip = self.selected_clip()
        if clip:
            open_path(Path(clip["path"]).parent)

    def open_tiktok(self):
        clip = self.selected_clip()
        if clip:
            open_path(DATA / "jobs" / clip["job_id"] / "tiktok")
        webbrowser.open("https://www.tiktok.com/tiktokstudio/upload")

    def copy_caption(self):
        clip = self.selected_clip()
        if clip:
            self.clipboard_clear()
            self.clipboard_append(clip["description"])

    def refresh_jobs(self):
        for job in self.store.jobs():
            options = json.loads(job["options"])
            values = (job["status"].replace("_", " ").title(), f"{options.get('minutes', '?')} min", job["message"])
            if self.jobs_tree.exists(job["id"]):
                self.jobs_tree.item(job["id"], values=values)
            else:
                self.jobs_tree.insert("", 0, iid=job["id"], values=values)
        self.show_job()

    def refresh(self):
        while not self.events.empty():
            kind, text = self.events.get_nowait()
            self.account_status.set(self.connection_status())
            if kind == "error":
                messagebox.showerror("Connection failed", text)
            else:
                messagebox.showinfo("Account connection", text)
        self.refresh_jobs()
        clips = self.store.clips()
        signature = json.dumps(clips, sort_keys=True)
        if signature != self.last_signature:
            self.last_signature = signature
            for clip in clips:
                self.clip_map[clip["id"]] = clip
                deliveries = {d["platform"]: d["status"].replace("_", " ") for d in clip["deliveries"]}
                values = (clip["title"], f"{clip['end']-clip['start']:.0f}s", deliveries.get("youtube", "export only"),
                          deliveries.get("facebook", "export only"), deliveries.get("tiktok", "exported"))
                if self.clips_tree.exists(clip["id"]):
                    self.clips_tree.item(clip["id"], values=values)
                else:
                    self.clips_tree.insert("", "end", iid=clip["id"], values=values)
            self.show_clip()
        self.after(1000, self.refresh)

    def close(self):
        self.pipeline.shutdown()
        self.job_message.set("Stopping safely. Any in-flight account request may take a moment to return.")
        self.wait_for_shutdown()

    def wait_for_shutdown(self):
        if self.pipeline.thread and self.pipeline.thread.is_alive():
            self.after(200, self.wait_for_shutdown)
        else:
            self.destroy()


def launch():
    App().mainloop()
