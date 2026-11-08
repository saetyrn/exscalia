import Foundation
import PencilKit
import XCTest

@testable import PaperCore

/// The page operations the editor's sidebar offers: Duplicate and Clear Page.
final class PageOperationTests: PaperStoreTestCase {
    private func stroke(at x: CGFloat) -> PKStroke {
        let points = (0..<8).map { index in
            PKStrokePoint(
                location: CGPoint(x: x + CGFloat(index) * 6, y: 120), timeOffset: Double(index) * 0.01,
                size: CGSize(width: 3, height: 3), opacity: 1, force: 1, azimuth: 0,
                altitude: .pi / 2)
        }
        return PKStroke(
            ink: PKInk(.pen, color: .black),
            path: PKStrokePath(controlPoints: points, creationDate: Date()))
    }

    private func ink(_ notebook: Notebook, _ pageID: String) throws -> PKDrawing {
        let data = try XCTUnwrap(store.drawingData(notebookPath: notebook.path, pageID: pageID))
        return try PKDrawing(data: data)
    }

    func testDuplicatingAPageCopiesItsContentRightAfterIt() throws {
        let notebook = try store.createNotebook(title: "Copies")
        let first = try XCTUnwrap(notebook.pages.first)
        let last = try store.createPage(notebookID: notebook.id).id
        let drawing = PKDrawing(strokes: [stroke(at: 40), stroke(at: 140)])
        try store.writeDrawing(drawing.dataRepresentation(), notebookPath: notebook.path, pageID: first)
        let text = PageObject(
            type: .text, frame: PageObject.Frame(x: 10, y: 10, width: 120, height: 40), text: "hi")
        try store.setObjects([text], notebookPath: notebook.path, pageID: first)
        _ = try store.setPageStyle(
            notebookID: notebook.id, pageID: first, template: .grid, paperColor: .cream)
        _ = try store.setPageTitle(notebookID: notebook.id, pageID: first, title: "Plan")
        _ = try store.setPageBookmarked(notebookID: notebook.id, pageID: first, true)
        try Data("png".utf8).write(to: store.paths.render(notebook.path, first))
        let journalled = try store.journal.pending().count

        let copy = try store.duplicatePage(notebookID: notebook.id, pageID: first)

        XCTAssertEqual(try store.notebook(id: notebook.id).pages, [first, copy.id, last])
        XCTAssertNotEqual(copy.id, first)
        XCTAssertEqual(copy.title, "Plan copy")
        XCTAssertFalse(copy.bookmarked)
        XCTAssertEqual(copy.revision, 0)
        XCTAssertEqual(copy.template, .grid)
        XCTAssertEqual(copy.paperColor, .cream)
        XCTAssertEqual(copy.objects, [text])
        XCTAssertEqual(try ink(notebook, copy.id).strokes.count, 2)
        XCTAssertTrue(exists(store.paths.render(notebook.path, copy.id)))
        // The original is untouched.
        XCTAssertEqual(try ink(notebook, first).strokes.count, 2)
        XCTAssertEqual(try store.page(notebookPath: notebook.path, id: first).title, "Plan")
        // One create-PUT, which carries the whole page.
        let ops = try store.journal.pending()
        XCTAssertEqual(ops.count, journalled + 1)
        XCTAssertEqual(ops.last?.pageID, copy.id)
        XCTAssertEqual(ops.last?.createsPage, true)
    }

    func testDuplicatingABoardKeepsItsExtentAndNamesTheCopy() throws {
        let board = try store.createDocument(
            title: "Board", folderPath: "paper", kind: .whiteboard,
            options: .whiteboardFactory)
        let first = try XCTUnwrap(board.pages.first)
        let copy = try store.duplicatePage(notebookID: board.id, pageID: first)
        let original = try store.page(notebookPath: board.path, id: first)
        XCTAssertEqual(copy.size, .board)
        XCTAssertEqual(copy.boardExtent, original.boardExtent)
        XCTAssertEqual(copy.title, "Board 1 copy")
    }

    func testDuplicatingAPageThatIsNotThereThrows() throws {
        let notebook = try store.createNotebook(title: "Missing")
        XCTAssertThrowsError(try store.duplicatePage(notebookID: notebook.id, pageID: "nope"))
    }

    func testClearingAPageKeepsObjectsAndSnapshotsTheInk() throws {
        let notebook = try store.createNotebook(title: "Clear")
        let page = try XCTUnwrap(notebook.pages.first)
        let before = PKDrawing(strokes: [stroke(at: 40)]).dataRepresentation()
        try store.writeDrawing(before, notebookPath: notebook.path, pageID: page)
        let text = PageObject(
            type: .text, frame: PageObject.Frame(x: 10, y: 10, width: 120, height: 40), text: "keep")
        try store.setObjects([text], notebookPath: notebook.path, pageID: page)
        let journalled = try store.journal.pending().count

        try store.clearPage(notebookID: notebook.id, pageID: page)

        XCTAssertTrue(try ink(notebook, page).strokes.isEmpty)
        XCTAssertEqual(try store.page(notebookPath: notebook.path, id: page).objects, [text])
        let snapshots = try SnapshotRing(paths: store.paths, database: store.database)
            .snapshots(pageID: page)
        XCTAssertEqual(snapshots.count, 1)
        XCTAssertEqual(try Data(contentsOf: try XCTUnwrap(snapshots.first).url), before)
        XCTAssertEqual(try store.journal.pending().count, journalled + 1)
        XCTAssertEqual(try store.journal.pending().last?.kind, .pagePut)
    }

    func testClearingAnEmptyPageWritesNothing() throws {
        let notebook = try store.createNotebook(title: "Empty")
        let page = try XCTUnwrap(notebook.pages.first)
        let journalled = try store.journal.pending().count
        try store.clearPage(notebookID: notebook.id, pageID: page)
        try store.writeDrawing(PKDrawing().dataRepresentation(), notebookPath: notebook.path, pageID: page)
        try store.clearPage(notebookID: notebook.id, pageID: page)
        XCTAssertEqual(try store.journal.pending().count, journalled)
        XCTAssertTrue(
            try SnapshotRing(paths: store.paths, database: store.database).snapshots(pageID: page)
                .isEmpty)
    }
}
