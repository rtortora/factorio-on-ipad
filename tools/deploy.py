#!/usr/bin/env python3
"""Build the development-signed app, verify it, install it on an iPad and launch it."""

import argparse
import json
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from config import ROOT, BUNDLE_ID, DEVICE_ID, TEAM_ID, save_setting

DERIVED_DATA = ROOT / "build/Xcode"
SIGNED_APP = DERIVED_DATA / "Build/Products/Debug-iphoneos/FactorioProbe.app"
VENV_PYTHON = ROOT / ".venv/bin/python"
# Replaced by each run, so the latest log is always in the same place.
LOG_PATH = Path(tempfile.gettempdir()) / "factorio-deploy.log"
STAGE_PATH = ROOT / "build/deploy-stage"
TIMINGS_PATH = ROOT / "build/deploy-timings.json"
LOG = None
# Label and estimated seconds until this Mac has its own timings. xcode_build.sh reports the middle stages.
STEPS = {
    "xcode": ("Preparing Xcode build", 20),
    "assets": ("Importing icon and mods", 5),
    "sdl": ("Building SDL", 300),
    "game": ("Patching and bundling the game", 60),
    "copy": ("Copying into the app", 60),
    "sign": ("Signing", 30),
    "verify": ("Verifying", 15),
    "install": ("Installing on the device", 180),
    "launch": ("Launching", 10),
}
BUILD_STEPS = ["xcode", "assets", "sdl", "game", "copy", "sign"]


def format_time(seconds):
    minutes, seconds = divmod(round(seconds), 60)
    return f"{minutes}m {seconds:02}s" if minutes else f"{seconds}s"


class Progress:
    """One bar on a terminal, one line per step otherwise. Estimates come from the last successful run."""

    def __init__(self, keys):
        self.keys = keys
        self.timings = (
            json.loads(TIMINGS_PATH.read_text()) if TIMINGS_PATH.exists() else {}
        )
        self.estimates = {k: self.timings.get(k, STEPS[k][1]) for k in keys}
        self.started = self.step_started = time.monotonic()
        self.index = -1
        self.live = sys.stdout.isatty()

    def record_step(self):
        if self.index >= 0:
            self.timings[self.keys[self.index]] = round(
                time.monotonic() - self.step_started, 1
            )
            TIMINGS_PATH.parent.mkdir(exist_ok=True)
            TIMINGS_PATH.write_text(json.dumps(self.timings, indent=2) + "\n")

    def advance(self, key):
        self.record_step()
        self.index = self.keys.index(key)
        self.step_started = time.monotonic()
        LOG.write(f"\n=== {STEPS[key][0]} ===\n".encode())
        if self.live:
            self.render()
        else:
            print(f"[{self.index+1}/{len(self.keys)}] {STEPS[key][0]}", flush=True)

    def render(self):
        if not self.live:
            return
        now = time.monotonic()
        key = self.keys[self.index]
        over = now - self.step_started > self.estimates[key]
        remaining = max(self.estimates[key] - (now - self.step_started), 0) + sum(
            self.estimates[k] for k in self.keys[self.index + 1 :]
        )
        elapsed = now - self.started
        fraction = min(elapsed / max(elapsed + remaining, 1), 0.99)
        filled = int(fraction * 20)
        status = (
            "taking longer than estimated"
            if over
            else f"~{format_time(remaining)} left"
        )
        line = (
            f'[{"#"*filled}{"-"*(20-filled)}] {fraction:3.0%}  '
            f"{self.index+1}/{len(self.keys)} {STEPS[key][0]}  {status}"
        )
        sys.stdout.write(
            "\r" + line[: shutil.get_terminal_size().columns - 1] + "\x1b[K"
        )
        sys.stdout.flush()

    def stop(self):
        if self.live:
            sys.stdout.write("\r\x1b[K")
            sys.stdout.flush()

    def finish(self):
        self.record_step()
        self.stop()
        return time.monotonic() - self.started


def run_step(progress, key, command, stages=False):
    progress.advance(key)
    LOG.write(f'$ {" ".join(command)}\n'.encode())
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=LOG,
        stderr=subprocess.STDOUT,
    )
    try:
        while process.poll() is None:
            stage = (
                STAGE_PATH.read_text().strip()
                if stages and STAGE_PATH.exists()
                else None
            )
            if stage in progress.keys and progress.keys.index(stage) > progress.index:
                progress.advance(stage)
            progress.render()
            time.sleep(0.25)
    except KeyboardInterrupt:
        process.terminate()
        process.wait()
        raise
    if process.returncode:
        progress.stop()
        raise SystemExit(
            f"Failed at step: {STEPS[progress.keys[progress.index]][0]} "
            f"(exit code {process.returncode}).\nFull log: {LOG_PATH}"
        )


def devicectl_json(*command):
    with tempfile.TemporaryDirectory() as folder:
        output = Path(folder) / "output.json"
        subprocess.run(
            ["xcrun", "devicectl", *command, "--quiet", "--json-output", str(output)],
            check=True,
            stdout=LOG,
            stderr=subprocess.STDOUT,
        )
        return json.loads(output.read_text())["result"]


def describe(device):
    hardware = device["hardwareProperties"]
    return f"{device['deviceProperties']['name']}, {hardware.get('marketingName', hardware['deviceType'])} ({hardware['udid']})"


def game_running(device):
    """Whether the game runs on the device, or None if the device does not respond."""
    try:
        processes = devicectl_json(
            "--timeout",
            "30",
            "device",
            "info",
            "processes",
            "--device",
            device["hardwareProperties"]["udid"],
        )["runningProcesses"]
    except subprocess.CalledProcessError:
        return None
    return any("/FactorioProbe.app/" in p.get("executable", "") for p in processes)


def pick(checked):
    """Choosing a device also confirms the warning shown beside it: installing terminates the running app."""
    warning = "game running; installing terminates it and loses unsaved progress"
    if not sys.stdin.isatty():
        if len(checked) == 1:
            raise SystemExit(
                f"{describe(checked[0][0])}: {warning}. Save first, then close it or pass --force."
            )
        names = ", ".join(describe(d) for d, _ in checked)
        raise SystemExit(
            f"Several devices are reachable: {names}. Choose one with --device."
        )
    print("Choose a device to deploy to:")
    for number, (device, running) in enumerate(checked, 1):
        print(f"  {number}) {describe(device)}" + (f" - {warning}" if running else ""))
    choices = "1" if len(checked) == 1 else f"1-{len(checked)}"
    while True:
        answer = input(f"Deploy to which device? [{choices}, Ctrl-C cancels] ").strip()
        if answer.isdecimal() and 1 <= int(answer) <= len(checked):
            return checked[int(answer) - 1][0]


def select_device(requested, force):
    physical = [
        d
        for d in devicectl_json("list", "devices")["devices"]
        if d["hardwareProperties"].get("reality") == "physical"
    ]
    if requested:
        candidates = [
            d
            for d in physical
            if requested
            in (
                d["deviceProperties"]["name"],
                d["hardwareProperties"]["udid"],
                d["identifier"],
            )
        ]
        if not candidates:
            raise SystemExit(
                f"No paired device matches {requested}. Check the name or UDID in: xcrun devicectl list devices"
            )
    else:
        candidates = [
            d
            for d in physical
            if d["hardwareProperties"].get("deviceType") == "iPad"
            and d["connectionProperties"].get("pairingState") == "paired"
            and d["connectionProperties"].get("transportType")
        ]
    # Device problems should surface now, not after a long build.
    checked = [
        (d, running)
        for d, running in ((d, game_running(d)) for d in candidates)
        if running is not None
    ]
    if not checked:
        raise SystemExit(
            "No paired iPad is reachable. Connect and unlock it, then check Xcode finished preparing it.\n"
            f"Full log: {LOG_PATH}"
        )
    if len(checked) == 1 and (force or not checked[0][1]):
        return checked[0][0]
    return pick(checked)


def signing_teams():
    """Teams for the Apple Accounts signed in to Xcode, as (id, name, type, free) tuples."""
    result = subprocess.run(
        ["defaults", "export", "com.apple.dt.Xcode", "-"], capture_output=True
    )
    if result.returncode:
        return []
    prefs = plistlib.loads(result.stdout).get("IDEProvisioningTeamByIdentifier", {})
    teams = {}
    for account in prefs.values():
        for team in account:
            teams[team["teamID"]] = (
                team["teamID"],
                team.get("teamName", ""),
                team.get("teamType", ""),
                bool(team.get("isFreeProvisioningTeam")),
            )
    return sorted(teams.values(), key=lambda t: (t[3], t[1]))


def describe_team(team):
    team_id, name, kind, free = team
    return f'{name} ({team_id}), {"free" if free else kind or "paid"}'


def select_team(requested):
    """The team to sign with: --team or the saved one, else the only or chosen team. A choice is saved."""
    teams = signing_teams()
    if requested or TEAM_ID:
        team_id = requested or TEAM_ID
        known = next((t for t in teams if t[0] == team_id), None)
        print(f"Signing with {describe_team(known) if known else team_id}", flush=True)
        return team_id
    if not teams:
        raise SystemExit(
            "No Apple Account team found. Sign in under Xcode > Settings > Apple Accounts, "
            "or pass --team with your team ID."
        )
    if len(teams) == 1:
        chosen = teams[0]
    elif not sys.stdin.isatty():
        raise SystemExit(
            "Several teams are available: "
            + "; ".join(map(describe_team, teams))
            + ". Choose one with --team."
        )
    else:
        print("Choose a team to sign with:")
        for number, team in enumerate(teams, 1):
            print(f"  {number}) {describe_team(team)}")
        while True:
            answer = input(
                f"Sign with which team? [1-{len(teams)}, Ctrl-C cancels] "
            ).strip()
            if answer.isdecimal() and 1 <= int(answer) <= len(teams):
                chosen = teams[int(answer) - 1]
                break
    print(f"Signing with {describe_team(chosen)}", flush=True)
    save_setting("team_id", chosen[0])
    return chosen[0]


def build(progress, team):
    command = [
        "xcodebuild",
        "-project",
        "FactorioProbe.xcodeproj",
        "-scheme",
        "FactorioProbe",
        "-configuration",
        "Debug",
        "-sdk",
        "iphoneos",
        "-destination",
        "generic/platform=iOS",
        "-derivedDataPath",
        str(DERIVED_DATA),
        "-allowProvisioningUpdates",
        f"FACTORIO_STAGE_FILE={STAGE_PATH}",
    ]
    command.append(f"DEVELOPMENT_TEAM={team}")
    # Without an override, the value in the Xcode project applies.
    if BUNDLE_ID:
        command.append(f"PRODUCT_BUNDLE_IDENTIFIER={BUNDLE_ID}")
    STAGE_PATH.unlink(missing_ok=True)
    run_step(progress, "xcode", [*command, "build"], stages=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--device",
        default=DEVICE_ID,
        help="Device name or UDID; without it, several iPads prompt for a choice",
    )
    parser.add_argument(
        "--team",
        help="Apple Developer team ID; without it, the saved or only team is used, or you choose one",
    )
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="Build and verify without touching a device",
    )
    parser.add_argument(
        "--skip-build", action="store_true", help="Install the existing Xcode product"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Do not ask to confirm when the game is running on the only device",
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help="Stay attached and stream the game output",
    )
    args = parser.parse_args()
    if args.build_only and args.skip_build:
        parser.error("--build-only and --skip-build exclude each other")
    if not VENV_PYTHON.exists():
        raise SystemExit("Run python3 tools/setup.py from the repository folder first.")
    if args.skip_build and not SIGNED_APP.is_dir():
        raise SystemExit(
            f"No signed app at {SIGNED_APP}. Build without --skip-build first."
        )
    team = None if args.skip_build else select_team(args.team)
    if args.team:
        save_setting("team_id", args.team)
    global LOG
    LOG = LOG_PATH.open("wb", buffering=0)
    device = name = None
    if not args.build_only:
        print("\nChecking devices...", flush=True)
        selected = select_device(args.device, args.force)
        device = selected["hardwareProperties"]["udid"]
        name = selected["deviceProperties"]["name"]
        print(f"Deploying to {describe(selected)}", flush=True)
    keys = ([] if args.skip_build else BUILD_STEPS) + ["verify"]
    if not args.build_only:
        keys += ["install"] if args.console else ["install", "launch"]
    progress = Progress(keys)
    if not args.skip_build:
        build(progress, team)
    bundle_id = plistlib.loads((SIGNED_APP / "Info.plist").read_bytes())[
        "CFBundleIdentifier"
    ]
    run_step(
        progress,
        "verify",
        [
            str(VENV_PYTHON),
            str(ROOT / "tools/verify.py"),
            "--signed-app",
            str(SIGNED_APP),
            "--bundle-id",
            bundle_id,
        ],
    )
    if args.build_only:
        print(f"Built and verified in {format_time(progress.finish())}.")
        return
    run_step(
        progress,
        "install",
        [
            "xcrun",
            "devicectl",
            "device",
            "install",
            "app",
            "--device",
            device,
            str(SIGNED_APP),
        ],
    )
    launch = [
        "xcrun",
        "devicectl",
        "device",
        "process",
        "launch",
        "--device",
        device,
        "--terminate-existing",
    ]
    if args.console:
        print(
            f"Installed on {name} in {format_time(progress.finish())}. Launching with game output:",
            flush=True,
        )
        subprocess.run([*launch, "--console", bundle_id], check=True)
        return
    run_step(progress, "launch", [*launch, bundle_id])
    print(f"Running on {name}. Finished in {format_time(progress.finish())}.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(
            f'Failed with exit code {error.returncode}: {" ".join(map(str, error.cmd))}\n'
            f"Full log: {LOG_PATH}"
        )
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\nCancelled.")
