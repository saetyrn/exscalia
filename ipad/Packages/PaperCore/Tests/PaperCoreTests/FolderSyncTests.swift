import Foundation
import XCTest

@testable import PaperCore

/// Folders between devices, against `MockPaperServer`: two simulated iPads,
/// each with its own store and engine, and one server.
///
/// The rule under test throughout: a folder renamed or moved on one device
/// is one directory rename on the other, with nothing inside fetched again
/// and the queue behind it re-pointed.
final class FolderSyncTests: XCTestCase {
    private var roots: [URL] = []

    /// One iPad.
    private struct Device {
        let store: PaperStore
        let engine: SyncEngine
        let changes: NotebookChangeRecorder
    }

    override func setUpWithError() throws {
        try super.setUpWithError()
        MockPaperServer.state.reset()
    }

    override func tearDownWithError() throws {
        MockPaperServer.state.reset()
        for root in roots { try? FileManager.default.removeItem(at: root) }
        roots = []
        try super.tearDownWithError()
    }

    // MARK: - Fixtures

    private func device(_ name: String) async throws -> Device {
        let root = FileManager.default.temporaryDirectory
            .appending(path: "FolderSyncTests-\(name)-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        roots.append(root)
        let store = try PaperStore(root: root)
        let settings = try XCTUnwrap(ServerSettings(urlString: "https://paper.test"))
        let api = PaperAPI(
            settings: settings, auth: AnonymousAuthProvider(),
            session: MockPaperServer.makeSession())
        let engine = SyncEngine(
            store: store, api: api, clientID: name, clock: TestClock(), jitter: { $0 })
        let changes = NotebookChangeRecorder()
        await engine.setHandlers(onNotebooksChanged: { changes.record($0) })
        return Device(store: store, engine: engine, changes: changes)
    }

    /// A notebook with ink on every page, queued as autosave leaves it.
    @discardableResult
    private func notebook(_ title: String, in folder: String, on device: Device, pages: Int = 1)
        throws -> Notebook
    {
        let notebook = try device.store.createNotebook(title: title, folder: folder, pageCount: pages)
        for pageID in notebook.pages { try ink("\(title) ink", notebook.path, pageID, on: device) }
        return notebook
    }

    private func ink(_ text: String, _ notebookPath: String, _ pageID: String, on device: Device)
        throws
    {
        try device.store.writeDrawing(Data(text.utf8), notebookPath: notebookPath, pageID: pageID)
        try device.store.journal.append(
            .init(kind: .pagePut, notebookPath: notebookPath, pageID: pageID))
    }

    /// Pushes and pulls until nothing is left to send.
    private func sync(_ device: Device) async {
        await device.engine.syncNow()
    }

    private func assertDrained(_ device: Device, file: StaticString = #filePath, line: UInt = #line) throws {
        XCTAssertEqual(try device.store.journal.pendingCount(), 0, file: file, line: line)
        XCTAssertEqual(try device.store.journal.parked(), [], file: file, line: line)
    }

    /// The requests `body` caused that fetch a page or its files.
    private func pageFetches(during body: () async throws -> Void) async rethrows -> [String] {
        let before = MockPaperServer.state.requests.count
        try await body()
        return MockPaperServer.state.requests.dropFirst(before).filter {
            $0.method == "GET"
                && ($0.path.hasPrefix("/api/paper/pages/") || $0.path.hasPrefix("/api/paper/blob/")
                    || $0.path.hasPrefix("/api/paper/notebooks/"))
        }.map(\.path)
    }

    private func server(_ path: String) -> MockServerState.Notebook? {
        MockPaperServer.state.withState { $0[path] }
    }

    private func serverFolder(_ path: String) -> MockServerState.Folder? {
        MockPaperServer.state.withFolders { $0[path] }
    }

    private func exists(_ path: String, on device: Device) -> Bool {
        FileManager.default.fileExists(
            atPath: device.store.paths.url(forVaultPath: path).path(percentEncoded: false))
    }

    /// Two iPads that both hold `paper/Work` with a notebook "Ideas" in it.
    private func twoDevicesSharingWork(pages: Int = 1) async throws -> (
        a: Device, b: Device, work: PaperFolder, ideas: Notebook
    ) {
        let a = try await device("ipad-a")
        let b = try await device("ipad-b")
        let work = try a.store.createFolder(name: "Work", in: "paper")
        let ideas = try notebook("Ideas", in: "Work", on: a, pages: pages)
        await sync(a)
        await sync(b)
        try assertDrained(a)
        XCTAssertEqual(try b.store.folder(path: "paper/Work")?.id, work.id)
        XCTAssertEqual(try b.store.notebook(path: ideas.path)?.id, ideas.id)
        return (a, b, work, ideas)
    }

    // MARK: - The wire

    func testEachFolderOpSendsTheBodyTheContractNames() async throws {
        let a = try await device("ipad-a")
        let work = try a.store.createFolder(
            name: "Work", in: "paper", color: .orange, icon: .symbol("briefcase"))
        let archive = try a.store.createFolder(name: "Archive", in: "paper")
        await a.engine.pushPending()
        _ = try a.store.renameFolder(id: work.id, to: "Jobs")
        await a.engine.pushPending()
        _ = try a.store.moveFolder(id: work.id, into: archive.path)
        await a.engine.pushPending()
        _ = try a.store.setFolderAppearance(id: work.id, color: .red, icon: .some(nil))
        await a.engine.pushPending()
        _ = try a.store.setFolderFavorite(id: work.id, true)
        await a.engine.pushPending()
        _ = try a.store.deleteFolder(id: work.id)
        await a.engine.pushPending()
        try assertDrained(a)

        let sent = MockPaperServer.state.jsonRequests(matching: "/api/paper/folders")
        XCTAssertEqual(
            sent.map { "\($0.method) \($0.path)" },
            [
                "POST /api/paper/folders", "POST /api/paper/folders",
                "POST /api/paper/folders/paper/Work/rename",
                "POST /api/paper/folders/paper/Jobs/rename",
                "PATCH /api/paper/folders/paper/Archive/Jobs",
                "PATCH /api/paper/folders/paper/Archive/Jobs",
                "DELETE /api/paper/folders/paper/Archive/Jobs",
            ])
        let expected: [[String: Any]] = [
            [
                "parent": "paper", "name": "Work", "id": work.id, "color": "orange",
                "icon": "symbol:briefcase", "favorite": false, "client_id": "ipad-a",
            ],
            [
                "parent": "paper", "name": "Archive", "id": archive.id, "color": "blue",
                "favorite": false, "client_id": "ipad-a",
            ],
            ["new_path": "paper/Jobs", "name": "Jobs", "client_id": "ipad-a"],
            ["new_path": "paper/Archive/Jobs", "client_id": "ipad-a"],
            ["color": "red", "icon": "", "client_id": "ipad-a"],
            ["favorite": true, "client_id": "ipad-a"],
            [:],
        ]
        for (request, body) in zip(sent, expected) {
            XCTAssertEqual(request.body as NSDictionary, body as NSDictionary, request.path)
        }
        XCTAssertEqual(MockPaperServer.state.folderPaths, ["paper/Archive"])
        XCTAssertEqual(
            MockPaperServer.state.tombstones.filter { $0.kind == "folder" }.map(\.id), [work.id])
    }

    func testAFolderRenamedBeforeItsCreateWentUpArrivesUnderTheNewName() async throws {
        let a = try await device("ipad-a")
        let work = try a.store.createFolder(name: "Work", in: "paper")
        let ideas = try notebook("Ideas", in: "Work", on: a)
        _ = try a.store.renameFolder(id: work.id, to: "Jobs")

        await a.engine.pushPending()

        try assertDrained(a)
        XCTAssertEqual(MockPaperServer.state.folderPaths, ["paper/Jobs"])
        XCTAssertEqual(serverFolder("paper/Jobs")?.id, work.id)
        XCTAssertEqual(serverFolder("paper/Jobs")?.name, "Jobs")
        XCTAssertEqual(
            server("paper/Jobs/Ideas.paper")?.pages[ideas.pages[0]]?.drawing, Data("Ideas ink".utf8))
        XCTAssertEqual(MockPaperServer.state.paths(matching: "/rename"), [], "nothing to rename")
    }

    // MARK: - A move from another device

    func testARemoteRenameIsOneDirectoryRenameWithNoPageFetched() async throws {
        let (a, b, work, ideas) = try await twoDevicesSharingWork(pages: 3)
        _ = try a.store.renameFolder(id: work.id, to: "Work 2026")
        await sync(a)
        let announcedBefore = b.changes.calls.count

        let fetched = await pageFetches { await b.engine.pull() }

        XCTAssertEqual(fetched, [], "no page, manifest or blob was fetched")
        let moved = "paper/Work 2026/Ideas.paper"
        XCTAssertEqual(try b.store.folder(id: work.id).path, "paper/Work 2026")
        XCTAssertEqual(try b.store.folder(id: work.id).name, "Work 2026")
        XCTAssertFalse(exists("paper/Work", on: b))
        XCTAssertEqual(try b.store.notebook(id: ideas.id).path, moved)
        XCTAssertEqual(try b.store.notebook(id: ideas.id).folder, "Work 2026")
        XCTAssertEqual(try b.store.pages(ofNotebook: ideas.id).count, 3)
        for pageID in ideas.pages {
            XCTAssertEqual(
                b.store.drawingData(notebookPath: moved, pageID: pageID), Data("Ideas ink".utf8))
        }
        let announced = b.changes.calls.dropFirst(announcedBefore).reduce(into: Set<String>()) {
            $0.formUnion($1)
        }
        XCTAssertTrue(announced.isSuperset(of: [ideas.path, moved]), "\(announced)")
        try assertDrained(b)
    }

    func testTheEchoOfAnOwnRenameCostsNothing() async throws {
        let (a, _, work, _) = try await twoDevicesSharingWork()
        _ = try a.store.renameFolder(id: work.id, to: "Jobs")
        await a.engine.pushPending()
        let announcedBefore = a.changes.calls.count

        let fetched = await pageFetches { await a.engine.pull() }

        XCTAssertEqual(fetched, [])
        XCTAssertEqual(a.changes.calls.count, announcedBefore)
        XCTAssertEqual(try a.store.folder(id: work.id).path, "paper/Jobs")
    }

    func testARemoteMoveRewritesTheQueueForTheNotebooksInside() async throws {
        let (a, b, work, ideas) = try await twoDevicesSharingWork()
        try ink("B's stroke", ideas.path, ideas.pages[0], on: b)
        _ = try b.store.setFavorite(notebookID: ideas.id, true)
        let archive = try a.store.createFolder(name: "Archive", in: "paper")
        _ = try a.store.moveFolder(id: work.id, into: archive.path)
        await sync(a)

        await b.engine.pull()

        let moved = "paper/Archive/Work/Ideas.paper"
        XCTAssertEqual(try b.store.journal.pending().map(\.notebookPath), [moved, moved])
        XCTAssertEqual(try b.store.notebook(id: ideas.id).path, moved)
        await b.engine.pushPending()
        try assertDrained(b)
        XCTAssertEqual(server(moved)?.pages[ideas.pages[0]]?.drawing, Data("B's stroke".utf8))
        XCTAssertEqual(server(moved)?.favorite, true)
    }

    /// The same, when the device pushes before it pulls, as a sync does: the
    /// work meets the old path first and goes again once the pull re-points
    /// it, in its order and within the same sync.
    func testWorkThatMetTheOldPathGoesAgainOnceThePullRePointsIt() async throws {
        let (a, b, work, ideas) = try await twoDevicesSharingWork()
        try ink("B's stroke", ideas.path, ideas.pages[0], on: b)
        _ = try b.store.setFavorite(notebookID: ideas.id, true)
        _ = try a.store.renameFolder(id: work.id, to: "Jobs")
        await sync(a)

        await sync(b)

        try assertDrained(b)
        let moved = "paper/Jobs/Ideas.paper"
        XCTAssertEqual(server(moved)?.pages[ideas.pages[0]]?.drawing, Data("B's stroke".utf8))
        XCTAssertEqual(server(moved)?.favorite, true)
        XCTAssertEqual(try b.store.notebook(id: ideas.id).path, moved)
    }

    /// The notebook is open on B with ink autosave has not written yet when
    /// A's rename arrives. The flush follows the notebook by id: the ink
    /// lands in the moved directory, and no copy of the old path appears.
    func testAFolderMoveArrivingUnderUnsavedInkKeepsTheInk() async throws {
        let (a, b, work, ideas) = try await twoDevicesSharingWork()
        let open = await OpenNotebook(store: b.store, ink: Data("unsaved".utf8))
        await open.markDirty(
            PageRef(notebookID: ideas.id, notebookPath: ideas.path, pageID: ideas.pages[0]))
        _ = try a.store.renameFolder(id: work.id, to: "Jobs")
        await sync(a)

        await b.engine.pull()
        await open.flush()

        let moved = "paper/Jobs/Ideas.paper"
        XCTAssertFalse(exists("paper/Work", on: b), "no stray copy of the old path")
        XCTAssertEqual(
            b.store.drawingData(notebookPath: moved, pageID: ideas.pages[0]), Data("unsaved".utf8))
        XCTAssertEqual(try b.store.journal.pending().map(\.notebookPath), [moved])
        await b.engine.pushPending()
        try assertDrained(b)
        XCTAssertEqual(server(moved)?.pages[ideas.pages[0]]?.drawing, Data("unsaved".utf8))
    }

    // MARK: - Conflicts

    func testTwoDevicesRenamingOneFolderConvergeOnTheLastPush() async throws {
        let (a, b, work, ideas) = try await twoDevicesSharingWork()
        _ = try a.store.renameFolder(id: work.id, to: "Alpha")
        await sync(a)
        _ = try b.store.renameFolder(id: work.id, to: "Beta")

        await sync(b)

        try assertDrained(b)
        XCTAssertEqual(MockPaperServer.state.folderPaths, ["paper/Beta"])
        XCTAssertEqual(serverFolder("paper/Beta")?.name, "Beta")
        XCTAssertNotNil(server("paper/Beta/Ideas.paper"))
        XCTAssertEqual(try b.store.folder(id: work.id).path, "paper/Beta")

        let fetched = await pageFetches { await a.engine.pull() }
        XCTAssertEqual(fetched, [])
        XCTAssertEqual(try a.store.folder(id: work.id).path, "paper/Beta")
        XCTAssertEqual(try a.store.folder(id: work.id).name, "Beta")
        XCTAssertEqual(try a.store.notebook(id: ideas.id).path, "paper/Beta/Ideas.paper")
        XCTAssertFalse(exists("paper/Alpha", on: a))
    }

    func testTwoCreatesOfOneNameMergeIntoTheFirst() async throws {
        let a = try await device("ipad-a")
        let b = try await device("ipad-b")
        let first = try a.store.createFolder(name: "Work", in: "paper")
        let fromA = try notebook("From A", in: "Work", on: a)
        await sync(a)
        let second = try b.store.createFolder(name: "Work", in: "paper", color: .green)
        let fromB = try notebook("From B", in: "Work", on: b)

        await sync(b)

        try assertDrained(b)
        XCTAssertEqual(MockPaperServer.state.folderPaths, ["paper/Work"])
        XCTAssertEqual(try b.store.folder(path: "paper/Work")?.id, first.id)
        XCTAssertNil(try? b.store.folder(id: second.id))
        XCTAssertEqual(try b.store.folder(path: "paper/Work")?.color, .blue, "the first one's record")
        XCTAssertNotNil(server(fromB.path))
        XCTAssertEqual(try b.store.notebook(path: fromA.path)?.id, fromA.id)

        await sync(a)
        XCTAssertEqual(try a.store.notebook(path: fromB.path)?.id, fromB.id)
    }

    func testARenameOntoASiblingsNameFollowsTheServersSuffix() async throws {
        let a = try await device("ipad-a")
        let b = try await device("ipad-b")
        let old = try b.store.createFolder(name: "Old", in: "paper")
        let inside = try notebook("Inside", in: "Old", on: b)
        await sync(b)
        _ = try a.store.createFolder(name: "Jobs", in: "paper")
        await sync(a)
        _ = try b.store.renameFolder(id: old.id, to: "jobs")
        try ink("after", "paper/jobs/Inside.paper", inside.pages[0], on: b)

        await b.engine.pushPending()

        try assertDrained(b)
        XCTAssertEqual(try b.store.folder(id: old.id).path, "paper/jobs 2")
        XCTAssertEqual(try b.store.folder(id: old.id).name, "jobs")
        XCTAssertEqual(
            server("paper/jobs 2/Inside.paper")?.pages[inside.pages[0]]?.drawing, Data("after".utf8))
        await b.engine.pull()
        XCTAssertNotNil(try b.store.folder(path: "paper/Jobs"))
    }

    func testARenameThatOnlyChangesCaseReachesTheOtherDevice() async throws {
        let (a, b, work, ideas) = try await twoDevicesSharingWork()
        _ = try a.store.renameFolder(id: work.id, to: "WORK")
        await sync(a)
        XCTAssertEqual(MockPaperServer.state.folderPaths, ["paper/WORK"])

        let fetched = await pageFetches { await b.engine.pull() }

        XCTAssertEqual(fetched, [])
        let names = try FileManager.default.contentsOfDirectory(
            atPath: b.store.paths.paper.path(percentEncoded: false))
        XCTAssertEqual(names, ["WORK"])
        XCTAssertEqual(try b.store.notebook(id: ideas.id).path, "paper/WORK/Ideas.paper")
    }

    /// B has a notebook inside Work it has not sent; A deletes Work. B's
    /// pull keeps the folder for it, under the record the server makes again
    /// when B's notebook arrives; the rest of Work goes to B's trash.
    func testAFolderDeleteLeavesAFolderForAPendingNotebook() async throws {
        let (a, b, work, ideas) = try await twoDevicesSharingWork()
        let late = try notebook("Late", in: "Work", on: b)
        _ = try a.store.deleteFolder(id: work.id)
        await sync(a)

        await b.engine.pull()

        XCTAssertNil(try b.store.notebook(path: ideas.path))
        XCTAssertNotNil(try b.store.notebook(path: late.path))
        let kept = try XCTUnwrap(try b.store.folder(path: "paper/Work"))
        XCTAssertEqual(kept.id, PaperFolderID.legacy(path: "paper/Work"))

        await sync(b)
        try assertDrained(b)
        XCTAssertEqual(serverFolder("paper/Work")?.id, kept.id)
        XCTAssertNotNil(server(late.path))
        XCTAssertNil(server(ideas.path))
    }

    /// The same, pushing first: the notebook brings the folder back on the
    /// server with the legacy id, and B adopts it.
    func testANotebookCreatedInAFolderAnotherDeviceTrashedBringsItBack() async throws {
        let (a, b, work, ideas) = try await twoDevicesSharingWork()
        let late = try notebook("Late", in: "Work", on: b)
        _ = try a.store.deleteFolder(id: work.id)
        await sync(a)

        await sync(b)

        try assertDrained(b)
        let legacy = PaperFolderID.legacy(path: "paper/Work")
        XCTAssertEqual(serverFolder("paper/Work")?.id, legacy)
        XCTAssertEqual(try b.store.folder(path: "paper/Work")?.id, legacy)
        XCTAssertNil(try b.store.notebook(path: ideas.path))
        XCTAssertNotNil(server(late.path))
        XCTAssertEqual(
            MockPaperServer.state.call("GET", "/api/paper/trash").1["items"].map { ($0 as? [Any])?.count },
            1, "the trashed folder stays in the Trash")

        await sync(a)
        XCTAssertEqual(try a.store.folder(path: "paper/Work")?.id, legacy)
        XCTAssertNotNil(try a.store.notebook(path: late.path))
    }

    func testAFolderDeletedElsewhereGoesToThisDevicesTrash() async throws {
        let (a, b, work, ideas) = try await twoDevicesSharingWork()
        _ = try b.store.renameFolder(id: work.id, to: "Renamed meanwhile")
        _ = try a.store.deleteFolder(id: work.id)
        await sync(a)

        await sync(b)

        try assertDrained(b)
        XCTAssertNil(try? b.store.folder(id: work.id))
        XCTAssertNil(try? b.store.notebook(id: ideas.id))
        XCTAssertEqual(try b.store.folderRecords(), [])
        let trash = try FileManager.default.contentsOfDirectory(
            atPath: b.store.paths.trash.path(percentEncoded: false))
        XCTAssertFalse(trash.isEmpty)
    }

    /// A rename whose folder is already at `new_path` upstream is done; one
    /// whose folder is nowhere upstream is parked, not hidden.
    func testARenameThatFindsNothingIsDoneOnlyWhenItAlreadyLanded() async throws {
        let landed = String(repeating: "c3", count: 16)
        MockPaperServer.state.seedFolder(path: "paper/New", id: landed)
        let b = try await device("ipad-b")
        try b.store.journal.append(
            .init(
                kind: .folderRename, notebookPath: "paper/Gone", notebookID: PaperFolderID.make(),
                payload: ["new_path": "paper/Elsewhere"]))
        try b.store.journal.append(
            .init(
                kind: .folderRename, notebookPath: "paper/Old", notebookID: landed,
                payload: ["new_path": "paper/New"]))

        await b.engine.pushPending()

        XCTAssertEqual(try b.store.journal.pendingCount(), 0)
        let parked = try b.store.journal.parked()
        XCTAssertEqual(parked.map(\.notebookPath), ["paper/Gone"])
        XCTAssertTrue(parked.first?.parkedReason?.hasPrefix("404") ?? false)
    }

    // MARK: - Full reconcile

    func testAFullReconcileBringsDownEveryFolderEmptyOrNot() async throws {
        let state = MockPaperServer.state
        state.seedFolder(path: "paper/Work", id: String(repeating: "a1", count: 16), color: "orange",
            icon: "symbol:briefcase", favorite: true)
        state.seedFolder(path: "paper/Work/Empty", id: String(repeating: "b2", count: 16))
        state.seedDirectory("paper/Bare")
        state.seed(
            notebook: "paper/Work/Ideas.paper", id: PaperID.make(), title: "Ideas",
            pages: [.init(id: "pagealpha", revision: 1, drawing: Data("ink".utf8))])
        let b = try await device("ipad-b")

        await b.engine.pull()

        let records = try b.store.folderRecords()
        XCTAssertEqual(records.map(\.path), ["paper/Bare", "paper/Work", "paper/Work/Empty"])
        XCTAssertEqual(
            records.map(\.id),
            [PaperFolderID.legacy(path: "paper/Bare"), String(repeating: "a1", count: 16),
             String(repeating: "b2", count: 16)])
        XCTAssertEqual(records[1].color, .orange)
        XCTAssertEqual(records[1].icon, .symbol("briefcase"))
        XCTAssertTrue(records[1].favorite)
        XCTAssertTrue(exists("paper/Work/Empty", on: b))
        XCTAssertTrue(exists("paper/Bare", on: b))
        XCTAssertEqual(
            b.store.drawingData(notebookPath: "paper/Work/Ideas.paper", pageID: "pagealpha"),
            Data("ink".utf8))
        XCTAssertEqual(try b.store.journal.queue(), [], "nothing is journalled")
    }

    // MARK: - Notebooks among folders

    /// A notebook whose name a folder holds upstream: the server suffixes the
    /// create and refuses a rename onto the folder's name. The device takes
    /// the server's path instead of retrying forever at the head of the queue.
    func testANotebookCreateRefusedByAFoldersNameFollowsTheServer() async throws {
        MockPaperServer.state.seedFolder(path: "paper/Ideas")
        let b = try await device("ipad-b")
        let ideas = try notebook("ideas", in: "", on: b)

        await b.engine.pushPending()

        try assertDrained(b)
        XCTAssertEqual(try b.store.notebook(id: ideas.id).path, "paper/ideas 2.paper")
        XCTAssertEqual(
            server("paper/ideas 2.paper")?.pages[ideas.pages[0]]?.drawing, Data("ideas ink".utf8))
    }

    func testKindOrientationAndPaperColorRideTheCreateThePatchAndThePull() async throws {
        let b = try await device("ipad-b")
        let board = try b.store.createDocument(
            title: "Plans", folderPath: "paper", kind: .whiteboard, options: .whiteboardFactory)
        let book = try b.store.createNotebook(title: "Book")
        await b.engine.pushPending()
        _ = try b.store.updateNotebookMetadata(
            notebookID: book.id, orientation: .landscape, paperColor: .cream)
        await b.engine.pushPending()

        let creates = MockPaperServer.state.jsonRequests(matching: "/api/paper/notebooks").filter {
            $0.method == "POST" && $0.path == "/api/paper/notebooks"
        }
        XCTAssertEqual(creates.first?.body["kind"] as? String, "whiteboard")
        XCTAssertEqual(creates.first?.body["paperColor"] as? String, "cream")
        XCTAssertEqual(server(board.path)?.kind, "whiteboard")
        XCTAssertEqual(server(board.path)?.paperSize, "board")
        XCTAssertEqual(server(book.path)?.orientation, "landscape")
        XCTAssertEqual(server(book.path)?.paperColor, "cream")

        MockPaperServer.state.editNotebook(book.path) { $0.paperColor = "dark" }
        await b.engine.pull()
        XCTAssertEqual(try b.store.notebook(id: book.id).paperColor, .dark)
        XCTAssertEqual(try b.store.notebook(id: board.id).kind, .whiteboard)
    }

    func testRenderRectRidesTheRenderItDescribes() async throws {
        let b = try await device("ipad-b")
        let book = try b.store.createNotebook(title: "Board")
        let pageID = book.pages[0]
        await b.engine.pushPending()
        let rect = PageObject.Frame(x: 10, y: 20, width: 300, height: 200)
        try b.store.updatePage(notebookPath: book.path, pageID: pageID) { $0.renderRect = rect }
        try AtomicFile.write(Data("png".utf8), to: b.store.paths.render(book.path, pageID))
        try ink("ink", book.path, pageID, on: b)

        await b.engine.pushPending()

        let stored = server(book.path)?.pages[pageID]?.renderRect
        XCTAssertEqual(stored?["x"] as? Double, 10)
        XCTAssertEqual(stored?["height"] as? Double, 200)

        try b.store.updatePage(notebookPath: book.path, pageID: pageID) { $0.renderRect = nil }
        try b.store.journal.append(.init(kind: .pageRender, notebookPath: book.path, pageID: pageID))
        await b.engine.pushPending()
        XCTAssertNil(server(book.path)?.pages[pageID]?.renderRect, "a render without one clears it")
        try assertDrained(b)
    }

    // MARK: - Unknown kinds

    func testAnOpOfAKindThisBuildCannotReadWaitsVisiblyAndSendsNothing() async throws {
        let b = try await device("ipad-b")
        try await b.store.database.queue.write { db in
            try db.execute(
                sql: """
                    INSERT INTO ops (kind, notebook_path, notebook_id, page_id, payload, created_at, pushed)
                    VALUES ('boardSplit', 'paper/Ideas.paper', '', 'p1', '{}', 0, 0)
                    """)
        }
        _ = try b.store.createFolder(name: "Work", in: "paper")

        await b.engine.pushPending()

        XCTAssertEqual(
            MockPaperServer.state.requests.map { "\($0.method) \($0.path)" },
            ["POST /api/paper/folders"], "no page save for it")
        XCTAssertEqual(try b.store.journal.pendingCount(), 0)
        XCTAssertEqual(try b.store.journal.parked().map(\.kind), [.unknown])
        let state = await b.engine.state
        XCTAssertEqual(state, .attention(message: "a change was not sent — unknown operation"))
    }

    // MARK: - Deletes queued before another device's move (D2)

    /// Deletes queued here while another device renamed the folder: the
    /// push meets 404 at the old path, before the pull says where the folder
    /// went. That used to count as done, and the notebook and the page lived
    /// on upstream. They are found by id and deleted where they are.
    func testDeletesQueuedBeforeAFolderMoveWasPulledReachWhatTheyDeleted() async throws {
        let (a, b, work, ideas) = try await twoDevicesSharingWork(pages: 2)
        let other = try notebook("Other", in: "Work", on: a)
        await sync(a)
        await sync(b)
        _ = try b.store.renameFolder(id: work.id, to: "Jobs")
        await sync(b)

        _ = try a.store.deletePage(notebookID: ideas.id, pageID: ideas.pages[1])
        _ = try a.store.delete(notebookID: other.id)
        await a.engine.pushPending()
        await sync(a)

        try assertDrained(a)
        XCTAssertNil(server("paper/Jobs/Other.paper"), "the notebook's delete was lost")
        XCTAssertEqual(server("paper/Jobs/Ideas.paper")?.order, [ideas.pages[0]], "the page's delete was lost")
        XCTAssertNil(try a.store.notebook(path: "paper/Jobs/Other.paper"))
        await sync(b)
        XCTAssertNil(try b.store.notebook(path: "paper/Jobs/Other.paper"))
        XCTAssertEqual(try b.store.notebook(path: "paper/Jobs/Ideas.paper")?.pages, [ideas.pages[0]])
    }

    // MARK: - Unsaved ink in a folder deleted elsewhere (E4)

    /// B has ink queued in a notebook the server already holds, inside a
    /// folder A deletes. The contract (2.7) has B's push bring the folder
    /// back holding that notebook; the page PUT met 404 instead and parked,
    /// and the ink stayed on B alone.
    func testInkQueuedInANotebookOfAFolderDeletedElsewhereBringsBothBack() async throws {
        let (a, b, work, ideas) = try await twoDevicesSharingWork(pages: 2)
        try ink("B, offline", ideas.path, ideas.pages[0], on: b)
        _ = try a.store.deleteFolder(id: work.id)
        await sync(a)
        XCTAssertNil(server(ideas.path))

        await sync(b)

        try assertDrained(b)
        let restored = try XCTUnwrap(server(ideas.path), "the notebook did not come back")
        XCTAssertEqual(restored.id, ideas.id)
        XCTAssertEqual(restored.order, ideas.pages)
        XCTAssertEqual(restored.pages[ideas.pages[0]]?.drawing, Data("B, offline".utf8))
        XCTAssertEqual(restored.pages[ideas.pages[1]]?.drawing, Data("Ideas ink".utf8))
        XCTAssertEqual(serverFolder("paper/Work")?.id, PaperFolderID.legacy(path: "paper/Work"))
        XCTAssertNotNil(try b.store.notebook(path: ideas.path))
        XCTAssertEqual(try b.store.folder(path: "paper/Work")?.id, PaperFolderID.legacy(path: "paper/Work"))
        // A gets it back too.
        await sync(a)
        XCTAssertEqual(
            a.store.drawingData(notebookPath: ideas.path, pageID: ideas.pages[0]), Data("B, offline".utf8))
    }

    /// A delete of something nowhere upstream is still done.
    func testADeleteOfANotebookNowhereUpstreamIsDone() async throws {
        let (a, _, _, ideas) = try await twoDevicesSharingWork()
        MockPaperServer.state.recordDeletion(path: ideas.path, kind: "notebook")
        _ = try a.store.delete(notebookID: ideas.id)
        await a.engine.pushPending()
        try assertDrained(a)
    }
}

/// A notebook open in the editor: autosave holding ink it has not written.
@MainActor
private final class OpenNotebook {
    private let autosave: AutosaveCoordinator

    init(store: PaperStore, ink: Data) {
        autosave = AutosaveCoordinator(
            store: store, snapshots: SnapshotRing(paths: store.paths, database: store.database),
            scheduler: ManualAutosaveScheduler(), drawingData: { _ in ink })
    }

    func markDirty(_ ref: PageRef) { autosave.markDirty(ref) }
    func flush() { autosave.flushAll() }
}
