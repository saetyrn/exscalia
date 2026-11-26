import Foundation
import GRDB
import XCTest

@testable import PaperCore

/// Folders on one device: what each folder operation does to the tree, the
/// index and the op journal, the journal's folder rules on their own, the
/// reconciler that gives legacy directories their records, and the
/// `v5-folders` migration on a container that already holds work.
final class FolderJournalTests: XCTestCase {
    private var root: URL!
    private var store: PaperStore!

    override func setUpWithError() throws {
        try super.setUpWithError()
        root = FileManager.default.temporaryDirectory
            .appending(path: "FolderJournalTests-\(UUID().uuidString)", directoryHint: .isDirectory)
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

    private func exists(_ path: String) -> Bool {
        FileManager.default.fileExists(
            atPath: store.paths.url(forVaultPath: path).path(percentEncoded: false))
    }

    /// Directory names as the disk spells them -- what a case-only rename is
    /// judged by on a case-insensitive Mac.
    private func children(_ path: String) throws -> [String] {
        try FileManager.default.contentsOfDirectory(atPath: store.paths.url(forVaultPath: path)
            .path(percentEncoded: false)).sorted()
    }

    /// Marks everything queued as pushed, as a drain that succeeded would.
    private func drain() throws {
        for op in try store.journal.pending() { try store.journal.complete(try XCTUnwrap(op.id)) }
    }

    private func notebook(_ title: String, in folder: String = "") throws -> Notebook {
        let notebook = try store.createNotebook(title: title, folder: folder)
        let pageID = try XCTUnwrap(notebook.pages.first)
        try store.writeDrawing(Data("ink".utf8), notebookPath: notebook.path, pageID: pageID)
        try store.journal.append(.init(kind: .pagePut, notebookPath: notebook.path, pageID: pageID))
        return notebook
    }

    private func folderJSON(_ path: String) -> [String: Any]? {
        let url = store.paths.url(forVaultPath: path).appending(path: "folder.json")
        return (try? Data(contentsOf: url)).flatMap {
            (try? JSONSerialization.jsonObject(with: $0)) as? [String: Any]
        }
    }

    // MARK: - Create

    func testCreateWritesTheRecordAndJournalsTheContractPayload() throws {
        let folder = try store.createFolder(
            name: "  Accounting ", in: "paper/Work", color: .red, icon: .symbol("chart.bar"))

        XCTAssertEqual(folder.path, "paper/Work/Accounting")
        XCTAssertEqual(folder.name, "Accounting")
        XCTAssertTrue(PaperFolderID.isValid(folder.id))
        XCTAssertEqual(folderJSON(folder.path)?["id"] as? String, folder.id)
        XCTAssertNil(folderJSON(folder.path)?["path"], "the file's place is its path")
        // The missing parent is made with the record the server derives too.
        let parent = try XCTUnwrap(try store.folder(path: "paper/Work"))
        XCTAssertEqual(parent.id, "9e7201dacb87ef0d43fb22798ccf4af6")
        XCTAssertEqual(try store.folder(id: folder.id), folder)

        let queued = try store.journal.pending()
        XCTAssertEqual(queued.map(\.kind), [.folderCreate], "the parent journals nothing")
        XCTAssertEqual(queued.first?.notebookPath, "paper/Work/Accounting")
        XCTAssertEqual(queued.first?.notebookID, folder.id)
        XCTAssertEqual(
            queued.first?.payload,
            ["name": "Accounting", "color": "red", "icon": "symbol:chart.bar", "favorite": "false"])
    }

    /// The folders table keeps the seconds a `Date` holds. Kept as seconds
    /// since 1970 they lost the last bit of about half the dates `Date()`
    /// makes, and the test above failed one run in two.
    func testEveryFolderReadsBackExactlyAsItWasCreated() throws {
        var lossy = 0
        for index in 0..<24 {
            let folder = try store.createFolder(name: "Folder \(index)", in: "paper")
            XCTAssertEqual(try store.folder(id: folder.id), folder)
            if Date(timeIntervalSince1970: folder.created.timeIntervalSince1970) != folder.created {
                lossy += 1
            }
        }
        // Otherwise this proved nothing: one in about sixteen million.
        XCTAssertGreaterThan(lossy, 0, "no date here that seconds since 1970 would change")
    }

    /// A row an earlier build of this branch wrote in seconds since 1970
    /// reads 31 years late until the launch reconcile, which upserts every
    /// folder's row from its `folder.json`.
    func testAFolderRowInSecondsSince1970IsCorrectedByTheLaunchReconcile() throws {
        let folder = try store.createFolder(name: "Work", in: "paper")
        try store.database.queue.write { db in
            try db.execute(
                sql: "UPDATE folders SET created = ?, modified = ? WHERE id = ?",
                arguments: [
                    folder.created.timeIntervalSince1970, folder.modified.timeIntervalSince1970,
                    folder.id,
                ])
        }
        let stale = try store.folder(id: folder.id)
        XCTAssertGreaterThan(stale.created.timeIntervalSince(folder.created), 30 * 365 * 86_400)

        try store.reconcile()

        let url = store.paths.url(forVaultPath: folder.path).appending(path: "folder.json")
        let onDisk = try PaperJSON.decode(PaperFolder.self, from: Data(contentsOf: url))
        let healed = try store.folder(id: folder.id)
        XCTAssertEqual(healed.created, onDisk.created)
        XCTAssertEqual(healed.modified, onDisk.modified)
        XCTAssertEqual(healed.created.timeIntervalSince(folder.created), 0, accuracy: 0.001)
    }

    func testANameTakenBesideItNamesTheNewFolderWithASuffix() throws {
        _ = try store.createFolder(name: "Work", in: "paper")
        _ = try notebook("Ideas")

        let second = try store.createFolder(name: "WORK", in: "paper")
        let third = try store.createFolder(name: "ideas", in: "paper")
        let empty = try store.createFolder(name: "   ", in: "paper")

        XCTAssertEqual(second.path, "paper/WORK 2")
        XCTAssertEqual(second.name, "WORK 2")
        XCTAssertEqual(third.path, "paper/ideas 2")
        XCTAssertEqual(empty.name, "Untitled")
        XCTAssertEqual(
            try store.journal.pending().filter { $0.kind == .folderCreate }.map { $0.payload["name"] },
            ["Work", "WORK 2", "ideas 2", "Untitled"])
    }

    func testAFolderNamedLikeAPackageOrARecordIsSluggedApart() throws {
        let package = try store.createFolder(name: "Notes.paper", in: "paper")
        let record = try store.createFolder(name: "folder.json", in: "paper")
        XCTAssertEqual(package.path, "paper/Notes.paper_")
        XCTAssertEqual(record.path, "paper/folder.json_")
        XCTAssertThrowsError(try store.createFolder(name: "X", in: "paper/Ideas.paper"))
    }

    // MARK: - Rename and move

    func testARenameBeforeTheCreateWentUpRewritesTheQueueAndJournalsNoRename() throws {
        let folder = try store.createFolder(name: "Work", in: "paper")
        let inside = try notebook("Ideas", in: "Work")
        _ = try store.storeAsset(Data("png".utf8), preferredName: "a.png", notebookPath: inside.path)

        let renamed = try store.renameFolder(id: folder.id, to: "Jobs")

        XCTAssertEqual(renamed.path, "paper/Jobs")
        XCTAssertFalse(exists("paper/Work"))
        XCTAssertNotNil(try store.notebook(path: "paper/Jobs/Ideas.paper"))
        let queued = try store.journal.pending()
        XCTAssertFalse(queued.contains { $0.kind == .folderRename })
        XCTAssertTrue(queued.allSatisfy { $0.notebookPath.hasPrefix("paper/Jobs") }, "\(queued)")
        let asset = try XCTUnwrap(queued.first { $0.kind == .assetPut })
        XCTAssertTrue(asset.payload["path"]?.hasPrefix("paper/Jobs/Ideas.paper/assets/") ?? false)
        XCTAssertEqual(store.reindexedFolder("paper/Jobs/Ideas.paper"), "Jobs")
    }

    func testARenameOrAMoveOfAFolderTheServerHasIsJournalled() throws {
        let work = try store.createFolder(name: "Work", in: "paper")
        let archive = try store.createFolder(name: "Archive", in: "paper")
        let ideas = try notebook("Ideas", in: "Work")
        try drain()
        try store.writeDrawing(Data("more".utf8), notebookPath: ideas.path, pageID: ideas.pages[0])
        try store.journal.append(
            .init(kind: .pagePut, notebookPath: ideas.path, pageID: ideas.pages[0]))

        let renamed = try store.renameFolder(id: work.id, to: "Work 2026")
        let moved = try store.moveFolder(id: work.id, into: archive.path)

        XCTAssertEqual(moved.path, "paper/Archive/Work 2026")
        XCTAssertEqual(moved.name, "Work 2026")
        XCTAssertGreaterThan(moved.modified, work.modified)
        let queued = try store.journal.pending()
        XCTAssertEqual(queued.map(\.kind), [.pagePut, .folderRename, .folderRename])
        // The put still names the server's path; its files are found by id.
        XCTAssertEqual(queued[0].notebookPath, "paper/Work/Ideas.paper")
        XCTAssertEqual(queued[1].notebookPath, "paper/Work")
        XCTAssertEqual(queued[1].payload, ["new_path": renamed.path, "name": "Work 2026"])
        XCTAssertEqual(queued[2].notebookPath, renamed.path)
        XCTAssertEqual(queued[2].payload, ["new_path": moved.path], "a move sends no name")
        XCTAssertEqual(
            try store.notebook(id: ideas.id).path, "paper/Archive/Work 2026/Ideas.paper")
        XCTAssertEqual(try store.notebook(id: ideas.id).folder, "Archive/Work 2026")
    }

    func testARenameOntoASiblingIsSuffixedAndKeepsTheNameAsTyped() throws {
        _ = try store.createFolder(name: "Work", in: "paper")
        let jobs = try store.createFolder(name: "Jobs", in: "paper")
        let renamed = try store.renameFolder(id: jobs.id, to: "work")
        XCTAssertEqual(renamed.path, "paper/work 2")
        XCTAssertEqual(renamed.name, "work")
    }

    func testARenameThatOnlyChangesCaseGoesThroughATemporaryName() throws {
        let work = try store.createFolder(name: "Work", in: "paper")
        _ = try notebook("Ideas", in: "Work")
        try drain()

        let renamed = try store.renameFolder(id: work.id, to: "WORK")

        XCTAssertEqual(renamed.path, "paper/WORK")
        XCTAssertEqual(try children("paper"), ["WORK"])
        XCTAssertNotNil(try store.notebook(path: "paper/WORK/Ideas.paper"))
        XCTAssertEqual(
            try store.journal.pending().map(\.payload), [["new_path": "paper/WORK", "name": "WORK"]])
    }

    func testARetitleThatKeepsTheSlugStaysPut() throws {
        let work = try store.createFolder(name: "Work", in: "paper")
        try drain()
        let written = folderJSON(work.path)?["modified"] as? String
        let retitled = try store.renameFolder(id: work.id, to: "Work")
        XCTAssertEqual(retitled.path, work.path)
        XCTAssertEqual(folderJSON(work.path)?["modified"] as? String, written, "nothing written")
        XCTAssertEqual(try store.journal.pending(), [])
    }

    func testAFolderCannotMoveIntoItselfOrPastThePathLimit() throws {
        let work = try store.createFolder(name: "Work", in: "paper")
        let inner = try store.createFolder(name: "Inner", in: work.path)
        XCTAssertThrowsError(try store.moveFolder(id: work.id, into: work.path))
        XCTAssertThrowsError(try store.moveFolder(id: work.id, into: inner.path))

        // 809 bytes of parents and a 160-byte name: refused before anything
        // is made on disk.
        var deep = "paper"
        for index in 0..<4 { deep += "/" + String(repeating: "d\(index)", count: 100) }
        XCTAssertThrowsError(
            try store.createFolder(name: String(repeating: "\u{E9}", count: 80), in: deep)
        ) { error in
            XCTAssertEqual(error as? PaperStoreError, .unsafePath(deep + "/" + String(repeating: "\u{E9}", count: 80)))
        }
        XCTAssertFalse(exists(String(deep.prefix(205))), "nothing was made")
    }

    // MARK: - Appearance

    func testPatchesMergeByIDAndAChangeThatChangesNothingJournalsNothing() throws {
        let work = try store.createFolder(name: "Work", in: "paper")
        try drain()

        _ = try store.setFolderAppearance(id: work.id, color: .red, icon: nil)
        _ = try store.setFolderFavorite(id: work.id, true)
        let last = try store.setFolderAppearance(id: work.id, color: nil, icon: .some(.symbol("star")))
        _ = try store.setFolderAppearance(id: work.id, color: .red, icon: nil)
        _ = try store.setFolderFavorite(id: work.id, true)

        let queued = try store.journal.pending()
        XCTAssertEqual(queued.map(\.kind), [.folderPatch])
        XCTAssertEqual(queued.first?.payload, ["color": "red", "favorite": "true", "icon": "symbol:star"])
        XCTAssertEqual(last.icon, .symbol("star"))
        XCTAssertEqual(folderJSON(work.path)?["favorite"] as? Bool, true)

        _ = try store.setFolderAppearance(id: work.id, color: nil, icon: .some(nil))
        XCTAssertEqual(try store.journal.pending().first?.payload["icon"], "", "cleared")
        XCTAssertNil(try store.folder(id: work.id).icon)
    }

    func testContentsListFoldersAndDocumentsOfOneFolder() throws {
        let work = try store.createFolder(name: "Work", in: "paper")
        _ = try store.createFolder(name: "Accounting", in: work.path)
        _ = try notebook("Ideas", in: "Work")
        _ = try notebook("Top")

        let inside = try store.contents(ofFolder: work.path)
        XCTAssertEqual(inside.folders.map(\.name), ["Accounting"])
        XCTAssertEqual(inside.documents.map(\.title), ["Ideas"])
        let top = try store.contents(ofFolder: "paper")
        XCTAssertEqual(top.folders.map(\.name), ["Work"])
        XCTAssertEqual(top.documents.map(\.title), ["Top"])
    }

    // MARK: - Delete and undo

    func testADeleteDropsTheWorkItMakesMootAndKeepsTheMovesThatDecideWhatItTakes() throws {
        let work = try store.createFolder(name: "Work", in: "paper")
        let staying = try notebook("Staying", in: "Work")
        let leaving = try notebook("Leaving", in: "Work")
        let joining = try notebook("Joining")
        try drain()
        for book in [staying, leaving, joining] {
            try store.journal.append(
                .init(kind: .pagePut, notebookPath: book.path, pageID: book.pages[0]))
        }
        _ = try store.move(notebookID: leaving.id, toFolder: "")
        _ = try store.move(notebookID: joining.id, toFolder: "Work")
        let before = try store.journal.pending()

        let dropped = try store.journal.unpushed(underFolder: work.path)
        let token = try store.deleteFolder(id: work.id)

        XCTAssertEqual(token.kind, .folder)
        XCTAssertEqual(token.title, "Work")
        XCTAssertFalse(exists("paper/Work"))
        XCTAssertNil(try store.notebook(path: "paper/Work/Staying.paper"))
        XCTAssertNil(try? store.folder(id: work.id))
        // Staying's put and Joining's put -- the edits of what is going.
        XCTAssertEqual(
            dropped.map(\.notebookID).sorted(), [staying.id, joining.id].sorted())
        let queued = try store.journal.pending()
        XCTAssertEqual(
            queued.map(\.kind), [.pagePut, .notebookMove, .notebookMove, .folderDelete])
        XCTAssertEqual(queued.map(\.notebookID), [leaving.id, leaving.id, joining.id, work.id])
        XCTAssertEqual(queued.count + dropped.count, before.count + 1)
    }

    func testDeletingAFolderTheServerNeverSawJournalsNothing() throws {
        let work = try store.createFolder(name: "Work", in: "paper")
        _ = try notebook("Ideas", in: "Work")
        _ = try store.deleteFolder(id: work.id)
        XCTAssertEqual(try store.journal.pending(), [])
    }

    func testUndoBeforeTheDeleteLeftPutsEverythingBackAndQueuesItAgain() throws {
        let work = try store.createFolder(name: "Work", in: "paper")
        let inner = try store.createFolder(name: "Inner", in: work.path)
        let ideas = try notebook("Ideas", in: "Work/Inner")
        let before = try store.journal.pending().map { [$0.kind.rawValue, $0.notebookPath] }

        let token = try store.deleteFolder(id: work.id)
        XCTAssertNil(try store.restore(token))

        XCTAssertEqual(try store.folder(id: inner.id).path, "paper/Work/Inner")
        XCTAssertEqual(try store.notebook(id: ideas.id).path, ideas.path)
        XCTAssertEqual(
            try store.journal.pending().map { [$0.kind.rawValue, $0.notebookPath] }, before)
    }

    func testUndoAfterTheDeleteWentUpSendsTheFolderUpAsNew() throws {
        let work = try store.createFolder(name: "Work", in: "paper", color: .green)
        let ideas = try notebook("Ideas", in: "Work")
        try drain()
        let token = try store.deleteFolder(id: work.id)
        try drain()

        _ = try store.restore(token)

        let queued = try store.journal.pending()
        XCTAssertEqual(queued.first?.kind, .folderCreate)
        XCTAssertEqual(queued.first?.notebookID, work.id)
        XCTAssertEqual(queued.first?.payload["color"], "green")
        XCTAssertTrue(queued.contains { $0.kind == .notebookCreate && $0.notebookID == ideas.id })
        XCTAssertTrue(queued.contains { $0.createsPage && $0.pageID == ideas.pages[0] })
    }

    func testUndoBesideAFolderThatTookTheNameMeanwhile() throws {
        let work = try store.createFolder(name: "Work", in: "paper")
        let token = try store.deleteFolder(id: work.id)
        _ = try store.createFolder(name: "Work", in: "paper")
        _ = try store.restore(token)
        XCTAssertEqual(try store.folder(id: work.id).path, "paper/Work 2")
        // Never sent, so its create carries its name: one of its own, or it
        // would be merged into the folder that took its place.
        XCTAssertEqual(try store.folder(id: work.id).name, "Work 2")
    }

    // MARK: - The journal on its own

    func testRewritePrefixMovesPathsNewPathsAndAssetPathsFromTheCreateOn() throws {
        let journal = store.journal
        try journal.append(.init(kind: .notebookCreate, notebookPath: "paper/Work/Old.paper"))
        let create = try XCTUnwrap(
            try journal.append(.init(kind: .folderCreate, notebookPath: "paper/Work", notebookID: "f1")))
        try journal.append(.init(kind: .notebookCreate, notebookPath: "paper/Work/Ideas.paper"))
        try journal.append(
            .init(
                kind: .notebookMove, notebookPath: "paper/Loose.paper",
                payload: ["new_path": "paper/Work/Loose.paper"]))
        try journal.append(
            .init(
                kind: .assetPut, notebookPath: "paper/Work/Ideas.paper",
                payload: ["path": "paper/Work/Ideas.paper/assets/a.png"]))
        try journal.append(.init(kind: .notebookCreate, notebookPath: "paper/Workshop.paper"))

        XCTAssertEqual(try journal.rewritePrefix(from: "paper/Work", to: "paper/Jobs", sinceOpID: create), 4)

        let queued = try journal.pending()
        XCTAssertEqual(
            queued.map(\.notebookPath),
            ["paper/Work/Old.paper", "paper/Jobs", "paper/Jobs/Ideas.paper", "paper/Loose.paper",
             "paper/Jobs/Ideas.paper", "paper/Workshop.paper"])
        XCTAssertEqual(queued[3].payload["new_path"], "paper/Jobs/Loose.paper")
        XCTAssertEqual(queued[4].payload["path"], "paper/Jobs/Ideas.paper/assets/a.png")
    }

    func testFollowingAMoveStopsAtTheFoldersOwnRenameAndFollowsItsParents() throws {
        let journal = store.journal
        // Queued here: ink in the folder, the parent renamed, more ink at the
        // parent's new name, the folder's own rename, and a new folder at the
        // path that rename freed.
        try journal.append(.init(kind: .pagePut, notebookPath: "paper/P/Sub/N.paper", pageID: "a"))
        try journal.append(
            .init(
                kind: .folderRename, notebookPath: "paper/P", notebookID: "parent",
                payload: ["new_path": "paper/Q"]))
        try journal.append(.init(kind: .pagePut, notebookPath: "paper/Q/Sub/N.paper", pageID: "b"))
        try journal.append(
            .init(
                kind: .folderRename, notebookPath: "paper/Q/Sub", notebookID: "sub",
                payload: ["new_path": "paper/Q/Mine", "name": "Mine"]))
        try journal.append(
            .init(kind: .folderCreate, notebookPath: "paper/Q/Sub", notebookID: "fresh"))
        let parked = try XCTUnwrap(try journal.pending().first?.id)
        try journal.park(parked, reason: "404: Notebook not found")

        // Another device moved Sub out of P before any of that reached it.
        try journal.followFolderMove(from: "paper/P/Sub", to: "paper/Elsewhere")

        let queue = try journal.queue()
        XCTAssertEqual(
            queue.map(\.notebookPath),
            ["paper/Elsewhere/N.paper", "paper/P", "paper/Elsewhere/N.paper", "paper/Elsewhere",
             "paper/Q/Sub"])
        XCTAssertEqual(queue[3].payload["new_path"], "paper/Q/Mine", "the later push wins")
        XCTAssertNil(queue[0].parkedReason, "a re-pointed op is a new request")
    }

    func testAnOpOfAKindThisBuildCannotReadIsParkedNotSentAsAPageSave() throws {
        try store.database.queue.write { db in
            try db.execute(
                sql: """
                    INSERT INTO ops (kind, notebook_path, notebook_id, page_id, payload, created_at, pushed)
                    VALUES ('boardSplit', 'paper/Ideas.paper', '', 'p1', '{}', 0, 0)
                    """)
        }
        try store.journal.append(.init(kind: .notebookCreate, notebookPath: "paper/Other.paper"))

        XCTAssertEqual(try store.journal.pending().map(\.kind), [.notebookCreate])
        XCTAssertEqual(try store.journal.pendingCount(), 1)
        let parked = try store.journal.parked()
        XCTAssertEqual(parked.map(\.kind), [.unknown])
        XCTAssertEqual(parked.first?.parkedReason, "unknown operation")
        // "Try again" does not make it sendable.
        try store.journal.unparkAll()
        XCTAssertEqual(try store.journal.pending().map(\.kind), [.notebookCreate])
        XCTAssertNil(try store.journal.append(.init(kind: .unknown, notebookPath: "paper/X.paper")))
        // And a trash manifest holding one still opens.
        let decoded = try JSONDecoder().decode(
            PaperOp.Kind.self, from: Data(#""boardSplit""#.utf8))
        XCTAssertEqual(decoded, .unknown)
    }

    // MARK: - Reconciler and migration

    /// The owner's iPad: notebooks in nested folders with no records, ops
    /// queued for them, a directory an earlier move left empty, all under a
    /// database from before folders.
    func testTheMigrationAndTheReconcilerKeepAContainerThatHoldsWork() throws {
        let olderRoot = root.appending(path: "older", directoryHint: .isDirectory)
        let paths = PaperPaths(root: olderRoot)
        do {
            let older = try PaperStore(root: olderRoot)
            _ = try older.createNotebook(title: "Ideas", folder: "Work/Accounting")
            let moved = try older.createNotebook(title: "Moved", folder: "Old")
            _ = try older.move(notebookID: moved.id, toFolder: "")
        }
        // Back to the schema a build before folders left behind.
        let queue = try DatabaseQueue(path: paths.database.path(percentEncoded: false))
        try queue.write { db in
            try db.execute(sql: "DROP TABLE folders")
            try db.execute(sql: "DELETE FROM grdb_migrations WHERE identifier = 'v5-folders'")
        }
        let queuedBefore = try queue.read { db in
            try Row.fetchAll(db, sql: "SELECT kind, notebook_path FROM ops ORDER BY id")
        }.map { "\($0["kind"] as String) \($0["notebook_path"] as String)" }
        XCTAssertFalse(queuedBefore.isEmpty)

        let upgraded = try PaperStore(root: olderRoot)
        XCTAssertEqual(try upgraded.folderRecords(), [], "the migration writes no folder")
        try upgraded.reconcile()

        let records = try upgraded.folderRecords()
        XCTAssertEqual(records.map(\.path), ["paper/Old", "paper/Work", "paper/Work/Accounting"])
        XCTAssertEqual(
            records.map(\.id),
            [PaperFolderID.legacy(path: "paper/Old"), "9e7201dacb87ef0d43fb22798ccf4af6",
             "3dfc9f48645d394cc5a07b793b247654"])
        XCTAssertEqual(records.map(\.name), ["Old", "Work", "Accounting"])
        XCTAssertEqual(records.map(\.parentPath), ["paper", "paper", "paper/Work"])
        let queuedAfter = try upgraded.journal.queue().map {
            "\($0.kind.rawValue) \($0.notebookPath)"
        }
        XCTAssertEqual(queuedAfter, queuedBefore, "the journal is untouched")
        XCTAssertEqual(try upgraded.contents(ofFolder: "paper/Old").documents, [])

        // A second launch changes nothing.
        let ids = records.map(\.id)
        try upgraded.reconcile()
        XCTAssertEqual(try upgraded.folderRecords().map(\.id), ids)
    }

    func testTheReconcilerSetsACorruptRecordAsideAndReIdsACopy() throws {
        let work = try store.createFolder(name: "Work", in: "paper", color: .red)
        let manager = FileManager.default
        try manager.copyItem(
            at: store.paths.url(forVaultPath: "paper/Work"),
            to: store.paths.url(forVaultPath: "paper/Work copy"))
        let broken = store.paths.url(forVaultPath: "paper/Broken")
        try manager.createDirectory(at: broken, withIntermediateDirectories: true)
        try Data("{not json".utf8).write(to: broken.appending(path: "folder.json"))
        let partial = store.paths.url(forVaultPath: "paper/Taxes")
        try manager.createDirectory(at: partial, withIntermediateDirectories: true)
        try Data(#"{"name":"Taxes 2026","color":"green","shelf":3}"#.utf8)
            .write(to: partial.appending(path: "folder.json"))

        try store.reconcile()

        XCTAssertEqual(try store.folder(path: "paper/Work")?.id, work.id, "the earlier path keeps it")
        XCTAssertEqual(
            try store.folder(path: "paper/Work copy")?.id, PaperFolderID.legacy(path: "paper/Work copy"))
        XCTAssertEqual(try store.folder(path: "paper/Work copy")?.color, .red)
        XCTAssertEqual(try store.folder(path: "paper/Broken")?.id, PaperFolderID.legacy(path: "paper/Broken"))
        XCTAssertTrue(
            try children("paper/Broken").contains { $0.hasPrefix("folder.json.corrupt-") })
        let taxes = try XCTUnwrap(try store.folder(path: "paper/Taxes"))
        XCTAssertEqual(taxes.id, PaperFolderID.legacy(path: "paper/Taxes"))
        XCTAssertEqual(taxes.name, "Taxes 2026")
        XCTAssertEqual(taxes.color, .green)
        XCTAssertEqual(folderJSON("paper/Taxes")?["shelf"] as? Int, 3, "unknown keys are kept")
    }

    func testTheReconcilerDropsRowsOfGoneDirectoriesAndFinishesAnInterruptedRename() throws {
        let gone = try store.createFolder(name: "Gone", in: "paper")
        let work = try store.createFolder(name: "Work", in: "paper")
        _ = try notebook("Ideas", in: "Work")
        let manager = FileManager.default
        try manager.removeItem(at: store.paths.url(forVaultPath: gone.path))
        // A case-only rename that stopped between its two moves.
        try manager.moveItem(
            at: store.paths.url(forVaultPath: "paper/Work"),
            to: store.paths.url(forVaultPath: "paper/.rename-\(work.id)"))

        try store.reconcile()

        XCTAssertNil(try store.folder(path: gone.path))
        XCTAssertEqual(try store.folder(id: work.id).path, "paper/Work")
        XCTAssertNotNil(try store.notebook(path: "paper/Work/Ideas.paper"))
    }
}

extension PaperStore {
    /// The `folder` column of the notebook at `path`, as the index holds it.
    fileprivate func reindexedFolder(_ path: String) -> String? {
        try? database.queue.read { db in
            try String.fetchOne(db, sql: "SELECT folder FROM notebooks WHERE path = ?", arguments: [path])
        }
    }
}
