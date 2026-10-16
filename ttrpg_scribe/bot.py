"""Voice-bot capture mode (Discord backend): per-user audio streams → perfect speaker names.

Each person in voice is a separate Opus stream, so no diarization is needed —
the transcript is labeled with real server display names. This is the most
accurate mode; use it on servers where you (or your GM) can invite the bot.

Requires the Pycord fork (``py-cord[voice]``) — stock ``discord.py`` has no
``discord.sinks`` module and cannot receive audio.

No typing in Discord: tell it where to join and it starts on its own.

  ttrpg-scribe "C:\\Notes\\X" --mode bot --guild "My Server" --channel "Game Night"

It joins that voice channel immediately and transcribes until you kill it
(Ctrl+C in this terminal) — killing it stops the recording, flushes the last
utterances, and leaves the voice channel. The notes file stays on disk.

Setup (one time):
  1. https://discord.com/developers → New Application → Bot → copy token.
  2. OAuth2 → URL Generator → scopes: bot, applications.commands →
     permissions: View Channels, Connect, Speak → open the URL to invite it.
  3. Windows needs Opus: opus.dll is shipped in the project folder and used
     automatically (or set "opus_dll_path" in the config for a custom path).
  4. Save the token in %USERPROFILE%\\.ttrpg-scribe.json as "discord_token"
     (see config.example.json), and optionally persist guild/channel with
     --set-default.

Audio is decoded to RAM, transcribed, and freed — never written to disk.

NOTE (2026): Discord's DAVE encryption broke Pycord voice reception upstream
(#3139; stock 2.8.1 dies with ``OpusError('corrupted stream')``). This bot
applies a local, version-gated backport of the known fix at startup (watch
for ``[bot] DAVE workaround applied …``): correct DAVE→Opus decrypt order,
no decrypt of already-decoded PCM, per-packet error isolation, plus
sink-API shims. Unproven against live DAVE audio — if it joins yet yields
silence or "Unknown (ssrc-…)" labels, use loopback mode (GoXLR Stream Mix),
which works today.
"""
from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, cast

import numpy as np

from ttrpg_scribe.loopback import VadChunker
from ttrpg_scribe.transcribe import _prompt_worthy

if TYPE_CHECKING:
    from discord.voice import VoiceClient

    from ttrpg_scribe.transcribe import StreamingTranscriber
    from ttrpg_scribe.writer import NotesWriter


def _require_discord() -> None:
    """Friendly error when the optional voice-bot libraries are missing.

    Loopback/sidecar modes never touch them; bot/hybrid modes do.
    """
    try:
        import discord  # noqa: F401
    except ImportError:
        raise SystemExit(
            "[bot] bot/hybrid modes need the Discord voice libraries, which are not installed.\n"
            "Install them: pip install -e \".[discord]\" "
            "(or: pip install \"py-cord[voice]>=2.5\")"
        )


def _match_one(wanted: str, options: Sequence[tuple[str, Any]], kind: str) -> Any:
    """Match by numeric ID or case-insensitive name substring."""
    if wanted.isdigit():
        for name, obj in options:
            if str(getattr(obj, "id", "")) == wanted:
                return obj
    hits = [(n, o) for n, o in options if wanted.lower() in n.lower()]
    if len(hits) == 1:
        return hits[0][1]
    print(f"[bot] no unique {kind} matching {wanted!r}.")
    for name, obj in options:
        print(f"  {name}  (id {getattr(obj, 'id', '?')})")
    raise SystemExit(f"[bot] specify --{kind} with an exact name or id from the list above.")


def _unpack_voice_packet(data: Any, user: Any = None) -> tuple[Any, str, bytes, bool] | None:
    """Normalize one audio chunk from Pycord voice receive.

    py-cord ≥2.7 passes a ``VoiceData`` (``.pcm`` bytes + ``.source`` user);
    older versions pass raw PCM bytes with the user as 2nd arg. Returns
    ``(key, display_name, pcm_bytes, needs_resolve)`` or ``None`` for
    unusable input. ``needs_resolve`` is True for ``User <id>``/SSRC
    fallbacks (member-cache miss) — callers should try a REST lookup.
    Unknown users key by packet SSRC so distinct speakers never merge
    into one buffer.
    """
    raw = getattr(data, "pcm", None)
    if raw is None:
        raw = data
    if not isinstance(raw, (bytes, bytearray)) or len(raw) == 0:
        return None
    src = user if user is not None else getattr(data, "source", None)
    uid = getattr(src, "id", None)
    if uid is None:
        ssrc = getattr(getattr(data, "packet", None), "ssrc", None)
        uid = f"ssrc-{ssrc}" if ssrc is not None else "unknown"
        return uid, f"Unknown ({uid})", bytes(raw), True
    name = getattr(src, "display_name", None) or getattr(src, "name", None)
    if name is None:
        return uid, f"User {uid}", bytes(raw), True
    return uid, name, bytes(raw), False


def _resolve_guild_name(guild: Any, loop: Any, uid: Any) -> str | None:
    """Server display name (nickname) for a user id.

    Member cache first, then a single-member REST fetch — no privileged
    intents needed. Returns None when unresolvable; caller keeps fallback.
    """
    if not isinstance(uid, int):
        return None
    try:
        member = guild.get_member(uid)
    except Exception:
        member = None
    if member is not None:
        return getattr(member, "display_name", None)
    fetch = getattr(guild, "fetch_member", None)
    if fetch is None or loop is None:
        print(f"[bot] name lookup unavailable (fetch={fetch is not None}, loop={loop is not None})",
              flush=True)
        return None
    try:
        member = asyncio.run_coroutine_threadsafe(fetch(uid), loop).result(timeout=5)
    except Exception as e:
        print(f"[bot] name lookup failed for {uid}: {e}", flush=True)
        return None
    return getattr(member, "display_name", None)


class SpeakerTimeline:
    """Who spoke when, from Discord speaking events (signaling — no audio).

    A presence-only bot records every speaking event with a monotonic
    timestamp; the capture side asks who owned an utterance span. This pairs
    clean local audio with true Discord names without touching the broken
    voice-receive stack. Thread-safe.
    """

    def __init__(self, holdover: float = 3.0, memory: float = 120.0) -> None:
        self._holdover = holdover
        self._memory = memory
        self._lock = threading.Lock()
        self._ops: list[tuple[float, int, Any]] = []  # (monotonic, seq, uid)
        self._seq = 0
        self._names: dict[Any, str] = {}

    def remember(self, uid: Any, display_name: str | None) -> None:
        """Preload a known name without implying speech."""
        if display_name:
            with self._lock:
                self._names[uid] = display_name

    def note(self, uid: Any, display_name: str | None) -> None:
        """Record one speaking event."""
        now = time.monotonic()
        with self._lock:
            if display_name:
                self._names[uid] = display_name
            self._seq += 1
            self._ops.append((now, self._seq, uid))
            cutoff = now - self._memory
            self._ops = [(t, s, u) for t, s, u in self._ops if t >= cutoff]

    def attribute(self, end: float, duration: float) -> str | None:
        """Display name of the dominant speaker over
        [end-duration-holdover, end]. Ties break toward the most recent
        event; None when nobody spoke there."""
        start = end - duration - self._holdover
        with self._lock:
            counts: dict[Any, int] = {}
            last: dict[Any, tuple[float, int]] = {}
            for t, s, u in self._ops:
                if t >= start:
                    counts[u] = counts.get(u, 0) + 1
                    last[u] = (t, s)
            if not counts:
                return None
            winner = max(counts, key=lambda u: (counts[u], last[u]))
            return self._names.get(winner)


async def _resolve_voice_target(bot: Any, guild_want: str | None,
                                channel_want: str | None) -> tuple[Any, Any] | None:
    """Pick (guild, voice channel): flags win, else singletons.

    Prints guidance and closes the bot when the target is ambiguous.
    Shared by record and presence modes.
    """
    guilds = [(g.name, g) for g in bot.guilds]
    if not guilds:
        raise SystemExit("[bot] not a member of any server — invite it first (see README).")
    if guild_want:
        target_guild = _match_one(guild_want, guilds, "guild")
    elif len(guilds) == 1:
        target_guild = guilds[0][1]
    else:
        target_guild = None
    if target_guild is None:
        print("[bot] member of multiple servers — specify one with --guild:")
        for name, g in guilds:
            print(f"  {name}  (id {g.id})")
        await bot.close()
        return None
    voice_channels = [(c.name, c) for c in target_guild.voice_channels]
    if not channel_want:
        print("[bot] specify a voice channel with --channel (or --set-default channel):")
        for name, c in voice_channels:
            print(f"  {name}  (id {c.id})")
        await bot.close()
        return None
    return target_guild, _match_one(channel_want, voice_channels, "channel")


_WORKAROUND_STATS: dict[str, int] = {"dave_silence": 0, "fake_plc": 0, "skipped": 0,
                                            "rtp_in": 0, "rtcp_in": 0, "jb_drops": 0}


def _channel_occupants(guild: Any, channel_id: Any, me: Any) -> list[int]:
    """User ids currently in a voice channel, from gateway voice states.

    Works without privileged intents, but only sees occupants whose state
    events have arrived — often nobody yet at the instant we join. The
    per-utterance REST lookup remains the backstop.
    """
    try:
        states = guild._voice_states
    except AttributeError:
        return []
    if not isinstance(states, dict):
        return []
    out: list[int] = []
    for uid, vs in states.items():
        ch = getattr(vs, "channel", None)
        if getattr(ch, "id", None) == channel_id and uid != me and isinstance(uid, int):
            out.append(uid)
    return out


def _apply_pycord_dave_workaround() -> str:
    """Backport DAVE voice-receive fixes to py-cord 2.8.1 at runtime.

    Upstream 2.8.1 cannot record DAVE-encrypted voice (Pycord #3139): it dies
    with ``OpusError('corrupted stream')`` on the first audio. Root causes
    (logic reviewed from vito1317/pycord@5a95f98; applied here so no fork is
    installed):
      1. ``PacketDecryptor.decrypt_rtp`` strips extension bytes from
         ``dave.decrypt()`` output, but that output is already raw Opus.
      2. ``PacketDecoder._decode_packet`` runs ``dave.decrypt`` over
         already-decoded PCM when the user "can passthrough".
      3. ``PacketRouter._do_run`` lets one undecodable packet kill recording.
    Each patch applies only if the installed source still matches the
    verified 2.8.x layout; otherwise it is skipped. Returns a status line.
    """
    import inspect

    import discord

    ver = getattr(discord, "__version__", "?")
    if not ver.startswith("2.8."):
        return f"DAVE workaround skipped (py-cord {ver} layout unverified)"

    from discord.opus import OpusError, PacketDecoder
    from discord.voice.receive import reader as recv_reader

    applied: list[str] = []

    # --- 1. decrypt_rtp: don't strip extension bytes from raw Opus ---
    orig_decrypt = recv_reader.PacketDecryptor.decrypt_rtp
    if "update_extended_header(decrypted_audio)" in inspect.getsource(orig_decrypt):
        def _fixed_decrypt_rtp(self: Any, packet: Any) -> bytes:
            import davey
            from discord.voice.packets.core import OPUS_SILENCE

            state = self.client._connection
            dave = state.dave_session
            raw_payload = self._decryptor_rtp(packet)
            if dave is not None and dave.ready:
                uid = state.ssrc_user_map.get(packet.ssrc)
                if not uid:
                    # Speaking-event mapping can lag the first packets: try
                    # every DAVE user until one decrypts, then remember it.
                    for cand in list(getattr(dave, "get_user_ids", lambda: [])()):
                        try:
                            raw_payload = dave.decrypt(cand, davey.MediaType.audio, raw_payload)
                        except Exception:
                            continue
                        add_ssrc = getattr(self.client, "_add_ssrc", None)
                        if add_ssrc is not None:
                            try:
                                add_ssrc(cand, packet.ssrc)
                            except Exception:
                                pass
                        uid = cand
                        break
                if uid:
                    try:
                        # dave.decrypt returns raw Opus; extension bytes were
                        # already stripped by _decryptor_rtp above.
                        packet.decrypted_data = dave.decrypt(uid, davey.MediaType.audio, raw_payload)
                    except Exception:
                        _WORKAROUND_STATS["dave_silence"] += 1
                        packet.decrypted_data = OPUS_SILENCE
            return packet.decrypted_data

        _fixed_decrypt_rtp.__name__ = "decrypt_rtp"
        recv_reader.PacketDecryptor.decrypt_rtp = _fixed_decrypt_rtp  # type: ignore[method-assign]
        applied.append("decrypt_rtp")

    # --- 2. _decode_packet: never DAVE-decrypt already-decoded PCM ---
    # NB: this method returns a (packet, pcm) TUPLE (unpacked by
    # _process_packet); member resolution + VoiceData live in _process_packet.
    orig_decode = PacketDecoder._decode_packet
    src = inspect.getsource(orig_decode)
    if "can_passthrough" in src and "return packet, pcm" in src:
        def _fixed_decode_packet(self: Any, packet: Any) -> tuple[Any, bytes]:
            assert self._decoder is not None
            assert self.sink.client
            if type(packet).__name__ == "FakePacket":
                _WORKAROUND_STATS["fake_plc"] += 1  # concealment: real audio was lost
            other_code = True
            if packet:
                other_code = False
                pcm = self._decoder.decode(packet.decrypted_data, fec=False)
            if other_code:
                next_packet = self._buffer.peek_next()
                if next_packet is not None:
                    pcm = self._decoder.decode(next_packet.decrypted_data, fec=True)
                else:
                    pcm = self._decoder.decode(None, fec=False)
            # NOTE: upstream's trailing dave.decrypt() over decoded PCM is
            # intentionally omitted — that call corrupts the audio stream.
            return packet, pcm

        _fixed_decode_packet.__name__ = "_decode_packet"
        PacketDecoder._decode_packet = _fixed_decode_packet  # type: ignore[method-assign]
        applied.append("decode_packet")

    # --- 3. pop_data: skip one bad packet instead of killing the recording ---
    orig_pop = PacketDecoder.pop_data
    if not getattr(orig_pop, "_ttrpg_skip_guard", False):
        warned = False

        def _safe_pop_data(self: Any, *, timeout: float = 0) -> Any:
            nonlocal warned
            try:
                return orig_pop(self, timeout=timeout)
            except (OpusError, AssertionError) as exc:
                _WORKAROUND_STATS["skipped"] += 1
                if not warned:
                    warned = True
                    print(f"[bot] skipping undecodable voice packet(s) ({exc!r})", flush=True)
                return None

        _safe_pop_data.__name__ = "pop_data"
        _safe_pop_data._ttrpg_skip_guard = True  # type: ignore[attr-defined]
        PacketDecoder.pop_data = _safe_pop_data  # type: ignore[method-assign]
        applied.append("pop_data")

    # --- 4. feed counters: packets arriving at the router vs transcribed.
    # Opus voice runs at ~50 packets/s; if rtp_in crawls while audio is loud,
    # packets die before pycord (kernel drops / DTX / sender) — not in it.
    from discord.voice.receive.router import PacketRouter

    for _meth, _key in (("feed_rtp", "rtp_in"), ("feed_rtcp", "rtcp_in")):
        _orig = getattr(PacketRouter, _meth, None)
        if _orig is None or getattr(_orig, "_ttrpg_counted", False):
            continue

        def _counted(self: Any, packet: Any, _orig: Any = _orig, _key: str = _key) -> Any:
            _WORKAROUND_STATS[_key] += 1
            return _orig(self, packet)

        _counted.__name__ = _meth
        _counted._ttrpg_counted = True  # type: ignore[attr-defined]
        setattr(PacketRouter, _meth, _counted)
        applied.append(_key)

    # --- 5. drain per wake + deeper jitter cushion + drop accounting.
    # Upstream pops ONE packet per router wake out of a 10-packet buffer that
    # silently evicts the oldest on overflow. Any GIL/scheduling lag (e.g.
    # our transcribe worker's CPU bursts) compounds into steady loss with no
    # warning — observed ~3:1 (rtp_in vs transcribed). Drain fully per wake
    # so the router catches up in bursts instead of shedding load.
    from discord.voice.receive.router import PacketRouter as _PR
    from discord.voice.utils import buffer as _bufmod

    if "for decoder in self.waiter.items" in inspect.getsource(_PR._do_run):
        def _draining_do_run(self: Any) -> None:
            while not self._end_thread.is_set():
                self.waiter.wait()
                with self._lock:
                    for decoder in list(self.waiter.items):
                        while True:
                            data = decoder.pop_data()
                            if data is None:
                                break
                            self.sink.write(data, data.source)

        _draining_do_run.__name__ = "_do_run"
        _PR._do_run = _draining_do_run  # type: ignore[method-assign]
        applied.append("drain")

    if "max_size: int = 10," in inspect.getsource(_bufmod.JitterBuffer.__init__):
        _orig_jb_init = _bufmod.JitterBuffer.__init__

        def _roomy_init(self: Any, max_size: int = 100, *,
                        pref_size: int = 1, prefill: int = 1) -> None:
            _orig_jb_init(self, max_size=max_size, pref_size=pref_size, prefill=prefill)

        _roomy_init.__name__ = "__init__"
        _bufmod.JitterBuffer.__init__ = _roomy_init  # type: ignore[method-assign]
        applied.append("jitter100")

    if "while len(self._buffer) > self.max_size" in inspect.getsource(_bufmod.JitterBuffer._cleanup):
        _orig_cleanup = _bufmod.JitterBuffer._cleanup

        def _counted_cleanup(self: Any) -> None:
            before = len(self._buffer)
            _orig_cleanup(self)
            dropped = before - len(self._buffer)
            if dropped > 0:
                _WORKAROUND_STATS["jb_drops"] += dropped

        _counted_cleanup.__name__ = "_cleanup"
        _bufmod.JitterBuffer._cleanup = _counted_cleanup  # type: ignore[method-assign]
        applied.append("jbcount")

    if applied:
        return f"DAVE workaround applied ({', '.join(applied)}) for py-cord {ver}"
    return f"DAVE workaround skipped (py-cord {ver} already differs)"


def _levels(mono16: np.ndarray) -> tuple[float, float]:
    """RMS + peak of mono float32 audio, for receive-health telemetry."""
    if mono16.size == 0:
        return 0.0, 0.0
    return float(np.sqrt(np.mean(mono16 * mono16))), float(np.max(np.abs(mono16)))


def _resample_48stereo_to_16mono(raw: bytes) -> np.ndarray | None:
    """48 kHz stereo s16le → 16 kHz mono float32. Returns None on garbage."""
    from scipy.signal import resample_poly

    try:
        stereo = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    except ValueError:
        return None
    if stereo.size < 4:
        return None
    stereo = stereo[: (stereo.size // 2) * 2]  # whole L/R frames only
    mono48 = stereo.reshape(-1, 2).mean(axis=1)
    return resample_poly(mono48, 1, 3).astype(np.float32)  # 48k → 16k


class UtterancePump:
    """Turns per-speaker transport chunks into transcribed utterances, in order.

    Voice threads call :meth:`feed` with small PCM batches. A light VAD stage
    resamples, runs one persistent :class:`VadChunker` per speaker (utterances
    cut on real silence, never at transport boundaries) and never blocks on
    GPU, so the voice router is never starved. A second stage transcribes in
    arrival order. :meth:`close` flushes trailing speech and joins both.
    No audio is stored — buffers are freed after transcription.
    """

    def __init__(
        self,
        transcribe_fn: Callable[[Any, str, np.ndarray], None],
        transport_seconds: float = 2.0,
        name_resolver: Callable[[Any], str | None] | None = None,
        debug_log: Any = None,
        verbose: bool = False,
    ) -> None:
        self._fn = transcribe_fn
        self._resolver = name_resolver
        self._debug_log = debug_log
        self._verbose = verbose
        if self._debug_log is not None:
            try:
                from datetime import datetime
                with open(self._debug_log, "a", encoding="utf-8") as f:
                    f.write(f"\n---\n## Audio session started {datetime.now().isoformat(timespec='seconds')}\n")
            except Exception:
                pass
        # 48 kHz stereo s16le transport batches per user before handoff.
        self._flush_bytes = int(48000 * 2 * 2 * transport_seconds)
        self._bufs: dict[Any, bytearray] = {}
        self._names: dict[Any, str] = {}
        self._vad: dict[Any, VadChunker] = {}
        self._needs_resolve: set[Any] = set()
        self._resolved: set[Any] = set()
        self._nlock = threading.Lock()  # names/resolve sets: voice feed vs tx thread
        self._in_queue: queue.Queue[tuple[Any, str, bytes] | None] = queue.Queue()
        self._tx_queue: queue.Queue[tuple[Any, np.ndarray] | None] = queue.Queue()
        self.chunks = 0
        self.speakers: set[Any] = set()
        self.utterances = 0
        self._vad_thread = threading.Thread(target=self._vad_loop, daemon=True,
                                            name="ttrpg-vad-pump")
        self._tx_thread = threading.Thread(target=self._tx_loop, daemon=True,
                                           name="ttrpg-transcribe-pump")
        self._vad_thread.start()
        self._tx_thread.start()

    def feed(self, uid: Any, name: str, raw48: bytes, needs_resolve: bool = False) -> str | None:
        """Buffer one transport chunk; returns the name on first sight (for
        logging) once enough is buffered to hand to the worker."""
        with self._nlock:
            if not needs_resolve or uid not in self._resolved:
                # Never clobber a REST-resolved server name with a fallback.
                self._names[uid] = name
        buf = self._bufs.setdefault(uid, bytearray())
        buf += raw48
        self.chunks += 1
        announce: str | None = None
        if uid not in self.speakers:
            self.speakers.add(uid)
            announce = name
        if needs_resolve:
            with self._nlock:
                if uid not in self._needs_resolve:
                    print(f"[bot] tracking {name} for name lookup", flush=True)
                self._needs_resolve.add(uid)
        if len(buf) >= self._flush_bytes:
            self._in_queue.put((uid, self._names[uid], bytes(buf)))
            del self._bufs[uid]
        return announce

    def preseed(self, names: dict[Any, str]) -> None:
        """Preload known server display names (e.g. channel members at join)
        so first audio is labeled immediately — no lookup wait."""
        with self._nlock:
            for uid, name in names.items():
                if name:
                    self._names[uid] = name
                    self._resolved.add(uid)

    def close(self, timeout: float = 60.0) -> tuple[int, int, int]:
        """Flush trailing speech, join both stages.

        Returns ``(transport chunks, speakers, utterances transcribed)``.
        """
        self._in_queue.put(None)
        self._vad_thread.join(timeout=5.0)
        self._tx_thread.join(timeout=max(1.0, timeout - 5.0))
        return self.chunks, len(self.speakers), self.utterances

    def _debug(self, line: str) -> None:
        """Telemetry to the per-day debug log; console only with --debug."""
        if self._debug_log is not None:
            try:
                from datetime import datetime
                with open(self._debug_log, "a", encoding="utf-8") as f:
                    f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {line}\n")
            except Exception:
                pass
        if self._verbose:
            print(line, flush=True)

    def _maybe_resolve(self, uid: Any) -> str:
        """Server display name for a fallback-labeled speaker, once per uid."""
        with self._nlock:
            needs = uid in self._needs_resolve
            done = uid in self._resolved
            current = self._names.get(uid, str(uid))
        if not needs or done or self._resolver is None:
            return current
        real: str | None = None
        try:
            real = self._resolver(uid)
        except Exception as e:
            print(f"[bot] name lookup failed for {uid}: {e}", flush=True)
        with self._nlock:
            self._resolved.add(uid)
            if real:
                self._names[uid] = real
                print(f"[bot] {current} is {real}", flush=True)
                return real
            return self._names.get(uid, current)

    def _vad_loop(self) -> None:
        """Stage 1 (light): resample transport chunks, run persistent per-user
        VAD, hand utterances to the transcribe stage. Never blocks on GPU, so
        the voice router is never starved by transcription."""
        import time

        last_beat = time.monotonic()
        ema = 0.0
        peak_hold = 0.0
        while True:
            item = self._in_queue.get()
            if item is None:
                break
            uid, _name, raw48 = item
            mono16 = _resample_48stereo_to_16mono(raw48)
            if mono16 is None:
                continue
            rms, peak = _levels(mono16)
            ema = 0.9 * ema + 0.1 * rms
            peak_hold = max(peak_hold * 0.95, peak)
            if time.monotonic() - last_beat >= 15.0:
                last_beat = time.monotonic()
                loss = _WORKAROUND_STATS
                self._debug(f"[bot] audio: {self.chunks} chunks, {len(self.speakers)} speaker(s), "
                      f"{self.utterances} utterance(s), level rms={ema:.4f} peak={peak_hold:.3f}, "
                      f"loss=dave:{loss['dave_silence']}/plc:{loss['fake_plc']}/skip:{loss['skipped']}"
                      f"/rtp:{loss['rtp_in']}/jb:{loss['jb_drops']}")
                peak_hold = 0.0
            pcm16 = (np.clip(mono16, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
            chunker = self._vad.get(uid)
            if chunker is None:
                chunker = self._vad[uid] = VadChunker()
            for utterance in chunker.feed(pcm16):
                self._tx_queue.put((uid, utterance))
        for uid, chunker in self._vad.items():
            tail = chunker.flush()
            if tail is not None:
                self._tx_queue.put((uid, tail))
        self._tx_queue.put(None)

    def _tx_loop(self) -> None:
        """Stage 2 (slow): name resolution + GPU transcription, in order."""
        while True:
            item = self._tx_queue.get()
            if item is None:
                break
            uid, utterance = item
            self._fn(uid, self._maybe_resolve(uid), utterance)
            self.utterances += 1


def _make_transcribe_user(
    writer: NotesWriter, transcriber: StreamingTranscriber
) -> Callable[[Any, str, np.ndarray], None]:
    """Per-utterance callback with prompt chaining (shared by bot/sidecar)."""
    _last_text: dict[Any, str] = {}

    def transcribe_user(uid: Any, name: str, utterance: np.ndarray) -> None:
        # Previous utterance as Whisper prompt: names/terms carry over.
        # Garbage never becomes a prompt (it would poison the session).
        prompt = _last_text.get(uid)
        text = transcriber.transcribe(utterance, prompt=(prompt[-224:] if prompt else None))
        if text and _prompt_worthy(text):
            _last_text[uid] = text
        writer.write(name, text)

    return transcribe_user


def _forward_sidecar_frames(stream: Any, pump: UtterancePump,
                             names: dict[int, str],
                             stop: threading.Event) -> None:
    """Parse sidecar stdout frames into the pump. Ends on EOF or ``stop``.

    Frame: magic(1) + uid u64 LE + len u32 LE + body. ``N`` = display name
    (utf-8), ``A`` = 48 kHz stereo s16le PCM. Unknown tags are ignored.
    """
    while not stop.is_set():
        hdr = stream.read(13)
        if len(hdr) < 13:
            break
        tag = hdr[0:1]
        uid = int.from_bytes(hdr[1:9], "little")
        size = int.from_bytes(hdr[9:13], "little")
        if size > 10_000_000:
            print(f"[sidecar] absurd frame size {size}, resyncing", flush=True)
            break
        body = stream.read(size)
        if len(body) < size:
            break
        if tag == b"N":
            try:
                name = body.decode("utf-8", errors="replace")
            except Exception:
                continue
            if names.get(uid) != name:
                names[uid] = name
                pump.preseed({uid: name})
                print(f"[sidecar] {uid} is {name}", flush=True)
        elif tag == b"A":
            pump.feed(uid, names.get(uid, f"User {uid}"), body,
                      uid not in names)


def run_sidecar(writer: NotesWriter, transcriber: StreamingTranscriber,
                guild: str | None = None, channel: str | None = None,
                token: str | None = None, debug: bool = False) -> None:
    """Capture via the Rust songbird sidecar into the shared transcribe worker.

    The sidecar (``bot-rust/``) joins voice and streams per-user PCM + display
    names over stdout; here each frame feeds the same :class:`UtterancePump`
    + GPU worker the ``bot`` mode uses. No pycord/discord.py involved.
    """
    import os
    import subprocess

    from pathlib import Path

    token = _require_token(token)
    exe = "ttrpg-sidecar.exe" if os.name == "nt" else "ttrpg-sidecar"
    binary = Path(__file__).resolve().parent.parent / "bot-rust" / "target" / "release" / exe
    if not binary.exists():
        raise SystemExit(
            f"[sidecar] binary not found at {binary}\n"
            "Build it once: cd bot-rust && cargo build --release"
        )
    if not guild or not channel:
        raise SystemExit("[sidecar] needs --guild and --channel (or a folder mapping)")

    cmd = [str(binary), "--guild", guild, "--channel", channel]
    env = dict(os.environ, DISCORD_TOKEN=token)
    print(f"[sidecar] starting {binary.name} → guild={guild!r} channel={channel!r}", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.PIPE,
                            stderr=None, env=env)
    assert proc.stdout is not None and proc.stdin is not None

    pump = UtterancePump(_make_transcribe_user(writer, transcriber),
                         debug_log=getattr(writer, "debug_path", None),
                         verbose=debug)
    names: dict[int, str] = {}
    stop_reader = threading.Event()

    assert proc.stdout is not None
    reader = threading.Thread(target=_forward_sidecar_frames,
                              args=(proc.stdout, pump, names, stop_reader),
                              daemon=True, name="ttrpg-sidecar-reader")
    reader.start()
    try:
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop_reader.set()
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        reader.join(timeout=10)
        chunks, speakers, utterances = pump.close()
        print(f"[sidecar] done: {chunks} chunks from {speakers} speaker(s), "
              f"{utterances} utterance(s) transcribed", flush=True)


def run_bot(writer: NotesWriter, transcriber: StreamingTranscriber,
            token: str | None = None,
            guild: str | None = None, channel: str | None = None,
            debug: bool = False) -> None:
    _require_discord()
    import discord
    from discord.ext import commands

    print(f"[bot] {_apply_pycord_dave_workaround()}", flush=True)

    from ttrpg_scribe.loopback import VadChunker  # reused for per-user silence splitting

    token = _require_token(token)

    from pathlib import Path

    from ttrpg_scribe import config as cfg

    candidates: list[str] = []
    if cfg.secrets().get("opus_dll_path"):
        candidates.append(cfg.secrets()["opus_dll_path"])
    # opus.dll shipped in the project folder (V:\ttrpg-scribe\opus.dll).
    candidates.append(str(Path(__file__).resolve().parent.parent / "opus.dll"))
    candidates.append("opus.dll")
    candidates.append("libopus-0.x64.dll")
    if not discord.opus.is_loaded():
        loaded = False
        tried: list[str] = []
        for cand in candidates:
            try:
                discord.opus.load_opus(cand)
                if discord.opus.is_loaded():
                    print(f"[bot] opus loaded from {cand}", flush=True)
                    loaded = True
                    break
            except OSError:
                tried.append(cand)
        if not loaded:
            raise SystemExit(
                f"Opus not loaded (tried {', '.join(tried)}). "
                f"Set \"opus_dll_path\" in {cfg.CONFIG_PATH} to the full path of opus.dll."
            )

    transcribe_user = _make_transcribe_user(writer, transcriber)

    class TranscriptionSink(discord.sinks.Sink):
        """Thin voice-thread adapter: unpack packets, hand PCM to the pump.

        Compat shims for the py-cord ≥2.7 voice stack, whose router/decoder
        expect a newer sink API than ``discord.sinks.Sink`` provides in 2.8.1
        (``__sink_listeners__``/``walk_children``/``is_opus``), and whose
        ``AudioReader`` never sets ``sink.vc`` (the decoder asserts
        ``sink.client``). All buffering/VAD/transcription lives in the
        shared :class:`UtterancePump`.
        """

        # Router event registration (we have no listener-decorated methods).
        __sink_listeners__: list[tuple[str, str]] = []

        def __init__(
            self,
            transcribe_fn: Callable[[Any, str, np.ndarray], None],
            name_resolver: Callable[[Any], str | None] | None = None,
            debug_log: Any = None,
            verbose: bool = False,
        ) -> None:
            super().__init__()
            self._pump = UtterancePump(transcribe_fn, name_resolver=name_resolver,
                                       debug_log=debug_log, verbose=verbose)

        def walk_children(self) -> list[Any]:  # no nested sinks
            return []

        def is_opus(self) -> bool:  # we want decoded PCM, not opus frames
            return False

        def preseed(self, names: dict[Any, str]) -> None:
            """Preload known server display names (channel members at join)."""
            self._pump.preseed(names)

        # Pycord calls this on the voice thread with a VoiceData (2.7+).
        def write(self, data: Any, user: Any = None) -> None:
            unpacked = _unpack_voice_packet(data, user)
            if unpacked is None:
                return
            uid, name, raw, needs_resolve = unpacked
            announce = self._pump.feed(uid, name, raw, needs_resolve)
            if announce is not None:
                src = user if user is not None else getattr(data, "source", None)
                print(f"[bot] +audio from {announce} "
                      f"(src={type(src).__name__}, resolve={needs_resolve})", flush=True)

        def cleanup(self) -> None:  # flush remainders at shutdown (blocking)
            chunks, speakers, utterances = self._pump.close()
            print(f"[bot] sink stats: {chunks} chunks from {speakers} speaker(s), "
                  f"{utterances} utterance(s) transcribed", flush=True)

    intents = discord.Intents.default()
    intents.voice_states = True
    # Server display names need the member list: request it here AND enable
    # "Server Members Intent" in the portal (see README). Without the portal
    # toggle Discord rejects the connection — handled below with directions.
    intents.members = True
    # No other privileged intents (presence/message_content stay off), so no
    # other portal toggles are required.

    class ScribeBot(commands.Bot):
        async def close(self) -> None:
            for vc_proto in self.voice_clients:
                vc = cast("VoiceClient", vc_proto)
                try:
                    reader = getattr(vc, "_reader", None)
                    err = getattr(reader, "error", None)
                    if err is not None:
                        print(f"[bot] receive thread died: {err!r}", flush=True)
                    if vc.is_recording():
                        vc.stop_recording()  # triggers sink.cleanup() flush
                    await vc.disconnect()
                except Exception:
                    pass
            await super().close()

    bot = ScribeBot(command_prefix="!", intents=intents)
    started = threading.Event()

    @bot.event
    async def on_ready() -> None:
        if started.is_set():
            return
        started.set()
        print(f"[bot] logged in as {bot.user}", flush=True)
        resolved = await _resolve_voice_target(bot, guild, channel)
        if resolved is None:
            return
        tg_any, tc_any = resolved
        target_guild = cast("discord.Guild", tg_any)
        target_channel = cast("discord.VoiceChannel", tc_any)
        try:
            vc = cast("VoiceClient", await target_channel.connect())
        except Exception as e:
            print(f"[bot] could not join {target_channel.name}: {e}")
            await bot.close()
            return
        try:
            loop = asyncio.get_running_loop()  # robust: bot.loop may be unset
            sink = TranscriptionSink(
                transcribe_user,
                lambda uid: _resolve_guild_name(target_guild, loop, uid),
                debug_log=getattr(writer, "debug_path", None),
                verbose=debug,
            )
            sink.init(vc)  # AudioReader never sets sink.vc; decoder asserts sink.client
            try:
                me = bot.user.id if bot.user is not None else None
                present = {m.id: m.display_name for m in target_channel.members
                           if m.id != me and m.display_name}
                occupants = [u for u in _channel_occupants(target_guild, target_channel.id, me)
                             if u not in present]
                if occupants:
                    try:
                        fetched = await asyncio.wait_for(
                            asyncio.gather(*[target_guild.fetch_member(u) for u in occupants],
                                           return_exceptions=True),
                            timeout=15,
                        )
                    except Exception:
                        fetched = []
                    for m in fetched:
                        if isinstance(m, Exception) or m is None:
                            continue
                        m_any: Any = m
                        dname: Any = getattr(m_any, "display_name", None)
                        mid: Any = getattr(m_any, "id", None)
                        if mid is not None and dname:
                            present[mid] = dname
                if present:
                    sink.preseed(present)
                    print(f"[bot] preloaded {len(present)} channel name(s)", flush=True)
            except Exception as e:
                print(f"[bot] member preload failed: {e}", flush=True)

            reported = False

            def _record_finished(exc: Exception | None) -> None:
                nonlocal reported
                if exc is not None and not reported:
                    reported = True
                    import traceback
                    print("[bot] recording stopped with error:", flush=True)
                    traceback.print_exception(exc)

            vc.start_recording(sink, _record_finished)
        except Exception as e:
            print(f"[bot] could not start recording: {e}", flush=True)
            print("[bot] voice receive is broken in this Pycord release "
                  "(DAVE E2EE, Pycord issue #3139 — even stock sinks crash).", flush=True)
            print("[bot] use loopback mode instead: "
                  "ttrpg-scribe \"<folder>\" --mode loopback", flush=True)
            try:
                await vc.disconnect()
            except Exception:
                pass
            await bot.close()
            return
        print(f"[bot] 🎙️ recording '{target_channel.name}' → {writer.path.name} (Ctrl+C here to stop)", flush=True)

    print("[bot] logging in…", flush=True)
    try:
        bot.run(token)
    except discord.errors.PrivilegedIntentsRequired:
        raise SystemExit(_INTENTS_ERROR)


_INTENTS_ERROR = (
    "[bot] Discord rejected the connection: this bot requests the "
    "'Server Members Intent' (for server display names) but it is not "
    "enabled in the developer portal.\n"
    "Fix (30 s, one time): https://discord.com/developers/applications "
    "→ your app → Bot → Privileged Gateway Intents → enable "
    "'Server Members Intent' → Save. No re-invite needed — just restart "
    "ttrpg-scribe."
)


def _require_token(token: str | None) -> str:
    from ttrpg_scribe import config as cfg

    resolved = token or cfg.secrets().get("discord_token", "")
    if not resolved:
        raise SystemExit(f"Set \"discord_token\" in {cfg.CONFIG_PATH} (see config.example.json)")
    return resolved


def _bot_intents() -> Any:
    """Voice states (speaking events) + member list (display names)."""
    import discord

    intents = discord.Intents.default()
    intents.voice_states = True
    intents.members = True
    return intents


def run_presence(timeline: SpeakerTimeline, token: str | None = None,
                 guild: str | None = None, channel: str | None = None) -> None:
    """Join voice presence-only: track who speaks, record nothing.

    No audio is received or decoded, so DAVE/voice-receive breakage does not
    apply. Pair with loopback capture (``hybrid`` mode) for clean local audio
    labeled with true Discord names, correlated by timing.
    """
    _require_discord()
    import discord
    from discord.ext import commands

    token = _require_token(token)

    class PresenceBot(commands.Bot):
        async def close(self) -> None:
            for vc_proto in self.voice_clients:
                vc = cast("VoiceClient", vc_proto)
                try:
                    await vc.disconnect()
                except Exception:
                    pass
            await super().close()

    bot = PresenceBot(command_prefix="!", intents=_bot_intents())
    started = threading.Event()

    @bot.event
    async def on_ready() -> None:
        if started.is_set():
            return
        started.set()
        print(f"[bot] logged in as {bot.user}", flush=True)
        resolved = await _resolve_voice_target(bot, guild, channel)
        if resolved is None:
            return
        tg_any, tc_any = resolved
        target_guild = cast("discord.Guild", tg_any)
        target_channel = cast("discord.VoiceChannel", tc_any)
        try:
            me = bot.user.id if bot.user is not None else None
            for m in target_channel.members:
                if m.id != me and m.display_name:
                    timeline.remember(m.id, m.display_name)
            await target_channel.connect()
        except Exception as e:
            print(f"[bot] could not join {target_channel.name}: {e}")
            await bot.close()
            return
        print(f"[bot] presence in '{target_channel.name}' (names only, no recording)", flush=True)

    @bot.event
    async def on_member_speaking_state_update(member: Any, ssrc: int, state: Any) -> None:
        try:
            active = int(state) != 0
        except Exception:
            active = True
        if not active:
            return
        uid = getattr(member, "id", None)
        name = getattr(member, "display_name", None) or getattr(member, "name", None)
        if uid is None:
            try:
                for vc_proto in bot.voice_clients:
                    mapping = getattr(vc_proto, "_ssrc_to_id", None)
                    if mapping and ssrc in mapping:
                        uid = mapping[ssrc]
                        break
            except Exception:
                pass
        if uid is None:
            return
        timeline.note(uid, name)

    print("[bot] logging in…", flush=True)
    try:
        bot.run(token)
    except discord.errors.PrivilegedIntentsRequired:
        raise SystemExit(_INTENTS_ERROR)
