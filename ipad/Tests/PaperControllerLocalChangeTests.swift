import PaperCore
import PencilKit
import XCTest

@testable import PaperUI

/// Every window shares one controller, one store and one dirty set, but each
/// holds its own canvas. A second window on the same notebook used to keep the
/// ink it loaded and, on its next stroke, write that whole page back over what
/// the first window had written.
@MainActor
final class PaperControllerLocalChangeTests: XCTestCase {
    /// Held here because a page keeps its delegate weakly.
    private var host: LocalChangeStubDelegate?

    // MARK: - The channel

    func testMarkingAPageDirtyAnnouncesTheWindowItCameFrom() throws {
        let fixture = try makeController()

        fixture.controller.markPageDirty(fixture.ref, source: fixture.a)

        XCTAssertEqual(fixture.recorder.events, [.dirtied(fixture.ref, source: fixture.a)])
    }

    func testAWriteAnnouncesTheWindowWhoseInkWasWritten() throws {
        let fixture = try makeController()

        fixture.controller.markPageDirty(fixture.ref, source: fixture.a)
        fixture.controller.autosave.flushAll()
        XCTAssertEqual(
            fixture.recorder.events,
            [.dirtied(fixture.ref, source: fixture.a), .flushed(fixture.ref, owner: fixture.a)])
        XCTAssertEqual(fixture.onDisk, Self.inkA)

        fixture.recorder.events.removeAll()
        fixture.controller.markPageDirty(fixture.ref, source: fixture.b)
        fixture.controller.autosave.flushAll()
        XCTAssertEqual(
            fixture.recorder.events,
            [.dirtied(fixture.ref, source: fixture.b), .flushed(fixture.ref, owner: fixture.b)])
        XCTAssertEqual(fixture.onDisk, Self.inkB)
    }

    func testAnEditFromNoWindowIsAnnouncedWithoutASource() throws {
        let fixture = try makeController()

        fixture.controller.markPageDirty(fixture.ref)
        fixture.controller.autosave.flushAll()

        XCTAssertEqual(
            fixture.recorder.events,
            [.dirtied(fixture.ref, source: nil), .flushed(fixture.ref, owner: nil)])
    }

    func testARemovedObserverHearsNothingMore() throws {
        let fixture = try makeController()
        fixture.controller.removeLocalPageChangeObserver(fixture.observer)

        fixture.controller.markPageDirty(fixture.ref, source: fixture.a)
        fixture.controller.autosave.flushAll()

        XCTAssertTrue(fixture.recorder.events.isEmpty)
    }

    // MARK: - Two windows on one notebook

    func testInkWrittenInOneWindowReachesTheOtherAndBothSurvive() throws {
        let fixture = try makeWindows()
        let pageA = try XCTUnwrap(fixture.a.pageViews.first)
        let pageB = try XCTUnwrap(fixture.b.pageViews.first)
        let canvasA = try XCTUnwrap(pageA.canvas)
        let canvasB = try XCTUnwrap(pageB.canvas)

        canvasA.drawing = PKDrawing(strokes: [Self.stroke(y: 240)])
        fixture.a.pageViewDrawingChanged(pageA)

        // B stops taking input on the page while A's ink is not on disk.
        XCTAssertTrue(pageB.isExternallyEditing)
        XCTAssertFalse(canvasB.isUserInteractionEnabled)
        XCTAssertFalse(pageA.isExternallyEditing, "a window yielded to itself")
        XCTAssertTrue(canvasA.isUserInteractionEnabled)
        XCTAssertFalse(
            try XCTUnwrap(fixture.b.pageViews.last).isExternallyEditing,
            "a page nobody wrote on yielded")

        fixture.controller.autosave.flushAll()

        try assertInk(canvasA, matches: fixture.onDisk(pageA))
        try assertInk(canvasB, matches: fixture.onDisk(pageA), "B still shows the ink it loaded")
        XCTAssertEqual(canvasB.drawing.strokes.count, 1)
        XCTAssertFalse(pageB.isExternallyEditing)
        XCTAssertTrue(canvasB.isUserInteractionEnabled)
        XCTAssertFalse(
            fixture.controller.autosave.hasUnsavedChanges,
            "the reload was reported as an edit and would be written straight back")

        // Now B writes, on top of A's stroke.
        canvasB.drawing = canvasB.drawing.appending(PKDrawing(strokes: [Self.stroke(y: 480)]))
        fixture.b.pageViewDrawingChanged(pageB)
        XCTAssertTrue(pageA.isExternallyEditing)
        XCTAssertFalse(canvasA.isUserInteractionEnabled)

        fixture.controller.autosave.flushAll()

        let second = try XCTUnwrap(fixture.onDisk(pageA))
        XCTAssertEqual(try PKDrawing(data: second).strokes.count, 2, "one window's ink was lost")
        try assertInk(canvasA, matches: second)
        XCTAssertFalse(pageA.isExternallyEditing)
        XCTAssertTrue(canvasA.isUserInteractionEnabled)
    }

    func testAnEditThatStillReachesTheWaitingWindowDoesNotWriteItsStaleInk() throws {
        let fixture = try makeWindows()
        let pageA = try XCTUnwrap(fixture.a.pageViews.first)
        let pageB = try XCTUnwrap(fixture.b.pageViews.first)
        let canvasA = try XCTUnwrap(pageA.canvas)
        let canvasB = try XCTUnwrap(pageB.canvas)

        canvasA.drawing = PKDrawing(strokes: [Self.stroke(y: 240)])
        fixture.a.pageViewDrawingChanged(pageA)
        XCTAssertTrue(pageB.isExternallyEditing)

        // What an undo in B does: no touch, so the lock does not stop it.
        canvasB.drawing = PKDrawing(strokes: [Self.stroke(y: 600), Self.stroke(y: 700)])
        fixture.b.pageViewDrawingChanged(pageB)

        fixture.controller.autosave.flushAll()

        try assertInk(canvasA, matches: fixture.onDisk(pageA), "B's stale copy was written")
        try assertInk(canvasB, matches: fixture.onDisk(pageA))
        XCTAssertFalse(pageB.isExternallyEditing)
    }

    func testAPageThatComesIntoViewWhileAnotherWindowWritesItWaitsForThatInk() throws {
        let fixture = try makeWindows(pages: 6)
        let scrollB = try XCTUnwrap(
            fixture.b.view.subviews.compactMap { $0 as? PageScrollView }.first)
        scrollB.contentOffset = CGPoint(x: 0, y: fixture.b.pageViews[4].frame.minY)
        let pageB = fixture.b.pageViews[0]
        XCTAssertFalse(pageB.isLive)

        let pageA = try XCTUnwrap(fixture.a.pageViews.first)
        let canvasA = try XCTUnwrap(pageA.canvas)
        canvasA.drawing = PKDrawing(strokes: [Self.stroke(y: 240)])
        fixture.a.pageViewDrawingChanged(pageA)

        scrollB.contentOffset = .zero
        XCTAssertTrue(pageB.isLive)
        XCTAssertTrue(pageB.isExternallyEditing)
        XCTAssertEqual(pageB.canvas?.isUserInteractionEnabled, false)

        fixture.controller.autosave.flushAll()

        XCTAssertFalse(pageB.isExternallyEditing)
        try assertInk(try XCTUnwrap(pageB.canvas), matches: fixture.onDisk(pageA))
        XCTAssertEqual(pageB.canvas?.drawing.strokes.count, 1)
    }

    func testAStrokeInProgressIsNeverReloadedUnderThePen() throws {
        let fixture = try makeWindows()
        let pageA = try XCTUnwrap(fixture.a.pageViews.first)
        let pageB = try XCTUnwrap(fixture.b.pageViews.first)
        let canvasA = try XCTUnwrap(pageA.canvas)
        let canvasB = try XCTUnwrap(pageB.canvas)

        fixture.b.pageViewDidBeginUsingTool(pageB)
        canvasA.drawing = PKDrawing(strokes: [Self.stroke(y: 240)])
        fixture.a.pageViewDrawingChanged(pageA)
        XCTAssertFalse(pageB.isExternallyEditing, "the canvas was switched off under the pen")

        fixture.controller.autosave.flushAll()
        XCTAssertTrue(canvasB.drawing.strokes.isEmpty, "the canvas was reloaded under the pen")

        // The pen lifts without having changed anything: the page catches up.
        fixture.b.pageViewDidEndUsingTool(pageB)
        try assertInk(canvasB, matches: fixture.onDisk(pageA))
        XCTAssertEqual(canvasB.drawing.strokes.count, 1)
        XCTAssertTrue(canvasB.isUserInteractionEnabled)
    }

    /// The residual race: two pens on one page inside one debounce. Neither
    /// window's ink may exist only on its screen, and the two must agree.
    func testWhenBothWindowsWriteInOneDebounceBothVersionsLandAndTheWindowsAgree() throws {
        let fixture = try makeWindows()
        let pageA = try XCTUnwrap(fixture.a.pageViews.first)
        let pageB = try XCTUnwrap(fixture.b.pageViews.first)
        let canvasA = try XCTUnwrap(pageA.canvas)
        let canvasB = try XCTUnwrap(pageB.canvas)

        fixture.b.pageViewDidBeginUsingTool(pageB)
        canvasA.drawing = PKDrawing(strokes: [Self.stroke(y: 240)])
        fixture.a.pageViewDrawingChanged(pageA)
        canvasB.drawing = PKDrawing(strokes: [Self.stroke(y: 520)])
        fixture.b.pageViewDrawingChanged(pageB)
        fixture.b.pageViewDidEndUsingTool(pageB)

        // B wrote last, so B's canvas is the one written...
        fixture.controller.autosave.flushAll()
        try assertInk(canvasB, matches: fixture.onDisk(pageA))
        // ...and A, whose stroke is only on its screen, claims the page again.
        XCTAssertTrue(fixture.controller.autosave.hasUnsavedChanges)
        XCTAssertTrue(pageB.isExternallyEditing)

        fixture.controller.autosave.flushAll()
        try assertInk(canvasA, matches: fixture.onDisk(pageA))
        try assertInk(canvasB, matches: fixture.onDisk(pageA), "the two windows disagree")
        XCTAssertFalse(pageB.isExternallyEditing)
        XCTAssertFalse(fixture.controller.autosave.hasUnsavedChanges)
        XCTAssertEqual(
            try fixture.controller.snapshots.snapshots(pageID: pageA.page.id).count, 2,
            "the version replaced on screen is not in the page history")
    }

    // MARK: - The page

    func testAPageAnotherWindowIsWritingTakesNoInputUntilItIsReleased() throws {
        let view = makeLivePage()
        let canvas = try XCTUnwrap(view.canvas)
        let object = PageObject(
            type: .text, frame: PageObject.Frame(x: 100, y: 100, width: 200, height: 60),
            text: "x")
        view.insertObject(object)
        view.deselectObjects()
        let inside = CGPoint(x: object.frame.rect.midX, y: object.frame.rect.midY)

        view.setExternallyEditing(true)
        XCTAssertFalse(canvas.isUserInteractionEnabled)
        view.setLassoActive(true)
        XCTAssertEqual(view.lasso?.isUserInteractionEnabled, false)
        view.setLassoActive(false)
        view.handleObjectTap(at: inside)
        XCTAssertFalse(view.hasSelectedObject)
        XCTAssertFalse(canvas.isUserInteractionEnabled, "putting the Lasso down released the page")
        view.update(page: view.page)
        XCTAssertTrue(view.isExternallyEditing, "a metadata refresh released the page")

        view.setExternallyEditing(false)
        XCTAssertTrue(canvas.isUserInteractionEnabled)
        view.handleObjectTap(at: inside)
        XCTAssertTrue(view.hasSelectedObject)
        XCTAssertFalse(canvas.isUserInteractionEnabled)
        XCTAssertTrue(view.objects.isUserInteractionEnabled)

        // The selection survives another window's edit and gets its touches back.
        view.setExternallyEditing(true)
        XCTAssertFalse(view.objects.isUserInteractionEnabled)
        view.setExternallyEditing(false)
        XCTAssertTrue(view.hasSelectedObject)
        XCTAssertTrue(view.objects.isUserInteractionEnabled)
        XCTAssertFalse(canvas.isUserInteractionEnabled)
    }

    // MARK: - Helpers

    private final class Recorder {
        var events: [LocalPageEvent] = []
    }

    private struct ControllerFixture {
        let store: PaperStore
        let controller: PaperController
        let ref: PageRef
        let a: UUID
        let b: UUID
        let recorder: Recorder
        let observer: UUID

        var onDisk: Data? {
            store.drawingData(notebookPath: ref.notebookPath, pageID: ref.pageID)
        }
    }

    private static let inkA = Data("window A".utf8)
    private static let inkB = Data("window B".utf8)

    private func makeController() throws -> ControllerFixture {
        let store = try PaperStore(root: try temporaryRoot())
        let controller = PaperController(store: store)
        let notebook = try store.createNotebook(title: "Local changes")
        let pageID = try XCTUnwrap(notebook.pages.first)
        let ref = PageRef(notebookID: notebook.id, notebookPath: notebook.path, pageID: pageID)
        let a = controller.addDrawingProvider { _ in Self.inkA }
        let b = controller.addDrawingProvider { _ in Self.inkB }
        let recorder = Recorder()
        let observer = controller.observeLocalPageChanges { recorder.events.append($0) }
        return ControllerFixture(
            store: store, controller: controller, ref: ref, a: a, b: b, recorder: recorder,
            observer: observer)
    }

    @MainActor
    private struct WindowsFixture {
        let store: PaperStore
        let controller: PaperController
        let notebook: Notebook
        let a: NotebookViewController
        let b: NotebookViewController

        func onDisk(_ page: PageView) -> Data? {
            store.drawingData(notebookPath: notebook.path, pageID: page.page.id)
        }
    }

    /// One notebook open in two windows, the way two scenes share the app's
    /// one controller.
    private func makeWindows(pages: Int = 2) throws -> WindowsFixture {
        let store = try PaperStore(root: try temporaryRoot())
        let controller = PaperController(store: store)
        let notebook = try store.createNotebook(title: "Side by side", pageCount: pages)
        return WindowsFixture(
            store: store, controller: controller, notebook: notebook,
            a: try openWindow(controller: controller, notebook: notebook),
            b: try openWindow(controller: controller, notebook: notebook))
    }

    private func openWindow(controller: PaperController, notebook: Notebook) throws
        -> NotebookViewController
    {
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
        notebookController.view.layoutIfNeeded()
        notebookController.beginAppearanceTransition(true, animated: false)
        notebookController.endAppearanceTransition()
        XCTAssertNotNil(notebookController.pageViews.first?.canvas)
        return notebookController
    }

    private func makeLivePage() -> PageView {
        let host = LocalChangeStubDelegate()
        self.host = host
        let view = PageView(page: Page(), notebookPath: "paper/T.paper")
        view.delegate = host
        view.goLive(drawingData: nil)
        XCTAssertTrue(view.isLive)
        return view
    }

    /// The same ink, compared by what it draws: a drawing decoded from disk
    /// need not encode back to the same bytes.
    private func assertInk(
        _ canvas: PKCanvasView, matches data: Data?, _ message: String = "",
        file: StaticString = #filePath, line: UInt = #line
    ) throws {
        let data = try XCTUnwrap(data, "nothing on disk", file: file, line: line)
        let stored = try PKDrawing(data: data)
        XCTAssertEqual(
            canvas.drawing.strokes.count, stored.strokes.count, message, file: file, line: line)
        XCTAssertEqual(
            canvas.drawing.bounds.integral, stored.bounds.integral, message, file: file,
            line: line)
    }

    private func temporaryRoot() throws -> URL {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("PaperControllerLocalChangeTests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        return root
    }

    private static func stroke(y: Double) -> PKStroke {
        let points = stride(from: 0.0, through: 320.0, by: 8.0).map { offset -> PKStrokePoint in
            PKStrokePoint(
                location: CGPoint(x: 90 + offset, y: y + sin(offset / 40) * 30),
                timeOffset: offset / 320, size: CGSize(width: 5, height: 5), opacity: 1,
                force: 1, azimuth: 0, altitude: .pi / 2)
        }
        return PKStroke(
            ink: PKInk(.pen, color: .black),
            path: PKStrokePath(controlPoints: points, creationDate: Date()))
    }
}

@MainActor
private final class LocalChangeStubDelegate: PageViewDelegate {
    let tools = ToolState()

    func pageViewDrawingChanged(_ pageView: PageView) {}
    func pageViewDidBeginUsingTool(_ pageView: PageView) {}
    func pageViewDidEndUsingTool(_ pageView: PageView) {}
    func pageViewToolState(_ pageView: PageView) -> ToolState { tools }
    func pageViewDrawingPolicy(_ pageView: PageView) -> PKCanvasViewDrawingPolicy { .pencilOnly }
    func pageView(_ pageView: PageView, didChangeSelection isEmpty: Bool) {}
    func pageView(_ pageView: PageView, didChangeObjects objects: [PageObject]) {}
    func pageView(_ pageView: PageView, imageForAsset assetPath: String) -> UIImage? { nil }
    func pageView(_ pageView: PageView, didDropImage data: Data, name: String, at point: CGPoint) {}
    func pageView(_ pageView: PageView, didDropPDF data: Data, name: String) {}
}
