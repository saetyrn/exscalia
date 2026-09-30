import Foundation
import GRDB
import XCTest

@testable import PaperCore

final class OpJournalTests: XCTestCase {
    private var database: PaperDatabase!
    private var journal: OpJournal!

    override func setUpWithError() throws {
        database = try PaperDatabase()
        journal = OpJournal(database: database)
    }

    func testRepeatedPagePutsCoalesceIntoOne() throws {
        for _ in 0..<25 {
            try journal.append(.init(kind: .pagePut, notebookPath: "paper/A.paper", pageID: "p1"))
        }
        try journal.append(.init(kind: .pagePut, notebookPath: "paper/A.paper", pageID: "p2"))

        let pending = try journal.pending()
        XCTAssertEqual(pending.count, 2)
        XCTAssertEqual(pending.map(\.pageID), ["p1", "p2"])
    }

    func testNotebookDeleteDropsThatNotebooksPendingWork() throws {
        try journal.append(.init(kind: .notebookCreate, notebookPath: "paper/A.paper"))
        try journal.append(.init(kind: .pagePut, notebookPath: "paper/A.paper", pageID: "p1"))
        try journal.append(.init(kind: .pagePut, notebookPath: "paper/B.paper", pageID: "p9"))

        try journal.append(.init(kind: .notebookDelete, notebookPath: "paper/A.paper"))

        let pending = try journal.pending()
        XCTAssertEqual(pending.map(\.kind), [.pagePut, .notebookDelete])
        XCTAssertEqual(pending.first?.notebookPath, "paper/B.paper")
    }

    func testDeletingAPageTheServerNeverSawEmitsNothing() throws {
        try journal.append(.init(kind: .pageCreate, notebookPath: "paper/A.paper", pageID: "p1"))
        try journal.append(.init(kind: .pagePut, notebookPath: "paper/A.paper", pageID: "p1"))

        try journal.append(.init(kind: .pageDelete, notebookPath: "paper/A.paper", pageID: "p1"))
        XCTAssertEqual(try journal.pendingCount(), 0)
    }

    /// A run of created pages is chained by `after`. Writing on the first one
    /// before the run goes up must not move its create behind the others, or
    /// the server places them next to a page it has not seen yet.
    func testAPageCreatingPutKeepsItsPlaceWhenThePageIsEditedBeforeItGoesUp() throws {
        try journal.append(.creatingPage(notebookPath: "paper/A.paper", pageID: "p1", after: nil))
        try journal.append(.creatingPage(notebookPath: "paper/A.paper", pageID: "p2", after: "p1"))
        try journal.append(.init(kind: .pageRender, notebookPath: "paper/A.paper", pageID: "p1"))
        try journal.append(.init(kind: .pagePut, notebookPath: "paper/A.paper", pageID: "p1"))
        try journal.append(.init(kind: .pagePut, notebookPath: "paper/A.paper", pageID: "p1"))

        let pending = try journal.pending()
        XCTAssertEqual(pending.map(\.pageID), ["p1", "p2", "p1"])
        XCTAssertEqual(pending.map(\.createsPage), [true, true, false])
        XCTAssertEqual(pending.map { $0.payload["after"] }, [nil, "p1", nil])
    }

    func testDeletingAPageWhoseCreatingPutNeverWentUpEmitsNothing() throws {
        try journal.append(.creatingPage(notebookPath: "paper/A.paper", pageID: "p1", after: nil))
        try journal.append(.init(kind: .pagePut, notebookPath: "paper/A.paper", pageID: "p1"))

        try journal.append(.init(kind: .pageDelete, notebookPath: "paper/A.paper", pageID: "p1"))
        XCTAssertEqual(try journal.pendingCount(), 0)
    }

    func testDeletingAPushedPageStillEmitsADelete() throws {
        try journal.append(.init(kind: .pageCreate, notebookPath: "paper/A.paper", pageID: "p1"))
        try database.queue.write { db in
            try db.execute(sql: "UPDATE ops SET pushed = 1")
        }
        try journal.append(.init(kind: .pageDelete, notebookPath: "paper/A.paper", pageID: "p1"))

        XCTAssertEqual(try journal.pending().map(\.kind), [.pageDelete])
    }

    /// Patches are partial, so the one that stays carries every field: a
    /// favorite followed by a reorder used to leave only the order, and the
    /// favorite never reached the server.
    func testNotebookPatchesCoalesceButOtherKindsDoNot() throws {
        try journal.append(
            .init(kind: .notebookPatch, notebookPath: "paper/A.paper", payload: ["favorite": "true"]))
        try journal.append(
            .init(kind: .notebookPatch, notebookPath: "paper/A.paper", payload: ["tags": "a"]))
        try journal.append(
            .init(
                kind: .notebookPatch, notebookPath: "paper/A.paper",
                payload: ["pages": "p1,p2", "tags": "a,b"]))
        try journal.append(.init(kind: .notebookRename, notebookPath: "paper/A.paper"))
        try journal.append(.init(kind: .notebookRename, notebookPath: "paper/A.paper"))

        let pending = try journal.pending()
        XCTAssertEqual(pending.map(\.kind), [.notebookPatch, .notebookRename, .notebookRename])
        XCTAssertEqual(
            pending.first?.payload, ["favorite": "true", "pages": "p1,p2", "tags": "a,b"])
    }

    /// A new notebook at the path a rename freed must not take the renamed
    /// one's queued patch with its own.
    func testAPatchCoalescesOnlyWithItsOwnNotebooksAtTheSamePath() throws {
        let a = "paper/A.paper"
        try journal.append(
            .init(kind: .notebookPatch, notebookPath: a, notebookID: "X", payload: ["tags": "x"]))
        try journal.append(
            .init(
                kind: .notebookRename, notebookPath: a, notebookID: "X",
                payload: ["new_path": "paper/B.paper"]))
        try journal.append(.init(kind: .notebookCreate, notebookPath: a, notebookID: "Y"))
        try journal.append(
            .init(kind: .notebookPatch, notebookPath: a, notebookID: "Y", payload: ["pages": "q1"]))

        let pending = try journal.pending()
        XCTAssertEqual(
            pending.map(\.kind), [.notebookPatch, .notebookRename, .notebookCreate, .notebookPatch])
        XCTAssertEqual(pending.map(\.notebookID), ["X", "X", "Y", "Y"])
        XCTAssertEqual(pending.first?.payload, ["tags": "x"])
        XCTAssertEqual(pending.last?.payload, ["pages": "q1"])
    }

    /// A parked patch is not folded into the next one: whatever the server
    /// refused in it would park that one too.
    func testAParkedPatchIsDroppedNotFoldedIn() throws {
        try journal.append(
            .init(kind: .notebookPatch, notebookPath: "paper/A.paper", payload: ["cover": "gone"]))
        let parked = try XCTUnwrap(try journal.pending().first?.id)
        try journal.park(parked, reason: "422: unknown page")
        try journal.append(
            .init(kind: .notebookPatch, notebookPath: "paper/A.paper", payload: ["favorite": "true"]))

        XCTAssertEqual(try journal.parked(), [])
        XCTAssertEqual(try journal.pending().map(\.payload), [["favorite": "true"]])
    }

    func testCancellingAnUnpushedDeleteIsTheUndoToast() throws {
        try journal.append(.init(kind: .notebookDelete, notebookPath: "paper/A.paper"))
        XCTAssertTrue(try journal.cancelUnpushed(kind: .notebookDelete, notebookPath: "paper/A.paper"))
        XCTAssertEqual(try journal.pendingCount(), 0)
        XCTAssertFalse(
            try journal.cancelUnpushed(kind: .notebookDelete, notebookPath: "paper/A.paper"))
    }

    // MARK: - Derived ops (M5)

    /// A transcript is derived data, exactly like a render: repeats collapse,
    /// and one queued behind the ink it describes is redundant because that
    /// push reads `text.json` off the disk as it goes.
    func testPageTextCoalescesAndDefersToAPendingPagePut() throws {
        try journal.append(.init(kind: .pageText, notebookPath: "paper/A.paper", pageID: "p1"))
        try journal.append(.init(kind: .pageText, notebookPath: "paper/A.paper", pageID: "p1"))
        XCTAssertEqual(try journal.pending().map(\.kind), [.pageText])

        try journal.append(.init(kind: .pagePut, notebookPath: "paper/A.paper", pageID: "p1"))
        // The put supersedes the queued transcript and carries it along.
        XCTAssertEqual(try journal.pending().map(\.kind), [.pagePut])

        try journal.append(.init(kind: .pageText, notebookPath: "paper/A.paper", pageID: "p1"))
        XCTAssertEqual(try journal.pending().map(\.kind), [.pagePut])
    }

    /// The render and the transcript are separate queues: one must not swallow
    /// the other, because the server takes them as separate parts.
    func testPageTextAndPageRenderDoNotCoalesceIntoEachOther() throws {
        try journal.append(.init(kind: .pageRender, notebookPath: "paper/A.paper", pageID: "p1"))
        try journal.append(.init(kind: .pageText, notebookPath: "paper/A.paper", pageID: "p1"))
        try journal.append(.init(kind: .pageRender, notebookPath: "paper/A.paper", pageID: "p1"))

        XCTAssertEqual(try journal.pending().map(\.kind), [.pageText, .pageRender])
    }

    func testDeletingAPageDropsItsQueuedTranscript() throws {
        try journal.append(.init(kind: .pageCreate, notebookPath: "paper/A.paper", pageID: "p1"))
        try database.queue.write { db in
            try db.execute(sql: "UPDATE ops SET pushed = 1")
        }
        try journal.append(.init(kind: .pageText, notebookPath: "paper/A.paper", pageID: "p1"))
        try journal.append(.init(kind: .pageDelete, notebookPath: "paper/A.paper", pageID: "p1"))

        XCTAssertEqual(try journal.pending().map(\.kind), [.pageDelete])
    }

    func testOrderIsFIFO() throws {
        try journal.append(.init(kind: .notebookCreate, notebookPath: "paper/A.paper"))
        try journal.append(.init(kind: .pageCreate, notebookPath: "paper/A.paper", pageID: "p1"))
        try journal.append(.init(kind: .pagePut, notebookPath: "paper/A.paper", pageID: "p1"))
        XCTAssertEqual(try journal.pending().map(\.kind), [.notebookCreate, .pageCreate, .pagePut])
    }

    // MARK: - Renames and deletes

    /// A notebook renamed before its create went up: every op still queued
    /// for it follows it to the new path, in place, and nothing else moves.
    func testRewritePathMovesOnlyUnpushedOpsAndKeepsTheirOrder() throws {
        try journal.append(.init(kind: .pageCreate, notebookPath: "paper/A.paper", pageID: "p0"))
        try database.queue.write { db in
            try db.execute(sql: "UPDATE ops SET pushed = 1")
        }
        try journal.append(.init(kind: .notebookCreate, notebookPath: "paper/A.paper"))
        try journal.append(.init(kind: .pageCreate, notebookPath: "paper/A.paper", pageID: "p1"))
        try journal.append(.init(kind: .pagePut, notebookPath: "paper/B.paper", pageID: "p9"))
        try journal.append(.init(kind: .pagePut, notebookPath: "paper/A.paper", pageID: "p1"))
        let before = try journal.pending()

        XCTAssertTrue(try journal.rewritePath(from: "paper/A.paper", to: "paper/C.paper"))

        let after = try journal.pending()
        XCTAssertEqual(after.map(\.id), before.map(\.id))
        XCTAssertEqual(after.map(\.kind), before.map(\.kind))
        XCTAssertEqual(
            after.map(\.notebookPath),
            ["paper/C.paper", "paper/C.paper", "paper/B.paper", "paper/C.paper"])
        let pushed = try database.queue.read { db in
            try String.fetchAll(db, sql: "SELECT notebook_path FROM ops WHERE pushed = 1")
        }
        XCTAssertEqual(pushed, ["paper/A.paper"])
    }

    /// Offline: X renamed from A to B, then Y created at the free path A. X's
    /// work still names A, where the server has it, and neither Y's rename
    /// nor Y's delete may take it: re-pointed it 404s, dropped it is lost and
    /// the delete trashes X upstream.
    func testAnotherNotebooksOpsAtTheSamePathAreNotMovedOrDropped() throws {
        let a = "paper/A.paper"
        let x = [
            PaperOp(kind: .pagePut, notebookPath: a, notebookID: "X", pageID: "p1"),
            PaperOp(
                kind: .notebookRename, notebookPath: a, notebookID: "X",
                payload: ["new_path": "paper/B.paper"]),
        ]
        for op in x { try journal.append(op) }
        try journal.append(.init(kind: .notebookCreate, notebookPath: a, notebookID: "Y"))
        try journal.append(.init(kind: .pageCreate, notebookPath: a, notebookID: "Y", pageID: "q1"))

        XCTAssertFalse(try journal.rewritePath(from: a, to: "paper/D.paper", notebookID: "X"))
        XCTAssertTrue(try journal.rewritePath(from: a, to: "paper/C.paper", notebookID: "Y"))
        XCTAssertEqual(
            try journal.pending().map(\.notebookPath), [a, a, "paper/C.paper", "paper/C.paper"])

        try journal.append(.init(kind: .notebookCreate, notebookPath: a, notebookID: "Z"))
        XCTAssertEqual(
            try journal.unpushed(notebookPath: a, notebookID: "Z").map(\.kind), [.notebookCreate])
        try journal.append(.init(kind: .notebookDelete, notebookPath: a, notebookID: "Z"))
        let atA = try journal.pending().filter { $0.notebookPath == a }
        XCTAssertEqual(atA.map(\.kind), [.pagePut, .notebookRename, .notebookDelete])
        XCTAssertEqual(atA.map(\.notebookID), ["X", "X", "Z"])
    }

    /// Once the create is upstream the server's copy is at the old path, so
    /// the queue is left alone and the caller journals a rename.
    func testRewritePathLeavesANotebookTheServerAlreadyHasAlone() throws {
        try journal.append(.init(kind: .pagePut, notebookPath: "paper/A.paper", pageID: "p1"))

        XCTAssertFalse(try journal.rewritePath(from: "paper/A.paper", to: "paper/C.paper"))
        XCTAssertEqual(try journal.pending().map(\.notebookPath), ["paper/A.paper"])
    }

    /// What the device trash keeps for the undo toast is exactly what the
    /// delete drops, parked ops included.
    func testUnpushedIsWhatADeleteDrops() throws {
        try journal.append(
            .init(kind: .notebookPatch, notebookPath: "paper/A.paper", payload: ["favorite": "true"]))
        try journal.append(.init(kind: .pagePut, notebookPath: "paper/A.paper", pageID: "p1"))
        try journal.append(.init(kind: .pageText, notebookPath: "paper/A.paper", pageID: "p2"))
        try journal.append(.init(kind: .pagePut, notebookPath: "paper/B.paper", pageID: "p9"))
        let put = try XCTUnwrap(try journal.pending().first { $0.pageID == "p1" }?.id)
        try journal.park(put, reason: "400: no")

        XCTAssertEqual(
            try journal.unpushed(notebookPath: "paper/A.paper", pageID: "p1").map(\.id), [put])
        let notebook = try journal.unpushed(notebookPath: "paper/A.paper")
        XCTAssertEqual(notebook.map(\.kind), [.notebookPatch, .pagePut, .pageText])

        try journal.append(.init(kind: .notebookDelete, notebookPath: "paper/A.paper"))
        XCTAssertEqual(try journal.unpushed(notebookPath: "paper/A.paper").map(\.kind), [.notebookDelete])
        XCTAssertEqual(try journal.parked(), [])
    }

    /// Rows queued before ops carried a notebook id take the id of the
    /// notebook still at their path; the rest keep an empty one and fall back
    /// to the path.
    func testTheMigrationGivesQueuedOpsTheIDOfTheNotebookAtTheirPath() throws {
        let url = FileManager.default.temporaryDirectory
            .appending(path: "OpJournalTests-\(UUID().uuidString).sqlite", directoryHint: .notDirectory)
        defer { try? FileManager.default.removeItem(at: url) }
        var queue: DatabaseQueue? = try DatabaseQueue(path: url.path(percentEncoded: false))
        try PaperDatabase.migrator.migrate(try XCTUnwrap(queue), upTo: "v2-paper-metadata")
        try queue?.write { db in
            try db.execute(
                sql: """
                    INSERT INTO notebooks (id, path, title, template, paper_size, created, modified)
                    VALUES ('n1', 'paper/A.paper', 'A', 'ruled', 'a4', 0, 0)
                    """)
            try db.execute(
                sql: """
                    INSERT INTO ops (kind, notebook_path, page_id, created_at)
                    VALUES ('pagePut', 'paper/A.paper', 'p1', 0),
                           ('notebookRename', 'paper/Gone.paper', '', 0)
                    """)
        }

        queue = nil

        let ops = try OpJournal(database: PaperDatabase(at: url)).pending()
        XCTAssertEqual(ops.map(\.notebookID), ["n1", ""])
    }
}
