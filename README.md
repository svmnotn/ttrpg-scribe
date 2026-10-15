# TTRPG Scribe

Live transcription of voice calls for TTRPG session notes. Runs 100% locally
on your NVIDIA GPU — free, no cloud, and **no audio is ever saved to disk**
(buffers live in RAM, get transcribed, then are freed).

```powershell
ttrpg-scribe "C:\Notes\CurseOfStrahd"
```

That creates `C:\Notes\CurseOfStrahd\2026-10-15.txt` (ISO 8601) and prints every
line to the console too — live feedback + long-term notes. A second session in
one day appends under a `## Session started …` header; nothing is overwritten.

English-only: the recognizer is locked to English (faster + more accurate than
auto-detect when the language is known).

## Modes

| Mode | Audio | Speaker labels | Setup |
|---|---|---|---|
| `sidecar` | Rust songbird receive | Real server display names | Build `bot-rust` once (+ folder mapped for auto-join) |
| `loopback` | Local capture (Stream Mix) | `SPEAKER_00`, `SPEAKER_01`, … | None |
| `hybrid` (default, no build needed) | Local capture + presence names | Real server display names | Folder mapped |
| `bot` | Bot voice receive | Real server display names | Invite the bot once (below) — currently degraded upstream (see below) |

```powershell
# Mapped folder: hybrid — local audio + server names, no flags needed
ttrpg-scribe "C:\Notes\CurseOfStrahd"

# Just local audio with diarized labels (no voice bot needed at all)
ttrpg-scribe "C:\Notes\CurseOfStrahd" --mode loopback

# Legacy bot-record mode (per-user voice streams; degraded by DAVE, see below)
ttrpg-scribe "C:\Notes\CurseOfStrahd" --mode bot --channel "Game Night"

# One-off override: capture something else this time only
ttrpg-scribe "C:\Notes\CurseOfStrahd" --input "Chat"

# See exact device names (--input/--device/--mic match case-insensitive substrings)
ttrpg-scribe --list-devices
```

## Defaults are configurable

Built-in default: Stream Mix capture, hybrid mode. Everything — defaults,
bot token, folder mappings — lives in one file, `%USERPROFILE%\.ttrpg-scribe.json`
(see `config.example.json`):

```powershell
ttrpg-scribe --show-config                  # see current defaults
ttrpg-scribe --set-default input "Chat"      # change the default capture
ttrpg-scribe --set-default mode bot          # default to bot mode
ttrpg-scribe --set-default input ""          # clear back to speaker+mic loopback
```

CLI flags always override the file for one run. If the configured source isn't
found (device unplugged/renamed), you get a numbered list and pick one on the
spot — with an offer to save it as the new default — instead of an error.

## Folder → guild/channel mapping (all voice modes)

Skip `--guild`/`--channel` every run by pinning them to the folder. Several
folders may share one target. A mapped folder auto-selects hybrid mode —
no `--mode` flag needed (pass `--mode loopback` to override for one run):

```powershell
ttrpg-scribe --map "C:\Notes\CurseOfStrahd" --guild "My Server" --channel "Game Night"
ttrpg-scribe --map "C:\Notes\OneShot" --guild "My Server" --channel "Game Night"
ttrpg-scribe "C:\Notes\CurseOfStrahd"   # joins from the mapping
ttrpg-scribe --maps                                 # list them
ttrpg-scribe --unmap "C:\Notes\OneShot"             # forget one
```

Precedence: `--mode`/`--guild`/`--channel` flags > folder mapping (implies hybrid) > config defaults.

`--set-default mode sidecar` pins the sidecar instead, so a bare folder on
any machine uses remote receive without a mapping.

Use `sidecar` for the best quality (per-user remote audio, no local capture
needed). `hybrid` is the out-of-the-box default: clean local audio plus
presence names with nothing to build. Use `bot` (record) only as a fallback. Use
`loopback`/`--device` when you play on someone else's server. Expect each
line ~5–20 s after the words are spoken (utterance buffering + GPU).

## Discord backend setup (one time, ~10 min)

The first supported voice backend is Discord; others can plug in via
[SIDECAR_API.md](SIDECAR_API.md).

1. <https://discord.com/developers> → New Application → Bot → Reset Token → copy it.
2. OAuth2 → URL Generator → scopes `bot` + `applications.commands` →
   permissions `View Channels`, `Connect`, `Speak` → open the URL, invite it.
   (Invite once; permission changes later need a re-invite, intent changes
   below do not — just restart the scribe.)
3. Bot → Privileged Gateway Intents → enable **Server Members Intent** →
   Save. This is what gives transcripts server nicknames instead of
   `User 123…`. (Small private servers need no verification for this.)
   If you skip it, the scribe refuses to start and tells you exactly this.
4. Opus voice library (only needed for `--mode bot`): already installed
   (`opus.dll` in the project folder, wired up via `"opus_dll_path"`).
   Skip this step unless the bot complains about Opus at startup — sidecar,
   hybrid, and loopback modes never touch it. The DLL is redistributed
   under its own BSD license; see THIRD-PARTY-NOTICES.md.
5. Save the token as `"discord_token"` in `%USERPROFILE%\.ttrpg-scribe.json`
   (see `config.example.json`) — easiest with:
   `code $env:USERPROFILE\.ttrpg-scribe.json`
   and optionally the usual table so flags aren't needed:
   `ttrpg-scribe --set-default guild "My Server"` and
   `ttrpg-scribe --set-default channel "Game Night"` (one run per key).
6. Pin the folder once:
   `ttrpg-scribe --map "C:\Notes\MyCampaign" --guild "My Server" --channel "Game Night"`.
   Mapped folders auto-join (sidecar mode) — no flags needed afterwards.
7. Just run it — the bot joins the channel itself and starts transcribing.
   Nothing to type in chat. Ctrl+C in this terminal stops it, flushes the
   last few seconds of speech to the file, and leaves the voice channel.

## Sidecar mode (Rust songbird receive — best quality)

`--mode sidecar` replaces Pycord voice receive with a small Rust recorder
(`bot-rust/`, songbird 0.6 + DAVE) that streams per-user PCM + display names
into the same Python transcribe worker. In testing it transcribes near
verbatim where the Pycord path garbles. Build once — needs the Rust
toolchain plus MSVC build tools and cmake (e.g.
`winget install -e --id Kitware.CMake`):

```powershell
cd V:\ttrpg-scribe\bot-rust
cargo build --release
ttrpg-scribe "C:\Notes\CurseOfStrahd" --mode sidecar
```

Adding a capture bot for another voice app? The producer contract
(process framing, audio format, names, lifecycle) is specified in
[SIDECAR_API.md](SIDECAR_API.md) — implement the pipe, keep the worker.

## Troubleshooting

- Wrong mode (bot never joins, only `[cap] direct input…` lines): you ran in
  `loopback` mode. Mapped folders now auto-select `hybrid` and print
  `[mode] hybrid (folder mapping → …)` at startup — if you see `[mode] loopback`
  instead, the folder path doesn't match the mapping (check
  `ttrpg-scribe --maps`, paths are case-insensitive but must resolve to the
  same folder) or you passed `--mode loopback`.
- `ttrpg-scribe` not recognized in a new terminal: the installer puts it in
  `Python312\Scripts`, which is on PATH — open a fresh terminal after install.
- Loopback captures silence: check `ttrpg-scribe --list-devices`, confirm the
  `Stream Mix` / speaker device is the one your voice app plays through.
- Bot joins but stops with `OpusError('corrupted stream')`: Discord's DAVE
  encryption vs Pycord (#3139). The bot applies a local backport of the
  known fix at startup (`[bot] DAVE workaround applied …`) and retries per
  packet. If you still get silence or `Unknown (ssrc-…)` labels, prefer
  `sidecar` (or `hybrid`/`loopback`) over Pycord receive.
- `module 'discord' has no attribute 'sinks'`: you have stock `discord.py`
  installed. Bot mode needs the Pycord fork: `pip uninstall -y discord.py`
  then `pip install -e .` (which pulls `py-cord[voice]`).
- `Opus not loaded`: DLL path wrong — set `"opus_dll_path"` to the full path.

## Reinstall (fresh machine or broken Python env)

```powershell
cd V:\ttrpg-scribe
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
pip install -e .                  # loopback + sidecar modes (no Discord library needed)
pip install -e ".[discord]"       # additionally: bot + hybrid modes (Pycord voice)
```

Discord capture is optional: the base install covers loopback (local audio)
and sidecar (the sidecar is a separate Rust binary, needs `cargo build`
once — see above). Only `bot` and `hybrid` need the `[discord]` extra.

Any recent NVIDIA GPU works (8 GB+ VRAM comfortably runs
`large-v3-turbo` in float16). RTX 50xx (Blackwell) cards specifically need
the cu128 torch build — a plain `pip install torch` may give you a CPU or
older-CUDA build that can't see the GPU. The `--model` default can be lowered
(`small`, `medium`) on weaker cards. Models re-download on first run
(~2 GB, then offline).

## About & contributing

This repo was built entirely with `Muse Spark 1.3 Free`, and the owner
has not read the code — expect rough edges, and please file issues with
full console output. Discord is the first supported backend, but PRs adding
capture bots for other voice-chat apps are welcome. Released into the public
domain (see LICENSE — third-party `opus.dll` excepted, see
THIRD-PARTY-NOTICES.md).
