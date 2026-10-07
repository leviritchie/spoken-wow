"""Build candidate-only adaptive speech outputs from immutable 1.5x-stage originals."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time
import uuid

import AdaptiveAudioPilot as Pilot


RUN_SCHEMA = 3
CACHE_SCHEMA = 8
POLICY_VERSION = Pilot.DECISION_POLICY_VERSION
PACK_ORDER = ("Alliance", "Horde", "Shared", "Gossip")
PACK_COUNTS = {"Alliance": 2832, "Horde": 2373, "Shared": 3646, "Gossip": 4112}
EXPECTED_AUDIO_COUNT = 12963
EXPECTED_TABLES = 4
STAGE_MANIFEST_SHA256 = "22C0AB02AF7D7468DB72936019E72363606C0B7A5DD583F16B08A4BFDB312A1B"
PILOT_ANALYSIS = Path(r"V:\Games\WoW-Addon-Tests\SpokenQuests-Adaptive-Pilot\run-003\analysis.json")
PILOT_ANALYSIS_SHA256 = "A28AF544A442DD1BB9FE4A8973FF3B71AFF4C212DA38D855F6C898902FEF7ADA"
FULL_APPROVAL_TEXT = "adaptive method approved"
APPROVAL_DATE = "2026-10-06"
TEMPO_RECIPE = "ffmpeg atempo=<factor>, libvorbis q5, one encoder/filter thread; source is immutable original"
MANIFEST_RECIPE = "ffmpeg atempo=1.5, libvorbis q5, one encoder thread"
RELATIVE_OGG = re.compile(r"^generated/sounds/(?:quests|gossip|followup)/[^/\\]+\.ogg$", re.ASCII)
TIMING_LINE = re.compile(
    r'(?m)^(?P<prefix>[ \t]*\["(?P<key>[^"]+)"\][ \t]*=[ \t]*)'
    r'(?P<value>(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)'
    r'(?P<suffix>,[ \t]*\r?)$'
)
TIMING_KEY_START = re.compile(r'(?m)^[ \t]*\["[^"]+"\]')
WOW_PROCESS_NAMES = {"wow", "wowb", "wowclassic", "wowclassicb", "wowt", "wow-64", "wowb-64"}


class ResourceBlocked(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def write_json_new(path: Path, value: object) -> None:
    data = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.partial")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # This Windows-only runner's os.rename fails rather than replacing an existing destination.
        os.rename(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def is_reparse(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def assert_no_reparse(path: Path, root: Path) -> None:
    root = root.resolve(strict=True)
    path = path.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes its expected root: {path}") from exc
    current = root
    if is_reparse(current):
        raise ValueError(f"reparse point is not allowed: {current}")
    for part in relative.parts:
        current = current / part
        if is_reparse(current):
            raise ValueError(f"reparse point is not allowed: {current}")


def assert_no_reparse_ancestors(path: Path, include_leaf: bool = True) -> None:
    absolute = Path(os.path.abspath(str(path)))
    current = absolute if include_leaf else absolute.parent
    while True:
        if is_reparse(current):
            raise ValueError(f"reparse point in protected path: {current}")
        parent = current.parent
        if parent == current:
            break
        current = parent


def reject_wow_addons_path(path: Path, label: str) -> None:
    forbidden = {"world of warcraft", "interface", "addons"}
    if any(part.casefold() in forbidden for part in path.parts):
        raise ValueError(f"{label} must not be under a WoW Interface/AddOns path")


def safe_relative(value: str) -> str:
    if not isinstance(value, str) or "\\" in value:
        raise ValueError(f"invalid manifest relative path: {value!r}")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts) or not RELATIVE_OGG.fullmatch(value):
        raise ValueError(f"audio path is outside the frozen corpus contract: {value!r}")
    return value


def parse_timing_table(data: bytes, label: str) -> dict:
    has_bom = data.startswith(b"\xef\xbb\xbf")
    payload = data[3:] if has_bom else data
    text = payload.decode("utf-8", errors="strict")
    values = {}
    matches = list(TIMING_LINE.finditer(text))
    for match in matches:
        key = match.group("key")
        literal = match.group("value")
        value = float(literal)
        if key in values or not math.isfinite(value) or value <= 0:
            raise ValueError(f"duplicate or invalid timing entry {key!r} in {label}")
        values[key] = literal
    if len(list(TIMING_KEY_START.finditer(text))) != len(matches) or not values:
        raise ValueError(f"non-literal or missing timing entries in {label}")
    return {"text": text, "has_bom": has_bom, "values": values}


def mask_timing_text(text: str, keys: set[str]) -> str:
    return TIMING_LINE.sub(
        lambda match: match.group("prefix") + "<DURATION>" + match.group("suffix")
        if match.group("key") in keys else match.group(0),
        text,
    )


def rewrite_timing_table(table: dict, durations: dict[str, float], expected_keys: set[str], label: str) -> bytes:
    if set(table["values"]) != expected_keys or set(durations) != expected_keys:
        missing = sorted(expected_keys - set(table["values"]))[:8]
        extra = sorted(set(table["values"]) - expected_keys)[:8]
        raise ValueError(f"{label} timing-key coverage mismatch; missing={missing}, extra={extra}")
    replaced = set()

    def replace(match: re.Match) -> str:
        key = match.group("key")
        if key not in durations:
            return match.group(0)
        if key in replaced:
            raise ValueError(f"duplicate timing key {key!r} in {label}")
        duration = float(durations[key])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError(f"invalid candidate duration for {key!r} in {label}")
        replaced.add(key)
        return match.group("prefix") + repr(duration) + match.group("suffix")

    output = TIMING_LINE.sub(replace, table["text"])
    if replaced != expected_keys or mask_timing_text(output, expected_keys) != mask_timing_text(table["text"], expected_keys):
        raise ValueError(f"timing rewrite changed unrelated content or omitted keys in {label}")
    encoded = output.encode("utf-8")
    return (b"\xef\xbb\xbf" + encoded) if table["has_bom"] else encoded


def audio_key(item: dict) -> tuple[str, str]:
    return item["Pack"], item["RelativePath"]


def enumerate_original_ogg_paths(pack_root: Path) -> set[str]:
    assert_no_reparse_ancestors(pack_root, include_leaf=True)
    if not pack_root.is_dir():
        raise ValueError(f"missing original pack directory: {pack_root}")
    paths = set()
    for path in pack_root.rglob("*"):
        if is_reparse(path):
            raise ValueError(f"reparse point in original pack tree: {path}")
        if path.is_file() and path.suffix.casefold() == ".ogg":
            relative = path.relative_to(pack_root).as_posix()
            safe_relative(relative)
            paths.add(relative)
    return paths


def validate_ogg_inventory(pack: str, actual_paths: set[str], manifest_paths: set[str]) -> None:
    if actual_paths != manifest_paths:
        missing = sorted(actual_paths - manifest_paths)[:8]
        absent = sorted(manifest_paths - actual_paths)[:8]
        raise ValueError(f"{pack} original OGG inventory differs from manifest; unlisted={missing}, missing={absent}")


def load_and_validate_stage(stage: Path) -> tuple[dict, list[dict], dict[str, dict]]:
    manifest_path = stage / "manifest.json"
    manifest_hash = sha256(manifest_path)
    if manifest_hash.casefold() != STAGE_MANIFEST_SHA256.casefold():
        raise ValueError(f"full-corpus stage manifest SHA-256 mismatch: {manifest_hash}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if sha256(manifest_path).casefold() != manifest_hash.casefold():
        raise ValueError("full-corpus stage manifest changed while it was being read")
    if manifest.get("Schema") != 1 or manifest.get("Recipe") != MANIFEST_RECIPE:
        raise ValueError("full-corpus stage schema or frozen recipe changed")
    if manifest.get("Status") not in {"Built", "Deployed", "Restored"}:
        raise ValueError("full-corpus stage is not in a completed built state")
    audio = manifest.get("Audio")
    tables = manifest.get("Tables")
    if not isinstance(audio, list) or len(audio) != EXPECTED_AUDIO_COUNT:
        raise ValueError(f"full-corpus stage must contain exactly {EXPECTED_AUDIO_COUNT} audio records")
    if not isinstance(tables, list) or len(tables) != EXPECTED_TABLES:
        raise ValueError(f"full-corpus stage must contain exactly {EXPECTED_TABLES} timing tables")
    table_by_pack = {}
    for table in tables:
        pack = table.get("Pack")
        if pack not in PACK_COUNTS or pack in table_by_pack or table.get("RelativePath") != "generated/sound_length_table.lua":
            raise ValueError(f"invalid timing-table record: {table}")
        if table.get("State") != "Built" or not re.fullmatch(r"[0-9A-Fa-f]{64}", table.get("OriginalSha256", "")):
            raise ValueError(f"unbuilt or invalid timing-table record for {pack}")
        table_by_pack[pack] = table
    if set(table_by_pack) != set(PACK_COUNTS):
        raise ValueError("timing-table pack inventory does not match the frozen corpus")

    records, seen_paths, seen_keys, counts = [], set(), set(), {pack: 0 for pack in PACK_COUNTS}
    manifest_paths = {pack: set() for pack in PACK_COUNTS}
    stage_original = stage / "original"
    for item in audio:
        pack = item.get("Pack")
        relative = safe_relative(item.get("RelativePath"))
        key = item.get("Key")
        if pack not in PACK_COUNTS or item.get("State") != "Built" or key != PurePosixPath(relative).stem:
            raise ValueError(f"unbuilt or malformed audio manifest entry: {pack}/{relative}")
        manifest_key = (pack, relative)
        pack_key = (pack, key)
        if manifest_key in seen_paths or pack_key in seen_keys:
            raise ValueError(f"duplicate audio path/key in full-stage manifest: {manifest_key}")
        seen_paths.add(manifest_key)
        seen_keys.add(pack_key)
        manifest_paths[pack].add(relative)
        original_hash = item.get("OriginalSha256", "")
        if not re.fullmatch(r"[0-9A-Fa-f]{64}", original_hash):
            raise ValueError(f"invalid original SHA-256 for {pack}/{relative}")
        if not re.fullmatch(r"[0-9A-Fa-f]{64}", item.get("FasterSha256", "")):
            raise ValueError(f"missing validated 1.5x output hash for {pack}/{relative}")
        duration = float(item.get("OriginalDuration", 0))
        faster_duration = float(item.get("FasterDuration", 0))
        rate, channels = int(item.get("SampleRate", 0)), int(item.get("Channels", 0))
        byte_count, granule = int(item.get("OriginalBytes", 0)), int(item.get("OriginalGranule", 0))
        if (not math.isfinite(duration) or duration <= 0 or not math.isfinite(faster_duration) or faster_duration <= 0
                or rate <= 0 or channels <= 0 or byte_count <= 0 or granule <= 0
                or abs(duration - granule / rate) > 1e-9):
            raise ValueError(f"invalid original Ogg metadata for {pack}/{relative}")
        source = stage_original / pack / Path(relative)
        assert_no_reparse(source, stage_original)
        if not source.is_file() or source.stat().st_size != byte_count:
            raise ValueError(f"missing or changed original snapshot size: {pack}/{relative}")
        actual_hash = sha256(source)
        if actual_hash.casefold() != original_hash.casefold():
            raise ValueError(f"original snapshot SHA-256 mismatch: {pack}/{relative}")
        record = dict(item)
        record["_source"] = source
        record["_source_sha256"] = actual_hash
        records.append(record)
        counts[pack] += 1
    if counts != PACK_COUNTS:
        raise ValueError(f"full-stage pack counts changed: {counts}")
    for pack, count in PACK_COUNTS.items():
        actual_paths = enumerate_original_ogg_paths(stage_original / pack)
        if len(actual_paths) != count:
            raise ValueError(f"{pack} original OGG tree contains {len(actual_paths)} files; expected {count}")
        validate_ogg_inventory(pack, actual_paths, manifest_paths[pack])

    table_sources = {}
    for pack, table_record in table_by_pack.items():
        path = stage_original / pack / table_record["RelativePath"]
        assert_no_reparse(path, stage_original)
        data = path.read_bytes()
        actual_hash = hashlib.sha256(data).hexdigest().upper()
        if actual_hash.casefold() != table_record["OriginalSha256"].casefold():
            raise ValueError(f"original timing-table SHA-256 mismatch: {pack}")
        parsed = parse_timing_table(data, f"{pack} original table")
        expected_keys = {item["Key"] for item in records if item["Pack"] == pack}
        if set(parsed["values"]) != expected_keys:
            raise ValueError(f"{pack} original table does not exactly cover its audio keys")
        table_sources[pack] = {"record": table_record, "path": path, "data": data, "parsed": parsed,
                               "sha256": actual_hash, "keys": expected_keys}
    records.sort(key=lambda row: (PACK_ORDER.index(row["Pack"]), row["RelativePath"]))
    return manifest, records, table_sources


def select_sample32(records: list[dict]) -> list[dict]:
    by_key = {(row["Pack"], row["RelativePath"]): row for row in records}
    anchors = [
        ("Alliance", "generated/sounds/quests/783-accept.ogg"),
        ("Alliance", "generated/sounds/quests/783-complete.ogg"),
        ("Horde", "generated/sounds/quests/880-accept.ogg"),
        ("Horde", "generated/sounds/quests/880-complete.ogg"),
    ]
    selected = []
    for key in anchors:
        if key not in by_key:
            raise ValueError(f"required adaptive pilot reference clip is missing: {key}")
        selected.append(by_key[key])
    selected_keys = {audio_key(item) for item in selected}
    remainder = [item for item in records if audio_key(item) not in selected_keys]
    needed = 32 - len(selected)
    for i in range(needed):
        index = min(len(remainder) - 1, int((i + 0.5) * len(remainder) / needed))
        selected.append(remainder[index])
    selected.sort(key=lambda row: (PACK_ORDER.index(row["Pack"]), row["RelativePath"]))
    if len(selected) != 32 or len({audio_key(item) for item in selected}) != 32:
        raise AssertionError("deterministic 32-clip sample selection is not unique")
    return selected


def load_pilot_calibration(stage_records: list[dict]) -> dict:
    if sha256(PILOT_ANALYSIS) != PILOT_ANALYSIS_SHA256:
        raise ValueError("validated run-003 pilot analysis changed; calibration binding does not match")
    pilot = json.loads(PILOT_ANALYSIS.read_text(encoding="utf-8"))
    if pilot.get("schema") != 3 or pilot.get("status") != "analysis_complete":
        raise ValueError("run-003 adaptive pilot analysis is not the validated schema-3 result")
    source = next((item for item in stage_records if item["Pack"] == "Horde"
                   and item["RelativePath"] == "generated/sounds/quests/880-accept.ogg"), None)
    decision = next((row for row in pilot.get("decisions", [])
                     if row.get("quest") == "880" and row.get("kind") == "accept"), None)
    if source is None or decision is None or decision.get("source_sha256", "").casefold() != source["_source_sha256"].casefold():
        raise ValueError("run-003 880 accept calibration source differs from the frozen full-stage original")
    ratio = float(source["OriginalDuration"]) / float(source["FasterDuration"])
    pilot_ratio = float(pilot.get("reference", {}).get("ratio", 0))
    if not 1.30 <= ratio <= 1.60 or abs(ratio - pilot_ratio) > 1e-12:
        raise ValueError("full-stage 880 accept duration ratio does not match the approved run-003 reference")
    return {"approval_text": FULL_APPROVAL_TEXT, "approval_date": APPROVAL_DATE,
            "pilot_run": str(PILOT_ANALYSIS.parent), "pilot_analysis_sha256": PILOT_ANALYSIS_SHA256,
            "reference": "Horde/generated/sounds/quests/880-accept.ogg",
            "reference_source_sha256": source["_source_sha256"], "duration_ratio": ratio,
            "ratio_source": "immutable full-stage original duration / its measured 1.5x candidate duration",
            "interpretation": "scaled phrase-rate target; the faster audio is not used as an ASR source",
            "deployment_authorized": False}


def validate_locations(stage: Path, output: Path, model_cache: Path, ffmpeg: Path) -> tuple[Path, Path, Path, Path]:
    if os.name != "nt":
        raise RuntimeError("this installation-specific workflow is Windows-only")
    stage, output, model_cache, ffmpeg = (Path(os.path.abspath(str(path)))
                                          for path in (stage, output, model_cache, ffmpeg))
    assert_no_reparse_ancestors(stage, include_leaf=True)
    assert_no_reparse_ancestors(output, include_leaf=False)
    assert_no_reparse_ancestors(model_cache, include_leaf=True)
    assert_no_reparse_ancestors(ffmpeg, include_leaf=True)
    reject_wow_addons_path(output, "candidate output")
    reject_wow_addons_path(model_cache, "analysis cache")
    if os.path.lexists(output):
        raise FileExistsError(f"output must be a new path and will never be overwritten: {output}")
    stage = stage.resolve(strict=True)
    output = output.parent.resolve(strict=True) / output.name
    model_cache = model_cache.resolve(strict=True)
    ffmpeg = ffmpeg.resolve(strict=True)
    if any(path.drive.upper() != "V:" for path in (stage, output, model_cache)):
        raise ValueError("stage, output, and model cache must remain on V:")
    if not stage.is_dir() or not (stage / "manifest.json").is_file():
        raise ValueError("--stage must be the existing full-corpus stage with manifest.json")
    if not model_cache.is_dir():
        raise ValueError("--model-cache must be an existing local model-cache directory")
    if not ffmpeg.is_file():
        raise FileNotFoundError("--ffmpeg must name the approved local ffmpeg.exe")
    if not output.parent.is_dir():
        raise ValueError("output parent directory must already exist")
    if any(inside(a, b) or inside(b, a) for a, b in ((stage, output), (model_cache, output), (stage, model_cache))):
        raise ValueError("stage, output, and model cache must be separate trees")
    assert_no_reparse(stage / "manifest.json", stage)
    return stage, output, model_cache, ffmpeg


def run_command(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=timeout,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {args[0]}\n{result.stderr[-3000:]}")
    return result


def ffmpeg_version(ffmpeg: Path) -> str:
    result = run_command([str(ffmpeg), "-hide_banner", "-version"], 30)
    lines = (result.stdout + "\n" + result.stderr).splitlines()
    first = next((line.strip() for line in lines if line.strip().startswith("ffmpeg version ")), "")
    if not first.startswith("ffmpeg version "):
        raise ValueError("could not identify the configured ffmpeg build")
    return first


def runtime_info() -> dict:
    packages = ("faster-whisper", "ctranslate2", "av", "numpy", "onnxruntime")
    versions = {name: importlib.metadata.version(name) for name in packages}
    if versions["faster-whisper"] != Pilot.WHISPER_VERSION:
        raise RuntimeError(f"requires faster-whisper {Pilot.WHISPER_VERSION}; found {versions['faster-whisper']}")
    import ctranslate2
    device_count = int(ctranslate2.get_cuda_device_count())
    if device_count < 1:
        raise RuntimeError("no CUDA device is available; CPU fallback is prohibited")
    try:
        properties = ctranslate2.get_cuda_device_properties(0)
        device_properties = {str(key): str(value) for key, value in properties.items()}
    except (AttributeError, TypeError):
        device_properties = {"name": "unavailable from this pinned runtime"}
    return {"versions": versions, "cuda_device_count": device_count,
            "cuda_device_0": device_properties, "device": "cuda:0", "compute_type": "float16"}


def resource_guard(label: str) -> dict:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        raise ResourceBlocked(f"PowerShell is unavailable; active WoW processes cannot be checked during {label}")
    process_command = ("$ErrorActionPreference='Stop'; "
                       "Get-Process -ErrorAction Stop | Where-Object { $_.ProcessName -in "
                       "@('WowB','Wow','WowClassic','WowClassicB','WowT') } "
                       "| ForEach-Object { $_.ProcessName }")
    try:
        process_check = run_command([powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                                     "-Command", process_command], 30)
    except Exception as exc:
        raise ResourceBlocked(f"could not inspect active processes during {label}: {exc}") from exc
    processes = [name.strip() for name in process_check.stdout.splitlines() if name.strip()]
    running_wow = sorted({name for name in processes if name.casefold().removesuffix(".exe") in WOW_PROCESS_NAMES})
    if running_wow:
        raise ResourceBlocked(f"WoW process is active during {label}: {', '.join(running_wow)}; no process was stopped")
    executable = shutil.which("nvidia-smi.exe") or shutil.which("nvidia-smi")
    if not executable:
        raise ResourceBlocked(f"nvidia-smi is unavailable; GPU headroom cannot be verified during {label}")
    def read_gpu_utilization() -> int:
        try:
            sample = run_command([executable, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits", "--id=0"], 15)
        except Exception as exc:
            raise ResourceBlocked(f"could not read GPU utilization during {label}: {exc}") from exc
        values = [line.strip() for line in sample.stdout.splitlines() if line.strip()]
        if len(values) != 1:
            raise ResourceBlocked(f"expected one GPU-utilization value during {label}; got {values}")
        try:
            result = int(values[0])
        except ValueError as exc:
            raise ResourceBlocked(f"GPU utilization sample is not numeric during {label}: {values[0]!r}") from exc
        if not 0 <= result <= 100:
            raise ResourceBlocked(f"GPU utilization sample is outside [0,100] during {label}: {result}")
        return result

    first = read_gpu_utilization()
    time.sleep(0.25)
    second = read_gpu_utilization()
    readings = [first, second]
    if all(value > 80 for value in readings):
        raise ResourceBlocked(f"GPU utilization remained above 80% during {label}: {readings}; no workload/process was stopped")
    return {"checkpoint": label, "gpu_utilization_samples": readings, "active_wow_processes": []}


def load_model(model_cache: Path, runtime: dict):
    from faster_whisper import WhisperModel
    model = WhisperModel(Pilot.MODEL_NAME, device="cuda", device_index=0, compute_type="float16",
                         cpu_threads=Pilot.CPU_THREADS, num_workers=1, revision=Pilot.MODEL_REVISION,
                         local_files_only=True, download_root=str(model_cache))
    actual_device = str(getattr(model.model, "device", ""))
    raw_index = getattr(model.model, "device_index", 0)
    if isinstance(raw_index, (list, tuple)):
        if len(raw_index) != 1:
            raise RuntimeError(f"model uses multiple CUDA device indices: {raw_index}")
        raw_index = raw_index[0]
    actual_index = int(raw_index)
    if actual_device.casefold() != "cuda" or actual_index != 0:
        raise RuntimeError(f"model did not remain on the requested CUDA device: {actual_device}:{actual_index}")
    runtime["loaded_model_device"] = f"{actual_device}:{actual_index}"
    return model


def analysis_identity(source_hash: str, runtime: dict) -> dict:
    identity = Pilot.cache_identity(source_hash)
    identity.update({"cache_schema": CACHE_SCHEMA, "tool": "AdaptiveAudioBatch-v8",
                     "runtime_versions": runtime["versions"], "device": "cuda:0",
                     "compute_type": "float16", "num_workers": 1,
                     "cuda_device_count": runtime["cuda_device_count"],
                     "cuda_device_0": runtime["cuda_device_0"],
                     "cpu_fallback": False, "decoded_audio": "PyAV mono float32 at 16000 Hz"})
    return identity


def cached_analysis(path: Path, identity: dict) -> dict:
    cached = json.loads(path.read_text(encoding="utf-8"))
    if cached.get("identity") != identity or not isinstance(cached.get("analysis"), dict):
        raise ValueError(f"analysis-cache identity/content mismatch: {path}")
    serialized = json.dumps(cached["analysis"], ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False).encode("utf-8")
    if hashlib.sha256(serialized).hexdigest().upper() != cached.get("analysis_sha256"):
        raise ValueError(f"analysis-cache payload checksum mismatch: {path}")
    validate_analysis(cached["analysis"], path)
    return cached["analysis"]


def validate_analysis(analysis: dict, label: object) -> None:
    required = ("transcript", "words", "decoded_seconds", "transcript_word_count", "timestamped_word_count",
                "word_coverage", "raw_word_coverage_ratio", "aligned_word_count_exceeds_transcript",
                "mean_word_probability", "low_confidence_words", "language",
                "language_probability", "phrase_window_wpm", "phrase_window_word_counts",
                "phrase_window_word_coverage", "pace_eligible_word_count", "phrase_window_word_count",
                "excluded_window_word_count", "median_phrase_wpm", "p90_phrase_wpm",
                "long_pause_count", "long_pause_total_seconds", "long_pause_p90_seconds",
                "overall_wpm_including_pauses", "review_reasons")
    if any(name not in analysis for name in required):
        raise ValueError(f"analysis result is incomplete: {label}")
    if not isinstance(analysis["transcript"], str) or not isinstance(analysis["words"], list):
        raise ValueError(f"analysis transcript/word rows have invalid types: {label}")
    if not isinstance(analysis["review_reasons"], list) or any(not isinstance(reason, str) for reason in analysis["review_reasons"]):
        raise ValueError(f"analysis review reasons are malformed: {label}")
    for name in ("word_coverage", "mean_word_probability", "language_probability", "phrase_window_word_coverage"):
        value = analysis[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"analysis metric {name} is nonfinite or outside [0,1]: {label}")
    for name in ("decoded_seconds", "long_pause_total_seconds", "long_pause_p90_seconds", "overall_wpm_including_pauses"):
        value = analysis[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"analysis metric {name} is malformed: {label}")
    if analysis["decoded_seconds"] <= 0:
        raise ValueError(f"analysis decoded duration must be positive: {label}")
    for name in ("transcript_word_count", "timestamped_word_count", "low_confidence_words", "long_pause_count",
                 "pace_eligible_word_count", "phrase_window_word_count", "excluded_window_word_count"):
        value = analysis[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"analysis metric {name} is malformed: {label}")
    if not isinstance(analysis["aligned_word_count_exceeds_transcript"], bool):
        raise ValueError(f"analysis alignment-mismatch flag is malformed: {label}")
    raw_coverage = analysis["raw_word_coverage_ratio"]
    if analysis["transcript_word_count"] == 0:
        if raw_coverage is not None:
            raise ValueError(f"analysis raw word-coverage ratio must be null for an empty transcript: {label}")
    elif (isinstance(raw_coverage, bool) or not isinstance(raw_coverage, (int, float))
          or not math.isfinite(raw_coverage) or raw_coverage < 0):
        raise ValueError(f"analysis raw word-coverage ratio is malformed: {label}")
    if analysis["language"] is not None and not isinstance(analysis["language"], str):
        raise ValueError(f"analysis language label has an invalid type: {label}")
    for name in ("median_phrase_wpm", "p90_phrase_wpm"):
        value = analysis[name]
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or value <= 0):
            raise ValueError(f"analysis metric {name} is malformed: {label}")
    rates, sizes = analysis["phrase_window_wpm"], analysis["phrase_window_word_counts"]
    if not isinstance(rates, list) or not isinstance(sizes, list) or len(rates) != len(sizes):
        raise ValueError(f"analysis pace-window arrays are malformed: {label}")
    if any(isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0 for rate in rates):
        raise ValueError(f"analysis pace-window rate is malformed: {label}")
    if any(isinstance(size, bool) or not isinstance(size, int) for size in sizes):
        raise ValueError(f"analysis pace-window word count is malformed: {label}")
    import numpy as np
    for word in analysis["words"]:
        if not isinstance(word, dict) or not isinstance(word.get("text"), str):
            raise ValueError(f"analysis word row is malformed: {label}")
        probability = word.get("probability")
        if isinstance(probability, bool) or not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError(f"analysis word confidence is nonfinite or outside [0,1]: {label}")
        count = word.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"analysis word count is malformed: {label}")
        if count != len(Pilot.WORD_RE.findall(word["text"])):
            raise ValueError(f"analysis word count does not match word text: {label}")
        for name in ("start", "end"):
            value = word.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"analysis word timestamp {name} is malformed: {label}")
    timed, pace_eligible, timestamp_flags = Pilot.classify_word_timings(analysis["words"], analysis["decoded_seconds"])
    if analysis["transcript_word_count"] != len(Pilot.WORD_RE.findall(analysis["transcript"])):
        raise ValueError(f"analysis transcript word count mismatch: {label}")
    timed_count = sum(word["count"] for word in timed)
    expected_raw_coverage = timed_count / analysis["transcript_word_count"] if analysis["transcript_word_count"] else None
    expected_coverage = min(1.0, expected_raw_coverage) if expected_raw_coverage is not None else 0.0
    if (analysis["timestamped_word_count"] != timed_count
            or abs(analysis["word_coverage"] - expected_coverage) > 1e-9
            or (expected_raw_coverage is None) != (analysis["raw_word_coverage_ratio"] is None)
            or (expected_raw_coverage is not None
                and abs(analysis["raw_word_coverage_ratio"] - expected_raw_coverage) > 1e-9)
            or analysis["aligned_word_count_exceeds_transcript"] != (timed_count > analysis["transcript_word_count"])):
        raise ValueError(f"analysis timestamped-word coverage mismatch: {label}")
    probabilities = [float(word["probability"]) for word in timed]
    expected_mean = sum(probabilities) / len(probabilities) if probabilities else 0.0
    expected_low = sum(probability < Pilot.LOW_WORD_PROB for probability in probabilities)
    if (abs(analysis["mean_word_probability"] - expected_mean) > 1e-9
            or analysis["low_confidence_words"] != expected_low):
        raise ValueError(f"analysis confidence summaries mismatch: {label}")
    expected_rates, expected_sizes, expected_pauses, expected_excluded = Pilot.pace_metrics(pace_eligible)
    rates_match = len(rates) == len(expected_rates) and all(
        abs(actual - expected) <= 1e-9 for actual, expected in zip(rates, expected_rates))
    if (not rates_match or sizes != expected_sizes
            or analysis["excluded_window_word_count"] != expected_excluded):
        max_eligible_row_weight = max((word["count"] for word in pace_eligible), default=0)
        raise ValueError(f"analysis pace windows do not match exact pace_metrics recomputation: {label}; "
                         f"observed_sizes={sizes}, expected_sizes={expected_sizes}, "
                         f"max_eligible_row_lexical_count={max_eligible_row_weight}")
    expected_median = float(np.median(expected_rates)) if expected_rates else None
    expected_p90 = float(np.percentile(expected_rates, 90)) if expected_rates else None
    if ((expected_median is None) != (analysis["median_phrase_wpm"] is None)
            or (expected_median is not None and abs(expected_median - analysis["median_phrase_wpm"]) > 1e-9)
            or (expected_p90 is not None and abs(expected_p90 - analysis["p90_phrase_wpm"]) > 1e-9)):
        raise ValueError(f"analysis phrase summaries mismatch: {label}")
    pace_word_count = sum(word["count"] for word in pace_eligible)
    if (analysis["pace_eligible_word_count"] != pace_word_count
            or analysis["phrase_window_word_count"] != sum(expected_sizes)
            or sum(expected_sizes) + expected_excluded != pace_word_count):
        raise ValueError(f"analysis pace-window lexical accounting mismatch: {label}; "
                         f"eligible={pace_word_count}, measured={sum(expected_sizes)}, "
                         f"excluded={expected_excluded}")
    expected_window_coverage = sum(expected_sizes) / pace_word_count if pace_word_count else 0.0
    if abs(analysis["phrase_window_word_coverage"] - expected_window_coverage) > 1e-9:
        raise ValueError(f"analysis phrase-window coverage mismatch: {label}")
    if (analysis["long_pause_count"] != len(expected_pauses)
            or abs(analysis["long_pause_total_seconds"] - sum(expected_pauses)) > 1e-9
            or abs(analysis["long_pause_p90_seconds"] - (float(np.percentile(expected_pauses, 90)) if expected_pauses else 0.0)) > 1e-9):
        raise ValueError(f"analysis long-pause summaries mismatch: {label}")
    expected_overall = 60.0 * analysis["transcript_word_count"] / analysis["decoded_seconds"]
    if abs(analysis["overall_wpm_including_pauses"] - expected_overall) > 1e-9:
        raise ValueError(f"analysis overall pace mismatch: {label}")
    expected_reasons = list(timestamp_flags)
    if timed_count > analysis["transcript_word_count"]:
        expected_reasons.append(Pilot.ALIGNMENT_MISMATCH_REASON)
    if len(probabilities) != len(timed):
        expected_reasons.append("missing word confidence")
    if expected_mean < Pilot.MIN_MEAN_PROB or (probabilities and expected_low / len(probabilities) > Pilot.MAX_LOW_CONF_FRACTION):
        expected_reasons.append("low word confidence")
    if analysis["language"] != "en" or analysis["language_probability"] < 0.80:
        expected_reasons.append("uncertain English language detection")
    if expected_coverage < 0.90:
        expected_reasons.append("insufficient word-timestamp coverage")
    if not expected_rates:
        expected_reasons.append("insufficient meaningful phrase windows")
    if expected_window_coverage < 0.70:
        expected_reasons.append("insufficient meaningful phrase-window coverage")
    if expected_excluded and expected_window_coverage < 0.70:
        expected_reasons.append(Pilot.PACE_WINDOW_EXCLUSION_REASON)
    if analysis["transcript_word_count"] < Pilot.MIN_WINDOW_WORDS or timed_count < Pilot.MIN_WINDOW_WORDS:
        expected_reasons.append("insufficient words for pace decision")
    if analysis["review_reasons"] != sorted(set(expected_reasons)):
        raise ValueError(f"analysis review reasons do not match independently derived quality flags: {label}")


def get_analysis(item: dict, identity: dict, cache_dir: Path, model, counter: dict) -> tuple[dict, str, str, float]:
    cache_id = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    path = cache_dir / f"{cache_id}.json"
    assert_no_reparse(path, cache_dir)
    if path.exists():
        return cached_analysis(path, identity), "cached", cache_id, 0.0
    if model is None:
        raise RuntimeError("cache miss requires the single pinned CUDA model instance")
    source_hash = item["_source_sha256"]
    if sha256(item["_source"]) != source_hash:
        raise ValueError(f"original changed before analysis: {item['Pack']}/{item['RelativePath']}")
    started = time.monotonic()
    audio, duration, rate, channels = Pilot.decode16k(item["_source"])
    if (rate, channels) != (int(item["SampleRate"]), int(item["Channels"])):
        raise ValueError(f"source format differs from frozen manifest: {item['Pack']}/{item['RelativePath']}")
    if abs(duration - float(item["OriginalDuration"])) > 0.10 + duration * 0.01:
        raise ValueError(f"decoded source duration differs from manifest: {item['Pack']}/{item['RelativePath']}")
    result = Pilot.analyze(model, {"source_audio": audio}, duration)
    del audio
    elapsed = time.monotonic() - started
    validate_analysis(result, item["RelativePath"])
    if sha256(item["_source"]) != source_hash:
        raise ValueError(f"original changed during analysis: {item['Pack']}/{item['RelativePath']}")
    try:
        serialized = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        write_json_new(path, {"identity": identity, "analysis_sha256": hashlib.sha256(serialized).hexdigest().upper(),
                              "analysis": result})
    except FileExistsError:
        result = cached_analysis(path, identity)
        counter["cache_race_hits"] += 1
        return result, "cached-after-race", cache_id, elapsed
    counter["new_analysis_count"] += 1
    counter["asr_seconds"] += elapsed
    return result, "analyzed", cache_id, elapsed


def analysis_summary(analysis: dict) -> dict:
    timed, _, timestamp_flags = Pilot.classify_word_timings(analysis["words"], analysis["decoded_seconds"])
    return {name: analysis[name] for name in (
        "decoded_seconds", "transcript_word_count", "timestamped_word_count", "word_coverage",
        "raw_word_coverage_ratio", "aligned_word_count_exceeds_transcript",
        "mean_word_probability", "low_confidence_words", "language", "language_probability",
        "phrase_window_word_coverage", "pace_eligible_word_count", "phrase_window_word_count",
        "excluded_window_word_count", "median_phrase_wpm", "p90_phrase_wpm",
        "long_pause_count", "long_pause_total_seconds", "long_pause_p90_seconds",
        "overall_wpm_including_pauses", "review_reasons")} | {
        "meaningful_phrase_window_count": len(analysis["phrase_window_wpm"]),
        "timestamped_word_row_count": len(timed),
        "timed_word_confidence_count": sum(word["probability"] is not None for word in timed),
        "timestamp_quality_flags": timestamp_flags}


def validate_analysis_summary(summary: dict, label: str) -> None:
    if not isinstance(summary, dict):
        raise ValueError(f"analysis summary is malformed: {label}")
    required = ("decoded_seconds", "transcript_word_count", "timestamped_word_count", "word_coverage",
                "raw_word_coverage_ratio", "aligned_word_count_exceeds_transcript",
                "mean_word_probability", "low_confidence_words", "language", "language_probability",
                "phrase_window_word_coverage", "pace_eligible_word_count", "phrase_window_word_count",
                "excluded_window_word_count", "median_phrase_wpm", "p90_phrase_wpm",
                "long_pause_count", "long_pause_total_seconds", "long_pause_p90_seconds",
                "overall_wpm_including_pauses", "review_reasons", "meaningful_phrase_window_count",
                "timestamped_word_row_count", "timed_word_confidence_count", "timestamp_quality_flags")
    if any(name not in summary for name in required):
        raise ValueError(f"analysis summary is incomplete: {label}")
    if not isinstance(summary["review_reasons"], list) or any(not isinstance(item, str) for item in summary["review_reasons"]):
        raise ValueError(f"analysis summary review reasons are malformed: {label}")
    for name in ("decoded_seconds", "long_pause_total_seconds", "long_pause_p90_seconds", "overall_wpm_including_pauses"):
        value = summary[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"analysis summary metric {name} is malformed: {label}")
    if summary["decoded_seconds"] <= 0:
        raise ValueError(f"analysis summary decoded duration must be positive: {label}")
    for name in ("word_coverage", "mean_word_probability", "language_probability", "phrase_window_word_coverage"):
        value = summary[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"analysis summary metric {name} is outside [0,1]: {label}")
    for name in ("transcript_word_count", "timestamped_word_count", "low_confidence_words", "long_pause_count",
                 "meaningful_phrase_window_count", "timestamped_word_row_count", "timed_word_confidence_count",
                 "pace_eligible_word_count", "phrase_window_word_count", "excluded_window_word_count"):
        value = summary[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"analysis summary count {name} is malformed: {label}")
    if (summary["timestamped_word_row_count"] > summary["timestamped_word_count"]
            or summary["low_confidence_words"] > summary["timed_word_confidence_count"]
            or summary["timed_word_confidence_count"] > summary["timestamped_word_row_count"]):
        raise ValueError(f"analysis summary word counts are inconsistent: {label}")
    if not isinstance(summary["aligned_word_count_exceeds_transcript"], bool):
        raise ValueError(f"analysis summary alignment-mismatch flag is malformed: {label}")
    expected_raw_coverage = (summary["timestamped_word_count"] / summary["transcript_word_count"]
                             if summary["transcript_word_count"] else None)
    if ((expected_raw_coverage is None) != (summary["raw_word_coverage_ratio"] is None)
            or (expected_raw_coverage is not None
                and (isinstance(summary["raw_word_coverage_ratio"], bool)
                     or not isinstance(summary["raw_word_coverage_ratio"], (int, float))
                     or not math.isfinite(summary["raw_word_coverage_ratio"])
                     or abs(summary["raw_word_coverage_ratio"] - expected_raw_coverage) > 1e-9))
            or abs(summary["word_coverage"] - (min(1.0, expected_raw_coverage)
                                                if expected_raw_coverage is not None else 0.0)) > 1e-9
            or summary["aligned_word_count_exceeds_transcript"]
               != (summary["timestamped_word_count"] > summary["transcript_word_count"])):
        raise ValueError(f"analysis summary transcript/alignment coverage mismatch: {label}")
    if (summary["phrase_window_word_count"] + summary["excluded_window_word_count"]
            != summary["pace_eligible_word_count"]):
        raise ValueError(f"analysis summary pace-window lexical accounting mismatch: {label}")
    expected_window_coverage = (summary["phrase_window_word_count"] / summary["pace_eligible_word_count"]
                                if summary["pace_eligible_word_count"] else 0.0)
    if abs(summary["phrase_window_word_coverage"] - expected_window_coverage) > 1e-9:
        raise ValueError(f"analysis summary pace-window coverage mismatch: {label}")
    if summary["language"] is not None and not isinstance(summary["language"], str):
        raise ValueError(f"analysis summary language label is malformed: {label}")
    for name in ("median_phrase_wpm", "p90_phrase_wpm"):
        value = summary[name]
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or value <= 0):
            raise ValueError(f"analysis summary pace {name} is malformed: {label}")
    flags = summary["timestamp_quality_flags"]
    allowed_flags = {"missing word timestamp", "invalid word timestamp", "non-monotonic word timestamps"}
    if (not isinstance(flags, list) or any(not isinstance(flag, str) or flag not in allowed_flags for flag in flags)
            or flags != sorted(set(flags))):
        raise ValueError(f"analysis summary timestamp-quality flags are malformed: {label}")
    expected_reasons = list(flags)
    if summary["timestamped_word_count"] > summary["transcript_word_count"]:
        expected_reasons.append(Pilot.ALIGNMENT_MISMATCH_REASON)
    confidence_count = summary["timed_word_confidence_count"]
    if confidence_count != summary["timestamped_word_row_count"]:
        expected_reasons.append("missing word confidence")
    if (summary["mean_word_probability"] < Pilot.MIN_MEAN_PROB
            or (confidence_count and summary["low_confidence_words"] / confidence_count > Pilot.MAX_LOW_CONF_FRACTION)):
        expected_reasons.append("low word confidence")
    if summary["language"] != "en" or summary["language_probability"] < 0.80:
        expected_reasons.append("uncertain English language detection")
    if summary["word_coverage"] < 0.90:
        expected_reasons.append("insufficient word-timestamp coverage")
    if summary["meaningful_phrase_window_count"] == 0:
        expected_reasons.append("insufficient meaningful phrase windows")
    if summary["phrase_window_word_coverage"] < 0.70:
        expected_reasons.append("insufficient meaningful phrase-window coverage")
    if (summary["excluded_window_word_count"]
            and summary["phrase_window_word_coverage"] < 0.70):
        expected_reasons.append(Pilot.PACE_WINDOW_EXCLUSION_REASON)
    if (summary["transcript_word_count"] < Pilot.MIN_WINDOW_WORDS
            or summary["timestamped_word_count"] < Pilot.MIN_WINDOW_WORDS):
        expected_reasons.append("insufficient words for pace decision")
    if summary["review_reasons"] != sorted(set(expected_reasons)):
        raise ValueError(f"analysis summary review reasons do not match independently derived quality flags: {label}")


def validate_decision_policy(reference_summary: dict, decision: dict, ratio: float, comfort_band: float) -> None:
    label = f"{decision.get('pack')}/{decision.get('relative_path')}"
    summary = decision.get("analysis")
    validate_analysis_summary(reference_summary, "880 accept reference")
    validate_analysis_summary(summary, label)
    expected = Pilot.decide_factor(reference_summary, summary, ratio, comfort_band)
    for field in ("target_median_wpm", "ceiling_p90_wpm", "factor", "status", "review_reasons"):
        if decision.get(field) != expected[field]:
            raise ValueError(f"recorded decision policy does not rederive for {label}: {field}")


def final_candidate_status(scope: str, review_count: int) -> str:
    if scope == "sample32":
        return "sample_candidate_only_not_full_corpus"
    if scope != "full":
        raise ValueError(f"unsupported candidate scope: {scope}")
    return "candidate_only_review_required" if review_count else "candidate_only_human_review_pending"


def source_analysis(item: dict, identity: dict, cache_dir: Path, model, counter: dict) -> tuple[dict, str, str, float]:
    return get_analysis(item, identity, cache_dir, model, counter)


def decode_candidate(path: Path) -> dict:
    import av
    import numpy as np

    with av.open(str(path), mode="r") as container:
        audio_streams = [stream for stream in container.streams if stream.type == "audio"]
        if len(audio_streams) != 1 or len(container.streams) != 1 or audio_streams[0].codec_context.name != "vorbis":
            raise ValueError(f"candidate is not a single Vorbis audio stream: {path}")
        stream = audio_streams[0]
        sample_rate = int(stream.codec_context.sample_rate)
        channels = len(stream.codec_context.layout.channels)
        samples = 0
        frames = 0
        for frame in container.decode(stream):
            if int(frame.sample_rate or sample_rate) != sample_rate or len(frame.layout.channels) != channels:
                raise ValueError(f"candidate audio format changed mid-stream: {path}")
            decoded = frame.to_ndarray()
            if not np.isfinite(decoded).all():
                raise ValueError(f"candidate contains non-finite decoded samples: {path}")
            samples += int(frame.samples)
            frames += 1
    if sample_rate <= 0 or channels <= 0 or samples <= 0 or frames <= 0:
        raise ValueError(f"candidate decoded no valid audio samples: {path}")
    return {"codec": "vorbis", "sample_rate": sample_rate, "channels": channels,
            "decoded_samples": samples, "decoded_frames": frames, "duration": samples / sample_rate}


def copy_to_exclusive(source: Path, target: Path) -> None:
    with source.open("rb") as src, target.open("xb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())


def encode_candidate(ffmpeg: Path, source: Path, target: Path, factor: float) -> None:
    tempo = f"{factor:.9f}"
    run_command([str(ffmpeg), "-hide_banner", "-v", "error", "-nostdin", "-n", "-xerror",
                 "-threads", "1", "-filter_threads", "1", "-i", str(source), "-map", "0:a:0",
                 "-vn", "-sn", "-dn", "-af", f"atempo={tempo}", "-c:a", "libvorbis", "-q:a", "5",
                 "-threads", "1", str(target)])


def build_one(item: dict, decision: dict, output_root: Path, ffmpeg: Path) -> dict:
    started = time.monotonic()
    source = item["_source"]
    source_hash = item["_source_sha256"]
    relative = item["RelativePath"]
    if sha256(source) != source_hash:
        raise ValueError(f"original changed before candidate build: {item['Pack']}/{relative}")
    candidate = output_root / "candidate_packs" / item["Pack"] / Path(relative)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    if candidate.exists():
        raise FileExistsError(f"candidate already exists in new stage: {candidate}")
    temporary = candidate.with_name(candidate.stem + ".partial.ogg")
    if temporary.exists():
        raise FileExistsError(f"unfamiliar partial candidate exists: {temporary}")
    factor = decision["factor"]
    is_review = factor is None
    exact_copy = is_review or float(factor) == 1.0
    applied_factor = None if is_review else float(factor)
    encode_seconds = 0.0
    copy_seconds = 0.0
    if exact_copy:
        copy_started = time.monotonic()
        copy_to_exclusive(source, temporary)
        copy_seconds = time.monotonic() - copy_started
        method = "review-required-placeholder-exact-copy" if is_review else "factor-1-exact-copy"
    else:
        encode_started = time.monotonic()
        encode_candidate(ffmpeg, source, temporary, applied_factor)
        encode_seconds = time.monotonic() - encode_started
        method = "ffmpeg-atempo-libvorbis-q5"
    if sha256(source) != source_hash:
        raise ValueError(f"original changed during candidate build: {item['Pack']}/{relative}")
    decode_started = time.monotonic()
    candidate_info = decode_candidate(temporary)
    decode_seconds = time.monotonic() - decode_started
    if candidate_info["sample_rate"] != int(item["SampleRate"]) or candidate_info["channels"] != int(item["Channels"]):
        raise ValueError(f"candidate changed sample rate or channel count: {item['Pack']}/{relative}")
    expected_duration = float(item["OriginalDuration"]) if exact_copy else float(item["OriginalDuration"]) / applied_factor
    if abs(candidate_info["duration"] - expected_duration) > 0.05 + expected_duration * 0.01:
        raise ValueError(f"candidate tempo/duration validation failed: {item['Pack']}/{relative}")
    candidate_hash = sha256(temporary)
    candidate_bytes = temporary.stat().st_size
    if exact_copy and (candidate_hash.casefold() != source_hash.casefold() or candidate_bytes != int(item["OriginalBytes"])):
        raise ValueError(f"required byte-exact original candidate copy failed: {item['Pack']}/{relative}")
    if sha256(temporary) != candidate_hash:
        raise ValueError(f"candidate changed during validation: {item['Pack']}/{relative}")
    os.rename(temporary, candidate)
    if sha256(candidate) != candidate_hash:
        raise ValueError(f"candidate changed during atomic publication: {item['Pack']}/{relative}")
    return {"pack": item["Pack"], "relative_path": relative, "source_sha256": source_hash,
            "key": item["Key"], "original_duration": float(item["OriginalDuration"]),
            "original_bytes": int(item["OriginalBytes"]), "sample_rate": int(item["SampleRate"]),
            "channels": int(item["Channels"]),
            "candidate_sha256": candidate_hash, "candidate_bytes": candidate_bytes,
            "candidate_path": candidate.relative_to(output_root).as_posix(),
            "proposal_status": decision["status"], "proposed_factor": factor,
            "applied_factor": applied_factor, "candidate_status": "review_placeholder" if is_review else "built",
            "measured_tempo_factor": float(item["OriginalDuration"]) / candidate_info["duration"],
            "build_method": method, "encode_seconds": encode_seconds, "copy_seconds": copy_seconds,
            "decode_seconds": decode_seconds, "build_seconds": time.monotonic() - started,
            "decode_status": "passed", "decode": candidate_info}


def update_run_manifest(path: Path, data: dict, initial: bool = False) -> None:
    payload = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if initial:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return
    if not path.is_file():
        raise FileNotFoundError(f"owned run manifest disappeared: {path}")
    previous = json.loads(path.read_text(encoding="utf-8"))
    if previous.get("run_id") != data.get("run_id"):
        raise ValueError("refusing to replace a run manifest not owned by this process")
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def validate_candidate_stage(stage: Path, output: Path) -> dict:
    assert_no_reparse_ancestors(Path(stage), include_leaf=True)
    assert_no_reparse_ancestors(Path(output), include_leaf=True)
    reject_wow_addons_path(Path(output).absolute(), "candidate validation output")
    stage, output = stage.resolve(strict=True), output.resolve(strict=True)
    if os.name != "nt" or stage.drive.upper() != "V:" or output.drive.upper() != "V:":
        raise ValueError("candidate validation is installation-specific and requires both paths on V:")
    if inside(output, stage) or inside(stage, output):
        raise ValueError("candidate output and source stage must remain separate")
    run_path, analysis_path = output / "run-manifest.json", output / "analysis-report.json"
    if not run_path.is_file() or not analysis_path.is_file():
        raise ValueError("candidate stage is missing its run or analysis report")
    run = json.loads(run_path.read_text(encoding="utf-8"))
    analysis_report = json.loads(analysis_path.read_text(encoding="utf-8"))
    if run.get("schema") != RUN_SCHEMA or run.get("scope") not in {"sample32", "full"}:
        raise ValueError("unsupported candidate-only run manifest")
    if run.get("deployable") is not False or run.get("deployment_authorized") is not False or run.get("non_deployable") is not True:
        raise ValueError("candidate stage is not explicitly marked non-deployable")
    if run.get("status") not in {"validating_candidate_only", "candidate_only_review_required",
                                  "candidate_only_human_review_pending", "sample_candidate_only_not_full_corpus"}:
        raise ValueError("candidate stage has not reached validation")
    before_manifest_hash = sha256(stage / "manifest.json")
    _, all_records, table_sources = load_and_validate_stage(stage)
    after_manifest_hash = sha256(stage / "manifest.json")
    if before_manifest_hash != after_manifest_hash or before_manifest_hash != run.get("stage_manifest_sha256"):
        raise ValueError("full-stage manifest changed during candidate validation")
    all_by_key = {audio_key(item): item for item in all_records}
    all_by_pack_key = {(item["Pack"], item["Key"]): item for item in all_records}
    calibration = load_pilot_calibration(all_records)
    if run.get("calibration") != calibration or analysis_report.get("reference") != calibration:
        raise ValueError("candidate calibration approval/reference binding changed")
    if sha256(analysis_path).casefold() != run.get("analysis_report_sha256", "").casefold():
        raise ValueError("candidate analysis report hash does not match its run manifest")
    selected = select_sample32(all_records) if run["scope"] == "sample32" else all_records
    expected_keys = {audio_key(item) for item in selected}
    if run.get("source_corpus_count") != EXPECTED_AUDIO_COUNT or run.get("selected_count") != len(selected):
        raise ValueError("candidate stage source/selection count disagrees with frozen corpus contract")
    if run.get("pack_counts") != PACK_COUNTS:
        raise ValueError("candidate stage pack-count contract changed")
    if analysis_report.get("schema") != RUN_SCHEMA or analysis_report.get("scope") != run["scope"]:
        raise ValueError("candidate analysis report schema/scope mismatch")
    decisions = analysis_report.get("decisions")
    if not isinstance(decisions, list) or len(decisions) != len(selected):
        raise ValueError("analysis report does not have one decision per selected source")
    if (analysis_report.get("analyzed_count") != len(selected)
            or analysis_report.get("proposed_count") != sum(row.get("factor") is not None for row in decisions)
            or analysis_report.get("review_required_count") != sum(row.get("factor") is None for row in decisions)):
        raise ValueError("analysis report proposal/analyzed counts are inconsistent")
    decisions_by_key = {}
    for decision in decisions:
        key = (decision.get("pack"), decision.get("relative_path"))
        if key in decisions_by_key or key not in expected_keys:
            raise ValueError(f"duplicate or unexpected analysis decision: {key}")
        source = all_by_key[key]
        if decision.get("source_sha256", "").casefold() != source["_source_sha256"].casefold():
            raise ValueError(f"analysis report source hash mismatch: {key}")
        factor = decision.get("factor")
        if factor is not None and (isinstance(factor, bool) or not isinstance(factor, (int, float))
                                   or not math.isfinite(factor) or not 1.0 <= factor <= 1.5):
            raise ValueError(f"invalid proposed factor in analysis report: {key}")
        if decision.get("status") != ("ready" if factor is not None else "review"):
            raise ValueError(f"analysis report proposal status/factor mismatch: {key}")
        if factor is not None and decision.get("review_reasons"):
            raise ValueError(f"analysis report proposes a tempo factor despite review reasons: {key}")
        if factor is None and not decision.get("review_reasons"):
            raise ValueError(f"review-required clip has no recorded uncertainty reason: {key}")
        decisions_by_key[key] = decision
    if set(decisions_by_key) != expected_keys:
        raise ValueError("analysis decision keys do not exactly match selected source keys")
    reference_key = ("Horde", "generated/sounds/quests/880-accept.ogg")
    reference_decision = decisions_by_key.get(reference_key)
    if reference_decision is None or reference_decision.get("source_sha256", "").casefold() != calibration["reference_source_sha256"].casefold():
        raise ValueError("analysis report is missing the approved 880 accept reference decision")
    ratio = float(calibration["duration_ratio"])
    comfort_band = analysis_report.get("comfort_band")
    if (not math.isfinite(ratio) or not 1.30 <= ratio <= 1.60
            or isinstance(comfort_band, bool) or not isinstance(comfort_band, (int, float))
            or not math.isfinite(comfort_band) or not 0 <= comfort_band <= 0.25
            or run.get("comfort_band") != comfort_band):
        raise ValueError("recorded decision ratio or comfort-band configuration is invalid")
    reference_summary = reference_decision.get("analysis")
    for decision in decisions:
        validate_decision_policy(reference_summary, decision, ratio, float(comfort_band))
    review_count = sum(decision.get("factor") is None for decision in decisions)
    expected_final_status = final_candidate_status(run["scope"], review_count)
    if run.get("intended_final_status") != expected_final_status:
        raise ValueError("candidate run status does not match full/sample scope and review-required count")
    if run.get("status") != "validating_candidate_only" and run.get("status") != expected_final_status:
        raise ValueError("final candidate run status does not match full/sample scope and review-required count")

    candidate_rows = run.get("candidate_items")
    if not isinstance(candidate_rows, list) or len(candidate_rows) != len(selected):
        raise ValueError("candidate manifest does not cover each selected clip")
    candidate_by_key = {}
    for row in candidate_rows:
        key = (row.get("pack"), row.get("relative_path"))
        if key in candidate_by_key or key not in expected_keys:
            raise ValueError(f"duplicate or unexpected candidate entry: {key}")
        candidate_by_key[key] = row
    if set(candidate_by_key) != expected_keys:
        raise ValueError("candidate output keys do not exactly match selected source keys")

    expected_files = set()
    decoded_count = encoded_count = exact_copy_count = placeholder_count = 0
    for key in sorted(expected_keys, key=lambda value: (PACK_ORDER.index(value[0]), value[1])):
        source = all_by_key[key]
        decision = decisions_by_key[key]
        candidate = candidate_by_key[key]
        expected_relative = f"candidate_packs/{key[0]}/{key[1]}"
        if candidate.get("candidate_path") != expected_relative or candidate.get("key") != source["Key"]:
            raise ValueError(f"candidate path/key mismatch: {key}")
        output_path = output / PurePosixPath(expected_relative)
        assert_no_reparse(output_path, output)
        if not output_path.is_file():
            raise FileNotFoundError(f"candidate is missing: {output_path}")
        expected_files.add(expected_relative)
        actual_hash = sha256(output_path)
        if actual_hash.casefold() != candidate.get("candidate_sha256", "").casefold():
            raise ValueError(f"candidate SHA-256 mismatch: {key}")
        actual_bytes = output_path.stat().st_size
        if actual_bytes != candidate.get("candidate_bytes"):
            raise ValueError(f"candidate byte count mismatch: {key}")
        if candidate.get("source_sha256", "").casefold() != source["_source_sha256"].casefold():
            raise ValueError(f"candidate source hash mismatch: {key}")
        if candidate.get("original_bytes") != source["OriginalBytes"] or candidate.get("original_duration") != source["OriginalDuration"]:
            raise ValueError(f"candidate source metadata mismatch: {key}")
        if candidate.get("sample_rate") != source["SampleRate"] or candidate.get("channels") != source["Channels"]:
            raise ValueError(f"candidate expected format metadata mismatch: {key}")
        if candidate.get("proposed_factor") != decision.get("factor") or candidate.get("proposal_status") != decision.get("status"):
            raise ValueError(f"candidate build did not honor its proposed decision: {key}")
        decoded = decode_candidate(output_path)
        if decoded != candidate.get("decode") or candidate.get("decode_status") != "passed":
            raise ValueError(f"candidate full-decode report does not match revalidation: {key}")
        if decoded["sample_rate"] != source["SampleRate"] or decoded["channels"] != source["Channels"]:
            raise ValueError(f"candidate codec/rate/channel validation failed: {key}")
        factor = decision.get("factor")
        measured_factor = float(source["OriginalDuration"]) / decoded["duration"]
        if not math.isfinite(measured_factor) or abs(measured_factor - float(candidate.get("measured_tempo_factor", 0))) > 1e-12:
            raise ValueError(f"candidate measured tempo ratio mismatch: {key}")
        if factor is None:
            if (candidate.get("candidate_status") != "review_placeholder" or candidate.get("applied_factor") is not None
                    or candidate.get("build_method") != "review-required-placeholder-exact-copy"
                    or actual_hash.casefold() != source["_source_sha256"].casefold()
                    or actual_bytes != source["OriginalBytes"]):
                raise ValueError(f"uncertain clip is not an exact-copy review placeholder: {key}")
            expected_duration = float(source["OriginalDuration"])
            placeholder_count += 1
            exact_copy_count += 1
        elif float(factor) == 1.0:
            if (candidate.get("candidate_status") != "built" or candidate.get("applied_factor") != 1.0
                    or candidate.get("build_method") != "factor-1-exact-copy"
                    or actual_hash.casefold() != source["_source_sha256"].casefold()
                    or actual_bytes != source["OriginalBytes"]):
                raise ValueError(f"factor-1 candidate is not an exact original copy: {key}")
            expected_duration = float(source["OriginalDuration"])
            exact_copy_count += 1
        else:
            if (candidate.get("candidate_status") != "built" or candidate.get("applied_factor") != factor
                    or candidate.get("build_method") != "ffmpeg-atempo-libvorbis-q5"):
                raise ValueError(f"encoded candidate recipe/status mismatch: {key}")
            expected_duration = float(source["OriginalDuration"]) / float(factor)
            encoded_count += 1
        if abs(decoded["duration"] - expected_duration) > 0.05 + expected_duration * 0.01:
            raise ValueError(f"candidate measured duration/factor check failed: {key}")
        decoded_count += 1

    table_outputs = run.get("table_outputs")
    if run["scope"] == "full":
        if not isinstance(table_outputs, list) or len(table_outputs) != EXPECTED_TABLES:
            raise ValueError("full-corpus candidate stage must contain four rewritten timing tables")
        for pack in PACK_ORDER:
            relative = f"candidate_packs/{pack}/generated/sound_length_table.lua"
            record = next((row for row in table_outputs if row.get("pack") == pack), None)
            if record is None or record.get("relative_path") != relative:
                raise ValueError(f"missing candidate timing-table manifest record: {pack}")
            table_path = output / PurePosixPath(relative)
            assert_no_reparse(table_path, output)
            if not table_path.is_file() or sha256(table_path).casefold() != record.get("sha256", "").casefold():
                raise ValueError(f"candidate timing table is missing or has a hash mismatch: {pack}")
            expected_files.add(relative)
            parsed = parse_timing_table(table_path.read_bytes(), f"{pack} candidate table")
            original_table = table_sources[pack]["parsed"]
            keys = table_sources[pack]["keys"]
            if set(parsed["values"]) != keys or mask_timing_text(parsed["text"], keys) != mask_timing_text(original_table["text"], keys):
                raise ValueError(f"candidate timing table keys or unrelated text changed: {pack}")
            for key in keys:
                source = all_by_pack_key[(pack, key)]
                candidate = candidate_by_key[audio_key(source)]
                if float(parsed["values"][key]) != candidate["decode"]["duration"]:
                    raise ValueError(f"candidate timing table does not match measured audio duration: {pack}/{key}")
    elif table_outputs:
        raise ValueError("sample-only stage must not contain incomplete timing-table outputs")

    candidate_root = output / "candidate_packs"
    assert_no_reparse(candidate_root, output)
    actual_files = set()
    for path in candidate_root.rglob("*"):
        if is_reparse(path):
            raise ValueError(f"unexpected reparse point in candidate tree: {path}")
        if path.is_file():
            actual_files.add(path.relative_to(output).as_posix())
    if actual_files != expected_files:
        raise ValueError(f"candidate output file coverage mismatch; missing={sorted(expected_files - actual_files)[:8]}, "
                         f"extra={sorted(actual_files - expected_files)[:8]}")
    expected_counts = {"candidate_count": len(selected), "decoded_count": decoded_count,
                       "analysis_count": len(decisions),
                       "proposed_count": sum(row.get("factor") is not None for row in decisions),
                       "review_required_count": sum(row.get("factor") is None for row in decisions),
                       "built_count": len(selected) - placeholder_count,
                       "encoded_count": encoded_count, "exact_copy_count": exact_copy_count,
                       "review_placeholder_count": placeholder_count,
                       "timing_table_count": EXPECTED_TABLES if run["scope"] == "full" else 0}
    for name, value in expected_counts.items():
        if run.get(name) != value:
            raise ValueError(f"candidate run summary count mismatch for {name}: {run.get(name)} != {value}")
    for item in all_records:
        if sha256(item["_source"]).casefold() != item["_source_sha256"].casefold():
            raise ValueError(f"original changed during final candidate validation: {item['Pack']}/{item['RelativePath']}")
    for pack, table in table_sources.items():
        if sha256(table["path"]).casefold() != table["sha256"].casefold():
            raise ValueError(f"original timing table changed during final candidate validation: {pack}")
    if sha256(stage / "manifest.json") != before_manifest_hash:
        raise ValueError("full-stage manifest changed during final candidate validation")
    return {"status": "passed", "checked_source_hashes": len(all_records),
            "checked_candidate_hashes": len(candidate_rows), "full_candidate_decodes": decoded_count,
            "checked_timing_tables": EXPECTED_TABLES if run["scope"] == "full" else 0,
            "output_file_coverage": "exact", "source_manifest_unchanged": True}


def percentile90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(0.90 * len(ordered)) - 1)])


def throughput_report(decisions: list[dict], candidates: list[dict], analysis_wall: float,
                      build_wall: float) -> dict:
    asr_rows = [row for row in decisions if row.get("analysis_state") == "analyzed"]
    asr_times = [float(row["analysis_seconds"]) for row in asr_rows if float(row.get("analysis_seconds", 0)) > 0]
    encode_rows = [row for row in candidates if row["build_method"] == "ffmpeg-atempo-libvorbis-q5"]
    encode_seconds = sum(float(row["encode_seconds"]) for row in encode_rows)
    decode_seconds = sum(float(row["decode_seconds"]) for row in candidates)
    candidate_times = [float(row["build_seconds"]) for row in candidates]
    margin = 1.5
    asr_p90 = percentile90(asr_times)
    candidate_p90 = percentile90(candidate_times)
    projected_new_asr = max(0, EXPECTED_AUDIO_COUNT - len(decisions))
    projected_candidates = max(0, EXPECTED_AUDIO_COUNT - len(candidates))
    projected_asr_seconds = asr_p90 * projected_new_asr * margin if asr_p90 is not None else None
    projected_build_seconds = candidate_p90 * projected_candidates * margin if candidate_p90 is not None else None
    return {"sample_analysis_wall_seconds": analysis_wall,
            "new_asr_clip_count": len(asr_times), "asr_inference_and_decode_seconds": sum(asr_times),
            "new_asr_clips_per_second": len(asr_times) / sum(asr_times) if sum(asr_times) else None,
            "sample_analysis_clips_per_second_wall": len(decisions) / analysis_wall if analysis_wall > 0 else None,
            "asr_p90_seconds_per_new_clip": asr_p90,
            "sample_candidate_build_wall_seconds": build_wall,
            "candidate_clips_per_second_wall": len(candidates) / build_wall if build_wall > 0 else None,
            "encoded_clip_count": len(encode_rows),
            "encoded_clips_per_second_worker_time": len(encode_rows) / encode_seconds if encode_seconds else None,
            "full_decode_clip_count": len(candidates),
            "decoded_clips_per_second_worker_time": len(candidates) / decode_seconds if decode_seconds else None,
            "candidate_p90_seconds_per_clip": candidate_p90,
            "projection": {"method": "sample p90 seconds per clip x remaining clips x 1.5 margin",
                           "conservative_margin": margin,
                           "full_asr_clips_remaining_after_cache": projected_new_asr,
                           "full_candidate_clips_remaining_after_sample": projected_candidates,
                           "projected_asr_seconds": projected_asr_seconds,
                           "projected_asr_hms": estimate_total_seconds(projected_asr_seconds),
                           "projected_candidate_build_seconds": projected_build_seconds,
                           "projected_candidate_build_hms": estimate_total_seconds(projected_build_seconds)}}


def estimate_total_seconds(value: object) -> str | None:
    if value is None or not math.isfinite(float(value)):
        return None
    seconds = max(0, int(float(value)))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def run_self_tests() -> None:
    import numpy as np

    Pilot.run_synthetic_tests()
    synthetic = []
    for i in range(40):
        pack = PACK_ORDER[i % len(PACK_ORDER)]
        synthetic.append({"Pack": pack, "RelativePath": f"generated/sounds/quests/{i:04d}.ogg", "Key": f"{i:04d}"})
    sample = select_sample32_for_test(synthetic)
    if len(sample) != 32 or len({audio_key(row) for row in sample}) != 32:
        raise AssertionError("sample selection did not produce 32 unique clips")
    inventory = {"generated/sounds/quests/alpha.ogg", "generated/sounds/gossip/beta.ogg"}
    validate_ogg_inventory("self-test", set(inventory), set(inventory))
    try:
        validate_ogg_inventory("self-test", set(inventory) | {"generated/sounds/followup/extra.ogg"}, set(inventory))
    except ValueError:
        pass
    else:
        raise AssertionError("original OGG inventory mismatch was not rejected")
    bom_table = b'\xef\xbb\xbfreturn {\r\n  ["alpha"] = 2.5,\r\n  ["beta"] = 3,\r\n}\r\n'
    parsed = parse_timing_table(bom_table, "self-test")
    rewritten = rewrite_timing_table(parsed, {"alpha": 1.25, "beta": 3.0}, {"alpha", "beta"}, "self-test")
    reparsed = parse_timing_table(rewritten, "self-test output")
    if not rewritten.startswith(b"\xef\xbb\xbf") or float(reparsed["values"]["alpha"]) != 1.25:
        raise AssertionError("timing table rewrite/BOM preservation failed")
    try:
        parse_timing_table(b'return {\n ["alpha"] = dynamic_value,\n}\n', "malformed self-test")
    except ValueError:
        pass
    else:
        raise AssertionError("dynamic timing-table entry was not rejected")
    words = [{"text": "word", "start": i * 0.25, "end": i * 0.25 + 0.2,
              "probability": 0.95, "count": 1} for i in range(16)]
    rates, sizes, pauses, excluded = Pilot.pace_metrics(words)
    analysis = {"transcript": " ".join("word" for _ in words), "words": words, "decoded_seconds": 5.0,
                "transcript_word_count": 16, "timestamped_word_count": 16, "word_coverage": 1.0,
                "raw_word_coverage_ratio": 1.0, "aligned_word_count_exceeds_transcript": False,
                "mean_word_probability": 0.95, "low_confidence_words": 0, "language": "en",
                "language_probability": 0.99, "phrase_window_wpm": rates, "phrase_window_word_counts": sizes,
                "phrase_window_word_coverage": sum(sizes) / 16, "pace_eligible_word_count": 16,
                "phrase_window_word_count": sum(sizes), "excluded_window_word_count": excluded,
                "median_phrase_wpm": float(np.median(rates)),
                "p90_phrase_wpm": float(np.percentile(rates, 90)), "long_pause_count": len(pauses),
                "long_pause_total_seconds": sum(pauses), "long_pause_p90_seconds": 0.0,
                "overall_wpm_including_pauses": 192.0, "review_reasons": []}
    validate_analysis(analysis, "synthetic valid analysis")
    reference_summary = analysis_summary(analysis)
    validate_analysis_summary(reference_summary, "synthetic valid report summary")

    weighted_words = [{"text": "word word" if i == 11 else "word", "start": i * 0.3,
                       "end": i * 0.3 + 0.2, "probability": 0.95, "count": 2 if i == 11 else 1}
                      for i in range(100)]
    weighted_rates, weighted_sizes, weighted_pauses, weighted_excluded = Pilot.pace_metrics(weighted_words)
    weighted_word_total = sum(word["count"] for word in weighted_words)
    weighted_analysis = {"transcript": " ".join(word["text"] for word in weighted_words),
                         "words": weighted_words, "decoded_seconds": 30.0,
                         "transcript_word_count": weighted_word_total,
                         "timestamped_word_count": weighted_word_total, "word_coverage": 1.0,
                         "raw_word_coverage_ratio": 1.0, "aligned_word_count_exceeds_transcript": False,
                         "mean_word_probability": 0.95, "low_confidence_words": 0, "language": "en",
                         "language_probability": 0.99, "phrase_window_wpm": weighted_rates,
                         "phrase_window_word_counts": weighted_sizes,
                         "phrase_window_word_coverage": sum(weighted_sizes) / weighted_word_total,
                         "pace_eligible_word_count": weighted_word_total,
                         "phrase_window_word_count": sum(weighted_sizes),
                         "excluded_window_word_count": weighted_excluded,
                         "median_phrase_wpm": float(np.median(weighted_rates)),
                         "p90_phrase_wpm": float(np.percentile(weighted_rates, 90)),
                         "long_pause_count": len(weighted_pauses),
                         "long_pause_total_seconds": sum(weighted_pauses), "long_pause_p90_seconds": 0.0,
                         "overall_wpm_including_pauses": 60.0 * weighted_word_total / 30.0,
                         "review_reasons": []}
    validate_analysis(weighted_analysis, "synthetic doubled lexical-token analysis")
    weighted_summary = analysis_summary(weighted_analysis)
    validate_analysis_summary(weighted_summary, "synthetic doubled lexical-token report summary")
    if (13 not in weighted_sizes or sum(weighted_sizes) != weighted_word_total
            or weighted_summary["phrase_window_word_coverage"] != 1.0
            or weighted_summary["review_reasons"]):
        raise AssertionError("count-2 aligned-row overshoot or review coverage was not preserved")
    wrong_weighted_sizes = dict(weighted_analysis)
    wrong_weighted_sizes["phrase_window_word_counts"] = list(weighted_sizes)
    wrong_weighted_sizes["phrase_window_word_counts"][0] -= 1
    try:
        validate_analysis(wrong_weighted_sizes, "synthetic altered weighted window")
    except ValueError as exc:
        diagnostic = str(exc)
        if "observed_sizes=" not in diagnostic or "expected_sizes=" not in diagnostic or "max_eligible_row_lexical_count=2" not in diagnostic:
            raise AssertionError("exact window mismatch diagnostic omitted observed/expected sizes or row weight") from exc
    else:
        raise AssertionError("validator accepted weighted window sizes that differ from pace_metrics")

    nonmonotonic_words = [dict(word) for word in weighted_words]
    nonmonotonic_words[11]["start"], nonmonotonic_words[11]["end"] = 2.95, 3.15
    quality_timed, pace_eligible, timing_flags = Pilot.classify_word_timings(nonmonotonic_words, 30.0)
    pace_rates, pace_sizes, pace_pauses, pace_excluded = Pilot.pace_metrics(pace_eligible)
    quality_count = sum(word["count"] for word in quality_timed)
    pace_count = sum(word["count"] for word in pace_eligible)
    nonmonotonic_analysis = {"transcript": " ".join(word["text"] for word in nonmonotonic_words),
                             "words": nonmonotonic_words, "decoded_seconds": 30.0,
                             "transcript_word_count": weighted_word_total,
                             "timestamped_word_count": quality_count,
                             "word_coverage": quality_count / weighted_word_total,
                             "raw_word_coverage_ratio": quality_count / weighted_word_total,
                             "aligned_word_count_exceeds_transcript": False,
                             "mean_word_probability": 0.95, "low_confidence_words": 0, "language": "en",
                             "language_probability": 0.99, "phrase_window_wpm": pace_rates,
                             "phrase_window_word_counts": pace_sizes,
                             "phrase_window_word_coverage": sum(pace_sizes) / pace_count,
                             "pace_eligible_word_count": pace_count,
                             "phrase_window_word_count": sum(pace_sizes),
                             "excluded_window_word_count": pace_excluded,
                             "median_phrase_wpm": float(np.median(pace_rates)),
                             "p90_phrase_wpm": float(np.percentile(pace_rates, 90)),
                             "long_pause_count": len(pace_pauses),
                             "long_pause_total_seconds": sum(pace_pauses), "long_pause_p90_seconds": 0.0,
                             "overall_wpm_including_pauses": 60.0 * weighted_word_total / 30.0,
                             "review_reasons": timing_flags}
    validate_analysis(nonmonotonic_analysis, "synthetic nonmonotonic count-2 row")
    nonmonotonic_summary = analysis_summary(nonmonotonic_analysis)
    validate_analysis_summary(nonmonotonic_summary, "synthetic nonmonotonic report summary")
    blocked_policy = Pilot.decide_factor(nonmonotonic_summary, nonmonotonic_summary, 1.5, 0.10)
    if (len(quality_timed) != 99 or len(pace_eligible) != 100 or "non-monotonic word timestamps" not in timing_flags
            or 13 not in pace_sizes or nonmonotonic_summary["phrase_window_word_coverage"] != 1.0
            or blocked_policy["factor"] is not None):
        raise AssertionError("nonmonotonic count-2 pace input, coverage, or review blocking diverged")

    expected_policy = Pilot.decide_factor(reference_summary, reference_summary, 1.5, 0.10)
    decision = {"pack": "Horde", "relative_path": "generated/sounds/quests/880-accept.ogg",
                "analysis": reference_summary, **expected_policy}
    validate_decision_policy(reference_summary, decision, 1.5, 0.10)
    altered_decision = dict(decision)
    altered_decision["factor"] = 1.0 if decision["factor"] != 1.0 else 1.5
    try:
        validate_decision_policy(reference_summary, altered_decision, 1.5, 0.10)
    except ValueError:
        pass
    else:
        raise AssertionError("validator failed to catch an altered adaptive proposal")

    def assert_reason_cannot_be_removed(case: dict, reason: str) -> None:
        if reason not in case["review_reasons"]:
            raise AssertionError(f"synthetic case did not derive expected blocker: {reason}")
        tampered = dict(case)
        tampered["review_reasons"] = [value for value in case["review_reasons"] if value != reason]
        try:
            validate_analysis(tampered, f"tampered raw analysis missing {reason}")
        except ValueError:
            pass
        else:
            raise AssertionError(f"raw-analysis validator accepted omitted review blocker: {reason}")
        compact = analysis_summary(case)
        compact["review_reasons"] = [value for value in compact["review_reasons"] if value != reason]
        try:
            validate_analysis_summary(compact, f"tampered summary missing {reason}")
        except ValueError:
            pass
        else:
            raise AssertionError(f"report validator accepted omitted review blocker: {reason}")

    alignment_mismatch = dict(analysis)
    alignment_mismatch.update({"transcript": " ".join(["word"] * 15), "transcript_word_count": 15,
                               "word_coverage": 1.0, "raw_word_coverage_ratio": 16 / 15,
                               "aligned_word_count_exceeds_transcript": True,
                               "overall_wpm_including_pauses": 180.0,
                               "review_reasons": [Pilot.ALIGNMENT_MISMATCH_REASON]})
    validate_analysis(alignment_mismatch, "synthetic ASR transcript/alignment mismatch")
    alignment_summary = analysis_summary(alignment_mismatch)
    validate_analysis_summary(alignment_summary, "synthetic alignment-mismatch summary")
    if (alignment_summary["word_coverage"] != 1.0
            or alignment_summary["raw_word_coverage_ratio"] <= 1.0
            or Pilot.decide_factor(reference_summary, alignment_summary, 1.5, 0.10)["factor"] is not None):
        raise AssertionError("alignment mismatch was clamped silently or failed to block a proposal")
    assert_reason_cannot_be_removed(alignment_mismatch, Pilot.ALIGNMENT_MISMATCH_REASON)
    unbounded_public_coverage = dict(alignment_mismatch, word_coverage=16 / 15)
    try:
        validate_analysis(unbounded_public_coverage, "synthetic unbounded public coverage")
    except ValueError:
        pass
    else:
        raise AssertionError("raw-analysis validator accepted word_coverage above 1")

    def make_pace_analysis(rows: list[dict], duration: float) -> dict:
        transcript = " ".join(row["text"] for row in rows)
        transcript_count = len(Pilot.WORD_RE.findall(transcript))
        timed, pace_eligible, timestamp_flags = Pilot.classify_word_timings(rows, duration)
        timed_count = sum(row["count"] for row in timed)
        raw_coverage = timed_count / transcript_count if transcript_count else None
        word_coverage = min(1.0, raw_coverage) if raw_coverage is not None else 0.0
        pace_count = sum(row["count"] for row in pace_eligible)
        rates, sizes, pauses, excluded = Pilot.pace_metrics(pace_eligible)
        window_coverage = sum(sizes) / pace_count if pace_count else 0.0
        reasons = list(timestamp_flags)
        if timed_count > transcript_count:
            reasons.append(Pilot.ALIGNMENT_MISMATCH_REASON)
        if not rates:
            reasons.append("insufficient meaningful phrase windows")
        if window_coverage < 0.70:
            reasons.append("insufficient meaningful phrase-window coverage")
        if excluded and window_coverage < 0.70:
            reasons.append(Pilot.PACE_WINDOW_EXCLUSION_REASON)
        if transcript_count < Pilot.MIN_WINDOW_WORDS or timed_count < Pilot.MIN_WINDOW_WORDS:
            reasons.append("insufficient words for pace decision")
        return {"transcript": transcript, "words": rows, "decoded_seconds": duration,
                "transcript_word_count": transcript_count, "timestamped_word_count": timed_count,
                "word_coverage": word_coverage,
                "raw_word_coverage_ratio": raw_coverage,
                "aligned_word_count_exceeds_transcript": timed_count > transcript_count,
                "mean_word_probability": 0.95, "low_confidence_words": 0, "language": "en",
                "language_probability": 0.99, "phrase_window_wpm": rates,
                "phrase_window_word_counts": sizes, "phrase_window_word_coverage": window_coverage,
                "pace_eligible_word_count": pace_count, "phrase_window_word_count": sum(sizes),
                "excluded_window_word_count": excluded,
                "median_phrase_wpm": float(np.median(rates)) if rates else None,
                "p90_phrase_wpm": float(np.percentile(rates, 90)) if rates else None,
                "long_pause_count": len(pauses), "long_pause_total_seconds": sum(pauses),
                "long_pause_p90_seconds": float(np.percentile(pauses, 90)) if pauses else 0.0,
                "overall_wpm_including_pauses": 60.0 * transcript_count / duration,
                "review_reasons": sorted(set(reasons))}

    tail_words = [{"text": "word word" if i < 6 else "word", "start": i * 0.3,
                   "end": i * 0.3 + 0.2, "probability": 0.95, "count": 2 if i < 6 else 1}
                  for i in range(94)]
    tail_analysis = make_pace_analysis(tail_words, 30.0)
    validate_analysis(tail_analysis, "synthetic 100-lexical-word weighted tail")
    tail_summary = analysis_summary(tail_analysis)
    validate_analysis_summary(tail_summary, "synthetic 100-lexical-word tail summary")
    if (tail_analysis["pace_eligible_word_count"] != 100
            or tail_analysis["phrase_window_word_count"] != 100
            or tail_analysis["excluded_window_word_count"] != 0
            or tail_analysis["phrase_window_word_coverage"] != 1.0
            or tail_analysis["review_reasons"]):
        raise AssertionError("100-lexical-word weighted tail was omitted or spuriously reviewed")
    omitted_tail = dict(tail_analysis)
    omitted_tail["phrase_window_word_counts"] = list(tail_analysis["phrase_window_word_counts"])
    omitted_tail["phrase_window_word_counts"][-1] -= 1
    omitted_tail["phrase_window_word_count"] -= 1
    try:
        validate_analysis(omitted_tail, "synthetic omitted weighted tail")
    except ValueError:
        pass
    else:
        raise AssertionError("raw-analysis validator accepted omitted weighted tail lexical words")

    short_tail_rows = ([{"text": "word", "start": i * 0.3, "end": i * 0.3 + 0.2,
                         "probability": 0.95, "count": 1} for i in range(96)] +
                       [{"text": "word", "start": 40.0 + i * 0.3, "end": 40.2 + i * 0.3,
                         "probability": 0.95, "count": 1} for i in range(4)])
    excluded_tail = make_pace_analysis(short_tail_rows, 45.0)
    validate_analysis(excluded_tail, "synthetic short pause-separated tail")
    excluded_summary = analysis_summary(excluded_tail)
    validate_analysis_summary(excluded_summary, "synthetic short-tail summary")
    if (excluded_tail["phrase_window_word_count"] != 96
            or excluded_tail["excluded_window_word_count"] != 4
            or excluded_tail["phrase_window_word_coverage"] <= 0.70
            or Pilot.PACE_WINDOW_EXCLUSION_REASON in excluded_tail["review_reasons"]
            or excluded_tail["review_reasons"]):
        raise AssertionError("4/100 short-tail words were not disclosed while remaining eligible")
    if Pilot.decide_factor(excluded_summary, excluded_summary, 1.5, 0.10)["factor"] is None:
        raise AssertionError("4/100 excluded words incorrectly blocked an otherwise valid decision")
    bad_excluded_count = dict(excluded_tail, excluded_window_word_count=0)
    try:
        validate_analysis(bad_excluded_count, "synthetic underreported excluded tail")
    except ValueError:
        pass
    else:
        raise AssertionError("raw-analysis validator accepted underreported excluded tail words")
    bad_excluded_summary = dict(excluded_summary, excluded_window_word_count=0)
    try:
        validate_analysis_summary(bad_excluded_summary, "synthetic underreported tail summary")
    except ValueError:
        pass
    else:
        raise AssertionError("report validator accepted underreported excluded tail words")

    blocking_tail_rows = ([{"text": "word", "start": i * 0.3, "end": i * 0.3 + 0.2,
                            "probability": 0.95, "count": 1} for i in range(60)] +
                          [{"text": "word", "start": 30.0 + group * 2.0 + i * 0.3,
                            "end": 30.2 + group * 2.0 + i * 0.3,
                            "probability": 0.95, "count": 1}
                           for group in range(10) for i in range(4)])
    blocking_tail = make_pace_analysis(blocking_tail_rows, 55.0)
    validate_analysis(blocking_tail, "synthetic 40-percent excluded tails")
    blocking_summary = analysis_summary(blocking_tail)
    validate_analysis_summary(blocking_summary, "synthetic 40-percent tail summary")
    if (blocking_tail["phrase_window_word_count"] != 60
            or blocking_tail["excluded_window_word_count"] != 40
            or blocking_tail["phrase_window_word_coverage"] != 0.60
            or Pilot.PACE_WINDOW_EXCLUSION_REASON not in blocking_tail["review_reasons"]
            or Pilot.decide_factor(blocking_summary, blocking_summary, 1.5, 0.10)["factor"] is not None):
        raise AssertionError("40/100 excluded words did not block the adaptive proposal")
    assert_reason_cannot_be_removed(blocking_tail, Pilot.PACE_WINDOW_EXCLUSION_REASON)

    low_coverage_words = [dict(word) for word in words]
    for word in low_coverage_words[8:]:
        word["start"], word["end"] = -1.0, -0.8
    low_coverage_rates, low_coverage_sizes, low_coverage_pauses, low_coverage_excluded = Pilot.pace_metrics(low_coverage_words[:8])
    low_coverage = dict(analysis)
    low_coverage.update({"words": low_coverage_words, "timestamped_word_count": 8, "word_coverage": 0.5,
                        "raw_word_coverage_ratio": 0.5, "aligned_word_count_exceeds_transcript": False,
                        "phrase_window_wpm": low_coverage_rates, "phrase_window_word_counts": low_coverage_sizes,
                        "phrase_window_word_coverage": sum(low_coverage_sizes) / 8,
                        "pace_eligible_word_count": 8, "phrase_window_word_count": sum(low_coverage_sizes),
                        "excluded_window_word_count": low_coverage_excluded,
                        "median_phrase_wpm": float(np.median(low_coverage_rates)),
                        "p90_phrase_wpm": float(np.percentile(low_coverage_rates, 90)),
                        "long_pause_count": len(low_coverage_pauses),
                        "long_pause_total_seconds": sum(low_coverage_pauses),
                        "long_pause_p90_seconds": float(np.percentile(low_coverage_pauses, 90)) if low_coverage_pauses else 0.0,
                        "review_reasons": ["insufficient word-timestamp coverage", "invalid word timestamp"]})
    validate_analysis(low_coverage, "synthetic low coverage analysis")
    assert_reason_cannot_be_removed(low_coverage, "insufficient word-timestamp coverage")

    low_confidence = dict(analysis)
    low_confidence["words"] = [dict(word, probability=0.20) for word in words]
    low_confidence.update({"mean_word_probability": 0.20, "low_confidence_words": 16,
                           "review_reasons": ["low word confidence"]})
    validate_analysis(low_confidence, "synthetic low confidence analysis")
    assert_reason_cannot_be_removed(low_confidence, "low word confidence")

    non_english = dict(analysis, language="fr", review_reasons=["uncertain English language detection"])
    validate_analysis(non_english, "synthetic non-English analysis")
    assert_reason_cannot_be_removed(non_english, "uncertain English language detection")

    short_words = [dict(word) for word in words[:7]]
    short_rates, short_sizes, short_pauses, short_excluded = Pilot.pace_metrics(short_words)
    no_windows = dict(analysis)
    no_windows.update({"transcript": " ".join(word["text"] for word in short_words), "words": short_words,
                       "transcript_word_count": 7, "timestamped_word_count": 7, "word_coverage": 1.0,
                       "raw_word_coverage_ratio": 1.0, "aligned_word_count_exceeds_transcript": False,
                       "phrase_window_wpm": short_rates, "phrase_window_word_counts": short_sizes,
                       "phrase_window_word_coverage": 0.0, "median_phrase_wpm": None, "p90_phrase_wpm": None,
                       "pace_eligible_word_count": 7, "phrase_window_word_count": 0,
                       "excluded_window_word_count": short_excluded,
                       "long_pause_count": len(short_pauses), "long_pause_total_seconds": sum(short_pauses),
                       "long_pause_p90_seconds": float(np.percentile(short_pauses, 90)) if short_pauses else 0.0,
                       "overall_wpm_including_pauses": 84.0,
                       "review_reasons": sorted(["insufficient meaningful phrase windows",
                                                 "insufficient meaningful phrase-window coverage",
                                                 Pilot.PACE_WINDOW_EXCLUSION_REASON,
                                                 "insufficient words for pace decision"])})
    validate_analysis(no_windows, "synthetic no-window analysis")
    for reason in no_windows["review_reasons"]:
        assert_reason_cannot_be_removed(no_windows, reason)

    if (final_candidate_status("sample32", 0) != "sample_candidate_only_not_full_corpus"
            or final_candidate_status("full", 1) != "candidate_only_review_required"
            or final_candidate_status("full", 0) != "candidate_only_human_review_pending"):
        raise AssertionError("candidate status/scope policy test failed")
    bad = dict(analysis)
    bad["words"] = [dict(word) for word in words]
    bad["words"][0]["probability"] = 1.01
    try:
        validate_analysis(bad, "synthetic invalid confidence")
    except ValueError:
        pass
    else:
        raise AssertionError("out-of-range word confidence was not rejected")
    bad = dict(analysis)
    bad["language_probability"] = float("nan")
    try:
        validate_analysis(bad, "synthetic invalid language confidence")
    except ValueError:
        pass
    else:
        raise AssertionError("nonfinite language confidence was not rejected")
    print("AdaptiveAudioBatch self-tests passed (pace windows, proposal policy, sample selection, strict timing tables).")


def select_sample32_for_test(records: list[dict]) -> list[dict]:
    """Exercise deterministic selection with synthetic rows that include its four required anchors."""
    anchors = [
        {"Pack": "Alliance", "RelativePath": "generated/sounds/quests/783-accept.ogg", "Key": "783-accept"},
        {"Pack": "Alliance", "RelativePath": "generated/sounds/quests/783-complete.ogg", "Key": "783-complete"},
        {"Pack": "Horde", "RelativePath": "generated/sounds/quests/880-accept.ogg", "Key": "880-accept"},
        {"Pack": "Horde", "RelativePath": "generated/sounds/quests/880-complete.ogg", "Key": "880-complete"},
    ]
    return select_sample32(anchors + records)


def main() -> int:
    parser = argparse.ArgumentParser(description="Candidate-only adaptive speech analysis/build; never deploys into WoW.")
    parser.add_argument("--self-test", action="store_true", help="run local pure-function tests without model or filesystem access")
    parser.add_argument("--validate", action="store_true", help="revalidate an existing candidate-only output without loading ASR")
    parser.add_argument("--resource-check", action="store_true", help="check WoW process and GPU headroom without loading ASR or writing files")
    parser.add_argument("--stage", type=Path, help="existing full-corpus stage with immutable originals")
    parser.add_argument("--output", type=Path, help="new V: candidate-only output directory; never overwritten")
    parser.add_argument("--model-cache", type=Path, help="existing local V: faster-whisper cache root")
    parser.add_argument("--ffmpeg", type=Path, help="local ffmpeg.exe; no ffprobe is used")
    parser.add_argument("--scope", choices=("sample32", "full"), default="sample32")
    parser.add_argument("--confirm-full-corpus", help="must exactly equal the recorded user approval text for --scope full")
    parser.add_argument("--comfort-band", type=float, default=0.10)
    parser.add_argument("--workers", type=int, default=8, help="bounded candidate workers (1-8); ASR remains single-process")
    args = parser.parse_args()
    if args.self_test:
        try:
            run_self_tests()
            return 0
        except Exception as exc:
            print(f"AdaptiveAudioBatch self-test failed: {exc}", file=sys.stderr)
            return 1
    if args.resource_check:
        try:
            print(json.dumps(resource_guard("standalone resource check"), indent=2))
            return 0
        except ResourceBlocked as exc:
            print(f"AdaptiveAudioBatch stopped for resource safety: {exc}", file=sys.stderr)
            return 3
        except Exception as exc:
            print(f"AdaptiveAudioBatch resource check failed closed: {exc}", file=sys.stderr)
            return 1
    if args.validate:
        try:
            if args.stage is None or args.output is None:
                raise ValueError("--validate requires --stage and --output")
            result = validate_candidate_stage(args.stage, args.output)
            print(json.dumps(result, indent=2))
            return 0
        except Exception as exc:
            print(f"AdaptiveAudioBatch validation failed closed: {exc}", file=sys.stderr)
            return 1
    output = None
    lock_path = None
    lock_created = False
    status_path = None
    run_record = None
    started = time.monotonic()
    try:
        if any(value is None for value in (args.stage, args.output, args.model_cache, args.ffmpeg)):
            raise ValueError("--stage, --output, --model-cache, and --ffmpeg are required")
        if not 0 <= args.comfort_band <= 0.25 or not math.isfinite(args.comfort_band):
            raise ValueError("--comfort-band must be finite and between 0 and 0.25")
        if args.workers not in range(1, 9):
            raise ValueError("--workers must be between 1 and 8")
        if args.scope == "full" and args.confirm_full_corpus != FULL_APPROVAL_TEXT:
            raise ValueError("full-corpus mode requires --confirm-full-corpus with the exact recorded approval text")
        if args.scope != "full" and args.confirm_full_corpus is not None:
            raise ValueError("--confirm-full-corpus is valid only with --scope full")
        stage, output, model_cache, ffmpeg = validate_locations(args.stage, args.output, args.model_cache, args.ffmpeg)
        initial_manifest_sha = sha256(stage / "manifest.json")
        stage_manifest, all_records, table_sources = load_and_validate_stage(stage)
        if sha256(stage / "manifest.json") != initial_manifest_sha:
            raise ValueError("full-stage manifest changed during initial source validation")
        calibration = load_pilot_calibration(all_records)
        selected = select_sample32(all_records) if args.scope == "sample32" else all_records
        pilot_ratio = calibration["duration_ratio"]
        ffmpeg_ver = ffmpeg_version(ffmpeg)
        runtime = runtime_info()
        cache_dir = model_cache / "adaptive-analysis-v1"
        assert_no_reparse_ancestors(cache_dir, include_leaf=os.path.lexists(cache_dir))
        cache_dir.mkdir(exist_ok=True)
        assert_no_reparse_ancestors(cache_dir, include_leaf=True)
        if not cache_dir.is_dir():
            raise ValueError("analysis cache path is not a directory")
        lock_path = cache_dir / ".adaptive-audio-batch.lock"
        try:
            with lock_path.open("x", encoding="ascii") as lock:
                lock.write(f"pid={os.getpid()}\n")
                lock.flush()
                os.fsync(lock.fileno())
            lock_created = True
        except FileExistsError as exc:
            raise RuntimeError(f"analysis cache is locked; inspect before removing the lock: {lock_path}") from exc

        output.mkdir()
        run_id = uuid.uuid4().hex
        status_path = output / "run-manifest.json"
        analysis_path = output / "analysis-report.json"
        run_record = {"schema": RUN_SCHEMA, "run_id": run_id, "status": "analysis_running",
                      "scope": args.scope, "stage": str(stage), "stage_manifest_sha256": initial_manifest_sha,
                      "output": str(output), "source_corpus_count": len(all_records),
                      "selected_count": len(selected), "pack_counts": PACK_COUNTS,
                      "asr_model": Pilot.MODEL_NAME, "model_revision": Pilot.MODEL_REVISION,
                      "runtime": runtime, "ffmpeg": ffmpeg_ver, "tempo_recipe": TEMPO_RECIPE,
                      "candidate_workers": args.workers, "analysis_processes": 1,
                      "deployment_authorized": False, "calibration": calibration,
                      "decision_policy_version": POLICY_VERSION, "comfort_band": args.comfort_band,
                      "analysis_count": 0, "proposed_count": 0, "review_required_count": 0,
                      "built_count": 0, "decoded_count": 0, "timing_table_count": 0,
                      "resource_checks": []}
        update_run_manifest(status_path, run_record, initial=True)

        def check_resources(label: str) -> None:
            result = resource_guard(label)
            run_record["resource_checks"].append(result)
            update_run_manifest(status_path, run_record)

        check_resources("before ASR/model work")

        reference_item = next(row for row in all_records if row["Pack"] == "Horde"
                              and row["RelativePath"] == "generated/sounds/quests/880-accept.ogg")
        ordered = [reference_item] + [row for row in selected if audio_key(row) != audio_key(reference_item)]
        identities = {audio_key(row): analysis_identity(row["_source_sha256"], runtime) for row in ordered}
        cache_counter = {"new_analysis_count": 0, "cache_race_hits": 0, "asr_seconds": 0.0}
        model = None
        model_load_seconds = 0.0

        def ensure_model():
            nonlocal model, model_load_seconds
            if model is None:
                check_resources("before loading pinned CUDA model")
                model_started = time.monotonic()
                model = load_model(model_cache, runtime)
                model_load_seconds += time.monotonic() - model_started

        identity_ref = identities[audio_key(reference_item)]
        ref_cache_key = hashlib.sha256(json.dumps(identity_ref, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        ref_cache_path = cache_dir / f"{ref_cache_key}.json"
        if not ref_cache_path.exists():
            ensure_model()
        analysis_started = time.monotonic()
        reference, reference_state, reference_cache_key, reference_seconds = source_analysis(
            reference_item, identity_ref, cache_dir, model, cache_counter)
        decisions, analyzed_count, cached_count = [], 1, int(reference_state.startswith("cached"))
        for index, item in enumerate(ordered[1:], start=2):
            identity = identities[audio_key(item)]
            cache_key = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            if not (cache_dir / f"{cache_key}.json").exists() and model is None:
                ensure_model()
            analysis, state, cache_key, analysis_seconds = source_analysis(item, identity, cache_dir, model, cache_counter)
            policy = Pilot.decide_factor(reference, analysis, pilot_ratio, args.comfort_band)
            row = {"pack": item["Pack"], "relative_path": item["RelativePath"], "key": item["Key"],
                   "source_sha256": item["_source_sha256"], "analysis_state": state,
                   "analysis_cache_key": cache_key, "analysis_seconds": analysis_seconds,
                   "analysis": analysis_summary(analysis), **policy}
            decisions.append(row)
            analyzed_count += 1
            cached_count += int(state.startswith("cached"))
            if index % 32 == 0 or index == len(ordered):
                check_resources(f"ASR checkpoint {analyzed_count}/{len(ordered)}")
                run_record.update({"status": "analysis_running", "analysis_count": analyzed_count,
                                   "cache_hit_count": cached_count,
                                   "new_analysis_count": cache_counter["new_analysis_count"]})
                update_run_manifest(status_path, run_record)
                print(f"Analyzed {analyzed_count}/{len(ordered)} selected originals ({cached_count} cache hits).", flush=True)
            del analysis
        analysis_wall = time.monotonic() - analysis_started
        ref_policy = Pilot.decide_factor(reference, reference, pilot_ratio, args.comfort_band)
        decisions.insert(0, {"pack": reference_item["Pack"], "relative_path": reference_item["RelativePath"],
                             "key": reference_item["Key"], "source_sha256": reference_item["_source_sha256"],
                             "analysis_state": reference_state, "analysis_cache_key": reference_cache_key,
                             "analysis_seconds": reference_seconds,
                             "analysis": analysis_summary(reference), **ref_policy})
        del reference
        proposal_count = sum(row["factor"] is not None for row in decisions)
        review_count = len(decisions) - proposal_count
        write_json_new(analysis_path, {"schema": RUN_SCHEMA, "status": "analysis_complete",
                                       "scope": args.scope, "source_corpus_count": len(all_records),
                                       "analyzed_count": analyzed_count, "cache_hit_count": cached_count,
                                       "new_analysis_count": cache_counter["new_analysis_count"],
                                       "asr_inference_and_decode_seconds": cache_counter["asr_seconds"],
                                       "reference": calibration, "comfort_band": args.comfort_band,
                                       "decision_policy_version": POLICY_VERSION,
                                       "proposed_count": proposal_count, "review_required_count": review_count,
                                       "decisions": decisions})
        run_record.update({"status": "candidate_build_running", "analysis_count": analyzed_count,
                           "cache_hit_count": cached_count, "new_analysis_count": cache_counter["new_analysis_count"],
                           "proposed_count": proposal_count, "review_required_count": review_count})
        update_run_manifest(status_path, run_record)

        item_by_key = {audio_key(row): row for row in selected}
        candidate_results = {}
        completed = 0
        build_started = time.monotonic()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="adaptive-candidate")
        decisions_iter = iter(decisions)
        futures = {}

        def submit_next() -> bool:
            try:
                decision = next(decisions_iter)
            except StopIteration:
                return False
            key = (decision["pack"], decision["relative_path"])
            item = item_by_key[key]
            future = pool.submit(build_one, item, decision, output, ffmpeg)
            futures[future] = audio_key(item)
            return True

        try:
            while len(futures) < min(len(decisions), args.workers * 2) and submit_next():
                pass
            while futures:
                done, _ = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    key = futures.pop(future)
                    candidate_results[key] = future.result()
                    completed += 1
                    submit_next()
                    if completed % 32 == 0 or completed == len(decisions):
                        check_resources(f"candidate checkpoint {completed}/{len(decisions)}")
                        run_record.update({"status": "candidate_build_running", "built_count": completed,
                                           "decoded_count": completed})
                        update_run_manifest(status_path, run_record)
                        print(f"Built and fully decoded {completed}/{len(decisions)} selected candidates.", flush=True)
        except Exception:
            for future in futures:
                future.cancel()
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)
        build_wall = time.monotonic() - build_started
        if len(candidate_results) != len(decisions):
            raise ValueError("candidate output set is incomplete")

        table_count = 0
        if args.scope == "full":
            for pack in PACK_ORDER:
                pack_records = [row for row in selected if row["Pack"] == pack]
                durations = {}
                for item in pack_records:
                    result = candidate_results[audio_key(item)]
                    durations[item["Key"]] = float(result["decode"]["duration"])
                table = table_sources[pack]
                table_bytes = rewrite_timing_table(table["parsed"], durations, table["keys"], f"{pack} candidate table")
                target = output / "candidate_packs" / pack / "generated" / "sound_length_table.lua"
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(table_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
                reread = parse_timing_table(target.read_bytes(), f"{pack} candidate table output")
                if set(reread["values"]) != table["keys"]:
                    raise ValueError(f"candidate timing table key coverage changed: {pack}")
                for key, duration in durations.items():
                    if float(reread["values"][key]) != duration:
                        raise ValueError(f"candidate timing table/audio duration mismatch: {pack}/{key}")
                if mask_timing_text(reread["text"], table["keys"]) != mask_timing_text(table["parsed"]["text"], table["keys"]):
                    raise ValueError(f"candidate timing table changed unrelated content: {pack}")
                table_count += 1

        candidate_manifest_items = [candidate_results[audio_key(item)] for item in selected]
        candidate_manifest_items.sort(key=lambda row: (PACK_ORDER.index(row["pack"]), row["relative_path"]))
        built_count = sum(row["candidate_status"] == "built" for row in candidate_manifest_items)
        encoded_count = sum(row["build_method"] == "ffmpeg-atempo-libvorbis-q5" for row in candidate_manifest_items)
        exact_copy_count = len(candidate_manifest_items) - encoded_count
        placeholder_count = sum(row["candidate_status"] == "review_placeholder" for row in candidate_manifest_items)
        decoded_count = sum(row["decode_status"] == "passed" for row in candidate_manifest_items)
        status = final_candidate_status(args.scope, review_count)
        run_record.update({"status": "validating_candidate_only", "intended_final_status": status,
                           "candidate_count": len(candidate_manifest_items),
                           "built_count": built_count, "encoded_count": encoded_count,
                           "exact_copy_count": exact_copy_count,
                           "review_placeholder_count": placeholder_count, "decoded_count": decoded_count,
                           "timing_table_count": table_count, "analysis_wall_seconds": analysis_wall,
                           "model_load_seconds": model_load_seconds, "asr_seconds": cache_counter["asr_seconds"],
                           "candidate_build_wall_seconds": build_wall,
                           "throughput": throughput_report(decisions, candidate_manifest_items, analysis_wall, build_wall),
                           "completed_at_unix": time.time(), "elapsed_seconds": time.monotonic() - started,
                           "non_deployable": True, "deployable": False,
                           "candidate_items": candidate_manifest_items,
                           "analysis_report_sha256": sha256(analysis_path),
                           "table_outputs": ([{"pack": pack, "relative_path": f"candidate_packs/{pack}/generated/sound_length_table.lua",
                                               "sha256": sha256(output / "candidate_packs" / pack / "generated" / "sound_length_table.lua")}
                                              for pack in PACK_ORDER] if table_count else [])})
        update_run_manifest(status_path, run_record)
        validation_started = time.monotonic()
        validation = validate_candidate_stage(stage, output)
        validation["elapsed_seconds"] = time.monotonic() - validation_started
        run_record["validation"] = validation
        run_record["status"] = status
        run_record["completed_at_unix"] = time.time()
        run_record["elapsed_seconds"] = time.monotonic() - started
        update_run_manifest(status_path, run_record)
        print(f"Candidate-only stage complete: {len(candidate_manifest_items)} built, {placeholder_count} review placeholders, "
              f"{decoded_count} validated full decodes, {table_count} timing tables. Never deploy this stage. Output: {output}")
        return 0 if review_count == 0 else 2
    except ResourceBlocked as exc:
        if status_path is not None and run_record is not None and status_path.is_file():
            run_record["status"] = "resource_blocked"
            run_record["resource_blocker"] = str(exc)
            try:
                update_run_manifest(status_path, run_record)
            except Exception as write_exc:
                print(f"Could not persist resource blocker to run manifest: {write_exc}", file=sys.stderr)
        print(f"AdaptiveAudioBatch stopped for resource safety: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        if status_path is not None and run_record is not None and status_path.is_file():
            run_record["status"] = "failed"
            run_record["error"] = str(exc)
            run_record["failed_at_unix"] = time.time()
            try:
                update_run_manifest(status_path, run_record)
            except Exception as write_exc:
                print(f"Could not persist failed status to run manifest: {write_exc}", file=sys.stderr)
        print(f"AdaptiveAudioBatch failed closed: {exc}", file=sys.stderr)
        return 1
    finally:
        if lock_created and lock_path is not None:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
