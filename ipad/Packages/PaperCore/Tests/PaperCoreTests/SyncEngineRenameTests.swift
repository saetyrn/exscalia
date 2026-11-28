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

    // MARK: - A name taken beside it (A)

    /// The server refuses (409) a notebook renamed onto a name a folder or
    /// another notebook beside it already holds, case folded. The device
    /// chooses the name the server would -- "<name> 2" -- so the rename
    /// lands, the queue behind it drains and pulls go on. It was allowed
    /// here, refused upstream, retried for ever at the head of the queue, and
    /// no pull ran again.
    func testANotebookRenamedOntoASiblingFoldersNameTakesTheNameTheServerWould() async throws {
        _ = try store.createFolder(name: "Physics", in: "paper")
        let ideas = try store.createNotebook(title: "Ideas", folder: "", pageCount: 1)
        let other = try store.createNotebook(title: "Other", folder: "", pageCount: 1)
        let engine = try makeEngine()
        await engine.syncNow()
        try assertDrained()

        let renamed = try store.rename(notebookID: ideas.id, to: "Physics")
        XCTAssertEqual(renamed.path, "paper/Physics 2.paper")
        XCTAssertEqual(renamed.title, "Physics")
        try write("other ink", notebook: other, pageID: other.pages[0])
        MockPaperServer.state.seed(
            notebook: "paper/Upstream.paper", id: PaperID.make(), title: "Upstream", pages: [])

        await engine.syncNow()

        try assertDrained()
        XCTAssertNil(server(ideas.path))
        XCTAssertEqual(server("paper/Physics 2.paper")?.id, ideas.id)
        XCTAssertEqual(server("paper/Physics 2.paper")?.title, "Physics")
        XCTAssertEqual(server(other.path)?.pages[other.pages[0]]?.drawing, Data("other ink".utf8))
        XCTAssertNotNil(try store.notebook(path: "paper/Upstream.paper"), "pulls go on")
    }

    /// The same rule for a notebook beside it whose name differs only in
    /// case, and for a move into a folder holding a folder of its name.
    func testANameTakenBesideItIsTakenWhateverItsCaseAndWhateverHoldsIt() throws {
        let plans = try store.createNotebook(title: "plans", folder: "Work")
        let ideas = try store.createNotebook(title: "Ideas", folder: "Work")
        XCTAssertEqual(try store.rename(notebookID: ideas.id, to: "Plans").path, "paper/Work/Plans 2.paper")
        XCTAssertEqual(try store.notebook(id: plans.id).path, "paper/Work/plans.paper")

        _ = try store.createFolder(name: "Physics", in: "paper/Work")
        let physics = try store.createNotebook(title: "Physics", folder: "")
        XCTAssertEqual(
            try store.move(notebookID: physics.id, toFolder: "Work").path,
            "paper/Work/Physics 2.paper")
        // Its own name is its own: a retitle in place keeps the path.
        XCTAssertEqual(
            try store.rename(notebookID: physics.id, to: "Physics").path,
            "paper/Work/Physics 2.paper")
    }

    /// A name taken upstream by something this device has not pulled yet:
    /// the 409 is not retried for ever. The rename takes the first name free
    /// there, the device follows it with its files and the queue behind it,
    /// and pulls go on.
    func testARenameRefusedUpstreamTakesTheFirstFreeNameThereAndTheDeviceFollows() async throws {
        let notebook = try seedLocalNotebook()
        let engine = try makeEngine()
        await engine.syncNow()
        try assertDrained()
        // Another device made a folder "Plans" in Work; this one has not
        // pulled it.
        MockPaperServer.state.seedFolder(path: "paper/Work/Plans")

        let renamed = try store.rename(notebookID: notebook.id, to: "Plans")
        XCTAssertEqual(renamed.path, "paper/Work/Plans.paper")
        try write("after the rename", notebook: renamed, pageID: notebook.pages[0])

        await engine.syncNow()

        try assertDrained()
        let landed = "paper/Work/Plans 2.paper"
        XCTAssertEqual(server(landed)?.id, notebook.id)
        XCTAssertEqual(server(landed)?.title, "Plans")
        XCTAssertEqual(server(landed)?.pages[notebook.pages[0]]?.drawing, Data("after the rename".utf8))
        XCTAssertEqual(try store.notebook(id: notebook.id).path, landed)
        XCTAssertEqual(try store.notebook(id: notebook.id).title, "Plans")
        XCTAssertEqual(
            store.drawingData(notebookPath: landed, pageID: notebook.pages[0]),
            Data("after the rename".utf8))
        XCTAssertNotNil(try store.folder(path: "paper/Work/Plans"), "pulls go on")
    }

    // MARK: - No refusal holds the queue (A)

    /// Every answer that will never succeed on retry -- 400, 404, 409, 413,
    /// 422 -- parks the op it met and the queue goes on behind it; only 429
    /// and 5xx are retried, and nothing is pulled after them.
    func testEveryRefusalParksTheOpAndTheQueueGoesOn() async throws {
        for status in [400, 404, 409, 413, 422] {
            MockPaperServer.state.reset()
            try? FileManager.default.removeItem(at: root)
            store = try PaperStore(root: root)
            let first = try store.createNotebook(title: "First", folder: "", pageCount: 1)
            let second = try store.createNotebook(title: "Second", folder: "", pageCount: 1)
            let engine = try makeEngine()
            await engine.syncNow()
            try assertDrained()

            try write("refused", notebook: first, pageID: first.pages[0])
            try write("sent", notebook: second, pageID: second.pages[0])
            _ = try store.createFolder(name: "Later", in: "paper")
            MockPaperServer.state.failStatus = status
            MockPaperServer.state.failPathsContaining = "/api/paper/pages/\(first.path)"
            MockPaperServer.state.failuresRemaining = 1
            MockPaperServer.state.seed(
                notebook: "paper/Upstream.paper", id: PaperID.make(), title: "Upstream", pages: [])

            await engine.syncNow()

            let parked = try store.journal.parked()
            XCTAssertEqual(parked.map(\.pageID), [first.pages[0]], "\(status)")
            XCTAssertTrue(parked.first?.parkedReason?.hasPrefix("\(status)") ?? false, "\(status)")
            XCTAssertEqual(try store.journal.pendingCount(), 0, "\(status)")
            XCTAssertEqual(server(second.path)?.pages[second.pages[0]]?.drawing, Data("sent".utf8))
            XCTAssertNotNil(MockPaperServer.state.withFolders { $0["paper/Later"] }, "\(status)")
            XCTAssertNotNil(try store.notebook(path: "paper/Upstream.paper"), "pulled after \(status)")
        }
    }

    /// A folder rename answered 409 (something that is not a folder holds
    /// the name upstream) parks rather than holding every op behind it.
    func testAFolderRenameRefusedWith409ParksAndTheQueueGoesOn() async throws {
        let work = try store.createFolder(name: "Work", in: "paper")
        let other = try store.createNotebook(title: "Other", folder: "", pageCount: 1)
        let engine = try makeEngine()
        await engine.syncNow()
        try assertDrained()

        _ = try store.renameFolder(id: work.id, to: "Jobs")
        try write("behind it", notebook: other, pageID: other.pages[0])
        MockPaperServer.state.failStatus = 409
        MockPaperServer.state.failPathsContaining = "/api/paper/folders/paper/Work/rename"
        MockPaperServer.state.failuresRemaining = 1

        await engine.syncNow()

        XCTAssertEqual(try store.journal.parked().map(\.kind), [.folderRename])
        XCTAssertEqual(try store.journal.pendingCount(), 0)
        XCTAssertEqual(server(other.path)?.pages[other.pages[0]]?.drawing, Data("behind it".utf8))
    }

    /// The same 409 rule for every kind of op: a notebook patch, a page
    /// create, a page delete, a notebook delete, an attachment, a folder
    /// create, patch and delete each park on a 409 that is not one their
    /// route resolves, and what is behind them goes.
    func testA409ParksEveryKindOfOpItIsNotResolvedFor() async throws {
        let cases: [(String, (PaperStore, Notebook) throws -> Void)] = [
            ("PATCH /api/paper/notebooks/", { store, notebook in
                _ = try store.setFavorite(notebookID: notebook.id, true)
            }),
            ("DELETE /api/paper/pages/", { store, notebook in
                _ = try store.createPage(notebookID: notebook.id)
                _ = try store.deletePage(notebookID: notebook.id, pageID: notebook.pages[0])
            }),
            ("DELETE /api/paper/notebooks/", { store, notebook in
                _ = try store.delete(notebookID: notebook.id)
            }),
            ("POST /api/paper/assets/", { store, notebook in
                _ = try store.storeAsset(Data("png".utf8), preferredName: "a.png", notebookPath: notebook.path)
            }),
            ("POST /api/paper/folders", { store, _ in
                _ = try store.createFolder(name: "New", in: "paper")
            }),
        ]
        for (route, queue) in cases {
            MockPaperServer.state.reset()
            try? FileManager.default.removeItem(at: root)
            store = try PaperStore(root: root)
            let notebook = try store.createNotebook(title: "First", folder: "", pageCount: 1)
            let behind = try store.createNotebook(title: "Behind", folder: "", pageCount: 1)
            let engine = try makeEngine()
            await engine.syncNow()
            try assertDrained()

            try queue(store, notebook)
            try write("behind", notebook: behind, pageID: behind.pages[0])
            let parts = route.split(separator: " ", maxSplits: 1).map(String.init)
            MockPaperServer.state.failStatus = 409
            MockPaperServer.state.failPathsContaining = parts[1]
            MockPaperServer.state.failuresRemaining = 1

            await engine.pushPending()

            XCTAssertEqual(try store.journal.parked().count, 1, route)
            XCTAssertTrue(try store.journal.parked().first?.parkedReason?.hasPrefix("409") ?? false, route)
            XCTAssertEqual(try store.journal.pendingCount(), 0, route)
            XCTAssertEqual(server(behind.path)?.pages[behind.pages[0]]?.drawing, Data("behind".utf8), route)
        }
    }

    /// (E4, the patch route) A favourite queued for a notebook deleted
    /// upstream meanwhile sends the notebook up again rather than vanishing.
    func testAPatchForANotebookDeletedUpstreamSendsItUpAgain() async throws {
        let notebook = try seedLocalNotebook(pageCount: 2)
        let engine = try makeEngine()
        await engine.syncNow()
        try assertDrained()
        MockPaperServer.state.recordDeletion(path: notebook.path, kind: "notebook")
        _ = try store.setFavorite(notebookID: notebook.id, true)

        await engine.pushPending()

        try assertDrained()
        let back = try XCTUnwrap(server(notebook.path))
        XCTAssertEqual(back.id, notebook.id)
        XCTAssertTrue(back.favorite)
        XCTAssertEqual(back.order, notebook.pages)
        for pageID in notebook.pages {
            XCTAssertEqual(back.pages[pageID]?.drawing, Data("stroke".utf8))
        }
    }

    // MARK: - A create the server refuses (round 2, item 2)

    /// A notebook whose create the server refuses -- a server older than the
    /// build answers a whiteboard's `paperSize: board` with 400 -- parks its
    /// create, and the page ops behind it, which meet 404, park too: it was
    /// never deleted upstream, so it is not sent up again. Sending it again
    /// was refused again, up to `pushBatch` times a cycle, every cycle.
    func testARefusedCreateParksOnceAndIsNotSentUpAgain() async throws {
        let engine = try makeEngine()
        let notebook = try store.createNotebook(title: "Ideas", folder: "", pageCount: 1)
        try write("ink", notebook: notebook, pageID: notebook.pages[0])
        MockPaperServer.state.failPathEquals = "/api/paper/notebooks"
        MockPaperServer.state.failStatus = 400
        MockPaperServer.state.failuresRemaining = 100_000
        func creates() -> Int {
            MockPaperServer.state.requests.filter {
                $0.method == "POST" && $0.path == "/api/paper/notebooks"
            }.count
        }

        await engine.syncNow()
        await engine.syncNow()
        await engine.syncNow()

        XCTAssertEqual(creates(), 1, "the refused create was sent again")
        XCTAssertEqual(try store.journal.pendingCount(), 0, "the queue never settles")
        XCTAssertEqual(
            Set(try store.journal.parked().map(\.kind)), [.notebookCreate, .pageCreate, .pagePut])
        XCTAssertNil(MockPaperServer.state.withState { $0[notebook.path] })

        // Retry asks once more, and settles the same way.
        await engine.retryParked()
        await engine.syncNow()
        XCTAssertEqual(creates(), 2)
        XCTAssertEqual(try store.journal.pendingCount(), 0)
    }

    /// The server refuses a notebook path over 900 bytes (contract 2.2), so
    /// the device does not make one: its create would be refused for good.
    func testANotebookPathTheServerWouldRefuseForLengthIsNotMade() throws {
        // Four-byte characters: a name's slug is at most 200 bytes.
        let long = String(repeating: "\u{1D49C}", count: 50)
        var made = try store.createFolder(name: long, in: "paper")
        for _ in 0..<3 { made = try store.createFolder(name: long, in: made.path) }
        let folder = String(made.path.dropFirst("paper/".count))
        XCTAssertLessThan(made.path.utf8.count, 900)
        let title = long
        XCTAssertGreaterThan("\(made.path)/\(title).paper".utf8.count, 900)

        XCTAssertThrowsError(try store.createNotebook(title: title, folder: folder))
        XCTAssertThrowsError(
            try store.createDocument(
                title: title, folderPath: made.path, kind: .whiteboard,
                options: NewDocumentOptions.whiteboardFactory))
        let top = try store.createNotebook(title: title, folder: "")
        XCTAssertThrowsError(try store.move(notebookID: top.id, toFolder: folder))
        XCTAssertEqual(try store.notebook(id: top.id).path, top.path, "moved anyway")
        XCTAssertEqual(
            try store.move(notebookID: try store.createNotebook(title: "Short").id, toFolder: folder).path,
            "\(made.path)/Short.paper")
        XCTAssertFalse(try store.journal.queue().contains { $0.notebookPath.utf8.count > 900 })
    }
}
