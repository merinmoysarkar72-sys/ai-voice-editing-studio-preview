from __future__ import annotations
import json
import subprocess
import struct
import math
from array import array
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

_model = None


def get_model():
    """Load the first configured, installed multilingual Whisper implementation.

    faster-whisper is preferred for CPU int8 and word timestamps. OpenAI Whisper is
    a compatible fallback when it is installed. No mock transcript is ever returned.
    """
    global _model
    if _model is not None:
        return _model
    from .config import WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE
    errors = []
    try:
        from faster_whisper import WhisperModel
        _model = ("faster-whisper", WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE))
        return _model
    except Exception as exc:
        errors.append(f"faster-whisper: {exc}")
    try:
        import whisper
        _model = ("openai-whisper", whisper.load_model(WHISPER_MODEL, device=WHISPER_DEVICE))
        return _model
    except Exception as exc:
        errors.append(f"openai-whisper: {exc}")
    cpp_model = Path(os.getenv("WHISPER_CPP_MODEL_PATH", ""))
    if shutil.which("whisper-cli") and cpp_model.is_file():
        _model = ("whisper.cpp", cpp_model)
        return _model
    errors.append("whisper.cpp: install whisper-cli and set WHISPER_CPP_MODEL_PATH")
    raise RuntimeError("No compatible Whisper engine could be loaded. " + " | ".join(errors))


def _run(command: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, check=True, timeout=timeout)


def probe_audio(path: Path) -> dict[str, Any]:
    command = ["ffprobe", "-v", "error", "-show_entries", "format=duration,size:stream=codec_name,sample_rate,channels", "-of", "json", str(path)]
    result = _run(command, timeout=30)
    data = json.loads(result.stdout)
    stream = next((s for s in data.get("streams", []) if "codec_name" in s), {})
    return {
        "duration": float(data.get("format", {}).get("duration", 0)),
        "size": int(data.get("format", {}).get("size", path.stat().st_size)),
        "codec": stream.get("codec_name"),
        "sample_rate": stream.get("sample_rate"),
        "channels": stream.get("channels"),
    }


def _timestamp_seconds(value: str) -> float:
    try:
        h, m, sec = value.split(":")
        return int(h)*3600 + int(m)*60 + float(sec)
    except Exception:
        return 0.0


def transcribe(path: Path, language_hint: str = "auto") -> tuple[list[dict], str]:
    engine, model = get_model()
    rows = []
    if engine == "faster-whisper":
        segments_iter, info = model.transcribe(
            str(path), language=None if language_hint == "auto" else language_hint,
            beam_size=5, word_timestamps=True, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 350}, condition_on_previous_text=True,
        )
        for segment in segments_iter:
            words = [{"word": w.word, "start": float(w.start), "end": float(w.end), "probability": float(w.probability)} for w in (segment.words or [])]
            rows.append({"start": float(segment.start), "end": float(segment.end), "text": segment.text,
                         "avg_logprob": float(segment.avg_logprob), "words": words})
        return rows, getattr(info, "language", "unknown")

    if engine == "whisper.cpp":
        with tempfile.TemporaryDirectory(prefix="aves-whisper-") as tmp:
            output_base = str(Path(tmp) / "transcript")
            command = ["whisper-cli", "-m", str(model), "-f", str(path), "-oj", "-of", output_base, "-np"]
            if language_hint != "auto": command += ["-l", language_hint]
            proc = subprocess.run(command, capture_output=True, text=True, timeout=7200)
            json_path = Path(output_base + ".json")
            if proc.returncode or not json_path.exists():
                raise RuntimeError((proc.stderr or proc.stdout or "whisper.cpp transcription failed")[-1200:])
            data = json.loads(json_path.read_text(encoding="utf-8"))
            raw_segments = data.get("transcription", data.get("segments", []))
            for segment in raw_segments:
                offsets = segment.get("offsets", {})
                stamps = segment.get("timestamps", {})
                start = float(offsets.get("from", 0)) / 1000 if offsets else _timestamp_seconds(stamps.get("from", "00:00:00.000"))
                end = float(offsets.get("to", 0)) / 1000 if offsets else _timestamp_seconds(stamps.get("to", "00:00:00.000"))
                text = str(segment.get("text", ""))
                parts = text.split(); span = max(0.0, end-start)
                words = [{"word": word, "start": start+span*i/max(1,len(parts)), "end": start+span*(i+1)/max(1,len(parts)),
                          "probability": 0.55, "estimated_timestamp": True} for i,word in enumerate(parts)]
                rows.append({"start":start,"end":end,"text":text,"avg_logprob":0.0,"words":words,"word_timestamps_estimated":True})
            return rows, language_hint if language_hint != "auto" else data.get("result", {}).get("language", "unknown")

    # OpenAI Whisper fallback. It provides genuine segment timestamps; word-level
    # boundaries are conservatively interpolated and marked as estimated.
    result = model.transcribe(str(path), language=None if language_hint == "auto" else language_hint, fp16=False, verbose=False, word_timestamps=False)
    for segment in result.get("segments", []):
        text = str(segment.get("text", ""))
        parts = text.split()
        start, end = float(segment.get("start", 0)), float(segment.get("end", 0))
        span = max(0.0, end - start)
        words = [{"word": word, "start": start + span * i / max(1, len(parts)),
                  "end": start + span * (i + 1) / max(1, len(parts)), "probability": 0.55,
                  "estimated_timestamp": True} for i, word in enumerate(parts)]
        rows.append({"start": start, "end": end, "text": text,
                     "avg_logprob": float(segment.get("avg_logprob", 0)), "words": words,
                     "word_timestamps_estimated": True})
    return rows, result.get("language", "unknown")


def analyze_take_signal_quality(path: Path, candidates: list[dict], sample_rate: int = 4000) -> dict:
    """Measure real per-take RMS, peak, clipping and low-level noise indicators."""
    if not candidates:
        return {}
    command=["ffmpeg","-v","error","-i",str(path),"-vn","-ac","1","-ar",str(sample_rate),"-f","f32le","pipe:1"]
    proc=subprocess.run(command,capture_output=True,timeout=1800)
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode("utf-8","replace")[-500:] or "Could not decode audio for take-quality analysis.")
    samples=array("f"); samples.frombytes(proc.stdout[:len(proc.stdout)-(len(proc.stdout)%4)])
    if struct.pack("=I",1)!=struct.pack("<I",1): samples.byteswap()
    results={}
    for item in candidates:
        start=max(0,int(float(item["start"])*sample_rate)); end=min(len(samples),int(float(item["end"])*sample_rate))
        part=samples[start:end]
        if len(part)<max(80,int(sample_rate*.04)): continue
        squares=sum(float(x)*float(x) for x in part)
        rms=math.sqrt(squares/len(part)); peak=max(abs(float(x)) for x in part)
        clipped=sum(1 for x in part if abs(float(x))>=.995)/len(part)
        abs_sorted=sorted(abs(float(x)) for x in part)
        floor=abs_sorted[min(len(abs_sorted)-1,int(len(abs_sorted)*.10))]
        rms_db=20*math.log10(max(rms,1e-8)); peak_db=20*math.log10(max(peak,1e-8)); floor_db=20*math.log10(max(floor,1e-8))
        level_score=max(0.0,1.0-abs(rms_db+22.0)/42.0)
        noise_score=max(0.0,min(1.0,(-floor_db-18.0)/34.0))
        clipping_score=max(0.0,1.0-min(1.0,clipped*100.0))
        signal_score=.50*level_score+.25*noise_score+.25*clipping_score
        key=f"{float(item['start']):.3f}:{float(item['end']):.3f}"
        results[key]={"rms_db":round(rms_db,2),"peak_db":round(peak_db,2),"noise_floor_db":round(floor_db,2),
                      "clipping_ratio":round(clipped,5),"signal_score":round(signal_score,4)}
    return results


def waveform_peaks(path: Path, points: int = 1800) -> dict:
    """Return a compact peak envelope without decoding the full source in browser RAM."""
    points = max(100, min(int(points), 6000))
    command = ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "2000", "-f", "f32le", "pipe:1"]
    proc = subprocess.run(command, capture_output=True, timeout=900)
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-500:] or "Waveform decoding failed.")
    usable = len(proc.stdout) - (len(proc.stdout) % 4)
    samples = struct.unpack(f"<{usable // 4}f", proc.stdout[:usable]) if usable else ()
    if not samples:
        return {"peaks": [], "sample_rate": 2000, "points": 0}
    bucket = max(1, math.ceil(len(samples) / points))
    peaks = []
    for i in range(0, len(samples), bucket):
        part = samples[i:i + bucket]
        peaks.append(round(max((abs(x) for x in part), default=0.0), 4))
    return {"peaks": peaks, "sample_rate": 2000, "points": len(peaks), "duration": round(len(samples) / 2000, 3)}


def detect_silences(path: Path) -> list[dict]:
    command = ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "silencedetect=noise=-38dB:d=1.5", "-f", "null", "-"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    log = result.stderr
    import re
    starts = [float(v) for v in re.findall(r"silence_start: ([0-9.]+)", log)]
    ends = [float(v) for v in re.findall(r"silence_end: ([0-9.]+)", log)]
    items = []
    for index, start in enumerate(starts):
        end = ends[index] if index < len(ends) else start
        items.append({"start": round(start, 3), "end": round(end, 3), "duration": round(max(0, end - start), 3)})
    return items



def analyze_audio_quality(path: Path) -> dict[str, Any]:
    """Measure whole-file peak/average level and clipping using FFmpeg.

    These are objective signal measurements, not claims about pronunciation or
    subjective naturalness. Noise quality is not inferred from loudness alone.
    """
    command = ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    import re
    log = result.stderr
    mean = re.search(r"mean_volume:\s*(-?inf|[-0-9.]+)\s*dB", log)
    peak = re.search(r"max_volume:\s*(-?inf|[-0-9.]+)\s*dB", log)
    clipped = re.search(r"\b([0-9]+)\s+ near clipping", log)
    def level(match):
        if not match:
            return None
        return None if match.group(1) == "-inf" else float(match.group(1))
    return {
        "mean_volume_db": level(mean),
        "peak_volume_db": level(peak),
        "near_clipping_samples": int(clipped.group(1)) if clipped else 0,
        "clipping_detected": bool(clipped and int(clipped.group(1)) > 0),
        "measurement": "FFmpeg volumedetect; does not estimate intelligibility or SNR",
    }



def replace_audio_segment(source: Path, replacement: Path, output: Path, start: float, end: float,
                          take_start: float = 0.0, take_end: float | None = None) -> dict:
    """Replace one source interval with a take, time-fitting it without changing total duration."""
    source_info, take_info = probe_audio(source), probe_audio(replacement)
    duration = float(source_info["duration"])
    take_end = float(take_end if take_end is not None else take_info["duration"])
    target_len, take_len = float(end-start), float(take_end-take_start)
    if start < 0 or end <= start or end > duration + 0.02:
        raise ValueError("Replacement target must be inside the recording.")
    if take_start < 0 or take_end <= take_start or take_end > float(take_info["duration"]) + 0.02:
        raise ValueError("Selected take range is outside the replacement audio.")
    ratio = take_len / target_len
    if not 0.5 <= ratio <= 2.0:
        raise ValueError("This take differs too much in duration. Choose a closer-length take (0.5× to 2×).")
    filters, labels = [], []
    if start > 0.015:
        filters.append(f"[0:a]atrim=start=0:end={start:.4f},asetpts=PTS-STARTPTS[pre]")
        labels.append("[pre]")
    filters.append(f"[1:a]atrim=start={take_start:.4f}:end={take_end:.4f},asetpts=PTS-STARTPTS,atempo={ratio:.6f},apad,atrim=duration={target_len:.4f}[replacement]")
    labels.append("[replacement]")
    if end < duration - 0.015:
        filters.append(f"[0:a]atrim=start={end:.4f}:end={duration:.4f},asetpts=PTS-STARTPTS[post]")
        labels.append("[post]")
    if len(labels) > 1:
        filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1,aresample=44100,aformat=channel_layouts=mono[out]")
    else:
        filters.append("[replacement]aresample=44100,aformat=channel_layouts=mono[out]")
    command=["ffmpeg","-y","-hide_banner","-loglevel","error","-filter_complex_threads","1","-i",str(source),"-i",str(replacement),
             "-filter_complex",";".join(filters),"-map","[out]","-c:a","pcm_s16le",str(output)]
    _run(command, timeout=1800)
    meta=probe_audio(output)
    if abs(meta["duration"]-duration) > 0.18:
        output.unlink(missing_ok=True)
        raise RuntimeError("Replacement changed the full recording duration unexpectedly.")
    return {"duration":meta["duration"],"size":meta["size"],"target_start":start,"target_end":end,
            "take_start":take_start,"take_end":take_end,"time_stretch_ratio":round(ratio,4)}


def export_audio(source: Path, out_path: Path, selected_rows: list[dict], options: dict) -> dict:
    """Build an edit directly in FFmpeg; the full uncompressed recording is not loaded into Python RAM."""
    if not selected_rows:
        raise ValueError("No approved matched sentences were selected for export.")

    sample_rate = int(options.get("sample_rate", 44100))
    channels = int(options.get("channels", 1))
    channel_layout = "mono" if channels == 1 else "stereo"
    noise_mode = options.get("noise_reduction", "light")
    pause_mode = options.get("pause_mode", "balanced")
    pause_limits = {"natural": (260, 1200), "balanced": (120, 650), "tight": (70, 360)}
    lower_pause, upper_pause = pause_limits.get(pause_mode, pause_limits["balanced"])

    # Flatten sentence ranges in script order. Each range represents contiguous
    # matched spoken words; gaps can contain detected stumbles or extra speech.
    ranges = []
    keep_ranges = options.get("timeline_ranges")
    for row in sorted(selected_rows, key=lambda x: int(x["index"])):
        row_ranges = row.get("audio_ranges") or [{"start": row.get("start"), "end": row.get("end")}]
        for item in row_ranges:
            if item.get("start") is None or item.get("end") is None:
                continue
            a, b = float(item["start"]), float(item["end"])
            intersections = [(a, b)]
            if keep_ranges is not None:
                intersections = [(max(a, float(k["start"])), min(b, float(k["end"]))) for k in keep_ranges if min(b, float(k["end"])) > max(a, float(k["start"]))]
            for left, right in intersections:
                start = max(0.0, left - 0.075)
                end = right + 0.110
                if end - start >= 0.06:
                    ranges.append({"start": start, "end": end})
    ranges.sort(key=lambda x: x["start"])
    split_points = sorted(float(x) for x in (options.get("timeline_splits") or []))
    if split_points:
        split_ranges = []
        for item in ranges:
            cuts = [x for x in split_points if item["start"] + 0.04 < x < item["end"] - 0.04]
            left = item["start"]
            for cut in cuts:
                split_ranges.append({"start": left, "end": cut})
                left = cut
            split_ranges.append({"start": left, "end": item["end"]})
        ranges = split_ranges
    if not ranges:
        raise ValueError("The selected sections did not produce usable audio.")

    filters = []
    if noise_mode == "light":
        source_filter = f"highpass=f=65,afftdn=nr=8:nf=-50:tn=1,lowpass=f=15000,aresample={sample_rate},aformat=channel_layouts={channel_layout}"
    elif noise_mode == "medium":
        source_filter = f"highpass=f=65,afftdn=nr=14:nf=-50:tn=1,lowpass=f=15000,aresample={sample_rate},aformat=channel_layouts={channel_layout}"
    else:
        source_filter = f"aresample={sample_rate},aformat=channel_layouts={channel_layout}"

    # Split one decoded/filter-cleaned stream into per-range streams.
    split_outputs = "".join(f"[source{i}]" for i in range(len(ranges)))
    filters.append(f"[0:a]{source_filter},asplit={len(ranges)}{split_outputs}")
    concat_inputs = []
    previous_end = None
    output_duration = 0.0
    for index, item in enumerate(ranges):
        duration = item["end"] - item["start"]
        fade_out_at = max(0.0, duration - 0.025)
        filters.append(
            f"[source{index}]atrim=start={item['start']:.3f}:end={item['end']:.3f},"
            f"asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.020,"
            f"afade=t=out:st={fade_out_at:.3f}:d=0.025[clip{index}]"
        )
        if index:
            original_gap = max(0.0, item["start"] - float(previous_end or 0))
            if original_gap > 0.04:
                pause_ms = int(max(lower_pause, min(upper_pause, original_gap * 1000)))
                pause_seconds = pause_ms / 1000
                filters.append(f"anullsrc=r={sample_rate}:cl={channel_layout},atrim=duration={pause_seconds:.3f}[pause{index}]")
                concat_inputs.append(f"[pause{index}]")
                output_duration += pause_seconds
        concat_inputs.append(f"[clip{index}]")
        output_duration += duration
        previous_end = item["end"]

    concat_label = "assembled"
    filters.append(f"{''.join(concat_inputs)}concat=n={len(concat_inputs)}:v=0:a=1[{concat_label}]")
    if options.get("normalize", True):
        filters.append(f"[{concat_label}]loudnorm=I=-16:TP=-1.5:LRA=11[final]")
    else:
        filters.append(f"[{concat_label}]anull[final]")

    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-filter_complex_threads", "1",
        "-i", str(source), "-filter_complex", ";".join(filters), "-map", "[final]",
        "-ar", str(sample_rate), "-ac", str(channels), "-vn",
    ]
    if options.get("format") == "mp3":
        command += ["-c:a", "libmp3lame", "-b:a", options.get("bitrate", "192k")]
    else:
        command += ["-c:a", "pcm_s16le"]
    command.append(str(out_path))
    try:
        _run(command, timeout=1800)
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or "").strip().splitlines()
        raise RuntimeError(details[-1] if details else "FFmpeg could not build the edited audio.") from exc

    metadata = probe_audio(out_path)
    source_duration = float(probe_audio(source)["duration"])
    kept_duration = source_duration if keep_ranges is None else sum(max(0.0, float(x["end"])-float(x["start"])) for x in keep_ranges)
    return {
        "duration": round(metadata["duration"], 2),
        "size": metadata["size"],
        "selected_segments": len(ranges),
        "selected_sentences": len(selected_rows),
        "noise_reduction": noise_mode,
        "normalized": bool(options.get("normalize", True)),
        "timeline_removed_seconds": round(max(0.0, source_duration-kept_duration), 3),
    }
