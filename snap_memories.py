#!/usr/bin/env python3
"""
Turn the memories in a Snapchat "My Data" export into dated, geotagged media.

Snapchat exports each memory as a main file plus an optional overlay (text,
stickers, drawings), and keeps the date and location in memories_history.json.
The JSON has no file IDs, but every zip entry's UTC mtime is exactly the JSON
"Date", so that's what we match on. Matching on the date alone gets most files
wrong.

What it does:
  - burns overlays into photos and videos
  - writes capture time, UTC offset and GPS (videos too)
  - picks the timezone from GPS, or the nearest GPS fix within 30 days
  - joins multi-snap / Spectacles recordings back into a single video
  - drops duplicates (copies go to _review/duplicates so you can check)
  - reads straight out of the zips, nothing is extracted up front

Usage:
  snap_memories.py OUT_DIR mydata.zip [mydata-2.zip ...]
  snap_memories.py OUT_DIR mydata*.zip --chat-media    # chat media as well
"""
import argparse
import bisect
import csv
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import zipfile
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps
from timezonefinder import TimezoneFinder

MEMORY_RE = re.compile(r"^memories/(\d{4}-\d{2}-\d{2}_.+)-(main|overlay)\.(\w+)$")
VIDEO_EXTS = {"mp4", "mov"}
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

THUMB_SIZE = 24          # frames are compared as 24x24 grayscale thumbnails
SEGMENT_MAX_GAP = 12     # seconds between one segment's start and the next
SEGMENT_MIN_LEN = 9.3    # every segment except the last one is ~10s
SEGMENT_MAX_DIFF = 25    # last frame vs next first frame (unrelated clips are ~70)
DUPLICATE_MAX_DIFF = 8


# Reading the zips

def zip_mtime_utc(info):
    """UTC mtime from the extended timestamp extra field (0x5455).

    Falls back to the DOS timestamp, which has no timezone and is only
    accurate to 2 seconds.
    """
    extra = info.extra
    i = 0
    while i + 4 <= len(extra):
        header_id, length = struct.unpack("<HH", extra[i:i + 4])
        if header_id == 0x5455 and length >= 5 and extra[i + 4] & 1:
            ts = struct.unpack("<I", extra[i + 5:i + 9])[0]
            return datetime.fromtimestamp(ts, timezone.utc)
        i += 4 + length
    return datetime(*info.date_time, tzinfo=timezone.utc)


def member_ref(zip_path, info, ext):
    return {
        "zip": str(zip_path),
        "name": info.filename,
        "ext": ext,
        "crc": info.CRC,
        "size": info.file_size,
        "utc": zip_mtime_utc(info),
    }


def load_json(zips, filename):
    """Parse every distinct copy of <filename> found in the zips.

    Multi-part exports repeat the JSON in some parts, and an older export can
    be passed in alongside a newer one, so duplicates are skipped by hash.
    """
    docs = []
    seen = set()
    for zip_path in zips:
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                if name.rsplit("/", 1)[-1] != filename:
                    continue
                raw = zf.read(name)
                digest = hashlib.md5(raw).hexdigest()
                if digest not in seen:
                    seen.add(digest)
                    docs.append(json.loads(raw))
    return docs


def index_memories(zips):
    """-> {key: {"main": ref, "overlay": ref}} and the merged JSON entries."""
    items = {}
    for zip_path in zips:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                m = MEMORY_RE.match(info.filename)
                if not m:
                    continue
                key, role, ext = m.group(1), m.group(2), m.group(3).lower()
                if role == "overlay" and info.file_size == 0:
                    continue
                items.setdefault(key, {}).setdefault(role, member_ref(zip_path, info, ext))

    entries = []
    for doc in load_json(zips, "memories_history.json"):
        entries += doc.get("Saved Media", [])
    return items, entries


def read_member(ref):
    with zipfile.ZipFile(ref["zip"]) as zf:
        if "offset" in ref:
            # the name is used by more than one file in this zip
            return zf.read(next(i for i in zf.infolist() if i.header_offset == ref["offset"]))
        return zf.read(ref["name"])


def overlay_png(ref):
    """The overlay as PNG bytes, or None if it's empty or unreadable.

    Some memory overlays are PNGs wrapped in a Snapchat "SCOF" container, so
    skip ahead to the PNG header. Chat overlays are often WebP; those get
    converted so ffmpeg and Pillow only ever see PNG.
    """
    data = read_member(ref)
    start = data.find(PNG_MAGIC)
    if start >= 0:
        data = data[start:]
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return None
    if img.format == "PNG":
        return data
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def extract_member(ref, dest):
    if not dest.exists():
        # two workers can want the same file at once
        tmp = dest.with_suffix(f"{dest.suffix}.{os.getpid()}.part")
        tmp.write_bytes(read_member(ref))
        tmp.rename(dest)
    return dest


# Dates, locations and timezones

def parse_location(text):
    m = re.search(r"([-\d.]+),\s*([-\d.]+)\s*$", text or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if lat == 0 and lon == 0:
        return None
    return lat, lon


class Timeline:
    """Memory metadata indexed by UTC time, plus a GPS track for timezones."""

    def __init__(self, entries, fallback_tz=None):
        self.tf = TimezoneFinder()
        self.fallback_tz = fallback_tz or ZoneInfo("UTC")
        self.by_time = defaultdict(list)
        points = []
        for entry in entries:
            try:
                when = datetime.strptime(entry["Date"].replace(" UTC", ""), "%Y-%m-%d %H:%M:%S")
            except (KeyError, ValueError):
                continue
            when = when.replace(tzinfo=timezone.utc)
            loc = parse_location(entry.get("Location"))
            self.by_time[when].append((entry.get("Media Type"), loc))
            if loc:
                points.append((when.timestamp(), loc))
        points.sort()
        self.gps_times = [t for t, _ in points]
        self.gps_locs = [loc for _, loc in points]
        self._tz_cache = {}

    def lookup(self, utc, is_video):
        """-> (gps or None, how it matched: "exact", "+1s", ... or "none")"""
        want = "Video" if is_video else "Image"
        for delta in (0, -1, 1, -2, 2):
            found = self.by_time.get(utc + timedelta(seconds=delta))
            if not found:
                continue
            same_type = [loc for kind, loc in found if kind == want]
            candidates = same_type or [loc for _, loc in found]
            gps = next((loc for loc in candidates if loc), None)
            return gps, "exact" if delta == 0 else f"{delta:+d}s"
        return None, "none"

    def timezone_for(self, utc, gps):
        """-> (ZoneInfo, where it came from)"""
        if gps:
            return self._zone(gps), "gps"

        ts = utc.timestamp()
        i = bisect.bisect_left(self.gps_times, ts)
        nearby = [j for j in (i - 1, i) if 0 <= j < len(self.gps_times)]
        if nearby:
            j = min(nearby, key=lambda j: abs(self.gps_times[j] - ts))
            if abs(self.gps_times[j] - ts) < 30 * 86400:
                return self._zone(self.gps_locs[j]), "nearest-gps"

        # retag_tz.py looks for "utc-fallback", so keep that label for UTC
        if str(self.fallback_tz) == "UTC":
            return self.fallback_tz, "utc-fallback"
        return self.fallback_tz, "default-tz"

    def _zone(self, loc):
        key = (round(loc[0], 2), round(loc[1], 2))
        if key not in self._tz_cache:
            name = self.tf.timezone_at(lat=loc[0], lng=loc[1])
            self._tz_cache[key] = ZoneInfo(name or "UTC")
        return self._tz_cache[key]


# Probing media

def run(cmd):
    return subprocess.run(cmd, capture_output=True)


def gray_thumb(ffmpeg_input_args):
    """One frame as a THUMB_SIZE x THUMB_SIZE grayscale byte string."""
    out = run(["ffmpeg", "-v", "error"] + ffmpeg_input_args + [
        "-vf", f"scale={THUMB_SIZE}:{THUMB_SIZE},format=gray",
        "-f", "rawvideo", "-",
    ]).stdout
    n = THUMB_SIZE * THUMB_SIZE
    return out[-n:] if len(out) >= n else None


def probe_video(path):
    """Size (rotation applied), duration, audio, and first/middle/last frame thumbs.

    Returns None if the file can't be read. Audio-only files (voice notes)
    come back with a size of 0x0 and no thumbs.
    """
    r = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,width,height"
                         ":stream_side_data=rotation:stream_tags=rotate",
        "-of", "json", str(path),
    ])
    try:
        info = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    streams = info.get("streams", [])
    duration = float(info.get("format", {}).get("duration", 0) or 0)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    video = [s for s in streams if s.get("codec_type") == "video"]
    if not video:
        if has_audio:
            return dict(w=0, h=0, dur=duration, audio=True, first=None, mid=None, last=None)
        return None

    v = video[0]
    rotation = int(v.get("tags", {}).get("rotate", 0) or 0)
    for side_data in v.get("side_data_list", []):
        if side_data.get("rotation") is not None:
            rotation = int(float(side_data["rotation"]))
    w, h = v["width"], v["height"]
    if abs(rotation) % 180 == 90:
        w, h = h, w

    return dict(
        w=w, h=h, dur=duration, audio=has_audio,
        first=gray_thumb(["-i", str(path), "-frames:v", "1"]),
        mid=gray_thumb(["-ss", f"{duration / 2:.2f}", "-i", str(path), "-frames:v", "1"]),
        last=gray_thumb(["-sseof", "-0.3", "-i", str(path), "-update", "1"]),
    )


def photo_thumb(data):
    """-> (grayscale thumb bytes, (w, h)) or (None, None)"""
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        return img.convert("L").resize((THUMB_SIZE, THUMB_SIZE)).tobytes(), img.size
    except Exception:
        return None, None


def thumb_diff(a, b):
    """Mean absolute difference between two thumbnails (0-255)."""
    if not a or not b:
        return 999.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def real_image_ext(data):
    """Snapchat sometimes names a PNG or HEIC file .jpg"""
    if data[:8] == PNG_MAGIC:
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[4:12] in (b"ftypheic", b"ftypmif1", b"ftypheix"):
        return "heic"
    return None


# Writing output

def exif_args(kind, ext, utc, tz, gps, description=None):
    local = utc.astimezone(tz)
    offset = local.strftime("%z")
    offset = offset[:3] + ":" + offset[3:]
    stamp = local.strftime("%Y:%m:%d %H:%M:%S")

    if kind in ("video", "audio"):
        args = ["-api", "QuickTimeUTC=1"]
        for tag in ("CreateDate", "ModifyDate", "TrackCreateDate", "TrackModifyDate",
                    "MediaCreateDate", "MediaModifyDate"):
            args.append(f"-QuickTime:{tag}={stamp}{offset}")
        args += [f"-Keys:CreationDate={stamp}{offset}",
                 f"-UserData:DateTimeOriginal={stamp}{offset}"]
        if gps:
            args += [f"-Keys:GPSCoordinates={gps[0]}, {gps[1]}, 0",
                     f"-UserData:GPSCoordinates={gps[0]}, {gps[1]}, 0"]
        if description:
            args.append(f"-Keys:Description={description}")
        return args

    if ext in ("png", "gif"):  # no EXIF block, use XMP
        args = [f"-XMP-exif:DateTimeOriginal={stamp}{offset}",
                f"-XMP-xmp:CreateDate={stamp}{offset}"]
    else:
        args = [f"-EXIF:DateTimeOriginal={stamp}", f"-EXIF:CreateDate={stamp}",
                f"-EXIF:ModifyDate={stamp}", f"-EXIF:OffsetTimeOriginal={offset}",
                f"-EXIF:OffsetTime={offset}", f"-EXIF:OffsetTimeDigitized={offset}"]
    if gps:
        args += [f"-GPSLatitude={abs(gps[0])}",
                 f"-GPSLatitudeRef={'N' if gps[0] >= 0 else 'S'}",
                 f"-GPSLongitude={abs(gps[1])}",
                 f"-GPSLongitudeRef={'E' if gps[1] >= 0 else 'W'}"]
    if description:
        args.append(f"-XMP-dc:Description={description}")
    return args


def finalize(path, kind, ext, utc, tz, gps, description=None):
    """Write metadata with exiftool and set the file mtime to the capture time."""
    cmd = (["exiftool", "-overwrite_original", "-q", "-m"]
           + exif_args(kind, ext, utc, tz, gps, description) + [str(path)])
    r = run(cmd)
    if r.returncode != 0 and b"movie fragments" in r.stderr:
        # exiftool can't write to fragmented MP4s; remuxing (no re-encode) fixes it
        tmp = Path(path).with_suffix(f".remux{Path(path).suffix}")
        remux = run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-map", "0", "-c", "copy",
                     "-movflags", "+faststart", str(tmp)])
        if remux.returncode == 0:
            tmp.replace(path)
            r = run(cmd)
        else:
            tmp.unlink(missing_ok=True)
    os.utime(path, (utc.timestamp(), utc.timestamp()))
    return r.returncode == 0, r.stderr.decode(errors="replace").strip()[-300:]


def overlay_filter(video_input, overlay_input, w, h, label):
    """ffmpeg filter that scales/pads one video to w x h, burns in the overlay
    if there is one, and outputs [label]."""
    chain = (f"[{video_input}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
             f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    if overlay_input is None:
        return chain + f",format=yuv420p[{label}]"
    return (chain + f"[base_{label}];"
            f"[{overlay_input}:v]scale={w}:{h}:flags=lanczos[ov_{label}];"
            f"[base_{label}][ov_{label}]overlay=0:0:format=auto,format=yuv420p[{label}]")


# Worker processes

def probe_worker(key, ref, work_dir):
    safe_key = re.sub(r"[^\w.~-]", "_", key)
    path = extract_member(ref, Path(work_dir) / f"{safe_key}-main.{ref['ext']}")
    return key, probe_video(path)


def process_photo(job, out):
    part = job["parts"][0]
    data = read_member(part["main"])
    overlay = part["overlay"] and overlay_png(part["overlay"])
    note = ""
    if overlay:
        base = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGBA")
        top = Image.open(io.BytesIO(overlay)).convert("RGBA")
        if top.size != base.size:
            top = top.resize(base.size, Image.LANCZOS)
        Image.alpha_composite(base, top).convert("RGB").save(out, "JPEG", quality=95)
        note = "composited"
    else:
        if part["overlay"]:
            note = "overlay unreadable, main only;"
        out.write_bytes(data)
    return note


def process_video(job, out, work_dir, crf):
    """Copy, composite, or join the video parts into `out`. Returns (note, error)."""
    note = ""
    parts = []
    for part in job["parts"]:
        main_path = extract_member(part["main"], work_dir / Path(part["main"]["name"]).name)
        overlay_path = None
        if part["overlay"]:
            overlay_path = work_dir / (Path(part["overlay"]["name"]).stem + ".png")
            if not overlay_path.exists():
                png = overlay_png(part["overlay"])
                if png:
                    # another worker may be about to read it, so write it in one go
                    tmp = overlay_path.with_suffix(f".{os.getpid()}.part")
                    tmp.write_bytes(png)
                    tmp.rename(overlay_path)
                else:
                    overlay_path = None
                    note = "overlay unreadable, main only;"
        parts.append((main_path, overlay_path, part["probe"]))

    # nothing to burn in or join: keep the original stream untouched
    if len(parts) == 1 and parts[0][1] is None:
        shutil.copyfile(parts[0][0], out)
        return note, None

    w, h = parts[0][2]["w"], parts[0][2]["h"]
    w, h = w - w % 2, h - h % 2
    inputs, filters, concat_inputs = [], [], []
    for n, (main_path, overlay_path, probe) in enumerate(parts):
        video_input = inputs.count("-i")
        inputs += ["-i", str(main_path)]
        overlay_input = None
        if overlay_path:
            inputs += ["-i", str(overlay_path)]
            overlay_input = video_input + 1
        filters.append(overlay_filter(video_input, overlay_input, w, h, f"v{n}"))
        if len(parts) > 1:
            # concat needs every segment to have audio, so pad silent ones
            if probe["audio"]:
                filters.append(f"[{video_input}:a]aresample=48000,"
                               f"aformat=channel_layouts=stereo[a{n}]")
            else:
                filters.append(f"anullsrc=r=48000:cl=stereo,"
                               f"atrim=duration={probe['dur']:.3f}[a{n}]")
            concat_inputs.append(f"[v{n}][a{n}]")

    if len(parts) > 1:
        filters.append(f"{''.join(concat_inputs)}concat=n={len(parts)}:v=1:a=1[vout][aout]")
        maps = ["-map", "[vout]", "-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
        note += f"joined {len(parts)} segments"
        if any(overlay_path for _, overlay_path, _ in parts):
            note += " +overlays"
    else:
        filters[-1] = filters[-1].replace("[v0]", "[vout]")
        maps = ["-map", "[vout]", "-map", "0:a?", "-c:a", "copy"]
        note += "composited"

    cmd = (["ffmpeg", "-y", "-v", "error"] + inputs
           + ["-filter_complex", ";".join(filters)] + maps
           + ["-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
              "-movflags", "+faststart", str(out)])
    r = run(cmd)
    if r.returncode != 0:
        return note, "ffmpeg: " + r.stderr.decode(errors="replace")[-400:]
    return note, None


def process_worker(job, work_dir, out_dir, crf):
    """Build one output file.

    job = dict(kind="photo"|"video"|"audio", out_name, utc, tz, gps,
               description, parts=[dict(main, overlay, probe), ...])
    Returns (out_name, ok, note).
    """
    work_dir, out_dir = Path(work_dir), Path(out_dir)
    out = out_dir / job["out_name"]
    out.parent.mkdir(parents=True, exist_ok=True)
    tz = ZoneInfo(job["tz"])
    try:
        if job["kind"] == "photo":
            note = process_photo(job, out)
        elif job["kind"] == "audio":
            note = ""
            out.write_bytes(read_member(job["parts"][0]["main"]))
        else:
            note, error = process_video(job, out, work_dir, crf)
            if error:
                return job["out_name"], False, error
        ok, err = finalize(out, job["kind"], out.suffix[1:], job["utc"], tz,
                           job["gps"], job.get("description"))
        return job["out_name"], ok, f"{note} {err}".strip()
    except Exception as e:
        return job["out_name"], False, f"{type(e).__name__}: {e}"


def probe_all(refs, work_dir, workers):
    """Probe {key: ref} videos in parallel -> {key: probe or None}"""
    probes = {}
    with ProcessPoolExecutor(workers) as pool:
        futures = [pool.submit(probe_worker, key, ref, str(work_dir)) for key, ref in refs.items()]
        for future in as_completed(futures):
            key, probe = future.result()
            probes[key] = probe
    return probes


def run_jobs(jobs, work_dir, out_dir, crf, workers, on_done):
    """Run process_worker over the jobs, calling on_done(name, ok, note) for each."""
    start = time.time()
    done = 0
    with ProcessPoolExecutor(workers) as pool:
        futures = [pool.submit(process_worker, job, str(work_dir), str(out_dir), crf)
                   for job in jobs]
        for future in as_completed(futures):
            name, ok, note = future.result()
            on_done(name, ok, note)
            done += 1
            if not ok:
                print(f"  FAIL {name}: {note}")
            if done % 100 == 0 or done == len(jobs):
                print(f"  {done}/{len(jobs)}  elapsed {(time.time() - start) / 60:.1f}m")


class NameAllocator:
    """Hands out YYYY-MM-DD_HHMMSS.ext names (local time), adding _2, _3, ...
    when two files land on the same second. Names in taken are never handed out."""

    def __init__(self, taken=()):
        self.used = set(taken)

    def __call__(self, utc, tz, ext, folder=""):
        base = utc.astimezone(tz).strftime("%Y-%m-%d_%H%M%S")
        if folder:
            base = f"{folder}/{base}"
        name = f"{base}.{ext}"
        n = 2
        while name in self.used:
            name = f"{base}_{n}.{ext}"
            n += 1
        self.used.add(name)
        return name


def write_report(path, rows, columns):
    rows = sorted(rows, key=lambda r: (r.get("output") or "~", r.get("sources", "")))
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})


# Memories

def overlays_match(a, b):
    """True if both overlays are missing, or both look the same."""
    if a is None or b is None:
        return a is None and b is None
    imgs = []
    for ref in (a, b):
        png = overlay_png(ref)
        if not png:
            return False
        imgs.append(Image.open(io.BytesIO(png)).convert("RGBA").resize((48, 48)).tobytes())
    return thumb_diff(*imgs) < 1.0


def find_duplicates(items, probes, is_video):
    """-> (dropped {key: (kept_key, "exact"|"near")}, variant pairs)

    Near duplicates are the same base media saved twice (re-encoded, so the
    bytes differ). If the overlays differ it's a deliberate variant, e.g. the
    same photo with a different caption, and both are kept.
    """
    dropped = {}

    # exact: same main bytes and same overlay bytes, keep the earliest
    groups = defaultdict(list)
    for key, item in items.items():
        overlay = item.get("overlay")
        sig = (item["main"]["crc"], item["main"]["size"],
               overlay and (overlay["crc"], overlay["size"]))
        groups[sig].append(key)
    for keys in groups.values():
        keys.sort(key=lambda k: items[k]["main"]["utc"])
        for key in keys[1:]:
            dropped[key] = (keys[0], "exact")

    photo_thumbs = {}

    def thumbs(key):
        if is_video(key):
            p = probes.get(key)
            return p and (p["first"], p["mid"], p["last"])
        if key not in photo_thumbs:
            photo_thumbs[key] = photo_thumb(read_member(items[key]["main"]))[0]
        return (photo_thumbs[key],) if photo_thumbs[key] else None

    by_second = defaultdict(list)
    for key, item in items.items():
        by_second[int(item["main"]["utc"].timestamp())].append(key)

    variants = []
    for sec in sorted(by_second):
        candidates = [k for d in (0, 1) for k in by_second.get(sec + d, []) if k not in dropped]
        for i, x in enumerate(candidates):
            for y in candidates[i + 1:]:
                if x in dropped or y in dropped or is_video(x) != is_video(y):
                    continue
                if is_video(x):
                    px, py = probes.get(x), probes.get(y)
                    if not px or not py or abs(px["dur"] - py["dur"]) > 0.06:
                        continue
                tx, ty = thumbs(x), thumbs(y)
                if not tx or not ty:
                    continue
                if max(thumb_diff(a, b) for a, b in zip(tx, ty)) > DUPLICATE_MAX_DIFF:
                    continue
                if not overlays_match(items[x].get("overlay"), items[y].get("overlay")):
                    variants.append((x, y))
                    continue
                if items[x]["main"]["size"] >= items[y]["main"]["size"]:
                    dropped[y] = (x, "near")
                else:
                    dropped[x] = (y, "near")
    return dropped, variants


def find_segment_chains(videos, items, probes):
    """Link videos where B starts right after A ends and B's first frame
    matches A's last frame. Returns a list of chains (lists of keys)."""
    ordered = sorted(videos, key=lambda k: items[k]["main"]["utc"])
    edges = []
    for i, x in enumerate(ordered):
        px, tx = probes[x], items[x]["main"]["utc"]
        if px["dur"] < SEGMENT_MIN_LEN:
            continue
        for y in ordered[i + 1:] + ordered[max(0, i - 10):i]:
            gap = (items[y]["main"]["utc"] - tx).total_seconds()
            if y == x or not 0 <= gap <= SEGMENT_MAX_GAP:
                continue
            py = probes[y]
            if (px["w"], px["h"]) != (py["w"], py["h"]):
                continue
            diff = thumb_diff(px["last"], py["first"])
            if diff <= SEGMENT_MAX_DIFF:
                edges.append((diff, x, y))

    # best matches first; each video gets at most one next and one previous
    # segment, and no loops
    nxt, prev, parent = {}, {}, {}

    def root(k):
        while parent.get(k, k) != k:
            k = parent[k]
        return k

    for _, x, y in sorted(edges):
        if x in nxt or y in prev or root(x) == root(y):
            continue
        nxt[x], prev[y] = y, x
        parent[root(y)] = root(x)

    chains = []
    for key in ordered:
        if key in nxt and key not in prev:
            chain = [key]
            while chain[-1] in nxt:
                chain.append(nxt[chain[-1]])
            chains.append(chain)
    return chains


def process_memories(out, zips, workers=4, crf=18, fallback_tz=None):
    start = time.time()
    out = Path(out)
    work = out / "_work"
    review = out / "_review"
    for d in (out, work, review / "duplicates", review / "orphan_overlays"):
        d.mkdir(parents=True, exist_ok=True)

    print(f"Indexing {len(zips)} zip(s)...")
    items, entries = index_memories(zips)
    timeline = Timeline(entries, fallback_tz)
    print(f"  {len(items)} memories, {len(entries)} JSON entries")
    report = []

    # overlays whose main file isn't in any of the zips
    for key, item in list(items.items()):
        if "main" not in item:
            ref = item["overlay"]
            (review / "orphan_overlays" / Path(ref["name"]).name).write_bytes(read_member(ref))
            report.append(dict(action="orphan-overlay", sources=key))
            del items[key]

    def is_video(key):
        return items[key]["main"]["ext"] in VIDEO_EXTS

    videos = [k for k in items if is_video(k)]
    print(f"Probing {len(videos)} videos...")
    probes = probe_all({k: items[k]["main"] for k in videos}, work, workers)

    dropped, variants = find_duplicates(items, probes, is_video)
    for key, (kept, how) in dropped.items():
        for role in ("main", "overlay"):
            ref = items[key].get(role)
            if ref:
                (review / "duplicates" / Path(ref["name"]).name).write_bytes(read_member(ref))
        report.append(dict(action=f"dropped-duplicate({how})", sources=key, note=f"kept {kept}"))
    live = {k: v for k, v in items.items() if k not in dropped}
    variant_of = {}
    for x, y in variants:
        variant_of[x], variant_of[y] = y, x

    readable_videos = [k for k in videos if k in live and probes.get(k)]
    chains = find_segment_chains(readable_videos, live, probes)
    in_chain = {k for chain in chains for k in chain}

    name_for = NameAllocator()
    jobs = []
    report_info = {}

    def add_job(keys, kind):
        first = live[keys[0]]["main"]
        utc = first["utc"]
        gps, matched = timeline.lookup(utc, kind == "video")
        tz, tz_source = timeline.timezone_for(utc, gps)
        if kind == "video":
            ext = "mp4"
        elif live[keys[0]].get("overlay"):
            ext = "jpg"
        else:
            ext = real_image_ext(read_member(first)) or first["ext"]
        name = name_for(utc, tz, ext)
        jobs.append(dict(
            kind=kind, out_name=name, utc=utc, tz=str(tz), gps=gps,
            parts=[dict(main=live[k]["main"], overlay=live[k].get("overlay"),
                        probe=probes.get(k)) for k in keys],
        ))
        report_info[name] = dict(
            sources=" + ".join(keys),
            utc=utc.strftime("%Y-%m-%d %H:%M:%S"),
            local=utc.astimezone(tz).isoformat(),
            tz=f"{tz} ({tz_source})",
            json=matched,
            gps=gps and f"{gps[0]},{gps[1]}",
        )

    for chain in chains:
        add_job(chain, "video")
    for key in live:
        if key in in_chain:
            continue
        if is_video(key) and not probes.get(key):
            report.append(dict(action="unreadable-video", sources=key))
            continue
        add_job([key], "video" if is_video(key) else "photo")

    print(f"Processing {len(jobs)} outputs ({len(chains)} joined videos from "
          f"{len(in_chain)} segments, {len(dropped)} duplicates dropped)...")

    def on_done(name, ok, note):
        info = report_info[name]
        variant = next((variant_of[k] for k in info["sources"].split(" + ") if k in variant_of), None)
        if variant:
            note = f"{note} variant-of {variant}".strip()
        report.append(dict(output=name, action="ok" if ok else "FAILED", note=note, **info))

    run_jobs(jobs, work, out, crf, workers, on_done)
    shutil.rmtree(work, ignore_errors=True)
    write_report(out / "_report.csv", report,
                 ["output", "action", "sources", "utc", "local", "tz", "json", "gps", "note"])

    def count(pred):
        return sum(1 for r in report if pred(r))

    def is_composited(r, ext):
        return r.get("output", "").endswith(ext) and r.get("note", "").startswith("composited")

    print(f"\nDone in {(time.time() - start) / 60:.1f}m -> {out}")
    print(f"  outputs: {len(jobs)} (failed {count(lambda r: r['action'] == 'FAILED')})")
    print(f"  photos composited: {count(lambda r: is_composited(r, '.jpg'))}")
    print(f"  videos composited: {count(lambda r: is_composited(r, '.mp4'))}")
    print(f"  joined videos: {len(chains)} (from {len(in_chain)} segments)")
    print(f"  duplicates dropped: {len(dropped)}  -> _review/duplicates")
    print(f"  same-moment variants kept (different/missing overlay): {len(variants)} pairs")
    print(f"  orphan overlays: {count(lambda r: r['action'] == 'orphan-overlay')}"
          f"  -> _review/orphan_overlays")
    matches = ("exact", "-1s", "+1s", "-2s", "+2s", "none")
    print("  JSON time match: " + ", ".join(
        f"{m}={count(lambda r, m=m: r.get('json') == m)}" for m in matches))
    sources = ("(gps)", "nearest-gps", "utc-fallback", "default-tz")
    print("  tz source: " + ", ".join(
        f"{s}={count(lambda r, s=s: s in r.get('tz', ''))}" for s in sources))
    print(f"  report: {out / '_report.csv'}")


def find_zips(paths):
    """Zip files from the command line. A folder means every .zip in it."""
    zips = []
    for path in map(Path, paths):
        if path.is_dir():
            zips += sorted(path.glob("*.zip"))
        else:
            zips.append(path)
    if not zips:
        raise SystemExit("no zip files found")
    return zips


def main():
    parser = argparse.ArgumentParser(
        description="Turn Snapchat memories into dated, geotagged photos and videos.")
    parser.add_argument("out", help="output folder")
    parser.add_argument("zips", nargs="+",
                        help="the mydata~*.zip files from the export, or a folder of them")
    parser.add_argument("--chat-media", action="store_true",
                        help="also process chat media into OUT/chat_media (snap_chat_media.py)")
    parser.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) - 2))
    parser.add_argument("--crf", type=int, default=18,
                        help="x264 quality for re-encoded videos, lower is better (default 18)")
    parser.add_argument("--tz", type=ZoneInfo, default=None,
                        help="timezone to use when there's no GPS fix within 30 days, "
                             "e.g. America/Winnipeg (default UTC)")
    parser.add_argument("--no-sort", dest="sort", action="store_false",
                        help="with --chat-media, don't move memes and stickers into subfolders")
    parser.add_argument("--skip-resends", action="store_true",
                        help="with --chat-media, keep only the first send of a snap that was "
                             "sent more than once in a conversation")
    args = parser.parse_args()
    zips = find_zips(args.zips)

    process_memories(args.out, zips, args.workers, args.crf, args.tz)

    if args.chat_media:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import snap_chat_media
        print()
        snap_chat_media.process_chat_media(Path(args.out) / "chat_media", zips,
                                           args.workers, args.crf, args.tz, args.sort,
                                           args.skip_resends)


if __name__ == "__main__":
    main()
