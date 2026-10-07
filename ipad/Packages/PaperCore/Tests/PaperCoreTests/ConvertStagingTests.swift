import CoreGraphics
import Foundation
import PencilKit
import XCTest

@testable import PaperCore

/// The parts of Convert to Whiteboard that run beside everything else: the
/// boards are built without the store's lock, a page written meanwhile is
/// built again from what was written, and a board that cannot be swapped in
/// whole leaves its page as it was.
final class ConvertStagingTests: XCTestCase {
    private var root: URL!
    private var store: PaperStore!

    override func setUpWithError() throws {
        try super.setUpWithError()
        root = FileManager.default.temporaryDirectory
            .appending(path: "ConvertStagingTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        store = try PaperStore(root: root)
    }

    override func tearDownWithError() throws {
        store = nil
        try? FileManager.default.removeItem(at: root)
        try super.tearDownWithError()
    }

    private func stroke(_ x0: CGFloat, _ y0: CGFloat, _ x1: CGFloat, _ y1: CGFloat) -> PKStroke {
        InkFixtures.stroke(from: CGPoint(x: x0, y: y0), to: CGPoint(x: x1, y: y1), width: 4)
    }

    private func notebook(pages: Int = 3) throws -> Notebook {
        let notebook = try store.createNotebook(title: "Lecture", template: .ruled, size: .a4, pageCount: pages)
        for (index, pageID) in notebook.pages.enumerated() {
            try store.writeDrawing(
                InkFixtures.data([stroke(100, 100 + CGFloat(index) * 50, 400, 300)]),
                notebookPath: notebook.path, pageID: pageID)
        }
        return notebook
    }

    /// Autosave takes the lock on the main thread. The decoding, the PDF
    /// pictures and the moved ink are built without it, so a save waits only
    /// for the swaps, not for the whole conversion.
    func testTheBoardsAreBuiltWithoutTheStoresLock() throws {
        let notebook = try notebook()
        let store = try XCTUnwrap(self.store)
        let free = LockedFlag()
        store.testHookBeforeConversionLock = {
            // From another thread: the lock is recursive.
            DispatchQueue.global().sync {
                if store.writeLock.try() {
                    free.set()
                    store.writeLock.unlock()
                }
            }
        }

        try store.convertToWhiteboard(notebookID: notebook.id)

        XCTAssertTrue(free.value, "the lock was held while the boards were built")
        XCTAssertEqual(try store.notebook(id: notebook.id).kind, .whiteboard)
        XCTAssertFalse(
            FileManager.default.fileExists(
                atPath: store.conversionStaging(notebookID: notebook.id).path(percentEncoded: false)),
            "nothing is left where the boards were built")
    }

    /// Ink written to a page after its board was built -- the lock is not
    /// held then -- is what the board holds: the stale board is not swapped
    /// in, and the page is planned and built again under the lock.
    func testAPageWrittenWhileTheBoardsWereBuiltIsBuiltAgainFromWhatWasWritten() throws {
        let notebook = try notebook()
        let store = try XCTUnwrap(self.store)
        let changed = notebook.pages[1]
        let later = InkFixtures.data([stroke(50, 600, 500, 700), stroke(60, 720, 300, 760)])
        store.testHookBeforeConversionLock = {
            try? store.writeDrawing(later, notebookPath: notebook.path, pageID: changed)
        }

        try store.convertToWhiteboard(notebookID: notebook.id)

        let plan = try XCTUnwrap(
            WhiteboardConversion.plan(
                page: Page(id: changed, template: .ruled, size: .a4), number: 2,
                inkBounds: WhiteboardConversion.inkBounds(of: later)))
        let board = try PKDrawing(
            data: XCTUnwrap(store.drawingData(notebookPath: notebook.path, pageID: changed)))
        XCTAssertEqual(board.strokes.count, 2, "the ink written meanwhile, not the ink first read")
        let expected = WhiteboardConversion.inkBounds(
            of: try WhiteboardConversion.translatedDrawing(later, by: plan.translation))
        XCTAssertEqual(board.bounds.midX, try XCTUnwrap(expected).midX, accuracy: 0.5)
        XCTAssertEqual(board.bounds.midY, try XCTUnwrap(expected).midY, accuracy: 0.5)
        let ring = SnapshotRing(paths: store.paths, database: store.database)
        let snapshots = try ring.snapshots(pageID: changed)
        XCTAssertTrue(
            try snapshots.contains { try Data(contentsOf: $0.url) == later },
            "History holds the ink written meanwhile as it was")
        for pageID in notebook.pages {
            XCTAssertEqual(try store.page(notebookPath: notebook.path, id: pageID).size, .board)
        }
    }

    /// (Round 2, item 5) Two conversions of one notebook at once -- two
    /// windows, or a double tap -- shared one staging directory: the second,
    /// starting while the first was swapping, removed the boards the first
    /// was about to swap in, and the first failed half way. The store runs
    /// one conversion per notebook at a time: the second waits, and finds
    /// the work done.
    func testTwoConversionsOfOneNotebookAtOnceRunOneAfterTheOther() throws {
        let notebook = try notebook(pages: 3)
        let store = try XCTUnwrap(self.store)
        let first = notebook.pages[0]
        let before = try XCTUnwrap(store.drawingData(notebookPath: notebook.path, pageID: first))
        let second = Outcome()
        let done = DispatchSemaphore(value: 0)
        let secondStarted = DispatchSemaphore(value: 0)
        let firstDone = DispatchSemaphore(value: 0)
        let starts = Counter()
        let dispatched = LockedFlag()
        // The second run, once it has cleared the staging directory, waits
        // for the first to end -- if it ever gets that far meanwhile.
        store.testHookConversionStarted = {
            guard starts.next() == 2 else { return }
            secondStarted.signal()
            _ = firstDone.wait(timeout: .now() + 20)
        }
        // The first, having swapped its first board, lets the second start.
        store.testHookAfterConversionSwap = { index in
            guard index == 0, !dispatched.value else { return }
            dispatched.set()
            DispatchQueue.global().async {
                second.set(Result { try store.convertToWhiteboard(notebookID: notebook.id) })
                done.signal()
            }
            _ = secondStarted.wait(timeout: .now() + 0.5)
        }

        XCTAssertNoThrow(try store.convertToWhiteboard(notebookID: notebook.id))
        firstDone.signal()
        XCTAssertEqual(done.wait(timeout: .now() + 20), .success)

        XCTAssertNoThrow(try second.value?.get())
        XCTAssertEqual(try store.notebook(id: notebook.id).kind, .whiteboard)
        for pageID in notebook.pages {
            XCTAssertEqual(try store.page(notebookPath: notebook.path, id: pageID).size, .board)
        }
        let plan = try XCTUnwrap(
            WhiteboardConversion.plan(
                page: Page(id: first, template: .ruled, size: .a4), number: 1,
                inkBounds: WhiteboardConversion.inkBounds(of: before)))
        let once = try XCTUnwrap(
            WhiteboardConversion.inkBounds(of: WhiteboardConversion.translatedDrawing(before, by: plan.translation)))
        let board = try XCTUnwrap(
            WhiteboardConversion.inkBounds(of: store.drawingData(notebookPath: notebook.path, pageID: first)))
        XCTAssertEqual(board.midX, once.midX, accuracy: 0.5, "moved once")
    }

    /// (Round 2, item 3) A kill before the first swap left a full copy of
    /// every page in `converting/<notebook>` for good: only a later
    /// conversion of the same notebook cleared it. The launch removes it;
    /// a conversion running then keeps its own.
    func testTheLaunchRemovesWhatAKilledConversionStaged() throws {
        let notebook = try notebook()
        let leftover = store.conversionStaging(notebookID: notebook.id)
            .appending(path: notebook.pages[0], directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: leftover, withIntermediateDirectories: true)
        try Data("a page's copy".utf8).write(to: leftover.appending(path: "drawing.pkdrawing"))
        let running = store.conversionStaging(notebookID: "running")
        try FileManager.default.createDirectory(at: running, withIntermediateDirectories: true)
        store.conversions.enter("running")
        defer { store.conversions.leave("running") }

        store.removeConversionStaging()

        XCTAssertFalse(
            FileManager.default.fileExists(
                atPath: store.conversionStaging(notebookID: notebook.id).path(percentEncoded: false)))
        XCTAssertTrue(FileManager.default.fileExists(atPath: running.path(percentEncoded: false)))
    }

    /// (B5) A board that cannot be swapped in with one rename is not written
    /// into the live page file by file: the conversion stops, the page is
    /// untouched, and a run that can swap finishes it, moving nothing twice.
    func testABoardThatCannotSwapInLeavesItsPageAsItWas() throws {
        let notebook = try notebook(pages: 2)
        let first = notebook.pages[0]
        let pageBefore = try Data(contentsOf: store.paths.pageJSON(notebook.path, first))
        let inkBefore = try XCTUnwrap(store.drawingData(notebookPath: notebook.path, pageID: first))
        store.testSwapFails = true

        XCTAssertThrowsError(try store.convertToWhiteboard(notebookID: notebook.id))

        XCTAssertEqual(try Data(contentsOf: store.paths.pageJSON(notebook.path, first)), pageBefore)
        XCTAssertEqual(store.drawingData(notebookPath: notebook.path, pageID: first), inkBefore)
        XCTAssertEqual(try store.notebook(id: notebook.id).kind, .notebook)
        XCTAssertFalse(try store.journal.queue().contains { $0.payload["metadata"] == "convert" })

        store.testSwapFails = false
        try store.convertToWhiteboard(notebookID: notebook.id)
        let plan = try XCTUnwrap(
            WhiteboardConversion.plan(
                page: Page(id: first, template: .ruled, size: .a4), number: 1,
                inkBounds: WhiteboardConversion.inkBounds(of: inkBefore)))
        let board = try XCTUnwrap(
            WhiteboardConversion.inkBounds(of: store.drawingData(notebookPath: notebook.path, pageID: first)))
        let once = try XCTUnwrap(
            WhiteboardConversion.inkBounds(
                of: WhiteboardConversion.translatedDrawing(inkBefore, by: plan.translation)))
        XCTAssertEqual(board.midX, once.midX, accuracy: 0.5, "moved once")
        XCTAssertEqual(board.midY, once.midY, accuracy: 0.5, "moved once")
    }
}

/// A conversion an app killed half way, and a whiteboard left holding a
/// sheet: neither may stay half converted.
final class ConvertResumeTests: XCTestCase {
    private var roots: [URL] = []

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

    private func device() throws -> (PaperStore, SyncEngine, URL) {
        let root = FileManager.default.temporaryDirectory
            .appending(path: "ConvertResumeTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        roots.append(root)
        let store = try PaperStore(root: root)
        let settings = try XCTUnwrap(ServerSettings(urlString: "https://paper.test"))
        let api = PaperAPI(
            settings: settings, auth: AnonymousAuthProvider(), session: MockPaperServer.makeSession())
        return (store, SyncEngine(store: store, api: api, clientID: "ipad", clock: TestClock(), jitter: { $0 }), root)
    }

    private func ink(_ store: PaperStore, _ notebook: Notebook, _ pageID: String, _ y: CGFloat) throws {
        try store.writeDrawing(
            InkFixtures.data([InkFixtures.stroke(from: CGPoint(x: 100, y: y), to: CGPoint(x: 400, y: y + 100))]),
            notebookPath: notebook.path, pageID: pageID)
        try store.journal.append(.init(kind: .pagePut, notebookPath: notebook.path, pageID: pageID))
    }

    /// Killed after a board was swapped in and its PUT queued, before the
    /// notebook patch: the server would keep a notebook whose first page is
    /// a board, and nothing finished it. The launch reconcile does now.
    func testAConversionStoppedBeforeThePatchIsFinishedAtLaunch() async throws {
        let (store, engine, root) = try device()
        let notebook = try store.createNotebook(title: "Lecture", template: .ruled, size: .a4, pageCount: 2)
        try ink(store, notebook, notebook.pages[0], 100)
        try ink(store, notebook, notebook.pages[1], 600)
        await engine.syncNow()
        XCTAssertEqual(try store.journal.pendingCount(), 0)
        XCTAssertThrowsError(
            try store.convertToWhiteboard(notebookID: notebook.id, stoppingAt: .pageJournalled(0)))
        let firstBoard = store.drawingData(notebookPath: notebook.path, pageID: notebook.pages[0])

        // Relaunch: a new store over the same files, the launch reconcile.
        let relaunched = try PaperStore(root: root)
        _ = try relaunched.reconcile()
        XCTAssertEqual(relaunched.resumeInterruptedConversions(), [notebook.id])
        XCTAssertEqual(relaunched.resumeInterruptedConversions(), [], "nothing left to finish")
        let settings = try XCTUnwrap(ServerSettings(urlString: "https://paper.test"))
        let api = PaperAPI(
            settings: settings, auth: AnonymousAuthProvider(), session: MockPaperServer.makeSession())
        await SyncEngine(store: relaunched, api: api, clientID: "ipad", clock: TestClock(), jitter: { $0 })
            .syncNow()

        XCTAssertEqual(try relaunched.journal.pendingCount(), 0)
        XCTAssertEqual(try relaunched.notebook(id: notebook.id).kind, .whiteboard)
        XCTAssertEqual(
            relaunched.drawingData(notebookPath: notebook.path, pageID: notebook.pages[0]), firstBoard,
            "the board swapped in before the kill is not moved twice")
        let upstream = try XCTUnwrap(MockPaperServer.state.withState { $0[notebook.path] })
        XCTAssertEqual(upstream.kind, "whiteboard")
        for pageID in notebook.pages {
            XCTAssertEqual(upstream.pages[pageID]?.sizeName, "board")
            XCTAssertEqual(try relaunched.page(notebookPath: notebook.path, id: pageID).size, .board)
        }
    }

    /// (Round 2, item 1) A run killed after its first swap left index rows
    /// that still say sheet, so the index alone did not find it before the
    /// launch reconcile read the tree -- too late for a window restoring
    /// its tab. The mark set before the first swap finds it at once, and
    /// goes once the conversion is finished.
    func testAConversionKilledAfterItsFirstSwapIsFoundBeforeTheReconcile() throws {
        let (store, _, root) = try device()
        let notebook = try store.createNotebook(title: "Lecture", template: .ruled, size: .a4, pageCount: 2)
        try ink(store, notebook, notebook.pages[0], 100)
        XCTAssertEqual(store.conversionsToFinish(), [])
        XCTAssertThrowsError(
            try store.convertToWhiteboard(notebookID: notebook.id, stoppingAt: .pageSwapped(0)))

        let relaunched = try PaperStore(root: root)
        XCTAssertEqual(relaunched.interruptedConversions(), [], "the rows still say sheet")
        XCTAssertEqual(relaunched.conversionsToFinish(), [notebook.id])

        _ = try relaunched.reconcile()
        XCTAssertEqual(relaunched.resumeInterruptedConversions(), [notebook.id])
        XCTAssertEqual(relaunched.conversionsToFinish(), [])
        XCTAssertEqual(try relaunched.notebook(id: notebook.id).kind, .whiteboard)
    }

    /// A mark left by a run stopped before its first swap holds nothing back
    /// once the launch has looked: there is nothing to finish.
    func testAMarkWithNothingToFinishGoesAtLaunch() throws {
        let (store, _, _) = try device()
        let notebook = try store.createNotebook(title: "Lecture", pageCount: 1)
        try store.setSyncValue("1", for: PaperStore.conversionMark(notebook.id))
        XCTAssertEqual(store.conversionsToFinish(), [notebook.id])
        XCTAssertEqual(store.resumeInterruptedConversions(), [])
        XCTAssertEqual(store.conversionsToFinish(), [])
        XCTAssertEqual(try store.notebook(id: notebook.id).kind, .notebook)
    }

    /// A whiteboard that holds a sheet -- another device's page kept as the
    /// original of a conflict -- is offered the conversion again, which
    /// turns just that sheet into a board.
    func testAWhiteboardHoldingASheetConvertsJustTheSheet() throws {
        let (store, _, _) = try device()
        let whiteboard = try store.createDocument(
            title: "Plan", folderPath: "paper", kind: .whiteboard,
            options: NewDocumentOptions.whiteboardFactory)
        let board = whiteboard.pages[0]
        let boardFiles = try Data(contentsOf: store.paths.pageJSON(whiteboard.path, board))
        let sheet = try store.insertPage(Page(template: .ruled, size: .a4), notebookID: whiteboard.id)
        try ink(store, whiteboard, sheet.id, 300)
        XCTAssertTrue(store.holdsSheet(notebookID: whiteboard.id))
        XCTAssertEqual(store.resumeInterruptedConversions(), [], "a whiteboard is not resumed at launch")

        try store.convertToWhiteboard(notebookID: whiteboard.id)

        XCTAssertFalse(store.holdsSheet(notebookID: whiteboard.id))
        XCTAssertEqual(try store.page(notebookPath: whiteboard.path, id: sheet.id).size, .board)
        XCTAssertEqual(try Data(contentsOf: store.paths.pageJSON(whiteboard.path, board)), boardFiles)
        let queued = try store.journal.queue().filter { $0.payload["metadata"] == "convert" }
        XCTAssertEqual(queued.map(\.pageID), [sheet.id])
    }

    /// (B7) Every converted board is owed a render until it is drawn, whether
    /// or not it is ever opened, and an app killed before then owes it still.
    func testEveryConvertedBoardIsOwedARenderUntilItIsDrawn() throws {
        let (store, _, _) = try device()
        let notebook = try store.createNotebook(title: "Lecture", template: .ruled, size: .a4, pageCount: 3)
        for pageID in notebook.pages {
            try AtomicFile.write(Data("sheet picture".utf8), to: store.paths.render(notebook.path, pageID))
        }
        XCTAssertEqual(store.boardsOwingRenders(), [])

        try store.convertToWhiteboard(notebookID: notebook.id)

        XCTAssertEqual(store.boardsOwingRenders().map(\.pageID), notebook.pages)
        XCTAssertEqual(store.boardsOwingRenders(notebookIDs: ["other"]), [])
        try AtomicFile.write(Data("board picture".utf8), to: store.paths.render(notebook.path, notebook.pages[1]))
        XCTAssertEqual(store.boardsOwingRenders().map(\.pageID), [notebook.pages[0], notebook.pages[2]])
    }
}

/// A conversion's result, set from another thread.
private final class Outcome: @unchecked Sendable {
    private let lock = NSLock()
    private var result: Result<Notebook, Error>?

    var value: Result<Notebook, Error>? {
        lock.lock()
        defer { lock.unlock() }
        return result
    }

    func set(_ result: Result<Notebook, Error>) {
        lock.lock()
        self.result = result
        lock.unlock()
    }
}

/// A count kept across threads.
private final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0

    func next() -> Int {
        lock.lock()
        defer { lock.unlock() }
        count += 1
        return count
    }
}

/// A flag set from another thread.
private final class LockedFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var flag = false

    var value: Bool {
        lock.lock()
        defer { lock.unlock() }
        return flag
    }

    func set() {
        lock.lock()
        flag = true
        lock.unlock()
    }
}
