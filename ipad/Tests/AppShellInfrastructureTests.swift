import PaperCore
import XCTest

@testable import EXSCALIA

@MainActor
final class AppShellInfrastructureTests: XCTestCase {
    private func emptyDefaults() throws -> UserDefaults {
        try XCTUnwrap(UserDefaults(suiteName: "AppShellInfrastructure-\(UUID().uuidString)"))
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
