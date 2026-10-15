# Sidecar capture API — bring your own voice app

`ttrpg-scribe --mode sidecar` splits the job in two: a **producer** (any
program, any language) captures per-user voice audio from some voice-chat
app and streams it over stdout; the Python side transcribes it with the
shared worker (VAD → faster-whisper → notes file). The reference producer
is `bot-rust/` (Discord via Songbird). This document specifies the producer
contract so you can add Teamspeak / Mumble / Zoom / game-chat / etc.
without touching the transcription pipeline.

## Architecture

```
voice app ---> [producer child process] --framed stdout--> [Python reader]
                                                              |
                                                     UtterancePump
                                                      (transport buffering
                                                       + per-user VAD)
                                                              |
                                                     transcribe worker
                                                      (prompt chaining,
                                                       faster-whisper GPU)
                                                              |
                                                     notes file + console
```

The producer owns everything app-specific: login, joining, user lists,
decoding, display names. Python owns everything app-agnostic: buffering,
segmentation, transcription, output. The only shared surface is the frame
protocol below.

## Process contract

* Python spawns the producer with stdout piped (binary), stdin piped and
  held open, stderr inherited (your logs appear on the console — use it).
* **Stdout is binary.** Never print logs to stdout; they corrupt the stream.
* Secrets (tokens) travel via **environment variables, never argv** (argv
  leaks into process lists). Name yours clearly, e.g. `DISCORD_TOKEN`.
* **Shutdown:** Python closes stdin and terminates the child on exit. Exit
  promptly when stdin hits EOF (your own death-watch), so no orphaned bots
  linger in voice channels. Exit codes: `0` clean shutdown, `2` config
  error (bad args, unknown guild/channel, auth failure).
* The producer decides its own CLI flags; Python passes guild/channel (or
  equivalents) as `--guild`/`--channel` name substrings by convention.

## Frame protocol (producer → Python)

Little-endian, no alignment padding. Python resyncs (drops the stream) on
absurd lengths and ends cleanly on truncation — design for crash-safety,
not checksums.

| Frame | Bytes |
|---|---|
| Name | `b'N'` (1) + `uid:u64` (8) + `namelen:u32` (4) + name UTF-8 (`namelen`) |
| Audio | `b'A'` (1) + `uid:u64` (8) + `pcmlen:u32` (4) + PCM s16le (`pcmlen`) |

* `uid` is any stable per-speaker integer (Discord snowflake, Mumble
  session id, …). Unknown tags are **ignored**, so the protocol is
  extensible — reserve new single-byte tags as needed.
* Lengths above 10,000,000 bytes are treated as corruption: Python breaks
  the stream rather than allocating.
* Short reads (EOF mid-frame) end the session cleanly; partial frames are
  discarded.

## Audio format

* **48 kHz stereo signed-16-bit little-endian PCM**, matching Discord's
  native voice format. The pump resamples to 16 kHz mono internally.
* Chunking is free: send natural network frames (e.g. 20 ms ticks) or
  larger batches — the pump re-batches per user to its transport size
  (default 2.0 s) before VAD, so transport boundaries never cut utterances.
* On startup, log one line to stderr with the per-tick sample count
  (e.g. `first voice tick: 1 speakers, 1920 samples/user`). 1920 =
  20 ms stereo as expected; anything else means the format assumption is
  wrong and must be fixed on one side or the other.
* Silence is fine and expected (pauses, mute): it flows through, VAD
  rejects it, nothing is transcribed, nothing is stored.

## Names

* Send an `N` frame for every occupant at join, re-send periodically
  (30 s is plenty) for late joiners and nickname changes, and any time a
  new speaker appears. Re-sends are idempotent; Python announces each
  mapping once.
* Prefer server nicknames over global usernames where the app has both.
* Speakers with no name yet are labeled `User <uid>` until an `N` frame
  arrives — transcription is never blocked waiting for a name.

## What Python guarantees back

* **Order**: frames are processed FIFO; utterances for one speaker come
  out in arrival order.
* **Segmentation**: one persistent VAD segmenter per speaker (WebRTC VAD,
  ~0.8 s trailing silence, ~10 s force-split, energy-gated absolute cap
  so loud non-speech still surfaces). Utterances below ~0.5 s are dropped.
* **Continuity**: the previous utterance (up to 224 chars) is passed as
  Whisper's prompt, unless it looks like a hallucination loop — then it is
  skipped as a prompt (but still written to the notes).
* **Liveness**: expect each line ~5–20 s after the words are spoken.
* **Shutdown**: trailing speech is flushed through transcription before
  the process exits; the parent reports chunk/speaker/utterance totals.

## Minimal producer checklist

1. Join the target voice channel and stay joined (reconnect with backoff).
2. Map app-level speaker identity → stable integer `uid` (SSRC maps,
   session ids, …) the same way Songbird maps SSRC→UserId on speaking
   events — audio packets alone usually don't identify users.
3. Emit `N` frames (join snapshot + periodic refresh), `A` frames per
   tick. Silence needs no special handling.
4. Death-watch stdin; exit 0 on EOF, 2 on config errors; logs on stderr.
5. Verify with the Python harness: pipe scripted frames into
   `ttrpg_scribe.bot._forward_sidecar_frames` (see the round-trip tests
   in the dev log) and through `UtterancePump` with synthetic voiced
   audio before going live.

Reference implementation: `bot-rust/src/main.rs` (~350 lines, including
the DAVE-capable receive path that motivated this split).
