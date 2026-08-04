#!/usr/bin/env python3
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import tempfile
import threading
import traceback
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from faster_whisper import WhisperModel


COMMON_FFMPEG_DIRS = [
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/local/bin",
    "/usr/bin",
    "/bin",
]


@dataclass
class SegmentChunk:
    index: int
    start_sec: float
    end_sec: float
    path: Path


@dataclass
class TranscribedSegment:
    start_sec: float
    end_sec: float
    text: str


def seconds_to_ts(seconds: float) -> str:
    ms_total = int(round(seconds * 1000))
    td = timedelta(milliseconds=ms_total)
    total = int(td.total_seconds())
    ms = ms_total % 1000
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _find_binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for base in COMMON_FFMPEG_DIRS:
        candidate = Path(base) / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def resolve_ffmpeg_bins() -> tuple[str | None, str | None]:
    ffmpeg_bin = _find_binary("ffmpeg")
    ffprobe_bin = _find_binary("ffprobe")
    return ffmpeg_bin, ffprobe_bin


def ffprobe_duration(input_path: Path, ffprobe_bin: str) -> float:
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(input_path),
    ]
    out = subprocess.check_output(cmd, text=True).strip()
    return float(out)


def transcode_to_wav(input_path: Path, output_wav: Path, ffmpeg_bin: str) -> None:
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-sample_fmt",
        "s16",
        str(output_wav),
    ]
    subprocess.run(cmd, check=True)


def cut_chunks(
    wav_path: Path,
    chunk_minutes: int,
    overlap_seconds: float,
    work_dir: Path,
    ffmpeg_bin: str,
    ffprobe_bin: str,
) -> list[SegmentChunk]:
    duration = ffprobe_duration(wav_path, ffprobe_bin=ffprobe_bin)
    chunk_len = chunk_minutes * 60
    chunks: list[SegmentChunk] = []
    idx = 0
    cursor = 0.0

    while cursor < duration:
        clip_start = max(0.0, cursor - overlap_seconds if idx > 0 else cursor)
        clip_end = min(duration, cursor + chunk_len + overlap_seconds)
        clip_duration = max(0.0, clip_end - clip_start)

        out = work_dir / f"chunk_{idx:04d}.wav"
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            str(wav_path),
            "-ss",
            f"{clip_start:.3f}",
            "-t",
            f"{clip_duration:.3f}",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            str(out),
        ]
        subprocess.run(cmd, check=True)

        chunks.append(SegmentChunk(index=idx, start_sec=clip_start, end_sec=clip_end, path=out))
        idx += 1
        cursor += chunk_len

    return chunks


def transcribe_chunks(
    chunks: list[SegmentChunk],
    model_name: str,
    language: str,
    log,
) -> list[TranscribedSegment]:
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    merged: list[TranscribedSegment] = []

    for chunk in chunks:
        log(f"转写分段 {chunk.index + 1}/{len(chunks)} ...")
        segments, _ = model.transcribe(
            str(chunk.path),
            language=language,
            beam_size=5,
            best_of=5,
            vad_filter=True,
            word_timestamps=False,
            condition_on_previous_text=True,
            temperature=0,
        )

        for seg in segments:
            abs_start = chunk.start_sec + float(seg.start)
            abs_end = chunk.start_sec + float(seg.end)
            text = seg.text.strip()
            if not text:
                continue
            merged.append(TranscribedSegment(start_sec=abs_start, end_sec=abs_end, text=text))

    merged.sort(key=lambda x: x.start_sec)

    deduped: list[TranscribedSegment] = []
    for seg in merged:
        if not deduped:
            deduped.append(seg)
            continue
        prev = deduped[-1]
        same_text = prev.text == seg.text
        overlap = seg.start_sec < prev.end_sec
        if same_text and overlap:
            if seg.end_sec > prev.end_sec:
                prev.end_sec = seg.end_sec
            continue
        if same_text and abs(seg.start_sec - prev.start_sec) < 0.8:
            if seg.end_sec > prev.end_sec:
                prev.end_sec = seg.end_sec
            continue
        deduped.append(seg)

    return deduped


def write_txt_with_timestamps(segments: list[TranscribedSegment], out_path: Path) -> None:
    with out_path.open("w", encoding="utf-8") as f:
        for seg in segments:
            f.write(f"[{seconds_to_ts(seg.start_sec)} - {seconds_to_ts(seg.end_sec)}] {seg.text}\n")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("录音提取工具（faster-whisper）")
        self.root.geometry("820x560")

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar(value=str(Path.home() / "Desktop"))
        self.chunk_var = tk.IntVar(value=15)
        self.overlap_var = tk.DoubleVar(value=2.0)
        self.mode_var = tk.StringVar(value="balanced")
        self.diarization_var = tk.BooleanVar(value=False)

        self.ffmpeg_bin, self.ffprobe_bin = resolve_ffmpeg_bins()

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker: threading.Thread | None = None

        self._build_ui()
        self._poll_log_queue()

    def _build_ui(self):
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="录音文件").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.input_var, width=80).grid(row=1, column=0, sticky="we", padx=(0, 8))
        ttk.Button(frame, text="选择文件", command=self.pick_input).grid(row=1, column=1, sticky="e")

        ttk.Label(frame, text="输出目录").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(frame, textvariable=self.output_var, width=80).grid(row=3, column=0, sticky="we", padx=(0, 8))
        ttk.Button(frame, text="选择目录", command=self.pick_output).grid(row=3, column=1, sticky="e")

        opts = ttk.Frame(frame)
        opts.grid(row=4, column=0, columnspan=2, sticky="we", pady=(12, 8))

        ttk.Label(opts, text="模式").grid(row=0, column=0, sticky="w")
        mode = ttk.Combobox(opts, textvariable=self.mode_var, values=["balanced", "accurate"], state="readonly", width=12)
        mode.grid(row=1, column=0, sticky="w", padx=(0, 12))

        ttk.Label(opts, text="分段分钟").grid(row=0, column=1, sticky="w")
        ttk.Spinbox(opts, from_=5, to=30, textvariable=self.chunk_var, width=8).grid(row=1, column=1, sticky="w", padx=(0, 12))

        ttk.Label(opts, text="重叠秒数").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(opts, from_=0, to=10, increment=0.5, textvariable=self.overlap_var, width=8).grid(row=1, column=2, sticky="w", padx=(0, 12))

        diar = ttk.Checkbutton(
            opts,
            text="说话人分离（暂未启用，需 HuggingFace Token）",
            variable=self.diarization_var,
            state=tk.DISABLED,
        )
        diar.grid(row=1, column=3, sticky="w")

        self.start_btn = ttk.Button(frame, text="开始提取", command=self.start)
        self.start_btn.grid(row=5, column=0, sticky="w", pady=(0, 10))

        ttk.Label(frame, text="运行日志").grid(row=6, column=0, sticky="w")
        self.log_text = tk.Text(frame, height=18, wrap=tk.WORD)
        self.log_text.grid(row=7, column=0, columnspan=2, sticky="nsew")

        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(7, weight=1)

    def pick_input(self):
        path = filedialog.askopenfilename(
            title="选择录音文件",
            filetypes=[
                ("Audio/Video", "*.m4a *.mp3 *.wav *.aac *.mp4 *.mov *.caf"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.input_var.set(path)

    def pick_output(self):
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.output_var.set(path)

    def log(self, msg: str):
        self.log_queue.put(msg)

    def _poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.insert(tk.END, msg + "\n")
                self.log_text.see(tk.END)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log_queue)

    def start(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "任务正在运行中")
            return

        input_path = Path(self.input_var.get().strip())
        output_dir = Path(self.output_var.get().strip())

        if not input_path.exists():
            messagebox.showerror("错误", "录音文件不存在")
            return
        if not output_dir.exists():
            messagebox.showerror("错误", "输出目录不存在")
            return

        self.ffmpeg_bin, self.ffprobe_bin = resolve_ffmpeg_bins()
        if not self.ffmpeg_bin or not self.ffprobe_bin:
            messagebox.showerror("错误", "未检测到 ffmpeg/ffprobe，请安装后重试")
            return

        self.start_btn.config(state=tk.DISABLED)
        self.log_text.delete("1.0", tk.END)

        self.worker = threading.Thread(target=self._run_pipeline, daemon=True)
        self.worker.start()

    def _run_pipeline(self):
        try:
            input_path = Path(self.input_var.get().strip())
            output_dir = Path(self.output_var.get().strip())
            chunk_minutes = int(self.chunk_var.get())
            overlap_seconds = float(self.overlap_var.get())
            model_name = "medium" if self.mode_var.get() == "balanced" else "large-v3"

            stem = input_path.stem
            out_txt = output_dir / f"{stem}_transcript.txt"

            with tempfile.TemporaryDirectory(prefix="luyintiqu_") as tmp:
                tmp_dir = Path(tmp)
                wav_path = tmp_dir / "input_16k_mono.wav"

                self.log("开始转码为 16k 单声道 WAV...")
                transcode_to_wav(input_path, wav_path, ffmpeg_bin=self.ffmpeg_bin)

                self.log("开始固定时长切段...")
                chunks = cut_chunks(
                    wav_path,
                    chunk_minutes=chunk_minutes,
                    overlap_seconds=overlap_seconds,
                    work_dir=tmp_dir,
                    ffmpeg_bin=self.ffmpeg_bin,
                    ffprobe_bin=self.ffprobe_bin,
                )
                self.log(f"切段完成，共 {len(chunks)} 段")

                self.log(f"加载模型：{model_name}（中文）...")
                segments = transcribe_chunks(chunks, model_name=model_name, language="zh", log=self.log)

                self.log("写出 txt（含时间戳）...")
                write_txt_with_timestamps(segments, out_txt)

            self.log(f"完成，输出文件：{out_txt}")
            self.root.after(0, lambda: messagebox.showinfo("完成", f"转写完成\n{out_txt}"))

        except Exception as e:
            self.log("发生错误：" + str(e))
            self.log(traceback.format_exc())
            self.root.after(0, lambda: messagebox.showerror("错误", str(e)))
        finally:
            self.root.after(0, lambda: self.start_btn.config(state=tk.NORMAL))


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
