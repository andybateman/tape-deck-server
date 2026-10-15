# Tape Deck Server

Your own music folder, served to the [Tape Deck](https://andybateman.github.io/tapedeck/) app on iPhone, iPad and Mac. One small Python file, or one Docker container; nothing else to run.

Point it at a folder of albums, type its address into the app (tape settings › Library › Music server), and the whole library is listed in the app beside what's on the device. A track downloads when you play it, the rest of its album after it, and it plays offline from then on.

## Run it with Docker

```sh
docker run -d --name tape-deck-server --restart unless-stopped \
  -p 8080:8080 \
  -v /path/to/your/music:/music:ro \
  -v tape-state:/state \
  -e TAPE_PASSWORD=choose-one \
  ghcr.io/andybateman/tape-deck-server:latest
```

Or with Compose: copy [`compose.yaml`](../compose.yaml), set the music folder's path in it, put `TAPE_PASSWORD=…` in a `.env` file beside it, and `docker compose up -d`.

The image runs as an unprivileged user, reads the music folder read-only, and keeps what it writes in the `/state` volume. The image is for `linux/amd64` and `linux/arm64`, so it runs on a Raspberry Pi as well as a NAS.

## Or run the script

Python 3.10 or later. With [uv](https://docs.astral.sh/uv/), the two libraries it uses come by themselves:

```sh
uv run tape_deck_server.py --music ~/Music/Albums --state ~/.tape-deck-server
```

Or with plain Python: `pip install -r requirements.txt`, then `python3 tape_deck_server.py --music … --state …`. Without the libraries it still runs, with titles from the file names and no covers.

## Laying out the music

One folder per artist, with a folder per album inside it:

```
Music/
  Artist/
    Album/
      01 First Track.m4a
      02 Second Track.m4a
      cover.jpg
    Another Album/
      CD1/ …              discs in their own folders stay one album
    A Single.mp3          a track on its own, straight in the artist's folder
```

The folders give the artist and the album; the tags give each track's title and length. A cover is `cover.jpg` (or `folder`, `front`, `albumart`, or the folder's only picture), else the art in the tracks. MP3, AAC and ALAC (`.m4a`), FLAC, WAV and AIFF are listed; Ogg and Opus aren't, since the app can't play them. Hidden files and folders are left out.

The folder is read again every 15 minutes (`--rescan`), so albums copied in show up by themselves. Tags and covers are kept in the state folder and read only for files that are new or changed.

## HTTPS

The app connects over HTTPS. Put the server behind something that gives it a certificate, such as [Caddy](https://caddyserver.com), [Tailscale Serve](https://tailscale.com/kb/1312/serve), nginx or Traefik, and type that address into the app.

## Passwords and favourites

| Setting | What it does |
|---|---|
| No password | Anyone who can reach the server can play the music, and everyone shares one set of favourites. Fine on a network that's yours alone. |
| `TAPE_PASSWORD` | The owner's password. The app asks for it once, keeps the server's cookie for 30 days, and asks again after. Signed in with it, you have the favourites and the albums listened to (the app's Recent tab), kept in step between your devices. |
| `TAPE_GUEST_PASSWORD` | Optional: a second password, for friends. A guest plays the whole library but can't read or change your favourites; theirs stay on their own devices (and in their own iCloud, in the app). |

Five wrong passwords from one address lock it out for five minutes, and 25 from everyone together lock everyone out for five. Changing a password signs everyone out.

**Your own network.** Run as a plain script, a direct connection from a private address (your LAN, or a Tailscale address) is taken as the owner and isn't asked. Behind a reverse proxy every request seems to come from the proxy, so a forwarded request is always asked. In Docker everyone is asked, since a published port can make any caller look local; with `network_mode: host`, `TAPE_HOME_NETWORKS=private` turns it back on. `TAPE_HOME_NETWORKS` also takes `none`, or your own list of CIDRs.

**Behind Tailscale Serve**, `TAPE_TAILSCALE=1` takes a named tailnet user (Tailscale's `Tailscale-User-Login` header, which it sets and a client can't) as the owner. Only use it behind Tailscale Serve: another proxy might pass that header on from anyone.

## Settings

Each can be given on the command line or in the environment.

| Option | Environment | Default |
|---|---|---|
| `--music` | `TAPE_MUSIC` | `/music` |
| `--state` | `TAPE_STATE` | `/state` |
| `--port` | `TAPE_PORT` | `8080` |
| `--bind` | `TAPE_BIND` | `0.0.0.0` |
| `--password` | `TAPE_PASSWORD` | none (better in the environment, out of the process list) |
| `--guest-password` | `TAPE_GUEST_PASSWORD` | none |
| `--home-networks` | `TAPE_HOME_NETWORKS` | `private`; `none` in the Docker image |
| `--tailscale` | `TAPE_TAILSCALE=1` | off |
| `--rescan` | `TAPE_RESCAN_MINUTES` | `15`; `0` reads the folder once |
| `--verbose` | `TAPE_VERBOSE=1` | off: log every request |

## What it serves

Only what the app asks for: `/library.html` (the catalogue, as two lines of JSON, `const TRACKS = …;` and `const COVERS = …;`, gzipped and with an ETag, so an unchanged library costs nothing to check), `/Music/…` (the audio, with byte ranges, so a dropped download carries on where it stopped), `/.covers/…` (160-pixel JPEGs), `/favs`, `/login`, `/logout` and `/health`. Nothing outside the music folder, and nothing hidden in it.

## Checks

`python3 tests/test_server.py` runs the server against a folder of generated tones and makes the requests the app makes: the catalogue as the app parses it, ranges and resumes, paths that mustn't be served, the passwords, the guest kept from the favourites, a forged or promoted cookie, the lockout, and a new album picked up by the next scan. The same runs on GitHub with and without the two libraries, and the Docker image is built and started.

## Licence

MIT. See [LICENSE](../LICENSE).
