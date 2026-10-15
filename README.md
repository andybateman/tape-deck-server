# Tape Deck Server

<!-- Project metadata: read by the project tools, hidden when the README is rendered.
**Keywords:** Tape Deck app, music server, self-hosted music, Docker image, GHCR, Python HTTP server, byte ranges, owner and guest passwords, favourites sync, album covers, mutagen tags, Pillow thumbnails, Tailscale Serve, Caddy HTTPS, MIT licence, open source release, GitHub Actions, 20261015
**Status:** Active
**Start Date:** 2026-10-15
**Last Updated:** 2026-10-15
**Context:** @personal
**Repository:** https://github.com/andybateman/tape-deck-server (public)
-->

## Overview

A small, headless, open-source server that lets anyone serve their own music folder to the Tape Deck app, so testers and others can use the app with their own library rather than Andy's. It serves only what the app reads: the catalogue (`library.html` with its `const TRACKS` and `const COVERS` lines), the audio with ranges, covers, the password and the owner's favourites. The web player itself stays in the private Tape Deck repository (Andy, 2026-10-15: app-only server, its own public repo, Docker and a plain script, MIT).

The public README, what people read on GitHub, is [.github/README.md](.github/README.md).

## Goals

- One Python file, standard library plus mutagen and Pillow (optional), run by `uv run` or `python3`.
- A Docker image on GitHub Container Registry, amd64 and arm64, built and released by GitHub Actions on a `v*` tag.
- Safe by default: an owner's password and an optional guest password; a guest never reads or changes the owner's favourites; nothing outside the music folder is served.

## Files

| File | Purpose |
|---|---|
| `tape_deck_server.py` | The server |
| `tests/test_server.py` | Its checks, against generated tones, as the app makes its requests |
| `Dockerfile`, `compose.yaml`, `requirements.txt` | The image and how to run it |
| `.github/README.md` | The public README |
| `.github/workflows/ci.yml` | Tests (Python 3.10 and 3.13, with and without the libraries) and the image built and started |
| `.github/workflows/release.yml` | On a `v*` tag: the image to `ghcr.io/andybateman/tape-deck-server`, and a GitHub release |
| `CLAUDE.md` | Working rules |

## Next Steps

1. Link it from the app's help and the website's Tape Deck pages, and from the TestFlight notes.

## Known Issues

- The Docker actions in `release.yml` (`docker/*-action`) still run on Node 20, which GitHub warns about (as at 2026-10-15); a newer major of each when they're out.

- The app's page keeps favourites in step through `/favs`, which a guest is refused; the app's iCloud carries a guest's between their own devices instead.
- Not tried against a library of real size yet (as at 2026-10-15): the first scan reads every file's tags, and the catalogue answers 503 until it's done.
