#!/usr/bin/env python3
"""Tape Deck Server, checked as the app uses it: a folder of tones, the server
as a subprocess, and requests the way the app and its page make them.

    python3 tests/test_server.py         # stdlib only; with mutagen and Pillow, the tags and covers too
"""
import gzip
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import ClassVar

SERVER = Path(__file__).resolve().parent.parent / "tape_deck_server.py"
try:
    import mutagen  # noqa: F401
    HAVE_TAGS = True
except ImportError:
    HAVE_TAGS = False
try:
    from PIL import Image
except ImportError:
    Image = None


def tone(path: Path, seconds=2, hz=440):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * hz * k / 8000)))
                               for k in range(8000 * seconds)))


def library(root: Path):
    tone(root / "Test Artist" / "First Album" / "01 Song One.wav")
    tone(root / "Test Artist" / "First Album" / "02 Song Two.wav", hz=550)
    tone(root / "Test Artist" / "Double" / "CD1" / "01 Disc One.wav")
    tone(root / "Other Band" / "Loose Track.wav")
    (root / "Other Band" / "skip.ogg").write_bytes(b"OggS not playable")
    tone(root / "Other Band" / ".hidden" / "01 Secret.wav")
    (root / ".DS_Store").write_bytes(b"x")
    if Image:
        Image.new("RGB", (400, 400), (200, 40, 40)).save(root / "Test Artist" / "First Album" / "cover.jpg")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


OPEN = urllib.request.build_opener(NoRedirect)


class Running:
    """The server in a subprocess, with these environment variables."""

    def __init__(self, music: Path, state: Path, **env):
        self.port = free_port()
        full = {k: v for k, v in os.environ.items() if not k.startswith("TAPE_")}
        full.update(TAPE_MUSIC=str(music), TAPE_STATE=str(state), TAPE_PORT=str(self.port),
                    TAPE_BIND="127.0.0.1", **env)
        self.proc = subprocess.Popen([sys.executable, str(SERVER)], env=full,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(100):   # up, and the first scan done
            try:
                if self.get("/health")[0] == 200 and (env.get("TAPE_PASSWORD") or self.get("/library.html")[0] != 503):
                    break
            except OSError:
                pass
            time.sleep(0.1)
        if env.get("TAPE_PASSWORD"):
            time.sleep(1)   # no catalogue to wait on without signing in; give the scan its moment

    def call(self, path, method="GET", body=None, **headers):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, data=body,
                                     headers={k.replace("_", "-"): v for k, v in headers.items()})
        try:
            with OPEN.open(req, timeout=10) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers, e.read()

    def get(self, path, **headers):
        return self.call(path, **headers)

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)
        self.proc.stderr.close()


def embedded(marker: str, html: str):
    """As the app reads the catalogue: the JSON after the marker, to the end of its line, less the semicolon."""
    rest = html[html.index(marker) + len(marker):]
    line = rest.split("\n", 1)[0]
    return json.loads(line.removesuffix(";"))


class Base(unittest.TestCase):
    env: ClassVar[dict] = {}

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.music, cls.state = cls.tmp / "music", cls.tmp / "state"
        library(cls.music)
        cls.server = Running(cls.music, cls.state, **cls.env)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)


class Open(Base):
    """No password: the library and the favourites for anyone."""

    def catalogue(self):
        status, h, body = self.server.get("/library.html")
        self.assertEqual(status, 200)
        html = body.decode()
        return h, embedded("const TRACKS = ", html), embedded("const COVERS = ", html)

    def test_catalogue_as_the_app_reads_it(self):
        _, tracks, _ = self.catalogue()
        got = sorted((t["a"], t["b"], t["t"], t["f"]) for t in tracks)
        self.assertEqual(got, [
            ("Other Band", "", "Loose Track", "Other Band/Loose Track.wav"),
            ("Test Artist", "Double", "Disc One", "Test Artist/Double/CD1/01 Disc One.wav"),
            ("Test Artist", "First Album", "Song One", "Test Artist/First Album/01 Song One.wav"),
            ("Test Artist", "First Album", "Song Two", "Test Artist/First Album/02 Song Two.wav"),
        ], "folders give the artist and album, discs belong to their album, Ogg and hidden files are left out")
        for t in tracks:
            self.assertIsInstance(t["n"], int)
            self.assertEqual(t["d"], 2 if HAVE_TAGS else 0)

    @unittest.skipUnless(Image, "Pillow")
    def test_cover(self):
        _, _, covers = self.catalogue()
        name = covers.get("Test Artist\x00First Album")
        self.assertTrue(name, covers)
        status, h, body = self.server.get("/.covers/" + name)
        self.assertEqual((status, h["Content-Type"]), (200, "image/jpeg"))
        self.assertLessEqual(max(Image.open(__import__("io").BytesIO(body)).size), 160)
        self.assertEqual(self.server.get("/.covers/../tags.json")[0], 404)

    def test_etag_and_gzip(self):
        h, _, _ = self.catalogue()
        self.assertEqual(self.server.get("/library.html", If_None_Match=h["ETag"])[0], 304)
        status, h2, body = self.server.get("/library.html", Accept_Encoding="gzip")
        self.assertEqual((status, h2["Content-Encoding"]), (200, "gzip"))
        self.assertIn(b"const TRACKS = ", gzip.decompress(body))

    def test_probe(self):
        self.assertEqual(self.server.call("/manifest.webmanifest", "HEAD")[0], 200)

    def test_ranges(self):
        path = "/Music/Test%20Artist/First%20Album/01%20Song%20One.wav"
        whole = (self.music / "Test Artist/First Album/01 Song One.wav").read_bytes()
        status, h, body = self.server.get(path)
        self.assertEqual((status, body, h["Accept-Ranges"]), (200, whole, "bytes"))
        status, h, body = self.server.get(path, Range="bytes=100-199")
        self.assertEqual((status, body, h["Content-Range"]), (206, whole[100:200], f"bytes 100-199/{len(whole)}"))
        self.assertEqual(self.server.get(path, Range="bytes=-50")[2], whole[-50:])
        self.assertEqual(self.server.get(path, Range="bytes=1000-")[2], whole[1000:])
        self.assertEqual(self.server.get(path, Range=f"bytes={len(whole)}-")[0], 416)
        lm = h["Last-Modified"]
        self.assertEqual(self.server.get(path, Range="bytes=100-", If_Range=lm)[0], 206, "resumed under the same date")
        old = "Mon, 01 Jan 2001 00:00:00 GMT"
        self.assertEqual(self.server.get(path, Range="bytes=100-", If_Range=old)[:3:2], (200, whole),
                         "a file changed since comes whole")

    def test_nothing_else_is_served(self):
        for path in ("/Music/../state/tags.json", "/Music/%2e%2e/state/tags.json", "/Music/Other%20Band/skip.ogg",
                     "/Music/Other%20Band/.hidden/01%20Secret.wav", "/Music/.DS_Store", "/tags.json", "/Music/"):
            self.assertEqual(self.server.get(path)[0], 404, path)

    def test_favourites_shared_without_a_password(self):
        sent = json.dumps({"ops": [["add", "album", "A\x00B"]], "played": {"A\x00B": int(time.time())}}).encode()
        status, h, body = self.server.call("/favs", "POST", sent, Content_Type="text/plain")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["albums"], ["A\x00B"])
        self.assertEqual(h["Access-Control-Allow-Origin"], "*")
        self.assertEqual(json.loads(self.server.get("/favs", X_Forwarded_For="203.0.113.9")[2])["albums"], ["A\x00B"])
        self.assertEqual(self.server.call("/favs", "OPTIONS")[0], 204)

    def test_new_album_found_by_the_next_scan(self):
        tone(self.music / "New Artist" / "New Album" / "01 Fresh.wav")
        for _ in range(60):
            _, tracks, _ = self.catalogue()
            if any(t["a"] == "New Artist" for t in tracks):
                break
            time.sleep(0.25)
        self.assertTrue(any(t["t"] == "Fresh" for t in tracks))


Open.env = {"TAPE_RESCAN_MINUTES": "0.02"}


class Password(Base):
    """The owner's password and a guest's: home callers skip both; the owner has the favourites, a guest doesn't."""
    env: ClassVar[dict] = {"TAPE_PASSWORD": "sesame", "TAPE_GUEST_PASSWORD": "visitor"}
    outside: ClassVar[dict] = {"X_Forwarded_For": "203.0.113.9"}   # through a proxy: never placed by address

    def sign_in(self, password=b"sesame", **headers):
        status, h, _ = self.server.call("/login", "POST", b"p=" + password, **headers)
        self.assertEqual(status, 303)
        return h["Set-Cookie"].split(";")[0]

    def test_home_skips_it(self):
        status, _, body = self.server.get("/library.html")
        self.assertEqual(status, 200)
        self.assertIn(b"const TRACKS = ", body)
        self.assertEqual(self.server.get("/favs")[0], 200)

    def test_outside_is_asked(self):
        status, _, body = self.server.get("/library.html", **self.outside)
        self.assertEqual(status, 200)
        self.assertIn(b'action="/login"', body, "the sign-in page, which is how the app knows to ask")
        self.assertNotIn(b"const TRACKS", body)
        for path in ("/manifest.webmanifest", "/favs", "/Music/Other%20Band/Loose%20Track.wav"):
            self.assertEqual(self.server.get(path, **self.outside)[0], 401, path)
        self.assertEqual(self.server.get("/health", **self.outside)[0], 200)

    def test_owner_from_outside(self):
        owner = dict(self.outside, Cookie=self.sign_in(**self.outside))
        self.assertEqual(self.server.get("/favs", **owner)[0], 200, "the owner's password has the favourites from anywhere")

    def test_guest(self):
        cookie = self.sign_in(b"visitor", **self.outside)
        guest = dict(self.outside, Cookie=cookie)
        self.assertIn(b"const TRACKS = ", self.server.get("/library.html", **guest)[2])
        self.assertEqual(self.server.call("/manifest.webmanifest", "HEAD", **guest)[0], 200)
        self.assertEqual(self.server.get("/Music/Other%20Band/Loose%20Track.wav", Range="bytes=0-9", **guest)[0], 206)
        before = self.server.get("/favs")[2]
        self.assertEqual(self.server.get("/favs", **guest)[0], 403, "not the owner's favourites")
        sent = json.dumps({"ops": [["add", "album", "G\x00H"]], "played": {"G\x00H": int(time.time())}}).encode()
        status, h, _ = self.server.call("/favs", "POST", sent, Content_Type="text/plain", **guest)
        self.assertEqual((status, h["Access-Control-Allow-Origin"]), (403, "*"))
        self.assertEqual(self.server.get("/favs")[2], before, "nothing a guest sent was kept")
        self.assertIn("Max-Age=0", self.server.get("/logout", **guest)[1]["Set-Cookie"])

    def test_a_forged_cookie(self):
        guest = self.sign_in(b"visitor", **self.outside)
        promoted = guest.replace("tape_session=guest.", "tape_session=owner.")
        self.assertEqual(self.server.get("/favs", Cookie=promoted, **self.outside)[0], 401, "a guest's cookie made the owner's")
        forged = f"tape_session=owner.{int(time.time()) + 999}.{'0' * 64}"
        self.assertEqual(self.server.get("/favs", Cookie=forged, **self.outside)[0], 401)

    def test_wrong_passwords_lock_out(self):
        who = {"X_Forwarded_For": "198.51.100.7"}
        for _ in range(5):
            self.assertEqual(self.server.call("/login", "POST", b"p=wrong", **who)[0], 401)
        self.assertEqual(self.server.call("/login", "POST", b"p=sesame", **who)[0], 429, "even the right one, for a while")
        self.assertEqual(self.server.call("/login", "POST", b"p=sesame", X_Forwarded_For="198.51.100.7, 192.0.2.1")[0], 303,
                         "counted against the last forwarded address, which the client can't forge")


class GuestNeedsOwner(unittest.TestCase):
    def test_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            for env in ({"TAPE_GUEST_PASSWORD": "x"}, {"TAPE_PASSWORD": "same", "TAPE_GUEST_PASSWORD": "same"}):
                full = {k: v for k, v in os.environ.items() if not k.startswith("TAPE_")}
                full.update(TAPE_MUSIC=tmp, TAPE_STATE=tmp + "/state", **env)
                run = subprocess.run([sys.executable, str(SERVER)], env=full, capture_output=True, timeout=10, check=False)
                self.assertEqual(run.returncode, 1, env)


class Tailscale(Base):
    """--tailscale: behind Tailscale Serve, a named tailnet user is home; a forged header from outside isn't."""
    env: ClassVar[dict] = {"TAPE_PASSWORD": "sesame", "TAPE_TAILSCALE": "1"}

    def test_named_user_is_home(self):
        user = {"Tailscale_User_Login": "me@example.com", "X_Forwarded_For": "100.64.0.7"}
        self.assertEqual(self.server.get("/favs", **user)[0], 200)
        self.assertEqual(self.server.get("/favs", Tailscale_Funnel_Request="?1", **user)[0], 401)


class AskEveryone(Base):
    """--home-networks none: the password for everyone, home included."""
    env: ClassVar[dict] = {"TAPE_PASSWORD": "sesame", "TAPE_HOME_NETWORKS": "none"}

    def test_asked(self):
        self.assertEqual(self.server.get("/favs")[0], 401)
        cookie = self.server.call("/login", "POST", b"p=sesame")[1]["Set-Cookie"].split(";")[0]
        self.assertEqual(self.server.get("/library.html", Cookie=cookie)[0], 200)
        self.assertEqual(self.server.get("/favs", Cookie=cookie)[0], 200, "the owner, signed in, has the favourites")


if __name__ == "__main__":
    unittest.main(verbosity=2)
