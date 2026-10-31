// A window over the existing tools: checks the build tools, shows the Factorio install, its mods,
// the signing team and the iPads, and runs tools/deploy.py. Built by Build.command; no dependencies.
import SwiftUI

// Minimum versions; setup.py enforces the same Python and iOS SDK floors.
let minXcode = [26], minSDK = [26], minPython = [3, 12], minCMake = [3, 24]

let stepLine = #/^\[(\d+)/(\d+)\] (.+)$/#
let toolPath = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
let root = URL(fileURLWithPath: Bundle.main.object(forInfoDictionaryKey: "RepoRoot") as? String ?? FileManager.default.currentDirectoryPath)
let deployLog = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("factorio-deploy.log")

func version(in text: String) -> [Int]? {
    guard let range = text.range(of: #"[0-9]+(\.[0-9]+)*"#, options: .regularExpression) else { return nil }
    return text[range].split(separator: ".").compactMap { Int($0) }
}

func atLeast(_ have: [Int], _ need: [Int]) -> Bool {
    for i in 0..<max(have.count, need.count) {
        let (h, n) = (i < have.count ? have[i] : 0, i < need.count ? need[i] : 0)
        if h != n { return h > n }
    }
    return true
}

func shown(_ v: [Int]) -> String { v.map(String.init).joined(separator: ".") }

func environment() -> [String: String] {
    var env = ProcessInfo.processInfo.environment
    env["PATH"] = (toolPath + (env["PATH"] ?? "").split(separator: ":").map(String.init)).joined(separator: ":")
    env["PYTHONUNBUFFERED"] = "1"
    return env
}

func process(_ executable: String, _ arguments: [String]) -> Process {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: executable)
    p.arguments = arguments
    p.currentDirectoryURL = root
    p.environment = environment()
    p.standardInput = FileHandle.nullDevice
    return p
}

/// Runs a short command off the main thread; nil when it cannot start, else its status and combined output.
func run(_ executable: String, _ arguments: [String]) async -> (status: Int32, output: String)? {
    await Task.detached {
        let p = process(executable, arguments)
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        do { try p.run() } catch { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return (p.terminationStatus, String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines))
    }.value
}

func find(_ name: String) -> [String] {
    toolPath.map { "\($0)/\(name)" }.filter { FileManager.default.isExecutableFile(atPath: $0) }
}

struct Check: Identifiable {
    let id: String
    var ok = false
    var detail = "Checking…"
    var fix: String?
}

struct Mod: Decodable, Identifiable {
    let name: String, state: String, version: String?
    var id: String { name }
}
struct Game: Decodable {
    let app: String, version: String?, supported: Bool, problem: String?, mods: [Mod], mods_problem: String?
    let user_data: String
}
struct Team: Decodable, Identifiable, Hashable {
    let id: String, name: String, kind: String, free: Bool
}
struct Teams: Decodable { let selected: String?, teams: [Team] }
struct Device: Decodable, Identifiable, Hashable {
    let id: String, name: String, model: String, reachable: Bool, running: Bool?
}
struct Devices: Decodable { let selected: String?, devices: [Device] }

@MainActor
final class Model: ObservableObject {
    @Published var checks = ["Xcode", "iOS SDK", "Python", "CMake"].map { Check(id: $0) }
    @Published var game: Game?
    @Published var gameError: String?
    @Published var teams: [Team] = []
    @Published var team: Team.ID?
    @Published var devices: [Device] = []
    @Published var device: Device.ID?
    @Published var devicesLoading = false
    @Published var deviceError: String?
    @Published var building = false
    @Published var step = ""
    @Published var progress = 0.0
    @Published var outcome: (text: String, ok: Bool)?
    @Published var log = ""
    var python = "/usr/bin/python3"
    var job: Process?
    var cancelled = false

    var checksPass: Bool { checks.allSatisfy(\.ok) }
    var ready: Bool {
        checksPass && game?.supported == true && team != nil &&
            devices.first { $0.id == device }?.reachable == true && !building
    }
    var selectedDevice: Device? { devices.first { $0.id == device } }

    func refresh() async {
        await runChecks()
        await loadGame()
        await loadDevices()
    }

    func setCheck(_ id: String, ok: Bool, _ detail: String, fix: String? = nil) {
        if let i = checks.firstIndex(where: { $0.id == id }) { checks[i] = Check(id: id, ok: ok, detail: detail, fix: fix) }
    }

    func runChecks() async {
        for i in checks.indices { checks[i] = Check(id: checks[i].id) }
        let store = "https://apps.apple.com/app/xcode/id497799835"
        // Without developer tools, xcodebuild and friends only pop up an installer dialog.
        let dir = (await run("/usr/bin/xcode-select", ["-p"]))?.output ?? ""
        var haveXcode = false
        if dir.isEmpty || dir.contains("CommandLineTools") {
            setCheck("Xcode", ok: false, dir.isEmpty ? "Not installed" : "Only Command Line Tools are selected",
                     fix: "Install Xcode from the App Store and open it once: \(store)")
        } else if let r = await run("/usr/bin/xcodebuild", ["-version"]), r.status == 0, let v = version(in: r.output) {
            haveXcode = true
            if !atLeast(v, minXcode) {
                setCheck("Xcode", ok: false, "Xcode \(shown(v)) is too old; \(shown(minXcode)) or newer is required",
                         fix: "Update it from the App Store: \(store)")
            } else if (await run("/usr/bin/xcodebuild", ["-license", "check"]))?.status != 0 {
                setCheck("Xcode", ok: false, "Xcode \(shown(v)): license not accepted", fix: "Run: sudo xcodebuild -license accept")
            } else if (await run("/usr/bin/xcodebuild", ["-checkFirstLaunchStatus"]))?.status != 0 {
                setCheck("Xcode", ok: false, "Xcode \(shown(v)): first launch not finished",
                         fix: "Open Xcode once, or run: sudo xcodebuild -runFirstLaunch")
            } else {
                setCheck("Xcode", ok: true, "Xcode \(shown(v))")
            }
        } else {
            setCheck("Xcode", ok: false, "xcodebuild does not run", fix: "Open Xcode once and finish its setup.")
        }
        if !haveXcode {
            setCheck("iOS SDK", ok: false, "Checked once Xcode is fixed")
        } else if let r = await run("/usr/bin/xcrun", ["--sdk", "iphoneos", "--show-sdk-version"]), r.status == 0, let v = version(in: r.output) {
            setCheck("iOS SDK", ok: atLeast(v, minSDK), atLeast(v, minSDK) ? "iOS SDK \(shown(v))" : "iOS SDK \(shown(v)) is too old; \(shown(minSDK)) or newer is required",
                     fix: atLeast(v, minSDK) ? nil : "Update Xcode from the App Store.")
        } else {
            setCheck("iOS SDK", ok: false, "Not found", fix: "Install iOS under Xcode > Settings > Components, or run: xcodebuild -downloadPlatform iOS")
        }
        let venv = root.appendingPathComponent(".venv/bin/python3").path
        await tool("Python", candidates: (FileManager.default.isExecutableFile(atPath: venv) ? [venv] : []) + find("python3"),
                   formula: "python", minimum: minPython, haveDeveloperTools: !dir.isEmpty)
        await tool("CMake", candidates: find("cmake"), formula: "cmake", minimum: minCMake, haveDeveloperTools: !dir.isEmpty)
    }

    /// Passes with the first candidate that is new enough; else reports the best one found.
    func tool(_ name: String, candidates: [String], formula: String, minimum: [Int], haveDeveloperTools: Bool) async {
        let fix = "Run: brew install \(formula)  (Homebrew: https://brew.sh)"
        var best: (path: String, version: [Int])?
        for path in candidates where haveDeveloperTools || !path.hasPrefix("/usr/bin/") {
            guard let r = await run(path, ["--version"]), let v = version(in: r.output) else { continue }
            if atLeast(v, minimum) {
                if name == "Python" { python = path }
                return setCheck(name, ok: true, "\(name) \(shown(v))")
            }
            if best == nil || atLeast(v, best!.version) { best = (path, v) }
        }
        if let best {
            setCheck(name, ok: false, "\(name) \(shown(best.version)) is too old; \(shown(minimum)) or newer is required", fix: fix)
        } else {
            setCheck(name, ok: false, "Not installed; \(shown(minimum)) or newer is required", fix: fix)
        }
    }

    func status<T: Decodable>(_ command: String, as type: T.Type) async throws -> T {
        guard let r = await run(python, ["tools/status.py", command]) else { throw Failure(text: "Cannot run \(python)") }
        guard r.status == 0, let data = r.output.split(separator: "\n").last?.data(using: .utf8) else { throw Failure(text: r.output) }
        return try JSONDecoder().decode(T.self, from: data)
    }

    struct Failure: Error { let text: String }

    func loadGame() async {
        do {
            game = try await status("game", as: Game.self)
            gameError = nil
            let list = try await status("teams", as: Teams.self)
            teams = list.teams
            if !teams.contains(where: { $0.id == team }) { team = list.selected.flatMap { s in teams.first { $0.id == s }?.id } ?? (teams.count == 1 ? teams[0].id : nil) }
        } catch {
            game = nil
            gameError = (error as? Failure)?.text ?? "\(error)"
        }
    }

    func loadDevices() async {
        devicesLoading = true
        defer { devicesLoading = false }
        do {
            let list = try await status("devices", as: Devices.self)
            devices = list.devices
            deviceError = nil
            if !devices.contains(where: { $0.id == device }) {
                device = list.selected.flatMap { s in devices.first { $0.id == s }?.id } ?? devices.first(where: \.reachable)?.id
            }
        } catch {
            devices = []
            deviceError = (error as? Failure)?.text ?? "\(error)"
        }
    }

    /// Runs setup first when the virtualenv does not exist yet, then deploy.
    func ship() {
        guard let team, let device else { return }
        building = true
        cancelled = false
        outcome = nil
        log = ""
        progress = 0
        step = "Starting"
        try? FileManager.default.removeItem(at: deployLog)
        var jobs: [[String]] = []
        if !FileManager.default.fileExists(atPath: root.appendingPathComponent(".venv/bin/python").path) {
            jobs.append(["tools/setup.py"])
        }
        jobs.append(["tools/deploy.py", "--team", team, "--device", device, "--force"])
        Task {
            var failure: String?
            for (n, arguments) in jobs.enumerated() {
                if let problem = await runJob(arguments, isSetup: arguments[0].hasSuffix("setup.py"), position: n) { failure = problem; break }
            }
            building = false
            job = nil
            if cancelled { outcome = ("Cancelled.", false) }
            else if let failure { outcome = (failure, false) }
            else { outcome = ("Installed and launched on \(selectedDevice?.name ?? "the iPad").", true); progress = 1 }
            await loadDevices()
        }
    }

    func runJob(_ arguments: [String], isSetup: Bool, position: Int) async -> String? {
        let p = process(python, [root.appendingPathComponent(arguments[0]).path] + arguments.dropFirst())
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        var output = ""
        if isSetup { step = "Setting up" }
        let lines = Task { @MainActor in
            var pending = ""
            for try await byte in pipe.fileHandleForReading.bytes {
                if byte == 10 || byte == 13 {
                    handle(line: pending, isSetup: isSetup)
                    output += pending + "\n"
                    pending = ""
                } else { pending += String(UnicodeScalar(byte)) }
            }
        }
        let tail = Task { @MainActor in
            while !Task.isCancelled && !isSetup {
                if let text = try? String(contentsOf: deployLog, encoding: .utf8) { log = String(text.suffix(6000)) }
                try? await Task.sleep(for: .seconds(1))
            }
        }
        do { try p.run() } catch { tail.cancel(); return "Cannot start \(python): \(error.localizedDescription)" }
        job = p
        await Task.detached { p.waitUntilExit() }.value
        _ = try? await lines.value
        tail.cancel()
        if let text = try? String(contentsOf: deployLog, encoding: .utf8) { log = String(text.suffix(6000)) }
        if p.terminationStatus == 0 { return nil }
        let reason = output.split(separator: "\n").suffix(6).joined(separator: "\n")
        return reason.isEmpty ? "\(arguments[0]) failed (exit \(p.terminationStatus))." : reason
    }

    func handle(line: String, isSetup: Bool) {
        if isSetup { log += line + "\n"; return }
        // deploy.py prints "[3/9] Building SDL" when it is not on a terminal.
        if let m = line.firstMatch(of: stepLine), let i = Double(m.1), let n = Double(m.2) {
            step = String(m.3)
            progress = (i - 1) / n
        }
    }

    func cancel() {
        cancelled = true
        job?.interrupt()
    }
}

struct Card<Content: View>: View {
    let title: String
    var trailing: AnyView?
    @ViewBuilder let content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack { Text(title).font(.headline); Spacer(); trailing }
            content
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 10))
    }
}

struct Mark: View {
    let ok: Bool?
    var body: some View {
        switch ok {
        case true?: Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
        case false?: Image(systemName: "xmark.circle.fill").foregroundStyle(.red)
        case nil: ProgressView().controlSize(.small)
        }
    }
}

struct ContentView: View {
    @StateObject private var model = Model()
    @State private var confirming = false

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                Card(title: "Build tools") {
                    ForEach(model.checks) { check in
                        HStack(alignment: .firstTextBaseline) {
                            Mark(ok: check.detail == "Checking…" ? nil : check.ok)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(check.detail)
                                if let fix = check.fix { Text(fix).font(.caption).foregroundStyle(.secondary).textSelection(.enabled) }
                            }
                        }
                    }
                }
                Card(title: "Factorio") {
                    if let game = model.game {
                        HStack(alignment: .firstTextBaseline) {
                            Mark(ok: game.supported)
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Factorio \(game.version ?? "?")" + (game.supported ? "" : " is not supported"))
                                Text(game.app).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                                if let problem = game.problem { Text(problem).font(.caption).foregroundStyle(.red).textSelection(.enabled) }
                            }
                        }
                    } else if let error = model.gameError {
                        Text(error).foregroundStyle(.red).textSelection(.enabled)
                    } else { ProgressView().controlSize(.small) }
                }
                Card(title: "Mods to Bundle") {
                    if let game = model.game {
                        if game.mods.isEmpty && game.mods_problem == nil { Text("No third-party mods enabled.").foregroundStyle(.secondary) }
                        ForEach(game.mods) { mod in
                            HStack {
                                Text(mod.name)
                                Spacer()
                                Text(mod.version ?? "").foregroundStyle(.secondary).monospacedDigit()
                                Text(mod.state == "bundled" ? "will install" : mod.state == "missing" ? "no compatible zip" : "disabled")
                                    .font(.caption)
                                    .foregroundStyle(mod.state == "bundled" ? .green : mod.state == "missing" ? .orange : .secondary)
                                    .frame(width: 110, alignment: .trailing)
                            }
                        }
                        if let problem = game.mods_problem { Text(problem).font(.caption).foregroundStyle(.red).textSelection(.enabled) }
                        Text("Enabled mods are copied from \(game.user_data). Change them in Factorio, then refresh.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                Card(title: "Signing team") {
                    if model.teams.isEmpty {
                        Text("No Apple Account found. Sign in under Xcode > Settings > Apple Accounts.").foregroundStyle(.secondary)
                    } else {
                        Picker("Team", selection: $model.team) {
                            Text("Choose a team").tag(Team.ID?.none)
                            ForEach(model.teams) { team in
                                Text("\(team.name) (\(team.id)), \(team.free ? "free" : team.kind)").tag(Team.ID?.some(team.id))
                            }
                        }.labelsHidden()
                    }
                }
                Card(title: "Install Target", trailing: AnyView(Button("Rescan") { Task { await model.loadDevices() } }.disabled(model.devicesLoading || model.building))) {
                    if model.devicesLoading { ProgressView().controlSize(.small) }
                    else if let error = model.deviceError { Text(error).foregroundStyle(.red).textSelection(.enabled) }
                    else if model.devices.isEmpty { Text("No iPad found. Connect and unlock it, then rescan.").foregroundStyle(.secondary) }
                    else {
                        Picker("iPad", selection: $model.device) {
                            Text("Choose an iPad").tag(Device.ID?.none)
                            ForEach(model.devices) { d in
                                Text("\(d.name), \(d.model)" + (d.reachable ? "" : " (not reachable)")).tag(Device.ID?.some(d.id))
                            }
                        }.labelsHidden()
                        if model.selectedDevice?.running == true {
                            Label("The game is running on this iPad. Installing closes it and loses unsaved progress.", systemImage: "exclamationmark.triangle.fill")
                                .font(.caption).foregroundStyle(.orange)
                        }
                    }
                }
                Card(title: "Build & ship") {
                    HStack {
                        if model.building {
                            Button("Cancel", role: .cancel) { model.cancel() }
                        } else {
                            Button("Build & Ship to iPad") {
                                if model.selectedDevice?.running == true { confirming = true } else { model.ship() }
                            }
                            .keyboardShortcut(.defaultAction)
                            .disabled(!model.ready)
                        }
                        Button("Refresh") { Task { await model.refresh() } }.disabled(model.building)
                        Spacer()
                        if model.building { Text(model.step).foregroundStyle(.secondary) }
                    }
                    if model.building { ProgressView(value: model.progress) }
                    if let outcome = model.outcome {
                        Label(outcome.text, systemImage: outcome.ok ? "checkmark.circle.fill" : "xmark.octagon.fill")
                            .foregroundStyle(outcome.ok ? .green : .red).textSelection(.enabled)
                    }
                    if !model.log.isEmpty {
                        ScrollView { Text(model.log).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading) }
                            .defaultScrollAnchor(.bottom)
                            .frame(height: 140)
                            .background(.black.opacity(0.08), in: RoundedRectangle(cornerRadius: 6))
                    }
                }
            }
            .padding(16)
        }
        .frame(minWidth: 560, minHeight: 640)
        .task { await model.refresh() }
        .confirmationDialog("Close the game on \(model.selectedDevice?.name ?? "the iPad")?", isPresented: $confirming) {
            Button("Install and close the game", role: .destructive) { model.ship() }
        } message: { Text("Installing terminates the running game. Save first.") }
    }
}

@main
struct InstallerApp: App {
    var body: some Scene {
        WindowGroup("Factorio for iPad") { ContentView() }
            .defaultSize(width: 620, height: 860)
    }
}
