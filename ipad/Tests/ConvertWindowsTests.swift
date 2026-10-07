import PaperCore
import PencilKit
import XCTest

@testable import PaperUI

/// Convert to Whiteboard across the windows of one controller: every window
/// showing the document writes its ink and comes down before the store
/// rewrites it, and opens again on the whiteboard afterwards.
@MainActor
final class ConvertWindowsTests: XCTestCase {
    private func makeController() throws -> (PaperController, PaperStore) {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("ConvertWindowsTests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        let store = try PaperStore(root: root)
        return (PaperController(store: store), store)
    }

    private static func stroke(y: Double) -> PKStroke {
        let points = stride(from: 0.0, through: 320.0, by: 8.0).map { offset -> PKStrokePoint in
            PKStrokePoint(
                location: CGPoint(x: 90 + offset, y: y + sin(offset / 40) * 30),
                timeOffset: offset / 320, size: CGSize(width: 5, height: 5), opacity: 1,
                force: 1, azimuth: 0, altitude: .pi / 2)
        }
        return PKStroke(
            ink: PKInk(.pen, color: .black),
            path: PKStrokePath(controlPoints: points, creationDate: Date()))
    }

    /// Window B shows the notebook too and holds ink not yet written. It used
    /// to keep its canvas through the conversion, and its next save wrote its
    /// whole old-coordinate drawing over the board.
    func testEveryWindowShowingTheDocumentWritesItsInkAndComesDownFirst() async throws {
        let (controller, store) = try makeController()
        let notebook = try store.createNotebook(
            title: "Lecture", template: .ruled, size: .a4, pageCount: 1)
        let pageID = notebook.pages[0]
        controller.refresh()
        let a = DocumentTabsModel(controller: controller)
        let b = DocumentTabsModel(controller: controller)
        a.open(notebook)
        b.open(notebook)
        let oldB = try XCTUnwrap(b.session)
        let ink = PKDrawing(strokes: [Self.stroke(y: 400)]).dataRepresentation()
        let canvasB = controller.addDrawingProvider { $0.pageID == pageID ? ink : nil }
        controller.markPageDirty(
            PageRef(notebookID: notebook.id, notebookPath: notebook.path, pageID: pageID),
            source: canvasB)
        var kindWhenBWrote: DocumentKind?
        oldB.onFlush = { kindWhenBWrote = try? store.notebook(id: notebook.id).kind }

        await PaperConvertDocumentAction(tabs: a).convert(notebook)

        XCTAssertEqual(kindWhenBWrote, .notebook, "B wrote its ink and came down before the rewrite")
        XCTAssertFalse(b.session === oldB, "B kept the canvas it had")
        XCTAssertEqual(b.activeID, notebook.id)
        XCTAssertEqual(b.session?.notebook.kind, .whiteboard)
        XCTAssertEqual(a.session?.notebook.kind, .whiteboard)
        XCTAssertFalse(controller.isConverting(notebook.id))
        // B's ink is on the board, moved with it to the middle.
        let board = try PKDrawing(
            data: XCTUnwrap(store.drawingData(notebookPath: notebook.path, pageID: pageID)))
        XCTAssertEqual(board.strokes.count, 1)
        XCTAssertEqual(board.bounds.midX, 8192, accuracy: 2)
        XCTAssertEqual(board.bounds.midY, 8192, accuracy: 2)
    }

    /// A window that asks for the document while it is being converted gets
    /// it afterwards, not a canvas on the pages being rewritten.
    func testNothingOpensTheDocumentWhileItIsBeingConverted() async throws {
        let (controller, store) = try makeController()
        let notebook = try store.createNotebook(title: "Held", pageCount: 1)
        let other = try store.createNotebook(title: "Other", pageCount: 1)
        controller.refresh()
        let a = DocumentTabsModel(controller: controller)
        let b = DocumentTabsModel(controller: controller)
        a.open(notebook)
        b.open(other)
        b.open(notebook)
        b.activate(other.id)

        let converting = Task { await PaperConvertDocumentAction(tabs: a).convert(notebook) }
        // Until it reaches the store, which runs off the main actor.
        for _ in 0..<100 where !controller.isConverting(notebook.id) { await Task.yield() }
        XCTAssertTrue(controller.isConverting(notebook.id))
        b.goHome()
        b.activate(notebook.id)
        XCTAssertNil(b.session, "a canvas opened on a document being converted")
        await converting.value

        XCTAssertEqual(b.session?.notebook.id, notebook.id)
        XCTAssertEqual(b.session?.notebook.kind, .whiteboard)
    }

    /// (B4) A whiteboard left holding a sheet is offered the conversion
    /// again; a whole whiteboard is not.
    func testAWhiteboardHoldingASheetIsOfferedTheConversion() throws {
        let (controller, store) = try makeController()
        let whiteboard = try store.createDocument(
            title: "Plan", folderPath: "paper", kind: .whiteboard,
            options: NewDocumentOptions.whiteboardFactory)
        controller.refresh()
        let tabs = DocumentTabsModel(controller: controller)
        let action = PaperConvertDocumentAction(tabs: tabs)
        XCTAssertFalse(action.isOffered(whiteboard))
        XCTAssertTrue(action.isBlocked(whiteboard, inTab: false))

        _ = try store.insertPage(Page(template: .ruled, size: .a4), notebookID: whiteboard.id)
        let holding = try store.notebook(id: whiteboard.id)
        XCTAssertTrue(action.isOffered(holding))
        XCTAssertFalse(action.isBlocked(holding, inTab: false))
    }

    /// (B4) A notebook an app killed mid-conversion -- a notebook holding a
    /// board -- is finished by the launch reconcile, and its boards drawn.
    func testTheLaunchReconcileFinishesAConversionAKillLeftHalfDone() async throws {
        let (_, store) = try makeController()
        let notebook = try store.createNotebook(title: "Halfway", template: .ruled, size: .a4, pageCount: 2)
        var board = try store.page(notebookPath: notebook.path, id: notebook.pages[0])
        board.size = .board
        board.boardExtent = SizePoints(width: 16_384, height: 16_384)
        try store.save(page: board, notebookPath: notebook.path)

        let relaunched = PaperController(store: store)
        await relaunched.waitForInitialReconcile()

        XCTAssertEqual(try store.notebook(id: notebook.id).kind, .whiteboard)
        for pageID in notebook.pages {
            XCTAssertEqual(try store.page(notebookPath: notebook.path, id: pageID).size, .board)
        }
        XCTAssertTrue(
            try store.journal.pending().contains { $0.kind == .notebookPatch && $0.payload["kind"] == "whiteboard" })
    }

    /// (Round 2, item 1) A conversion a kill left half done is finished by
    /// the launch reconcile, off the main actor. Windows restore their tabs
    /// as soon as they appear, and the library and Recents open documents on
    /// a tap: none of them may put an editor on that notebook first, whose
    /// canvas would hold the old ink and write it over the boards. They wait
    /// for the launch to finish it, and then open it as a whiteboard.
    func testNoWindowOpensAConversionTheLaunchIsFinishingBeforeItIsFinished() async throws {
        let (seeded, store) = try makeController()
        _ = seeded
        let notebook = try store.createNotebook(title: "Halfway", template: .ruled, size: .a4, pageCount: 2)
        let other = try store.createNotebook(title: "Other", pageCount: 1)
        var board = try store.page(notebookPath: notebook.path, id: notebook.pages[0])
        board.size = .board
        board.boardExtent = SizePoints(width: 16_384, height: 16_384)
        try store.save(page: board, notebookPath: notebook.path)

        // The relaunch: a new controller, and its windows before the
        // launch reconcile has run.
        let controller = PaperController(store: store)
        var announced: Set<String> = []
        controller.observeRemoteChanges { announced.formUnion($0) }
        let restored = DocumentTabsModel(controller: controller)
        let tapped = DocumentTabsModel(controller: controller)
        let recents = DocumentTabsModel(controller: controller)
        let stored = DocumentTabsModel.Stored(
            tabs: [other.id, notebook.id], active: notebook.id, sidebar: false, sidebarTab: "pages")
        restored.restore(json: String(decoding: try JSONEncoder().encode(stored), as: UTF8.self))
        XCTAssertNil(restored.session, "tab restore opened the notebook being finished")
        tapped.open(try store.notebook(id: notebook.id))
        XCTAssertNil(tapped.session, "a library tap opened it")
        let recent = try XCTUnwrap(controller.recentNotebooks().first { $0.id == notebook.id })
        recents.open(recent)
        XCTAssertNil(recents.session, "Recents opened it")
        recents.open(other)
        XCTAssertEqual(recents.session?.notebook.id, other.id, "other documents open as usual")

        await controller.waitForInitialReconcile()

        XCTAssertEqual(try store.notebook(id: notebook.id).kind, .whiteboard)
        XCTAssertFalse(controller.isConverting(notebook.id))
        XCTAssertTrue(announced.contains(notebook.path), "the windows were not told")
        XCTAssertEqual(restored.session?.notebook.id, notebook.id)
        XCTAssertEqual(restored.session?.notebook.kind, .whiteboard)
        XCTAssertEqual(tapped.session?.notebook.kind, .whiteboard)
        XCTAssertEqual(recents.session?.notebook.id, other.id, "a window that moved on stays")
    }

    /// (Round 2, item 3) What a killed conversion staged is removed at launch.
    func testTheLaunchRemovesWhatAKilledConversionStaged() async throws {
        let (_, store) = try makeController()
        let notebook = try store.createNotebook(title: "Staged", pageCount: 1)
        let staged = store.paths.root.appending(path: "converting/\(notebook.id)/\(notebook.pages[0])")
        try FileManager.default.createDirectory(at: staged, withIntermediateDirectories: true)
        try Data("a copy of the page".utf8).write(to: staged.appending(path: "drawing.pkdrawing"))

        let controller = PaperController(store: store)
        await controller.waitForInitialReconcile()

        XCTAssertFalse(
            FileManager.default.fileExists(
                atPath: store.paths.root.appending(path: "converting/\(notebook.id)").path(percentEncoded: false)))
    }

    /// A conversion with the sync engine held, and the app sent to the
    /// background half way (its flush takes the store's lock on the main
    /// thread): nothing deadlocks, the conversion finishes, and the hold is
    /// released. The server here is one nothing listens on, so every
    /// request fails at once, as a suspended app's do.
    func testAConversionSentToTheBackgroundFinishesAndReleasesTheEngine() async throws {
        let (controller, store) = try makeController()
        let settings = try XCTUnwrap(ServerSettings(urlString: "http://127.0.0.1:8799"))
        let session = PaperAPI.makeSession()
        controller.configureSync(
            api: PaperAPI(settings: settings, auth: AnonymousAuthProvider(), session: session),
            clientID: "background-test", session: session)
        let engine = try XCTUnwrap(controller.sync)
        let notebook = try store.createNotebook(title: "Away", template: .ruled, size: .a4, pageCount: 3)
        let other = try store.createNotebook(title: "Other", pageCount: 1)
        controller.refresh()
        let window = DocumentTabsModel(controller: controller)
        window.open(other)
        let ink = PKDrawing(strokes: [Self.stroke(y: 300)]).dataRepresentation()
        let canvas = controller.addDrawingProvider { $0.pageID == other.pages[0] ? ink : nil }

        let converting = Task {
            await controller.convertToWhiteboard(notebook, syncWait: .milliseconds(200))
        }
        for _ in 0..<200 where !controller.isConverting(notebook.id) { await Task.yield() }
        // Ink in another document, then the app goes to the background.
        controller.markPageDirty(
            PageRef(notebookID: other.id, notebookPath: other.path, pageID: other.pages[0]),
            source: canvas)
        controller.flushForBackground()

        let finished = expectation(description: "the conversion finished")
        Task {
            let refusal = await converting.value
            XCTAssertNil(refusal)
            finished.fulfill()
        }
        await fulfillment(of: [finished], timeout: 30)

        XCTAssertEqual(try store.notebook(id: notebook.id).kind, .whiteboard)
        XCTAssertNotNil(store.drawingData(notebookPath: other.path, pageID: other.pages[0]))
        var released = false
        for _ in 0..<100 {
            if await !engine.isHeld {
                released = true
                break
            }
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTAssertTrue(released, "the engine was left held")
        await engine.stop()
    }
}
