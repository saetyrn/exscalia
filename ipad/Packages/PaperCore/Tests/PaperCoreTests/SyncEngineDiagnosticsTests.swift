import Foundation
import XCTest

@testable import PaperCore

/// What a failure leaves behind: the engine names the op or page and what the
/// server said, keeps it across launches, and never lets a later success
/// erase it.
final class SyncEngineDiagnosticsTests: XCTestCase {
    private var root: URL!
    private var store: PaperStore!
    private var clock: TestClock!
    private let clientID = "ipad-test-client"
    private let lastErrorKey = "sync.last_error"

    override func setUpWithError() throws {
        try super.setUpWithError()
        MockPaperServer.state.reset()
        root = FileManager.default.temporaryDirectory
            .appending(
                path: "SyncEngineDiagnosticsTests-\(UUID().uuidString)",
                directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        store = try PaperStore(root: root)
        clock = TestClock()
    }

    override func tearDownWithError() throws {
        MockPaperServer.state.reset()
        store = nil
        if let root { try? FileManager.default.removeItem(at: root) }
        root = nil
        try super.tearDownWithError()
    }

    // MARK: - Fixtures

    private func makeEngine() throws -> SyncEngine {
        let settings = try XCTUnwrap(ServerSettings(urlString: "https://paper.test"))
        let api = PaperAPI(
            settings: settings, auth: AnonymousAuthProvider(),
            session: MockPaperServer.makeSession())
        return SyncEngine(
            store: store, api: api, clientID: clientID, clock: clock, jitter: { $0 })
    }

    /// A notebook with one inked page, queued and not yet pushed.
    @discardableResult
    private func seedLocalNotebook() throws -> Notebook {
        let notebook = try store.createNotebook(title: "Ideas", folder: "Work")
        let pageID = try XCTUnwrap(notebook.pages.first)
        try store.writeDrawing(Data("stroke".utf8), notebookPath: notebook.path, pageID: pageID)
        try store.journal.append(
            .init(kind: .pagePut, notebookPath: notebook.path, pageID: pageID))
        return notebook
    }

    private func failNext(_ times: Int, status: Int = 500, containing fragment: String? = nil) {
        MockPaperServer.state.failPathsContaining = fragment
        MockPaperServer.state.failStatus = status
        MockPaperServer.state.failuresRemaining = times
    }

    // MARK: - Push

    func testAServerErrorOnThePushIsKeptWithTheOpAndTheStatus() async throws {
        let notebook = try seedLocalNotebook()
        failNext(1)
        let engine = try makeEngine()

        let sent = await engine.pushPending()

        XCTAssertEqual(sent, 0)
        let lastErrorValue = await engine.lastError
        let lastError = try XCTUnwrap(lastErrorValue)
        XCTAssertTrue(lastError.contains("500"), lastError)
        XCTAssertTrue(lastError.contains("notebookCreate"), lastError)
        XCTAssertTrue(lastError.contains(notebook.path), lastError)
        let at = await engine.lastErrorAt
        XCTAssertEqual(at, clock.now)
        let stored = try XCTUnwrap(store.syncValue(lastErrorKey))
        XCTAssertTrue(stored.hasSuffix(lastError), stored)
    }

    func testAParkedOpIsKeptToo() async throws {
        let notebook = try seedLocalNotebook()
        let engine = try makeEngine()
        await engine.pushPending()
        let clean = await engine.lastError
        XCTAssertNil(clean, "nothing failed yet")

        let pageID = try XCTUnwrap(notebook.pages.first)
        try store.journal.append(
            .init(kind: .pagePut, notebookPath: notebook.path, pageID: pageID))
        MockPaperServer.state.rejectNextPut = true
        await engine.pushPending()

        let lastErrorValue = await engine.lastError
        let lastError = try XCTUnwrap(lastErrorValue)
        XCTAssertTrue(lastError.contains("pagePut parked"), lastError)
        XCTAssertTrue(lastError.contains(pageID), lastError)
        XCTAssertTrue(lastError.contains("400"), lastError)
    }

    // MARK: - Pull

    func testAFailedBlobFetchNamesThePage() async throws {
        let path = "paper/Shared.paper"
        let pageID = "pagealpha"
        MockPaperServer.state.seed(
            notebook: path, id: "nb-shared", title: "Shared",
            pages: [.init(id: pageID, revision: 2, drawing: Data("ink".utf8))])
        failNext(.max, status: 503, containing: "/api/paper/blob/")
        let engine = try makeEngine()

        await engine.pull()

        let lastErrorValue = await engine.lastError
        let lastError = try XCTUnwrap(lastErrorValue)
        XCTAssertTrue(lastError.contains(pageID), lastError)
        XCTAssertTrue(lastError.contains("drawing.pkdrawing"), lastError)
        XCTAssertTrue(lastError.contains("503"), lastError)
        XCTAssertNotNil(store.syncValue(lastErrorKey))
    }

    func testAFailedFeedIsKept() async throws {
        failNext(1, status: 502, containing: "/api/paper/changes")
        let engine = try makeEngine()

        await engine.pull()

        let lastErrorValue = await engine.lastError
        let lastError = try XCTUnwrap(lastErrorValue)
        XCTAssertTrue(lastError.contains("pull changes"), lastError)
        XCTAssertTrue(lastError.contains("502"), lastError)
    }

    // MARK: - Lifetime

    func testTheNextLaunchReadsTheLastError() async throws {
        try seedLocalNotebook()
        failNext(1)
        let first = try makeEngine()
        await first.pushPending()
        let recordedValue = await first.lastError
        let recorded = try XCTUnwrap(recordedValue)
        let recordedAt = await first.lastErrorAt

        let second = try makeEngine()

        let loaded = await second.lastError
        XCTAssertEqual(loaded, recorded)
        let loadedAt = await second.lastErrorAt
        XCTAssertEqual(loadedAt, recordedAt)
    }

    func testAValueWithoutATimeIsStillShown() async throws {
        try store.setSyncValue("something went wrong", for: lastErrorKey)

        let engine = try makeEngine()

        let loaded = await engine.lastError
        XCTAssertEqual(loaded, "something went wrong")
        let at = await engine.lastErrorAt
        XCTAssertNil(at)
    }

    func testASuccessfulSyncLeavesTheLastErrorAlone() async throws {
        try seedLocalNotebook()
        failNext(1)
        let engine = try makeEngine()
        await engine.pushPending()
        let recordedValue = await engine.lastError
        let recorded = try XCTUnwrap(recordedValue)
        let stored = store.syncValue(lastErrorKey)

        await engine.syncNow()

        let pending = try store.journal.pendingCount()
        XCTAssertEqual(pending, 0, "the retry went through")
        let state = await engine.state
        guard case .synced = state else { return XCTFail("expected synced, got \(state)") }
        let after = await engine.lastError
        XCTAssertEqual(after, recorded, "a diagnostic, not a state")
        XCTAssertEqual(store.syncValue(lastErrorKey), stored)
    }

    // MARK: - Transition log

    func testTheTransitionLogNeverQuotesAnAttentionMessage() {
        XCTAssertEqual(SyncEngine.label(.syncing(count: 3)), "syncing(3)")
        XCTAssertEqual(SyncEngine.label(.synced(at: Date())), "synced")
        XCTAssertEqual(
            SyncEngine.label(.attention(message: "a page was not sent — 400: paper/Work")),
            "attention")
    }
}
