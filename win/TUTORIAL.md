# Tutorial: Build an IPA from NSP/XCI on Windows

This guide walks you through building an unsigned `.ipa` on a regular Windows PC and sideloading it onto your iPhone or iPad—no Mac or Xcode required.

## 0. Requirements

- **PC:** Windows 10/11 64-bit, with free disk space of roughly 2× the NSP size + 10 GB.
- **iPhone / iPad:** iOS / iPadOS 26 or later, with an **A17 Pro or newer** chip (iPhone 15 Pro and newer) or an **M1 or newer** iPad. *(Older chips without BC texture support may still run some games via automatic ASTC conversion, subject to RAM limits.)*
- **Your own files:** A game dump (NSP/XCI, plus updates and DLC if applicable) and your own `prod.keys`.

## 1. Install the Tools (One-Time Setup)

Open **PowerShell** (Start menu → type `PowerShell` → Enter) and run:

```powershell
winget install --id LLVM.LLVM -e
winget install --id Microsoft.DotNet.SDK.10 -e
winget install --id KhronosGroup.VulkanSDK -e
winget install --id Meta.Zstandard -e
python -m pip install lz4 capstone fonttools pillow
```

Python 3.10 or newer must already be installed (from [python.org](https://www.python.org/)). Two Apple tools require signing in with a free Apple ID at [developer.apple.com/download](https://developer.apple.com/download/):

1. **Metal Developer Tools for Windows.** Install it and note its installation folder (for example, `C:\Program Files\Metal Developer Tools`).
2. **Xcode 26 or 27** (`.xip` file). Extract the iPhoneOS SDK from it once (takes about 1–2 minutes):
   ```powershell
   cd C:\nxnative
   python win\sdk\extract_xip.py C:\path\to\Xcode_27.1.xip
   ```

If any tool is not auto-detected, copy `win\toolchain.example.json` to `win\toolchain.json` and update its paths.

## 2. Verify Your Setup

Always run commands from the repository folder:

```powershell
cd C:\nxnative
python win\doctor.py
```

Every line should show `[ok ]`, ending with `everything a build needs is there`. If anything is marked `MISSING`, the `get it:` line below it explains how to install it.

## 3. Prepare Your Game Folder

Use **one folder per game** containing:

- Base game NSP or XCI (required)
- Update and DLC NSPs (optional; the newest update in the folder is used automatically)
- `prod.keys` (can also live in another folder)

Example:

```text
C:\Games\Balatro\
  Balatro [0100CD801CE5E000][v0].nsp
  Balatro [0100CD801CE5E800][v851968].nsp     <- update 1.1.3, the upstream-tested version (recommended)
  prod.keys
```

## 4. Build the IPA

### Easy way: GUI

Double-click **`Build-IPA-GUI.bat`** in the repository root (`C:\nxnative`). Then:

1. **Game files:** Click the dashed box to pick the NSP/XCI files (base, update, DLC for one game), or click *Choose folder*.
2. **prod.keys:** Click *Browse*.
3. Click **Build IPA**. The button turns into a progress bar. Once finished, a green card displays the IPA path with a *Show in folder* button.

The GUI defaults to:

- App name = game title
- Bundle ID = `org.nxnativewindows.<title ID>`
- Language = game default
- Output location = next to the game files (can be changed with *Change*)

Full logs are visible under *Details*.

If a folder contains multiple games, the GUI isolates the selected files into a `<TitleID>_nxw` subfolder using hard links, which takes no extra disk space.

### Command line

```powershell
cd C:\nxnative
python win\build_ipa_windows.py "C:\Games\Balatro" -k "C:\Games\Balatro\prod.keys"
```

Build stages:

1. Build `nx_extract`
2. Extract NSP/XCI
3. Rewrite game modules
4. Translate & compile shaders
5. Build the iOS app (`clang` + `ld64.lld`)
6. Pack the IPA

The finished file appears in the game folder (for example, `C:\Games\Balatro\Balatro.ipa`). The `<TitleID>_build\` work folder is cached to speed up subsequent builds, and per-stage logs are saved in `<TitleID>_build\logs\`.

Useful options (run with `-h` to see all):

| Option | Description |
|---|---|
| `-o D:\out\Game.ipa` | Custom output path for the IPA |
| `--app-name "Name"` | App name shown on the Home Screen |
| `-l ja` | Game language |
| `--no-show-fps` | Disable the FPS overlay |

## 4b. Split Mode: Small IPA + Data Folder

Choose **03 Package → App + data folder** in the GUI, or pass `--split-data` on the command line. In the same card you can toggle **Increased Memory Limit On / Off** (default On; pass `--no-entitlements` on the CLI to disable). This produces two items:

- `<GameName>.ipa`: App binary only (tens of MB), so signing and installing in Sideloadly takes just seconds.
- `<GameName>-data\`: Contains `romfs`, `msl` (shaders), and `aoc` (DLC), created via hard links so it uses no extra disk space on your PC.

How to install on iPhone / iPad:

1. Install `<GameName>.ipa` with Sideloadly as usual (Step 5).
2. Launch the app once. A **Game data needed** screen appears showing the folder name, size, and instructions.
3. Copy the entire `<GameName>-data` folder into the app's Documents folder:
   - **From Windows:** Open the **Apple Devices** app (or iTunes), select your device → **Files** → the app name → drag `<GameName>-data` into it.
   - **On iPhone / iPad:** Open the **Files** app → *On My iPhone/iPad* → the app name, and paste the folder from a USB drive, network share, or iCloud Drive.
4. Wait for the copy to finish (large games like TotK can take 10–30 minutes), then tap **Check again**. Once it shows "Found", force-close the app from the app switcher and relaunch it.
5. The launcher opens. Tap **Play**.

**Easiest method: Import ZIP.** On your PC, pack the data folder into an uncompressed ZIP using 7-Zip (*Store* level for speed): `7z a -tzip -mx=0 <GameName>-data.zip <GameName>-data`. Transfer the ZIP to your device (Files, USB drive, or cloud). On the **Game data needed** screen, tap **Import ZIP…** and select it. The app extracts it directly into its Documents directory:

- The progress bar shows GB copied, file count, and estimated time remaining, with a **Cancel** button.
- On iOS/iPadOS 26+, extraction continues in the background with Live Activity progress on the Lock Screen and a "Game data imported" notification when done (allow notifications when prompted).
- The ZIP is streamed in place, so you only need enough free space for the extracted data plus the ZIP itself (you can delete the ZIP afterward).
- When finished, tap **Close the app** and relaunch it.

You do not need to rename the folder—the app looks for `romfs` in Documents and in every immediate subfolder. When re-signing every 7 days, you only need to reinstall the small IPA; your data folder in Documents stays intact. Only re-copy the data folder after rebuilding the same game.

## 4c. The Legend of Zelda: Tears of the Kingdom

- **Version:** Use **1.4.3** (update `v720896`). Place the base XCI/NSP and the 1.4.3 update NSP in the same folder. The official profile targets 1.4.3; other versions do not receive the 60 fps and render scale patches.
- **Split mode is strongly recommended.** Game data is ~17 GB, so a single monolithic IPA takes a very long time to sign and install.
- **Free space on device:** At least ~25 GB (game data plus shader cache and saves).
- **Memory:** TotK uses up to 5 GB of game memory. Enable **Increased Memory Limit** in Sideloadly. Upstream tested TotK on iPhone 17 Pro Max and iPad Pro M2; devices with 6 GB RAM or chips without hardware BC textures (A16 and older) may still be terminated by iOS under memory pressure.
- **Slow first launch:** "Preparing shaders" on first launch is normal—wait for it to complete without closing the app.
- **Automatic memory saving:** On A16 and older, BC textures are converted to ASTC 4×4 (¼ the memory of RGBA8). The memory guard lowers the cap to 30 fps below 600 MB free and trims caches below 300 MB (HUD displays `GUARD`). On 6 GB devices, *Metal 4 pipelines* and *Pipeline prebuild* are off by default so there is no long "Preparing pipelines" wait, and shader compilation runs in the background.
- **If it crashes:** Retrieve `log.txt` and `log.prev.txt` from Files → *On My iPhone/iPad* → the TotK app folder.

## 4d. Mods (Performance Patches, Textures, etc.)

Windows builds support standard emulator (Yuzu/Ryujinx/Eden) and Atmosphère mod layouts. Create a `mods` folder next to your game files, with one subfolder per mod:

```text
C:\Games\TotK\
  mods\
    ModName\
      exefs\<build ID>.ips   or   exefs\name.pchtxt       <- code patches
      romfs\...                                           <- replacement files
```

- **Code patches (`.ips`, `.pchtxt`)** are applied through the nxnative rewriter just like built-in patches. Only patches matching your game build ID are applied; mismatches are skipped with a log message. (For TotK 1.4.3, the `main` build ID is `277178b7dba1b6d4…`.) Patches that conflict with official profile fixes are also skipped.
- **`romfs` files** override the original game files in the data folder via hard links (no extra disk space).
- Match your mods to your exact game version (e.g., 1.4.2 mods will not work on 1.4.3).
- After adding or removing mods, rebuild and reinstall the IPA. If a mod modifies `romfs`, also re-copy the data folder or ZIP.

## 5. Sideload onto iPhone / iPad

1. Install **Sideloadly** ([sideloadly.io](https://sideloadly.io/)) on Windows. If prompted, install the Apple-website versions of iTunes and iCloud (not the Microsoft Store versions).
2. Connect your iPhone/iPad, open Sideloadly, drag the `.ipa` file into the window, enter your Apple ID, and click **Start**.
3. Under **Advanced options**, enable *Increased Memory Limit* if available (required for large games).
4. On your device, enable *Settings → Privacy & Security → Developer Mode* and restart. Then go to *Settings → General → VPN & Device Management* and trust your Apple ID.
5. Open the app. The settings launcher appears—tap **Play**.

With a free Apple ID, apps must be re-signed every 7 days.

## 6. Troubleshooting

- **Build fails:** The `[RED] ...` error line and the last 40 lines of the log are printed. Full logs are in `<TitleID>_build\logs\`.
- **App crashes on iPhone/iPad:** Open the **Files** app → *On My iPhone/iPad → \<GameName\>* and inspect `log.txt`.
- **"this IPA uses vX, which game profile ... was not brought up on":** That game version has not been tested upstream. Use the update version listed in the upstream README (for example, Balatro 1.1.3).
- **Games that need a Mac:** The build stops with a message mentioning the Mac host for games that require runtime shader capture. LÖVE games such as Balatro are *not* affected—their shaders are assembled offline on Windows by `win\love_shaders.py`.

## 6b. Games with Runtime Shaders (Portal, Fire Emblem: Three Houses, Super Mario Galaxy)

Some games compile shaders at runtime. Upstream captures them on a Mac; on Windows, you capture them directly on your iOS device:

1. The first build stops with a message and creates `<GameName>-capture.ipa` in your game folder.
2. Sideload that IPA, play until the title screen, and wait about one minute (the screen may stay black or blank because shaders are not built yet).
3. Open **Files** → *On My iPhone/iPad* → `<GameName> (capture)` and copy the `glslc_capture` (or `vk_capture`) folder back to your game folder on Windows, next to the NSP files.
4. Run the build again. It detects the capture folder, matches it against the profile table, and builds the playable IPA.

Oodle/Unreal games (Octopath Traveler, Bayonetta 3, Pokémon Legends: Z-A, DRAGON QUEST XI S, etc.) do not need this step: their decoders run locally via the built-in ARM64 emulator on your PC.

## 7. Updating the Repository

```powershell
cd C:\nxnative
git fetch upstream
git merge upstream/main
```
