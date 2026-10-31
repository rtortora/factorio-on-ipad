# Architecture and limitations

## How the original executable starts on the iPad

The tools read the ARM64 slice of the macOS executable identified by the pinned
SHA-256 hash. They create a new file under `build/`, change its Mach-O platform
to iOS, and replace library paths with local facade libraries that refer to iOS
libraries and the compatibility layer.

Before signing, `tools/entrypoint.py` inserts static ARM64 trampolines into
verified unused Mach-O space. Factorio 2.1.24 links SDL statically, so in the
current build 1,269 public SDL entry points are redirected to SDL 3.4.14, built
separately for iOS. The new entry point runs through UIKit and reaches the
preserved original `main`. There is no JIT and no runtime modification of
executable memory pages.

The facade libraries preserve the original dyld library ordinals. Other missing
symbols receive diagnostic placeholders that abort with an explicit error.
Successful linking therefore does not mean that every game code path has been
implemented on iOS.

| File | Purpose |
|---|---|
| `Build.command` | Compile and open the build window, which checks the build tools, then runs setup and deployment |
| `tools/config.py`, `tools/setup.py` | Local paths, original executable validation, and setup |
| `tools/audit.py` | Check original imports against iOS SDK exports and generate a local report |
| `tools/macho.py` | Read ARM64 Mach-O; change the platform and load paths on a copy |
| `tools/entrypoint.py` | Static branches and documented platform patches |
| `tools/prepare_sdl.py` | Download SDL, verify its hash, patch its UIKit event pump, and compile it for iOS |
| `tools/import_game_assets.py` | Generate the app icon and `mods/` from the local game install |
| `tools/build_probe.py` | Build compatibility libraries, the executable, and the app bundle |
| `tools/xcode_build.sh` | Integrate with Xcode, apply its bundle ID, and sign libraries |
| `tools/deploy.py` | Build, verify, install, and launch on a connected iPad |
| `probe/UIKitEntry.m` | App lifecycle, local data paths, bundled mod sync, original game startup, and CLI probes |
| `probe/MetalCompat.m` | Supply the missing macOS depth-format query |
| `probe/BitmapCompat.m` | Required NSBitmapImageRep/NSColorSpace operations using ImageIO/RGBA8 |
| `probe/PlatformFallbacks.m` | Report OpenCL and Steam as unavailable |
| `probe/TouchControls.m` | English UIKit buttons, movement, keyboard, and visible touch cursor |
| `probe/SDLInputBridge.c` | Keep events and polled input state consistent through SDL |
| `tools/verify.py` | Check the original hash, patch provenance, platforms, and signatures |

## Additional adaptations

Factorio 2.1.24 needs no instruction patches beyond the SDL redirects. The game
engine and its Metal renderer are preserved. On the tested M1 iPad, the
compatibility layer implements the missing
`isDepth24Stencil8PixelFormatSupported` method by returning `NO`, causing the
original renderer to select its alternative depth format.

iOS forbids submitting GPU work while an app is in the background, so
`tools/prepare_sdl.py` patches SDL's UIKit event pump to block the game's main
loop until the app returns to the foreground. The app requests the
`increased-memory-limit` entitlement. Steam is reported as unavailable, so the
game shows a Steam error at startup that the player dismisses. Local achievement
tracking does not reliably survive relaunches, so achievement popups are hidden
through the game's own setting. Crash-log uploading is disabled the same way.

The game's `data` folder, including Space Age, is copied from the local
installation into the app bundle. Configuration and user data are stored under
`Documents/Factorio` in the private app container. Its changing absolute paths
are adjusted at startup. Simulation and rendering require no connection to the
Mac.

`tools/import_game_assets.py` copies the zipped third-party mods enabled on the
Mac, and `mod-settings.dat`, into `mods/` for bundling. Each time the bundled
selection changes, the app applies it once at launch: bundled mods are installed
and enabled, mods dropped from the selection are removed, and `mod-settings.dat`
is replaced. Later changes made in the game on the iPad stay until the selection
changes again. The enabled state of Space Age and its companion mods is not
copied from the Mac.

## Verified behavior and remaining gaps

Current target: Factorio: Space Age 2.1.24, macOS Steam executable, built with
SDL 3.4.14, Xcode 27.1, and SDK 27.1. On this version, Magic Keyboard key and
mouse control and local saving and loading are supported. Touch controls are
available, but key and mouse control is recommended.

Earlier baseline as of September 8, 2026: Factorio 2.0.77 / build 84539, 11-inch
iPad Pro M1, iPadOS 26.6.1, Xcode 26.3, and SDK 26.2. Observed behavior included
graphical startup, the tutorial, the WASD stick, Tab, inventory, mining trees,
on-screen text entry, and saving and reloading a game. Audio was confirmed by the
user. Positioning the cursor alone did not click; pressing the separate Left click
button activated the targeted menu item. The mouse buttons are grouped together.

On that baseline, a shared test save with a 64 × 64 map and seed 12345 produced
checksum `2892897386` after 120 updates on the original Mac version, the
simulator, and the iPad. This is a small simulation probe, not a general
compatibility guarantee. `verify.py` still expects this value, but it has not been
reconfirmed on 2.1.24. The original test logs and device screenshots are not
included; `verify.py` can check logs generated by your own tests.

Complex factories, many shader and texture paths, color fidelity, the full range
of building actions involving multiple fingers, applying changed mod selections on
the iPad, the base game without Space Age, and long-term behavior remain
unverified. Bitmap compatibility reduces 16-bit images to 8 bits. Steam features
and controllers are not supported. Multiplayer has not been tested. The iPad app
still displays the original macOS executable's version label; that label does not
establish the build target.

## Simulator

`--simulator-metal`, the diagnostic that bypassed the GPU-family check in the
simulator, is rejected for Factorio 2.1.24 because its patch site was not
re-audited. On 2.0.77, the simulator then failed on the unsupported BC3_RGBA
format. Verification rejects this exception in device builds. The simulator is
intended for selected CLI probes here; it is not a verified environment for
graphical gameplay.
