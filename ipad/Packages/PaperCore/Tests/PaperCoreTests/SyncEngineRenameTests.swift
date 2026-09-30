import Foundation
import XCTest

@testable import PaperCore

/// A rename, a move or an undone delete never costs the queue its work: ops
/// find their files by notebook id, a notebook the server has never seen is
/// created at its new name, and the undo toast queues again what the delete
/// dropped.
final class SyncEngineRenameTests: XCTestCase {
    private var root: URL!
    private var store: PaperStore!
    private let clientID = "ipad-test-client"

    override func setUpWithError() throws {
        try super.setUpWithError()
        MockPaperServer.state.reset()
        root = FileManager.default.temporaryDirectory
            .appending(path: "SyncEngineRenameTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        store = try PaperStore(root: root)
    }

    override func tearDownWithError() throws {
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
            store: store, api: api, clientID: clientID, clock: TestClock(), jitter: { $0 })
    }

    /// A notebook with ink on its pages, as autosave leaves it: files on disk,
    /// ops in the journal, nothing pushed.
    @discardableResult
    private func seedLocalNotebook(pageCount: Int = 1) throws -> Notebook {
        let notebook = try store.createNotebook(title: "Ideas", folder: "Work", pageCount: pageCount)
        for pageID in notebook.pages { try write("stroke", notebook: notebook, pageID: pageID) }
        return notebook
    }

    private func write(_ ink: String, notebook: Notebook, pageID: String) throws {
        try store.writeDrawing(Data(ink.utf8), notebookPath: notebook.path, pageID: pageID)
        try store.journal.append(
            .init(kind: .pagePut, notebookPath: notebook.path, pageID: pageID))
    }

    private func server(_ path: String) -> MockServerState.Notebook? {
        MockPaperServer.state.withState { $0[path] }
    }

    private func serverPaths() -> [String] {
        MockPaperServer.state.withState { $0.keys.sorted() }
    }

    private func assertDrained(file: StaticString = #filePath, line: UInt = #line) throws {
        XCTAssertEqual(try store.journal.pendingCount(), 0, file: file, line: line)
        XCTAssertEqual(try store.journal.parked(), [], file: file, line: line)
    }

    // MARK: - Ops name their notebook

    func testOpsNameTheirNotebookByIDWhenTheProducerDidNot() throws {
        let notebook = try seedLocalNotebook()

        let queued = try store.journal.pending()
        XCTAssertEqual(queued.map(\.kind), [.notebookCreate, .pageCreate, .pagePut])
        XCTAssertEqual(queued.map(\.notebookID), Array(repeating: notebook.id, count: 3))
    }

    // MARK: - Rename

    /// (A) Created offline, written on, renamed: the create itself goes to the
    /// new path, so there is nothing left for a rename to find.
    func testANotebookRenamedBeforeItsCreateWentUpArrivesUnderTheNewName() async throws {
        let notebook = try seedLocalNotebook()
        let pageID = try XCTUnwrap(notebook.pages.first)
        let renamed = try store.rename(notebookID: notebook.id, to: "Plans")
        XCTAssertEqual(renamed.path, "paper/Work/Plans.paper")

        let queued = try store.journal.pending()
        XCTAssertEqual(queued.map(\.kind), [.notebookCreate, .pageCreate, .pagePut])
        XCTAssertEqual(queued.map(\.notebookPath), Array(repeating: renamed.path, count: 3))

        _ = try await makeEngine().pushPending()

        XCTAssertEqual(serverPaths(), [renamed.path])
        XCTAssertEqual(server(renamed.path)?.id, notebook.id)
        XCTAssertEqual(server(renamed.path)?.title, "Plans")
        XCTAssertEqual(server(renamed.path)?.pages[pageID]?.drawing, Data("stroke".utf8))
        XCTAssertEqual(store.localRevision(notebookPath: renamed.path, pageID: pageID), 2)
        try assertDrained()
    }

    /// The same for a move to another folder.
    func testANotebookMovedBeforeItsCreateWentUpArrivesInTheNewFolder() async throws {
        let notebook = try seedLocalNotebook()
        let pageID = try XCTUnwrap(notebook.pages.first)
        let moved = try store.move(notebookID: notebook.id, toFolder: "Archive")
        XCTAssertEqual(moved.path, "paper/Archive/Ideas.paper")

        _ = try await makeEngine().pushPending()

        XCTAssertEqual(serverPaths(), [moved.path])
        XCTAssertEqual(server(moved.path)?.pages[pageID]?.drawing, Data("stroke".utf8))
        try assertDrained()
    }

    /// (B) Already upstream, ink flushed, renamed before the queue drained:
    /// the put reads the files where they are now and lands at the old path,
    /// which the rename behind it then moves.
    func testInkFlushedJustBeforeARenameReachesTheRenamedNotebook() async throws {
        let notebook = try seedLocalNotebook()
        let pageID = try XCTUnwrap(notebook.pages.first)
        let engine = try makeEngine()
        await engine.pushPending()

        try write("important stroke", notebook: notebook, pageID: pageID)
        let renamed = try store.rename(notebookID: notebook.id, to: "Plans")
        let queued = try store.journal.pending()
        XCTAssertEqual(queued.map(\.kind), [.pagePut, .notebookRename])
        XCTAssertEqual(queued.map(\.notebookPath), [notebook.path, notebook.path])
        XCTAssertEqual(queued.last?.notebookID, notebook.id)

        await engine.pushPending()

        XCTAssertEqual(serverPaths(), [renamed.path])
        let page = server(renamed.path)?.pages[pageID]
        XCTAssertEqual(page?.drawing, Data("important stroke".utf8))
        XCTAssertEqual(page?.revision, 3)
        XCTAssertEqual(store.localRevision(notebookPath: renamed.path, pageID: pageID), 3)
        try assertDrained()
    }

    /// The same, and then a new notebook takes the path the first one left
    /// and is renamed, or deleted, before anything drains: the first one's
    /// work still names that path and reaches it all the same.
    func testANewNotebookAtAPathARenameFreedLeavesTheQueuedWorkThereAlone() async throws {
        for deleteIt in [false, true] {
            MockPaperServer.state.reset()
            let notebook = try store.createNotebook(
                title: deleteIt ? "Drafts" : "Ideas", folder: "Work")
            let pageID = try XCTUnwrap(notebook.pages.first)
            let engine = try makeEngine()
            await engine.pushPending()

            try write("important stroke", notebook: notebook, pageID: pageID)
            let renamed = try store.rename(
                notebookID: notebook.id, to: deleteIt ? "Final" : "Plans")
            let newcomer = try store.createNotebook(title: notebook.title, folder: "Work")
            XCTAssertEqual(newcomer.path, notebook.path)
            if deleteIt {
                _ = try store.delete(notebookID: newcomer.id)
            } else {
                _ = try store.rename(notebookID: newcomer.id, to: "Other")
            }

            await engine.pushPending()

            XCTAssertEqual(
                server(renamed.path)?.pages[pageID]?.drawing, Data("important stroke".utf8))
            XCTAssertNil(server(notebook.path))
            try assertDrained()
        }
    }

    /// A notebook's queued patch survives a new notebook taking its old path
    /// and patching its own page order there, and a favorite survives the
    /// reorder queued after it.
    func testQueuedPatchesReachTheServerPastANewNotebookAtTheFreedPath() async throws {
        let notebook = try store.createNotebook(title: "Ideas", folder: "Work")
        let engine = try makeEngine()
        await engine.pushPending()

        _ = try store.setTags(notebookID: notebook.id, ["urgent"])
        let renamed = try store.rename(notebookID: notebook.id, to: "Plans")
        let newcomer = try store.createNotebook(title: notebook.title, folder: "Work")
        XCTAssertEqual(newcomer.path, notebook.path)
        _ = try store.setFavorite(notebookID: newcomer.id, true)
        let page = try store.createPage(notebookID: newcomer.id)
        try store.reorderPages(
            notebookID: newcomer.id, order: [page.id] + newcomer.pages)

        await engine.pushPending()

        XCTAssertEqual(server(renamed.path)?.tags, ["urgent"])
        XCTAssertEqual(server(newcomer.path)?.favorite, true)
        XCTAssertEqual(server(newcomer.path)?.tags, [])
        try assertDrained()
    }

    /// A rename whose old path is gone and whose new one is there already
    /// landed; a rename of something that is nowhere upstream is parked.
    func testARenameThatFindsNothingIsDoneOnlyWhenItAlreadyLanded() async throws {
        MockPaperServer.state.seed(
            notebook: "paper/Plans.paper", id: PaperID.make(), title: "Plans", pages: [])
        try store.journal.append(
            .init(
                kind: .notebookRename, notebookPath: "paper/Ideas.paper",
                payload: ["new_path": "paper/Plans.paper"]))
        try store.journal.append(
            .init(
                kind: .notebookRename, notebookPath: "paper/Nowhere.paper",
                payload: ["new_path": "paper/Elsewhere.paper"]))

        _ = try await makeEngine().pushPending()

        XCTAssertEqual(try store.journal.pendingCount(), 0)
        let parked = try store.journal.parked()
        XCTAssertEqual(parked.map(\.notebookPath), ["paper/Nowhere.paper"])
        XCTAssertTrue(parked.first?.parkedReason?.hasPrefix("404") ?? false)
    }

    /// The server re-stamps every page a rename moves, so other devices find
    /// them at the new path. The device that renamed already has them there:
    /// the echo on its next pull costs it no page or blob request and no
    /// reload -- except for a page that had moved on upstream before the move.
    func testAPushedRenameCostsTheRenamingDeviceNoDownloads() async throws {
        let notebook = try seedLocalNotebook(pageCount: 3)
        let engine = try makeEngine()
        let changes = NotebookChangeRecorder()
        await engine.setHandlers(onNotebooksChanged: { changes.record($0) })
        await engine.pushPending()
        await engine.pull()  // full: establishes the cursor

        // Somebody else's change this device has not pulled yet.
        let touched = notebook.pages[1]
        MockPaperServer.state.updatePage(notebook: notebook.path, id: touched) {
            $0.text = Data(#"{"lines":[],"transcript":"theirs"}"#.utf8)
        }
        let renamed = try store.rename(notebookID: notebook.id, to: "Plans")
        await engine.pushPending()
        try assertDrained()
        let before = MockPaperServer.state.requests.count
        let announcedBefore = changes.calls.count

        await engine.pull()

        let fetched = MockPaperServer.state.requests.dropFirst(before).map(\.path).filter {
            $0.hasPrefix("/api/paper/pages/") || $0.hasPrefix("/api/paper/blob/")
        }
        XCTAssertEqual(
            fetched.filter { $0.hasPrefix("/api/paper/pages/") },
            ["/api/paper/pages/\(renamed.path)/\(touched)"])
        XCTAssertTrue(fetched.allSatisfy { $0.contains("/\(touched)") }, "\(fetched)")
        for pageID in notebook.pages {
            XCTAssertEqual(
                try store.page(notebookPath: renamed.path, id: pageID).seq,
                server(renamed.path)?.pages[pageID]?.seq)
        }
        let announced = changes.calls.dropFirst(announcedBefore)
        XCTAssertEqual(announced.map { $0 }, [[renamed.path]], "only the page that moved on")

        // With nothing new upstream, a second rename's echo is silent.
        let again = try store.rename(notebookID: notebook.id, to: "Later")
        await engine.pushPending()
        let quiet = MockPaperServer.state.requests.count
        let quietAnnounced = changes.calls.count
        await engine.pull()
        XCTAssertEqual(
            MockPaperServer.state.requests.dropFirst(quiet).map(\.path).filter {
                $0.hasPrefix("/api/paper/pages/") || $0.hasPrefix("/api/paper/blob/")
            }, [])
        XCTAssertEqual(changes.calls.count, quietAnnounced)
        XCTAssertEqual(serverPaths(), [again.path])
    }

    // MARK: - Missing pages

    func testAPutForAPageThatIsNotOnThisDeviceIsParkedNotCompleted() async throws {
        let notebook = try seedLocalNotebook()
        let pageID = try XCTUnwrap(notebook.pages.first)
        let engine = try makeEngine()
        await engine.pushPending()

        try write("more", notebook: notebook, pageID: pageID)
        try FileManager.default.removeItem(at: store.paths.pageDirectory(notebook.path, pageID))
        await engine.pushPending()

        XCTAssertEqual(try store.journal.pendingCount(), 0)
        let parked = try store.journal.parked()
        XCTAssertEqual(parked.map(\.kind), [.pagePut])
        XCTAssertEqual(parked.first?.parkedReason, "page missing on this device")
        let state = await engine.state
        XCTAssertEqual(state, .attention(message: "a page was not sent — page missing on this device"))
    }

    /// Deleting the page after the rename matches the delete by the new path,
    /// so the put queued under the old one outlives it. The page is gone
    /// because the user deleted it: the put is moot, not lost.
    func testAPutMadeMootByADeleteAfterARenameCompletesQuietly() async throws {
        let notebook = try seedLocalNotebook(pageCount: 2)
        let doomed = notebook.pages[1]
        let engine = try makeEngine()
        await engine.pushPending()

        try write("scribble", notebook: notebook, pageID: doomed)
        let renamed = try store.rename(notebookID: notebook.id, to: "Plans")
        _ = try store.deletePage(notebookID: notebook.id, pageID: doomed)
        await engine.pushPending()

        try assertDrained()
        XCTAssertEqual(server(renamed.path)?.order, [notebook.pages[0]])
        XCTAssertNil(server(renamed.path)?.pages[doomed])
    }

    // MARK: - Undo

    /// (C) Created offline, deleted, undone: the delete never left the
    /// device, so what it dropped from the queue is queued again.
    func testUndoingTheDeleteOfANotebookThatNeverWentUpQueuesItAgain() async throws {
        let notebook = try seedLocalNotebook()
        let pageID = try XCTUnwrap(notebook.pages.first)

        let token = try store.delete(notebookID: notebook.id)
        XCTAssertEqual(try store.journal.pending().map(\.kind), [.notebookDelete])
        _ = try store.restore(token)
        XCTAssertEqual(
            try store.journal.pending().map(\.kind), [.notebookCreate, .pageCreate, .pagePut])

        _ = try await makeEngine().pushPending()

        XCTAssertEqual(server(notebook.path)?.pages[pageID]?.drawing, Data("stroke".utf8))
        try assertDrained()
    }

    /// The same for one page: its unpushed ink comes back with it.
    func testUndoingAPageDeleteBeforeItWentUpQueuesItsInkAgain() async throws {
        let notebook = try seedLocalNotebook(pageCount: 2)
        let pageID = notebook.pages[1]
        let engine = try makeEngine()
        await engine.pushPending()

        try write("late ink", notebook: notebook, pageID: pageID)
        let token = try store.deletePage(notebookID: notebook.id, pageID: pageID)
        _ = try store.restore(token)
        XCTAssertTrue(try store.journal.pending().contains { $0.kind == .pagePut && $0.pageID == pageID })

        await engine.pushPending()

        XCTAssertEqual(server(notebook.path)?.pages[pageID]?.drawing, Data("late ink".utf8))
        XCTAssertEqual(server(notebook.path)?.order, notebook.pages)
        try assertDrained()
    }

    /// (D) Upstream, deleted, the delete pushed, then undone: the server has
    /// it in its trash, so the restored files go up as new -- the create
    /// again, every page as a PUT at base 0, and the metadata the create does
    /// not carry.
    func testUndoingADeleteThatReachedTheServerSendsTheNotebookUpAsNew() async throws {
        let notebook = try seedLocalNotebook(pageCount: 2)
        let engine = try makeEngine()
        _ = try store.setFavorite(notebookID: notebook.id, true)
        await engine.pushPending()

        let token = try store.delete(notebookID: notebook.id)
        await engine.pushPending()
        XCTAssertEqual(serverPaths(), [])

        let restored = try XCTUnwrap(try store.restore(token))
        for pageID in notebook.pages {
            XCTAssertEqual(store.localRevision(notebookPath: restored.path, pageID: pageID), 0)
        }
        let queued = try store.journal.pending()
        XCTAssertEqual(queued.map(\.kind), [.notebookCreate, .pagePut, .pagePut, .notebookPatch])
        XCTAssertEqual(queued.map(\.createsPage), [false, true, true, false])

        await engine.pushPending()

        let stored = try XCTUnwrap(server(notebook.path))
        XCTAssertEqual(stored.id, notebook.id)
        XCTAssertTrue(stored.favorite)
        XCTAssertEqual(stored.order, notebook.pages)
        for pageID in notebook.pages {
            XCTAssertEqual(stored.pages[pageID]?.drawing, Data("stroke".utf8))
            XCTAssertEqual(stored.pages[pageID]?.lastPutBaseRevision, 0)
            XCTAssertEqual(store.localRevision(notebookPath: restored.path, pageID: pageID), 1)
        }
        try assertDrained()
    }

    /// The same for one page whose delete reached the server.
    func testUndoingAPageDeleteThatReachedTheServerSendsThePageUpAsNew() async throws {
        let notebook = try seedLocalNotebook(pageCount: 3)
        let pageID = notebook.pages[1]
        let engine = try makeEngine()
        await engine.pushPending()

        let token = try store.deletePage(notebookID: notebook.id, pageID: pageID)
        await engine.pushPending()
        XCTAssertNil(server(notebook.path)?.pages[pageID])

        _ = try store.restore(token)
        XCTAssertEqual(store.localRevision(notebookPath: notebook.path, pageID: pageID), 0)
        let create = try XCTUnwrap(try store.journal.pending().first { $0.pageID == pageID })
        XCTAssertTrue(create.createsPage)
        XCTAssertEqual(create.payload["after"], notebook.pages[0])

        await engine.pushPending()

        let stored = try XCTUnwrap(server(notebook.path))
        XCTAssertEqual(stored.pages[pageID]?.drawing, Data("stroke".utf8))
        XCTAssertEqual(stored.order, notebook.pages)
        try assertDrained()
    }
}
