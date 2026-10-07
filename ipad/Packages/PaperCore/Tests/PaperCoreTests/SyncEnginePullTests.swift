import Foundation
import XCTest

@testable import PaperCore

/// What a pull may claim: a page is written only once all of it arrived, the
/// change cursor moves only past changes that landed, and a transcript the
/// server wrote without touching the ink still comes down.
final class SyncEnginePullTests: XCTestCase {
    private var root: URL!
    private var store: PaperStore!
    private var clock: TestClock!
    private let clientID = "ipad-test-client"
    private let path = "paper/Shared.paper"
    private let pageID = "pagealpha"

    override func setUpWithError() throws {
        try super.setUpWithError()
        MockPaperServer.state.reset()
        root = FileManager.default.temporaryDirectory
            .appending(
                path: "SyncEnginePullTests-\(UUID().uuidString)", directoryHint: .isDirectory)
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

    private func seedRemotePage(_ page: MockServerState.Page) {
        MockPaperServer.state.seed(notebook: path, id: "nb-shared", title: "Shared", pages: [page])
    }

    private func failRequests(containing fragment: String, status: Int = 503, times: Int = .max) {
        MockPaperServer.state.failPathsContaining = fragment
        MockPaperServer.state.failStatus = status
        MockPaperServer.state.failuresRemaining = times
    }

    private func heal() {
        MockPaperServer.state.failuresRemaining = 0
        MockPaperServer.state.failPathsContaining = nil
        MockPaperServer.state.failPathEquals = nil
    }

    private var cursor: String? { store.syncValue("changes.seq") }
    private var drawingETagKey: String { "etag.\(path)/pages/\(pageID).drawing.pkdrawing" }

    private func exists(_ url: URL) -> Bool {
        FileManager.default.fileExists(atPath: url.path(percentEncoded: false))
    }

    private func transcript(_ text: String, ai: String? = nil, revision: Int = 0) throws -> Data {
        try PaperJSON.encode(
            PageText(
                lines: [
                    RecognizedLine(
                        text: text, box: PageObject.Frame(x: 60, y: 120, width: 200, height: 24),
                        confidence: 0.9)
                ],
                transcriptAI: ai.map { AITranscript(text: $0, model: "test", revision: revision) }))
    }

    // MARK: - All of a page or none of it

    func testAFailedBlobWritesNothingAndKeepsTheCursor() async throws {
        seedRemotePage(.init(id: pageID, revision: 2, drawing: Data("v2".utf8)))
        let engine = try makeEngine()
        await engine.pull()
        XCTAssertEqual(store.localRevision(notebookPath: path, pageID: pageID), 2)
        let before = try XCTUnwrap(cursor)

        // Another device draws. The page GET gets through; the ink does not.
        MockPaperServer.state.updatePage(notebook: path, id: pageID) {
            $0.revision = 3
            $0.drawing = Data("v3".utf8)
        }
        failRequests(containing: "/api/paper/blob/")
        await engine.pull()

        XCTAssertEqual(
            store.localRevision(notebookPath: path, pageID: pageID), 2,
            "page.json still names the ink that is actually here")
        XCTAssertEqual(store.drawingData(notebookPath: path, pageID: pageID), Data("v2".utf8))
        XCTAssertEqual(cursor, before, "the change is not stepped past")
        let failures = await engine.failureCount
        XCTAssertEqual(failures, 1, "an incomplete pull is a step of backoff")

        // Healed: the same change is asked for again and lands whole.
        heal()
        await engine.pull()
        XCTAssertEqual(store.localRevision(notebookPath: path, pageID: pageID), 3)
        XCTAssertEqual(store.drawingData(notebookPath: path, pageID: pageID), Data("v3".utf8))
        XCTAssertNotEqual(cursor, before)
        let healed = await engine.failureCount
        XCTAssertEqual(healed, 0)
    }

    func testAFailedBlobOnAFreshDeviceLeavesNoPageBehind() async throws {
        seedRemotePage(.init(id: pageID, revision: 3, drawing: Data("ink".utf8)))
        let engine = try makeEngine()
        failRequests(containing: "/api/paper/blob/")

        await engine.pull()

        XCTAssertFalse(exists(store.paths.pageJSON(path, pageID)))
        XCTAssertEqual(store.localRevision(notebookPath: path, pageID: pageID), 0)
        XCTAssertNil(cursor, "a full reconcile that did not finish writes no cursor")
        XCTAssertNil(store.syncValue(drawingETagKey))

        heal()
        await engine.pull()
        XCTAssertEqual(store.drawingData(notebookPath: path, pageID: pageID), Data("ink".utf8))
        XCTAssertEqual(store.localRevision(notebookPath: path, pageID: pageID), 3)
        XCTAssertNotNil(cursor)
    }

    /// The consequence the rule exists for: after a pull that did not get the
    /// ink, this device's next stroke is still a conflict, not an overwrite.
    func testAStrokeAfterAFailedPullConflictsRatherThanReplacingTheOtherInk() async throws {
        seedRemotePage(.init(id: pageID, revision: 2, drawing: Data("v2".utf8)))
        let engine = try makeEngine()
        await engine.pull()
        MockPaperServer.state.updatePage(notebook: path, id: pageID) {
            $0.revision = 3
            $0.drawing = Data("theirs".utf8)
        }
        failRequests(containing: "/api/paper/blob/")
        await engine.pull()
        heal()

        try store.writeDrawing(Data("mine".utf8), notebookPath: path, pageID: pageID)
        try store.journal.append(.init(kind: .pagePut, notebookPath: path, pageID: pageID))
        await engine.pushPending()

        let server = MockPaperServer.state.withState { $0[path] }
        XCTAssertEqual(server?.pages[pageID]?.drawing, Data("theirs".utf8))
        XCTAssertEqual(server?.pages[pageID]?.revision, 3)
        let sibling = "\(pageID).conflict-20260902120000"
        XCTAssertEqual(server?.pages[sibling]?.drawing, Data("mine".utf8))
        XCTAssertEqual(store.drawingData(notebookPath: path, pageID: pageID), Data("theirs".utf8))
    }

    // MARK: - ETags vouch only for files on disk

    func testAPageWhoseFilesWentAwayIsFetchedAgainRatherThanAnswered304() async throws {
        seedRemotePage(.init(id: pageID, revision: 3, drawing: Data("ink".utf8)))
        let engine = try makeEngine()
        await engine.pull()
        XCTAssertNotNil(store.syncValue(drawingETagKey))

        // The page's directory goes; its ETag row stays behind.
        try FileManager.default.removeItem(at: store.paths.pageDirectory(path, pageID))
        MockPaperServer.state.epoch = "epoch-2"
        await engine.pull()

        XCTAssertEqual(store.drawingData(notebookPath: path, pageID: pageID), Data("ink".utf8))
        XCTAssertEqual(store.localRevision(notebookPath: path, pageID: pageID), 3)
        XCTAssertEqual(MockPaperServer.state.notModifiedPaths, [])
    }

    func testARemoteDeleteForgetsThePageETagsAndARestoreComesBackWhole() async throws {
        seedRemotePage(.init(id: pageID, revision: 3, drawing: Data("ink".utf8)))
        let engine = try makeEngine()
        await engine.pull()
        let saved = try XCTUnwrap(MockPaperServer.state.withState { $0[path]?.pages[pageID] })

        MockPaperServer.state.recordDeletion(path: "\(path)/pages/\(pageID)", kind: "page")
        await engine.pull()
        XCTAssertFalse(exists(store.paths.pageJSON(path, pageID)))
        XCTAssertNil(store.syncValue(drawingETagKey))

        // Restored from the server's trash: same id, same revision, same ETag.
        MockPaperServer.state.withState { notebooks in
            notebooks[path]?.pages[pageID] = saved
            notebooks[path]?.order.append(pageID)
        }
        MockPaperServer.state.epoch = "epoch-2"
        await engine.pull()

        XCTAssertEqual(store.drawingData(notebookPath: path, pageID: pageID), Data("ink".utf8))
        XCTAssertEqual(store.localRevision(notebookPath: path, pageID: pageID), 3)
    }

    // MARK: - The cursor

    func testAFailedManifestKeepsTheCursorAndTheNextPullAppliesIt() async throws {
        seedRemotePage(.init(id: pageID, revision: 1, drawing: Data("ink".utf8)))
        let engine = try makeEngine()
        await engine.pull()
        let before = try XCTUnwrap(cursor)

        MockPaperServer.state.updateNotebook(path: path) { $0.title = "Renamed" }
        failRequests(containing: "/api/paper/notebooks/", times: 1)
        await engine.pull()
        XCTAssertEqual(try store.notebook(path: path)?.title, "Shared")
        XCTAssertEqual(cursor, before)

        await engine.pull()
        XCTAssertEqual(try store.notebook(path: path)?.title, "Renamed")
        XCTAssertNotEqual(cursor, before)
    }

    func testAFailedLibraryReadWritesNoCursor() async throws {
        seedRemotePage(.init(id: pageID, revision: 1, drawing: Data("ink".utf8)))
        let engine = try makeEngine()
        MockPaperServer.state.failPathEquals = "/api/paper"
        MockPaperServer.state.failuresRemaining = 1

        await engine.pull()

        XCTAssertNil(cursor, "an empty library must not come with an up-to-date cursor")
        XCTAssertNil(store.syncValue("changes.epoch"))
        XCTAssertFalse(exists(store.paths.notebookJSON(path)))
        let failures = await engine.failureCount
        XCTAssertEqual(failures, 1)

        // Still a full feed next time, and this time it lands.
        await engine.pull()
        XCTAssertTrue(exists(store.paths.notebookJSON(path)))
        XCTAssertNotNil(cursor)
        XCTAssertEqual(store.syncValue("changes.epoch"), "epoch-1")
    }

    func testAFailedRebuildKeepsTheCursorItWasReplacing() async throws {
        seedRemotePage(.init(id: pageID, revision: 1, drawing: Data("ink".utf8)))
        let engine = try makeEngine()
        await engine.pull()
        let before = try XCTUnwrap(cursor)

        MockPaperServer.state.epoch = "epoch-2"
        MockPaperServer.state.failPathEquals = "/api/paper"
        MockPaperServer.state.failuresRemaining = 1
        await engine.pull()
        XCTAssertEqual(cursor, before, "not cleared before the rebuild succeeded")
        XCTAssertEqual(store.syncValue("changes.epoch"), "epoch-1")

        await engine.pull()
        XCTAssertEqual(store.syncValue("changes.epoch"), "epoch-2")
    }

    /// One failed pull used to leave `failureCount` above zero with nothing
    /// queued to reset it, and a cycle only pulled at zero: pulling stopped
    /// until something was drawn.
    func testAFailedPullIsRetriedByTheNextCycleWithNothingQueued() async throws {
        seedRemotePage(.init(id: pageID, revision: 1, drawing: Data("ink".utf8)))
        let engine = try makeEngine()
        failRequests(containing: "/api/paper/changes", times: 1)
        await engine.syncNow()
        let failed = await engine.failureCount
        XCTAssertEqual(failed, 1)

        await engine.syncNow()

        XCTAssertEqual(MockPaperServer.state.paths(matching: "/api/paper/changes").count, 2)
        XCTAssertTrue(exists(store.paths.pageJSON(path, pageID)))
        let failures = await engine.failureCount
        XCTAssertEqual(failures, 0)
    }

    /// Passed over while it had unpushed work, a notebook's change is behind
    /// the cursor; it is asked for by name once the queue has drained.
    func testANotebookPassedOverForUnpushedWorkIsReconciledOnceTheQueueDrains() async throws {
        let notebook = try store.createNotebook(title: "Ideas", folder: "Work")
        let localPage = try XCTUnwrap(notebook.pages.first)
        try store.writeDrawing(Data("stroke".utf8), notebookPath: notebook.path, pageID: localPage)
        try store.journal.append(
            .init(kind: .pagePut, notebookPath: notebook.path, pageID: localPage))
        let engine = try makeEngine()
        await engine.pushPending()
        await engine.pull()

        MockPaperServer.state.updateNotebook(path: notebook.path) { $0.title = "Ideas, revised" }
        try store.writeDrawing(Data("more".utf8), notebookPath: notebook.path, pageID: localPage)
        try store.journal.append(
            .init(kind: .pagePut, notebookPath: notebook.path, pageID: localPage))
        await engine.pull()
        XCTAssertEqual(try store.notebook(path: notebook.path)?.title, "Ideas")

        await engine.pushPending()
        await engine.pull()
        XCTAssertEqual(try store.notebook(path: notebook.path)?.title, "Ideas, revised")
    }

    // MARK: - Transcripts

    func testANewerTranscriptArrivesThoughTheInkDidNotChange() async throws {
        seedRemotePage(
            .init(
                id: pageID, revision: 3, textRevision: 2, drawing: Data("ink".utf8),
                text: try transcript("first reading")))
        let engine = try makeEngine()
        await engine.pull()
        XCTAssertEqual(store.localTextRevision(notebookPath: path, pageID: pageID), 2)
        // As if that reading were an hour old: the index passes over a file
        // whose modification date it has already recorded.
        try FileManager.default.setAttributes(
            [.modificationDate: Date(timeIntervalSinceNow: -3600)],
            ofItemAtPath: store.paths.text(path, pageID).path(percentEncoded: false))
        store.indexPageText(pageID: pageID, notebookPath: path, textRevision: 2)

        let reading = try transcript("quarterly review")
        MockPaperServer.state.updatePage(notebook: path, id: pageID) {
            $0.textRevision = 3
            $0.text = reading
        }
        await engine.pull()

        XCTAssertEqual(
            store.pageText(notebookPath: path, pageID: pageID)?.transcript, "quarterly review")
        XCTAssertEqual(store.localTextRevision(notebookPath: path, pageID: pageID), 3)
        XCTAssertEqual(store.localRevision(notebookPath: path, pageID: pageID), 3)
        XCTAssertEqual(store.textIndex.search("quarterly").first?.pages.first?.pageID, pageID)
        XCTAssertTrue(store.textIndex.search("first reading").isEmpty)
        XCTAssertTrue(
            MockPaperServer.state.notModifiedPaths.contains { $0.hasSuffix("drawing.pkdrawing") },
            "the unchanged ink cost a conditional request and no bytes")
    }

    /// `set_transcript_ai` stamps `text_revision = revision`: on a page that
    /// had already been read, only `seq` moves.
    func testTheServersAIReadingArrivesAtTheSameTextRevision() async throws {
        seedRemotePage(
            .init(
                id: pageID, revision: 3, textRevision: 3, drawing: Data("ink".utf8),
                text: try transcript("quarterly review")))
        let engine = try makeEngine()
        await engine.pull()

        let withAI = try transcript(
            "quarterly review", ai: "Quarterly review: ship it", revision: 3)
        MockPaperServer.state.updatePage(notebook: path, id: pageID) { $0.text = withAI }
        await engine.pull()

        let text = try XCTUnwrap(store.pageText(notebookPath: path, pageID: pageID))
        XCTAssertEqual(text.transcriptAI?.text, "Quarterly review: ship it")
        XCTAssertEqual(store.localTextRevision(notebookPath: path, pageID: pageID), 3)

        // Taken once; the next pull has nothing new.
        let fetched = MockPaperServer.state.paths(matching: "text.json").count
        await engine.pull()
        XCTAssertEqual(MockPaperServer.state.paths(matching: "text.json").count, fetched)
    }
}
