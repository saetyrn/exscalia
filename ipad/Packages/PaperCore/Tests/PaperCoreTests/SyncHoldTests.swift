import Foundation
import PencilKit
import XCTest

@testable import PaperCore

/// What a pull may write, and when: never over a page with work queued here,
/// and nothing at all while the engine is held for a conversion.
final class SyncHoldTests: XCTestCase {
    private var root: URL!
    private var store: PaperStore!

    override func setUpWithError() throws {
        try super.setUpWithError()
        MockPaperServer.state.reset()
        root = FileManager.default.temporaryDirectory
            .appending(path: "SyncHoldTests-\(UUID().uuidString)", directoryHint: .isDirectory)
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

    private func makeEngine() throws -> SyncEngine {
        let settings = try XCTUnwrap(ServerSettings(urlString: "https://paper.test"))
        let api = PaperAPI(
            settings: settings, auth: AnonymousAuthProvider(), session: MockPaperServer.makeSession())
        return SyncEngine(store: store, api: api, clientID: "ipad", clock: TestClock(), jitter: { $0 })
    }

    private func ink(_ y: CGFloat) -> Data {
        InkFixtures.data([
            InkFixtures.stroke(from: CGPoint(x: 100, y: y), to: CGPoint(x: 400, y: y + 40), width: 4)
        ])
    }

    private func write(_ data: Data, _ notebook: Notebook, _ pageID: String) throws {
        try store.writeDrawing(data, notebookPath: notebook.path, pageID: pageID)
        try store.journal.append(.init(kind: .pagePut, notebookPath: notebook.path, pageID: pageID))
    }

    /// A notebook of one page with ink, pushed and pulled, so its blob ETags
    /// are stored, and the server's copy then read again by its own AI: a
    /// text-only change that makes the next pull fetch the page.
    private func syncedNotebookReadUpstream(_ engine: SyncEngine) async throws -> Notebook {
        let notebook = try store.createNotebook(title: "Lecture", template: .ruled, size: .a4, pageCount: 1)
        try write(ink(200), notebook, notebook.pages[0])
        await engine.syncNow()
        // A second device writes nothing; the server's reader files a
        // transcript at the same revision.
        MockPaperServer.state.updatePage(notebook: notebook.path, id: notebook.pages[0]) {
            $0.text = Data(#"{"lines":[],"transcript":"read upstream"}"#.utf8)
            $0.textRevision = $0.revision
        }
        await engine.syncNow()
        XCTAssertEqual(try store.journal.pendingCount(), 0)
        MockPaperServer.state.updatePage(notebook: notebook.path, id: notebook.pages[0]) {
            $0.text = Data(#"{"lines":[],"transcript":"read again"}"#.utf8)
        }
        return notebook
    }

    /// (B3) A pull that fetched the old sheet before the conversion began
    /// lands nothing once the engine is held: the board stays a board with
    /// its own ink, and the queue then sends it.
    func testAPullOutWhenAConversionStartsLandsNothingOverTheBoard() async throws {
        let engine = try makeEngine()
        let notebook = try await syncedNotebookReadUpstream(engine)
        let pageID = notebook.pages[0]
        XCTAssertNotNil(store.syncValue("etag.\(notebook.path)/pages/\(pageID).drawing.pkdrawing"))

        MockPaperServer.state.holdNext(pathContaining: "/api/paper/blob/")
        let pulling = Task { await engine.pull() }
        try await MockPaperServer.state.waitUntilHeld()
        await engine.hold()
        try store.convertToWhiteboard(notebookID: notebook.id)
        let board = store.drawingData(notebookPath: notebook.path, pageID: pageID)
        XCTAssertNil(
            store.syncValue("etag.\(notebook.path)/pages/\(pageID).drawing.pkdrawing"),
            "the board's files are not the ones the ETag vouched for")
        MockPaperServer.state.releaseHeld()
        await pulling.value

        let page = try store.page(notebookPath: notebook.path, id: pageID)
        XCTAssertEqual(page.size, .board, "the server's sheet was written over the board")
        XCTAssertEqual(store.drawingData(notebookPath: notebook.path, pageID: pageID), board)

        let requests = MockPaperServer.state.requests.count
        await engine.syncNow()
        XCTAssertEqual(MockPaperServer.state.requests.count, requests, "held: nothing sent")
        await engine.release()
        await engine.syncNow()
        XCTAssertEqual(try store.journal.pendingCount(), 0)
        XCTAssertEqual(
            MockPaperServer.state.withState { $0[notebook.path]?.pages[pageID]?.sizeName }, "board")
        XCTAssertEqual(try store.page(notebookPath: notebook.path, id: pageID).size, .board)
    }

    /// (B3) The same for the notebook's manifest: one fetched before the
    /// conversion, still saying "notebook", does not turn the whiteboard
    /// back once the engine is held.
    func testAManifestOutWhenAConversionStartsDoesNotTurnTheWhiteboardBack() async throws {
        let engine = try makeEngine()
        let notebook = try store.createNotebook(title: "Lecture", template: .ruled, size: .a4, pageCount: 1)
        try write(ink(200), notebook, notebook.pages[0])
        await engine.syncNow()
        MockPaperServer.state.updateNotebook(path: notebook.path) { $0.favorite = true }

        MockPaperServer.state.holdNext(pathContaining: "/api/paper/notebooks/\(notebook.path)")
        let pulling = Task { await engine.pull() }
        try await MockPaperServer.state.waitUntilHeld()
        await engine.hold()
        try store.convertToWhiteboard(notebookID: notebook.id)
        MockPaperServer.state.releaseHeld()
        await pulling.value

        XCTAssertEqual(try store.notebook(id: notebook.id).kind, .whiteboard)
        XCTAssertEqual(
            try AtomicFile.readJSON(Notebook.self, at: store.paths.notebookJSON(notebook.path)).kind,
            .whiteboard, "the manifest fetched before the conversion was written over it")
        await engine.release()
        await engine.syncNow()
        XCTAssertEqual(try store.journal.pendingCount(), 0)
        XCTAssertEqual(MockPaperServer.state.withState { $0[notebook.path]?.kind }, "whiteboard")
    }

    /// (B3, in general) Ink autosave writes while a pull is out is not
    /// overwritten when the pull lands: the page is left alone, and the push
    /// then meets the server's version as a conflict, both kept.
    func testInkWrittenWhileAPullWasOutIsNotOverwritten() async throws {
        let engine = try makeEngine()
        let notebook = try store.createNotebook(title: "Shared", template: .blank, size: .a4, pageCount: 1)
        let pageID = notebook.pages[0]
        try write(ink(100), notebook, pageID)
        await engine.syncNow()
        // Another device's version.
        let theirs = ink(500)
        MockPaperServer.state.updatePage(notebook: notebook.path, id: pageID) {
            $0.drawing = theirs
            $0.revision += 1
        }

        MockPaperServer.state.holdNext(pathContaining: "/api/paper/blob/")
        let pulling = Task { await engine.pull() }
        try await MockPaperServer.state.waitUntilHeld()
        let mine = ink(800)
        try write(mine, notebook, pageID)
        MockPaperServer.state.releaseHeld()
        await pulling.value

        XCTAssertEqual(store.drawingData(notebookPath: notebook.path, pageID: pageID), mine)
        await engine.syncNow()
        XCTAssertEqual(try store.journal.pendingCount(), 0)
        XCTAssertEqual(try store.journal.parked(), [])
        let pages = try store.notebook(id: notebook.id).pages
        let sibling = try XCTUnwrap(pages.first { $0.hasPrefix("\(pageID).conflict-") })
        XCTAssertEqual(store.drawingData(notebookPath: notebook.path, pageID: pageID), theirs)
        XCTAssertEqual(store.drawingData(notebookPath: notebook.path, pageID: sibling), mine)
        XCTAssertEqual(MockPaperServer.state.withState { $0[notebook.path]?.pages[sibling]?.drawing }, mine)
    }

    /// Ink autosave writes while its page's push is out, when that push
    /// comes back a conflict, was written into the directory that becomes
    /// the sibling: it goes up as the sibling's, and the original comes down.
    func testInkWrittenWhileAConflictingPushWasOutGoesUpAsTheSibling() async throws {
        let engine = try makeEngine()
        let notebook = try store.createNotebook(title: "Shared", template: .blank, size: .a4, pageCount: 1)
        let pageID = notebook.pages[0]
        try write(ink(100), notebook, pageID)
        await engine.syncNow()
        let theirs = ink(500)
        MockPaperServer.state.updatePage(notebook: notebook.path, id: pageID) {
            $0.drawing = theirs
            $0.revision += 1
        }
        try write(ink(200), notebook, pageID)

        MockPaperServer.state.holdNext(pathContaining: "/api/paper/pages/\(notebook.path)/\(pageID)")
        let pushing = Task { await engine.pushPending() }
        try await MockPaperServer.state.waitUntilHeld()
        let newest = ink(900)
        try write(newest, notebook, pageID)
        MockPaperServer.state.releaseHeld()
        _ = await pushing.value
        await engine.syncNow()

        XCTAssertEqual(try store.journal.pendingCount(), 0)
        XCTAssertEqual(try store.journal.parked(), [])
        let pages = try store.notebook(id: notebook.id).pages
        let sibling = try XCTUnwrap(pages.first { $0.hasPrefix("\(pageID).conflict-") })
        XCTAssertEqual(store.drawingData(notebookPath: notebook.path, pageID: pageID), theirs)
        XCTAssertEqual(store.drawingData(notebookPath: notebook.path, pageID: sibling), newest)
        XCTAssertEqual(
            MockPaperServer.state.withState { $0[notebook.path]?.pages[sibling]?.drawing }, newest,
            "the newest ink reached the server")
    }

    /// (Round 2, item 6) The page is deleted while its save is out, and the
    /// save comes back a conflict. A delete is not checked against revisions
    /// -- sent a moment earlier it would have taken the server's page,
    /// whatever was on it, into the server's Trash -- so it takes both: the
    /// original, and the sibling the server made of the save the owner then
    /// deleted. Both stay in the server's Trash. The original used to come
    /// back down as a page the owner had deleted, and the delete went to the
    /// sibling instead.
    func testAPageDeletedWhileItsConflictingSaveWasOutIsDeletedWithItsSibling() async throws {
        let engine = try makeEngine()
        let notebook = try store.createNotebook(title: "Shared", template: .blank, size: .a4, pageCount: 2)
        let pageID = notebook.pages[0]
        try write(ink(100), notebook, pageID)
        await engine.syncNow()
        MockPaperServer.state.updatePage(notebook: notebook.path, id: pageID) {
            $0.drawing = self.ink(500)
            $0.revision += 1
        }
        try write(ink(200), notebook, pageID)

        MockPaperServer.state.holdNext(pathContaining: "/api/paper/pages/\(notebook.path)/\(pageID)")
        let pushing = Task { await engine.pushPending() }
        try await MockPaperServer.state.waitUntilHeld()
        _ = try store.deletePage(notebookID: notebook.id, pageID: pageID)
        MockPaperServer.state.releaseHeld()
        _ = await pushing.value
        await engine.syncNow()
        await engine.syncNow()

        XCTAssertEqual(try store.journal.pendingCount(), 0)
        XCTAssertEqual(try store.journal.parked(), [])
        XCTAssertEqual(try store.notebook(id: notebook.id).pages, [notebook.pages[1]])
        let upstream = try XCTUnwrap(MockPaperServer.state.withState { $0[notebook.path] })
        XCTAssertEqual(upstream.order, [notebook.pages[1]], "the deleted page, or its sibling, is still upstream")
        XCTAssertEqual(
            MockPaperServer.state.tombstones.filter { $0.path.contains("/pages/\(pageID)") }.count, 2,
            "both went to the server's Trash")
    }

    /// A hold waiting for a push that never gets its answer -- the app sent
    /// to the background, the loop stopped -- returns once that push is cut
    /// short, and the engine syncs as before once released.
    func testAHoldWaitingOnAPushThatIsCutShortReturnsAndIsReleased() async throws {
        let notebook = try store.createNotebook(title: "Away", template: .blank, size: .a4, pageCount: 1)
        try write(ink(100), notebook, notebook.pages[0])
        let settings = try XCTUnwrap(ServerSettings(urlString: "https://paper.test"))
        let api = PaperAPI(
            settings: settings, auth: AnonymousAuthProvider(), session: MockPaperServer.makeSession())
        let engine = SyncEngine(
            store: store, api: api, clientID: "ipad", clock: SystemSyncClock(), jitter: { $0 })
        MockPaperServer.state.holdNext(pathContaining: "/api/paper/notebooks")
        await engine.start()
        try await MockPaperServer.state.waitUntilHeld()

        let holding = Task { await engine.hold() }
        try await Task.sleep(nanoseconds: 100_000_000)
        await engine.stop()
        MockPaperServer.state.releaseHeld()
        await holding.value
        let held = await engine.isHeld
        XCTAssertTrue(held)
        await engine.release()
        let released = await engine.isHeld
        XCTAssertFalse(released)

        await engine.syncNow()
        XCTAssertEqual(try store.journal.pendingCount(), 0)
        XCTAssertNotNil(MockPaperServer.state.withState { $0[notebook.path] })
    }
}
