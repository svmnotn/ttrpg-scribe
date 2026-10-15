"""Game-folder transcript writer: ISO-8601 daily file + live console output.

No audio is ever written to disk here — only transcribed text lines.
"""
from __future__ import annotations

import threading
from datetime import date, datetime
from pathlib import Path


class NotesWriter:
    """Appends ``[HH:MM:SS] SPEAKER: text`` lines to ``<game-dir>/<YYYY-MM-DD>.txt``.

    If the file already exists (e.g. second session in one day) a session
    header is appended instead of overwriting.
    """

    game_dir: Path
    path: Path
    _lock: threading.Lock

    def __init__(self, game_dir: str | Path) -> None:
        self.game_dir = Path(game_dir).expanduser()
        self.game_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.game_dir / f"{date.today().isoformat()}.txt"
        self._lock = threading.Lock()
        new_file = not self.path.exists()
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            if new_file:
                f.write(f"# TTRPG session notes — {date.today().isoformat()}\n\n")
            else:
                f.write(f"\n---\n## Session started {datetime.now().strftime('%H:%M:%S')}\n\n")
        print(f"[notes] writing to {self.path}", flush=True)

    def write(self, speaker: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {speaker}: {text}"
        print(line, flush=True)  # live feedback for the GM
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
