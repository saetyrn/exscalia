import Foundation
import PaperCore

/// The result of a release-gate item. `hardwareOnly` is intentionally not a
/// pass: simulator runs cannot validate Pencil input, background execution or
/// provisioning on a real iPad.
enum ReleaseGateStatus: String, Equatable {
    case pass = "PASS"
    case hardwareOnly = "HARDWARE-ONLY"
}

struct ReleaseGateCheck: Equatable {
    let name: String
    let status: ReleaseGateStatus
    let detail: String
}

struct ReleaseGateReport: Equatable {
    let isSimulator: Bool
    let checks: [ReleaseGateCheck]

    var hasHardwareChecks: Bool {
        checks.contains { $0.status == .hardwareOnly }
    }

    /// A report is never "device ready" until the manual hardware items have
    /// been exercised. This prevents a green simulator run being mistaken for
    /// an iPad release sign-off.
    var isComplete: Bool { !hasHardwareChecks }

    var formatted: String {
        let platform = isSimulator ? "Simulator" : "Physical iPad"
        var lines = ["EXSCALIA physical release gate (\(platform))"]
        lines += checks.map { "\($0.status.rawValue.padding(toLength: 13, withPad: " ", startingAt: 0)) \($0.name): \($0.detail)" }
        lines.append(isComplete ? "RESULT: complete" : "RESULT: hardware validation required")
        return lines.joined(separator: "\n")
    }

    /// `paperStoreOpens` is whether this run opened the local Paper store;
    /// `nil` when nothing tried, which is not a pass.
    static func current(
        isSimulator: Bool = ReleaseGateReport.defaultIsSimulator,
        bundleIdentifier: String? = Bundle.main.bundleIdentifier,
        paperStoreOpens: Bool? = nil
    ) -> ReleaseGateReport {
        var checks: [ReleaseGateCheck] = []
        checks.append(
            ReleaseGateCheck(
                name: "Bundle ID",
                status: bundleIdentifier == "art.exscalia.ipad" ? .pass : .hardwareOnly,
                detail: bundleIdentifier == "art.exscalia.ipad"
                    ? "art.exscalia.ipad (unchanged)"
                    : "expected art.exscalia.ipad; inspect signing configuration"
            )
        )
        let launchDetail: String
        switch paperStoreOpens {
        case true?: launchDetail = "local store opened"
        case false?: launchDetail = "local store did not open; check the server setting"
        case nil: launchDetail = "local store not checked"
        }
        checks.append(
            ReleaseGateCheck(
                name: "Paper launch",
                status: paperStoreOpens == true ? .pass : .hardwareOnly,
                detail: launchDetail
            )
        )

        let hardwareDetail = isSimulator
            ? "not available in Simulator; run on the signed iPad"
            : "manual check required before sign-off"
        // One per item of RELEASE_GATE.md's physical pass, in its order, so the
        // report reads as the checklist does.
        for name in Self.hardwareChecks {
            checks.append(ReleaseGateCheck(name: name, status: .hardwareOnly, detail: hardwareDetail))
        }
        return ReleaseGateReport(isSimulator: isSimulator, checks: checks)
    }

    /// The items of RELEASE_GATE.md's "Required physical iPad pass", numbered
    /// as there. Keep the two in step.
    static let hardwareChecks = [
        "1 Sustained Pencil session: pressure, tilt, palm rejection",
        "2 A pan, with one finger or two, never draws; pinch zoom",
        "3 Pencil hover preview",
        "4 Background, lock and force-quit keep the ink",
        "5 Scanner and camera, then force-quit",
        "6 Large PDF import, annotation and export",
        "7 Airplane-mode sync and a two-device conflict",
        "8 Two windows on one notebook",
        "9 Free-team provisioning and 7-day re-sign",
        "10 A finger never inks; a finger tap selects",
        "11 A moved lasso selection stays moved",
        "12 Two- and three-finger double-tap undo and redo",
        "13 Pencil double-tap and squeeze",
        "14 Draw Shape",
        "15 Whiteboard zoom, pan and minimap",
        "16 Dark paper ink",
        "17 Folder rename syncs without downloads",
        "18 Tabs, keys and Open in New Window",
        "19 Convert to Whiteboard",
    ]

    private static var defaultIsSimulator: Bool {
        #if targetEnvironment(simulator)
            return true
        #else
            return false
        #endif
    }
}

/// A DEBUG-only executable harness. Launch with `-debugReleaseGate` and read
/// the report from Xcode's console or `simctl spawn log stream`; it is safe to
/// run in a simulator and never marks hardware-only checks as passed.
///
/// It runs at launch, before any sync, so it says nothing about sync. The
/// sync engine logs every failure it absorbs and every state it sends the
/// pill: `xcrun simctl spawn booted log stream --predicate 'subsystem ==
/// "art.exscalia.ipad"'` (add `--level info` for the state transitions), or
/// Console.app with the same filter on the iPad.
enum ReleaseGateDiagnostics {
    static let argument = "-debugReleaseGate"

    @MainActor
    static func runIfRequested(arguments: [String] = CommandLine.arguments) {
        #if DEBUG
            guard arguments.contains(argument) else { return }
            print(ReleaseGateReport.current(paperStoreOpens: paperStoreOpens()).formatted)
        #endif
    }

    /// Opens the Paper store where `AppModel` keeps it for the configured
    /// server, and lets it go again.
    @MainActor
    private static func paperStoreOpens() -> Bool {
        guard let root = try? AppModel.storedServer(in: .standard)?.storeRoot() else {
            return false
        }
        return (try? PaperStore(root: root)) != nil
    }
}
