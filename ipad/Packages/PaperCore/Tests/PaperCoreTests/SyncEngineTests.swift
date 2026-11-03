import Foundation
import XCTest

@testable import PaperCore

/// A clock that never waits and remembers what it was asked to wait for.
final class TestClock: SyncClock, @unchecked Sendable {
    private let lock = NSLock()
    private var recorded: [TimeInterval] = []
    private var _now = Date(timeIntervalSince1970: 1_756_800_000)

    var now: Date {
        lock.lock()
        defer { lock.unlock() }
        return _now
    }

    var sleeps: [TimeInterval] {
        lock.lock()
        defer { lock.unlock() }
        return recorded
    }

    func sleep(seconds: TimeInterval) async throws {
        record(seconds)
        await Task.yield()
    }

    private func record(_ seconds: TimeInterval) {
        lock.lock()
        recorded.append(seconds)
        _now = _now.addingTimeInterval(seconds)
        lock.unlock()
    }
}

/// Collects the states the pill would have shown, in order.
final class StateRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var recorded: [SaveState] = []

    var states: [SaveState] {
        lock.lock()
        defer { lock.unlock() }
        return recorded
    }

    func record(_ state: SaveState) {
        lock.lock()
        recorded.append(state)
        lock.unlock()
    }
}

/// Collects what `onNotebooksChanged` announced, one set per call.
final class NotebookChangeRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var recorded: [Set<String>] = []

    var calls: [Set<String>] {
        lock.lock()
        defer { lock.unlock() }
        return recorded
    }

    func record(_ paths: Set<String>) {
        lock.lock()
        recorded.append(paths)
        lock.unlock()
    }
}

final class SyncEngineTests: XCTestCase {
    private var root: URL!
    private var store: PaperStore!
    private var clock: TestClock!
    private let clientID = "ipad-test-client"

    override func setUpWithError() throws {
        try super.setUpWithError()
        MockPaperServer.state.reset()
        root = FileManager.default.temporaryDirectory
            .appending(path: "SyncEngineTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        store = try PaperStore(root: root)
        clock = TestClock()
    }

    override func tearDownWithError() throws {
        store = nil
        if let root { try? FileManager.default.removeItem(at: root) }
        root = nil
        try super.tearDownWithError()
    }

    // MARK: - Fixtures

    private func makeEngine(recorder: StateRecorder? = nil) async throws -> SyncEngine {
        let settings = try XCTUnwrap(ServerSettings(urlString: "https://paper.test"))
        let api = PaperAPI(
            settings: settings, auth: AnonymousAuthProvider(),
            session: MockPaperServer.makeSession())
        let engine = SyncEngine(
            store: store, api: api, clientID: clientID, clock: clock,
            jitter: { $0 })
        if let recorder {
            let sink = recorder
            await engine.setHandlers(onState: { state in sink.record(state) })
        }
        return engine
    }

    private func exists(_ url: URL) -> Bool {
        FileManager.default.fileExists(atPath: url.path(percentEncoded: false))
    }

    /// A notebook with one page carrying ink, exactly as autosave would leave
    /// it: files on disk, ops in the journal, nothing pushed.
    @discardableResult
    private func seedLocalNotebook(ink: Data = Data("stroke".utf8)) throws -> Notebook {
        let notebook = try store.createNotebook(title: "Ideas", folder: "Work")
        let pageID = try XCTUnwrap(notebook.pages.first)
        try store.writeDrawing(ink, notebookPath: notebook.path, pageID: pageID)
        try store.journal.append(
            .init(kind: .pagePut, notebookPath: notebook.path, pageID: pageID))
        return notebook
    }

    // MARK: - Push

    func testPushDrainsTheJournalInOrderAndLeavesTheInkOnTheServer() async throws {
        let notebook = try seedLocalNotebook()
        let pageID = try XCTUnwrap(notebook.pages.first)
        let engine = try await makeEngine()

        let sent = await engine.pushPending()

        XCTAssertEqual(sent, 3, "notebookCreate, pageCreate, pagePut")
        let calls = MockPaperServer.state.requests.map { "\($0.method) \($0.path)" }
        let mutations = calls.filter { !$0.hasPrefix("GET") }
        XCTAssertEqual(
            mutations,
            [
                "POST /api/paper/notebooks",
                "POST /api/paper/notebooks/paper/Work/Ideas.paper/pages",
                "PUT /api/paper/pages/paper/Work/Ideas.paper/\(pageID)",
            ])

        let stored = MockPaperServer.state.withState { $0["paper/Work/Ideas.paper"] }
        XCTAssertEqual(stored?.title, "Ideas")
        XCTAssertEqual(stored?.pages[pageID]?.drawing, Data("stroke".utf8))
        // create bumps it to 1, the put to 2.
        XCTAssertEqual(stored?.pages[pageID]?.revision, 2)
        let localRevision = store.localRevision(notebookPath: notebook.path, pageID: pageID)
        XCTAssertEqual(localRevision, 2, "the device records what the server called it")
        let pending = try store.journal.pendingCount()
        XCTAssertEqual(pending, 0)
    }

    func testThePageRenderRidesTheSamePutAsTheInk() async throws {
        let notebook = try seedLocalNotebook()
        let pageID = try XCTUnwrap(notebook.pages.first)
        // A render exists by the time the op is pushed; it rides along.
        try Data("png".utf8).write(to: store.paths.render(notebook.path, pageID))
        let engine = try await makeEngine()

        await engine.pushPending()

        let stored = MockPaperServer.state.withState {
            $0["paper/Work/Ideas.paper"]?.pages[pageID]
        }
        XCTAssertEqual(stored?.render, Data("png".utf8))
        XCTAssertEqual(
            stored?.renderRevision, stored?.revision,
            "for_revision named the revision the save landed on, so it is not stale")
    }

    /// M5's transcript takes the same road as the render: a derived-only PUT
    /// with no `page` and no `drawing`, stamped `for_revision`, which the
    /// server files without bumping the revision.
    func testATranscriptIsUploadedAsADerivedOnlyPut() async throws {
        let notebook = try seedLocalNotebook()
        let pageID = try XCTUnwrap(notebook.pages.first)
        let engine = try await makeEngine()
        await engine.pushPending()

        let revisionAfterInk = store.localRevision(notebookPath: notebook.path, pageID: pageID)
        XCTAssertEqual(revisionAfterInk, 2)

        // Vision finishes after the ink has already gone up.
        let text = PageText(
            lines: [
                RecognizedLine(
                    text: "quarterly review",
                    box: PageObject.Frame(x: 60, y: 120, width: 200, height: 24),
                    confidence: 0.9)
            ])
        try store.writePageText(text, notebookPath: notebook.path, pageID: pageID)
        try store.journal.append(
            .init(kind: .pageText, notebookPath: notebook.path, pageID: pageID))

        let sent = await engine.pushPending()
        XCTAssertEqual(sent, 1)

        let stored = try XCTUnwrap(
            MockPaperServer.state.withState { $0["paper/Work/Ideas.paper"]?.pages[pageID] })
        XCTAssertEqual(stored.lastPutParts, ["text"], "a derived push carries no source part")
        XCTAssertEqual(stored.lastPutForRevision, revisionAfterInk)
        XCTAssertEqual(stored.lastPutBaseRevision, revisionAfterInk)
        XCTAssertEqual(
            stored.revision, revisionAfterInk, "a transcript is a reading, never an edit")
        XCTAssertEqual(stored.textRevision, revisionAfterInk)
        let uploaded = try XCTUnwrap(stored.text)
        XCTAssertEqual(
            try PaperJSON.decode(PageText.self, from: uploaded).transcript, "quarterly review")
        XCTAssertEqual(try store.journal.pendingCount(), 0)
    }

    /// Somebody else writes the page between this device's ink and its
    /// render. The derived push answers with their revision; recording it on
    /// the older ink here would let the next stroke replace their change
    /// without a conflict. The revision stays, and the pull fetches theirs.
    func testADerivedPushNeverStampsTheServersNewerRevisionOnOlderInk() async throws {
        let notebook = try seedLocalNotebook()
        let pageID = try XCTUnwrap(notebook.pages.first)
        let engine = try await makeEngine()
        await engine.pushPending()
        await engine.pull()
        let mine = store.localRevision(notebookPath: notebook.path, pageID: pageID)
        XCTAssertEqual(mine, 2)

        MockPaperServer.state.updatePage(notebook: notebook.path, id: pageID) {
            $0.revision += 1
            $0.drawing = Data("theirs".utf8)
        }
        try Data("png".utf8).write(to: store.paths.render(notebook.path, pageID))
        try store.journal.append(
            .init(kind: .pageRender, notebookPath: notebook.path, pageID: pageID))
        // Skipped while the render is queued; the cursor moves past it.
        await engine.pull()
        await engine.pushPending()

        XCTAssertEqual(try store.journal.pendingCount(), 0)
        XCTAssertEqual(store.localRevision(notebookPath: notebook.path, pageID: pageID), mine)

        await engine.pull()

        XCTAssertEqual(store.localRevision(notebookPath: notebook.path, pageID: pageID), mine + 1)
        XCTAssertEqual(
            store.drawingData(notebookPath: notebook.path, pageID: pageID), Data("theirs".utf8))
    }

    /// The other direction: another device read the page, so this one takes
    /// the transcript rather than running Vision again.
    func testAPulledTranscriptLandsOnDiskAndInTheIndex() async throws {
        let transcript = try PaperJSON.encode(
            PageText(
                lines: [
                    RecognizedLine(
                        text: "quarterly review",
                        box: PageObject.Frame(x: 60, y: 120, width: 200, height: 24),
                        confidence: 0.9)
                ]))
        MockPaperServer.state.seed(
            notebook: "paper/Work/Remote.paper", id: "nb-remote", title: "Remote",
            pages: [
                .init(
                    id: "page-remote", revision: 3, renderRevision: 3, textRevision: 3,
                    drawing: Data("ink".utf8), render: Data("png".utf8), text: transcript)
            ])
        let engine = try await makeEngine()

        await engine.pull()

        let onDisk = try XCTUnwrap(
            store.pageText(notebookPath: "paper/Work/Remote.paper", pageID: "page-remote"))
        XCTAssertEqual(onDisk.transcript, "quarterly review")
        XCTAssertEqual(
            store.textIndex.search("quarterly").first?.pages.first?.pageID, "page-remote")

        // A second pull with nothing new must not re-fetch the blob.
        let before = MockPaperServer.state.paths(matching: "text.json").count
        await engine.pull()
        XCTAssertEqual(MockPaperServer.state.paths(matching: "text.json").count, before)
    }

    func testTheMultipartBodyCarriesTheFieldsAndPartsTheContractNames() throws {
        var body = MultipartBody(boundary: "BOUNDARY")
        body.addField("base_revision", "2")
        body.addField("for_revision", "3")
        body.addField("client_id", clientID)
        body.addField("after", "sibling-id")
        body.addPart(.json(name: "page", data: Data(#"{"template":"ruled"}"#.utf8)))
        body.addPart(.binary(name: "drawing", filename: "drawing.pkdrawing", data: Data("ink".utf8)))
        body.addPart(.png(name: "render", data: Data("png".utf8)))
        body.addPart(.json(name: "text", data: Data(#"{"transcript":"hi"}"#.utf8)))
        let encoded = body.encoded()
        let text = String(decoding: encoded, as: UTF8.self)

        XCTAssertEqual(body.contentType, "multipart/form-data; boundary=BOUNDARY")
        XCTAssertTrue(text.contains("Content-Disposition: form-data; name=\"base_revision\"\r\n\r\n2"))
        XCTAssertTrue(text.contains("name=\"for_revision\"\r\n\r\n3"))
        XCTAssertTrue(text.contains("name=\"client_id\"\r\n\r\n\(clientID)"))
        XCTAssertTrue(text.contains("name=\"after\"\r\n\r\nsibling-id"))
        XCTAssertTrue(
            text.contains(
                "name=\"drawing\"; filename=\"drawing.pkdrawing\"\r\nContent-Type: application/octet-stream"
            ))
        XCTAssertTrue(text.contains("name=\"render\"; filename=\"render.png\"\r\nContent-Type: image/png"))
        XCTAssertTrue(text.hasSuffix("--BOUNDARY--\r\n"))

        let parsed = MultipartParser.parse(
            body: encoded, contentType: "multipart/form-data; boundary=BOUNDARY")
        XCTAssertEqual(parsed.fields["base_revision"], "2")
        XCTAssertEqual(parsed.files["drawing"], Data("ink".utf8))
        XCTAssertEqual(parsed.files["render"], Data("png".utf8))
        XCTAssertEqual(parsed.files["text"], Data(#"{"transcript":"hi"}"#.utf8))
    }

    func testAConflictKeepsBothHalvesAndNamesThePage() async throws {
        let notebook = try seedLocalNotebook(ink: Data("mine".utf8))
        let pageID = try XCTUnwrap(notebook.pages.first)
        let engine = try await makeEngine()
        await engine.pushPending()

        // Another device writes the same page, then this one draws again with a
        // revision that is now stale.
        MockPaperServer.state.withState { notebooks in
            notebooks["paper/Work/Ideas.paper"]?.pages[pageID]?.revision = 9
            notebooks["paper/Work/Ideas.paper"]?.pages[pageID]?.drawing = Data("theirs".utf8)
        }
        try store.writeDrawing(Data("mine again".utf8), notebookPath: notebook.path, pageID: pageID)
        try store.journal.append(
            .init(kind: .pagePut, notebookPath: notebook.path, pageID: pageID))

        await engine.pushPending()

        let siblingID = "\(pageID).conflict-20260902120000"
        // The local page became the sibling; the original came back from the
        // server. Neither half was lost.
        XCTAssertTrue(exists(store.paths.pageJSON(notebook.path, siblingID)))
        XCTAssertEqual(
            store.drawingData(notebookPath: notebook.path, pageID: siblingID),
            Data("mine again".utf8))
        XCTAssertEqual(
            store.drawingData(notebookPath: notebook.path, pageID: pageID), Data("theirs".utf8))

        let refreshed = try XCTUnwrap(try store.notebook(path: notebook.path))
        XCTAssertEqual(refreshed.pages, [pageID, siblingID], "the sibling sits right after it")

        let target = await engine.conflict
        XCTAssertEqual(target?.siblingPageID, siblingID)
        XCTAssertEqual(target?.pageNumber, 2)
        let state = await engine.state
        XCTAssertEqual(state, .conflict(pageNumber: 2))

        let sibling = try store.page(notebookPath: notebook.path, id: siblingID)
        XCTAssertEqual(sibling.conflictOf, pageID)
        XCTAssertEqual(sibling.revision, 1)
        let original = try store.page(notebookPath: notebook.path, id: pageID)
        XCTAssertEqual(original.revision, 9)
        let pending = try store.journal.pendingCount()
        XCTAssertEqual(pending, 0, "the conflict resolved the op rather than parking it")
    }

    func testANonConflictClientErrorParksTheOpAndAsksForAttention() async throws {
        let notebook = try seedLocalNotebook()
        let engine = try await makeEngine()
        await engine.pushPending()  // gets the notebook and page up

        try store.journal.append(
            .init(
                kind: .pagePut, notebookPath: notebook.path,
                pageID: try XCTUnwrap(notebook.pages.first)))
        MockPaperServer.state.rejectNextPut = true

        await engine.pushPending()

        let parked = try store.journal.parked()
        XCTAssertEqual(parked.count, 1)
        XCTAssertEqual(parked.first?.kind, .pagePut)
        XCTAssertTrue(parked.first?.parkedReason?.contains("not acceptable") ?? false)
        let pending = try store.journal.pendingCount()
        XCTAssertEqual(pending, 0, "a parked op does not block the queue")
        let state = await engine.state
        guard case .attention(let message) = state else {
            return XCTFail("expected attention, got \(state)")
        }
        XCTAssertTrue(message.contains("a page was not sent"))
    }

    func testOfflinePausesThePushAndReconnectingResumesIt() async throws {
        try seedLocalNotebook()
        let recorder = StateRecorder()
        let engine = try await makeEngine(recorder: recorder)

        await engine.setOnline(false)
        let sentWhileOffline = await engine.pushPending()
        XCTAssertEqual(sentWhileOffline, 0)
        let offlineState = await engine.state
        XCTAssertEqual(offlineState, .offline)
        XCTAssertTrue(
            MockPaperServer.state.requests.isEmpty, "nothing goes near the network when offline")

        await engine.setOnline(true)
        await engine.syncNow()

        XCTAssertNotNil(MockPaperServer.state.withState { $0["paper/Work/Ideas.paper"] })
        let pending = try store.journal.pendingCount()
        XCTAssertEqual(pending, 0)
        XCTAssertTrue(recorder.states.contains(.offline))
    }

    func testTheBackoffDoublesToASixtySecondCeiling() async throws {
        XCTAssertEqual(
            (1...8).map { SyncEngine.backoffBase(attempt: $0) },
            [1, 2, 4, 8, 16, 32, 60, 60])

        try seedLocalNotebook()
        MockPaperServer.state.failuresRemaining = .max
        let engine = try await makeEngine()

        await engine.start()
        // The loop is driven by the test clock, so this is a spin, not a wait.
        var history: [TimeInterval] = []
        for _ in 0..<400 {
            history = await engine.backoffHistory
            if history.count >= 8 { break }
            try? await Task.sleep(nanoseconds: 5_000_000)
        }
        await engine.stop()

        XCTAssertEqual(Array(history.prefix(8)), [1, 2, 4, 8, 16, 32, 60, 60])
        let failures = await engine.failureCount
        XCTAssertGreaterThanOrEqual(failures, 8)
        let pending = try store.journal.pendingCount()
        XCTAssertEqual(pending, 3, "a failing server never costs an op")
    }

    func testTwoOverlappingCyclesStillPushEachOpExactlyOnce() async throws {
        // The poll loop and a foreground sync arriving together used to
        // interleave at the first `await` inside the drain, see the same head
        // of the queue, and create the notebook twice.
        try seedLocalNotebook()
        let engine = try await makeEngine()

        async let first = engine.pushPending()
        async let second = engine.pushPending()
        _ = await (first, second)

        let notebooks = MockPaperServer.state.withState { $0.keys.sorted() }
        XCTAssertEqual(notebooks, ["paper/Work/Ideas.paper"])
        let creates = MockPaperServer.state.requests.filter {
            $0.method == "POST" && $0.path == "/api/paper/notebooks"
        }
        XCTAssertEqual(creates.count, 1)
    }

    func testARetitleSendsTheVerbatimTitleAndAMoveKeepsIt() async throws {
        let notebook = try seedLocalNotebook()
        let engine = try await makeEngine()
        await engine.pushPending()

        let renamed = try store.rename(notebookID: notebook.id, to: "Q3: Plans?")
        XCTAssertEqual(renamed.path, "paper/Work/Q3 Plans.paper")
        await engine.pushPending()
        var stored = MockPaperServer.state.withState { $0["paper/Work/Q3 Plans.paper"] }
        XCTAssertEqual(stored?.title, "Q3: Plans?", "the path is only the slug")
        XCTAssertNil(MockPaperServer.state.withState { $0["paper/Work/Ideas.paper"] })

        _ = try store.move(notebookID: notebook.id, toFolder: "Archive")
        await engine.pushPending()
        stored = MockPaperServer.state.withState { $0["paper/Archive/Q3 Plans.paper"] }
        XCTAssertEqual(stored?.title, "Q3: Plans?")

        await engine.pull()
        XCTAssertEqual(try store.notebook(id: notebook.id).title, "Q3: Plans?")
    }

    func testSignedInClearsTheSignedOutStateAndResumesPushing() async throws {
        try seedLocalNotebook()
        let engine = try await makeEngine()
        MockPaperServer.state.failStatus = 401
        MockPaperServer.state.failuresRemaining = .max

        let refused = await engine.pushPending()
        XCTAssertEqual(refused, 0)
        let signedOut = await engine.state
        XCTAssertEqual(signedOut, .signedOut)
        // A cycle alone never recovers while the server still refuses: signed
        // out, the cycle only asks /api/auth/status; no push is even tried.
        let asked = MockPaperServer.state.requests.count
        await engine.syncNow()
        let tried = MockPaperServer.state.requests.dropFirst(asked)
        XCTAssertTrue(tried.allSatisfy { $0.path.contains("/api/auth/status") }, "\(tried)")
        let stillSignedOut = await engine.state
        XCTAssertEqual(stillSignedOut, .signedOut)

        // The user signs in on the web and the server answers again.
        MockPaperServer.state.failuresRemaining = 0
        let recovered = await engine.signedIn()
        XCTAssertTrue(recovered)
        let state = await engine.state
        XCTAssertNotEqual(state, .signedOut)
        let sent = await engine.pushPending()

        XCTAssertEqual(sent, 3, "notebookCreate, pageCreate, pagePut")
        XCTAssertNotNil(MockPaperServer.state.withState { $0["paper/Work/Ideas.paper"] })
        let pending = try store.journal.pendingCount()
        XCTAssertEqual(pending, 0)
        let again = await engine.signedIn()
        XCTAssertFalse(again, "a healthy engine is left alone")
    }

    // MARK: - Pull

    func testAFullFeedReconcilesFromTheLibrary() async throws {
        MockPaperServer.state.seed(
            notebook: "paper/Shared.paper", id: "nb-shared", title: "Shared",
            pages: [
                .init(id: "pagealpha", revision: 3, drawing: Data("theirs".utf8))
            ])
        let engine = try await makeEngine()

        await engine.pull()

        XCTAssertTrue(exists(store.paths.notebookJSON("paper/Shared.paper")))
        let notebook = try XCTUnwrap(try store.notebook(path: "paper/Shared.paper"))
        XCTAssertEqual(notebook.title, "Shared")
        XCTAssertEqual(notebook.pages, ["pagealpha"])
        XCTAssertEqual(
            store.drawingData(notebookPath: "paper/Shared.paper", pageID: "pagealpha"),
            Data("theirs".utf8))
        let page = try store.page(notebookPath: "paper/Shared.paper", id: "pagealpha")
        XCTAssertEqual(page.revision, 3)
        XCTAssertEqual(page.size, .a4, "the server's points became this device's size name")

        // The cursor is now current, so a second pull is incremental and quiet.
        MockPaperServer.state.reset()
        MockPaperServer.state.seed(
            notebook: "paper/Shared.paper", id: "nb-shared", title: "Shared", pages: [])
        XCTAssertEqual(store.syncValue("changes.epoch"), "epoch-1")
    }

    func testAPageAtTheRevisionWeAlreadyHaveIsNotFetchedAgain() async throws {
        let notebook = try seedLocalNotebook()
        let pageID = try XCTUnwrap(notebook.pages.first)
        let engine = try await makeEngine()
        await engine.pushPending()
        await engine.pull()  // full: establishes the cursor

        let before = MockPaperServer.state.paths(matching: "/api/paper/pages/").count
        // The feed still lists the page this device just pushed -- its own echo.
        await engine.pull()
        let after = MockPaperServer.state.paths(matching: "/api/paper/pages/").count

        XCTAssertEqual(before, after, "an echo of our own save costs no request")
        XCTAssertEqual(store.localRevision(notebookPath: notebook.path, pageID: pageID), 2)
    }

    func testAnEchoOfOurOwnNotebookWriteDoesNotNotify() async throws {
        let notebook = try seedLocalNotebook()
        let engine = try await makeEngine()
        let changes = NotebookChangeRecorder()
        await engine.setHandlers(onNotebooksChanged: { changes.record($0) })
        await engine.pushPending()
        await engine.pull()  // full: establishes the cursor

        // A page added here and written on, as the notebook and autosave leave
        // it: the order patch, the create, then the ink.
        let page = try store.createPage(notebookID: notebook.id, after: notebook.pages.first)
        try store.writeDrawing(Data("more".utf8), notebookPath: notebook.path, pageID: page.id)
        try store.journal.append(.init(kind: .pagePut, notebookPath: notebook.path, pageID: page.id))
        let sent = await engine.pushPending()
        XCTAssertEqual(sent, 3, "notebookPatch, pageCreate, pagePut")
        let before = changes.calls.count
        let manifest = "/api/paper/notebooks/\(notebook.path)"
        func manifestReads() -> Int {
            MockPaperServer.state.requests.filter { $0.method == "GET" && $0.path == manifest }.count
        }
        let readsBefore = manifestReads()

        // The server rewrote notebook.json for the create, so the feed lists
        // the notebook again -- with nothing in it this device does not have.
        await engine.pull()
        XCTAssertEqual(manifestReads(), readsBefore + 1, "the echo should be fetched and compared")

        let echoed = changes.calls.dropFirst(before).filter { $0.contains(notebook.path) }
        XCTAssertTrue(echoed.isEmpty, "our own page create came back as a remote change")
        let stored = try XCTUnwrap(try store.notebook(path: notebook.path))
        XCTAssertEqual(stored.pages.count, 2)
    }

    /// The same echo while the pen is still going: the next write is queued
    /// when the feed lists the notebook, so it is deferred and reconciled by
    /// manifest once that write is pushed. It still has nothing new in it.
    func testADeferredEchoOfOurOwnNotebookWriteDoesNotNotify() async throws {
        let notebook = try seedLocalNotebook()
        let engine = try await makeEngine()
        let changes = NotebookChangeRecorder()
        await engine.setHandlers(onNotebooksChanged: { changes.record($0) })
        await engine.pushPending()
        await engine.pull()  // full: establishes the cursor

        let page = try store.createPage(notebookID: notebook.id, after: notebook.pages.first)
        try store.writeDrawing(Data("more".utf8), notebookPath: notebook.path, pageID: page.id)
        try store.journal.append(.init(kind: .pagePut, notebookPath: notebook.path, pageID: page.id))
        await engine.pushPending()
        try store.writeDrawing(Data("more still".utf8), notebookPath: notebook.path, pageID: page.id)
        try store.journal.append(.init(kind: .pagePut, notebookPath: notebook.path, pageID: page.id))
        let before = changes.calls.count
        let manifest = "/api/paper/notebooks/\(notebook.path)"
        func manifestReads() -> Int {
            MockPaperServer.state.requests.filter { $0.method == "GET" && $0.path == manifest }.count
        }

        await engine.pull()  // the create's echo waits behind the queued write
        let readsBefore = manifestReads()
        let sent = await engine.pushPending()
        XCTAssertEqual(sent, 1)
        await engine.pull()
        XCTAssertEqual(
            manifestReads(), readsBefore + 1, "the deferred notebook should be reconciled")

        let echoed = changes.calls.dropFirst(before).filter { $0.contains(notebook.path) }
        XCTAssertTrue(echoed.isEmpty, "our own page create came back as a remote change")
        XCTAssertEqual(try store.notebook(path: notebook.path)?.pages.count, 2)
    }

    func testARealRemoteNotebookChangeStillNotifies() async throws {
        let notebook = try seedLocalNotebook()
        let engine = try await makeEngine()
        let changes = NotebookChangeRecorder()
        await engine.setHandlers(onNotebooksChanged: { changes.record($0) })
        await engine.pushPending()
        await engine.pull()
        let before = changes.calls.count

        MockPaperServer.state.editNotebook(notebook.path) { $0.title = "Ideas, revised" }
        await engine.pull()

        let announced = changes.calls.dropFirst(before).filter { $0.contains(notebook.path) }
        XCTAssertEqual(announced.count, 1)
        XCTAssertEqual(try store.notebook(path: notebook.path)?.title, "Ideas, revised")
    }

    func testAPageWithUnpushedWorkIsNeverOverwrittenByAPull() async throws {
        let notebook = try seedLocalNotebook(ink: Data("mine".utf8))
        let pageID = try XCTUnwrap(notebook.pages.first)
        let engine = try await makeEngine()
        await engine.pushPending()
        await engine.pull()

        // Somebody else moves the page on, and this device has unpushed ink.
        MockPaperServer.state.withState { notebooks in
            notebooks["paper/Work/Ideas.paper"]?.pages[pageID]?.revision = 7
            notebooks["paper/Work/Ideas.paper"]?.pages[pageID]?.drawing = Data("theirs".utf8)
        }
        try store.writeDrawing(Data("unpushed".utf8), notebookPath: notebook.path, pageID: pageID)
        try store.journal.append(
            .init(kind: .pagePut, notebookPath: notebook.path, pageID: pageID))

        await engine.pull()

        XCTAssertEqual(
            store.drawingData(notebookPath: notebook.path, pageID: pageID),
            Data("unpushed".utf8),
            "the pull left local work alone; the push will turn this into a conflict")
    }

    func testARemoteDeleteIsMirroredIntoTheDeviceTrash() async throws {
        MockPaperServer.state.seed(
            notebook: "paper/Shared.paper", id: "nb-shared", title: "Shared",
            pages: [.init(id: "pagealpha", revision: 1, drawing: Data("ink".utf8))])
        let engine = try await makeEngine()
        await engine.pull()
        XCTAssertTrue(exists(store.paths.notebookJSON("paper/Shared.paper")))

        MockPaperServer.state.recordDeletion(path: "paper/Shared.paper", kind: "notebook")
        await engine.pull()

        XCTAssertFalse(exists(store.paths.notebookJSON("paper/Shared.paper")))
        let entries = try FileManager.default.contentsOfDirectory(
            at: store.paths.trash, includingPropertiesForKeys: nil)
        XCTAssertEqual(entries.count, 1, "the bytes were moved, not unlinked")
    }

    // MARK: - State

    func testTheStateMachineWalksTheStatesThePillShows() async throws {
        let recorder = StateRecorder()
        try seedLocalNotebook()
        let engine = try await makeEngine(recorder: recorder)

        await engine.kick()
        let queued = await engine.state
        XCTAssertEqual(queued, .syncing(count: 3))

        await engine.syncNow()
        let done = await engine.state
        guard case .synced = done else { return XCTFail("expected synced, got \(done)") }
    }
}
