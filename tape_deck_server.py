#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mutagen>=1.47", "pillow>=10"]
# ///
"""Tape Deck Server: your own music folder, for the Tape Deck app.

Point it at a folder laid out as Artist/Album/tracks and type its address
into the app (tape settings > Library > Music server). It serves what the app
asks for and nothing else:

  /library.html        the catalogue: a `const TRACKS = ...;` line and a
                       `const COVERS = ...;` line, with an ETag
  /Music/<path>        the audio, with byte ranges for resumed downloads
  /.covers/<name>      a small JPEG per album
  /favs                favourites and albums listened to, for the owner
  /login               the password form, when there is a password
  /manifest.webmanifest, /health

    python3 tape_deck_server.py --music ~/Music/Library
    uv run tape_deck_server.py --music ~/Music/Library --password secret

Tags (titles, lengths) need mutagen and covers need Pillow; without them it
still runs, with titles from file names and no covers. Everything it writes
(the tag cache, the covers, the favourites) goes in the --state folder.

The passwords: with none, anyone who can reach the server can play the
music and shares one set of favourites. With --password, callers on your home
network (a direct connection from a private address) skip it, and everyone
else signs in; either way they're the owner, with the favourites. With
--guest-password as well, someone signed in with that one is a guest, who can
play the music but not read or change the owner's favourites. Behind a
reverse proxy every request arrives from the proxy, so then everyone is asked;
see the README.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate, parsedate_to_datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

try:
    import mutagen
except ImportError:
    mutagen = None
try:
    from PIL import Image
except ImportError:
    Image = None

VERSION = "1.0.0"

# What iOS and macOS can play. Ogg and Opus are left out: the app can't play them.
AUDIO = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".m4b": "audio/mp4", ".aac": "audio/aac",
         ".flac": "audio/flac", ".wav": "audio/wav", ".aif": "audio/aiff", ".aiff": "audio/aiff"}
IMAGES = {".jpg", ".jpeg", ".png"}
COVER_NAMES = ("cover", "folder", "front", "albumart")
THUMB = 160
TRACK_NUMBER = re.compile(r"^(\d{1,3})[.\s\-_]+")

COOKIE = "tape_session"
SESSION_SECONDS = 30 * 24 * 3600
FAIL_LIMIT = 5            # wrong passwords from one address before a lockout
FAIL_LIMIT_ALL = 25       # and from everyone together, against rotating addresses
LOCKOUT_SECONDS = 300
FAVS_MAX_BODY = 256 * 1024
FAVS_MAX_KEYS = 50_000
FAVS_MAX_KEY = 1000
PLAYED_MAX = 200


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, file=sys.stderr, flush=True)


# ── The library ──

def clean_title(name: str) -> str:
    return TRACK_NUMBER.sub("", Path(name).stem).strip() or Path(name).stem


def cover_name(artist: str, album: str) -> str:
    return hashlib.md5(f"{artist}\x00{album}".encode()).hexdigest()[:12] + ".jpg"


def read_tags(path: Path) -> tuple[str | None, int]:
    """(title or None, length in whole seconds or 0)."""
    if mutagen is None:
        return None, 0
    try:
        f = mutagen.File(path, easy=True)
        if f is None:
            return None, 0
        title = None
        if f.tags:
            values = f.tags.get("title")
            if values:
                title = str(values[0]).strip() or None
        length = round(f.info.length) if getattr(f, "info", None) and f.info.length else 0
        return title, length
    except Exception:  # noqa: BLE001 - a damaged file can raise anything from mutagen or Pillow
        return None, 0


def embedded_art(path: Path) -> bytes | None:
    if mutagen is None:
        return None
    try:
        f = mutagen.File(path)
        if f is None:
            return None
        tags = f.tags
        if tags is not None:
            if hasattr(tags, "getall"):            # ID3
                pics = tags.getall("APIC")
                if pics:
                    return bytes(pics[0].data)
            covr = tags.get("covr")                # MP4
            if covr:
                return bytes(covr[0])
        if getattr(f, "pictures", None):           # FLAC
            return bytes(f.pictures[0].data)
    except Exception:  # noqa: BLE001, S110 - a damaged file can raise anything; it just has no art
        pass
    return None


def write_thumb(data: bytes, out: Path) -> bool:
    try:
        img = Image.open(BytesIO(data)).convert("RGB")
        img.thumbnail((THUMB, THUMB))
        tmp = out.with_suffix(".tmp")
        img.save(tmp, "JPEG", quality=80, optimize=True)
        os.replace(tmp, out)
        return True
    except Exception:  # noqa: BLE001 - a damaged file can raise anything from mutagen or Pillow
        return False


class Catalogue:
    """One scan's result, ready to send: the page, gzipped, and its ETag."""

    def __init__(self, tracks: list[dict], covers: dict[str, str]):
        self.count = len(tracks)
        # One line each, as the app reads them; "</" escaped so neither can end the script
        def line(v) -> str:
            return json.dumps(v, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        body = ("<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                "<meta name=\"robots\" content=\"noindex, nofollow\">"
                "<title>Tape Deck Server</title></head>\n<body style=\"font-family:-apple-system,sans-serif;"
                "background:#0a0a0a;color:#f0f0f0;padding:2em\">\n"
                f"<p>A Tape Deck server with {len(tracks):,} tracks. Type this address into the app: "
                "tape settings, Library, Music server.</p>\n<script>\n"
                f"const TRACKS = {line(tracks)};\n"
                f"const COVERS = {line(covers)};\n"
                "</script>\n</body></html>\n")
        self.body = body.encode("utf-8")
        self.gzipped = gzip.compress(self.body, 6, mtime=0)
        self.etag = '"' + hashlib.sha256(self.body).hexdigest()[:20] + '"'


class Library:
    """The music folder, scanned now and then, with its tags and covers kept in the state folder."""

    def __init__(self, music: Path, state: Path):
        self.music = music.resolve()
        self.state = state
        self.covers_dir = state / "covers"
        self.tag_cache = state / "tags.json"
        self.catalogue: Catalogue | None = None
        self._lock = threading.Lock()

    def files(self) -> list[tuple[str, str, Path]]:
        """(artist, album, path) for every track: Artist/Album/…/track, or Artist/track with no album."""
        found = []
        for artist_dir in sorted(self.music.iterdir()):
            if not artist_dir.is_dir() or artist_dir.name.startswith("."):
                continue
            for item in sorted(artist_dir.iterdir()):
                if item.name.startswith("."):
                    continue
                if item.is_dir():
                    # discs nest a level further down (Album/CD1/…) and still belong to the album
                    for f in sorted(item.rglob("*")):
                        if f.suffix.lower() in AUDIO and not any(p.startswith(".") for p in f.relative_to(item).parts):
                            found.append((artist_dir.name, item.name, f))
                elif item.suffix.lower() in AUDIO:
                    found.append((artist_dir.name, "", item))
        return found

    def scan(self) -> None:
        start = time.time()
        with self._lock:
            files = self.files()
            try:
                cache = json.loads(self.tag_cache.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cache = {}
            now = int(time.time())

            def tags(item):
                _a, _b, path = item
                rel = path.relative_to(self.music).as_posix()
                try:
                    st = path.stat()
                    sig = [int(st.st_mtime), st.st_size]
                except OSError:
                    sig = None
                kept = cache.get(rel)
                # when it came in: the file's date when first seen, kept from then on
                added = kept[4] if kept else (min(sig[0], now) if sig else 0)
                if kept and sig and kept[2:4] == sig:
                    return rel, kept[0], kept[1], sig, added
                title, length = read_tags(path)
                return rel, title, length, sig, added

            tracks, fresh = [], {}
            with ThreadPoolExecutor(max_workers=8) as pool:
                for (artist, album, path), (rel, title, length, sig, added) in zip(files, pool.map(tags, files)):
                    tracks.append({"a": artist, "b": album, "t": title or clean_title(path.name),
                                   "d": length, "f": rel, "n": added})
                    if sig:
                        fresh[rel] = [title, length, sig[0], sig[1], added]
            tmp = self.tag_cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(fresh, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.tag_cache)

            albums: dict[tuple[str, str], list[Path]] = {}
            for artist, album, path in files:
                albums.setdefault((artist, album), []).append(path)
            covers = self.build_covers(albums)
            self.catalogue = Catalogue(tracks, covers)
        log(f"library: {len(tracks):,} tracks, {len(covers):,} covers, {time.time() - start:.1f} s")

    def build_covers(self, albums: dict) -> dict[str, str]:
        """{"artist\\0album": file in covers/}: a folder image first, then the art in the first tracks."""
        if Image is None:
            return {}
        self.covers_dir.mkdir(parents=True, exist_ok=True)

        def one(item):
            (artist, album), paths = item
            name = cover_name(artist, album)
            out = self.covers_dir / name
            if out.exists():
                return artist, album, name
            folder = paths[0].parent if not album else self.music / artist / album
            try:
                images = sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGES and not p.name.startswith("."))
            except OSError:
                images = []
            preferred = [p for p in images if p.stem.lower() in COVER_NAMES]
            for img in preferred or images[:1]:
                try:
                    if write_thumb(img.read_bytes(), out):
                        return artist, album, name
                except OSError:
                    pass
            for track in paths[:3]:
                data = embedded_art(track)
                if data and write_thumb(data, out):
                    return artist, album, name
            return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            return {f"{a}\x00{b}": n for a, b, n in filter(None, pool.map(one, albums.items()))}

    def audio_path(self, rel: str) -> Path | None:
        """The file for a path under /Music/, or None: nothing hidden, nothing outside the folder."""
        parts = [p for p in rel.split("/") if p]
        if not parts or any(p.startswith(".") for p in parts):
            return None
        path = (self.music / Path(*parts)).resolve()
        try:
            path.relative_to(self.music)
        except ValueError:
            return None
        return path if path.is_file() and path.suffix.lower() in AUDIO else None


def rescan_forever(library: Library, minutes: float) -> None:
    while True:
        time.sleep(minutes * 60)
        try:
            library.scan()
        except Exception as e:  # noqa: BLE001 - one failed scan must not stop the server   # a folder gone for a moment (a drive asleep) shouldn't stop the next try
            log(f"library: scan failed: {e}")


# ── Favourites ──

class Favourites:
    """The owner's favourites and albums listened to, as the app's page keeps them in step.

    The page sends changes, not lists (["add"|"del", "track"|"album", key]),
    and the albums it has played since it last asked ({key: seconds}); the
    answer is the whole set, which the page takes as its own.
    """

    def __init__(self, state: Path):
        self.path = state / "favourites.json"
        self.played_path = state / "played.json"
        self.lock = threading.Lock()

    @staticmethod
    def _write(path: Path, value) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {"v": int(data.get("v", 0)), "tracks": list(data.get("tracks", [])), "albums": list(data.get("albums", []))}
        except (OSError, ValueError, AttributeError):
            return {"v": 0, "tracks": [], "albums": []}

    def _played(self, sent=None) -> dict:
        try:
            known = {k: int(v) for k, v in json.loads(self.played_path.read_text(encoding="utf-8")).items()
                     if isinstance(k, str) and isinstance(v, (int, float))}
        except (OSError, ValueError, OverflowError, AttributeError):
            known = {}
        changed, now = False, int(time.time())
        for key, at in sent.items() if isinstance(sent, dict) else []:
            if not (isinstance(key, str) and 0 < len(key) <= FAVS_MAX_KEY
                    and isinstance(at, (int, float)) and not isinstance(at, bool) and 0 < at < 1e11):
                continue
            at = min(int(at), now)   # a device whose clock runs ahead can't hold an album at the top
            if at > known.get(key, 0):
                known[key], changed = at, True
        if changed:
            known = dict(sorted(known.items(), key=lambda kv: kv[1], reverse=True)[:PLAYED_MAX])
            self._write(self.played_path, known)
        return known

    def read(self) -> dict:
        with self.lock:
            return dict(self._load(), played=self._played())

    def apply(self, ops, played=None) -> dict:
        with self.lock:
            state = self._load()
            sets = {"track": set(state["tracks"]), "album": set(state["albums"])}
            changed = False
            for op in ops if isinstance(ops, list) else []:
                if not (isinstance(op, list) and len(op) == 3 and op[0] in ("add", "del") and op[1] in sets
                        and isinstance(op[2], str) and 0 < len(op[2]) <= FAVS_MAX_KEY):
                    continue
                target = sets[op[1]]
                if op[0] == "add" and op[2] not in target and len(target) < FAVS_MAX_KEYS:
                    target.add(op[2])
                    changed = True
                elif op[0] == "del" and op[2] in target:
                    target.discard(op[2])
                    changed = True
            if changed:
                state = {"v": state["v"] + 1, "tracks": sorted(sets["track"]), "albums": sorted(sets["album"])}
                self._write(self.path, state)
            return dict(state, played=self._played(played))


# ── The password ──

class Gate:
    """The owner's password and a guest's, a signed cookie that says which, and a brake on guessing."""

    def __init__(self, password: str, guest: str, home: list, tailscale: bool):
        self.passwords = {"owner": password, "guest": guest}
        self.home = home
        self.tailscale = tailscale
        # From the passwords: changing either signs everyone out, with nothing to clear
        self.key = hashlib.sha256(("tape-deck-server-v1:" + password + "\x00" + guest).encode()).digest()
        self.fails: dict[str, tuple[int, float]] = {}
        self.lock = threading.Lock()

    def _sign(self, value: str) -> str:
        return hmac.new(self.key, value.encode(), hashlib.sha256).hexdigest()

    def role(self, given: str) -> str | None:
        """Which password this is, if either. Both are compared, so the time taken doesn't say."""
        found = None
        for role, password in self.passwords.items():
            if password and hmac.compare_digest(given.encode(), password.encode()):
                found = role
        return found

    def token(self, role: str) -> str:
        value = f"{role}.{int(time.time()) + SESSION_SECONDS}"
        return value + "." + self._sign(value)

    def token_role(self, token: str) -> str | None:
        """The role a cookie was signed for, while it lasts."""
        try:
            role, expiry, sig = token.split(".", 2)
            if role not in self.passwords or int(expiry) < time.time():
                return None
        except (ValueError, AttributeError):
            return None
        return role if hmac.compare_digest(sig, self._sign(f"{role}.{expiry}")) else None

    def in_home(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self.home)

    def locked(self, ip: str) -> bool:
        now = time.time()
        with self.lock:
            return self.fails.get(ip, (0, 0.0))[1] > now or self.fails.get("*", (0, 0.0))[1] > now

    def failed(self, ip: str) -> None:
        now = time.time()
        with self.lock:
            for key, limit in ((ip, FAIL_LIMIT), ("*", FAIL_LIMIT_ALL)):
                n = self.fails.get(key, (0, 0.0))[0] + 1
                self.fails[key] = (0, now + LOCKOUT_SECONDS) if n >= limit else (n, 0.0)

    def passed(self, ip: str) -> None:
        with self.lock:
            self.fails.pop(ip, None)
            self.fails.pop("*", None)


def parse_networks(spec: str) -> list:
    """--home-networks: "private" (the default), "none", or a list of CIDRs."""
    spec = spec.strip().lower()
    if spec in ("", "none"):
        return []
    if spec == "private":
        spec = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,fc00::/7,fe80::/10"
    return [ipaddress.ip_network(n.strip(), strict=False) for n in spec.split(",") if n.strip()]


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Tape Deck Server</title>
<style>
  body { display: flex; min-height: 100vh; margin: 0; align-items: center; justify-content: center;
         background: #0a0a0a; color: #f0f0f0; font-family: -apple-system, BlinkMacSystemFont, sans-serif; }
  form { width: 100%; max-width: 300px; padding: 24px; text-align: center; }
  h1 { font-size: 15px; letter-spacing: 0.08em; text-transform: uppercase; color: #fbca12; }
  input, button { box-sizing: border-box; width: 100%; font-size: 16px; padding: 12px; border-radius: 8px; }
  input { background: #1a1a1a; border: 1px solid #444; color: #f0f0f0; text-align: center; }
  button { margin-top: 10px; background: #fbca12; color: #000; border: 0; font-weight: 700; }
  p { color: #e8a0a0; font-size: 13px; min-height: 1em; }
</style></head><body>
<form method="POST" action="/login">
  <h1>Tape Deck Server</h1>
  <input type="password" name="p" placeholder="Password" aria-label="Password" autocomplete="current-password" autofocus required>
  <button type="submit">Sign In</button>
  <p>@@ERROR@@</p>
</form></body></html>
"""
MANIFEST = json.dumps({"name": "Tape Deck Server", "short_name": "Tape Deck", "display": "standalone"}).encode()


# ── The server ──

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TapeDeckServer/" + VERSION
    library: Library
    favourites: Favourites
    gate: Gate | None

    def log_message(self, fmt, *args):
        if self.server.verbose:
            log(f"{self.client_address[0]} {fmt % args}")

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass

    # who's asking

    def forwarded(self) -> bool:
        return bool(self.headers.get("X-Forwarded-For") or self.headers.get("Forwarded"))

    def at_home(self) -> bool:
        """The owner: a direct caller from a home network, or (with --tailscale) a named tailnet user.

        Behind a proxy the peer is the proxy, so a forwarded request is never
        placed by its address. Tailscale Serve strips any Tailscale-* headers a
        client sent and sets its own, so with --tailscale its word is taken,
        from a proxy on a home network only.
        """
        if self.gate is None:
            return True
        peer = self.client_address[0]
        if self.gate.tailscale and self.headers.get("Tailscale-User-Login") \
                and not self.headers.get("Tailscale-Funnel-Request"):
            return self.gate.in_home(peer)
        return not self.forwarded() and self.gate.in_home(peer)

    def cookie(self) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.partition("=")
            if k.strip() == COOKIE:
                return v.strip()
        return ""

    def role(self) -> str | None:
        """"owner", "guest", or None for someone who hasn't signed in."""
        if self.gate is None or self.at_home():
            return "owner"
        return self.gate.token_role(self.cookie())

    def signed_in(self) -> bool:
        return self.role() is not None

    def caller(self) -> str:
        """Who to count a wrong password against: the last forwarded address, which a client can't forge."""
        fwd = self.headers.get("X-Forwarded-For", "")
        return fwd.split(",")[-1].strip() if fwd.strip() else self.client_address[0]

    # answers

    def send(self, status, body: bytes = b"", ctype="text/plain; charset=utf-8", headers=()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, value, status=HTTPStatus.OK) -> None:
        self.send(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8", CORS)

    def login_page(self, status=HTTPStatus.OK, error="") -> None:
        self.send(status, LOGIN_PAGE.replace("@@ERROR@@", error).encode(), "text/html; charset=utf-8")

    def session_cookie(self, value: str, age: int) -> str:
        c = f"{COOKIE}={value}; Path=/; Max-Age={age}; HttpOnly; SameSite=Lax"
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            c += "; Secure"
        return c

    # routes

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        route = unquote(urlsplit(self.path).path)
        if route == "/health":
            self.send(HTTPStatus.OK, b"ok\n")
            return
        if self.gate:
            if route == "/logout":
                self.send(HTTPStatus.SEE_OTHER, headers=[("Location", "/"), ("Set-Cookie", self.session_cookie("", 0))])
                return
            if not self.signed_in():
                if route in ("/", "/library.html", "/login"):
                    self.login_page()
                else:
                    self.send(HTTPStatus.UNAUTHORIZED, b"Unauthorized\n")
                return
        if route in ("/", "/login"):
            self.send(HTTPStatus.FOUND, headers=[("Location", "/library.html")])
        elif route == "/library.html":
            self.catalogue()
        elif route == "/manifest.webmanifest":
            self.send(HTTPStatus.OK, MANIFEST, "application/manifest+json")
        elif route == "/favs":
            if self.role() == "owner":
                self.json(self.favourites.read())
            else:
                self.send(HTTPStatus.FORBIDDEN, b"Forbidden\n", headers=CORS)
        elif route.startswith("/.covers/"):
            self.cover(route[len("/.covers/"):])
        elif route.startswith("/Music/"):
            self.audio(route[len("/Music/"):])
        else:
            self.send(HTTPStatus.NOT_FOUND, b"Not found\n")

    def do_OPTIONS(self):
        if urlsplit(self.path).path == "/favs":
            self.send(HTTPStatus.NO_CONTENT, headers=CORS)
        else:
            self.send(HTTPStatus.NOT_FOUND, b"Not found\n")

    def do_POST(self):
        route = urlsplit(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if route == "/favs":
            if not self.signed_in():
                self.send(HTTPStatus.UNAUTHORIZED, b"Unauthorized\n", headers=CORS)
            elif self.role() != "owner":
                self.send(HTTPStatus.FORBIDDEN, b"Forbidden\n", headers=CORS)
            elif not 0 < length <= FAVS_MAX_BODY:
                self.send(HTTPStatus.BAD_REQUEST, b"Bad request\n", headers=CORS)
            else:
                try:
                    sent = json.loads(self.rfile.read(length).decode("utf-8"))
                    ops, played = sent.get("ops", []), sent.get("played")
                except (ValueError, AttributeError):
                    self.send(HTTPStatus.BAD_REQUEST, b"Bad request\n", headers=CORS)
                    return
                self.json(self.favourites.apply(ops, played))
            return
        if route != "/login" or not self.gate:
            self.send(HTTPStatus.NOT_FOUND, b"Not found\n")
            return
        ip = self.caller()
        if self.gate.locked(ip):
            self.login_page(HTTPStatus.TOO_MANY_REQUESTS, "Too many tries. Wait a few minutes.")
            return
        if not 0 < length <= 4096:
            self.send(HTTPStatus.BAD_REQUEST, b"Bad request\n")
            return
        given = parse_qs(self.rfile.read(length).decode("utf-8", "replace")).get("p", [""])[0]
        role = self.gate.role(given)
        if role is None:
            self.gate.failed(ip)
            time.sleep(0.4)
            self.login_page(HTTPStatus.UNAUTHORIZED, "That password isn't right.")
            return
        self.gate.passed(ip)
        self.send(HTTPStatus.SEE_OTHER, headers=[("Location", "/library.html"),
                                                 ("Set-Cookie", self.session_cookie(self.gate.token(role), SESSION_SECONDS))])

    def catalogue(self) -> None:
        cat = self.library.catalogue
        if cat is None:
            self.send(HTTPStatus.SERVICE_UNAVAILABLE, b"Still reading the library\n", headers=[("Retry-After", "30")])
            return
        headers = [("ETag", cat.etag), ("Cache-Control", "private, no-cache"), ("Vary", "Accept-Encoding")]
        if cat.etag in [t.strip() for t in self.headers.get("If-None-Match", "").split(",")]:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            return
        body = cat.body
        if "gzip" in self.headers.get("Accept-Encoding", ""):
            body = cat.gzipped
            headers.append(("Content-Encoding", "gzip"))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def cover(self, name: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{12}\.jpg", name):
            self.send(HTTPStatus.NOT_FOUND, b"Not found\n")
            return
        path = self.library.covers_dir / name
        try:
            data = path.read_bytes()
        except OSError:
            self.send(HTTPStatus.NOT_FOUND, b"Not found\n")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def audio(self, rel: str) -> None:
        path = self.library.audio_path(rel)
        if path is None:
            self.send(HTTPStatus.NOT_FOUND, b"Not found\n")
            return
        try:
            st = path.stat()
            f = path.open("rb")
        except OSError:
            self.send(HTTPStatus.NOT_FOUND, b"Not found\n")
            return
        with f:
            size, modified = st.st_size, formatdate(st.st_mtime, usegmt=True)
            start, end = 0, size - 1
            ranged = False
            rng = self.headers.get("Range", "")
            if_range = self.headers.get("If-Range")
            # A resumed download names the Last-Modified it began under: a file
            # changed since gets sent whole, not a tail that won't fit (RFC 9110 13.1.5)
            if rng.startswith("bytes=") and (not if_range or self._same_date(if_range, st.st_mtime)):
                try:
                    first, last = rng[6:].split(",")[0].strip().split("-", 1)
                    if first == "":
                        start, end = max(0, size - int(last)), size - 1
                    else:
                        start, end = int(first), min(int(last) if last else size - 1, size - 1)
                except ValueError:
                    self.send(HTTPStatus.BAD_REQUEST, b"Bad range\n")
                    return
                if start >= size or start > end:
                    self.send(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, headers=[("Content-Range", f"bytes */{size}")])
                    return
                ranged = True
            self.send_response(HTTPStatus.PARTIAL_CONTENT if ranged else HTTPStatus.OK)
            self.send_header("Content-Type", AUDIO[path.suffix.lower()])
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Last-Modified", modified)
            self.send_header("Cache-Control", "private, max-age=86400")
            if ranged:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(256 * 1024, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    @staticmethod
    def _same_date(header: str, mtime: float) -> bool:
        try:
            return int(parsedate_to_datetime(header).timestamp()) == int(mtime)
        except (TypeError, ValueError):
            return False


# Any origin may ask for /favs: the app's page is another origin. The password still applies.
CORS = (("Access-Control-Allow-Origin", "*"),
        ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
        ("Access-Control-Allow-Headers", "Content-Type"))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    verbose = False


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Serve a music folder to the Tape Deck app.",
                                 epilog="Each option can also be set by the environment variable named in brackets.")
    ap.add_argument("--music", default=env("TAPE_MUSIC", "/music"), help="the music folder, Artist/Album/tracks [TAPE_MUSIC]")
    ap.add_argument("--state", default=env("TAPE_STATE", "/state"),
                    help="where the tag cache, covers and favourites are kept [TAPE_STATE]")
    ap.add_argument("--port", type=int, default=int(env("TAPE_PORT", "8080")), help="[TAPE_PORT] default 8080")
    ap.add_argument("--bind", default=env("TAPE_BIND", "0.0.0.0"), help="address to listen on [TAPE_BIND]")
    ap.add_argument("--password", default=None,
                    help="one shared password [TAPE_PASSWORD]; better from the environment, out of the process list")
    ap.add_argument("--guest-password", default=None,
                    help="a second password, for guests: the music, not the owner's favourites [TAPE_GUEST_PASSWORD]")
    ap.add_argument("--home-networks", default=env("TAPE_HOME_NETWORKS", "private"),
                    help="who skips the password and owns the favourites: 'private' (the default), 'none', "
                         "or CIDRs separated by commas [TAPE_HOME_NETWORKS]")
    ap.add_argument("--tailscale", action="store_true", default=env("TAPE_TAILSCALE") == "1",
                    help="behind Tailscale Serve: a named tailnet user counts as home [TAPE_TAILSCALE=1]")
    ap.add_argument("--rescan", type=float, default=float(env("TAPE_RESCAN_MINUTES", "15")),
                    help="minutes between scans of the music folder [TAPE_RESCAN_MINUTES] default 15")
    ap.add_argument("--verbose", action="store_true", default=env("TAPE_VERBOSE") == "1", help="log every request [TAPE_VERBOSE=1]")
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args(argv)

    music, state = Path(args.music).expanduser(), Path(args.state).expanduser()
    if not music.is_dir():
        print(f"No music folder at {music}", file=sys.stderr)
        return 1
    try:
        state.mkdir(parents=True, exist_ok=True)
        probe = state / f".write-{secrets.token_hex(4)}"
        probe.write_text("")
        probe.unlink()
    except OSError as e:
        print(f"Can't write to the state folder {state}: {e}", file=sys.stderr)
        return 1
    password = (args.password if args.password is not None else env("TAPE_PASSWORD")).strip()
    guest = (args.guest_password if args.guest_password is not None else env("TAPE_GUEST_PASSWORD")).strip()
    if guest and not password:
        print("A guest password needs the owner's password too (--password, TAPE_PASSWORD)", file=sys.stderr)
        return 1
    if guest and guest == password:
        print("The guest password has to differ from the owner's", file=sys.stderr)
        return 1
    try:
        home = parse_networks(args.home_networks)
    except ValueError as e:
        print(f"--home-networks: {e}", file=sys.stderr)
        return 1

    library = Library(music, state)
    Handler.library = library
    Handler.favourites = Favourites(state)
    Handler.gate = Gate(password, guest, home, args.tailscale) if password else None

    server = Server((args.bind, args.port), Handler)
    server.verbose = args.verbose
    log(f"Tape Deck Server {VERSION} on {args.bind}:{args.port}, music from {music}, state in {state}")
    if mutagen is None or Image is None:
        log("without " + " and ".join(n for n, m in (("mutagen", mutagen), ("Pillow", Image)) if m is None)
            + ": titles from file names" + ("" if mutagen else ", no lengths") + ("" if Image else ", no covers"))
    if password:
        log("password on: " + (f"skipped from {args.home_networks}" if home else "asked of everyone")
            + (", and for named tailnet users" if args.tailscale else "")
            + ("; a guest password too" if guest else ""))
    else:
        log("password off: anyone who can reach this address can play the music and change the favourites")

    def first_scan():
        try:
            library.scan()
        except Exception as e:  # noqa: BLE001 - one failed scan must not stop the server
            log(f"library: scan failed: {e}")
        if args.rescan > 0:
            rescan_forever(library, args.rescan)

    threading.Thread(target=first_scan, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
