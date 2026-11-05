#!/usr/bin/env python3
"""
Turn the chat media in a Snapchat "My Data" export into dated files, sorted
into one folder per conversation.

chat_media/ is messier than memories/. It has:
  - files named after a message's "Media IDs" in chat_history.json (b~... or a
    32-char hex id). One message often lists several files: the snap, a
    low-res copy of it, and its overlay as a separate image.
  - files named media~UUID / overlay~UUID / thumbnail~UUID that aren't in any
    of the JSON. These are usually a full-res copy of a snap above.
  - snaps that only show up in snap_history.json, which has no IDs at all.

What ties it together is the timestamp: a file's UTC mtime in the zip is the
same second as its message's "Created" time. So files are grouped by second,
copies of the same snap are collapsed (highest resolution wins), overlays are
burned in, and the message gives the conversation and who sent it. Multi-snaps
arrive as one message per 10s segment and get joined back together.

Chat media has no location. The timezone comes from the nearest GPS fix in
your memories (within 30 days), otherwise --tz, otherwise UTC.

Photos that weren't taken with the camera go in subfolders of the
conversation:
  stickers/  GIFs (the GIPHY ones from the sticker picker), Bitmoji/memoji
             and cutouts with a transparent background
  memes/     screenshots (PNGs) and images that are mostly text, going by
             tesseract if it's installed
Videos aren't sorted, there's nothing reliable to tell a meme video apart.

The same snap sent again later (forwarded, re-sent to a group) is kept once
per send. With --skip-resends only the first send in each folder is kept.

Usage:
  snap_chat_media.py OUT_DIR mydata.zip [mydata-2.zip ...]
"""
import argparse
import bisect
import io
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageChops, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parent))
from snap_memories import (  # noqa: E402
    DUPLICATE_MAX_DIFF, VIDEO_EXTS, NameAllocator, Timeline, find_segment_chains, find_zips,
    load_json, member_ref, overlays_match, probe_all, read_member, real_image_ext,
    run_jobs, thumb_diff, write_report,
)

# chat_media/2026-10-19_<id>.<ext>, where <id> may start with
# media~/overlay~/thumbnail~/metadata~
CHAT_MEDIA_RE = re.compile(
    r"^chat_media/\d{4}-\d{2}-\d{2}_((?:(media|overlay|thumbnail|metadata)~)?.+)\.(\w+)$")
OVERLAY_EXTS = {"png", "webp"}

# two videos in the same second are copies of one snap if their lengths are
# within this much (copies differ by ~0.04s) and their first frames look alike
SAME_SNAP_MAX_LEN_DIFF = 0.15
SAME_SNAP_MAX_FRAME_DIFF = 20

# memes vs photos. Snap captions are a handful of words, memes and tweet
# screenshots are more. Phone-shaped images need more words, since camera
# snaps can have long captions baked in.
PHONE_ASPECT = 1.7       # height / width of a full-screen snap
MEME_WORDS = 10
PHONE_MEME_WORDS = 30
OCR_MAX_SIDE = 1600
STICKER_MIN_CLEAR = 0.05  # share of transparent pixels that makes a cutout
CAPTION_MAX_SOLID = 0.12  # a lone caption overlay is a thin strip, a sticker is more
TESSERACT = shutil.which("tesseract")

# re-sent snaps. Photos are compared bigger than usual since a lot of chat
# photos are a line of text on black, and those all look alike when small.
RESEND_THUMB = 128
RESEND_PIXEL_DIFF = 48     # a pixel this far off counts as changed
RESEND_MAX_CHANGED = 0.05  # share of the picture's content allowed to change


def index_chat_media(zips):
    """A snap sent several times shows up as the same filename repeated in the
    zip with a different mtime for each send, so each one is kept.

    Now and then, different files share a name (e.g. a bare "media~.mp4").
    Those get the CRC added to their name so they stay apart, and are read
    by position, since reading by name only finds the last one.
    """
    files = {}
    crc_of_name = {}
    for zip_path in zips:
        with zipfile.ZipFile(zip_path) as zf:
            crcs_in_zip = defaultdict(set)
            for info in zf.infolist():
                crcs_in_zip[info.filename].add(info.CRC)
            for info in zf.infolist():
                m = CHAT_MEDIA_RE.match(info.filename)
                if not m or info.file_size == 0:
                    continue
                ref = member_ref(zip_path, info, m.group(3).lower())
                ref["id"] = m.group(1)
                ref["role"] = m.group(2) or ""
                if crc_of_name.setdefault(info.filename, info.CRC) != info.CRC:
                    stem, ext = info.filename.rsplit(".", 1)
                    ref["name"] = f"{stem}#{info.CRC:08x}.{ext}"
                if len(crcs_in_zip[info.filename]) > 1:
                    ref["offset"] = info.header_offset
                files.setdefault((ref["name"], ref["utc"]), ref)
    return list(files.values())


def short_name(ref):
    return ref["name"].split("/", 1)[1]


def message_info(conversation, msg):
    # "Created(microseconds)" is actually milliseconds
    try:
        second = int(msg["Created(microseconds)"]) // 1000
    except (KeyError, TypeError, ValueError):
        try:
            created = datetime.strptime(msg["Created"], "%Y-%m-%d %H:%M:%S UTC")
        except (KeyError, ValueError):
            return None
        second = int(created.replace(tzinfo=timezone.utc).timestamp())
    return dict(
        second=second,
        conversation=msg.get("Conversation Title") or conversation,
        sender=msg.get("From") or "",
        sent=bool(msg.get("IsSender")),
        type=msg.get("Media Type") or "",
    )


class Conversations:
    """Messages from chat_history.json and snap_history.json, indexed by
    media ID and by second."""

    def __init__(self, zips):
        self.by_media_id = {}
        self.by_second = defaultdict(list)
        self.snaps_by_second = defaultdict(list)

        for doc in load_json(zips, "chat_history.json"):
            for conversation, messages in doc.items():
                for msg in messages:
                    info = message_info(conversation, msg)
                    if not info:
                        continue
                    self.by_second[info["second"]].append(info)
                    for media_id in (msg.get("Media IDs") or "").split("|"):
                        if media_id.strip():
                            self.by_media_id[media_id.strip()] = info

        for doc in load_json(zips, "snap_history.json"):
            for conversation, snaps in doc.items():
                for snap in snaps:
                    info = message_info(conversation, snap)
                    if info:
                        self.snaps_by_second[info["second"]].append(info)

    def message_for(self, refs, second):
        """-> (message info or None, how it was found)"""
        for ref in refs:
            if ref["id"] in self.by_media_id:
                return self.by_media_id[ref["id"]], "media-id"

        same_second = sorted(self.by_second.get(second, []), key=lambda m: m["type"] != "MEDIA")
        if same_second:
            return same_second[0], "chat-time"

        # snap_history times are often a second or three before the file's
        for delta in (0, -1, 1, -2, 2, -3, 3):
            snaps = self.snaps_by_second.get(second + delta)
            if snaps:
                return snaps[0], "snap-time"
        return None, "none"


def plan_second(refs, probes, conversations):
    """Work out what to output from all the files saved in one second.

    Returns (outputs, skipped):
      outputs = [dict(kind, main, overlay, refs)]
      skipped = [(ref, action, note)]
    """
    outputs, skipped = [], []

    def probe(ref):
        return probes.get(ref["name"])

    has_video = any(ref["ext"] in VIDEO_EXTS and probe(ref) and probe(ref)["w"] for ref in refs)
    message_of = {ref["name"]: conversations.by_media_id.get(ref["id"]) for ref in refs}

    # byte-identical copies; prefer files a message points to
    refs = sorted(refs, key=lambda r: message_of[r["name"]] is None)
    seen = {}
    unique = []
    for ref in refs:
        sig = (ref["crc"], ref["size"])
        if sig in seen:
            skipped.append((ref, "duplicate", f"same as {short_name(seen[sig])}"))
        else:
            seen[sig] = ref
            unique.append(ref)

    videos, overlays, photos = [], [], []
    for ref in unique:
        if ref["role"] == "metadata":
            # a bit of JSON about a shared Discover story, not media
            skipped.append((ref, "metadata", ""))
        elif ref["role"] == "thumbnail" and has_video:
            skipped.append((ref, "thumbnail", ""))
        elif ref["role"] == "overlay" or (ref["ext"] in OVERLAY_EXTS and has_video):
            overlays.append(ref)
        elif ref["ext"] in VIDEO_EXTS:
            p = probe(ref)
            if p is None:
                skipped.append((ref, "unreadable", ""))
            elif p["w"] == 0:
                outputs.append(dict(kind="audio", main=ref, overlay=None, refs=[ref]))
            else:
                videos.append(ref)
        else:
            photos.append(ref)

    # collapse copies of the same snap, keeping the biggest
    snaps = []
    videos.sort(key=lambda r: probe(r)["w"] * probe(r)["h"], reverse=True)
    for ref in videos:
        p = probe(ref)
        for snap in snaps:
            best = probe(snap["main"])
            if (abs(best["dur"] - p["dur"]) <= SAME_SNAP_MAX_LEN_DIFF
                    and thumb_diff(best["first"], p["first"]) <= SAME_SNAP_MAX_FRAME_DIFF):
                snap["refs"].append(ref)
                skipped.append((ref, "lower-res copy", f"of {short_name(snap['main'])}"))
                break
        else:
            snaps.append(dict(kind="video", main=ref, overlay=None, refs=[ref]))

    # overlay~ files are often copies of the message's own overlay
    kept_overlays = []
    for ref in overlays:
        twin = next((o for o in kept_overlays if overlays_match(o, ref)), None)
        if twin:
            skipped.append((ref, "duplicate", f"same overlay as {short_name(twin)}"))
        else:
            kept_overlays.append(ref)

    # an overlay listed in the same message as one of a snap's copies is its overlay
    for snap in snaps:
        snap_messages = [message_of[r["name"]] for r in snap["refs"] if message_of[r["name"]]]
        mine = [o for o in kept_overlays
                if any(message_of[o["name"]] is m for m in snap_messages)]
        if len(mine) == 1:
            snap["overlay"] = mine[0]
            snap["refs"].append(mine[0])
            kept_overlays.remove(mine[0])

    # a single leftover overlay with a single thing to put it on
    targets = [s for s in snaps if s["overlay"] is None]
    if not snaps and len(photos) == 1:
        photo = dict(kind="photo", main=photos.pop(), overlay=None, refs=[])
        photo["refs"].append(photo["main"])
        outputs.append(photo)
        targets = [photo]
    if len(kept_overlays) == 1 and len(targets) == 1:
        targets[0]["overlay"] = kept_overlays[0]
        targets[0]["refs"].append(kept_overlays.pop())

    for ref in kept_overlays:
        skipped.append((ref, "unpaired overlay", "couldn't tell which snap it belongs to"))
    for ref in photos:
        outputs.append(dict(kind="photo", main=ref, overlay=None, refs=[ref]))
    return outputs + snaps, skipped


def ocr_word_count(img):
    if not TESSERACT:
        return 0
    if img.mode in ("RGBA", "LA", "P"):
        # grey, so both black and white text stay readable
        img = img.convert("RGBA")
        img = Image.alpha_composite(Image.new("RGBA", img.size, (128, 128, 128, 255)), img)
    img = img.convert("L")
    img.thumbnail((OCR_MAX_SIDE, OCR_MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    # tesseract's own threads fight the worker processes, ~15x slower without this
    env = dict(os.environ, OMP_THREAD_LIMIT="1")
    try:
        text = subprocess.run([TESSERACT, "stdin", "stdout"], input=buf.getvalue(),
                              capture_output=True, timeout=60, env=env).stdout.decode(errors="ignore")
    except subprocess.TimeoutExpired:
        return 0
    # tesseract finds "words" in photo noise, real ones are 3+ letters
    return len(re.findall(r"[A-Za-z]{3,}", text))


def clear_share(img):
    """Share of the image that's transparent"""
    if "A" not in img.getbands() and "transparency" not in img.info:
        return 0
    alpha = img.convert("RGBA").getchannel("A")
    return sum(alpha.histogram()[:128]) / (img.width * img.height)


def classify_worker(key, ref):
    """-> (key, "stickers" / "memes" / None) for a photo"""
    data = read_member(ref)
    ext = real_image_ext(data) or ref["ext"]
    if ext == "gif":
        return key, "stickers"
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        img.load()
    except Exception:
        return key, None  # HEIC, or broken
    clear = clear_share(img)
    if clear >= STICKER_MIN_CLEAR:
        # besides stickers, this catches screenshots pasted on a clear canvas
        # and caption overlays whose snap isn't in the export
        if ocr_word_count(img) >= MEME_WORDS:
            return key, "memes"
        if 1 - clear < CAPTION_MAX_SOLID:
            return key, None
        return key, "stickers"
    if ext == "png":
        return key, "memes"
    phone_shaped = img.height / max(img.width, 1) >= PHONE_ASPECT
    if ocr_word_count(img) >= (PHONE_MEME_WORDS if phone_shaped else MEME_WORDS):
        return key, "memes"
    return key, None


def classify_all(refs, workers):
    """Sort {key: ref} photos into stickers/memes -> {key: category or None}"""
    with ProcessPoolExecutor(workers) as pool:
        futures = [pool.submit(classify_worker, key, ref) for key, ref in refs.items()]
        return dict(f.result() for f in futures)


def thumb_worker(key, ref):
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(read_member(ref))))
        return key, img.convert("L").resize((RESEND_THUMB, RESEND_THUMB)).tobytes()
    except Exception:
        return key, None


def same_frame(a, b):
    side = int(len(a) ** 0.5)
    img_a, img_b = (Image.frombytes("L", (side, side), t) for t in (a, b))
    diff = ImageChops.difference(img_a, img_b)
    if sum(v * n for v, n in enumerate(diff.histogram())) / len(a) > DUPLICATE_MAX_DIFF:
        return False

    # changes are counted against what's actually in the picture, so a line
    # of text on black has to match on the text, not just on the black
    def mask(img, centre):
        return img.point(lambda v: 255 if abs(v - centre) > RESEND_PIXEL_DIFF else 0)

    content = ImageChops.lighter(mask(img_a, median(img_a)), mask(img_b, median(img_b)))
    changed = mask(diff, 0)
    n_content = content.histogram()[255]
    n_changed = ImageChops.multiply(content, changed).histogram()[255]
    return n_changed <= RESEND_MAX_CHANGED * max(n_content, 1)


def median(img):
    counts, half = img.histogram(), img.width * img.height / 2
    total = 0
    for value, n in enumerate(counts):
        total += n
        if total >= half:
            return value


def photo_thumbs(refs, workers):
    with ProcessPoolExecutor(workers) as pool:
        futures = [pool.submit(thumb_worker, key, ref) for key, ref in refs.items()]
        return dict(f.result() for f in futures)


def find_resends(sends):
    """-> {key: key of the first send} for media sent again later.

    sends is a list of (key, group, thumbs, length, overlays) in the order
    they were sent. Only sends in the same group are compared, and they need
    the same overlays and (for videos) the same length.
    """
    first_of = {}
    brightness = defaultdict(list)  # group -> sorted mean brightness of first sends
    kept = defaultdict(list)        # group -> (key, thumbs, length, overlays), same order
    for key, group, thumbs, length, overlays in sends:
        if not thumbs or not all(thumbs):
            continue
        # the mean difference of two thumbs is at least the difference of
        # their mean brightness, so only a narrow slice needs checking
        bright = sum(thumbs[0]) / len(thumbs[0])
        lo = bisect.bisect_left(brightness[group], bright - DUPLICATE_MAX_DIFF)
        hi = bisect.bisect_right(brightness[group], bright + DUPLICATE_MAX_DIFF)
        for k, t, n, o in kept[group][lo:hi]:
            if (abs(n - length) <= SAME_SNAP_MAX_LEN_DIFF
                    and all(same_frame(a, b) for a, b in zip(t, thumbs))
                    and all(overlays_match(a, b) for a, b in zip(o, overlays))):
                first_of[key] = k
                break
        else:
            at = bisect.bisect_right(brightness[group], bright)
            brightness[group].insert(at, bright)
            kept[group].insert(at, (key, thumbs, length, overlays))
    return first_of


def folder_name(conversation):
    return re.sub(r"[^\w.@ -]+", "_", conversation).strip(" .") or "_unknown"


def describe(msg):
    if not msg:
        return None
    if msg["sent"]:
        return f"Snapchat: sent to {msg['conversation']}"
    if msg["sender"] and msg["sender"] != msg["conversation"]:
        return f"Snapchat: from {msg['sender']} in {msg['conversation']}"
    return f"Snapchat: from {msg['sender'] or msg['conversation']}"


def output_ext(output):
    if output["kind"] == "video":
        return "mp4"
    if output["kind"] == "audio":
        return "m4a"
    if output["overlay"]:
        return "jpg"
    main = output["main"]
    return real_image_ext(read_member(main)) or main["ext"]


def process_chat_media(out, zips, workers=4, crf=18, fallback_tz=None, sort=True,
                       skip_resends=False):
    start = time.time()
    out = Path(out)
    work = out / "_work"
    review = out / "_review"
    for d in (out, work, review / "unpaired_overlays", review / "unreadable"):
        d.mkdir(parents=True, exist_ok=True)

    print(f"Indexing chat media in {len(zips)} zip(s)...")
    refs = index_chat_media(zips)
    if not refs:
        print("  no chat_media/ in these zips, nothing to do")
        return
    conversations = Conversations(zips)
    memories = [e for doc in load_json(zips, "memories_history.json")
                for e in doc.get("Saved Media", [])]
    timeline = Timeline(memories, fallback_tz)
    print(f"  {len(refs)} files, {len(conversations.by_media_id)} media IDs in chat history")

    videos = {r["name"]: r for r in refs if r["ext"] in VIDEO_EXTS}
    print(f"Probing {len(videos)} videos...")
    probes = probe_all(videos, work, workers)

    by_second = defaultdict(list)
    for ref in refs:
        by_second[int(ref["utc"].timestamp())].append(ref)

    report = []
    jobs = []
    report_info = {}
    name_for = NameAllocator()
    planned = []

    for second in sorted(by_second):
        outputs, skipped = plan_second(by_second[second], probes, conversations)
        planned += [(second, output) for output in outputs]

        for ref, action, note in skipped:
            if action == "unpaired overlay":
                (review / "unpaired_overlays" / Path(ref["name"]).name).write_bytes(read_member(ref))
            elif action == "unreadable":
                (review / "unreadable" / Path(ref["name"]).name).write_bytes(read_member(ref))
            report.append(dict(action=f"skipped-{action.replace(' ', '-')}",
                               sources=short_name(ref), note=note))

    categories = {}
    if sort:
        photos = {o["main"]["name"]: o["main"] for _, o in planned if o["kind"] == "photo"}
        print(f"Looking for memes and stickers in {len(photos)} photos"
              + ("..." if TESSERACT else " (no tesseract, so only PNGs and GIFs)..."))
        categories = classify_all(photos, workers)

    resolved = []
    for second, output in planned:
        msg, matched = conversations.message_for(output["refs"], second)
        folder = folder_name(msg["conversation"]) if msg else "_unknown"
        category = categories.get(output["main"]["name"]) if output["kind"] == "photo" else None
        resolved.append(dict(output=output, msg=msg, matched=matched, folder=folder,
                             category=category))

    # a multi-snap is sent as one message per 10s segment; join them back up,
    # only within a conversation
    items = {i: r["output"] for i, r in enumerate(resolved)}
    video_probes = {i: probes[o["main"]["name"]] for i, o in items.items() if o["kind"] == "video"}
    in_folder = defaultdict(list)
    for i in video_probes:
        in_folder[resolved[i]["folder"]].append(i)
    chains = [c for keys in in_folder.values()
              for c in find_segment_chains(keys, items, video_probes)]
    chain_of = {c[0]: c for c in chains}
    in_chain = {k for c in chains for k in c}
    heads = [i for i in range(len(resolved)) if i not in in_chain or i in chain_of]

    first_send = {}
    if skip_resends:
        photos = {i: items[i]["main"] for i in heads if items[i]["kind"] == "photo"}
        print(f"Looking for re-sent snaps ({len(photos)} photos, "
              f"{sum(items[i]['kind'] == 'video' for i in heads)} videos)...")
        thumbs = photo_thumbs(photos, workers)
        sends = []
        for i in heads:
            r, keys = resolved[i], chain_of.get(i, [i])
            group = (r["folder"], r["category"], items[i]["kind"], len(keys))
            overlays = [items[k]["overlay"] for k in keys]
            if items[i]["kind"] == "photo":
                sends.append((i, group, (thumbs[i],), 0, overlays))
            elif items[i]["kind"] == "video":
                first, last = video_probes[keys[0]], video_probes[keys[-1]]
                length = sum(video_probes[k]["dur"] for k in keys)
                sends.append((i, group, (first["first"], first["mid"], last["last"]),
                              length, overlays))
        first_send = find_resends(sends)

    out_name = {}
    for i in heads:
        r = resolved[i]
        keys = chain_of.get(i, [i])
        output, msg, folder, category = r["output"], r["msg"], r["folder"], r["category"]
        if i in first_send:
            report.append(dict(action="skipped-resend",
                               sources=" + ".join(short_name(ref) for k in keys
                                                  for ref in items[k]["refs"]),
                               note=f"same as {out_name[first_send[i]]}"))
            continue
        utc = output["main"]["utc"]
        tz, tz_source = timeline.timezone_for(utc, None)
        if category:
            folder = f"{folder}/{category}"
        name = name_for(utc, tz, output_ext(output), folder)
        out_name[i] = name
        jobs.append(dict(
            kind=output["kind"], out_name=name, utc=utc, tz=str(tz), gps=None,
            description=describe(msg),
            parts=[dict(main=items[k]["main"], overlay=items[k]["overlay"],
                        probe=probes.get(items[k]["main"]["name"])) for k in keys],
        ))
        report_info[name] = dict(
            sources=" + ".join(short_name(ref) for k in keys for ref in items[k]["refs"]),
            utc=utc.strftime("%Y-%m-%d %H:%M:%S"),
            local=utc.astimezone(tz).isoformat(),
            tz=f"{tz} ({tz_source})",
            conversation=msg["conversation"] if msg else "",
            sender=msg["sender"] if msg else "",
            direction=("sent" if msg["sent"] else "received") if msg else "",
            matched=r["matched"],
            category=category or "",
        )

    print(f"Processing {len(jobs)} outputs ({len(chains)} joined videos from "
          f"{len(in_chain)} segments)...")

    def on_done(name, ok, note):
        report.append(dict(output=name, action="ok" if ok else "FAILED", note=note,
                           **report_info[name]))

    run_jobs(jobs, work, out, crf, workers, on_done)
    shutil.rmtree(work, ignore_errors=True)
    write_report(out / "_report.csv", report,
                 ["output", "action", "sources", "utc", "local", "tz", "conversation",
                  "sender", "direction", "matched", "category", "note"])

    done = [r for r in report if r.get("output")]
    kinds = Counter(job["kind"] for job in jobs)
    skips = Counter(r["action"] for r in report if r["action"].startswith("skipped-"))
    matches = Counter(r["matched"] for r in done)
    folders = {r["output"].split("/", 1)[0] for r in done}
    sorted_into = Counter(r["category"] for r in done if r["category"])
    print(f"\nDone in {(time.time() - start) / 60:.1f}m -> {out}")
    print(f"  outputs: {len(done)} (failed {sum(r['action'] == 'FAILED' for r in done)}) in "
          f"{len(folders)} conversation folders")
    print(f"  photos {kinds['photo']}, videos {kinds['video']}, voice notes {kinds['audio']}, "
          f"overlays burned in {sum('composited' in r.get('note', '') for r in done)}")
    print(f"  multi-snaps joined: {len(chains)} (from {len(in_chain)} segments)")
    if sort:
        print(f"  stickers {sorted_into['stickers']}, memes {sorted_into['memes']}")
    print("  skipped: " + (", ".join(f"{k[8:]}={v}" for k, v in sorted(skips.items())) or "none"))
    print("  matched by: " + ", ".join(f"{k}={v}" for k, v in sorted(matches.items())))
    if skips["skipped-unpaired-overlay"]:
        print("  unpaired overlays -> _review/unpaired_overlays")
    print(f"  report: {out / '_report.csv'}")


def main():
    parser = argparse.ArgumentParser(
        description="Turn Snapchat chat media into dated files, one folder per conversation.")
    parser.add_argument("out", help="output folder")
    parser.add_argument("zips", nargs="+",
                        help="the mydata~*.zip files from the export, or a folder of them")
    parser.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) - 2))
    parser.add_argument("--crf", type=int, default=18,
                        help="x264 quality for re-encoded videos, lower is better (default 18)")
    parser.add_argument("--tz", type=ZoneInfo, default=None,
                        help="timezone to use when there's no GPS fix within 30 days, "
                             "e.g. America/Winnipeg (default UTC)")
    parser.add_argument("--no-sort", dest="sort", action="store_false",
                        help="don't move memes and stickers into subfolders")
    parser.add_argument("--skip-resends", action="store_true",
                        help="keep only the first send of a snap that was sent more than once "
                             "in a conversation")
    args = parser.parse_args()
    process_chat_media(args.out, find_zips(args.zips), args.workers, args.crf, args.tz,
                       args.sort, args.skip_resends)


if __name__ == "__main__":
    main()
