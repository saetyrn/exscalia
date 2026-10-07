import PaperCore
import PencilKit
import XCTest

@testable import PaperUI

/// A full-screen presentation (the document scanner, the camera) takes the
/// notebook off screen and hands the same controller back. Ink written after
/// that has to reach the disk; for a while it did not, and the pill said it
/// had.
@MainActor
final class NotebookViewControllerLifecycleTests: XCTestCase {
    /// One notebook on disk, open in a window.
    private struct Fixture {
        let store: PaperStore
        let controller: PaperController
        let notebook: Notebook
        let notebookController: NotebookViewController
    }

    func testInkWrittenAfterAFullScreenPresentationReachesTheDisk() throws {
        let fixture = try makeFixture()

        // What presenting and dismissing the scanner does to the canvas.
        fixture.notebookController.beginAppearanceTransition(false, animated: false)
        fixture.notebookController.endAppearanceTransition()
        fixture.notebookController.beginAppearanceTransition(true, animated: false)
        fixture.notebookController.endAppearanceTransition()

        try assertInkReachesTheDisk(fixture)
    }

    func testInkWrittenWithoutAPresentationReachesTheDisk() throws {
        try assertInkReachesTheDisk(try makeFixture())
    }

    // MARK: - Helpers

    private func makeFixture() throws -> Fixture {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("NotebookViewControllerLifecycleTests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }

        let store = try PaperStore(root: root)
        let controller = PaperController(store: store)
        let notebook = try store.createNotebook(title: "Lifecycle")
        let session = NotebookSession(controller: controller, notebook: notebook)
        let notebookController = NotebookViewController(session: session)

        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 1024, height: 1366))
        window.rootViewController = notebookController
        window.makeKeyAndVisible()
        addTeardownBlock { @MainActor in
            window.isHidden = true
            window.rootViewController = nil
        }
        notebookController.loadViewIfNeeded()
        notebookController.view.frame = window.bounds
        // Layout is what gives the visible page its canvas.
        notebookController.view.layoutIfNeeded()
        notebookController.beginAppearanceTransition(true, animated: false)
        notebookController.endAppearanceTransition()

        let page = try XCTUnwrap(notebookController.pageViews.first)
        XCTAssertNotNil(page.canvas, "the first page should be live after layout")
        return Fixture(
            store: store, controller: controller, notebook: notebook,
            notebookController: notebookController)
    }

    /// Writes one stroke the way a Pencil edit reports it, flushes, and reads
    /// the page back from disk.
    private func assertInkReachesTheDisk(
        _ fixture: Fixture, file: StaticString = #filePath, line: UInt = #line
    ) throws {
        let page = try XCTUnwrap(fixture.notebookController.pageViews.first, file: file, line: line)
        let canvas = try XCTUnwrap(page.canvas, file: file, line: line)
        canvas.drawing = PKDrawing(strokes: [Self.stroke()])
        // Driven directly: the simulator has no Pencil, and this is the path
        // PencilKit's delegate takes for a user edit.
        fixture.notebookController.pageViewDrawingChanged(page)

        fixture.controller.autosave.flushAll()

        let pageID = try XCTUnwrap(fixture.notebook.pages.first, file: file, line: line)
        let onDisk = fixture.store.drawingData(
            notebookPath: fixture.notebook.path, pageID: pageID)
        XCTAssertNotNil(onDisk, "the stroke never reached the disk", file: file, line: line)
        XCTAssertEqual(onDisk, canvas.drawing.dataRepresentation(), file: file, line: line)
        XCTAssertFalse(fixture.controller.autosave.hasUnsavedChanges, file: file, line: line)
    }

    private static func stroke() -> PKStroke {
        let points = stride(from: 0.0, through: 320.0, by: 8.0).map { offset -> PKStrokePoint in
            let wave = sin(offset / 40) * 40
            return PKStrokePoint(
                location: CGPoint(x: 90 + offset, y: 240 + wave),
                timeOffset: offset / 320, size: CGSize(width: 5, height: 5), opacity: 1,
                force: 1, azimuth: 0, altitude: .pi / 2)
        }
        return PKStroke(
            ink: PKInk(.pen, color: UIColor(red: 0x1c / 255, green: 0x1c / 255, blue: 0x1e / 255, alpha: 1)),
            path: PKStrokePath(controlPoints: points, creationDate: Date()))
    }
}
