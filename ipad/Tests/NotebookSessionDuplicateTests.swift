import PaperCore
import PencilKit
import XCTest

@testable import PaperUI

/// Duplicate Page copies what the page shows, the last strokes included.
@MainActor
final class NotebookSessionDuplicateTests: XCTestCase {
    /// A stroke drawn within the last second is still only in the canvas,
    /// waiting for autosave; the store copies the page from disk. It used to
    /// copy it without that stroke.
    func testADuplicateHoldsTheStrokesNotYetSaved() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("NotebookSessionDuplicateTests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        let store = try PaperStore(root: root)
        let controller = PaperController(store: store)
        let notebook = try store.createNotebook(title: "Copy", template: .ruled, size: .a4, pageCount: 1)
        let pageID = notebook.pages[0]
        let session = NotebookSession(controller: controller, notebook: notebook)
        let points = stride(from: 0.0, through: 200.0, by: 10.0).map {
            PKStrokePoint(
                location: CGPoint(x: 80 + $0, y: 300), timeOffset: $0 / 200,
                size: CGSize(width: 4, height: 4), opacity: 1, force: 1, azimuth: 0, altitude: .pi / 2)
        }
        let ink = PKDrawing(strokes: [
            PKStroke(ink: PKInk(.pen, color: .black), path: PKStrokePath(controlPoints: points, creationDate: Date()))
        ]).dataRepresentation()
        let canvas = controller.addDrawingProvider { $0.pageID == pageID ? ink : nil }
        controller.markPageDirty(
            PageRef(notebookID: notebook.id, notebookPath: notebook.path, pageID: pageID), source: canvas)

        session.duplicatePage(at: 0)

        let copyID = try XCTUnwrap(session.pages.first { $0.id != pageID }?.id)
        let copied = try PKDrawing(
            data: XCTUnwrap(store.drawingData(notebookPath: notebook.path, pageID: copyID)))
        XCTAssertEqual(copied.strokes.count, 1, "the copy lost the stroke still in the canvas")
    }
}
