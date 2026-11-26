import Foundation
import XCTest

@testable import PaperCore

/// A live event from another device that arrives while a cycle is running.
final class SyncWakeTests: XCTestCase {
    private var root: URL!
    private var store: PaperStore!

    override func setUpWithError() throws {
        try super.setUpWithError()
        MockPaperServer.state.reset()
        root = FileManager.default.temporaryDirectory
            .appending(path: "SyncWakeTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        store = try PaperStore(root: root)
    }

    override func tearDownWithError() throws {
        MockPaperServer.state.releaseHeld()
        MockPaperServer.state.reset()
        store = nil
        if let root { try? FileManager.default.removeItem(at: root) }
        root = nil
        try super.tearDownWithError()
    }

    /// (D5) The event for a delete made upstream arrives while this device's
    /// pull is out with a feed read before the delete. The wake found no
    /// sleep to cut short and was lost: the loop then slept the whole poll
    /// interval with the delete unpulled. It runs again at once now.
    func testAWakeDuringACycleRunsTheLoopAgainAtOnce() async throws {
        let notebook = try store.createNotebook(title: "Doomed", pageCount: 1)
        let settings = try XCTUnwrap(ServerSettings(urlString: "https://paper.test"))
        let api = PaperAPI(
            settings: settings, auth: AnonymousAuthProvider(), session: MockPaperServer.makeSession())
        let engine = SyncEngine(
            store: store, api: api, clientID: "ipad", clock: SystemSyncClock(), jitter: { $0 })
        await engine.syncNow()
        await engine.syncNow()
        XCTAssertNotNil(try store.notebook(path: notebook.path))

        MockPaperServer.state.holdNext(pathContaining: "/api/paper/changes")
        await engine.start()
        try await MockPaperServer.state.waitUntilHeld()
        // Deleted on the web meanwhile; its live event wakes the engine.
        MockPaperServer.state.recordDeletion(path: notebook.path, kind: "notebook")
        await engine.kick()
        MockPaperServer.state.releaseHeld()

        var gone = false
        for _ in 0..<100 {
            if (try? store.notebook(path: notebook.path)) == nil {
                gone = true
                break
            }
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        await engine.stop()
        XCTAssertTrue(gone, "the delete waited for the next poll")
    }
}
