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
burned in, and the message gives the conversation and who sent it.

Chat media has no location. The timezone comes from the nearest GPS fix in
your memories (within 30 days), otherwise --tz, otherwise UTC.

Usage:
  snap_chat_media.py OUT_DIR mydata.zip [mydata-2.zip ...]
"""
import argparse
import os
import re
import shutil
import sys
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from snap_memories import (  # noqa: E402
    VIDEO_EXTS, NameAllocator, Timeline, find_zips, load_json, member_ref, overlays_match,
    probe_all, read_member, real_image_ext, run_jobs, thumb_diff, write_report,
)

# chat_media/2026-10-19_<id>.<ext>, where <id> may start with media~/overlay~/thumbnail~
CHAT_MEDIA_RE = re.compile(
    r"^chat_media/\d{4}-\d{2}-\d{2}_((?:(media|overlay|thumbnail)~)?.+)\.(\w+)$")
OVERLAY_EXTS = {"png", "webp"}

# two videos in the same second are copies of one snap if their lengths are
# within this much (copies differ by ~0.04s) and their first frames look alike
SAME_SNAP_MAX_LEN_DIFF = 0.15
SAME_SNAP_MAX_FRAME_DIFF = 20


def index_chat_media(zips):
    """A snap sent several times shows up as the same filename repeated in the
    zip with a different mtime for each send, so each one is kept."""
    files = {}
    crc_of_name = {}
    for zip_path in zips:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                m = CHAT_MEDIA_RE.match(info.filename)
                if not m or info.file_size == 0:
                    continue
                # read_member() can only get the last entry with a given name,
                # so repeats are only safe if they're the same bytes
                if crc_of_name.setdefault(info.filename, info.CRC) != info.CRC:
                    print(f"  warning: skipping second, different copy of {info.filename}")
                    continue
                ref = member_ref(zip_path, info, m.group(3).lower())
                ref["id"] = m.group(1)
                ref["role"] = m.group(2) or ""
                files.setdefault((info.filename, ref["utc"]), ref)
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
        if ref["role"] == "thumbnail" and has_video:
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


def process_chat_media(out, zips, workers=4, crf=18, fallback_tz=None):
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

    for second in sorted(by_second):
        outputs, skipped = plan_second(by_second[second], probes, conversations)

        for ref, action, note in skipped:
            if action == "unpaired overlay":
                (review / "unpaired_overlays" / Path(ref["name"]).name).write_bytes(read_member(ref))
            elif action == "unreadable":
                (review / "unreadable" / Path(ref["name"]).name).write_bytes(read_member(ref))
            report.append(dict(action=f"skipped-{action.replace(' ', '-')}",
                               sources=short_name(ref), note=note))

        for output in outputs:
            msg, matched = conversations.message_for(output["refs"], second)
            utc = output["main"]["utc"]
            tz, tz_source = timeline.timezone_for(utc, None)
            folder = folder_name(msg["conversation"]) if msg else "_unknown"
            name = name_for(utc, tz, output_ext(output), folder)
            jobs.append(dict(
                kind=output["kind"], out_name=name, utc=utc, tz=str(tz), gps=None,
                description=describe(msg),
                parts=[dict(main=output["main"], overlay=output["overlay"],
                            probe=probes.get(output["main"]["name"]))],
            ))
            report_info[name] = dict(
                sources=" + ".join(short_name(r) for r in output["refs"]),
                utc=utc.strftime("%Y-%m-%d %H:%M:%S"),
                local=utc.astimezone(tz).isoformat(),
                tz=f"{tz} ({tz_source})",
                conversation=msg["conversation"] if msg else "",
                sender=msg["sender"] if msg else "",
                direction=("sent" if msg["sent"] else "received") if msg else "",
                matched=matched,
            )

    print(f"Processing {len(jobs)} outputs...")

    def on_done(name, ok, note):
        report.append(dict(output=name, action="ok" if ok else "FAILED", note=note,
                           **report_info[name]))

    run_jobs(jobs, work, out, crf, workers, on_done)
    shutil.rmtree(work, ignore_errors=True)
    write_report(out / "_report.csv", report,
                 ["output", "action", "sources", "utc", "local", "tz", "conversation",
                  "sender", "direction", "matched", "note"])

    done = [r for r in report if r.get("output")]
    kinds = Counter(job["kind"] for job in jobs)
    skips = Counter(r["action"] for r in report if r["action"].startswith("skipped-"))
    matches = Counter(r["matched"] for r in done)
    folders = {r["output"].split("/", 1)[0] for r in done}
    print(f"\nDone in {(time.time() - start) / 60:.1f}m -> {out}")
    print(f"  outputs: {len(done)} (failed {sum(r['action'] == 'FAILED' for r in done)}) in "
          f"{len(folders)} conversation folders")
    print(f"  photos {kinds['photo']}, videos {kinds['video']}, voice notes {kinds['audio']}, "
          f"overlays burned in {sum('composited' in r.get('note', '') for r in done)}")
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
    args = parser.parse_args()
    process_chat_media(args.out, find_zips(args.zips), args.workers, args.crf, args.tz)


if __name__ == "__main__":
    main()
