"""
Build script that packages RSC MIDI Player into a single portable Windows
.exe using PyInstaller.

IMPORTANT: This must be run on a WINDOWS machine (PyInstaller does not
cross-compile). It will NOT work if run on Linux/Mac and produce a .exe.

This script automatically downloads the FluidSynth audio engine (the .dll
files the app needs to actually make sound) straight from FluidSynth's
official GitHub releases -- you don't need to go find/download them
yourself. It only does this once; if a ".\bin" folder with DLLs already
exists, it reuses it.

Usage (from PowerShell, in this folder):
    pip install -r requirements.txt
    python build_exe.py

Result:
    dist\\RSC-MIDI-Player.exe   <- portable, copy it anywhere
"""

import json
import os
import subprocess
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(HERE, "bin")
APP_NAME = "RSC-MIDI-Player"
ENTRY = os.path.join(HERE, "rsc_midi_player.py")
ICON_PATH = os.path.join(HERE, "icon.ico")

GITHUB_API_LATEST = "https://api.github.com/repos/FluidSynth/fluidsynth/releases/latest"
# Which release asset to grab. FluidSynth's Windows x64 build as of the
# 2.x series is named like "fluidsynth-vX.Y.Z-win10-x64-cpp11.zip".
ASSET_MATCH = ("win10-x64", ".zip")


def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "rsc-midi-player-build-script"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def find_windows_asset():
    print(f"Checking {GITHUB_API_LATEST} for the latest FluidSynth release...")
    data = json.loads(_http_get(GITHUB_API_LATEST))
    for asset in data.get("assets", []):
        name = asset["name"].lower()
        if all(tok in name for tok in ASSET_MATCH):
            return data.get("tag_name", "?"), asset["name"], asset["browser_download_url"]
    raise RuntimeError(
        "Could not find a Windows x64 build in the latest FluidSynth release. "
        "Open https://github.com/FluidSynth/fluidsynth/releases/latest in a "
        "browser, download the win10-x64 zip yourself, and unzip its DLLs "
        "into a 'bin' folder next to this script."
    )


def download_fluidsynth_dlls():
    os.makedirs(BIN_DIR, exist_ok=True)
    tag, asset_name, url = find_windows_asset()
    print(f"Latest FluidSynth release: {tag} -- downloading {asset_name} ...")

    zip_path = os.path.join(HERE, asset_name)
    zip_bytes = _http_get(url)
    with open(zip_path, "wb") as f:
        f.write(zip_bytes)

    print("Extracting DLLs...")
    extracted = 0
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if member.lower().endswith(".dll"):
                dest = os.path.join(BIN_DIR, os.path.basename(member))
                with zf.open(member) as src, open(dest, "wb") as dst:
                    dst.write(src.read())
                extracted += 1

    os.remove(zip_path)

    if extracted == 0:
        raise RuntimeError(
            "Downloaded the FluidSynth release but found no .dll files inside "
            "it. The release layout may have changed -- check "
            "https://github.com/FluidSynth/fluidsynth/releases/latest manually."
        )
    print(f"Extracted {extracted} DLL file(s) into {BIN_DIR}")


def main():
    if not sys.platform.startswith("win"):
        print("WARNING: You are not running this on Windows. PyInstaller")
        print("cannot cross-compile a .exe from another OS. Run this script")
        print("on a Windows machine instead.")

    existing_dlls = []
    if os.path.isdir(BIN_DIR):
        existing_dlls = [f for f in os.listdir(BIN_DIR) if f.lower().endswith(".dll")]

    if existing_dlls:
        print(f"Found {len(existing_dlls)} existing DLL(s) in .\\bin -- skipping download.")
        print("(Delete the 'bin' folder if you want this script to re-download them.)")
    else:
        download_fluidsynth_dlls()

    dlls = [os.path.join(BIN_DIR, f) for f in os.listdir(BIN_DIR) if f.lower().endswith(".dll")]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--name", APP_NAME,
    ]
    for dll in dlls:
        cmd += ["--add-binary", f"{dll}{os.pathsep}."]

    if os.path.isfile(ICON_PATH):
        cmd += ["--icon", ICON_PATH]
        # Also bundle icon.ico as a runtime resource (not just the exe's own
        # file icon) so the running app can find it via get_icon_path() and
        # set the window/taskbar icon itself, e.g. for dialogs.
        cmd += ["--add-data", f"{ICON_PATH}{os.pathsep}."]
    else:
        print(f"NOTE: {ICON_PATH} not found -- building with PyInstaller's default icon.")

    cmd.append(ENTRY)

    print("Running:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=HERE)

    print()
    print("Done. Find your portable exe at:")
    print(os.path.join(HERE, "dist", APP_NAME + ".exe"))


if __name__ == "__main__":
    main()
