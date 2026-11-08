import Foundation
import XCTest

@testable import PaperCore

/// Where a notebook made, renamed or moved by folder name goes: into the
/// folder as its directory is spelled on disk, never a new twin of it, and
/// with a record for every folder it makes on the way.
final class NotebookFolderTests: XCTestCase {
    private var root: URL!
    private var store: PaperStore!

    override func setUpWithError() throws {
        try super.setUpWithError()
        root = FileManager.default.temporaryDirectory
            .appending(path: "NotebookFolderTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        store = try PaperStore(root: root)
    }

    override func tearDownWithError() throws {
        store = nil
        try? FileManager.default.removeItem(at: root)
        try super.tearDownWithError()
    }

    /// (D3) The legacy create -- what a PDF imported into a folder uses --
    /// made the folders on its way with no record: they were missing from
    /// the library's folder list until a relaunch's reconcile.
    func testTheLegacyCreateGivesTheFoldersItMakesTheirRecords() throws {
        let notebook = try store.createNotebook(title: "Imported", folder: "Work/Taxes")

        XCTAssertEqual(notebook.path, "paper/Work/Taxes/Imported.paper")
        XCTAssertEqual(try store.folder(path: "paper/Work")?.id, PaperFolderID.legacy(path: "paper/Work"))
        XCTAssertEqual(
            try store.folder(path: "paper/Work/Taxes")?.id, PaperFolderID.legacy(path: "paper/Work/Taxes"))
        XCTAssertEqual(try store.journal.queue().filter { $0.kind.isFolderOp }, [], "the server makes the same")
    }

    /// (D4) A folder whose directory is not the slug of its name -- made by
    /// another client, or before the slug rules -- is moved into as it is
    /// spelled, in any case, not slugged again into a twin beside it.
    func testAMoveGoesIntoTheFolderAsItsDirectoryIsSpelled() throws {
        try FileManager.default.createDirectory(
            at: store.paths.url(forVaultPath: "paper/Notes  2024"), withIntermediateDirectories: true)
        _ = try store.reconcile()
        let notebook = try store.createNotebook(title: "Lecture")

        let moved = try store.move(notebookID: notebook.id, toFolder: "Notes  2024")
        XCTAssertEqual(moved.path, "paper/Notes  2024/Lecture.paper")
        let renamed = try store.rename(notebookID: notebook.id, to: "Lecture 1")
        XCTAssertEqual(renamed.path, "paper/Notes  2024/Lecture 1.paper")
        let again = try store.createNotebook(title: "Other")
        XCTAssertEqual(
            try store.move(notebookID: again.id, toFolder: "notes  2024").path,
            "paper/Notes  2024/Other.paper")
        XCTAssertEqual(try store.folderRecords().map(\.path), ["paper/Notes  2024"], "a twin folder appeared")
    }

    /// A folder that is not there yet is made with its directory name and a
    /// record; one ending in `.paper` does not become a package.
    func testAMoveIntoANewFolderMakesItWithARecord() throws {
        let notebook = try store.createNotebook(title: "Lecture")
        let moved = try store.move(notebookID: notebook.id, toFolder: "Course.paper")
        XCTAssertEqual(moved.path, "paper/Course.paper_/Lecture.paper")
        XCTAssertNotNil(try store.folder(path: "paper/Course.paper_"))
    }
}
