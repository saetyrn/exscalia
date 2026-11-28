import Foundation
import GRDB
import XCTest

@testable import PaperCore

/// What a hook running on one thread tells the test on another.
private final class Flag: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Bool?

    var current: Bool? {
        lock.lock()
        defer { lock.unlock() }
        return value
    }

    func set(_ newValue: Bool) {
        lock.lock()
        value = newValue
        lock.unlock()
    }
}

/// The main actor saves `page.json` -- autosave, bookmarks, objects -- while
/// the sync engine records the server's revisions from its own executor. Both
/// read the file, change a field and write it back; `writeLock` is what keeps
/// the second write from carrying the first one's stale copy.
final class PaperStoreConcurrencyTests: PaperStoreTestCase {
    private var notebook: Notebook!
    private var pageID: String!

    override func setUpWithError() throws {
        try super.setUpWithError()
        notebook = try store.createNotebook(title: "Ideas")
        pageID = try XCTUnwrap(notebook.pages.first)
    }

    override func tearDownWithError() throws {
        store?.testHookBetweenReadAndWrite = nil
        try super.tearDownWithError()
    }

    /// Arms the next `updatePage`: between its read and its write it starts
    /// recording `revision` on a background queue and gives that write a
    /// moment to land. `landedInWindow` says whether it did; `done` is left
    /// once it has landed at all.
    private func recordDuringNextUpdate(revision: Int) -> (landedInWindow: Flag, done: DispatchGroup)
    {
        let store = self.store!
        let path = notebook.path
        let pageID = self.pageID!
        let landed = Flag()
        let done = DispatchGroup()
        store.testHookBetweenReadAndWrite = {
            store.testHookBetweenReadAndWrite = nil
            done.enter()
            DispatchQueue.global().async {
                try? store.recordServerRevisions(
                    notebookPath: path, pageID: pageID, revision: revision)
                done.leave()
            }
            landed.set(done.wait(timeout: .now() + 0.3) == .success)
        }
        return (landed, done)
    }

    func testARevisionRecordedDuringAnUpdateWaitsAndBothChangesSurvive() throws {
        let (landedInWindow, done) = recordDuringNextUpdate(revision: 5)

        try store.setPageBookmarked(notebookID: notebook.id, pageID: pageID, true)

        XCTAssertEqual(landedInWindow.current, false, "the revision landed between read and write")
        XCTAssertEqual(done.wait(timeout: .now() + 5), .success)
        let page = try store.page(notebookPath: notebook.path, id: pageID)
        XCTAssertTrue(page.bookmarked)
        XCTAssertEqual(page.revision, 5)
    }

    /// The interleaving that cost a conflict sibling: autosave read revision
    /// N, the push recorded N+1, autosave wrote N back and the next push was
    /// refused as a conflict with this device's own edit.
    @MainActor
    func testAnAutosaveFlushDoesNotWriteBackTheRevisionItRead() throws {
        let ref = PageRef(notebookID: notebook.id, notebookPath: notebook.path, pageID: pageID)
        let scheduler = ManualAutosaveScheduler()
        let coordinator = AutosaveCoordinator(
            store: store, snapshots: SnapshotRing(paths: store.paths, database: store.database),
            scheduler: scheduler, drawingData: { _ in Data("stroke".utf8) })
        let before = try store.page(notebookPath: notebook.path, id: pageID).modified
        let (landedInWindow, done) = recordDuringNextUpdate(revision: 7)

        coordinator.markDirty(ref)
        scheduler.fire()

        XCTAssertEqual(landedInWindow.current, false, "the revision landed between read and write")
        XCTAssertEqual(done.wait(timeout: .now() + 5), .success)
        let page = try store.page(notebookPath: notebook.path, id: pageID)
        XCTAssertEqual(page.revision, 7)
        XCTAssertGreaterThanOrEqual(page.modified, before)
    }

    func testInterleavedBookmarksAndRevisionsEndOnTheLastOfEach() throws {
        let store = self.store!
        let path = notebook.path
        let pageID = self.pageID!
        let done = DispatchGroup()
        done.enter()
        DispatchQueue.global().async {
            for revision in 1...200 {
                try? store.recordServerRevisions(
                    notebookPath: path, pageID: pageID, revision: revision)
            }
            done.leave()
        }
        var bookmarked = false
        for index in 0..<200 {
            bookmarked = index.isMultiple(of: 3)
            try store.setPageBookmarked(notebookID: notebook.id, pageID: pageID, bookmarked)
        }
        XCTAssertEqual(done.wait(timeout: .now() + 30), .success)

        let page = try store.page(notebookPath: path, id: pageID)
        XCTAssertEqual(page.revision, 200)
        XCTAssertEqual(page.bookmarked, bookmarked)
        // The row is written under the same lock, so it ends where the file does.
        let row = try store.database.queue.read { db in
            try Row.fetchOne(
                db, sql: "SELECT revision, bookmarked FROM pages WHERE id = ?",
                arguments: [pageID])
        }
        XCTAssertEqual(row?["revision"] as Int?, 200)
        XCTAssertEqual(row?["bookmarked"] as Bool?, bookmarked)
    }

    /// A PDF import inserts its pages off the main actor while the notebook
    /// stays in use. A page added meanwhile reads the same order the import
    /// is about to write back, and one of the two used to drop the other.
    func testAPageImportedDuringAPageAddKeepsBothInTheOrder() throws {
        let store = self.store!
        let notebookID = notebook.id
        let imported = Page(template: notebook.template, size: notebook.paperSize)
        let landed = Flag()
        let done = DispatchGroup()
        store.testHookBetweenReadAndWrite = {
            store.testHookBetweenReadAndWrite = nil
            done.enter()
            DispatchQueue.global().async {
                _ = try? store.insertPage(imported, notebookID: notebookID)
                done.leave()
            }
            landed.set(done.wait(timeout: .now() + 0.3) == .success)
        }

        let added = try store.createPage(notebookID: notebookID)

        XCTAssertEqual(landed.current, false, "the import landed between read and write")
        XCTAssertEqual(done.wait(timeout: .now() + 5), .success)
        let expected = [pageID!, added.id, imported.id]
        XCTAssertEqual(try store.notebook(id: notebookID).pages, expected)
        let file = try AtomicFile.readJSON(
            Notebook.self, at: store.paths.notebookJSON(notebook.path))
        XCTAssertEqual(file.pages, expected)
    }

    /// The launch reconcile walks every package off the main actor. A page
    /// deleted while the walk is on its notebook stays deleted: its directory
    /// is not written back as a blank page and its id is not put back in the
    /// order.
    func testAPageDeletedDuringTheReconcileWalkStaysDeleted() throws {
        let store = self.store!
        let notebookID = notebook.id
        let doomed = try store.createPage(notebookID: notebookID)
        let landed = Flag()
        let done = DispatchGroup()
        store.testHookBetweenReadAndWrite = {
            store.testHookBetweenReadAndWrite = nil
            done.enter()
            DispatchQueue.global().async {
                _ = try? store.deletePage(notebookID: notebookID, pageID: doomed.id)
                done.leave()
            }
            landed.set(done.wait(timeout: .now() + 0.3) == .success)
        }

        try store.indexNotebook(at: notebook.path)

        XCTAssertEqual(landed.current, false, "the delete landed inside the walk")
        XCTAssertEqual(done.wait(timeout: .now() + 5), .success)
        XCTAssertFalse(
            exists(store.paths.pageDirectory(notebook.path, doomed.id)),
            "the deleted page came back as a blank one")
        XCTAssertEqual(try store.notebook(id: notebookID).pages, [pageID!])
        let file = try AtomicFile.readJSON(
            Notebook.self, at: store.paths.notebookJSON(notebook.path))
        XCTAssertEqual(file.pages, [pageID!])
    }

    /// An extent that did not change is not a save: `page.json` is not
    /// rewritten and nothing is queued, as before the write moved under the
    /// lock.
    func testAnUnchangedExtentWritesNothing() throws {
        let extent = CGSize(width: 1200, height: 1600)
        let first = try store.setPageExtent(extent, notebookPath: notebook.path, pageID: pageID)
        let file = store.paths.pageJSON(notebook.path, pageID).path(percentEncoded: false)
        let written = try FileManager.default.attributesOfItem(atPath: file)[.modificationDate]
            as? Date
        let queued = try store.journal.pending().count

        let again = try store.setPageExtent(extent, notebookPath: notebook.path, pageID: pageID)

        XCTAssertEqual(again.customSize, first.customSize)
        XCTAssertEqual(
            try FileManager.default.attributesOfItem(atPath: file)[.modificationDate] as? Date,
            written)
        XCTAssertEqual(try store.journal.pending().count, queued)
    }
}
