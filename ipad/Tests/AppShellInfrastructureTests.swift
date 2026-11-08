import PaperCore
import XCTest

@testable import EXSCALIA

@MainActor
final class AppShellInfrastructureTests: XCTestCase {
    private func emptyDefaults() throws -> UserDefaults {
        try scratchDefaults("AppShellInfrastructure")
    }

    func testPaperRoutesStayInTheSceneThatRequestedThem() throws {
        let app = AppModel(defaults: try emptyDefaults(), launchArguments: [])
        let first = SceneModel()
        let second = SceneModel()
        first.attach(to: app)
        second.attach(to: app)

        first.openPaper(target: PaperTarget(notebook: "Work/One.paper", page: "p1"))
        XCTAssertEqual(first.surface, .paper)
        XCTAssertEqual(first.paperTarget.page, "p1")
        XCTAssertEqual(second.surface, .web)

        second.openPaper(target: PaperTarget(notebook: "Work/Two.paper", page: "p2"))
        XCTAssertEqual(second.surface, .paper)
        XCTAssertEqual(first.paperTarget.notebook, "Work/One.paper")
    }

    func testNativeWebNavigationIsScopedAndRejectsExternalPaths() throws {
        let app = AppModel(defaults: try emptyDefaults(), launchArguments: [])
        let first = SceneModel()
        let second = SceneModel()
        first.attach(to: app)
        second.attach(to: app)

        first.openOnWeb(path: "/paper/Work/One.paper#p1")
        XCTAssertEqual(first.surface, .web)
        XCTAssertEqual(first.consumeWebNavigation(), "/paper/Work/One.paper#p1")
        XCTAssertNil(second.consumeWebNavigation())

        first.openOnWeb(path: "https://example.com/")
        XCTAssertNil(first.consumeWebNavigation())
    }

    func testAppIntentStyleRouteWaitsForTheNextWindow() throws {
        let app = AppModel(defaults: try emptyDefaults(), launchArguments: [])
        app.route(.openPaper(PaperTarget(notebook: "Work/Queued.paper")))

        let scene = SceneModel()
        scene.attach(to: app)
        XCTAssertEqual(scene.surface, .paper)
        XCTAssertEqual(scene.paperTarget.notebook, "Work/Queued.paper")
    }

    func testReleaseGateNeverCallsSimulatorHardwareChecksPass() {
        let report = ReleaseGateReport.current(
            isSimulator: true, bundleIdentifier: "art.exscalia.ipad")
        XCTAssertFalse(report.isComplete)
        XCTAssertTrue(report.hasHardwareChecks)
        XCTAssertTrue(report.formatted.contains("HARDWARE-ONLY"))
    }

    /// One hardware check per item of RELEASE_GATE.md's physical pass,
    /// numbered as there, so neither names a control the other has lost.
    func testTheHardwareChecksFollowTheReleaseGateChecklist() throws {
        let file = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().appending(path: "RELEASE_GATE.md")
        let text = try String(contentsOf: file, encoding: .utf8)
        let pass = try XCTUnwrap(text.range(of: "## Required physical iPad pass"))
        let numbers = text[pass.upperBound...].split(separator: "\n").compactMap { line -> Int? in
            guard let dot = line.firstIndex(of: ".") else { return nil }
            return Int(line[..<dot])
        }
        XCTAssertEqual(numbers, Array(1...max(numbers.count, 1)))
        XCTAssertEqual(ReleaseGateReport.hardwareChecks.count, numbers.count)
        for (index, name) in ReleaseGateReport.hardwareChecks.enumerated() {
            XCTAssertTrue(name.hasPrefix("\(index + 1) "), name)
        }
        let report = ReleaseGateReport.current(isSimulator: true, bundleIdentifier: "art.exscalia.ipad")
        let hardware = report.checks.filter { $0.status == .hardwareOnly && $0.name != "Paper launch" }
        XCTAssertEqual(hardware.map(\.name), ReleaseGateReport.hardwareChecks)
        XCTAssertFalse(report.formatted.contains("two-finger pan"))
    }

    func testPaperLaunchPassesOnlyWhenTheStoreOpened() {
        func launch(_ opened: Bool?) -> ReleaseGateStatus? {
            ReleaseGateReport.current(
                isSimulator: true, bundleIdentifier: "art.exscalia.ipad", paperStoreOpens: opened
            ).checks.first { $0.name == "Paper launch" }?.status
        }
        XCTAssertEqual(launch(true), .pass)
        XCTAssertEqual(launch(false), .hardwareOnly)
        XCTAssertEqual(launch(nil), .hardwareOnly, "not checked is not a pass")
    }

    func testShortcutCatalogContainsBothPaperActions() {
        let shortcuts = EXSCALIAAppShortcuts.appShortcuts
        XCTAssertEqual(shortcuts.count, 2)
        XCTAssertEqual(EXSCALIAAppShortcuts.shortcutTitles, ["New Paper page", "Open last notebook"])
    }
}
