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
# Color palette matching the app icon (deep purple + gold), used to reskin
# every widget away from the stock Windows ("vista" theme) look.
# ---------------------------------------------------------------------------
COLOR_BG = "#2b1a4a"          # main background, deep purple
COLOR_BG_PANEL = "#3a2560"    # slightly lighter panels/fields
COLOR_BG_ALT = "#241640"      # recessed areas (canvases, troughs)
COLOR_ACCENT = "#e0ab3c"      # gold -- buttons, highlights
COLOR_ACCENT_ACTIVE = "#f2c15c"  # gold, hover/active
COLOR_ACCENT_DARK = "#a97c22" # gold, pressed/border
COLOR_TEXT = "#f3ead9"        # warm off-white text
COLOR_TEXT_MUTED = "#c3b3dd"  # muted lavender text (status lines, captions)
COLOR_BORDER = "#7a5aa8"      # lavender borders
COLOR_DISABLED_BG = "#4a3a70"
COLOR_DISABLED_FG = "#8f80ac"
DOWNLOAD_USER_AGENT = f"Mozilla/5.0 (compatible; RSC-MIDI-Player/{__version__})"
MIDI_MAGIC = b"MThd"


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
        self.root.geometry("600x360")
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

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        # -- Menu bar --
        menubar = tk.Menu(self.root)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About RSC MIDI Player", command=self._open_about_dialog)
        menubar.add_cascade(label="Help", menu=help_menu)
        style_menu(menubar)
        style_menu(help_menu)
        self.root.config(menu=menubar)

        # -- SoundFont row --
        frm_sf = ttk.LabelFrame(self.root, text="SoundFont")
        frm_sf.pack(fill="x", **pad)
        self.sf_combo = ttk.Combobox(frm_sf, state="readonly", width=28)
        self.sf_combo.grid(row=0, column=0, padx=(8, 4), pady=8, sticky="w")
        self.sf_combo.bind("<<ComboboxSelected>>", self._on_soundfont_selected)
        ttk.Button(frm_sf, text="Import...", command=self._import_soundfont).grid(
            row=0, column=1, padx=4
        )
        ttk.Button(frm_sf, text="Add via URL...", command=lambda: self._import_via_link("soundfont")).grid(
            row=0, column=2, padx=4
        )
        ttk.Button(frm_sf, text="Remove", command=self._remove_soundfont).grid(
            row=0, column=3, padx=(4, 8)
        )

        # -- MIDI row --
        frm_midi = ttk.LabelFrame(self.root, text="MIDI")
        frm_midi.pack(fill="x", **pad)
        self.midi_combo = ttk.Combobox(frm_midi, state="readonly", width=28)
        self.midi_combo.grid(row=0, column=0, padx=(8, 4), pady=8, sticky="w")
        self.midi_combo.bind("<<ComboboxSelected>>", self._on_midi_selected)
        ttk.Button(frm_midi, text="Import...", command=self._import_midi).grid(
            row=0, column=1, padx=4
        )
        ttk.Button(frm_midi, text="Add via URL...", command=lambda: self._import_via_link("midi")).grid(
            row=0, column=2, padx=4
        )
        ttk.Button(frm_midi, text="Remove", command=self._remove_midi).grid(
            row=0, column=3, padx=(4, 8)
        )

        # -- Seek --
        frm_seek = ttk.Frame(self.root)
        frm_seek.pack(fill="x", **pad)
        self.seek_scale = ttk.Scale(
            frm_seek, from_=0, to=1000, orient="horizontal",
            command=self._on_seek_drag,
        )
        self.seek_scale.pack(fill="x")
        self.seek_scale.bind("<ButtonPress-1>", lambda e: setattr(self, "_seeking", True))
        self.seek_scale.bind("<ButtonRelease-1>", self._on_seek_release)

        ttk.Label(self.root, textvariable=self.time_var).pack()

        # -- Transport --
        frm_controls = ttk.Frame(self.root)
        frm_controls.pack(**pad)
        self.play_btn = ttk.Button(frm_controls, text="Play", command=self._toggle_play, width=10)
        self.play_btn.grid(row=0, column=0, padx=4)
        ttk.Button(frm_controls, text="Stop", command=self._stop, width=10).grid(
            row=0, column=1, padx=4
        )
        ttk.Button(frm_controls, text="Export...", command=self._open_export_dialog, width=10).grid(
            row=0, column=2, padx=4
        )
        ttk.Button(frm_controls, text="Channels...", command=self._open_channel_mixer, width=10).grid(
            row=0, column=3, padx=4
        )

        # -- Volume --
        frm_vol = ttk.Frame(self.root)
        frm_vol.pack(fill="x", **pad)
        ttk.Label(frm_vol, text="Volume").pack(side="left")
        self.vol_scale = ttk.Scale(
            frm_vol, from_=0, to=100, orient="horizontal", command=self._on_volume
        )
        self.vol_scale.set(50)
        self.vol_scale.pack(side="left", fill="x", expand=True, padx=8)

        ttk.Label(self.root, textvariable=self.status_var, foreground=COLOR_TEXT_MUTED).pack(
            side="bottom", fill="x", padx=10, pady=(0, 8)
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
    """Reskin every widget to the app's own purple/gold look instead of the
    stock OS theme ("vista" on Windows just borrows native, Windows-styled
    controls). "clam" is a fully tk-drawn ttk theme, so every color below
    actually takes effect instead of being ignored in favor of native
    rendering."""
    root.configure(bg=COLOR_BG)

    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure(".", background=COLOR_BG, foreground=COLOR_TEXT,
                     fieldbackground=COLOR_BG_PANEL, bordercolor=COLOR_BORDER,
                     darkcolor=COLOR_BG_PANEL, lightcolor=COLOR_BG_PANEL,
                     troughcolor=COLOR_BG_ALT, focuscolor=COLOR_ACCENT)

    style.configure("TFrame", background=COLOR_BG)
    style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT)

    style.configure("TLabelframe", background=COLOR_BG, bordercolor=COLOR_BORDER, relief="solid")
    style.configure("TLabelframe.Label", background=COLOR_BG, foreground=COLOR_ACCENT,
                     font=("", 9, "bold"))

    style.configure("TButton", background=COLOR_ACCENT, foreground=COLOR_BG,
                     bordercolor=COLOR_ACCENT_DARK, relief="flat", focusthickness=0,
                     padding=(10, 5))
    style.map("TButton",
              background=[("disabled", COLOR_DISABLED_BG), ("pressed", COLOR_ACCENT_DARK),
                          ("active", COLOR_ACCENT_ACTIVE)],
              foreground=[("disabled", COLOR_DISABLED_FG)])

    style.configure("TCombobox", fieldbackground=COLOR_BG_PANEL, background=COLOR_ACCENT,
                     foreground=COLOR_TEXT, arrowcolor=COLOR_BG, bordercolor=COLOR_BORDER,
                     selectbackground=COLOR_BG_PANEL, selectforeground=COLOR_TEXT)
    style.map("TCombobox",
              fieldbackground=[("readonly", COLOR_BG_PANEL), ("disabled", COLOR_DISABLED_BG)],
              foreground=[("disabled", COLOR_DISABLED_FG)],
              background=[("disabled", COLOR_DISABLED_BG)])
    # The Combobox dropdown list is a plain Tk Listbox under the hood, not
    # themeable via ttk.Style -- set it through the classic option database.
    root.option_add("*TCombobox*Listbox.background", COLOR_BG_PANEL)
    root.option_add("*TCombobox*Listbox.foreground", COLOR_TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", COLOR_ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", COLOR_BG)

    style.configure("Horizontal.TScale", background=COLOR_BG, troughcolor=COLOR_BG_ALT,
                     bordercolor=COLOR_BORDER)
    style.map("Horizontal.TScale", background=[("active", COLOR_ACCENT)])

    style.configure("TSeparator", background=COLOR_BORDER)

    style.configure("Horizontal.TProgressbar", background=COLOR_ACCENT,
                     troughcolor=COLOR_BG_ALT, bordercolor=COLOR_BORDER, lightcolor=COLOR_ACCENT,
                     darkcolor=COLOR_ACCENT_DARK)

    style.configure("Vertical.TScrollbar", background=COLOR_ACCENT, troughcolor=COLOR_BG_ALT,
                     bordercolor=COLOR_BORDER, arrowcolor=COLOR_BG)
    style.map("Vertical.TScrollbar", background=[("active", COLOR_ACCENT_ACTIVE)])

    style.configure("TRadiobutton", background=COLOR_BG, foreground=COLOR_TEXT)
    style.map("TRadiobutton", background=[("active", COLOR_BG)])


def style_menu(menu):
    """tk.Menu is a classic Tk widget (not ttk), so it needs its colors set
    directly rather than through ttk.Style."""
    menu.configure(
        bg=COLOR_BG_PANEL, fg=COLOR_TEXT,
        activebackground=COLOR_ACCENT, activeforeground=COLOR_BG,
        borderwidth=0,
    )


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
