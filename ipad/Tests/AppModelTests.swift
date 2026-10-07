import PaperCore
import XCTest

@testable import EXSCALIA

@MainActor
final class AppModelTests: XCTestCase {
    private func emptyDefaults() throws -> UserDefaults {
        try scratchDefaults("AppModelTests")
    }

    func testStartsOnTheWebSurfaceAtTheDefaultServer() throws {
        let model = AppModel(defaults: try emptyDefaults(), launchArguments: [])
        XCTAssertEqual(model.surface, .web)
        XCTAssertEqual(model.server?.originString, "https://notes.exscalia.art")
    }

    func testLaunchArgumentsSelectSurfaceAndServer() throws {
        let model = AppModel(
            defaults: try emptyDefaults(),
            launchArguments: ["EXSCALIA", "-surface", "paper", "-server", "http://127.0.0.1:8600"])
        XCTAssertEqual(model.surface, .paper)
        XCTAssertEqual(model.server?.originString, "http://127.0.0.1:8600")
    }

    func testOpeningAndLeavingPaper() throws {
        let model = AppModel(defaults: try emptyDefaults(), launchArguments: [])
        model.openPaper(target: PaperTarget(notebook: "Work/Ideas.paper", page: "p1"))
        XCTAssertEqual(model.surface, .paper)
        XCTAssertEqual(model.paperTarget.notebook, "Work/Ideas.paper")
        model.showWeb()
        XCTAssertEqual(model.surface, .web)
    }

    func testSettingTheServerPersistsIt() throws {
        let defaults = try emptyDefaults()
        let model = AppModel(defaults: defaults, launchArguments: [])
        let settings = try XCTUnwrap(ServerSettings(urlString: "http://192.168.1.4:8600"))
        model.setServer(settings)
        XCTAssertEqual(
            defaults.string(forKey: ServerSettings.Key.serverURL), "http://192.168.1.4:8600")
        XCTAssertEqual(
            AppModel.storedServer(in: defaults)?.originString, "http://192.168.1.4:8600")
    }

    func testAStoreThatCannotOpenIsReportedAndRetryOpensItOnceItCan() async throws {
        let model = AppModel(defaults: try emptyDefaults(), launchArguments: [])
        // A fresh origin that never resolves, so its store root is this
        // test's alone and its sync engine talks to nobody.
        let host = "t\(UUID().uuidString.prefix(8).lowercased()).invalid"
        let settings = try XCTUnwrap(ServerSettings(urlString: "https://\(host)"))
        model.setServer(settings)
        let root = try settings.storeRoot()
        try FileManager.default.createDirectory(
            at: root.deletingLastPathComponent(), withIntermediateDirectories: true)
        // A file where the store's directory belongs: nothing can open it.
        try Data().write(to: root)
        defer { try? FileManager.default.removeItem(at: root) }

        XCTAssertNil(model.paperController)
        XCTAssertNotNil(model.paperControllerError)
        XCTAssertNil(model.openedPaperController)

        try FileManager.default.removeItem(at: root)
        XCTAssertNil(model.paperController, "the failure is remembered until Retry")

        model.retryPaperController()
        let controller = try XCTUnwrap(model.paperController)
        XCTAssertNil(model.paperControllerError)
        XCTAssertTrue(model.openedPaperController === controller)
        // `configureSync` starts the engine from a task of its own; let that
        // land before stopping it, or the stop would come first.
        try await Task.sleep(nanoseconds: 200_000_000)
        await controller.sync?.stop()
    }
}
