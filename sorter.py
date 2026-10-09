#!/usr/bin/env python3.12
"""
Music sorter daemon + session CLI.

Usage:
  sorter.py              — start the daemon
  sorter.py set          — prompt for current artist, save to session
  sorter.py set PHARAOH  — set artist directly
  sorter.py clear        — clear session (back to tag-based detection)
  sorter.py status       — show current session

While a session artist is set, ALL incoming files are routed to that
artist's folder regardless of their tags. Album tag is still used for
the subfolder name. Single detection (1 track = синглы) still works.
"""

import logging
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

from mutagen import File as MutagenFile
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

INBOX_DIR  = Path(os.environ.get("MUSIC_INBOX", Path.home() / "Музыка" / "inbox"))
MUSIC_ROOT = Path(os.environ.get("MUSIC_ROOT",  Path.home() / "Музыка" / "nicotine" / "spotify"))
SESSION_FILE = INBOX_DIR / ".artist"

SINGLES_FOLDER  = "синглы"
BATCH_WINDOW    = 60   # seconds after last file before sorting
STRAGGLER_WINDOW = 300 # seconds to remember a flushed album for late stragglers
AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac"}
TAG_READ_RETRIES = 5
TAG_READ_DELAY   = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Session ───────────────────────────────────────────────────────────────────

def session_get() -> str | None:
    try:
        text = SESSION_FILE.read_text(encoding="utf-8").strip()
        return text or None
    except FileNotFoundError:
        return None


def session_set(name: str) -> None:
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(name.strip(), encoding="utf-8")


def session_clear() -> None:
    SESSION_FILE.unlink(missing_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def sanitize(name: str) -> str:
    name = name.strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name or "Unknown"


def read_tags_with_retry(path: Path) -> dict:
    last_err = None
    for attempt in range(TAG_READ_RETRIES):
        try:
            audio = MutagenFile(path, easy=True)
            if audio is None:
                return {}

            def first(key):
                v = audio.get(key)
                return v[0].strip() if v else None

            album = first("album") or "Unknown"
            title = first("title") or path.stem
            return {"album": album, "title": title}

        except Exception as e:
            last_err = e
            if attempt < TAG_READ_RETRIES - 1:
                time.sleep(TAG_READ_DELAY)

    log.warning("Could not read tags from %s after %d attempts: %s",
                path.name, TAG_READ_RETRIES, last_err)
    return {}


def resolve_dest_dir(artist: str, album: str, is_single: bool) -> Path:
    s_artist = sanitize(artist)
    s_album  = sanitize(album)
    artist_dir   = MUSIC_ROOT / s_artist
    folder_name  = f"{s_artist} - {s_album}"
    singles_name = f"{s_artist} - {SINGLES_FOLDER}"

    if artist_dir.is_dir():
        return artist_dir / (singles_name if is_single else folder_name)

    # Check for a top-level album folder (legacy single-album artist)
    top_album = MUSIC_ROOT / folder_name
    if top_album.is_dir():
        return top_album

    # New artist — always create Artist/ subfolder
    return artist_dir / (singles_name if is_single else folder_name)


def move_file(src: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        stem, suffix = src.stem, src.suffix
        counter = 1
        while dest.exists():
            dest = dest_dir / f"{stem} ({counter}){suffix}"
            counter += 1
    shutil.move(str(src), str(dest))
    log.info("Moved  %s  →  %s", src.name, dest.relative_to(MUSIC_ROOT))


def _track_title(path: Path) -> str | None:
    """Return normalised track title from tags, or None on failure."""
    try:
        audio = MutagenFile(path, easy=True)
        if audio:
            v = audio.get("title")
            if v:
                return v[0].strip().casefold()
    except Exception:
        pass
    return None


def purge_singles_duplicates(artist: str, album_dir: Path) -> None:
    """Remove from синглы any track whose title matches a track in album_dir."""
    s_artist = sanitize(artist)
    singles_dir = MUSIC_ROOT / s_artist / f"{s_artist} - {SINGLES_FOLDER}"
    if not singles_dir.is_dir():
        return

    # Build title set from the album folder
    album_titles: set[str] = set()
    for f in album_dir.iterdir():
        if f.suffix.lower() in AUDIO_EXTENSIONS:
            t = _track_title(f)
            if t:
                album_titles.add(t)

    if not album_titles:
        return

    for f in list(singles_dir.iterdir()):
        if f.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        t = _track_title(f)
        if t and t in album_titles:
            f.unlink()
            log.info("Removed duplicate from синглы: %s", f.relative_to(MUSIC_ROOT))


# ── Batch accumulator ─────────────────────────────────────────────────────────

# key: (artist, album)  — artist comes from session, not tags
_batches: dict[tuple, dict] = {}
_recent_albums: dict[tuple, tuple] = {}
_lock = threading.Lock()


def _flush_batch(key: tuple) -> None:
    with _lock:
        batch = _batches.pop(key, None)
    if not batch:
        return

    files = list(batch["files"])  # set → list, dedup guaranteed by set
    artist, album = key
    is_single = len(files) == 1
    dest_dir = resolve_dest_dir(artist, album, is_single)

    label = "single → синглы" if is_single else f"album ({len(files)} tracks)"
    log.info("Sorting %s - %s  [%s]", artist, album, label)

    for f in files:
        if f.exists():
            move_file(f, dest_dir)
        else:
            log.warning("File disappeared before move: %s", f)

    if not is_single:
        purge_singles_duplicates(artist, dest_dir)

    with _lock:
        _recent_albums[key] = (dest_dir, time.time() + STRAGGLER_WINDOW)


def enqueue(path: Path) -> None:
    artist = session_get()
    if not artist:
        log.warning("No session artist set — skipping %s  (run: sorter.py set)", path.name)
        return

    tags = read_tags_with_retry(path)
    if not tags:
        log.warning("Giving up on %s — no readable tags", path.name)
        return

    album = tags["album"]
    key = (artist, album)

    with _lock:
        # Straggler: album was already sorted, drop file straight in
        if key in _recent_albums:
            dest_dir, expiry = _recent_albums[key]
            if time.time() < expiry:
                log.info("Straggler  %s  →  %s - %s", path.name, artist, album)
                threading.Thread(target=move_file, args=(path, dest_dir), daemon=True).start()
                return
            else:
                del _recent_albums[key]

        # Expire stale entries
        now = time.time()
        for k in [k for k, (_, exp) in _recent_albums.items() if now > exp]:
            del _recent_albums[k]

        if key in _batches:
            _batches[key]["timer"].cancel()
            _batches[key]["files"].add(path)
        else:
            _batches[key] = {"files": {path}}

        timer = threading.Timer(BATCH_WINDOW, _flush_batch, args=(key,))
        _batches[key]["timer"] = timer
        timer.start()

    log.info("Queued  %s  [%s - %s]  (window %ds)", path.name, artist, album, BATCH_WINDOW)


# ── Filesystem watcher ────────────────────────────────────────────────────────

def _handle_path(path: Path) -> None:
    if path.suffix.lower() in AUDIO_EXTENSIONS and path.exists():
        enqueue(path)


class InboxHandler(FileSystemEventHandler):
    def on_closed(self, event):
        if not event.is_directory:
            _handle_path(Path(event.src_path))

    def on_moved(self, event):
        if not event.is_directory:
            _handle_path(Path(event.dest_path))


# ── Entry points ──────────────────────────────────────────────────────────────

def cmd_set(args: list[str]) -> None:
    if args:
        name = " ".join(args)
    else:
        current = session_get()
        if current:
            print(f"Текущий артист: {current}")
        name = input("Артист: ").strip()
        if not name:
            print("Отменено.")
            return
    session_set(name)
    print(f"✓  Артист: {name}")
    print(f"   Все файлы → {MUSIC_ROOT / sanitize(name)}/")


def cmd_clear() -> None:
    session_clear()
    print("✓  Сессия сброшена — артист будет определяться из тегов")


def cmd_status() -> None:
    artist = session_get()
    if artist:
        print(f"Текущий артист: {artist}")
        print(f"Папка: {MUSIC_ROOT / sanitize(artist)}/")
    else:
        print("Сессия не задана — артист определяется из тегов")


def main_daemon() -> None:
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    artist = session_get()
    log.info("Watching inbox : %s", INBOX_DIR)
    log.info("Music root     : %s", MUSIC_ROOT)
    log.info("Session artist : %s", artist or "(не задан — запусти: sorter.py set)")
    log.info("Batch window   : %ds  |  straggler window: %ds", BATCH_WINDOW, STRAGGLER_WINDOW)

    observer = Observer()
    observer.schedule(InboxHandler(), str(INBOX_DIR), recursive=False)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stopping…")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        main_daemon()
    elif args[0] == "set":
        cmd_set(args[1:])
    elif args[0] == "clear":
        cmd_clear()
    elif args[0] == "status":
        cmd_status()
    else:
        print(f"Неизвестная команда: {args[0]}")
        print("Использование: sorter.py [set [АРТИСТ] | clear | status]")
        sys.exit(1)
