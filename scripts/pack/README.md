# Lantai Desktop packaging scripts

One-click build: each script first builds a **wheel** via
`scripts/wheel_build.sh` (includes the console frontend), then uses a
**temporary conda environment** and **conda-pack** (no current dev env).
Dependencies follow `pyproject.toml`.

- **Windows**: wheel → conda-pack → unpack → NSIS installer (`.exe`)
- **macOS**: wheel → conda-pack → unpack into `.app` → optional zip

## System Requirements

- **Windows**: Windows 10 or later
- **macOS**: macOS 14 (Sonoma) or later, Apple Silicon (M1/M2/M3/M4) recommended

## Prerequisites

- **conda** (Miniconda/Anaconda) on PATH
- **Node.js / npm** (for the console frontend)
- (Windows only) **NSIS**: `makensis` on PATH
- **Icons**: Pre-generated `icon.ico` (Windows) and `icon.icns` (macOS) are included in `scripts/pack/assets/`

## One-click build

From the **repo root**:

Before creating release artifacts, run the focused vibe-coding gate:

```bash
python scripts/vibe_coding_preflight.py --full
```

This checks runtime event continuity, preview diagnostics, repair prompts, focused frontend tests, and the console production build before packaging.

**macOS**
```bash
bash ./scripts/pack/build_macos.sh
# Output: dist/Lantai.app

CREATE_ZIP=1 bash ./scripts/pack/build_macos.sh   # also create .zip
```

**Windows (PowerShell)**
```powershell
./scripts/pack/build_win.ps1
# Output: dist/Lantai-Setup-<version>.exe
# Creates two launchers:
#   - Lantai Desktop.vbs (silent, no console window)
#   - Lantai Desktop (Debug).bat (shows console for troubleshooting)
# Note: Pre-compiles all Python files to .pyc for faster startup
```

## Run from terminal and see logs (macOS)

If the .app crashes on double-click, run it from Terminal to see the full error and logs:

```bash
# From repo root; force packed env only (no system conda / PYTHONPATH). Adjust path if needed.
APP_ENV="$(pwd)/dist/Lantai.app/Contents/Resources/env"
PYTHONNOUSERSITE=1 PYTHONPATH= PYTHONHOME="$APP_ENV" "$APP_ENV/bin/python" -m lantai desktop
```

The `PYTHONNOUSERSITE=1` prevents Python from loading packages from `~/.local/lib/pythonX.Y/site-packages`, which can conflict with the packaged environment. All stdout/stderr (including Python tracebacks) will appear in the terminal. Use this to debug startup errors or to run with `--log-level debug`.

When you **double-click** the .app and nothing appears, the launcher writes stderr/stdout to `~/.lantai/desktop.log`. Inspect that file for errors.

On first launch macOS may ask for “Desktop” or “Files and Folders” access: click **Allow** so the app can run properly; if you click Don’t Allow, the window may close.

## macOS signing and notarization

Official `lantai3` desktop builds for distribution should be:

- signed with `Developer ID Application`
- notarized with Apple `notarytool`
- stapled before shipping the `.app`, `.zip`, or `.dmg`

`build_and_deploy.sh` now enforces that rule. It accepts notarization credentials from one of:

- `LANTAI_NOTARY_KEYCHAIN_PROFILE` or `APPLE_KEYCHAIN_PROFILE`
- `APPLE_ID` + `APPLE_APP_SPECIFIC_PASSWORD` + `APPLE_TEAM_ID`
- `APPLE_API_KEY` + `APPLE_API_KEY_ID` + `APPLE_API_ISSUER`

If no Developer ID certificate or notarization credentials are available, the script stops instead of silently producing a broken redistributable package. For a same-machine smoke build only, set `LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1`.

## Official website releases

The official website build publishes installers in [`shenyangs/lantai3-releases`](https://github.com/shenyangs/lantai3-releases). `build_and_deploy.sh` produces the `.app`, `.dmg`, and `.zip`; it uploads them only when explicitly requested. The desktop app does not check for, download, or install new versions.

Do **not** pass the publish flag for routine development builds or same-machine smoke tests. An official release requires a clean source tree, a signed `v<version>` tag at the current commit, Developer ID signing, accepted notarization for both the app and DMG, and passing Gatekeeper and release-evidence gates.

Authenticate the official GitHub CLI once on the build Mac:

```bash
/Users/example/.local/bin/gh auth login --hostname github.com --git-protocol https --web
```

Then publish an official release with:

```bash
./build_and_deploy.sh --no-launch --publish-github-release
```

The script audits version lineage before and after upload, and publishes the `.dmg`, `.zip`, SBOM, supply-chain manifest, and artifact manifest. Tags are immutable `v<version>` values; an existing tag causes the publish step to fail instead of overwriting shipped artifacts. If another Node tool shadows `gh` in your shell, specify the official binary explicitly:

```bash
LANTAI_GITHUB_CLI=/Users/example/.local/bin/gh ./build_and_deploy.sh --no-launch --publish-github-release
```

## macOS: if Gatekeeper blocks an internal smoke build

Internal smoke builds that intentionally skip notarization may still show: *"Apple cannot verify that 'Lantai' contains no malicious software"*. They can be opened as follows:

- **Right-click to open (recommended)**
  Right-click (or Control+click) the Lantai app → **Open** → in the dialog click **Open** again. Gatekeeper will allow it; after that double-click works as usual.

- **Allow in System Settings**
  If still blocked, go to **System Settings → Privacy & Security**, find the message like *"Lantai was blocked because it is from an unidentified developer"*, and click **Open Anyway** or **Allow**.

- **Remove quarantine attribute (not recommended for most users)**
  In Terminal: `xattr -cr /Applications/Lantai.app` (or the path to the `.app` after unzipping). This clears the download quarantine flag; less safe than right-click → Open.

## CI

`.github/workflows/desktop-release.yml`:

- **Triggers**: Release publish or manual workflow_dispatch
- **Preflight**: runs `python scripts/vibe_coding_preflight.py --full` before macOS/Windows packaging
- **Windows**: Build console → temporary conda env + conda-pack → NSIS → upload artifact
- **macOS**: Build console → temporary conda env + conda-pack → .app → zip → upload artifact
- **Release**: publishes desktop artifacts to `shenyangs/lantai3-releases`

## Script reference

| File | Description |
|------|-------------|
| `build_common.py` | Create temporary conda env, install `lantai[full]` from a wheel, conda-pack; produces archive. |
| `build_macos.sh` | One-click: build wheel → build_common → unpack into Lantai.app; optional zip. |
| `build_win.ps1` | One-click: build wheel → build_common → unpack → create VBS/BAT launchers → makensis installer. |
| `desktop.nsi` | NSIS script: pack `dist/win-unpacked`, add icons, and create shortcuts. |
| `assets/icon.ico` | Pre-generated Windows icon (installer and shortcuts). |
| `assets/icon.icns` | Pre-generated macOS icon (app bundle). |
