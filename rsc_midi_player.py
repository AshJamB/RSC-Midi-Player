"""
RSC MIDI Player
A portable, standalone media-player-style app for playing MIDI files through
any SoundFont (.sf2) file -- RuneScape, Banjo-Kazooie, GXSCC 8-bit fonts,
whatever you've got. No Java required.

Playback is done with FluidSynth (via the pyfluidsynth ctypes bindings) and
MIDI parsing/timing is done with mido.

The app keeps a small on-disk library (next to the .exe) of every SoundFont
and MIDI you've ever imported, so you can pick them from dropdowns instead of
re-browsing your filesystem every time, and it remembers what you last had
loaded between sessions. See README.md for the folder layout.

Build into a single portable .exe with PyInstaller -- see build_exe.py and
README.md in this folder for full instructions.
"""

import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
import webbrowser
import zipfile
from tkinter import filedialog, messagebox, simpledialog, ttk

# ---------------------------------------------------------------------------
# Make sure bundled FluidSynth DLLs (Windows) are found before we import the
# fluidsynth python bindings. When frozen by PyInstaller, sys._MEIPASS is the
# temp folder the app was unpacked into; we ship the DLLs there via
# --add-binary (see build_exe.py). When running from source, we look in a
# local "bin" folder next to this script.
# ---------------------------------------------------------------------------
def _bootstrap_dll_search_path():
    if getattr(sys, "frozen", False):
        base_dir = sys._MEIPASS  # type: ignore[attr-defined]
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    candidates = [base_dir, os.path.join(base_dir, "bin")]
    for path in candidates:
        if os.path.isdir(path):
            try:
                os.add_dll_directory(path)  # Windows-only, Python 3.8+
            except (AttributeError, OSError):
                pass
            os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")


if sys.platform.startswith("win"):
    _bootstrap_dll_search_path()

try:
    import fluidsynth  # pyfluidsynth
except Exception as exc:  # pragma: no cover - surfaced to the user in the UI
    fluidsynth = None
    _FLUIDSYNTH_IMPORT_ERROR = exc
else:
    _FLUIDSYNTH_IMPORT_ERROR = None

try:
    import mido
except Exception as exc:  # pragma: no cover
    mido = None
    _MIDO_IMPORT_ERROR = exc
else:
    _MIDO_IMPORT_ERROR = None

try:
    import numpy as np
except Exception as exc:  # pragma: no cover
    np = None
    _NUMPY_IMPORT_ERROR = exc
else:
    _NUMPY_IMPORT_ERROR = None

try:
    import lameenc
except Exception as exc:  # pragma: no cover - MP3 export just gets disabled
    lameenc = None
    _LAMEENC_IMPORT_ERROR = exc
else:
    _LAMEENC_IMPORT_ERROR = None


__version__ = "1.0.0"

APP_TITLE = "RSC MIDI Player"
APP_AUTHOR = "Ash Brittain (AshJam)"
APP_GITHUB_USER = "AshJamB"
APP_REPO_URL = f"https://github.com/{APP_GITHUB_USER}/RSC-Midi-Player"
NUM_CHANNELS = 16
RENDER_SAMPLE_RATE = 44100

# ---------------------------------------------------------------------------
# Color palette -- a light, modern take on stock Windows/Fluent colors
# (rather than native "vista" chrome, which ignores most styling anyway).
# White/light-gray surfaces, Windows' own accent blue for the primary
# action and highlights, dark neutral text.
# ---------------------------------------------------------------------------
COLOR_BG = "#f3f3f3"          # main window background (Win11 app chrome gray)
COLOR_BG_PANEL = "#ffffff"    # cards/fields/panels
COLOR_BG_ALT = "#e5e5e5"      # recessed areas (troughs, canvases)
COLOR_ACCENT = "#0078d4"      # Windows accent blue -- primary button, highlights
COLOR_ACCENT_ACTIVE = "#1a86d9"  # accent, hover
COLOR_ACCENT_DARK = "#005a9e"    # accent, pressed
COLOR_TEXT = "#1a1a1a"        # near-black text
COLOR_TEXT_MUTED = "#605e5c"  # muted gray text (status lines, captions)
COLOR_BORDER = "#d1d1d1"      # light neutral borders
COLOR_DISABLED_BG = "#e8e8e8"
COLOR_DISABLED_FG = "#a6a4a2"
UI_FONT = ("Segoe UI", 9)
UI_FONT_BOLD = ("Segoe UI", 9, "bold")
DOWNLOAD_USER_AGENT = f"Mozilla/5.0 (compatible; RSC-MIDI-Player/{__version__})"
MIDI_MAGIC = b"MThd"

# ---------------------------------------------------------------------------
# Self-update. Uses GitHub's plain REST API with no credentials -- the same
# call anyone's browser makes -- so it only succeeds while the repo is
# public. While it's private this just fails quietly (an HTTP 404, as if the
# repo doesn't exist) and the app carries on as normal; nothing here stores,
# prompts for, or requires a token. If the repo is ever made public, this
# starts working with no code changes needed.
# ---------------------------------------------------------------------------
GITHUB_RELEASES_LATEST_API = f"https://api.github.com/repos/{APP_GITHUB_USER}/RSC-Midi-Player/releases/latest"

# Matches the folder-name convention used by the release zip itself, e.g.
# "RSC-Midi-Player-1.0.2-windows" -- if (and only if) the app's own folder
# still looks like this, self-update renames it to match the new version too,
# so the folder name never lags behind what's actually installed. A folder
# the user renamed to something else (it won't match this pattern) is left
# alone -- we never touch a folder name we don't recognize.
UPDATE_FOLDER_NAME_RE = re.compile(r"^(RSC-Midi-Player-)(\d+\.\d+\.\d+)(-windows)$", re.IGNORECASE)

# Sanity floor for the .exe pulled out of a downloaded update -- this build
# bundles numpy and the FluidSynth DLLs, so a genuine build is comfortably
# tens of MB. A file smaller than this is almost certainly a truncated or
# corrupted download, never a real release, so it's rejected before it ever
# gets near overwriting the working install.
MIN_UPDATE_EXE_SIZE = 5 * 1024 * 1024


def get_app_dir():
    """Directory the app's own folder (Soundfonts/, Midis/, library.json)
    lives next to. This is the folder containing the .exe when frozen, or
    the folder containing this script when run from source -- NOT the
    PyInstaller temp extraction dir, so the library survives between runs."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def is_sf2_data(data):
    return len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"sfbk"


def guess_extension_from_url(url, kind):
    path = urllib.parse.urlparse(url).path
    ext = os.path.splitext(path)[1].lower()
    valid = LibraryManager.KINDS[kind]["exts"]
    if ext in valid:
        return ext
    return ".sf2" if kind == "soundfont" else ".mid"


def guess_name_from_url(url):
    path = urllib.parse.urlparse(url).path
    name = os.path.basename(urllib.parse.unquote(path))
    name = os.path.splitext(name)[0].strip()
    return name or "Downloaded file"


def download_url_to_file(url, dest_path, kind, progress_cb=None):
    """Stream-download url to dest_path, validating that the content looks
    like the expected file type (checked on the first bytes received, before
    committing to the rest of the download). Raises on failure; cleans up
    dest_path if validation fails partway through."""
    req = urllib.request.Request(url, headers={"User-Agent": DOWNLOAD_USER_AGENT})
    try:
        resp = urllib.request.urlopen(req, timeout=20)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Server returned an error: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach that URL: {exc.reason}") from exc

    with resp:
        total = resp.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else None
        downloaded = 0
        header_buf = b""
        checked = False

        with open(dest_path, "wb") as f:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                if not checked:
                    header_buf += chunk
                    if len(header_buf) >= 16:
                        _validate_magic(header_buf, kind, dest_path)
                        checked = True
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb:
                    progress_cb(downloaded, total)

        if not checked:
            # File was smaller than our magic-byte check window.
            _validate_magic(header_buf, kind, dest_path)


def _validate_magic(header_buf, kind, dest_path):
    ok = header_buf.startswith(MIDI_MAGIC) if kind == "midi" else is_sf2_data(header_buf)
    if not ok:
        try:
            os.remove(dest_path)
        except OSError:
            pass
        expected = "a MIDI file (should start with 'MThd')" if kind == "midi" else "an SF2 SoundFont (should be a RIFF/sfbk file)"
        raise ValueError(f"That link doesn't look like {expected}.")


def _parse_semver(text):
    """'v1.2.3' or '1.2.3' -> (1, 2, 3); anything else -> None."""
    if not text:
        return None
    m = re.match(r"^[vV]?(\d+)\.(\d+)\.(\d+)$", text.strip())
    return tuple(int(x) for x in m.groups()) if m else None


def fetch_latest_release():
    """Query GitHub's public Releases API for this repo's latest release.
    No authentication is sent -- this is exactly the request a browser makes
    for a public repo. Raises RuntimeError/ValueError on any failure (no
    internet, rate limiting, or -- while the repo is private -- a 404, since
    a private repo's releases simply aren't visible without a token this app
    doesn't store). Callers decide how loudly to surface that."""
    req = urllib.request.Request(
        GITHUB_RELEASES_LATEST_API,
        headers={"User-Agent": DOWNLOAD_USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(
                "No release found (this happens while the repo is private -- "
                "checking for updates needs the repo, or at least its "
                "releases, to be public)."
            ) from exc
        raise RuntimeError(f"GitHub returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach GitHub: {exc.reason}") from exc

    tag = data.get("tag_name", "")
    version = _parse_semver(tag)
    if version is None:
        raise ValueError(f"Latest release tag {tag!r} isn't a plain vX.Y.Z version.")

    asset_url = None
    asset_name = None
    for asset in data.get("assets", []):
        name = asset.get("name", "")
        if name.lower().endswith(".zip") and "windows" in name.lower():
            asset_url = asset.get("browser_download_url")
            asset_name = name
            break
    if not asset_url:
        raise ValueError("Latest release has no Windows .zip asset to download.")

    return {
        "tag": tag,
        "version": version,
        "version_str": "%d.%d.%d" % version,
        "asset_name": asset_name,
        "download_url": asset_url,
        "notes": (data.get("body") or "").strip(),
    }


def plan_update_folder_rename(exe_dir, new_version_str):
    """If exe_dir's own name still looks like the release folder convention
    ("RSC-Midi-Player-<old version>-windows"), return the sibling path it
    should be renamed to so the folder name matches the version being
    installed. Returns None if the name doesn't match that convention (e.g.
    the user renamed the folder to something of their own), if it already
    matches the new version, or if a folder with the new name already
    exists (never clobber something that's already there)."""
    parent_dir = os.path.dirname(exe_dir.rstrip("\\/"))
    folder_name = os.path.basename(exe_dir.rstrip("\\/"))
    m = UPDATE_FOLDER_NAME_RE.match(folder_name)
    if not m or m.group(2) == new_version_str:
        return None
    new_folder_name = f"{m.group(1)}{new_version_str}{m.group(3)}"
    new_dir = os.path.join(parent_dir, new_folder_name)
    if os.path.normcase(new_dir) == os.path.normcase(exe_dir):
        return None
    if os.path.exists(new_dir):
        return None
    return new_dir


def start_self_update(zip_bytes, new_version_str=None):
    """Extract the new .exe from a downloaded release zip and hand off to a
    tiny detached helper script that waits for this process to exit, swaps
    the exe, relaunches it, then deletes itself. Only meaningful for the
    frozen .exe -- raises if called while running from source, since there's
    no exe here to replace.

    Everything that can be checked ahead of time -- that the zip isn't
    corrupt, that it actually contains an .exe, that the .exe isn't
    suspiciously small (a truncated/corrupted download) -- is checked here,
    entirely in a scratch temp folder, *before* anything about the current,
    working install is touched. If any check fails, this raises and the
    caller's install is left exactly as it was.

    If new_version_str is given and the app's own folder still follows the
    release-zip naming convention (RSC-Midi-Player-X.Y.Z-windows), the
    helper script also renames that folder to match the new version, so the
    folder name never ends up stuck on an old version number. A folder
    that's been renamed to anything else is left untouched."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Self-update only applies to the built .exe, not when running from source.")

    current_exe = os.path.abspath(sys.executable)
    exe_dir = os.path.dirname(current_exe)
    exe_name = os.path.basename(current_exe)

    tmp_dir = tempfile.mkdtemp(prefix="rscmp_update_")
    zip_path = os.path.join(tmp_dir, "update.zip")
    with open(zip_path, "wb") as f:
        f.write(zip_bytes)

    with zipfile.ZipFile(zip_path) as zf:
        bad_entry = zf.testzip()
        if bad_entry is not None:
            raise RuntimeError(
                f"Downloaded update is corrupt (bad file inside the zip: {bad_entry}). "
                "Nothing has been changed -- try Check for Updates again."
            )

        exe_member = next((n for n in zf.namelist() if n.lower().endswith(".exe")), None)
        if exe_member is None:
            raise RuntimeError("Downloaded update .zip doesn't contain an .exe.")
        new_exe_path = os.path.join(tmp_dir, "new.exe")
        with zf.open(exe_member) as src, open(new_exe_path, "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)

    new_exe_size = os.path.getsize(new_exe_path)
    if new_exe_size < MIN_UPDATE_EXE_SIZE:
        raise RuntimeError(
            f"Downloaded update looks incomplete or corrupt (the extracted .exe "
            f"is only {new_exe_size / (1024*1024):.1f} MB, well under what a real "
            "build should be). Nothing has been changed -- try Check for Updates "
            "again."
        )

    final_exe = current_exe
    rename_line = ""
    if new_version_str:
        new_dir = plan_update_folder_rename(exe_dir, new_version_str)
        if new_dir:
            rename_line = f'if not exist "{new_dir}" move /y "{exe_dir}" "{new_dir}"\r\n'
            final_exe = os.path.join(new_dir, exe_name)

    # A short delay gives this process time to fully exit (and release its
    # lock on current_exe) before anything below is attempted. The *current*
    # exe is backed up (as "<name>.exe.bak", alongside it) before being
    # overwritten -- so a bad update, even one that somehow slipped past the
    # checks above, still leaves the previous known-good build recoverable
    # (rename the .bak back) instead of just being gone. Order matters: back
    # up + swap the exe while the folder still has its old name/path, THEN
    # rename the whole folder (carrying the new exe, the .bak, and
    # library.json/Soundfonts/Midis all together), THEN relaunch from
    # wherever it ended up.
    backup_exe = current_exe + ".bak"
    bat_path = os.path.join(tmp_dir, "apply_update.bat")
    with open(bat_path, "w", encoding="utf-8") as f:
        f.write(
            "@echo off\r\n"
            "timeout /t 2 /nobreak > NUL\r\n"
            f'if exist "{backup_exe}" del /f /q "{backup_exe}"\r\n'
            f'move /y "{current_exe}" "{backup_exe}"\r\n'
            f'move /y "{new_exe_path}" "{current_exe}"\r\n'
            f"{rename_line}"
            f'start "" "{final_exe}"\r\n'
            'del "%~f0"\r\n'
        )

    subprocess.Popen(
        ["cmd", "/c", bat_path],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )


def write_wav_file(path, pcm_bytes, sample_rate=RENDER_SAMPLE_RATE, channels=2, sampwidth=2):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)


def write_mp3_file(path, pcm_bytes, sample_rate=RENDER_SAMPLE_RATE, channels=2, bitrate=192):
    if lameenc is None:
        raise RuntimeError(f"MP3 export is unavailable: {_LAMEENC_IMPORT_ERROR}")
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(bitrate)
    encoder.set_in_sample_rate(sample_rate)
    encoder.set_channels(channels)
    encoder.set_quality(2)  # 2 = highest quality, 7 = fastest
    data = encoder.encode(pcm_bytes)
    data += encoder.flush()
    with open(path, "wb") as f:
        f.write(data)


# Brute-force scan range for enumerate_soundfont_presets(). SF2 banks are a
# 14-bit MIDI concept in theory, but in practice essentially every SoundFont
# (including hand-made game ones) only uses bank 0 (melodic) and bank 128
# (percussion, by GM convention), with maybe a handful of extra banks for
# variations. This range comfortably covers real-world fonts while staying
# fast (a fixed ~16.6k cheap lookups regardless of font size).
PRESET_SCAN_MAX_BANK = 129
PRESET_SCAN_MAX_PRESET = 128


def enumerate_soundfont_presets(synth, sfid):
    """Return every (bank, preset, name) defined in the loaded SoundFont,
    found by probing FluidSynth directly (there's no "list all presets" call
    in the bindings, only "does this bank/preset exist"). Fast: a fixed
    number of cheap C calls, independent of the font's actual size."""
    presets = []
    for bank in range(PRESET_SCAN_MAX_BANK + 1):
        for preset in range(PRESET_SCAN_MAX_PRESET):
            try:
                name = synth.sfpreset_name(sfid, bank, preset)
            except Exception:
                name = None
            if name:
                presets.append((bank, preset, name))
    return presets


def summarize_channel_programs(events):
    """For each MIDI channel used in events, figure out which (bank, program)
    it would use by default, for display in the Channel Instruments dialog.
    Snapshots each channel's bank/program state as of its first note, since
    that's what a listener actually hears (a handful of songs send further
    patch changes mid-track on the same channel, but the "first sound you
    hear" is what matters for identifying which instrument sounds wrong)."""
    state = {}
    result = {}
    for _abs_t, msg in events:
        ch = getattr(msg, "channel", None)
        if ch is None:
            continue
        st = state.setdefault(ch, {"bank": 0, "program": 0})
        if msg.type == "control_change" and msg.control == 0:
            st["bank"] = msg.value
        elif msg.type == "program_change":
            st["program"] = msg.program
        elif msg.type in ("note_on", "note_off") and ch not in result:
            result[ch] = (st["bank"], st["program"])
    for ch, st in state.items():
        result.setdefault(ch, (st["bank"], st["program"]))
    return result


# ---------------------------------------------------------------------------
# Library: remembers every SoundFont/MIDI you've imported, copies them into
# app-local Soundfonts/ and Midis/ folders (deduped by content hash so
# re-importing the same file twice doesn't waste space), and persists which
# ones you had selected last.
# ---------------------------------------------------------------------------
class LibraryManager:
    KINDS = {
        "soundfont": {"folder": "Soundfonts", "exts": (".sf2", ".sf3")},
        "midi": {"folder": "Midis", "exts": (".mid", ".midi")},
    }

    def __init__(self, app_dir):
        self.app_dir = app_dir
        self.manifest_path = os.path.join(app_dir, "library.json")
        self.data = {
            "soundfonts": [],
            "midis": [],
            "last_soundfont_id": None,
            "last_midi_id": None,
            # midi_id -> sfont_id -> {"<channel>": [bank, preset]}
            # Lets a per-channel instrument fix be remembered for one
            # specific song+SoundFont pairing, since a fix for one SoundFont
            # is meaningless for another.
            "channel_overrides": {},
            # Tag of a release the user explicitly dismissed via "Later", so
            # the startup check doesn't nag about the same version every
            # launch. A manual "Check for Updates" always checks regardless.
            "skipped_update_version": None,
        }
        for kind in self.KINDS.values():
            os.makedirs(os.path.join(app_dir, kind["folder"]), exist_ok=True)
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self):
        if os.path.isfile(self.manifest_path):
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self.data.update(loaded)
            except Exception:
                pass  # corrupt/missing manifest -> start fresh, don't crash

    def save(self):
        try:
            with open(self.manifest_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except Exception:
            pass  # non-fatal; worst case we lose the "remember last" feature

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _hash_file(path):
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _entries_key(self, kind):
        return "soundfonts" if kind == "soundfont" else "midis"

    def list(self, kind):
        return list(self.data[self._entries_key(kind)])

    def get(self, kind, entry_id):
        for e in self.data[self._entries_key(kind)]:
            if e["id"] == entry_id:
                return e
        return None

    # -- mutation -------------------------------------------------------
    def import_file(self, kind, source_path, display_name=None):
        """Copy source_path into the library (unless an identical file is
        already there) and return the library entry dict. display_name
        overrides the name shown in the dropdown (used for links, where
        source_path is a temp file with a meaningless name)."""
        info = self.KINDS[kind]
        digest = self._hash_file(source_path)

        # Dedup: if we already have a file with this exact content, reuse it.
        for e in self.data[self._entries_key(kind)]:
            if e.get("hash") == digest:
                return e

        ext = os.path.splitext(source_path)[1].lower()
        if ext not in info["exts"]:
            # Keep original extension anyway if it's something unexpected,
            # rather than rejecting the import outright.
            ext = ext or (".sf2" if kind == "soundfont" else ".mid")

        dest_name = f"{uuid.uuid4().hex}{ext}"
        dest_path = os.path.join(self.app_dir, info["folder"], dest_name)

        with open(source_path, "rb") as src, open(dest_path, "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)

        entry = {
            "id": uuid.uuid4().hex,
            "name": display_name or os.path.splitext(os.path.basename(source_path))[0],
            "file": os.path.join(info["folder"], dest_name),
            "hash": digest,
            "original_path": source_path,
        }
        self.data[self._entries_key(kind)].append(entry)
        self.save()
        return entry

    def remove(self, kind, entry_id, delete_file=True):
        key = self._entries_key(kind)
        entry = self.get(kind, entry_id)
        if entry is None:
            return
        self.data[key] = [e for e in self.data[key] if e["id"] != entry_id]
        if delete_file:
            full = os.path.join(self.app_dir, entry["file"])
            try:
                if os.path.isfile(full):
                    os.remove(full)
            except Exception:
                pass
        if self.data.get(f"last_{kind}_id") == entry_id:
            self.data[f"last_{kind}_id"] = None

        # Clean up any channel overrides that referenced this entry, so
        # library.json doesn't accumulate orphaned data forever.
        co = self.data.setdefault("channel_overrides", {})
        if kind == "midi":
            co.pop(entry_id, None)
        else:
            for per_sfont in co.values():
                per_sfont.pop(entry_id, None)

        self.save()

    def rename(self, kind, entry_id, new_name):
        entry = self.get(kind, entry_id)
        if entry:
            entry["name"] = new_name
            self.save()

    def full_path(self, kind, entry_id):
        entry = self.get(kind, entry_id)
        if entry is None:
            return None
        return os.path.join(self.app_dir, entry["file"])

    def set_last(self, kind, entry_id):
        self.data[f"last_{kind}_id"] = entry_id
        self.save()

    def get_last(self, kind):
        return self.data.get(f"last_{kind}_id")

    # -- per-channel instrument overrides -----------------------------------
    def get_channel_overrides(self, midi_id, sfont_id):
        """Returns {channel:int -> (bank:int, preset:int)} for this specific
        song+SoundFont pairing."""
        raw = self.data.get("channel_overrides", {}).get(midi_id, {}).get(sfont_id, {})
        return {int(ch): tuple(bp) for ch, bp in raw.items()}

    def set_channel_override(self, midi_id, sfont_id, channel, bank, preset):
        co = self.data.setdefault("channel_overrides", {})
        co.setdefault(midi_id, {}).setdefault(sfont_id, {})[str(channel)] = [bank, preset]
        self.save()

    def clear_channel_override(self, midi_id, sfont_id, channel):
        per_sfont = self.data.get("channel_overrides", {}).get(midi_id, {}).get(sfont_id, {})
        per_sfont.pop(str(channel), None)
        self.save()

    def clear_all_channel_overrides(self, midi_id, sfont_id):
        per_midi = self.data.get("channel_overrides", {}).get(midi_id)
        if per_midi is not None and sfont_id in per_midi:
            per_midi[sfont_id] = {}
            self.save()


class PlaybackEngine:
    """Owns the FluidSynth synth and drives MIDI playback on a worker thread."""

    def __init__(self, on_position_update, on_finished):
        self.on_position_update = on_position_update
        self.on_finished = on_finished

        self.synth = None
        self.sfid = None
        self.soundfont_path = None

        self.events = []          # list of (abs_time_seconds, mido.Message)
        self.total_time = 0.0
        self.midi_path = None

        self._thread = None
        self._stop_flag = threading.Event()
        self._pause_flag = threading.Event()  # set == paused
        self._seek_target = None
        self._lock = threading.Lock()
        self._gain = 0.5  # 0.0 - 2.0

        # channel:int -> (bank:int, preset:int). When a channel has an
        # override, the MIDI file's own program_change/bank-select messages
        # for that channel are ignored -- the channel is pinned to this
        # instrument for the whole song. Applies to both live playback and
        # offline export.
        self.channel_overrides = {}

    # -- per-channel instrument overrides -----------------------------------
    def apply_overrides_now(self, overrides):
        """Replace the current override set and, if a SoundFont is already
        loaded, apply it to the live synth immediately (so switching
        instruments works while a song is playing, for auditioning)."""
        self.channel_overrides = dict(overrides)
        if self.synth is not None and self.sfid is not None:
            self._apply_channel_overrides_to_synth(self.synth, self.sfid, self.channel_overrides)

    @staticmethod
    def _apply_channel_overrides_to_synth(synth, sfid, overrides):
        for ch, (bank, preset) in overrides.items():
            try:
                synth.program_select(ch, sfid, bank, preset)
            except Exception:
                pass

    # -- setup ---------------------------------------------------------
    def ensure_synth(self):
        if self.synth is None:
            self.synth = fluidsynth.Synth(samplerate=44100.0)
            driver = "dsound" if sys.platform.startswith("win") else None
            try:
                self.synth.start(driver=driver) if driver else self.synth.start()
            except Exception:
                # Fall back to letting fluidsynth pick a driver automatically.
                self.synth.start()
            self._apply_gain()

    def _apply_gain(self):
        """Set the synth's master gain. pyfluidsynth >=1.4 dropped the old
        set_gain() convenience method in favor of the generic setting()
        call; older versions only have set_gain(). Support both."""
        if self.synth is None:
            return
        try:
            self.synth.setting("synth.gain", self._gain)
        except Exception:
            try:
                self.synth.set_gain(self._gain)
            except Exception:
                pass

    def load_soundfont(self, path):
        self.ensure_synth()
        sfid = self.synth.sfload(path)
        if sfid == -1:
            raise RuntimeError("FluidSynth could not load that SoundFont file.")
        # Replace any previously loaded font.
        if self.sfid is not None:
            try:
                self.synth.sfunload(self.sfid)
            except Exception:
                pass
        self.sfid = sfid
        self.soundfont_path = path
        for ch in range(NUM_CHANNELS):
            try:
                self.synth.program_select(ch, self.sfid, 0, 0)
            except Exception:
                pass

    def load_midi(self, path):
        if mido is None:
            raise RuntimeError(f"mido is not available: {_MIDO_IMPORT_ERROR}")
        midi_file = mido.MidiFile(path)
        events = []
        t = 0.0
        for msg in midi_file:  # mido resolves tempo + ticks -> real seconds here
            t += msg.time
            if not msg.is_meta:
                events.append((t, msg))
        self.events = events
        self.total_time = t
        self.midi_path = path

    # -- transport -------------------------------------------------------
    def play(self):
        if self.synth is None or self.sfid is None:
            raise RuntimeError("Load a SoundFont first.")
        if not self.events:
            raise RuntimeError("Load a MIDI file first.")

        if self._thread and self._thread.is_alive():
            # Already running -- just unpause.
            self._pause_flag.clear()
            return

        self._stop_flag.clear()
        self._pause_flag.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def pause(self):
        self._pause_flag.set()

    def resume(self):
        self._pause_flag.clear()

    def is_paused(self):
        return self._pause_flag.is_set()

    def is_playing(self):
        return bool(self._thread and self._thread.is_alive())

    def stop(self):
        self._stop_flag.set()
        self._pause_flag.clear()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._all_notes_off()

    def seek(self, target_seconds):
        with self._lock:
            self._seek_target = max(0.0, min(target_seconds, self.total_time))

    def set_volume(self, value_0_to_100):
        self._gain = max(0.0, min(2.0, (value_0_to_100 / 100.0) * 2.0))
        self._apply_gain()

    def shutdown(self):
        self.stop()
        if self.synth is not None:
            try:
                self.synth.delete()
            except Exception:
                pass
            self.synth = None

    # -- offline rendering (export) -----------------------------------------
    def render_offline(self, progress_cb=None, cancel_check=None):
        """Render the currently loaded SoundFont+MIDI to raw 16-bit stereo
        PCM at RENDER_SAMPLE_RATE, entirely offline (no audio device, no
        real-time waiting) using a throwaway Synth so it never touches live
        playback state. Returns (pcm_bytes, sample_rate), or (None, None) if
        cancelled. progress_cb, if given, is called with a float in [0, 1]."""
        if not self.soundfont_path:
            raise RuntimeError("Load a SoundFont first.")
        if not self.events:
            raise RuntimeError("Load a MIDI file first.")
        if np is None:
            raise RuntimeError(f"numpy is unavailable: {_NUMPY_IMPORT_ERROR}")

        sample_rate = RENDER_SAMPLE_RATE
        render_synth = fluidsynth.Synth(samplerate=float(sample_rate))
        # Deliberately NOT calling render_synth.start() -- we only want
        # get_samples() offline rendering, never a live audio device, so this
        # can safely run concurrently with (or without) real playback.
        try:
            sfid = render_synth.sfload(self.soundfont_path)
            if sfid == -1:
                raise RuntimeError("FluidSynth could not (re)load the SoundFont for export.")
            for ch in range(NUM_CHANNELS):
                try:
                    render_synth.program_select(ch, sfid, 0, 0)
                except Exception:
                    pass
            # Re-apply the same per-channel overrides used for live playback,
            # so an exported file matches what you actually hear.
            self._apply_channel_overrides_to_synth(render_synth, sfid, self.channel_overrides)

            chunks = []
            last_time = 0.0
            total = self.total_time or 1.0
            n_events = len(self.events)

            for i, (abs_t, msg) in enumerate(self.events):
                if cancel_check and cancel_check():
                    return None, None
                gap = abs_t - last_time
                if gap > 0:
                    n = int(round(gap * sample_rate))
                    if n > 0:
                        chunks.append(render_synth.get_samples(n))
                self._apply_message(msg, audible=True, synth=render_synth)
                last_time = abs_t
                if progress_cb and (i % 100 == 0 or i == n_events - 1):
                    progress_cb(min(0.97, abs_t / total))

            # A couple seconds of tail so reverb/release isn't cut off abruptly.
            tail_samples = sample_rate * 2
            chunks.append(render_synth.get_samples(tail_samples))
        finally:
            try:
                render_synth.delete()
            except Exception:
                pass

        if progress_cb:
            progress_cb(1.0)

        pcm = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)
        return pcm.astype(np.int16).tobytes(), sample_rate

    # -- internals ---------------------------------------------------------
    def _all_notes_off(self):
        if self.synth is None:
            return
        for ch in range(NUM_CHANNELS):
            try:
                self.synth.cc(ch, 123, 0)  # all notes off
                self.synth.cc(ch, 120, 0)  # all sound off
            except Exception:
                pass

    def _replay_state_only(self, up_to_time):
        """Fast-forward non-audible state (program/control/pitch) up to a time,
        without actually sounding notes, so a seek lands with correct
        instrument/patch/pan/etc. Skips note_on/note_off."""
        for abs_t, msg in self.events:
            if abs_t > up_to_time:
                break
            self._apply_message(msg, audible=False)

    def _apply_message(self, msg, audible=True, synth=None):
        synth = synth if synth is not None else self.synth
        ch = getattr(msg, "channel", None)

        # A channel with an override is pinned to that instrument for the
        # whole song -- ignore the MIDI file's own patch/bank changes on it
        # so they can't fight the override.
        if ch is not None and ch in self.channel_overrides:
            if msg.type == "program_change":
                return
            if msg.type == "control_change" and msg.control in (0, 32):
                return

        try:
            if msg.type == "note_on" and audible:
                if msg.velocity == 0:
                    synth.noteoff(msg.channel, msg.note)
                else:
                    synth.noteon(msg.channel, msg.note, msg.velocity)
            elif msg.type == "note_off" and audible:
                synth.noteoff(msg.channel, msg.note)
            elif msg.type == "control_change":
                synth.cc(msg.channel, msg.control, msg.value)
            elif msg.type == "program_change":
                synth.program_change(msg.channel, msg.program)
            elif msg.type == "pitchwheel":
                synth.pitch_bend(msg.channel, msg.pitch)
            elif msg.type == "aftertouch":
                pass
            elif msg.type == "polytouch":
                pass
        except Exception:
            pass

    def _run(self):
        idx = 0
        n = len(self.events)
        start_wall = time.monotonic()
        start_pos = 0.0
        paused_accum = 0.0

        while idx < n and not self._stop_flag.is_set():
            # Handle a pending seek request.
            with self._lock:
                seek_to = self._seek_target
                self._seek_target = None
            if seek_to is not None:
                self._all_notes_off()
                self._replay_state_only(seek_to)
                idx = 0
                while idx < n and self.events[idx][0] < seek_to:
                    idx += 1
                start_wall = time.monotonic()
                start_pos = seek_to
                paused_accum = 0.0

            if self._pause_flag.is_set():
                pause_started = time.monotonic()
                while self._pause_flag.is_set() and not self._stop_flag.is_set():
                    time.sleep(0.05)
                    with self._lock:
                        if self._seek_target is not None:
                            break
                paused_accum += time.monotonic() - pause_started
                continue

            abs_t, msg = self.events[idx]
            elapsed = (time.monotonic() - start_wall - paused_accum) + start_pos
            wait = abs_t - elapsed
            if wait > 0:
                time.sleep(min(wait, 0.05))
                continue

            self._apply_message(msg, audible=True)
            idx += 1

            now_pos = abs_t
            self.on_position_update(now_pos, self.total_time)

        self._all_notes_off()
        if not self._stop_flag.is_set():
            self.on_finished()


class ProgressDialog(tk.Toplevel):
    """Small modal window with a progress bar + status label, used for both
    downloads (byte counts) and offline export rendering (percentage)."""

    def __init__(self, parent, title, allow_cancel=False, on_cancel=None):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=COLOR_BG)
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", lambda: None)  # no closing via the X

        self.status_var = tk.StringVar(value=title)
        ttk.Label(self, textvariable=self.status_var, width=46).pack(padx=16, pady=(16, 8))

        self.bar = ttk.Progressbar(self, orient="horizontal", length=320, mode="determinate", maximum=100)
        self.bar.pack(padx=16, pady=(0, 12))

        if allow_cancel:
            ttk.Button(self, text="Cancel", command=on_cancel).pack(pady=(0, 14))
        else:
            ttk.Frame(self, height=6).pack()

        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() // 2) - (self.winfo_width() // 2)
        y = parent.winfo_rooty() + (parent.winfo_height() // 2) - (self.winfo_height() // 2)
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()

    def set_fraction(self, frac):
        self.bar.config(mode="determinate")
        self.bar["value"] = max(0.0, min(1.0, frac)) * 100.0

    def set_status(self, text):
        self.status_var.set(text)

    def close(self):
        try:
            self.grab_release()
            self.destroy()
        except Exception:
            pass


class DownloadProgressDialog(ProgressDialog):
    def update_progress(self, downloaded, total):
        mb = downloaded / (1024 * 1024)
        if total:
            self.set_fraction(downloaded / total)
            self.set_status(f"{self.status_var.get().split(chr(10))[0]}\n{mb:.1f} MB / {total / (1024*1024):.1f} MB")
        else:
            self.bar.config(mode="indeterminate")
            self.bar.step(2)
            self.set_status(f"{self.status_var.get().split(chr(10))[0]}\n{mb:.1f} MB downloaded")


class PlayerApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_TITLE} v{__version__}")
        self.root.geometry("640x430")
        self.root.resizable(False, False)

        self.app_dir = get_app_dir()
        self.library = LibraryManager(self.app_dir)
        self.engine = PlaybackEngine(self._on_position_update, self._on_finished)

        self.time_var = tk.StringVar(value="00:00 / 00:00")
        self.status_var = tk.StringVar(value="Ready.")

        # id lists kept in parallel with combobox display strings
        self._sf_ids = []
        self._midi_ids = []

        # What's actually loaded in the engine right now (not necessarily
        # what the comboboxes show mid-load) -- used as the key for saving/
        # restoring per-song-per-SoundFont channel instrument overrides.
        self._current_sf_id = None
        self._current_midi_id = None
        self._preset_cache = {}  # soundfont entry id -> [(bank, preset, name), ...]

        self._seeking = False
        self._build_ui()
        self._refresh_soundfont_list(select_last=True)
        self._refresh_midi_list(select_last=True)

        if fluidsynth is None:
            messagebox.showerror(
                APP_TITLE,
                "FluidSynth could not be loaded.\n\n"
                f"{_FLUIDSYNTH_IMPORT_ERROR}\n\n"
                "Make sure fluidsynth DLLs are present next to the program "
                "(see README.md).",
            )

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Silent startup update check -- only surfaces UI if a genuinely
        # newer release is found; any failure (offline, private repo, rate
        # limited) is swallowed quietly here (see _check_for_updates).
        self.root.after(2000, lambda: self._check_for_updates(manual=False))

    def _build_ui(self):
        pad = {"padx": 16, "pady": 8}

        # -- Menu bar --
        menubar = tk.Menu(self.root)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="Check for Updates...", command=lambda: self._check_for_updates(manual=True))
        help_menu.add_separator()
        help_menu.add_command(label="About RSC MIDI Player", command=self._open_about_dialog)
        menubar.add_cascade(label="Help", menu=help_menu)
        style_menu(menubar)
        style_menu(help_menu)
        self.root.config(menu=menubar)

        # -- SoundFont row --
        frm_sf = ttk.LabelFrame(self.root, text="SoundFont")
        frm_sf.pack(fill="x", **pad)
        self.sf_combo = ttk.Combobox(frm_sf, state="readonly", width=28)
        self.sf_combo.grid(row=0, column=0, padx=(10, 6), pady=10, sticky="w")
        self.sf_combo.bind("<<ComboboxSelected>>", self._on_soundfont_selected)
        ttk.Button(frm_sf, text="Import...", command=self._import_soundfont).grid(
            row=0, column=1, padx=3
        )
        ttk.Button(frm_sf, text="Add via URL...", command=lambda: self._import_via_link("soundfont")).grid(
            row=0, column=2, padx=3
        )
        ttk.Button(frm_sf, text="Remove", command=self._remove_soundfont).grid(
            row=0, column=3, padx=(3, 10)
        )

        # -- MIDI row --
        frm_midi = ttk.LabelFrame(self.root, text="MIDI")
        frm_midi.pack(fill="x", **pad)
        self.midi_combo = ttk.Combobox(frm_midi, state="readonly", width=28)
        self.midi_combo.grid(row=0, column=0, padx=(10, 6), pady=10, sticky="w")
        self.midi_combo.bind("<<ComboboxSelected>>", self._on_midi_selected)
        ttk.Button(frm_midi, text="Import...", command=self._import_midi).grid(
            row=0, column=1, padx=3
        )
        ttk.Button(frm_midi, text="Add via URL...", command=lambda: self._import_via_link("midi")).grid(
            row=0, column=2, padx=3
        )
        ttk.Button(frm_midi, text="Remove", command=self._remove_midi).grid(
            row=0, column=3, padx=(3, 10)
        )

        # -- Seek --
        frm_seek = ttk.Frame(self.root)
        frm_seek.pack(fill="x", padx=16, pady=(14, 0))
        self.seek_scale = ttk.Scale(
            frm_seek, from_=0, to=1000, orient="horizontal",
            command=self._on_seek_drag,
        )
        self.seek_scale.pack(fill="x")
        self.seek_scale.bind("<ButtonPress-1>", lambda e: setattr(self, "_seeking", True))
        self.seek_scale.bind("<ButtonRelease-1>", self._on_seek_release)

        ttk.Label(self.root, textvariable=self.time_var, foreground=COLOR_TEXT_MUTED).pack(pady=(4, 0))

        # -- Transport -- Play is the one accented (primary) action; the
        # Channels button is a small square icon+label control set apart
        # from the plain text buttons.
        frm_controls = ttk.Frame(self.root)
        frm_controls.pack(pady=(14, 6))
        self.play_btn = ttk.Button(
            frm_controls, text="Play", command=self._toggle_play, width=10, style="Accent.TButton"
        )
        self.play_btn.grid(row=0, column=0, padx=6, sticky="s")
        ttk.Button(frm_controls, text="Stop", command=self._stop, width=10).grid(
            row=0, column=1, padx=6, sticky="s"
        )
        ttk.Button(frm_controls, text="Export...", command=self._open_export_dialog, width=10).grid(
            row=0, column=2, padx=6, sticky="s"
        )
        self._channels_icon = make_piano_icon()
        ttk.Button(
            frm_controls, text="Channels", image=self._channels_icon, compound="top",
            command=self._open_channel_mixer, style="Square.TButton",
        ).grid(row=0, column=3, padx=(14, 0), sticky="s")

        # -- Volume --
        frm_vol = ttk.Frame(self.root)
        frm_vol.pack(fill="x", padx=16, pady=(10, 6))
        ttk.Label(frm_vol, text="Volume", foreground=COLOR_TEXT_MUTED).pack(side="left")
        self.vol_scale = ttk.Scale(
            frm_vol, from_=0, to=100, orient="horizontal", command=self._on_volume
        )
        self.vol_scale.set(50)
        self.vol_scale.pack(side="left", fill="x", expand=True, padx=(10, 0))

        ttk.Separator(self.root, orient="horizontal").pack(side="bottom", fill="x")
        ttk.Label(self.root, textvariable=self.status_var, foreground=COLOR_TEXT_MUTED).pack(
            side="bottom", fill="x", padx=16, pady=8
        )

    # -- library-backed dropdowns -----------------------------------------
    def _refresh_soundfont_list(self, select_last=False):
        entries = self.library.list("soundfont")
        self._sf_ids = [e["id"] for e in entries]
        self.sf_combo["values"] = [e["name"] for e in entries]
        if select_last:
            last_id = self.library.get_last("soundfont")
            if last_id in self._sf_ids:
                idx = self._sf_ids.index(last_id)
                self.sf_combo.current(idx)
                self._load_soundfont_by_id(last_id, is_startup=True)

    def _refresh_midi_list(self, select_last=False):
        entries = self.library.list("midi")
        self._midi_ids = [e["id"] for e in entries]
        self.midi_combo["values"] = [e["name"] for e in entries]
        if select_last:
            last_id = self.library.get_last("midi")
            if last_id in self._midi_ids:
                idx = self._midi_ids.index(last_id)
                self.midi_combo.current(idx)
                self._load_midi_by_id(last_id, is_startup=True)

    # -- import / remove -----------------------------------------
    def _import_soundfont(self):
        path = filedialog.askopenfilename(
            title="Select a SoundFont",
            filetypes=[("SoundFont files", "*.sf2 *.sf3"), ("All files", "*.*")],
        )
        if not path:
            return
        entry = self.library.import_file("soundfont", path)
        self._refresh_soundfont_list(select_last=False)
        idx = self._sf_ids.index(entry["id"])
        self.sf_combo.current(idx)
        self._load_soundfont_by_id(entry["id"])

    def _import_midi(self):
        path = filedialog.askopenfilename(
            title="Select a MIDI file",
            filetypes=[("MIDI files", "*.mid *.midi"), ("All files", "*.*")],
        )
        if not path:
            return
        entry = self.library.import_file("midi", path)
        self._refresh_midi_list(select_last=False)
        idx = self._midi_ids.index(entry["id"])
        self.midi_combo.current(idx)
        self._load_midi_by_id(entry["id"])

    def _import_via_link(self, kind):
        label = "SoundFont" if kind == "soundfont" else "MIDI"
        url = simpledialog.askstring(
            APP_TITLE,
            f"Paste a direct download link to a {label} file:",
            parent=self.root,
        )
        if not url:
            return
        url = url.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            messagebox.showerror(APP_TITLE, "That doesn't look like a valid http(s) link.")
            return

        display_name = guess_name_from_url(url)
        ext = guess_extension_from_url(url, kind)
        fd, temp_path = tempfile.mkstemp(suffix=ext, prefix="rscmp_dl_")
        os.close(fd)

        progress = DownloadProgressDialog(self.root, f"Downloading {label}...")

        def on_progress(downloaded, total):
            self.root.after(0, progress.update_progress, downloaded, total)

        def worker():
            try:
                download_url_to_file(url, temp_path, kind, progress_cb=on_progress)
                entry = self.library.import_file(kind, temp_path, display_name=display_name)
            except Exception as exc:
                self.root.after(0, progress.close)
                self.root.after(0, lambda: messagebox.showerror(APP_TITLE, f"Download failed:\n{exc}"))
                return
            finally:
                try:
                    if os.path.isfile(temp_path):
                        os.remove(temp_path)
                except OSError:
                    pass

            def finish():
                progress.close()
                if kind == "soundfont":
                    self._refresh_soundfont_list(select_last=False)
                    idx = self._sf_ids.index(entry["id"])
                    self.sf_combo.current(idx)
                    self._load_soundfont_by_id(entry["id"])
                else:
                    self._refresh_midi_list(select_last=False)
                    idx = self._midi_ids.index(entry["id"])
                    self.midi_combo.current(idx)
                    self._load_midi_by_id(entry["id"])

            self.root.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def _remove_soundfont(self):
        sel = self.sf_combo.current()
        if sel < 0:
            return
        entry_id = self._sf_ids[sel]
        entry = self.library.get("soundfont", entry_id)
        if not messagebox.askyesno(
            APP_TITLE, f"Remove '{entry['name']}' from your library?\n\n"
            "This deletes the copy stored in the Soundfonts folder."
        ):
            return
        self.library.remove("soundfont", entry_id)
        self.sf_combo.set("")
        self._refresh_soundfont_list(select_last=False)

    def _remove_midi(self):
        sel = self.midi_combo.current()
        if sel < 0:
            return
        entry_id = self._midi_ids[sel]
        entry = self.library.get("midi", entry_id)
        if not messagebox.askyesno(
            APP_TITLE, f"Remove '{entry['name']}' from your library?\n\n"
            "This deletes the copy stored in the Midis folder."
        ):
            return
        self.library.remove("midi", entry_id)
        self.midi_combo.set("")
        self._refresh_midi_list(select_last=False)

    # -- selection handlers -----------------------------------------
    def _on_soundfont_selected(self, _event):
        sel = self.sf_combo.current()
        if sel < 0:
            return
        self._load_soundfont_by_id(self._sf_ids[sel])

    def _on_midi_selected(self, _event):
        sel = self.midi_combo.current()
        if sel < 0:
            return
        self._load_midi_by_id(self._midi_ids[sel])

    def _load_soundfont_by_id(self, entry_id, is_startup=False):
        path = self.library.full_path("soundfont", entry_id)
        entry = self.library.get("soundfont", entry_id)
        if path is None or not os.path.isfile(path):
            messagebox.showerror(APP_TITLE, "That library file is missing on disk.")
            return
        self.status_var.set(f"Loading SoundFont: {entry['name']}...")

        def worker():
            try:
                self.engine.load_soundfont(path)
                self.library.set_last("soundfont", entry_id)

                def on_success():
                    self.status_var.set(f"Loaded SoundFont: {entry['name']}")
                    self._current_sf_id = entry_id
                    self._maybe_apply_saved_overrides()

                self.root.after(0, on_success)
            except Exception as exc:
                self.root.after(0, lambda: messagebox.showerror(APP_TITLE, f"Failed to load SoundFont:\n{exc}"))
                self.root.after(0, lambda: self.status_var.set("Ready."))

        threading.Thread(target=worker, daemon=True).start()

    def _load_midi_by_id(self, entry_id, is_startup=False):
        path = self.library.full_path("midi", entry_id)
        entry = self.library.get("midi", entry_id)
        if path is None or not os.path.isfile(path):
            messagebox.showerror(APP_TITLE, "That library file is missing on disk.")
            return
        try:
            self.engine.load_midi(path)
            self.library.set_last("midi", entry_id)
            self.status_var.set(f"Loaded MIDI: {entry['name']}")
            self.seek_scale.set(0)
            self.time_var.set(f"00:00 / {_fmt_time(self.engine.total_time)}")
            self._current_midi_id = entry_id
            self._maybe_apply_saved_overrides()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Failed to load MIDI:\n{exc}")

    def _maybe_apply_saved_overrides(self):
        """Whenever the loaded SoundFont+MIDI pairing changes, load whatever
        per-channel overrides were previously saved for that exact pairing
        (or clear them, if this is a pairing with none saved)."""
        if self._current_sf_id and self._current_midi_id:
            overrides = self.library.get_channel_overrides(self._current_midi_id, self._current_sf_id)
        else:
            overrides = {}
        self.engine.apply_overrides_now(overrides)

    # -- transport UI -----------------------------------------
    def _toggle_play(self):
        try:
            if self.engine.is_playing() and not self.engine.is_paused():
                self.engine.pause()
                self.play_btn.config(text="Play")
                self.status_var.set("Paused.")
            elif self.engine.is_playing() and self.engine.is_paused():
                self.engine.resume()
                self.play_btn.config(text="Pause")
                self.status_var.set("Playing.")
            else:
                self.engine.play()
                self.play_btn.config(text="Pause")
                self.status_var.set("Playing.")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _stop(self):
        self.engine.stop()
        self.play_btn.config(text="Play")
        self.seek_scale.set(0)
        total = self.engine.total_time
        self.time_var.set(f"00:00 / {_fmt_time(total)}")
        self.status_var.set("Stopped.")

    def _open_export_dialog(self):
        if not self.engine.soundfont_path:
            messagebox.showerror(APP_TITLE, "Load a SoundFont first.")
            return
        if not self.engine.events:
            messagebox.showerror(APP_TITLE, "Load a MIDI file first.")
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("Export")
        dialog.configure(bg=COLOR_BG)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        pad = {"padx": 16, "pady": 6}
        fmt_var = tk.StringVar(value="wav")
        bitrate_var = tk.StringVar(value="192")

        ttk.Label(dialog, text="Export current SoundFont + MIDI as:").pack(anchor="w", **pad)

        frm_fmt = ttk.Frame(dialog)
        frm_fmt.pack(anchor="w", padx=16)
        ttk.Radiobutton(frm_fmt, text="WAV (uncompressed)", value="wav", variable=fmt_var,
                         command=lambda: on_fmt_change()).pack(anchor="w")
        mp3_state = "normal" if lameenc is not None else "disabled"
        ttk.Radiobutton(frm_fmt, text="MP3", value="mp3", variable=fmt_var,
                         command=lambda: on_fmt_change(), state=mp3_state).pack(anchor="w")
        if lameenc is None:
            ttk.Label(frm_fmt, text="(MP3 unavailable: lameenc not installed)", foreground="#ff8080").pack(anchor="w")

        frm_bitrate = ttk.Frame(dialog)
        frm_bitrate.pack(anchor="w", **pad)
        bitrate_label = ttk.Label(frm_bitrate, text="Bitrate:")
        bitrate_label.pack(side="left")
        bitrate_combo = ttk.Combobox(
            frm_bitrate, textvariable=bitrate_var, state="readonly", width=8,
            values=["128", "192", "256", "320"],
        )
        bitrate_combo.pack(side="left", padx=8)

        def on_fmt_change():
            enabled = fmt_var.get() == "mp3"
            state = "readonly" if enabled else "disabled"
            bitrate_combo.config(state=state)

        on_fmt_change()

        frm_buttons = ttk.Frame(dialog)
        frm_buttons.pack(pady=(6, 16))

        def do_export():
            fmt = fmt_var.get()
            ext = ".mp3" if fmt == "mp3" else ".wav"
            filetypes = [("MP3 audio", "*.mp3")] if fmt == "mp3" else [("WAV audio", "*.wav")]
            save_path = filedialog.asksaveasfilename(
                title="Export as",
                defaultextension=ext,
                filetypes=filetypes + [("All files", "*.*")],
            )
            if not save_path:
                return
            bitrate = int(bitrate_var.get())
            dialog.destroy()
            self._run_export(fmt, save_path, bitrate)

        ttk.Button(frm_buttons, text="Export...", command=do_export).pack(side="left", padx=6)
        ttk.Button(frm_buttons, text="Cancel", command=dialog.destroy).pack(side="left", padx=6)

        dialog.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() // 2) - (dialog.winfo_width() // 2)
        y = self.root.winfo_rooty() + (self.root.winfo_height() // 2) - (dialog.winfo_height() // 2)
        dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _run_export(self, fmt, save_path, bitrate):
        cancel_event = threading.Event()
        progress = ProgressDialog(
            self.root, "Rendering audio...", allow_cancel=True,
            on_cancel=cancel_event.set,
        )

        def progress_cb(frac):
            self.root.after(0, progress.set_fraction, frac)

        def worker():
            try:
                pcm, sample_rate = self.engine.render_offline(
                    progress_cb=progress_cb, cancel_check=cancel_event.is_set
                )
                if pcm is None:
                    self.root.after(0, progress.close)
                    self.root.after(0, lambda: self.status_var.set("Export cancelled."))
                    return

                self.root.after(0, progress.set_status, "Encoding...")
                if fmt == "mp3":
                    write_mp3_file(save_path, pcm, sample_rate=sample_rate, bitrate=bitrate)
                else:
                    write_wav_file(save_path, pcm, sample_rate=sample_rate)
            except Exception as exc:
                self.root.after(0, progress.close)
                self.root.after(0, lambda: messagebox.showerror(APP_TITLE, f"Export failed:\n{exc}"))
                return

            def finish():
                progress.close()
                self.status_var.set(f"Exported: {os.path.basename(save_path)}")

            self.root.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    # -- channel instrument overrides -----------------------------------
    def _open_channel_mixer(self):
        if not self.engine.soundfont_path or not self.engine.events:
            messagebox.showerror(APP_TITLE, "Load both a SoundFont and a MIDI file first.")
            return
        if not self._current_sf_id or not self._current_midi_id:
            messagebox.showerror(APP_TITLE, "Still loading -- try again in a moment.")
            return

        sf_entry_id = self._current_sf_id
        midi_entry_id = self._current_midi_id

        dialog = tk.Toplevel(self.root)
        dialog.title("Channel Instruments")
        dialog.configure(bg=COLOR_BG)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.geometry("620x440")

        status_var = tk.StringVar(value="Scanning instruments in the loaded SoundFont...")
        ttk.Label(dialog, textvariable=status_var, wraplength=580, justify="left").pack(
            anchor="w", padx=12, pady=(10, 4)
        )

        container = ttk.Frame(dialog)
        container.pack(fill="both", expand=True, padx=12, pady=6)

        canvas = tk.Canvas(container, borderwidth=0, highlightthickness=0, bg=COLOR_BG)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        rows_frame = ttk.Frame(canvas)
        rows_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=rows_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def on_mousewheel(event):
            canvas.yview_scroll(-1 * (event.delta // 120), "units")
        canvas.bind_all("<MouseWheel>", on_mousewheel)

        btn_row = ttk.Frame(dialog)
        btn_row.pack(pady=(4, 10))
        ttk.Button(
            btn_row, text="Reset All to MIDI Defaults",
            command=lambda: reset_all(),
        ).pack(side="left", padx=6)
        ttk.Button(btn_row, text="Close", command=dialog.destroy).pack(side="left", padx=6)

        def on_dialog_close():
            canvas.unbind_all("<MouseWheel>")
            dialog.destroy()
        dialog.protocol("WM_DELETE_WINDOW", on_dialog_close)

        def reset_all():
            self.library.clear_all_channel_overrides(midi_entry_id, sf_entry_id)
            self.engine.apply_overrides_now({})
            for child in rows_frame.winfo_children():
                child.destroy()
            build_rows(self._preset_cache.get(sf_entry_id, []))

        def build_rows(presets):
            for child in rows_frame.winfo_children():
                child.destroy()

            preset_labels = [f"Bank {b} / Preset {p}: {n}" for b, p, n in presets]
            preset_lookup = {label: (b, p) for label, (b, p, _n) in zip(preset_labels, presets)}

            channel_programs = summarize_channel_programs(self.engine.events)
            current_overrides = self.library.get_channel_overrides(midi_entry_id, sf_entry_id)

            if not channel_programs:
                ttk.Label(rows_frame, text="This MIDI file doesn't use any channels?").pack(padx=8, pady=8)

            for ch in sorted(channel_programs.keys()):
                bank, program = channel_programs[ch]
                declared_name = None
                if self.engine.synth is not None and self.engine.sfid is not None:
                    try:
                        declared_name = self.engine.synth.sfpreset_name(self.engine.sfid, bank, program)
                    except Exception:
                        declared_name = None

                if declared_name:
                    declared_label = declared_name
                elif ch == 9 and bank == 0 and program == 0:
                    declared_label = "Percussion (channel 10 default)"
                else:
                    declared_label = f"Bank {bank} / Preset {program} (not defined in this SoundFont!)"

                row = ttk.Frame(rows_frame)
                row.pack(fill="x", pady=3, padx=4)
                ttk.Label(row, text=f"Ch {ch + 1}", width=6).pack(side="left")
                ttk.Label(row, text=declared_label, width=32, anchor="w").pack(side="left")

                combo = ttk.Combobox(row, state="readonly", width=28, values=["(MIDI default)"] + preset_labels)
                override = current_overrides.get(ch)
                matched = False
                if override:
                    for label, bp in preset_lookup.items():
                        if bp == override:
                            combo.set(label)
                            matched = True
                            break
                if not matched:
                    combo.current(0)
                combo.pack(side="left", padx=6)

                def on_change(_event, ch=ch, combo=combo):
                    label = combo.get()
                    if label == "(MIDI default)":
                        self.library.clear_channel_override(midi_entry_id, sf_entry_id, ch)
                    else:
                        bank_p, preset_p = preset_lookup[label]
                        self.library.set_channel_override(midi_entry_id, sf_entry_id, ch, bank_p, preset_p)
                    overrides = self.library.get_channel_overrides(midi_entry_id, sf_entry_id)
                    self.engine.apply_overrides_now(overrides)

                combo.bind("<<ComboboxSelected>>", on_change)

            status_var.set(
                f"{len(presets)} instrument(s) found in this SoundFont. "
                "Changes apply immediately (even mid-playback) and are remembered "
                "for this song + SoundFont pairing."
            )

        def worker():
            if sf_entry_id in self._preset_cache:
                presets = self._preset_cache[sf_entry_id]
            else:
                presets = enumerate_soundfont_presets(self.engine.synth, self.engine.sfid)
                self._preset_cache[sf_entry_id] = presets
            self.root.after(0, build_rows, presets)

        threading.Thread(target=worker, daemon=True).start()

    def _on_seek_drag(self, _value):
        pass  # actual seek happens on release, see below

    def _on_seek_release(self, _event):
        self._seeking = False
        if self.engine.total_time <= 0:
            return
        frac = self.seek_scale.get() / 1000.0
        target = frac * self.engine.total_time
        self.engine.seek(target)

    def _on_volume(self, value):
        self.engine.set_volume(float(value))

    # -- engine callbacks (called from worker thread -> marshal to UI thread)
    def _on_position_update(self, pos, total):
        self.root.after(0, self._update_time_label, pos, total)

    def _update_time_label(self, pos, total):
        if self._seeking:
            return
        self.time_var.set(f"{_fmt_time(pos)} / {_fmt_time(total)}")
        if total > 0:
            self.seek_scale.set((pos / total) * 1000.0)

    def _on_finished(self):
        self.root.after(0, self._handle_finished)

    def _handle_finished(self):
        self.play_btn.config(text="Play")
        self.status_var.set("Finished.")

    def _on_close(self):
        self.engine.shutdown()
        self.root.destroy()

    # -- self-update -----------------------------------------
    def _check_for_updates(self, manual=False):
        """Ask GitHub whether a newer release exists. manual=False (the
        startup check) is silent about every kind of failure -- offline, the
        repo still being private, whatever -- and only ever shows anything
        when a genuinely newer version is found and the user hasn't already
        dismissed that exact version. manual=True (the Help menu item)
        always reports a result, including errors, since the user asked."""
        def worker():
            try:
                info = fetch_latest_release()
            except Exception as exc:
                if manual:
                    self.root.after(0, lambda: messagebox.showinfo(
                        APP_TITLE, f"Couldn't check for updates:\n\n{exc}"))
                return

            current = _parse_semver(__version__) or (0, 0, 0)
            if info["version"] <= current:
                if manual:
                    self.root.after(0, lambda: messagebox.showinfo(
                        APP_TITLE, f"You're up to date (v{__version__})."))
                return

            if not manual and info["tag"] == self.library.data.get("skipped_update_version"):
                return

            self.root.after(0, self._prompt_update, info)

        threading.Thread(target=worker, daemon=True).start()

    def _prompt_update(self, info):
        msg = f"A new version is available: {info['tag']} (you have v{__version__})."
        if info["notes"]:
            msg += "\n\nWhat's new:\n" + info["notes"][:600]
        msg += "\n\nUpdate now? The app will close and reopen on the new version."

        if not messagebox.askyesno(APP_TITLE, msg):
            self.library.data["skipped_update_version"] = info["tag"]
            self.library.save()
            return

        if not getattr(sys, "frozen", False):
            messagebox.showinfo(
                APP_TITLE,
                "You're running from source, not the built .exe, so there's "
                "nothing here for the app to replace itself with.\n\n"
                f"Grab the new version manually from:\n{APP_REPO_URL}/releases",
            )
            return

        self._download_and_apply_update(info)

    def _download_and_apply_update(self, info):
        progress = DownloadProgressDialog(self.root, f"Downloading {info['tag']}...")

        def on_progress(downloaded, total):
            self.root.after(0, progress.update_progress, downloaded, total)

        def worker():
            try:
                req = urllib.request.Request(
                    info["download_url"], headers={"User-Agent": DOWNLOAD_USER_AGENT}
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    total = resp.headers.get("Content-Length")
                    total = int(total) if total and total.isdigit() else None
                    chunks = []
                    downloaded = 0
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        downloaded += len(chunk)
                        on_progress(downloaded, total)
                    data = b"".join(chunks)

                if total is not None and downloaded != total:
                    raise RuntimeError(
                        f"Download was incomplete ({downloaded} of {total} bytes arrived) "
                        "-- your current version hasn't been touched. Try again."
                    )

                self.root.after(0, progress.set_status, "Applying update...")
                start_self_update(data, info["version_str"])
            except Exception as exc:
                self.root.after(0, progress.close)
                self.root.after(0, lambda: messagebox.showerror(APP_TITLE, f"Update failed:\n{exc}"))
                return

            self.root.after(0, self._quit_for_update)

        threading.Thread(target=worker, daemon=True).start()

    def _quit_for_update(self):
        self.engine.shutdown()
        self.root.destroy()

    # -- about -----------------------------------------
    def _open_about_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title(f"About {APP_TITLE}")
        dialog.configure(bg=COLOR_BG)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        pad = {"padx": 24, "pady": 4}
        ttk.Label(dialog, text=APP_TITLE, font=("", 14, "bold")).pack(padx=24, pady=(20, 2))
        ttk.Label(dialog, text=f"Version {__version__}").pack(**pad)
        ttk.Label(dialog, text="A portable player for MIDI files through any\nSoundFont: game soundfonts, retro fonts, or anything else.",
                  justify="center").pack(padx=24, pady=(4, 10))
        ttk.Separator(dialog, orient="horizontal").pack(fill="x", padx=20, pady=4)
        ttk.Label(dialog, text=f"Created by {APP_AUTHOR}").pack(**pad)
        ttk.Label(dialog, text=f"github.com/{APP_GITHUB_USER}", foreground=COLOR_ACCENT_ACTIVE, cursor="hand2").pack(**pad)

        def open_repo(_event=None):
            webbrowser.open(APP_REPO_URL)

        for child in dialog.winfo_children():
            if isinstance(child, ttk.Label) and child.cget("text") == f"github.com/{APP_GITHUB_USER}":
                child.bind("<Button-1>", open_repo)

        ttk.Label(dialog, text="Licensed under the MIT License.", foreground=COLOR_TEXT_MUTED).pack(pady=(6, 4))

        ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=(6, 18))

        dialog.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() // 2) - (dialog.winfo_width() // 2)
        y = self.root.winfo_rooty() + (self.root.winfo_height() // 2) - (dialog.winfo_height() // 2)
        dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")


def _fmt_time(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def get_icon_path():
    base_dir = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))  # type: ignore[attr-defined]
    path = os.path.join(base_dir, "icon.ico")
    return path if os.path.isfile(path) else None


def apply_theme(root):
    """Reskin every widget with a light, modern take on stock Windows
    colors -- white/light-gray surfaces, Windows' own accent blue, dark
    neutral text -- instead of native "vista" chrome (which ignores almost
    all color styling) or the app's old heavy purple/gold look. "clam" is a
    fully tk-drawn ttk theme, so every color below actually takes effect."""
    root.configure(bg=COLOR_BG)

    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure(".", background=COLOR_BG, foreground=COLOR_TEXT,
                     fieldbackground=COLOR_BG_PANEL, bordercolor=COLOR_BORDER,
                     darkcolor=COLOR_BG_PANEL, lightcolor=COLOR_BG_PANEL,
                     troughcolor=COLOR_BG_ALT, focuscolor=COLOR_ACCENT,
                     font=UI_FONT)

    style.configure("TFrame", background=COLOR_BG)
    style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT, font=UI_FONT)

    style.configure("TLabelframe", background=COLOR_BG, bordercolor=COLOR_BORDER, relief="solid")
    style.configure("TLabelframe.Label", background=COLOR_BG, foreground=COLOR_TEXT_MUTED,
                     font=UI_FONT_BOLD)

    # Secondary (default) buttons: light, bordered, native-ish.
    style.configure("TButton", background=COLOR_BG_PANEL, foreground=COLOR_TEXT,
                     bordercolor=COLOR_BORDER, relief="solid", borderwidth=1,
                     focusthickness=0, padding=(12, 6), font=UI_FONT)
    style.map("TButton",
              background=[("disabled", COLOR_DISABLED_BG), ("pressed", COLOR_BG_ALT),
                          ("active", COLOR_BG_ALT)],
              foreground=[("disabled", COLOR_DISABLED_FG)],
              bordercolor=[("active", COLOR_ACCENT)])

    # Primary (accent-filled) button, used for the main Play/Pause action.
    style.configure("Accent.TButton", background=COLOR_ACCENT, foreground="#ffffff",
                     bordercolor=COLOR_ACCENT, relief="flat", focusthickness=0,
                     padding=(14, 6), font=UI_FONT_BOLD)
    style.map("Accent.TButton",
              background=[("disabled", COLOR_DISABLED_BG), ("pressed", COLOR_ACCENT_DARK),
                          ("active", COLOR_ACCENT_ACTIVE)],
              bordercolor=[("disabled", COLOR_DISABLED_BG)],
              foreground=[("disabled", COLOR_DISABLED_FG)])

    # Square icon+label button (Channels...), same look as a normal button,
    # just squarer padding so the icon-on-top layout reads as one unit.
    style.configure("Square.TButton", padding=(10, 6), anchor="center")

    style.configure("TCombobox", fieldbackground=COLOR_BG_PANEL, background=COLOR_BG_PANEL,
                     foreground=COLOR_TEXT, arrowcolor=COLOR_TEXT_MUTED, bordercolor=COLOR_BORDER,
                     selectbackground=COLOR_BG_PANEL, selectforeground=COLOR_TEXT)
    style.map("TCombobox",
              fieldbackground=[("readonly", COLOR_BG_PANEL), ("disabled", COLOR_DISABLED_BG)],
              foreground=[("disabled", COLOR_DISABLED_FG)],
              background=[("disabled", COLOR_DISABLED_BG)],
              bordercolor=[("focus", COLOR_ACCENT)])
    # The Combobox dropdown list is a plain Tk Listbox under the hood, not
    # themeable via ttk.Style -- set it through the classic option database.
    root.option_add("*TCombobox*Listbox.background", COLOR_BG_PANEL)
    root.option_add("*TCombobox*Listbox.foreground", COLOR_TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", COLOR_ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

    style.configure("Horizontal.TScale", background=COLOR_BG, troughcolor=COLOR_BG_ALT,
                     bordercolor=COLOR_BG_ALT, sliderlength=14, sliderthickness=14)
    style.map("Horizontal.TScale", background=[("active", COLOR_BG)])

    style.configure("TSeparator", background=COLOR_BORDER)

    style.configure("Horizontal.TProgressbar", background=COLOR_ACCENT,
                     troughcolor=COLOR_BG_ALT, bordercolor=COLOR_BG_ALT, lightcolor=COLOR_ACCENT,
                     darkcolor=COLOR_ACCENT, thickness=8)

    style.configure("Vertical.TScrollbar", background=COLOR_BG_ALT, troughcolor=COLOR_BG,
                     bordercolor=COLOR_BG, arrowcolor=COLOR_TEXT_MUTED)
    style.map("Vertical.TScrollbar", background=[("active", COLOR_ACCENT)])

    style.configure("TRadiobutton", background=COLOR_BG, foreground=COLOR_TEXT, font=UI_FONT)
    style.map("TRadiobutton", background=[("active", COLOR_BG)])


def style_menu(menu):
    """tk.Menu is a classic Tk widget (not ttk), so it needs its colors set
    directly rather than through ttk.Style."""
    menu.configure(
        bg=COLOR_BG_PANEL, fg=COLOR_TEXT,
        activebackground=COLOR_ACCENT, activeforeground="#ffffff",
        borderwidth=0, font=UI_FONT,
    )


def make_piano_icon(size=28):
    """Draw a tiny piano-keys icon at runtime (no bundled image asset needed
    -- just a handful of PhotoImage.put() pixel-rect fills), used on the
    square Channels button."""
    img = tk.PhotoImage(width=size, height=size)
    img.put(COLOR_BG_PANEL, to=(0, 0, size, size))

    border = COLOR_TEXT_MUTED
    img.put(border, to=(0, 0, size, 2))
    img.put(border, to=(0, size - 2, size, size))
    img.put(border, to=(0, 0, 2, size))
    img.put(border, to=(size - 2, 0, size, size))

    # A row of thin "key" dividers across the middle -- reads as piano keys
    # at small sizes without needing real key-shaped geometry.
    key_color = COLOR_TEXT
    margin = 5
    n_dividers = 4
    usable = size - margin * 2
    for i in range(1, n_dividers + 1):
        x = margin + round(i * usable / (n_dividers + 1))
        img.put(key_color, to=(x, margin, x + 1, size - margin))

    return img


def main():
    root = tk.Tk()
    apply_theme(root)
    icon_path = get_icon_path()
    if icon_path:
        try:
            root.iconbitmap(icon_path)
        except Exception:
            pass
    app = PlayerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
