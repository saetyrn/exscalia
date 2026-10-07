import Foundation
import XCTest

@testable import PaperCore

@MainActor
final class AutosaveCoordinatorTests: XCTestCase {
    private var root: URL!
    private var store: PaperStore!
    private var scheduler: ManualAutosaveScheduler!
    private var notebook: Notebook!
    private var ref: PageRef!
    private var ink = Data("stroke".utf8)

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appending(path: "AutosaveTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        store = try PaperStore(root: root)
        scheduler = ManualAutosaveScheduler()
        notebook = try store.createNotebook(title: "Ideas")
        ref = PageRef(
            notebookID: notebook.id, notebookPath: notebook.path,
            pageID: try XCTUnwrap(notebook.pages.first))
    }

    override func tearDownWithError() throws {
        if let root { try? FileManager.default.removeItem(at: root) }
    }

    /// Makes the page directory refuse new files, the way a full disk or a
    /// locked device's protected data refuses them.
    private func lockPageDirectory(_ locked: Bool) throws {
        try FileManager.default.setAttributes(
            [.posixPermissions: locked ? 0o555 : 0o755],
            ofItemAtPath: store.paths.pageDirectory(ref.notebookPath, ref.pageID)
                .path(percentEncoded: false))
    }

    private func pendingPuts() throws -> Int {
        try store.journal.pending().filter { $0.kind == .pagePut }.count
    }

    private func makeCoordinator() -> AutosaveCoordinator {
        AutosaveCoordinator(
            store: store,
            snapshots: SnapshotRing(paths: store.paths, database: store.database),
            scheduler: scheduler,
            drawingData: { [weak self] _ in self?.ink })
    }

    func testNothingIsWrittenUntilTheDebounceElapses() throws {
        let coordinator = makeCoordinator()
        coordinator.markDirty(ref)

        XCTAssertEqual(scheduler.scheduledDelay, AutosaveCoordinator.debounce)
        XCTAssertNil(store.drawingData(notebookPath: ref.notebookPath, pageID: ref.pageID))
        XCTAssertTrue(coordinator.hasUnsavedChanges)

        scheduler.fire()

        XCTAssertEqual(store.drawingData(notebookPath: ref.notebookPath, pageID: ref.pageID), ink)
        XCTAssertFalse(coordinator.hasUnsavedChanges)
        XCTAssertEqual(coordinator.saveState, .savedLocally)
    }

    func testEveryChangeRestartsTheWindowSoOneFlushCoversTheBurst() throws {
        let coordinator = makeCoordinator()
        for index in 0..<50 {
            ink = Data("stroke \(index)".utf8)
            coordinator.markDirty(ref)
        }
        XCTAssertTrue(scheduler.isScheduled)
        scheduler.fire()

        XCTAssertEqual(
            store.drawingData(notebookPath: ref.notebookPath, pageID: ref.pageID),
            Data("stroke 49".utf8))
        let ring = SnapshotRing(paths: store.paths, database: store.database)
        XCTAssertEqual(try ring.snapshots(pageID: ref.pageID).count, 1)
    }

    func testFlushingOnePageLeavesTheOthersDirty() throws {
        let coordinator = makeCoordinator()
        let second = try store.createPage(notebookID: notebook.id)
        let secondRef = PageRef(
            notebookID: notebook.id, notebookPath: notebook.path, pageID: second.id)

        coordinator.markDirty(ref)
        coordinator.markDirty(secondRef)
        coordinator.flush(ref)

        XCTAssertFalse(coordinator.isDirty(ref))
        XCTAssertTrue(coordinator.isDirty(secondRef))
        XCTAssertNotNil(store.drawingData(notebookPath: ref.notebookPath, pageID: ref.pageID))
        XCTAssertNil(store.drawingData(notebookPath: ref.notebookPath, pageID: second.id))
    }

    func testAFlushSnapshotsBumpsModifiedAndJournalsExactlyOnePut() throws {
        let coordinator = makeCoordinator()
        let before = try store.page(notebookPath: ref.notebookPath, id: ref.pageID).modified

        coordinator.markDirty(ref)
        scheduler.fire()
        ink = Data("stroke two".utf8)
        coordinator.markDirty(ref)
        scheduler.fire()

        let after = try store.page(notebookPath: ref.notebookPath, id: ref.pageID).modified
        XCTAssertGreaterThanOrEqual(after, before)

        let ring = SnapshotRing(paths: store.paths, database: store.database)
        XCTAssertEqual(try ring.snapshots(pageID: ref.pageID).count, 2)

        // pageCreate from notebook creation plus exactly one coalesced pagePut.
        let puts = try store.journal.pending().filter { $0.kind == .pagePut }
        XCTAssertEqual(puts.count, 1)
    }

    func testFlushAllIsWhatBackgroundingCalls() throws {
        let coordinator = makeCoordinator()
        var flushed: [String] = []
        coordinator.onFlushed = { flushed.append($0.pageID) }
        let second = try store.createPage(notebookID: notebook.id)

        coordinator.markDirty(ref)
        coordinator.markDirty(
            PageRef(notebookID: notebook.id, notebookPath: notebook.path, pageID: second.id))
        coordinator.flushAll()

        XCTAssertEqual(flushed.count, 2)
        XCTAssertFalse(scheduler.isScheduled)
        XCTAssertFalse(coordinator.hasUnsavedChanges)
    }

    func testTheNotebooksOwnTimestampSurvivesAReconcile() throws {
        let coordinator = makeCoordinator()
        let created = try store.notebook(id: notebook.id).modified
        coordinator.markDirty(ref)
        scheduler.fire()

        // The library sorts by this, and the reconciler reads it back off disk.
        let relaunched = try PaperStore(root: root)
        try relaunched.reconcile()
        let after = try relaunched.notebook(id: notebook.id).modified
        XCTAssertGreaterThan(after, created)
    }

    func testInkSurvivesTheProcessBoundary() throws {
        let coordinator = makeCoordinator()
        coordinator.markDirty(ref)
        scheduler.fire()

        // Everything the app held is gone; only the files remain.
        let relaunched = try PaperStore(root: root)
        try relaunched.reconcile()
        let notebook = try XCTUnwrap(try relaunched.notebook(path: ref.notebookPath))
        XCTAssertEqual(notebook.pages.first, ref.pageID)
        XCTAssertEqual(
            relaunched.drawingData(notebookPath: ref.notebookPath, pageID: ref.pageID), ink)
    }

    func testAFailedInkWriteStaysDirtyRetriesAndJournalsNothing() throws {
        let coordinator = makeCoordinator()
        var flushed = 0
        coordinator.onFlushed = { _ in flushed += 1 }
        let before = try store.page(notebookPath: ref.notebookPath, id: ref.pageID).modified
        try lockPageDirectory(true)
        defer { try? lockPageDirectory(false) }

        coordinator.markDirty(ref)
        scheduler.fire()

        XCTAssertTrue(coordinator.isDirty(ref))
        XCTAssertTrue(coordinator.hasUnsavedChanges)
        guard case .attention(let message) = coordinator.saveState else {
            return XCTFail("a failed write reported \(coordinator.saveState)")
        }
        XCTAssertTrue(message.hasPrefix("could not save page"))
        XCTAssertTrue(scheduler.isScheduled)
        XCTAssertEqual(scheduler.scheduledDelay, AutosaveCoordinator.retryDelay)
        XCTAssertNil(store.drawingData(notebookPath: ref.notebookPath, pageID: ref.pageID))
        XCTAssertEqual(try pendingPuts(), 0)
        XCTAssertEqual(
            try store.page(notebookPath: ref.notebookPath, id: ref.pageID).modified, before)
        XCTAssertEqual(flushed, 0)

        // Still failing: the wait doubles, up to the cap.
        scheduler.fire()
        XCTAssertEqual(scheduler.scheduledDelay, AutosaveCoordinator.retryDelay * 2)
        for _ in 0..<6 { scheduler.fire() }
        XCTAssertEqual(scheduler.scheduledDelay, AutosaveCoordinator.retryCap)
        XCTAssertEqual(try pendingPuts(), 0)

        try lockPageDirectory(false)
        scheduler.fire()

        XCTAssertEqual(store.drawingData(notebookPath: ref.notebookPath, pageID: ref.pageID), ink)
        XCTAssertFalse(coordinator.hasUnsavedChanges)
        XCTAssertEqual(coordinator.saveState, .savedLocally)
        XCTAssertFalse(scheduler.isScheduled)
        XCTAssertEqual(try pendingPuts(), 1)
        XCTAssertEqual(flushed, 1)

        // A later failure starts the ladder again.
        try lockPageDirectory(true)
        coordinator.markDirty(ref)
        scheduler.fire()
        XCTAssertEqual(scheduler.scheduledDelay, AutosaveCoordinator.retryDelay)
    }

    func testAFailedFlushBeforeRecyclingKeepsTheRetry() throws {
        let coordinator = makeCoordinator()
        try lockPageDirectory(true)
        defer { try? lockPageDirectory(false) }

        coordinator.markDirty(ref)
        coordinator.flush(ref)

        XCTAssertTrue(coordinator.isDirty(ref))
        XCTAssertTrue(scheduler.isScheduled)
        XCTAssertEqual(scheduler.scheduledDelay, AutosaveCoordinator.retryDelay)
        guard case .attention = coordinator.saveState else {
            return XCTFail("a failed flush reported \(coordinator.saveState)")
        }
        XCTAssertEqual(try pendingPuts(), 0)
    }

    func testASnapshotFailureIsStillASave() throws {
        let coordinator = makeCoordinator()
        // A file where the page's snapshot directory belongs.
        let directory = store.paths.snapshotDirectory(ref.pageID)
        let blocker = directory.deletingLastPathComponent()
            .appending(path: ref.pageID, directoryHint: .notDirectory)
        try FileManager.default.createDirectory(
            at: blocker.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("not a directory".utf8).write(to: blocker)

        coordinator.markDirty(ref)
        scheduler.fire()

        XCTAssertEqual(store.drawingData(notebookPath: ref.notebookPath, pageID: ref.pageID), ink)
        XCTAssertEqual(coordinator.saveState, .savedLocally)
        XCTAssertFalse(coordinator.hasUnsavedChanges)
        XCTAssertFalse(scheduler.isScheduled)
        XCTAssertEqual(try pendingPuts(), 1)
        let ring = SnapshotRing(paths: store.paths, database: store.database)
        XCTAssertEqual(try ring.snapshots(pageID: ref.pageID).count, 0)
    }
}
