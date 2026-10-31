# Playbook: porting a native ARM64 macOS Steam game to iPad

Distilled from the Factorio port in this repository (Sept 2026, updated for
Factorio 2.1.24 in Oct 2026). Written as working notes for anyone, or any AI
assistant, moving this port to another game version (see §9) or attempting the
same technique on another game. Factorio-specific file references point at the
code that solved each problem; reuse it rather than re-deriving.

**The technique in one sentence:** take the game's _unmodified_ arm64 macOS
Mach-O, re-tag it as an iOS binary, satisfy its imports with iOS frameworks plus
small shim dylibs, statically patch a handful of instructions (entry point,
statically linked SDL), and boot it inside UIKit. No recompilation, no source,
no JIT, no runtime code patching.

---

## 0. Ground rules

- **Personal use only, user's own purchased copy.** The repo ships tools, never
  the game binary, assets, built `.app`, or signing material. Keep `.gitignore`
  strict (see this repo's) and pin the input by SHA-256.
- **Never bypass the hash pin.** Every patch is offset-specific. A Steam update
  means a new analysis, not a relaxed check.
- **No runtime executable-memory writes.** iOS will kill the process. All
  patches happen on disk, before `codesign`.
- **Device-test early.** The simulator lies about GPU families and texture
  formats (see §7).

---

## 1. Triage: go / no-go in ~10 minutes

Run these against `Game.app/Contents/MacOS/<exe>` before writing any code.

```sh
EXE="/path/to/Game.app/Contents/MacOS/Game"
lipo -archs "$EXE"                          # must include arm64
otool -l "$EXE" | grep -A4 LC_ENCRYPTION    # cryptid must be 0 / absent
otool -L "$EXE"                             # linked libraries
nm -U "$EXE" | wc -l                        # defined symbols (stripped?)
nm -u "$EXE" | wc -l                        # imported symbols
nm -U "$EXE" | grep -c ' _SDL_'             # statically linked SDL?
nm -u "$EXE" | grep -c '_OBJC_CLASS_\$_NS'  # AppKit class dependence
ls "$(dirname "$EXE")/../Frameworks"        # bundled dylibs (SDL? Mono? Unity?)
strings "$EXE" | grep -iE 'sdl[ -]?[23]\.[0-9]+\.[0-9]+|glfw|vulkan|moltenvk' | head
```

| Signal           | Good                                                      | Workable                                           | Stop                                                                      |
| ---------------- | --------------------------------------------------------- | -------------------------------------------------- | ------------------------------------------------------------------------- |
| Architecture     | arm64 slice present                                       | —                                                  | x86_64 only. iOS has no Rosetta                                           |
| Code execution   | AOT native C/C++/Rust                                     | IL2CPP (Unity AOT) in principle                    | Needs a JIT: Mono JIT, .NET CoreCLR, JVM, LuaJIT in JIT mode, V8/Electron |
| Windowing layer  | **SDL2/SDL3** (dynamic is easiest, static is what we did) | Custom thin AppKit layer, if import count is small | GLFW (no iOS backend); deep AppKit/NSView/NSEvent usage                   |
| Renderer         | **Metal**                                                 | Vulkan via MoltenVK (swap in iOS MoltenVK)         | Desktop OpenGL only (iOS has GLES, not GL; ANGLE is a big project)        |
| Symbols          | **Unstripped** (Factorio 2.1.24: 287k defined)            | Stripped but has a few exported anchors            | — (stripped only makes it harder, it doesn't block the port)              |
| DRM / anti-cheat | None (Factorio)                                           | Steamworks API calls only (stub them)              | Wrapped/encrypted binary, Denuvo, kernel anti-cheat                       |
| Online-only      | No                                                        | Optional online features                           | Requires a server login to play                                           |

**Why Factorio was a near-ideal target:** native arm64, AOT C++, statically
linked SDL 3.4.14, Metal renderer, _unstripped symbols_, no DRM, Steam optional,
plain data directory, and a headless CLI (`--create`, `--benchmark`) usable as a
deterministic test harness.

### Engine-specific notes (unverified, but likely the first question)

- **Unity (Mono backend):** no-go (JIT). **Unity IL2CPP:** `UnityPlayer.dylib`
  is a macOS AppKit player; replacing it is out of scope. Treat as no-go.
- **Unreal:** heavy AppKit plus huge binaries. Treat as no-go for this
  technique.
- **Godot, LÖVE, Ren'Py, RPG Maker MV/MZ (HTML5):** probably **don't use this
  technique.** Those engines have iOS runtimes, so a better route is to run the
  game's portable data (`.pck`, `.love`, `game/`, `www/`) on the engine's own
  iOS build (unencrypted GDScript, Lua, Python, JS). That's a different
  playbook, so say so before starting.
- **Custom C/C++ engine on SDL + Metal or MoltenVK:** this is the sweet spot.

---

## 2. Pipeline (what this repo actually does)

Map from step to the code to copy:

| Step | What                                                                          | Reference                                                    |
| ---- | ----------------------------------------------------------------------------- | ------------------------------------------------------------ |
| 1    | Pin input by SHA-256; validate app layout                                     | [tools/config.py](../tools/config.py)                        |
| 2    | **Import audit:** every imported symbol vs. iOS SDK `.tbd` exports            | [tools/audit.py](../tools/audit.py)                          |
| 3    | Build iOS SDL from the **same version** the game links                        | [tools/prepare_sdl.py](../tools/prepare_sdl.py)              |
| 4    | Generate shim dylib: trap stubs for every missing symbol, plus real shims     | [tools/build_probe.py](../tools/build_probe.py) `build_shim` |
| 5    | One facade dylib per original library ordinal                                 | `build_shim` (bottom)                                        |
| 6    | Retarget Mach-O: platform → iOS, rewrite install names, drop signature        | [tools/macho.py](../tools/macho.py) `retarget`               |
| 7    | Inject entry trampoline and redirect static SDL functions                     | [tools/entrypoint.py](../tools/entrypoint.py)                |
| 8    | UIKit entry: paths, config, then `SDL_UIKitRunApp(argc, argv, original_main)` | [probe/UIKitEntry.m](../probe/UIKitEntry.m)                  |
| 9    | Xcode run-script phase swaps in the real binary and signs the dylibs          | [tools/xcode_build.sh](../tools/xcode_build.sh)              |
| 10   | Verify: reverse documented patches; all sections must match the original      | [tools/verify.py](../tools/verify.py)                        |

The Python is mostly game-agnostic. To retarget it at a new game, change the
executable name, hash set, bundle names, `ORIGINAL_MAIN_OFFSET` source, and the
game-specific patches in `entrypoint.py`.

---

## 3. Mach-O surgery: tips and traps

- **Platform tag:** rewrite `LC_BUILD_VERSION` (0x32) platform to `2` (iOS) or
  `7` (iOS simulator). Convert any legacy `LC_VERSION_MIN_MACOSX` (0x24) into an
  `LC_BUILD_VERSION`. Drop `LC_CODE_SIGNATURE` (0x1d); codesign regenerates it.
- **Install names:** macOS framework paths contain `/Versions/A/`, and iOS ones
  don't. Strip it (`mobile_path`). Exception: iOS IOKit really does keep a
  versioned install name, so check the SDK `.tbd` `install-name` before "fixing"
  it.
- **Load-command space:** rewritten commands must fit before the first section's
  file offset. Check it, and fail if they don't fit.
- **Ordinals matter.** Two-level namespace binds every import to a library
  _ordinal_. Keep the order and count of `LC_LOAD_DYLIB` commands. **Modern dyld
  rejects duplicate install names even at different ordinals**, so each replaced
  library gets its own empty facade dylib (`P00.dylib`, `P01.dylib`, …) that
  re-exports the shared shim.
- **Audit with `.tbd` files, not guesses.** Filter `.tbd` targets by OS suffix
  (`-ios`, `-ios-simulator`), not by exact CPU string. Newer SDKs list
  `arm64e-ios`, `arm64e.x1-ios`, and similar, and an exact-match check silently
  reports everything as missing.
- **Some "missing" symbols are just relocated.** Example: `_nan` binds to
  CoreServices on macOS but lives in libSystem on iOS, and 2.1.24 added
  `_modff`, `_feholdexcept`, and `_fesetenv`. Discard those from the stub list.
- **Trap stubs for everything else.** Auto-generate a function per missing
  symbol that prints `FACTORIO_PROBE_UNIMPLEMENTED: <sym>` plus a backtrace and
  aborts. Generate `_k*`/`_NS*` data symbols as `NSString` constants named after
  themselves. Missing ObjC classes get a stub class whose
  `doesNotRecognizeSelector:` traps with the selector name. **Linking cleanly
  proves nothing; the traps tell you which code paths actually run.**
- **Main executable lookup:** don't assume dyld image 0 is the app. Xcode debug
  launches insert `libLogRedirect` ahead of it. Search for `MH_EXECUTE`.

### Entry point injection ([tools/entrypoint.py](../tools/entrypoint.py))

- Find zero padding between the end of load commands and the first section in
  `__TEXT` and place 16-byte trampolines there
  (`adrp x16; ldr x16; br x16; nop`).
- Find a zeroed 8-byte slot after the last `__DATA_CONST` section. Append **bind
  opcodes** to the existing bind stream so dyld fills that slot with
  `_ProbeMain` from your shim. Point `LC_MAIN.entryoff` at the trampoline.
  Record the original `main` offset (from the symbol table) and pass it to the
  shim as `-DORIGINAL_MAIN_OFFSET`.
- When `__DATA_CONST` padding runs out, overflow pointer slots into `__DATA`'s
  zero-fill tail (vmsize > filesize). If that runs out too, grow the tail by
  whole 16 KiB pages and slide `__LINKEDIT`'s VM address by the same amount. No
  file contents move. SDL3's larger export list needed this check.
- Grow `__LINKEDIT` filesize/vmsize to cover the appended bind stream (16
  KiB-aligned vmsize).
- This only handles the classic `LC_DYLD_INFO(_ONLY)` layout. **Newer binaries
  built with chained fixups (`LC_DYLD_CHAINED_FIXUPS`) need a different
  binder.** Check with `otool -l | grep -i chained` during triage, because that
  decides whether `entrypoint.py` is reusable as-is.

### Redirecting a statically linked SDL

- Factorio links SDL statically with its Cocoa backend. We built iOS SDL **at
  the same version (3.4.14)** and patched the first instruction of every
  `_SDL_*` function in `__text` that the iOS dylib also exports (1,269 of them)
  to `b <trampoline>`, which jumps through a dyld-bound pointer into the iOS
  SDL. ABI compatibility depends on matching versions, so find the version first
  (`SDL_GetVersion` symbol, or `strings`).
- If the game links SDL **dynamically**, skip all of this: ship the iOS SDL
  dylib under the same install name.
- **ObjC class name collisions:** the game's static SDL defines classes such as
  `SDL3METAL_RenderData` and `SDL3_RumbleMotor`, and so does the iOS SDL. Rename
  them in the iOS dylib's `__objc_classname` section **to same-length names**
  (`FPI…`) before signing. The set changes with the SDL version; see
  `rename_objc_classes` in [tools/build_probe.py](../tools/build_probe.py). The
  shim's `NSColor` stub was renamed to `FPColor` the same way.

### Game-specific instruction patches

- Locate every patch **by symbol name plus offset**, then **assert the original
  instruction bytes** before writing. Record `{name, offset, original_bytes}` in
  a manifest.
- Payoff: on 2.0.77, the Space Age executable (different hash, same version)
  worked with **zero code changes**. Adding support was just adding its hash.
- `verify.py` reverses each manifest entry and requires every file-backed
  section to match the original. That catches accidental patches and proves the
  binary is otherwise untouched.
- Keep patches minimal. Factorio 2.0.77 needed exactly one for device builds
  (skipping a macOS-only display-ID read in a log function), and 2.1.24 needs
  none beyond the SDL redirects. Prefer shims to patches whenever a shim can do
  the job.
- Diagnostic-only patches (e.g. bypassing a GPU-family gate in the simulator)
  must be labelled, gated behind a flag, and **rejected by verify for device
  builds.** The 2.0.77 simulator patch no longer matches 2.1.24, so
  `--simulator-metal` is now refused outright.

---

## 4. Shimming macOS APIs at runtime

Order of preference: make the game take its own fallback path, then provide a
minimal real implementation, and only then patch instructions.

| Problem                                                             | Fix that worked                                                                                                                                                                                   | File                                                      |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| Steamworks (`SteamAPI_Init`, `RestartAppIfNecessary`)               | Return `false` from both, so the game runs in non-Steam mode                                                                                                                                      | [probe/PlatformFallbacks.m](../probe/PlatformFallbacks.m) |
| OpenCL                                                              | `clGetPlatformIDs` returns 0 platforms (`-1001`)                                                                                                                                                  | same                                                      |
| macOS-only Metal selector (`isDepth24Stencil8PixelFormatSupported`) | Override `+[NSObject resolveInstanceMethod:]` to add the method to **any** class on demand. Chasing device instances failed because `CAMetalLayer` creates its own device inside the shared cache | [probe/MetalCompat.m](../probe/MetalCompat.m)             |
| `NSBitmapImageRep` / `NSColorSpace` for PNG loading                 | Minimal reimplementation over ImageIO and CGContext. **Un-premultiply** afterwards if the game expects straight alpha. 16-bit images drop to 8-bit                                                | [probe/BitmapCompat.m](../probe/BitmapCompat.m)           |
| `NSApp`, `NSAppKitVersionNumber`                                    | Provide as `nil` / `0` data symbols                                                                                                                                                               | generated in `build_shim`                                 |

Tips:

- Follow the trap log. Implement only what traps on the paths you exercise.
- Use the game's own config file and CLI flags before reaching for code. For
  Factorio: `--force-metal`, `--graphics-quality medium`, `--nogamepad`, texture
  compression off, `video-memory-usage=low`, `check-updates=false`, crash-log
  upload off, achievement popups hidden.
- Without Steam, the game's local achievement tracking didn't persist and
  replayed "unlocked" popups on every launch. We hid them through the game's own
  setting rather than patching.

---

## 5. App container, data, and config

- Bundle the game's read-only data inside the `.app` (`cp -cR` clones on APFS;
  fall back to `copytree` on external non-APFS drives).
- Writable data goes under `Documents/<Game>/`. **The container path changes on
  every reinstall**, so rewrite the absolute paths in the game's config on
  **every launch** while leaving the user's other settings alone
  ([probe/UIKitEntry.m](../probe/UIKitEntry.m)).
- Bundle mods or extras in the `.app` and copy them into Documents once per
  change of the bundled selection, so in-game changes on the iPad stick until
  then. Record what was applied, and write that record last so an interrupted
  sync runs again. Enable mods by **internal name** (Factorio: `info.json`
  `name`, not the zip filename).
- Info.plist keys that mattered: `UIRequiresFullScreen`, landscape-only
  orientations, `UIApplicationSupportsIndirectInputEvents` (mouse and trackpad),
  status bar hidden, `UIDeviceFamily=[2]`.
- Entitlement: `com.apple.developer.kernel.increased-memory-limit`. Large
  desktop games need it.

---

## 6. Lifecycle, input, and UX

- **Backgrounding:** iOS forbids GPU submission in the background, and the game
  loop spins and spams `IOGPUMetalError`. Patch the vendored SDL's UIKit event
  pump to block in `CFRunLoopRunInMode` while backgrounded. The patch is applied
  by `prepare_sdl.py` after extraction because `build/` is gitignored.
- **Inject input inside SDL**, by compiling a bridge file into SDL's own target
  that calls `SDL_SendKeyboardKey`, `SDL_SendMouseButton`,
  `SDL_SendMouseMotion`, and `SDL_SendMouseWheel`. Queued events and polled
  state (`SDL_GetKeyboardState`) then stay consistent, which matters because
  many games poll ([probe/SDLInputBridge.c](../probe/SDLInputBridge.c)).
- **Text input trap:** `SDL_StartTextInput()` makes SDL's hidden `UITextField`
  first responder, which triggers the iOS press-and-hold accent picker on held
  keys. Instead, compose characters from scancodes and modifiers yourself and
  send them with `SDL_SendKeyboardText`. SDL3 only delivers text input to a
  window whose text input is active, so set the window's `text_input_active`
  flag directly, and re-assert it on every key press in case the game stopped
  text input.
- **Overlay controls** ([probe/TouchControls.m](../probe/TouchControls.m)):
  - Return `nil` from the overlay's `hitTest:` when the hit is the overlay
    itself, so touches pass through to the game.
  - Attach gesture recognizers to the **window**, not the overlay. Pass-through
    touches never reach recognizers on a view whose `hitTest:` returns nil. Set
    `cancelsTouchesInView=NO` and `delaysTouchesBegan=NO`.
  - Release every held key and button on `UIApplicationWillResignActive`, or
    keys stick after app switching.
  - Install the overlay only once SDL has a key window (poll for
    `SDL_GetKeyboardFocus()` and the key window's root view controller).
  - Mouse-driven desktop games need modes: **Tap** (touch = left click),
    **Hold** (touch-and-hold = held right button; labelled Mine here), and
    **Cursor** (touch moves a visible pointer, with separate click buttons). A
    joystick that maps to WASD covers movement.
  - Let the user hide the controls, and auto-hide the show/hide toggle after a
    few seconds.

---

## 7. Build, sign, install, test

- **Xcode integration trick:** the target compiles a dummy `main()`
  ([probe/MainPlaceholder.m](../probe/MainPlaceholder.m)). A run-script phase
  then `ditto`s the generated app over the product and re-signs every
  `Frameworks/*.dylib` with `$EXPANDED_CODE_SIGN_IDENTITY`. Xcode signs the app
  itself. The ad-hoc-signed intermediate in `build/` **cannot** be installed.
- `xcrun devicectl device install app` /
  `process launch --console --terminate-existing` give you stdout and stderr on
  the Mac. That's your main debugger for trap messages.
- Pull logs and saves with
  `devicectl device copy from --domain-type appDataContainer`.
- **Validate headless before graphics.** If the game has a CLI or dedicated
  server mode, run a deterministic job (map creation plus N ticks) on Mac,
  simulator, and iPad and compare a checksum. Factorio 2.0.77 produced
  `2892897386` on all three. That proved the CPU, libc, and filesystem path
  before any renderer work.
- **The simulator** works for headless probes only. It doesn't report Apple7 or
  Mac1 GPU families and doesn't support BC texture formats (it failed on
  BC3_RGBA). Real graphics work needs a device. BC/DXT textures work on M1+
  iPads (`supportsBCTextureCompression`).
- Screenshot automation through a separate XCTest helper
  ([tools/device_ui.py](../tools/device_ui.py)): it needs the **Xcode UDID**,
  which differs from the CoreDevice ID `devicectl` uses. Use fresh DerivedData
  per run to avoid mismatched test-runner signatures.
- Signing expiry: a free Personal Team profile lasts about 7 days and a paid
  developer account about 1 year. Back up saves before reinstalling, because
  deleting the app deletes its container.

---

## 8. Suggested order of work for a new game

1. Triage (§1). Stop if any column says _Stop_.
2. Copy this repo's `tools/` and `probe/` skeleton. Rename identifiers and
   change the executable name and hash pin.
3. Run the import audit and read the missing-symbol table per library. The
   AppKit, CoreGraphics, and Carbon counts are the real effort estimate.
4. Retarget plus trap stubs plus entry trampoline, with `ProbeMain` calling the
   original `main` directly (no UIKit yet) and `--version`/`--help`. First
   milestone: it prints its version on the device.
5. Headless run (dedicated server, benchmark, or CLI) as a deterministic
   checksum test.
6. Bring up the windowing layer (SDL redirect or swap), then the renderer. Work
   through traps one at a time.
7. Fix lifecycle: backgrounding, data paths, config rewriting.
8. Input overlay, then text input, then hardware keyboard and mouse.
9. Write `verify.py`-style provenance checks before the patch list grows.

## 9. Moving this port to another Factorio version

The worked example is the move from Factorio 2.0.77 (static SDL 2.30.9) to
2.1.24 (static SDL 3.4.14). The steps below are what that migration required, in
the order that found problems fastest. The repo's git history is not needed to
follow them; everything that mattered is recorded here and in the code. Each
executable has its own hash, so a base-game-only build counts as a new target
too, even at the same version number.

**Before touching code, diff the facts, not the files.** Collect these for the
old and new executables and compare. Each difference maps to a step below.

| Fact                                               | How to get it                                                                             | If it changed                                                                    |
| -------------------------------------------------- | ----------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| SHA-256 of the executable                          | `shasum -a 256`                                                                           | Step 1. Always changes on update                                                 |
| Linked SDL version                                 | `strings "$EXE" \| grep -E 'SDL[ -]?[23]\.[0-9]+'`, or call `SDL_GetVersion` symbol logic | Steps 2, 5, 6. A major bump is the expensive case                                |
| Imported-symbol set                                | `nm -u "$EXE"`, or rerun `tools/audit.py`                                                 | Step 3                                                                           |
| Load-command layout (dyld info vs. chained fixups) | `otool -l "$EXE" \| grep -iE 'dyld_info\|chained'`                                        | If it moved to chained fixups, `entrypoint.py` needs a new binder. Stop and plan |
| Offset and bytes of each patched function          | symbol lookup, then compare bytes against the manifest                                    | Step 4                                                                           |
| Count of redirectable `_SDL_*` symbols             | output of the entry-point step                                                            | Step 6                                                                           |

1. Pin the new executable's hash in [tools/config.py](../tools/config.py). Don't
   relax the check.
2. Find the SDL version the game links, and point
   [tools/prepare_sdl.py](../tools/prepare_sdl.py) at that release and its hash.
   A new SDL major version also means porting
   [probe/SDLInputBridge.c](../probe/SDLInputBridge.c) and
   [probe/TouchControls.m](../probe/TouchControls.m), and the vendored-SDL
   patches.
3. Rerun the import audit and handle new missing symbols. Relocated ones get
   discarded; anything Steam-related gets a fallback.
4. Re-audit every instruction patch instead of carrying it over. The 2.0.77
   display-ID patch was no longer needed, and the simulator patch no longer
   matched.
5. Recheck ObjC class collisions. The set changed completely with SDL3.
6. Check that the pointer slots fit. The redirect count rose from 817 to 1,269.
7. Remove stale build output. `ditto` merges, so files from the old version
   survived and old mod zips failed with "Incompatible Factorio version".
   [tools/xcode_build.sh](../tools/xcode_build.sh) now clears them first. Mods
   must also have versions that declare the new `factorio_version`.
8. Re-record the expected checksum in [tools/verify.py](../tools/verify.py) on
   the Mac, then repeat the headless and device tests (§7).

### Symptom → likely cause

Most failures after a version bump fall into a handful of buckets. Start from
the symptom.

| Symptom                                              | Likely cause                                                                 | Where to look                                                     |
| ---------------------------------------------------- | ---------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| Build refuses to start, hash mismatch                | New executable, as intended                                                  | Step 1. Pin the new hash after analysis, never skip               |
| `FACTORIO_PROBE_UNIMPLEMENTED: <sym>` on launch      | New import the audit flagged but nothing shimmed                             | Step 3; add a real shim or fallback for that symbol               |
| Every import reported "missing" after an SDK upgrade | `.tbd` target filter matches CPU strings exactly                             | §3, "Audit with `.tbd` files"                                     |
| dyld error about duplicate or missing install name   | Facade dylib count or order no longer matches `LC_LOAD_DYLIB`                | §3, "Ordinals matter"                                             |
| Crash or "Class … is implemented in both" at startup | ObjC class names in the new static SDL collide with the iOS SDL              | Step 5, `rename_objc_classes`                                     |
| Entry-point injection fails with "no room"           | Pointer slots or bind stream outgrew the padding                             | Step 6, §3 "Entry point injection"                                |
| Wrong behavior or crash inside a redirected SDL call | SDL version mismatch, so ABI drift                                           | Step 2; the iOS SDL must match the game's exactly                 |
| Patch assertion fails                                | Function moved or was recompiled                                             | Step 4; re-derive, never edit the expected bytes to match         |
| Old mods or data fail with a version error           | Stale files merged into the new bundle                                       | Step 7                                                            |
| Verify reports a section differs from the original   | An unintended write, or a patch not in the manifest                          | §3, "Game-specific instruction patches"                           |
| Checksum differs from the recorded one               | Expected when the game version changed; unexpected when only tooling changed | Step 8; compare Mac against device first, and only then re-record |

### Handoff checklist for the next version

- [ ] New hash pinned, old hash removed or kept as a separate known target.
- [ ] Linked SDL version and `prepare_sdl.py` pin match, with the new hash.
- [ ] Audit clean: every remaining missing symbol is shimmed or deliberately
      trapped.
- [ ] Every patch re-derived and asserted against the new bytes.
- [ ] `verify.py` passes and the checksum was re-recorded on the Mac.
- [ ] Headless run matches on Mac and device.
- [ ] Graphics launch reaches the main menu on a real iPad, with no trap output.
- [ ] Docs updated: target version in [ARCHITECTURE.md](ARCHITECTURE.md)
