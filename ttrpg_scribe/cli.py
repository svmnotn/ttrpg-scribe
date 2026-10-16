"""ttrpg-scribe: point at a game folder, get a live transcript + daily notes file.

Usage:
  ttrpg-scribe "C:\\Notes\\CurseOfStrahd"          # Stream Mix default, nothing else needed
  ttrpg-scribe "C:\\Notes\\CurseOfStrahd" --mode bot --channel "Game Night"
  ttrpg-scribe --map "C:\\Notes\\CurseOfStrahd" --channel "Game Night"   # remember it
  ttrpg-scribe "C:\\Notes\\CurseOfStrahd"          # mapped folder: hybrid (names + local audio)
  ttrpg-scribe --maps                    # list folder mappings
  ttrpg-scribe --unmap "C:\\Notes\\Old"  # forget one
  ttrpg-scribe "C:\\Notes\\CurseOfStrahd" --input "Chat"   # one-off override
  ttrpg-scribe --set-default input "Chat"          # change the default permanently
  ttrpg-scribe --show-config
  ttrpg-scribe --list-devices

Mode selection: --mode flag > folder mapping (implies hybrid) > config default.
Defaults live in %USERPROFILE%\\.ttrpg-scribe.json (one file for everything).
If the configured capture source is missing, you get a numbered device list
and pick one on the spot instead of an error.
"""
from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ttrpg-scribe",
        description="Local voice TTRPG transcriber (no audio stored).",
    )
    p.add_argument("game_dir", nargs="?", help="Folder for this game/campaign. A YYYY-MM-DD.txt file is created inside.")
    p.add_argument("--mode", choices=["loopback", "bot", "hybrid", "sidecar"], default=None,
                   help="Capture path. --mode wins; else a folder mapping implies hybrid; "
                        "else config default (built-in: hybrid).")
    p.add_argument("--input", default=None,
                   help="Capture a recording device directly, no mixing (case-insensitive substring). "
                        "Default comes from config (built-in: your GoXLR Stream Mix).")
    p.add_argument("--device", default=None,
                   help="Loopback source to capture (case-insensitive substring). Overrides the input default.")
    p.add_argument("--mic", default=None,
                   help="Microphone to mix in for loopback mode (case-insensitive substring).")
    p.add_argument("--model", default=None, help="faster-whisper model. Default from config (built-in: large-v3-turbo).")
    p.add_argument("--guild", default=None,
                   help="Voice-bot mode: server to join (name substring or id). Skipped if the bot is only on one server.")
    p.add_argument("--channel", default=None,
                   help="Voice-bot mode: voice channel to join and record (name substring or id).")
    p.add_argument("--list-devices", action="store_true", help="List audio devices and exit.")
    p.add_argument("--show-config", action="store_true", help="Show configured defaults and exit.")
    p.add_argument("--set-default", nargs=2, metavar=("KEY", "VALUE"),
                   help="Persist a default (KEY: mode|input|device|mic|model|guild|channel; empty VALUE clears) and exit.")
    p.add_argument("--map", metavar="FOLDER", default=None,
                   help="Remember guild/channel (--guild/--channel) for FOLDER and exit. "
                        "Several folders may share one target.")
    p.add_argument("--maps", action="store_true", help="List folder → guild/channel mappings and exit.")
    p.add_argument("--unmap", metavar="FOLDER", default=None, help="Forget the mapping for FOLDER and exit.")
    return p


def _resolve_mode(game_dir: str, args_mode: str | None,
                  settings: dict[str, str], mapped: dict[str, str]) -> str:
    """Pick capture mode: --mode flag > folder mapping (implies hybrid) >
    config default (built-in: hybrid). args_mode is None when unpassed."""
    if args_mode:
        return args_mode
    if mapped.get("channel"):
        return "hybrid"
    return settings.get("mode") or "loopback"


def _ask_text(prompt: str, default: str) -> str:
    """Prompt showing a [default]; Enter accepts it. EOF (piped stdin) → default."""
    try:
        raw = input(f"  {prompt} [{default}]: ").strip()
    except EOFError:
        return default
    return raw if raw else default


def _ask_choice(prompt: str, options: list[str], default: str) -> str:
    """Prompt restricted to options (unique prefix accepted)."""
    while True:
        try:
            raw = input(f"  {prompt} ({'/'.join(options)}) [{default}]: ").strip().lower()
        except EOFError:
            return default
        if not raw:
            return default
        hits = [o for o in options if o == raw or o.startswith(raw)]
        if len(hits) == 1:
            return hits[0]
        print(f"  pick one of: {', '.join(options)}")


def first_run_setup(game_dir: str) -> None:
    """Interactive setup when no config file exists yet.

    Walks every preference with its built-in default shown, saves the
    answers, and prints how to change them later (--set-default) and how to
    pin folders (--map). Non-interactive stdin keeps the built-ins.
    """
    from ttrpg_scribe import config as cfg

    print("[setup] first run — no config file yet, let's set your defaults.")
    print(f"[setup] ({cfg.CONFIG_PATH}). Enter accepts the [bracketed] default.")
    answers = {
        "mode": _ask_choice("capture mode", ["sidecar", "hybrid", "loopback", "bot"],
                            cfg.DEFAULTS["mode"]),
        "input": _ask_text("capture device substring", cfg.DEFAULTS["input"]),
        "device": _ask_text("loopback speaker (empty = system default)", cfg.DEFAULTS["device"]),
        "mic": _ask_text("loopback microphone (empty = system default)", cfg.DEFAULTS["mic"]),
        "model": _ask_text("whisper model", cfg.DEFAULTS["model"]),
        "guild": _ask_text("default server (empty = pick per folder)", cfg.DEFAULTS["guild"]),
        "channel": _ask_text("default voice channel (empty = pick per folder)",
                             cfg.DEFAULTS["channel"]),
    }
    cfg.save(answers)
    print(f"[setup] saved defaults to {cfg.CONFIG_PATH}:")
    for key in cfg.DEFAULTS:
        print(f"  {key} = {answers[key]!r}")
    print("[setup] change later: ttrpg-scribe --set-default KEY VALUE")
    print("[setup] pin a folder:  ttrpg-scribe --map FOLDER --guild G --channel C")
    if answers["channel"]:
        try:
            remember = input(f"  Remember guild/channel for this folder ({game_dir})? [y/N]: "
                             ).strip().lower()
        except EOFError:
            remember = ""
        if remember.startswith("y"):
            cfg.save_map(game_dir, answers["guild"], answers["channel"])
            print(f"[setup] mapped {game_dir} → guild={answers['guild']!r} "
                  f"channel={answers['channel']!r}")


def _pick_or_raise(wanted: str, names: list[str], title: str, save_key: str) -> str:
    """Match a device name; on miss, prompt interactively and offer to save."""
    from ttrpg_scribe import config as cfg

    hits = [n for n in names if wanted.lower() in n.lower()]
    if hits:
        return hits[0]
    print(f"[cap] no {title} matching {wanted!r}.")
    chosen = cfg.prompt_choice(title, names)
    if cfg.prompt_yes(f"Save {chosen!r} as the default {save_key}?"):
        cfg.save({save_key: chosen})
        print(f"[config] default {save_key} saved.")
    return chosen


def resolve_capture(args: argparse.Namespace, settings: dict[str, str]) -> dict[str, str | None]:
    """Turn flags + config into an exact, existing device selection.

    Direct-input path wins unless --device/--mic explicitly ask for loopback.
    """
    import soundcard as sc

    if args.input is not None or (args.device is None and args.mic is None):
        wanted = args.input if args.input is not None else settings.get("input", "")
        if wanted:
            names = [d.name for d in sc.all_microphones(include_loopback=True)]
            return {"input": _pick_or_raise(wanted, names, "recording inputs", "input"),
                    "device": None, "mic": None}
    speakers = [d.name for d in sc.all_speakers()]
    mics = [d.name for d in sc.all_microphones(include_loopback=False)]
    wanted_dev = args.device if args.device is not None else settings.get("device", "")
    wanted_mic = args.mic if args.mic is not None else settings.get("mic", "")
    device = _pick_or_raise(wanted_dev, speakers, "speakers/outputs", "device") if wanted_dev else sc.default_speaker().name
    mic = _pick_or_raise(wanted_mic, mics, "microphones", "mic") if wanted_mic else sc.default_microphone().name
    return {"input": None, "device": device, "mic": mic}


def main(argv: list[str] | None = None) -> None:
    from ttrpg_scribe import config as cfg

    args = build_parser().parse_args(argv)

    if args.set_default:
        key, value = args.set_default
        if key not in cfg.KEYS:
            build_parser().error(f"KEY must be one of: {', '.join(sorted(cfg.KEYS))}")
        if key == "mode" and value not in ("loopback", "bot", "hybrid", "sidecar", ""):
            build_parser().error("mode must be 'loopback', 'bot', 'hybrid' or 'sidecar' (or empty to clear)")
        cfg.save({key: value})
        print(f"[config] default {key} = {value!r} ({cfg.CONFIG_PATH})")
        return

    if args.show_config:
        print(cfg.describe())
        return

    if args.list_devices:
        from ttrpg_scribe.loopback import list_devices

        list_devices()
        return

    if args.maps:
        print(cfg.describe_maps())
        return

    if args.unmap:
        if cfg.remove_map(args.unmap):
            print(f"[maps] forgot {args.unmap}")
        else:
            print(f"[maps] no mapping for {args.unmap}")
        return

    if args.map:
        guild = args.guild or cfg.load().get("guild") or ""
        channel = args.channel or cfg.load().get("channel") or ""
        if not channel:
            build_parser().error("--map needs --channel (and optionally --guild)")
        cfg.save_map(args.map, guild, channel)
        print(f"[maps] {args.map} → guild={guild!r} channel={channel!r}")
        return

    if not args.game_dir:
        build_parser().error("game_dir is required (e.g. ttrpg-scribe \"C:\\Notes\\MyCampaign\")")

    if not cfg.CONFIG_PATH.exists():
        # Fresh machine: walk every preference instead of silently inventing it.
        first_run_setup(args.game_dir)

    settings = cfg.load()
    mapped = cfg.lookup_map(args.game_dir) or {}
    mode = _resolve_mode(args.game_dir, args.mode, settings, mapped)
    if args.mode:
        mode_why = f"--mode {args.mode}"
    elif mapped.get("channel"):
        mode_why = f"folder mapping → channel={mapped.get('channel')!r}"
    else:
        mode_why = f"config default ({mode})"
    if mode == "loopback" and mapped.get("channel"):
        print(f"[maps] note: {args.game_dir} is mapped to channel={mapped.get('channel')!r}, "
              f"but mode=loopback ({mode_why}) — run without --mode for sidecar (names + receive).", flush=True)
    else:
        print(f"[mode] {mode} ({mode_why})", flush=True)
    model = args.model or settings.get("model") or "large-v3-turbo"

    from ttrpg_scribe.transcribe import StreamingTranscriber
    from ttrpg_scribe.writer import NotesWriter

    writer = NotesWriter(args.game_dir)
    transcriber = StreamingTranscriber(model_size=model)

    try:
        if mode == "loopback":
            from ttrpg_scribe.diarizer import SpeakerCluster
            from ttrpg_scribe.loopback import run_loopback

            sel = resolve_capture(args, settings)
            run_loopback(writer, transcriber, SpeakerCluster(),
                         device=sel["device"], mic=sel["mic"], input_dev=sel["input"])
        elif mode == "hybrid":
            import threading

            from ttrpg_scribe.bot import SpeakerTimeline, run_presence
            from ttrpg_scribe.diarizer import SpeakerCluster
            from ttrpg_scribe.loopback import run_loopback

            timeline = SpeakerTimeline()
            sel = resolve_capture(args, settings)
            stop = threading.Event()

            def speaker_for(end: float, duration: float) -> str | None:
                return timeline.attribute(end, duration)

            cap = threading.Thread(
                target=run_loopback, daemon=True, name="ttrpg-loopback",
                kwargs={"writer": writer, "transcriber": transcriber,
                        "cluster": SpeakerCluster(),
                        "device": sel["device"], "mic": sel["mic"],
                        "input_dev": sel["input"],
                        "speaker_fn": speaker_for, "stop": stop},
            )
            cap.start()
            try:
                # Presence bot blocks here (main thread) until Ctrl+C.
                run_presence(timeline, None,
                             guild=args.guild or mapped.get("guild") or settings.get("guild") or None,
                             channel=args.channel or mapped.get("channel") or settings.get("channel") or None)
            except KeyboardInterrupt:
                print(f"\n[done] notes saved to {writer.path}", flush=True)
            finally:
                stop.set()
                cap.join(timeout=30)
        elif mode == "sidecar":
            from ttrpg_scribe.bot import run_sidecar

            run_sidecar(writer, transcriber,
                        guild=args.guild or mapped.get("guild") or settings.get("guild") or None,
                        channel=args.channel or mapped.get("channel") or settings.get("channel") or None)
        else:
            from ttrpg_scribe.bot import run_bot

            run_bot(writer, transcriber,
                    guild=args.guild or mapped.get("guild") or settings.get("guild") or None,
                    channel=args.channel or mapped.get("channel") or settings.get("channel") or None)
    except KeyboardInterrupt:
        print(f"\n[done] notes saved to {writer.path}", flush=True)


if __name__ == "__main__":
    main()
