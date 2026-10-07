"""Local, fail-closed 1.5x pacing pilot. Writes only to a new V: test directory."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


TOOL_VERSION = 6
DECISION_POLICY_VERSION = 4
WHISPER_VERSION = "1.2.1"
MODEL_NAME = "small.en"
MODEL_REVISION = "d1d751a5f8271d482d14ca55d9e2deeebbae577f"
CPU_THREADS = 2
GAP_SPLIT_SECONDS = 0.7
MIN_WINDOW_WORDS = 8
MIN_WINDOW_ROWS = 8
MAX_WINDOW_ROWS = 12
PACE_WINDOW_EXCLUSION_REASON = "pace-eligible words excluded from meaningful phrase windows"
ALIGNMENT_MISMATCH_REASON = "ASR transcript/alignment word-count mismatch"
LOW_WORD_PROB = 0.40
MIN_MEAN_PROB = 0.60
MAX_LOW_CONF_FRACTION = 0.10
WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)?")
SPECS = (("783", "Alliance"), ("880", "Horde"))
KINDS = ("accept", "complete")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest().upper()


def write_new(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def write_json_new(path: Path, value: object) -> None:
    write_new(path, (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"))


def copy_new(source: Path, target: Path) -> None:
    with source.open("rb") as src, target.open("xb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())


def inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_paths(stage: Path, output: Path, model_cache: Path) -> None:
    stage, output, model_cache = (p.resolve() for p in (stage, output, model_cache))
    if not stage.is_dir() or not (stage / "manifest.json").is_file():
        raise ValueError("--stage must be the existing full-corpus stage with manifest.json")
    if output.exists():
        raise FileExistsError(f"Output already exists; refusing to reuse or overwrite: {output}")
    if any(inside(a, b) or inside(b, a) for a, b in ((output, stage), (output, model_cache), (stage, model_cache))):
        raise ValueError("stage, output, and model cache must be separate trees")
    if any(part.casefold() in {"world of warcraft", "interface", "addons"} for path in (output, model_cache) for part in path.parts):
        raise ValueError("output must not be under a WoW Interface/AddOns path")
    if os.name == "nt":
        if output.drive.upper() != "V:" or model_cache.drive.upper() != "V:":
            raise ValueError("output and model cache must be on V:")
        if stage.drive.upper() != "V:":
            raise ValueError("the full-corpus stage must be on V:")
    if not output.parent.is_dir() or not model_cache.is_dir():
        raise ValueError("output parent and model-cache directory must already exist")


def load_inputs(stage: Path) -> tuple[dict, list[dict]]:
    manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("Status") not in {"Built", "Deployed", "Restored"}:
        raise ValueError("full-corpus manifest is not in a complete built state")
    records = {}
    for item in manifest.get("Audio", []):
        key = (item.get("Pack"), item.get("RelativePath"))
        if key in records:
            raise ValueError(f"duplicate full-stage manifest entry: {key}")
        records[key] = item
    selected = []
    for quest, pack in SPECS:
        for kind in KINDS:
            relative = f"generated/sounds/quests/{quest}-{kind}.ogg"
            item = records.get((pack, relative))
            if not item or item.get("State") != "Built":
                raise ValueError(f"missing built original for {pack}/{relative}")
            original = stage / "original" / pack / Path(relative)
            faster = stage / "faster" / pack / Path(relative)
            if sha256(original) != item.get("OriginalSha256", "").upper():
                raise ValueError(f"original snapshot hash mismatch: {pack}/{relative}")
            if sha256(faster) != item.get("FasterSha256", "").upper():
                raise ValueError(f"validated faster snapshot hash mismatch: {pack}/{relative}")
            for field in ("OriginalDuration", "FasterDuration"):
                if not math.isfinite(float(item.get(field, 0))) or float(item[field]) <= 0:
                    raise ValueError(f"invalid {field} for {pack}/{relative}")
            selected.append({"quest": quest, "pack": pack, "kind": kind, "relative": relative,
                             "source": original, "faster_source": faster, "record": item})
    return manifest, selected


def decode16k(path: Path):
    import av
    import numpy as np

    chunks = []
    with av.open(str(path), mode="r") as container:
        streams = [stream for stream in container.streams if stream.type == "audio"]
        if len(streams) != 1 or streams[0].codec_context.name != "vorbis":
            raise ValueError(f"expected one Vorbis stream: {path}")
        stream = streams[0]
        rate, channels = int(stream.codec_context.sample_rate), len(stream.codec_context.layout.channels)
        resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        raise ValueError(f"decoded no audio samples: {path}")
    audio = np.concatenate(chunks).astype(np.float32, copy=False)
    if not np.isfinite(audio).all():
        raise ValueError(f"non-finite decoded samples: {path}")
    return audio, len(audio) / 16000.0, rate, channels


def cache_identity(source_hash: str) -> dict:
    return {"schema": TOOL_VERSION, "decision_policy_version": DECISION_POLICY_VERSION,
            "source_sha256": source_hash, "model": MODEL_NAME,
            "model_revision": MODEL_REVISION, "faster_whisper": WHISPER_VERSION,
            "language": "en", "cpu_threads": CPU_THREADS, "compute_type": "int8",
            "vad_filter": True, "word_timestamps": True, "beam_size": 5,
            "temperature": 0.0, "condition_on_previous_text": False,
            "segment_text_join": "space-separated-segments",
            "gap_split_seconds": GAP_SPLIT_SECONDS, "minimum_window_words": MIN_WINDOW_WORDS,
            "minimum_window_rows": MIN_WINDOW_ROWS, "maximum_window_rows": MAX_WINDOW_ROWS,
            "pace_window_partition": "balanced-whisper-rows; block-exclusions-below-70-percent-coverage",
            "pace_timestamp_filter": "finite-in-range rows; monotonicity is review-only",
            "low_word_probability": LOW_WORD_PROB, "minimum_mean_probability": MIN_MEAN_PROB,
            "max_low_confidence_fraction": MAX_LOW_CONF_FRACTION}


def classify_word_timings(words: list[dict], duration: float) -> tuple[list[dict], list[dict], list[str]]:
    timed, pace_eligible, reasons = [], [], []
    previous_start = previous_end = -1.0
    for word in words:
        if word["count"] == 0:
            continue
        start, end = word["start"], word["end"]
        if start is None or end is None:
            reasons.append("missing word timestamp")
            continue
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start <= end <= duration + 0.5):
            reasons.append("invalid word timestamp")
            continue
        pace_eligible.append(word)
        if start < previous_start or end < previous_end:
            reasons.append("non-monotonic word timestamps")
            continue
        previous_start, previous_end = start, end
        timed.append(word)
    return timed, pace_eligible, sorted(set(reasons))


def join_transcript_segments(text_parts: list[str]) -> str:
    return " ".join(text.strip() for text in text_parts if text and text.strip())


def pace_metrics(words: list[dict]) -> tuple[list[float], list[int], list[float], int]:
    atoms, current, pauses = [], [], []
    for word in words:
        if current:
            gap = word["start"] - current[-1]["end"]
            if gap >= GAP_SPLIT_SECONDS:
                pauses.append(gap)
                atoms.append(current)
                current = []
        current.append(word)
    if current:
        atoms.append(current)

    rates, sizes = [], []
    excluded_word_count = 0
    for atom in atoms:
        row_count = len(atom)
        if row_count < MIN_WINDOW_ROWS:
            excluded_word_count += sum(word["count"] for word in atom)
            continue
        chunks = math.ceil(row_count / MAX_WINDOW_ROWS)
        if row_count < chunks * MIN_WINDOW_ROWS:
            # A 13-15 row atom cannot be split into two valid windows; retain
            # one maximum-size window and explicitly review the short tail.
            targets = [MAX_WINDOW_ROWS]
        else:
            base, remainder = divmod(row_count, chunks)
            targets = [base + (i < remainder) for i in range(chunks)]
        index = 0
        for target_rows in targets:
            window = atom[index:index + target_rows]
            index += len(window)
            lexical_count = sum(word["count"] for word in window)
            active = window[-1]["end"] - window[0]["start"]
            if len(window) < MIN_WINDOW_ROWS or active <= 0:
                excluded_word_count += lexical_count
                continue
            rates.append(60.0 * lexical_count / active)
            sizes.append(lexical_count)
        excluded_word_count += sum(word["count"] for word in atom[index:])
    return rates, sizes, pauses, excluded_word_count


def decide_factor(reference: dict, result: dict, ratio: float, comfort_band: float) -> dict:
    """Apply the calibrated reference/upper-tail policy to already measured pace data."""
    target_median = ceiling_p90 = factor = None
    reasons = list(result["review_reasons"])
    if not reasons and not reference["review_reasons"] and reference["median_phrase_wpm"] and reference["p90_phrase_wpm"]:
        target_median = reference["median_phrase_wpm"] * ratio
        ceiling_p90 = reference["p90_phrase_wpm"] * ratio * (1.0 + comfort_band)
        median, p90 = result["median_phrase_wpm"], result["p90_phrase_wpm"]
        if p90 and p90 > ceiling_p90:
            reasons.append("original fast passages exceed speed-up-only ceiling")
        elif median and p90 and abs(median - target_median) / target_median <= comfort_band:
            factor = 1.0
        elif median and p90:
            factor = max(1.0, min(1.5, target_median / median, ceiling_p90 / p90))
        else:
            reasons.append("missing pace metrics")
    else:
        reasons.extend(reference["review_reasons"])
    return {"target_median_wpm": target_median, "ceiling_p90_wpm": ceiling_p90,
            "factor": factor, "status": "ready" if factor is not None else "review",
            "review_reasons": sorted(set(reasons))}


def run_synthetic_tests() -> None:
    continuous = [{"start": i * 0.3, "end": i * 0.3 + 0.2, "count": 1} for i in range(100)]
    rates, sizes, pauses, excluded = pace_metrics(continuous)
    if (len(rates) != 9 or sum(sizes) + excluded != len(continuous)
            or any(size < MIN_WINDOW_ROWS or size > MAX_WINDOW_ROWS for size in sizes) or pauses or excluded):
        raise AssertionError("continuous-speech pace-window cap test failed")
    weighted = [{"start": i * 0.3, "end": i * 0.3 + 0.2, "count": 2 if i == 11 else 1}
                for i in range(100)]
    rates, sizes, pauses, excluded = pace_metrics(weighted)
    if (len(rates) != 9 or 13 not in sizes or sum(sizes) + excluded != sum(word["count"] for word in weighted)
            or pauses or excluded):
        raise AssertionError("weighted ASR-row overshoot bound test failed")
    tail_weighted = [{"start": i * 0.3, "end": i * 0.3 + 0.2, "count": 2 if i < 6 else 1}
                     for i in range(94)]
    rates, sizes, pauses, excluded = pace_metrics(tail_weighted)
    if (sum(word["count"] for word in tail_weighted) != 100 or sum(sizes) != 100
            or excluded != 0 or pauses or len(rates) != 8):
        raise AssertionError("100-lexical-word weighted tail accounting test failed")
    short_tail = ([{"start": i * 0.3, "end": i * 0.3 + 0.2, "count": 1} for i in range(8)] +
                  [{"start": 5.0 + i * 0.3, "end": 5.2 + i * 0.3, "count": 1} for i in range(4)])
    rates, sizes, pauses, excluded = pace_metrics(short_tail)
    if len(rates) != 1 or sizes != [8] or excluded != 4 or sum(sizes) + excluded != 12:
        raise AssertionError("short pause-separated tail must be explicitly excluded")
    split = ([{"start": i * 0.3, "end": i * 0.3 + 0.2, "count": 1} for i in range(8)] +
             [{"start": 5.0 + i * 0.3, "end": 5.2 + i * 0.3, "count": 1} for i in range(8)])
    rates, sizes, pauses, excluded = pace_metrics(split)
    if len(rates) != 2 or sizes != [8, 8] or len(pauses) != 1 or pauses[0] < GAP_SPLIT_SECONDS or excluded:
        raise AssertionError("long-pause separation test failed")
    points = [{"start": 0.0, "end": 0.0, "count": 1} for _ in range(8)]
    point_metrics = pace_metrics(points)
    if point_metrics[0] or point_metrics[3] != 8:
        raise AssertionError("point timestamps must not produce zero-span pace windows")
    reference = {"median_phrase_wpm": 100.0, "p90_phrase_wpm": 120.0, "review_reasons": []}
    decision = decide_factor(reference, reference, 1.5, 0.10)
    if decision["factor"] != 1.5 or decision["target_median_wpm"] != 150.0:
        raise AssertionError("factor cap/reference-ratio policy test failed")
    too_fast = {"median_phrase_wpm": 180.0, "p90_phrase_wpm": 220.0, "review_reasons": []}
    decision = decide_factor(reference, too_fast, 1.5, 0.10)
    if decision["factor"] is not None or "speed-up-only ceiling" not in decision["review_reasons"][0]:
        raise AssertionError("fast-passage review policy test failed")
    within_band = {"median_phrase_wpm": 150.0, "p90_phrase_wpm": 180.0, "review_reasons": []}
    if decide_factor(reference, within_band, 1.5, 0.10)["factor"] != 1.0:
        raise AssertionError("within-band no-op policy test failed")
    uncertain_reference = reference | {"review_reasons": ["uncertain reference"]}
    if decide_factor(uncertain_reference, within_band, 1.5, 0.10)["factor"] is not None:
        raise AssertionError("uncertain reference must block factor decisions")
    boundary_join = join_transcript_segments(["hello", "world"])
    if len(WORD_RE.findall(boundary_join)) != 2 or len(WORD_RE.findall("hello" + "world")) != 1:
        raise AssertionError("Whisper segment joining must preserve lexical boundaries")


def analyze(model, item: dict, duration: float) -> dict:
    segments, info = model.transcribe(item["source_audio"], language="en", beam_size=5, temperature=0.0,
                                      word_timestamps=True, vad_filter=True,
                                      condition_on_previous_text=False)
    words, text_parts = [], []
    for segment in segments:
        text_parts.append(segment.text)
        for word in segment.words or []:
            raw = word.word.strip()
            count = len(WORD_RE.findall(raw))
            words.append({"text": raw, "start": float(word.start), "end": float(word.end),
                          "probability": float(word.probability), "count": int(count)})
    transcript = join_transcript_segments(text_parts)
    transcript_count = len(WORD_RE.findall(transcript))
    timed, pace_eligible, reasons = classify_word_timings(words, duration)
    timed_count = sum(word["count"] for word in timed)
    raw_coverage = timed_count / transcript_count if transcript_count else None
    coverage = min(1.0, raw_coverage) if raw_coverage is not None else 0.0
    alignment_count_exceeds_transcript = timed_count > transcript_count
    if alignment_count_exceeds_transcript:
        reasons.append(ALIGNMENT_MISMATCH_REASON)
    probabilities = [word["probability"] for word in timed if word["probability"] is not None]
    if len(probabilities) != len(timed):
        reasons.append("missing word confidence")
    mean_prob = sum(probabilities) / len(probabilities) if probabilities else 0.0
    low_count = sum(p < LOW_WORD_PROB for p in probabilities)
    if mean_prob < MIN_MEAN_PROB or (probabilities and low_count / len(probabilities) > MAX_LOW_CONF_FRACTION):
        reasons.append("low word confidence")
    if getattr(info, "language", None) != "en" or float(getattr(info, "language_probability", 0.0)) < 0.80:
        reasons.append("uncertain English language detection")
    if coverage < 0.90:
        reasons.append("insufficient word-timestamp coverage")

    rates, window_sizes, pauses, excluded_window_word_count = pace_metrics(pace_eligible)
    pace_word_count = sum(word["count"] for word in pace_eligible)
    window_coverage = sum(window_sizes) / pace_word_count if pace_word_count else 0.0
    if excluded_window_word_count and window_coverage < 0.70:
        reasons.append(PACE_WINDOW_EXCLUSION_REASON)
    if not rates:
        reasons.append("insufficient meaningful phrase windows")
    if window_coverage < 0.70:
        reasons.append("insufficient meaningful phrase-window coverage")
    if transcript_count < MIN_WINDOW_WORDS or timed_count < MIN_WINDOW_WORDS:
        reasons.append("insufficient words for pace decision")
    import numpy as np
    median = float(np.median(rates)) if rates else None
    p90 = float(np.percentile(rates, 90)) if rates else None
    return {"transcript": transcript, "words": words, "decoded_seconds": duration,
            "transcript_word_count": transcript_count, "timestamped_word_count": timed_count,
            "word_coverage": coverage, "raw_word_coverage_ratio": raw_coverage,
            "aligned_word_count_exceeds_transcript": alignment_count_exceeds_transcript,
            "mean_word_probability": mean_prob,
            "low_confidence_words": low_count, "language": getattr(info, "language", None),
            "language_probability": float(getattr(info, "language_probability", 0.0)),
            "phrase_window_wpm": rates, "phrase_window_word_counts": window_sizes,
            "phrase_window_word_coverage": window_coverage,
            "pace_eligible_word_count": pace_word_count,
            "phrase_window_word_count": sum(window_sizes),
            "excluded_window_word_count": excluded_window_word_count,
            "median_phrase_wpm": median, "p90_phrase_wpm": p90,
            "long_pause_count": len(pauses), "long_pause_total_seconds": sum(pauses),
            "long_pause_p90_seconds": float(np.percentile(pauses, 90)) if pauses else 0.0,
            "overall_wpm_including_pauses": 60.0 * transcript_count / duration,
            "review_reasons": sorted(set(reasons))}


def run(command: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=timeout,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {command[0]}\n{result.stderr[-4000:]}")
    return result


def probe(ffprobe: Path, path: Path) -> dict:
    data = json.loads(run([str(ffprobe), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], 60).stdout)
    streams = data.get("streams", [])
    if len(streams) != 1 or streams[0].get("codec_type") != "audio":
        raise ValueError(f"expected one audio stream: {path}")
    return {"codec": streams[0].get("codec_name"), "sample_rate": int(streams[0]["sample_rate"]),
            "channels": int(streams[0]["channels"]), "duration": float(data["format"]["duration"])}


def encode_ogg(ffmpeg: Path, source: Path, target: Path, factor: float) -> None:
    run([str(ffmpeg), "-hide_banner", "-v", "error", "-nostdin", "-n", "-xerror", "-threads", "1",
         "-filter_threads", "1", "-i", str(source), "-map", "0:a:0", "-vn", "-af", f"atempo={factor:.9f}",
         "-c:a", "libvorbis", "-q:a", "5", "-threads", "1", str(target)])


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze four staged quest clips and create local A/B/C previews.")
    parser.add_argument("--stage", type=Path, required=True, help="existing full-corpus stage (read only)")
    parser.add_argument("--output", type=Path, required=True, help="new V: pilot output directory; never overwritten")
    parser.add_argument("--model-cache", type=Path, required=True, help="existing V: Faster-Whisper cache root")
    parser.add_argument("--ffmpeg", type=Path, help="ffmpeg.exe; sibling ffprobe.exe is required for previews")
    parser.add_argument("--comfort-band", type=float, default=0.10)
    args = parser.parse_args()
    try:
        run_synthetic_tests()
        if os.name != "nt":
            raise RuntimeError("this installation-specific pilot is Windows-only")
        if not 0 <= args.comfort_band <= 0.25:
            raise ValueError("--comfort-band must be between 0 and 0.25")
        stage, output, model_cache = (p.resolve() for p in (args.stage, args.output, args.model_cache))
        validate_paths(stage, output, model_cache)
        whisper_version = importlib.metadata.version("faster-whisper")
        if whisper_version != WHISPER_VERSION:
            raise RuntimeError(f"requires faster-whisper {WHISPER_VERSION}; found {whisper_version}")
        ffmpeg = args.ffmpeg.resolve() if args.ffmpeg else None
        ffprobe = ffmpeg.with_name("ffprobe.exe") if ffmpeg and os.name == "nt" else (ffmpeg.with_name("ffprobe") if ffmpeg else None)
        if ffmpeg and (not ffmpeg.is_file() or not ffprobe or not ffprobe.is_file()):
            raise FileNotFoundError("--ffmpeg and its sibling ffprobe must both exist")
        manifest, items = load_inputs(stage)
        # Re-decode and hash-verify all four original snapshots before inference.
        for item in items:
            item["source_sha256"] = sha256(item["source"])
            audio, duration, rate, channels = decode16k(item["source"])
            if sha256(item["source"]) != item["source_sha256"]:
                raise ValueError(f"source changed during decode: {item['relative']}")
            record = item["record"]
            if (rate, channels) != (int(record["SampleRate"]), int(record["Channels"])):
                raise ValueError(f"source format changed: {item['pack']}/{item['relative']}")
            if abs(duration - float(record["OriginalDuration"])) > 0.10 + duration * 0.01:
                raise ValueError(f"decoded duration disagrees with manifest: {item['relative']}")
            item["source_audio"], item["duration"], item["sample_rate"], item["channels"] = audio, duration, rate, channels

        cache_dir = model_cache / "adaptive-analysis-v3"
        if cache_dir.is_symlink() or (cache_dir.exists() and cache_dir.resolve() != cache_dir):
            raise ValueError("analysis cache must not be a redirected directory")
        cache_dir.mkdir(exist_ok=True)
        runtime_versions = {package: importlib.metadata.version(package)
                            for package in ("av", "numpy", "ctranslate2", "onnxruntime")}
        identities = [cache_identity(item["source_sha256"]) | {"runtime_versions": runtime_versions} for item in items]
        cache_paths = [cache_dir / (hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest() + ".json") for identity in identities]
        analyses = [None] * len(items)
        for i, path in enumerate(cache_paths):
            if path.exists():
                cached = json.loads(path.read_text(encoding="utf-8"))
                if cached.get("identity") != identities[i]:
                    raise ValueError(f"analysis cache identity mismatch: {path}")
                analyses[i] = cached["analysis"]
        if any(result is None for result in analyses):
            from faster_whisper import WhisperModel
            model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8", cpu_threads=CPU_THREADS,
                                 revision=MODEL_REVISION, local_files_only=True,
                                 download_root=str(model_cache))
            for i, item in enumerate(items):
                if analyses[i] is None:
                    print(f"Analyzing quest {item['quest']} {item['kind']} ({item['pack']})", flush=True)
                    result = analyze(model, item, item["duration"])
                    analyses[i] = result
                    write_json_new(cache_paths[i], {"identity": identities[i], "analysis": result})
                else:
                    print(f"Using cached analysis for quest {item['quest']} {item['kind']} ({item['pack']})", flush=True)
        ref_index = next(i for i, item in enumerate(items) if item["quest"] == "880" and item["kind"] == "accept")
        reference = analyses[ref_index]
        ratio = float(items[ref_index]["record"]["OriginalDuration"]) / float(items[ref_index]["record"]["FasterDuration"])
        if not 1.30 <= ratio <= 1.60:
            raise ValueError(f"reference original/faster duration ratio is outside the validated range: {ratio:.6f}")
        decisions = []
        for item, result in zip(items, analyses):
            policy = decide_factor(reference, result, ratio, args.comfort_band)
            decisions.append({"quest": item["quest"], "pack": item["pack"], "kind": item["kind"],
                              "source_sha256": item["source_sha256"], "metrics": result,
                              **policy})
        # Text/transcripts stay local; they contain copyrighted game dialogue.
        output.mkdir()
        report = {"schema": TOOL_VERSION, "status": "review_required" if any(d["factor"] is None for d in decisions) else "analysis_complete",
                  "stage": str(stage), "model": MODEL_NAME, "faster_whisper": whisper_version,
                  "model_revision": MODEL_REVISION, "runtime_versions": runtime_versions,
                  "device": "cpu-int8", "cpu_threads": CPU_THREADS,
                  "temperature": 0.0, "condition_on_previous_text": False,
                  "vad": "faster-whisper 1.2.1 integrated Silero defaults",
                  "reference": {"quest": "880", "kind": "accept", "ratio": ratio,
                                "ratio_source": "manifest measured original/faster durations; scaled median is an approximation, not a faster-audio transcript"},
                  "comfort_band": args.comfort_band, "decisions": decisions}
        write_json_new(output / "analysis.json", report)
        if any(d["factor"] is None for d in decisions):
            print(f"Review required; no candidate audio written. Report: {output / 'analysis.json'}", file=sys.stderr)
            return 2
        if not ffmpeg:
            print(f"Analysis complete; no candidates requested. Report: {output / 'analysis.json'}")
            return 0

        artifacts = []
        for item, decision in zip(items, decisions):
            prefix = f"quest{item['quest']}-{item['kind']}-{item['pack'].lower()}"
            variants = (("original", item["source"], 1.0, True),
                        ("blanket-1.5x", item["source"], 1.5, False),
                        ("adaptive", item["source"], float(decision["factor"]), decision["factor"] == 1.0))
            for label, source, factor, exact_copy in variants:
                if sha256(source) != item["source_sha256"]:
                    raise ValueError("source changed after analysis")
                ogg = output / f"{prefix}-{label}.ogg"
                if exact_copy:
                    copy_new(source, ogg)
                else:
                    encode_ogg(ffmpeg, source, ogg, factor)
                if sha256(source) != item["source_sha256"]:
                    raise ValueError("source changed during generation")
                if exact_copy and sha256(ogg) != item["source_sha256"]:
                    raise ValueError(f"byte-exact copy verification failed: {ogg}")
                info = probe(ffprobe, ogg)
                if info["codec"] != "vorbis" or (info["sample_rate"], info["channels"]) != (item["sample_rate"], item["channels"]):
                    raise ValueError(f"candidate format changed: {ogg}")
                expected = item["duration"] / factor
                if abs(info["duration"] - expected) > 0.05 + expected * 0.01:
                    raise ValueError(f"candidate duration check failed: {ogg}")
                run([str(ffmpeg), "-hide_banner", "-v", "error", "-nostdin", "-xerror", "-threads", "1", "-i", str(ogg), "-f", "null", "NUL"], 180)
                mp3 = output / f"{prefix}-{label}.mp3"
                run([str(ffmpeg), "-hide_banner", "-v", "error", "-nostdin", "-n", "-xerror", "-threads", "1", "-i", str(ogg), "-map", "0:a:0", "-c:a", "libmp3lame", "-q:a", "4", "-threads", "1", str(mp3)])
                mp3_info = probe(ffprobe, mp3)
                if mp3_info["codec"] != "mp3" or abs(mp3_info["duration"] - info["duration"]) > 0.10 + info["duration"] * 0.01:
                    raise ValueError(f"MP3 preview validation failed: {mp3}")
                artifacts.append({"quest": item["quest"], "kind": item["kind"], "variant": label, "factor": factor,
                                  "ogg": str(ogg), "ogg_sha256": sha256(ogg), "ogg_probe": info,
                                  "mp3": str(mp3), "mp3_sha256": sha256(mp3), "mp3_probe": mp3_info})
            print(f"Quest {item['quest']} {item['kind']}: original, blanket 1.5x, adaptive {decision['factor']:.3f}x ready")
        write_json_new(output / "artifacts.json", {"status": "validated", "ffmpeg": str(ffmpeg), "artifacts": artifacts})
        print(f"Validated previews: {output}")
        return 0
    except Exception as exc:
        print(f"AdaptiveAudioPilot failed closed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
