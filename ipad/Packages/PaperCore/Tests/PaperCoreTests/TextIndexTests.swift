import Foundation
import GRDB
import XCTest

@testable import PaperCore

/// The device's handwriting index: what it matches, and how it groups.
final class TextIndexTests: XCTestCase {
    private var root: URL!
    private var store: PaperStore!
    private var index: TextIndex!

    override func setUpWithError() throws {
        root = URL(fileURLWithPath: NSTemporaryDirectory())
            .appending(path: "paper-text-index-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        store = try PaperStore(root: root)
        index = store.textIndex
    }

    override func tearDownWithError() throws {
        if let root { try? FileManager.default.removeItem(at: root) }
    }

    // MARK: - Fixtures

    @discardableResult
    private func seed(
        notebookTitle: String, pages: [[String]]
    ) throws -> Notebook {
        var notebook = try store.createNotebook(
            title: notebookTitle, template: .ruled, size: .a4, pageCount: pages.count)
        notebook = try store.notebook(id: notebook.id)
        let stored = try store.pages(ofNotebook: notebook.id)
        for (offset, lines) in pages.enumerated() where stored.indices.contains(offset) {
            let text = PageText(
                lines: lines.enumerated().map { position, line in
                    RecognizedLine(
                        text: line,
                        box: PageObject.Frame(
                            x: 60, y: 100 + Double(position) * 30, width: 200, height: 24),
                        confidence: 0.9)
                })
            try store.writePageText(
                text, notebookPath: notebook.path, pageID: stored[offset].id)
            try index.upsert(
                notebookPath: notebook.path, pageID: stored[offset].id, text: text,
                forRevision: 1)
        }
        return notebook
    }

    // MARK: - Matching

    func testTrigramMatchFindsAFragmentInsideAWord() throws {
        try seed(notebookTitle: "Work", pages: [["Quarterly review of the roadmap"]])

        // Not a word: a run of characters in the middle of one. A word-boundary
        // index would miss this, which is why the tokeniser is trigram.
        let groups = index.search("arterl")
        XCTAssertEqual(groups.count, 1)
        XCTAssertEqual(groups.first?.pages.first?.line, "Quarterly review of the roadmap")
    }

    func testPrefixQueryMatches() throws {
        try seed(notebookTitle: "Work", pages: [["Quarterly review of the roadmap"]])

        XCTAssertEqual(index.search("quar").first?.pages.count, 1)
        XCTAssertEqual(index.search("Quarterly rev").first?.pages.count, 1)
        XCTAssertTrue(index.search("zzz").isEmpty)
    }

    func testVeryShortQueriesStillMatch() throws {
        try seed(notebookTitle: "Work", pages: [["Quarterly review"]])

        // Under three characters there is no trigram to match; the fallback
        // scan is what stops a two-letter query answering "nothing here".
        XCTAssertEqual(index.search("Qu").first?.pages.count, 1)
    }

    func testResultsAreGroupedByNotebookAndCarryPageNumbers() throws {
        let work = try seed(
            notebookTitle: "Work",
            pages: [["Nothing to see"], ["Quarterly review"], ["Quarterly targets"]])
        try seed(notebookTitle: "Ideas", pages: [["Quarterly poetry"]])

        let groups = index.search("Quarterly")
        XCTAssertEqual(Set(groups.map(\.notebookTitle)), ["Work", "Ideas"])

        let workGroup = try XCTUnwrap(groups.first { $0.notebookPath == work.path })
        XCTAssertEqual(workGroup.pages.map(\.pageNumber), [2, 3])
        XCTAssertEqual(workGroup.pages.first?.line, "Quarterly review")
    }

    func testOnlyMatchingLinesContributeBoxes() throws {
        try seed(
            notebookTitle: "Work",
            pages: [["A line that does not match", "Quarterly review", "Another miss"]])

        let match = try XCTUnwrap(index.search("Quarterly").first?.pages.first)
        XCTAssertEqual(match.boxes.count, 1)
        XCTAssertEqual(match.line, "Quarterly review")
        // Second line of the fixture: y = 100 + 30.
        XCTAssertEqual(match.unionBox.minY, 130, accuracy: 0.001)
    }

    func testNotebookTitlesMatchEvenWithNoTranscript() throws {
        try seed(notebookTitle: "Quarterly planning", pages: [["Nothing relevant"]])

        let groups = index.search("Quarterly")
        XCTAssertEqual(groups.count, 1)
        XCTAssertEqual(groups.first?.notebookTitle, "Quarterly planning")
        XCTAssertTrue(groups.first?.pages.isEmpty ?? false)
    }

    func testRemovingAPageClearsBothTables() throws {
        let notebook = try seed(notebookTitle: "Work", pages: [["Quarterly review"]])
        let page = try XCTUnwrap(try store.pages(ofNotebook: notebook.id).first)

        try index.remove(pageID: page.id)
        XCTAssertTrue(index.search("Quarterly").isEmpty)
        XCTAssertNil(index.text(pageID: page.id))
        XCTAssertEqual(index.indexedPageCount(), 0)
    }

    /// The tree is the truth: a reconcile rebuilds the index from the
    /// `text.json` files, so a restored backup is searchable without Vision.
    func testReconcileRebuildsTheIndexFromTextJSON() throws {
        let notebook = try seed(notebookTitle: "Work", pages: [["Quarterly review"]])
        let page = try XCTUnwrap(try store.pages(ofNotebook: notebook.id).first)
        try index.remove(pageID: page.id)
        XCTAssertTrue(index.search("Quarterly").isEmpty)

        try store.reconcile()

        XCTAssertEqual(index.search("Quarterly").first?.pages.first?.pageID, page.id)
    }

    // MARK: - Following the notebook

    private func storedPath(of pageID: String) throws -> String? {
        try store.database.queue.read { db in
            try String.fetchOne(
                db, sql: "SELECT notebook_path FROM page_text WHERE page_id = ?",
                arguments: [pageID])
        }
    }

    private func ftsRows() throws -> Int {
        try store.database.queue.read { db in
            try Int.fetchOne(db, sql: "SELECT COUNT(*) FROM page_fts") ?? 0
        }
    }

    func testARenamedNotebookIsFoundUnderItsNewPathAndTitle() throws {
        let notebook = try seed(notebookTitle: "Work", pages: [["Quarterly review"]])
        let renamed = try store.rename(notebookID: notebook.id, to: "Planning")
        XCTAssertNotEqual(renamed.path, notebook.path)

        let group = try XCTUnwrap(index.search("Quarterly").first)
        XCTAssertEqual(group.notebookPath, renamed.path)
        XCTAssertEqual(group.pages.first?.notebookPath, renamed.path)
        XCTAssertEqual(group.notebookTitle, "Planning")
        XCTAssertEqual(group.notebookTitle, PaperStore.title(ofNotebookPath: group.notebookPath))
        // The move kept text.json's date, so a reconcile skips the row: it
        // has to have learned the path at the move.
        try store.reconcile()
        XCTAssertEqual(try storedPath(of: try XCTUnwrap(group.pages.first?.pageID)), renamed.path)
        XCTAssertEqual(index.search("Quarterly").first?.notebookPath, renamed.path)
    }

    func testANotebookMovedToAFolderIsFoundThere() throws {
        let notebook = try seed(notebookTitle: "Work", pages: [["Quarterly review"]])
        let moved = try store.move(notebookID: notebook.id, toFolder: "Archive")
        XCTAssertNotEqual(moved.path, notebook.path)

        let match = try XCTUnwrap(index.search("Quarterly").first?.pages.first)
        XCTAssertEqual(match.notebookPath, moved.path)
        XCTAssertEqual(try storedPath(of: match.pageID), moved.path)
    }

    /// Whatever path the row copied, the hit opens the notebook that holds
    /// the page now.
    func testAStaleStoredPathStillResolvesToTheLiveNotebook() throws {
        let notebook = try seed(notebookTitle: "Work", pages: [["Quarterly review"]])
        try store.database.queue.write { db in
            try db.execute(sql: "UPDATE page_text SET notebook_path = 'paper/Gone.paper'")
        }

        let group = try XCTUnwrap(index.search("Quarterly").first)
        XCTAssertEqual(group.notebookPath, notebook.path)
        XCTAssertEqual(group.notebookTitle, "Work")
        XCTAssertEqual(index.search("Qu").first?.notebookPath, notebook.path)
    }

    func testADeletedPageNoLongerMatchesAndComesBackWithUndo() throws {
        let notebook = try seed(
            notebookTitle: "Work", pages: [["Quarterly review"], ["Nothing to see"]])
        let page = try XCTUnwrap(try store.pages(ofNotebook: notebook.id).first)

        let token = try store.deletePage(notebookID: notebook.id, pageID: page.id)
        XCTAssertTrue(index.search("Quarterly").isEmpty)
        XCTAssertTrue(index.search("Qu").isEmpty)
        XCTAssertNil(index.text(pageID: page.id))

        try store.restore(token)
        XCTAssertEqual(index.search("Quarterly").first?.pages.first?.pageID, page.id)
    }

    func testADeletedNotebookLeavesNothingBehind() throws {
        let work = try seed(
            notebookTitle: "Work", pages: [["Quarterly review"], ["Quarterly targets"]])
        try seed(notebookTitle: "Ideas", pages: [["Quarterly poetry"]])
        XCTAssertEqual(index.indexedPageCount(), 3)

        let token = try store.delete(notebookID: work.id)
        XCTAssertEqual(index.search("Quarterly").map(\.notebookTitle), ["Ideas"])
        XCTAssertEqual(index.indexedPageCount(), 1)
        XCTAssertEqual(try ftsRows(), 1)

        try store.restore(token)
        XCTAssertEqual(index.indexedPageCount(), 3)
        XCTAssertEqual(Set(index.search("Quarterly").map(\.notebookTitle)), ["Work", "Ideas"])
    }

    /// Text of a page that is gone -- removed by a pull, lost to a conflict,
    /// or left by an older build -- is swept on the next launch.
    func testReconcileDropsTextOfPagesThatAreGone() throws {
        try seed(notebookTitle: "Work", pages: [["Quarterly review"]])
        try index.upsert(
            notebookPath: "paper/Gone.paper", pageID: PaperID.make(),
            text: PageText(lines: [], transcript: "Quarterly ghost"), forRevision: 1)
        XCTAssertEqual(index.indexedPageCount(), 2)

        try store.reconcile()
        XCTAssertEqual(index.indexedPageCount(), 1)
        XCTAssertEqual(try ftsRows(), 1)
    }

    func testReconcileDropsTextWhoseFileIsGone() throws {
        let notebook = try seed(notebookTitle: "Work", pages: [["Quarterly review"]])
        let page = try XCTUnwrap(try store.pages(ofNotebook: notebook.id).first)
        try FileManager.default.removeItem(at: store.paths.text(notebook.path, page.id))

        try store.reconcile()
        XCTAssertEqual(index.indexedPageCount(), 0)
        XCTAssertEqual(try ftsRows(), 0)
    }

    func testQuotesAndOperatorsAreTextRatherThanSyntax() throws {
        try seed(notebookTitle: "Work", pages: [["Quarterly AND annual \"targets\""]])

        // `AND` is an FTS5 operator and `"` ends a phrase. Both have to arrive
        // as characters, or a stray quote is a crash rather than a miss.
        XCTAssertEqual(index.search("AND annual").first?.pages.count, 1)
        XCTAssertNoThrow(index.search("\"targets\""))
    }
}
