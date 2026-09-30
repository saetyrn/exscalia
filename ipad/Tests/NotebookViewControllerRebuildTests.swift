import PaperCore
import PencilKit
import XCTest

@testable import PaperUI

/// A title, a bookmark, a page added, this device's own sync echo: each used to
/// tear down every page view and recreate it, taking the canvas under the pen
/// with it. A rebuild is a diff by page id now, and waits for the pen to lift.
@MainActor
final class NotebookViewControllerRebuildTests: XCTestCase {
    private struct Fixture {
        let store: PaperStore
        let controller: PaperController
        let session: NotebookSession
        let notebookController: NotebookViewController
    }

    func testAddingAPageKeepsTheViewsAndCanvasesAlreadyThere() throws {
        let fixture = try makeFixture(pages: 3)
        let before = fixture.notebookController.pageViews
        XCTAssertEqual(before.count, 3)
        let canvas = try XCTUnwrap(before[0].canvas)

        fixture.session.addPage(after: 0)

        let after = fixture.notebookController.pageViews
        XCTAssertEqual(after.count, 4)
        for view in before {
            XCTAssertTrue(after.contains { $0 === view }, "a surviving page lost its view")
        }
        XCTAssertTrue(after[0] === before[0])
        XCTAssertTrue(after[2] === before[1])
        XCTAssertTrue(after[3] === before[2])
        XCTAssertFalse(before.contains { $0 === after[1] }, "the new page needs a view of its own")
        XCTAssertTrue(after[0].canvas === canvas, "the first page's canvas was replaced")
    }

    func testABookmarkKeepsTheCanvasAndTheInkOnIt() throws {
        let fixture = try makeFixture(pages: 3)
        let page = try XCTUnwrap(fixture.notebookController.pageViews.first)
        let canvas = try XCTUnwrap(page.canvas)
        canvas.drawing = PKDrawing(strokes: [Self.stroke(y: 240)])
        fixture.notebookController.pageViewDrawingChanged(page)
        let views = fixture.notebookController.pageViews

        // What the overflow menu's Bookmark page does.
        fixture.controller.setPageBookmarked(
            true, notebookPath: fixture.session.notebook.path, pageID: page.page.id)
        fixture.session.reload()
        fixture.session.onPagesChanged?()

        let after = fixture.notebookController.pageViews
        XCTAssertEqual(after.count, views.count)
        for (old, new) in zip(views, after) { XCTAssertTrue(old === new) }
        XCTAssertTrue(page.canvas === canvas)
        XCTAssertEqual(canvas.drawing.strokes.count, 1)
        XCTAssertTrue(page.page.bookmarked, "the kept view should show the new metadata")
    }

    func testInkWrittenElsewhereReachesTheLiveCanvas() throws {
        let fixture = try makeFixture(pages: 2)
        let page = try XCTUnwrap(fixture.notebookController.pageViews.first)
        let canvas = try XCTUnwrap(page.canvas)
        XCTAssertTrue(canvas.drawing.strokes.isEmpty)

        // What a pull does with another device's ink.
        let theirs = PKDrawing(strokes: [Self.stroke(y: 300), Self.stroke(y: 400)])
        try fixture.store.writeDrawing(
            theirs.dataRepresentation(), notebookPath: fixture.session.notebook.path,
            pageID: page.page.id)
        fixture.session.reload()
        fixture.session.onPagesChanged?()

        XCTAssertTrue(page.canvas === canvas)
        XCTAssertEqual(canvas.drawing.strokes.count, 2)
    }

    func testARebuildAskedForMidStrokeWaitsForThePenToLift() throws {
        let fixture = try makeFixture(pages: 3)
        let page = try XCTUnwrap(fixture.notebookController.pageViews.first)
        let canvas = try XCTUnwrap(page.canvas)

        fixture.notebookController.pageViewDidBeginUsingTool(page)
        fixture.session.addPage(after: 0)
        XCTAssertEqual(
            fixture.notebookController.pageViews.count, 3, "rebuilt under a live stroke")
        XCTAssertTrue(page.canvas === canvas)

        fixture.notebookController.pageViewDidEndUsingTool(page)
        XCTAssertEqual(fixture.notebookController.pageViews.count, 4)
        XCTAssertTrue(fixture.notebookController.pageViews[0] === page)
        XCTAssertEqual(
            fixture.notebookController.pageViews.map(\.page.id), fixture.session.pages.map(\.id))
    }

    func testCommandZDropsTheLassoSelectionAndMarksNothingByItself() throws {
        let fixture = try makeFixture(pages: 2)
        let page = try XCTUnwrap(fixture.notebookController.pageViews.first)
        let canvas = try XCTUnwrap(page.canvas)
        let lasso = try XCTUnwrap(page.lasso)
        page.applyDrawing(PKDrawing(strokes: [Self.stroke(y: 600)]))
        Self.select(around: CGRect(x: 60, y: 540, width: 380, height: 120), on: lasso)
        XCTAssertFalse(lasso.selection.isEmpty)
        Self.endEvent()
        canvas.undoManager?.removeAllActions()
        XCTAssertFalse(fixture.controller.autosave.hasUnsavedChanges)

        let undo = try XCTUnwrap(
            fixture.notebookController.keyCommands?
                .first { $0.input == "z" && $0.modifierFlags == [.command] }?.action)
        _ = fixture.notebookController.perform(undo)

        XCTAssertTrue(lasso.selection.isEmpty, "the box outlives the strokes it was drawn round")
        XCTAssertFalse(
            fixture.controller.autosave.hasUnsavedChanges,
            "an undo that changed nothing marked the page under the viewport dirty")
    }

    // MARK: - Helpers

    private func makeFixture(pages: Int) throws -> Fixture {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("NotebookViewControllerRebuildTests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }

        let store = try PaperStore(root: root)
        let controller = PaperController(store: store)
        let notebook = try store.createNotebook(title: "Rebuild", pageCount: pages)
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

        XCTAssertEqual(notebookController.pageViews.count, pages)
        XCTAssertNotNil(notebookController.pageViews.first?.canvas)
        return Fixture(
            store: store, controller: controller, session: session,
            notebookController: notebookController)
    }

    static func stroke(y: Double) -> PKStroke {
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

    /// Draws a lasso loop round `rect`, given in page points.
    static func select(around rect: CGRect, on lasso: LassoOverlayView) {
        let zoom = lasso.zoom
        let corners = [
            CGPoint(x: rect.minX, y: rect.minY), CGPoint(x: rect.maxX, y: rect.minY),
            CGPoint(x: rect.maxX, y: rect.maxY), CGPoint(x: rect.minX, y: rect.maxY),
        ].map { CGPoint(x: $0.x * zoom, y: $0.y * zoom) }
        lasso.handleTouchBegan(at: corners[0])
        for corner in corners.dropFirst() { lasso.handleTouchMoved(to: corner) }
        lasso.handleTouchMoved(to: corners[0])
        lasso.handleTouchEnded()
    }

    /// Lets the run loop turn, which is what closes an undo group: each step
    /// of a test becomes its own Cmd-Z, as each touch is in the app.
    static func endEvent() {
        RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.02))
    }
}

/// An infinite page that grows at the top or the left moves everything on it.
/// An undo registered before the growth holds coordinates from before it, and
/// used to put them back as they were: the ink jumped half a screen away from
/// the objects and the page it had grown with.
@MainActor
final class PageViewGrowthUndoTests: XCTestCase {
    /// Held here because the page keeps its delegate weakly.
    private var host: GrowthStubDelegate?

    func testGrowthAtTheTopOrLeftIsCountedOnThePage() throws {
        let page = try makeLivePage()
        XCTAssertEqual(page.growthTranslation, .zero)

        page.applyGrowth(Self.growth(dx: 300, dy: 0))
        XCTAssertEqual(page.growthTranslation, CGSize(width: 300, height: 0))
        page.applyGrowth(Self.growth(dx: 0, dy: 0))
        page.applyGrowth(Self.growth(dx: 0, dy: 200))
        XCTAssertEqual(page.growthTranslation, CGSize(width: 300, height: 200))
    }

    func testUndoingALassoMoveAfterAGrowthPutsTheInkWhereTheGrowthMovedIt() throws {
        let page = try makeLivePage()
        let canvas = try XCTUnwrap(page.canvas)
        let lasso = try XCTUnwrap(page.lasso)
        let manager = try XCTUnwrap(canvas.undoManager)
        page.applyDrawing(PKDrawing(strokes: [NotebookViewControllerRebuildTests.stroke(y: 600)]))
        let original = canvas.drawing.bounds
        NotebookViewControllerRebuildTests.select(
            around: CGRect(x: 60, y: 540, width: 380, height: 120), on: lasso)
        XCTAssertFalse(lasso.selection.isEmpty)
        NotebookViewControllerRebuildTests.endEvent()
        manager.removeAllActions()

        lasso.translateSelection(by: CGSize(width: 20, height: 0))
        let moved = canvas.drawing.bounds
        XCTAssertEqual(moved.minX, original.minX + 20, accuracy: 0.5)
        // What the notebook does, in the same turn, when an edit comes within
        // the margin of the left edge.
        page.applyGrowth(Self.growth(dx: 300, dy: 0))
        NotebookViewControllerRebuildTests.endEvent()

        manager.undo()
        XCTAssertEqual(canvas.drawing.bounds.minX, original.minX + 300, accuracy: 0.5)
        XCTAssertTrue(lasso.selection.isEmpty)

        manager.redo()
        XCTAssertEqual(canvas.drawing.bounds.minX, moved.minX + 300, accuracy: 0.5)
    }

    func testUndoingAnObjectEditAfterAGrowthKeepsTheObjectsWhereTheGrowthMovedThem() throws {
        let page = try makeLivePage()
        let manager = try XCTUnwrap(page.canvas?.undoManager)
        manager.removeAllActions()
        let first = Self.image(x: 200)
        page.insertObject(first)
        NotebookViewControllerRebuildTests.endEvent()

        let second = Self.image(x: 400)
        page.insertObject(second)
        page.applyGrowth(Self.growth(dx: 300, dy: 0))
        NotebookViewControllerRebuildTests.endEvent()

        manager.undo()
        XCTAssertEqual(page.page.objects.map(\.id), [first.id])
        XCTAssertEqual(page.page.objects.first?.frame.x, first.frame.x + 300)
        XCTAssertEqual(page.objects.objects, page.page.objects)

        manager.redo()
        XCTAssertEqual(page.page.objects.map(\.id), [first.id, second.id])
        XCTAssertEqual(page.page.objects.map(\.frame.x), [first.frame.x + 300, second.frame.x + 300])
    }

    // MARK: - Helpers

    private func makeLivePage() throws -> PageView {
        let host = GrowthStubDelegate()
        self.host = host
        let page = PageView(page: Page(), notebookPath: "paper/Growth.paper")
        page.delegate = host

        // In a window, so the canvas has an undo manager to register on.
        let root = UIViewController()
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 1024, height: 1366))
        window.rootViewController = root
        window.makeKeyAndVisible()
        addTeardownBlock { @MainActor in
            window.isHidden = true
            window.rootViewController = nil
        }
        root.view.addSubview(page)
        page.goLive(drawingData: nil)
        XCTAssertTrue(page.isLive)
        return page
    }

    private static func growth(dx: CGFloat, dy: CGFloat) -> InfiniteCanvas.Growth {
        InfiniteCanvas.Growth(
            extent: CGSize(
                width: InfiniteCanvas.baseExtent.width + dx,
                height: InfiniteCanvas.baseExtent.height + dy),
            translation: CGSize(width: dx, height: dy))
    }

    private static func image(x: Double) -> PageObject {
        PageObject(
            type: .image, frame: PageObject.Frame(x: x, y: 300, width: 120, height: 90),
            asset: "paper/Growth.paper/assets/photo.png")
    }
}

@MainActor
private final class GrowthStubDelegate: PageViewDelegate {
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
