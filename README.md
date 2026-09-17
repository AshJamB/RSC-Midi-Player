# RSC MIDI Player

A simple, portable, media-player-style app for playing MIDI files through any
SoundFont (.sf2) -- RuneScape, Banjo-Kazooie, GXSCC 8-bit fonts, whatever
you've collected. No Java, no hunting through someone else's GitHub repo for
a bundled font -- you load your own SoundFonts and MIDI files, and it
remembers them for next time.

Created by Ash Brittain ([AshJamB](https://github.com/AshJamB)).

## What's in this folder

- `rsc_midi_player.py` - the whole app (Tkinter GUI + FluidSynth playback engine)
- `requirements.txt` - Python packages needed
- `build_exe.py` - packages the app into a single portable `.exe`
- `README.md` - this file
- `LICENSE` - MIT license
- `icon.ico` - the app's icon, used for the built `.exe` (see **The icon** below)
- `.github/workflows/release.yml` - builds and publishes a GitHub Release automatically whenever you push to `main` (see **Releasing a new version** below)

## Important: building the .exe must happen on Windows

PyInstaller (the tool that turns the Python script into a `.exe`) does not
cross-compile. It has to be run on the same OS you're targeting. So to get a
Windows `.exe`, you'll run the build step yourself on your Windows machine --
it only takes a few minutes and the steps below are copy-paste.

You can also just run `python rsc_midi_player.py` directly on Windows without
ever building an exe, if you're comfortable having Python installed.

## Step 1: Install Python (if you don't have it)

Download Python 3.10 or newer from https://python.org (check "Add python.exe
to PATH" during install).

## Step 2: Install the Python dependencies

Open PowerShell in this folder and run:

```
pip install -r requirements.txt
```

## Step 3: Build the exe

```
python build_exe.py
```

The app uses FluidSynth (a free, open-source SoundFont synthesizer) under the
hood for actual audio playback. `build_exe.py` fetches the FluidSynth engine
DLLs for you automatically -- straight from FluidSynth's official GitHub
releases (https://github.com/FluidSynth/fluidsynth/releases) -- into a `bin`
folder next to the script, then bundles them into the exe. You don't need to
find or download anything by hand. (If it ever can't reach GitHub, it'll
tell you and you can download the win10-x64 zip yourself and unzip its DLLs
into a `bin` folder next to `rsc_midi_player.py`.)

When it finishes, your portable app will be at:

```
dist\RSC-MIDI-Player.exe
```

Copy that one file to wherever you want to keep the app -- e.g. a dedicated
folder like `C:\Apps\RSC MIDI Player\`. That location matters a bit now (see
below), so pick somewhere you're happy for it to live and create its
sidecar folders.

## Your SoundFont/MIDI library -- how it's stored

The app remembers every SoundFont and MIDI you import, so you can pick them
from dropdowns instead of re-browsing your filesystem each time, and it
reopens with whatever you last had loaded.

Everything is kept **next to the .exe**, self-contained, nothing written
anywhere else on your system:

```
RSC MIDI Player\
  RSC-MIDI-Player.exe
  library.json          <- index of everything you've imported + last-used selection
  Soundfonts\           <- your imported .sf2/.sf3 files live here
  Midis\                <- your imported .mid files live here
```

When you click **Import...** for a SoundFont or MIDI, the app copies the file
into `Soundfonts\` or `Midis\` (so playback keeps working even if you later
move, rename, or delete the original file you imported from). If you import
the exact same file content twice, it recognizes the duplicate and reuses the
existing copy instead of storing it twice.

**To fully uninstall / clean up:** just delete the whole `RSC MIDI Player`
folder (exe + `library.json` + `Soundfonts\` + `Midis\`). Nothing is written
to `%APPDATA%`, the registry, or anywhere else on the system, so there's
nothing left behind elsewhere.

**If you move the exe** to a different folder, take the whole folder with it
(the sidecar files travel together) -- that's what makes it portable. If you
only copy the `.exe` on its own, it will just start a fresh, empty library
next to wherever you put it.

Use the **Remove** button next to each dropdown to delete an entry from your
library (this also deletes its copied file from `Soundfonts\`/`Midis\`).

## Using the app

1. Open `RSC-MIDI-Player.exe`.
2. Click **Import...** next to SoundFont and pick a `.sf2`/`.sf3` file (do
   this once per soundfont -- next time it'll just be in the dropdown), or
   click **Add via URL...** and paste a direct download URL to have the app
   fetch it for you.
3. Do the same for MIDI: **Import...** for a local `.mid` file, or
   **Add via URL...** to download one from a URL.
4. Pick from the dropdowns any time to switch between soundfonts/MIDIs
   you've already imported.
5. Click **Play**. Use the seek bar to jump around, and the volume slider to
   adjust loudness.
6. Click **Export...** to render the currently loaded SoundFont+MIDI
   combination out to a `.wav` or `.mp3` file -- pick a format (and bitrate,
   for MP3), choose where to save, and the app renders it in the background
   (this is offline rendering, done in one fast pass -- it does not play the
   file out loud while exporting, and normally finishes well before the
   track's actual runtime).

### Add via URL

"Add via URL..." downloads whatever URL you paste and checks that its
content actually looks like a MIDI file (starts with the `MThd` header) or an
SF2 SoundFont (starts with a `RIFF`/`sfbk` header) before adding it to your
library -- so a broken link, an HTML error page, or the wrong file type gets
rejected with a clear message instead of silently corrupting your library.
The link needs to point directly at the file (a URL that ends up serving the
raw bytes when fetched), not a webpage that merely links to a download
button.

### Export to WAV/MP3

Export renders the SoundFont+MIDI pairing you currently have loaded (not
necessarily whatever's selected in the dropdowns if you've since changed
selection -- reload the pairing you want first, then export). WAV export
always works out of the box. MP3 export uses the `lameenc` library, which
`build_exe.py`'s `pip install -r requirements.txt` step installs like any
other dependency -- no separate download needed.

### Fixing wrong-sounding instruments (Channel Instruments)

A MIDI file doesn't contain any actual sound -- it just says things like
"channel 3, use instrument #40." Your SoundFont is what maps that number to
an actual sample. If a SoundFont doesn't fully implement the General MIDI
instrument list (common with smaller, hand-made game SoundFonts), it
substitutes something else for numbers it doesn't define, which is what
"wrong instrument" usually is.

Click **Channels...** (needs both a SoundFont and a MIDI loaded first) to see
every channel the current song uses, what instrument it's defaulting to, and
a dropdown to pin that channel to a different instrument actually available
in your loaded SoundFont instead. Changes take effect immediately, even
mid-playback, so you can audition instruments while the song plays. Each
change is saved automatically, keyed to this exact song + SoundFont
pairing -- reload the same two later and your fixes are still there; a
different SoundFont with the same song (or vice versa) starts with a clean
slate, since a fix for one SoundFont's instrument list is meaningless for
another's. "Reset All to MIDI Defaults" clears every override for the
current pairing. Overrides also apply to Export, so what you hear is what
gets rendered to WAV/MP3.

## Checking for updates

The app can check GitHub for a newer release and update itself in place --
**Help > Check for Updates...** checks on demand, and it also checks quietly
in the background a couple seconds after launch (only ever popping something
up if a genuinely newer version is found, never for a failed check).

If it finds one, it asks before doing anything: choosing "Yes" downloads the
new release's zip, extracts the exe, and hands off to a tiny helper script
that waits for the app to close, swaps the old exe for the new one, and
reopens it -- so it comes back up already on the new version. Choosing "No"
just dismisses that version; it won't ask about that exact release again on
future launches (Help > Check for Updates... always re-checks regardless).

This only works while the repo is **public** -- checking for releases uses
GitHub's plain, unauthenticated API (the same request your browser makes
for a public repo's releases page), and this app deliberately doesn't store
any GitHub token to read a private repo's releases. While the repo stays
private, the startup check just fails quietly in the background and the app
carries on as normal; the manual "Check for Updates..." menu item will say
it couldn't find a release. If you make the repo public later, this starts
working immediately with no code or settings changes needed.

Running `python rsc_midi_player.py` from source (not the built `.exe`) can
still *check* for updates, but there's no exe for it to replace -- accepting
the prompt just points you to the Releases page instead.

## Releasing a new version

Releases are fully automatic -- there's no tagging step to remember. Just
commit and push to `main` (through GitHub Desktop or however you normally
commit) and, if the push touched `rsc_midi_player.py`, `build_exe.py`, or
`requirements.txt`, a GitHub Actions workflow builds the portable exe on a
Windows runner, figures out the right version number, tags it, zips it up
with the README and license, and publishes it as a GitHub Release marked
"Latest" -- all without you touching the Actions tab.

The version number is resolved automatically, first rule that matches wins:

1. A `[release X.Y.Z]` anywhere in your commit message -- an explicit
   one-off override, e.g. `git commit -m "fix drum channel [release 1.4.0]"`.
2. `__version__` in `rsc_midi_player.py`, if you've bumped it ahead of the
   last released version -- the normal way to cut a release with a specific
   number in mind. Bump it, commit, push, done.
3. Otherwise, the previous release's version with the patch number bumped
   by one automatically (`1.3.2` -> `1.3.3`) -- so even if you forget to
   touch the version entirely, pushing a change still ships a new release,
   it just won't have a deliberate version bump.

Whichever version wins gets written into the built exe, so the window
title/About dialog always match the release people actually downloaded,
even if you forgot to bump `__version__` yourself.

You can also trigger a release manually with an exact version from GitHub's
Actions tab (Release RSC MIDI Player > Run workflow > version field) if you
ever need to.

## The icon

`build_exe.py` embeds `icon.ico` into the built exe via PyInstaller's
`--icon` flag, so it shows up in Explorer, the taskbar, and the title bar
instead of PyInstaller's own generic default icon (every unconfigured
PyInstaller app gets that same default -- it's not unique to this project).
To swap it for a different icon, replace `icon.ico` with your own
multi-resolution `.ico` file (16/32/48/64/128/256px) of the same name and
rebuild; if `icon.ico` is ever missing, the build still works, it just falls
back to PyInstaller's default.

## The UI theme

The whole app is reskinned to match the icon's purple/gold palette instead
of looking like a stock Windows dialog. This needs the `clam` ttk theme
specifically -- Windows' native "vista" theme renders controls via the OS's
own theme engine, which looks native but ignores almost all color styling;
`clam` draws every widget itself, so the colors actually apply. The palette
lives as a handful of `COLOR_*` constants near the top of
`rsc_midi_player.py` (`apply_theme()`) -- tweak those to re-theme the whole
app in one place.

## Troubleshooting

**"FluidSynth could not be loaded" error on startup**
The `.dll` files aren't where the app expects them. Make sure you copied them
into `bin\` before running `build_exe.py` (they get baked into the exe at
build time), or, if running from source, into a `bin\` folder next to
`rsc_midi_player.py`.

**No sound, but no error either**
Windows sometimes needs the default playback device set correctly. Try
unplugging/replugging headphones or checking Windows sound settings. You can
also try changing the audio driver in `rsc_midi_player.py`'s
`PlaybackEngine.ensure_synth` method from `"dsound"` to `"waveout"` and
rebuilding.

**"That library file is missing on disk"**
Someone (or something) deleted a file out of `Soundfonts\`/`Midis\` without
going through the app's Remove button. Just re-import it.

**Antivirus flags the exe**
This is a common false positive for PyInstaller-built executables (since
they're a single unsigned binary that unpacks itself at runtime). It's not
inherent to this app; you can verify by reading the source in
`rsc_midi_player.py`. Code-signing the exe would resolve this but requires a
paid certificate.

## Notes on how it works (for future changes)

- `LibraryManager` (top of `rsc_midi_player.py`) owns `library.json` and the
  `Soundfonts\`/`Midis\` folders: importing, deduping by SHA-1 content hash,
  removing, and remembering the last-selected soundfont/MIDI.
- `mido` parses the MIDI file and resolves tempo-map timing into real seconds
  for every event.
- A background thread walks through those timed events and calls into
  FluidSynth (`note_on`, `note_off`, `control_change`, `program_change`,
  `pitch_bend`) at the right moments.
- Seeking works by silencing all channels, "replaying" only the non-audible
  state changes (patch/program/pan/etc.) up to the target time so the sound
  is correct when playback resumes, then continuing from there.
- Export renders offline: it uses a second, throwaway FluidSynth instance
  (never touching the one used for live playback) and calls
  `get_samples()` directly instead of playing through an audio device, so
  it produces the whole file in roughly the time it takes to compute the
  audio rather than the track's real length. WAV is written with the
  standard-library `wave` module; MP3 is encoded with `lameenc`.
- "Add via URL" streams the download to disk in chunks (so it doesn't
  need to hold a huge SoundFont entirely in memory) and checks the file's
  magic bytes as soon as enough of it has arrived, before committing to
  the rest of the download.
- Channel Instruments works by probing FluidSynth for every (bank, preset)
  combination that exists in the loaded SoundFont (there's no built-in
  "list all instruments" call, only "does this one exist") -- cheap enough
  to do in full every time a new SoundFont is scanned. An override just
  means "ignore this channel's own program-change/bank-select messages for
  the rest of the song and stay pinned to this instrument," applied on top
  of whatever the MIDI file says, for both live playback and export.
- Self-update hits GitHub's public `/releases/latest` REST endpoint with no
  auth header at all, so it only succeeds while the repo is public (a
  private repo just 404s, which the app treats as "no update available"
  rather than an error worth bothering you with on every launch). Applying
  an update downloads the release zip, pulls the `.exe` out of it, then
  writes and launches a small detached `.bat` file that sleeps briefly,
  moves the new exe over the running one, relaunches it, and deletes
  itself -- necessary because a running Windows exe can't overwrite its own
  file directly.
