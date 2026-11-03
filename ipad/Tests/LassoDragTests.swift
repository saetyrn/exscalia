import PaperCore
import PencilKit
import XCTest

@testable import PaperUI

/// Moving a lasso selection, driven through `routeLassoTouch` -- the entry
/// point `LassoGestureRecognizer`'s action uses -- against a real notebook on
/// disk, a live `PKCanvasView` and the window's undo manager.
///
/// The simulator has no Pencil and cannot cancel a touch, so these are the
/// begin, move, end and cancel sequences a Pencil or a finger produces on the
/// iPad, written out.
@MainActor
final class LassoDragTests: XCTestCase {
    @MainActor
    private struct Fixture {
        let store: PaperStore
        let controller: PaperController
        let session: NotebookSession
        let notebookController: NotebookViewController
        let page: PageView
        let canvas: PKCanvasView
        let lasso: LassoOverlayView
        let stack: UIView
        let ref: PageRef
        let undo: UndoManager

        var zoom: CGFloat { page.zoom }

        /// A page point as the recogniser reports it: stack coordinates.
        func onScreen(_ point: CGPoint) -> CGPoint {
            stack.convert(CGPoint(x: point.x * zoom, y: point.y * zoom), from: page)
        }

        var isDirty: Bool { controller.autosave.isDirty(ref) }

        /// The page's ink as autosave last wrote it.
        func saved() throws -> Data {
            try XCTUnwrap(store.drawingData(notebookPath: ref.notebookPath, pageID: ref.pageID))
        }
    }

    // MARK: - Drop

    func testDraggingASelectionCarriesTheInkAndDropsItInOneUndoStep() throws {
        let f = try makeFixture()
        let before = f.canvas.drawing
        let ink = before.bounds
        select(round: ink, f)
        XCTAssertEqual(f.lasso.selection, [0])

        let start = f.onScreen(CGPoint(x: ink.midX, y: ink.midY))
        f.notebookController.routeLassoTouch(.began(start))
        carry(f, from: start, by: CGSize(width: 120, height: 160))

        // In the air: the canvas has let go of the stroke, and the overlay
        // draws it under the touch.
        XCTAssertTrue(f.lasso.isLifted)
        XCTAssertEqual(f.canvas.drawing.strokes.count, 0)
        let carried = try XCTUnwrap(f.lasso.carriedInkFrame, "the ink did not follow the touch")
        assertRect(
            carried,
            equals: ink.applying(CGAffineTransform(scaleX: f.zoom, y: f.zoom))
                .offsetBy(dx: 120 * f.zoom, dy: 160 * f.zoom))
        assertRect(
            f.lasso.selectionBounds,
            equals: strokeBounds(before, f).offsetBy(dx: 120 * f.zoom, dy: 160 * f.zoom))
        // The page still has it where it was picked up: carrying is not an
        // edit, registers nothing, and is not what autosave writes.
        XCTAssertEqual(f.page.drawing?.dataRepresentation(), before.dataRepresentation())
        XCTAssertEqual(f.page.drawingData, before.dataRepresentation())
        XCTAssertFalse(f.isDirty)
        XCTAssertFalse(f.undo.canUndo)

        f.notebookController.routeLassoTouch(.ended)

        XCTAssertFalse(f.lasso.isLifted)
        XCTAssertEqual(f.canvas.drawing.strokes.count, 1)
        assertRect(f.canvas.drawing.bounds, equals: ink.offsetBy(dx: 120, dy: 160))
        XCTAssertEqual(f.lasso.selection, [0], "the selection stays on the moved ink")
        XCTAssertNotNil(f.lasso.accessibleInspector, "the inspector came back")
        XCTAssertTrue(f.isDirty, "a drop is an edit")
        XCTAssertTrue(f.undo.canUndo)
        XCTAssertEqual(f.undo.undoActionName, "Move Selection")

        f.controller.autosave.flushAll()
        XCTAssertEqual(try f.saved(), f.canvas.drawing.dataRepresentation())

        f.undo.undo()
        assertRect(f.canvas.drawing.bounds, equals: ink)
        XCTAssertFalse(f.undo.canUndo, "the whole drag was one undo step")
        f.undo.redo()
        assertRect(f.canvas.drawing.bounds, equals: ink.offsetBy(dx: 120, dy: 160))
    }

    func testATouchOnTheSelectionThatDoesNotTravelChangesNothing() throws {
        let f = try makeFixture()
        let before = f.canvas.drawing
        select(round: before.bounds, f)
        let start = f.onScreen(CGPoint(x: before.bounds.midX, y: before.bounds.midY))

        f.notebookController.routeLassoTouch(.began(start))
        f.notebookController.routeLassoTouch(.moved(CGPoint(x: start.x + 1, y: start.y)))
        XCTAssertFalse(f.lasso.isLifted)
        f.notebookController.routeLassoTouch(.ended)

        XCTAssertEqual(f.canvas.drawing.dataRepresentation(), before.dataRepresentation())
        XCTAssertEqual(f.lasso.selection, [0])
        XCTAssertFalse(f.undo.canUndo)
        XCTAssertFalse(f.isDirty)
    }

    func testATouchAwayFromTheSelectionStartsANewLoopAndClearsIt() throws {
        let f = try makeFixture()
        let ink = f.canvas.drawing.bounds
        select(round: ink, f)
        XCTAssertEqual(f.lasso.selection, [0])

        let away = f.onScreen(CGPoint(x: ink.midX, y: ink.maxY + 300))
        f.notebookController.routeLassoTouch(.began(away))
        f.notebookController.routeLassoTouch(.ended)

        XCTAssertEqual(f.lasso.selection, [])
        XCTAssertNil(f.lasso.accessibleInspector)
        assertRect(f.canvas.drawing.bounds, equals: ink)
    }

    // MARK: - Cancel

    /// The system took the touch away part-way. The ink goes back where it
    /// started, with its outline, and nothing is left to save or undo.
    func testACancelledDragPutsTheInkAndTheOutlineBackWhereTheyStarted() throws {
        let f = try makeFixture()
        let before = f.canvas.drawing
        let ink = before.bounds
        select(round: ink, f)
        let outline = f.lasso.selectionBounds
        let start = f.onScreen(CGPoint(x: ink.midX, y: ink.midY))

        f.notebookController.routeLassoTouch(.began(start))
        carry(f, from: start, by: CGSize(width: 150, height: 90))
        XCTAssertNotEqual(f.lasso.selectionBounds, outline)

        f.notebookController.routeLassoTouch(.cancelled)

        XCTAssertFalse(f.lasso.isLifted)
        XCTAssertNil(f.lasso.drag)
        XCTAssertEqual(f.canvas.drawing.dataRepresentation(), before.dataRepresentation())
        XCTAssertEqual(f.lasso.selectionBounds, outline)
        XCTAssertEqual(f.lasso.selection, [0])
        XCTAssertNotNil(f.lasso.accessibleInspector)
        XCTAssertFalse(f.undo.canUndo)
        XCTAssertFalse(f.isDirty)

        // And the selection can be picked up again and dropped.
        f.notebookController.routeLassoTouch(.began(start))
        carry(f, from: start, by: CGSize(width: 40, height: 0))
        f.notebookController.routeLassoTouch(.ended)
        assertRect(f.canvas.drawing.bounds, equals: ink.offsetBy(dx: 40, dy: 0))
    }

    /// The autosave debounce of an earlier stroke fires mid-drag, or the app
    /// is sent to the background mid-drag: the page written is the page with
    /// the selection where it rests, never the page with a hole in it.
    func testAutosaveDuringADragWritesTheInkWhereItRests() throws {
        let f = try makeFixture()
        let before = f.canvas.drawing
        select(round: before.bounds, f)
        let start = f.onScreen(CGPoint(x: before.bounds.midX, y: before.bounds.midY))
        f.notebookController.routeLassoTouch(.began(start))
        carry(f, from: start, by: CGSize(width: 90, height: 90))
        XCTAssertEqual(f.canvas.drawing.strokes.count, 0)

        // A stroke written just before the drag left the page dirty.
        f.notebookController.pageViewDrawingChanged(f.page)
        XCTAssertNotNil(f.lasso.drag)
        f.controller.autosave.flushAll()
        XCTAssertEqual(try f.saved(), before.dataRepresentation())

        f.notebookController.routeLassoTouch(.cancelled)
        XCTAssertEqual(f.canvas.drawing.dataRepresentation(), try f.saved())
    }

    /// Picking another tool -- a Pencil double-tap, say -- puts a carried
    /// selection back before it lets go of it.
    func testSwitchingToolMidDragPutsTheInkBack() throws {
        let f = try makeFixture()
        let before = f.canvas.drawing
        select(round: before.bounds, f)
        let start = f.onScreen(CGPoint(x: before.bounds.midX, y: before.bounds.midY))
        f.notebookController.routeLassoTouch(.began(start))
        carry(f, from: start, by: CGSize(width: 90, height: 90))

        f.page.setLassoActive(false)

        XCTAssertNil(f.lasso.drag)
        XCTAssertEqual(f.lasso.selection, [])
        XCTAssertEqual(f.canvas.drawing.dataRepresentation(), before.dataRepresentation())
        XCTAssertFalse(f.isDirty)
    }

    // MARK: - Fingers

    /// Fingers are navigation: the lasso takes one only to carry the
    /// selection, and anywhere else leaves it to the scroll view. The Pencil
    /// may always lasso.
    func testAFingerIsTakenOnlyOnTheSelection() throws {
        try XCTSkipIf(EditorInput.fingerInk, "-debugFingerInk lets a finger draw loops anywhere")
        let f = try makeFixture()
        let ink = f.canvas.drawing.bounds
        let on = f.onScreen(CGPoint(x: ink.midX, y: ink.midY))
        let off = f.onScreen(CGPoint(x: ink.midX, y: ink.maxY + 300))

        XCTAssertFalse(f.notebookController.lassoShouldBegin(at: on, touchType: .direct))
        XCTAssertTrue(f.notebookController.lassoShouldBegin(at: on, touchType: .pencil))

        select(round: ink, f)
        XCTAssertTrue(f.notebookController.lassoShouldBegin(at: on, touchType: .direct))
        XCTAssertFalse(f.notebookController.lassoShouldBegin(at: off, touchType: .direct))
        XCTAssertTrue(f.notebookController.lassoShouldBegin(at: off, touchType: .pencil))
    }

    /// A finger tap away from the selection puts it down; one on it does not.
    func testAFingerTapAwayFromTheSelectionClearsIt() throws {
        let f = try makeFixture()
        let ink = f.canvas.drawing.bounds
        select(round: ink, f)
        f.page.setLassoActive(true)

        f.page.handleObjectTap(at: CGPoint(x: ink.midX * f.zoom, y: ink.midY * f.zoom))
        XCTAssertEqual(f.lasso.selection, [0])

        f.page.handleObjectTap(at: CGPoint(x: ink.midX * f.zoom, y: (ink.maxY + 300) * f.zoom))
        XCTAssertEqual(f.lasso.selection, [])
    }

    // MARK: - The other operations

    /// Nudge, duplicate, recolour and delete still act on the whole page
    /// after a drag, and a delete that arrives mid-drag (the keyboard's
    /// Delete) puts the selection back before removing it.
    func testTheInspectorsOperationsStillWorkAroundADrag() throws {
        let f = try makeFixture(strokes: [Self.stroke(x: 300, y: 240), Self.stroke(x: 300, y: 700)])
        let first = f.canvas.drawing.strokes[0].renderBounds
        let second = f.canvas.drawing.strokes[1].renderBounds
        select(round: first, f)
        XCTAssertEqual(f.lasso.selection, [0])

        let start = f.onScreen(CGPoint(x: first.midX, y: first.midY))
        f.notebookController.routeLassoTouch(.began(start))
        carry(f, from: start, by: CGSize(width: 0, height: 100))
        f.notebookController.routeLassoTouch(.ended)
        assertRect(f.canvas.drawing.strokes[0].renderBounds, equals: first.offsetBy(dx: 0, dy: 100))
        assertRect(f.canvas.drawing.strokes[1].renderBounds, equals: second)

        f.lasso.translateSelection(by: CGSize(width: 0, height: 16))
        assertRect(f.canvas.drawing.strokes[0].renderBounds, equals: first.offsetBy(dx: 0, dy: 116))
        XCTAssertEqual(f.undo.undoActionName, "Move Selection")

        f.lasso.duplicateSelection()
        XCTAssertEqual(f.canvas.drawing.strokes.count, 3)
        XCTAssertEqual(f.lasso.selection, [2])

        f.lasso.recolorSelection(to: ToolState.penColors[1])
        XCTAssertEqual(f.undo.undoActionName, "Recolour Selection")

        // Mid-drag: the selection is in the air when the Delete key arrives.
        let copy = f.canvas.drawing.strokes[2].renderBounds
        let copyStart = f.onScreen(CGPoint(x: copy.midX, y: copy.midY))
        f.notebookController.routeLassoTouch(.began(copyStart))
        carry(f, from: copyStart, by: CGSize(width: 60, height: 60))
        XCTAssertEqual(f.canvas.drawing.strokes.count, 2)
        f.lasso.deleteSelection()
        f.notebookController.routeLassoTouch(.ended)

        XCTAssertEqual(f.canvas.drawing.strokes.count, 2)
        assertRect(f.canvas.drawing.strokes[0].renderBounds, equals: first.offsetBy(dx: 0, dy: 116))
        assertRect(f.canvas.drawing.strokes[1].renderBounds, equals: second)
        XCTAssertEqual(f.lasso.selection, [])
    }

    /// The OMNIA actions and Copy link describe the selection where it was
    /// dropped, and never change the ink.
    func testOMNIAActionsAndTheLinkDescribeTheMovedSelection() throws {
        let f = try makeFixture()
        let ink = f.canvas.drawing.bounds
        select(round: ink, f)
        let start = f.onScreen(CGPoint(x: ink.midX, y: ink.midY))
        f.notebookController.routeLassoTouch(.began(start))
        carry(f, from: start, by: CGSize(width: 0, height: 200))
        f.notebookController.routeLassoTouch(.ended)
        let moved = f.canvas.drawing

        var sent: (SelectionAIAction, Data, CGRect)?
        f.lasso.onAIAction = { sent = ($0, $1, $2) }
        f.lasso.performAIAction(.transcribe)
        let action = try XCTUnwrap(sent, "no OMNIA action was sent")
        XCTAssertEqual(action.0, .transcribe)
        XCTAssertNotNil(UIImage(data: action.1), "the crop is not an image")
        XCTAssertTrue(action.2.contains(moved.bounds), "the crop is not where the ink was dropped")

        var linked: CGRect?
        f.lasso.onCopyLink = { linked = $0 }
        f.lasso.copySelectionLink()
        let link = try XCTUnwrap(linked)
        XCTAssertEqual(link.midY, moved.bounds.midY, accuracy: 4)
        XCTAssertEqual(f.canvas.drawing.dataRepresentation(), moved.dataRepresentation())
    }

    // MARK: - SelectionDrag

    func testSelectionDragSplitsAndMovesOnlyTheSelectedStrokes() {
        let strokes = [Self.stroke(x: 100, y: 100), Self.stroke(x: 100, y: 400), Self.stroke(x: 100, y: 700)]
        var drag = SelectionDrag(
            resting: PKDrawing(strokes: strokes), selection: [0, 2], origin: CGPoint(x: 10, y: 10),
            zoom: 2)
        drag.move(to: CGPoint(x: 50, y: 30))

        XCTAssertEqual(drag.viewTranslation, CGSize(width: 40, height: 20))
        XCTAssertEqual(drag.pageTranslation, CGSize(width: 20, height: 10))
        XCTAssertEqual(drag.remainder.strokes.count, 1)
        assertRect(drag.remainder.strokes[0].renderBounds, equals: strokes[1].renderBounds)
        XCTAssertEqual(drag.carried.strokes.count, 2)
        let dropped = drag.dropped.strokes
        XCTAssertEqual(dropped.count, 3)
        assertRect(dropped[0].renderBounds, equals: strokes[0].renderBounds.offsetBy(dx: 20, dy: 10))
        assertRect(dropped[1].renderBounds, equals: strokes[1].renderBounds)
        assertRect(dropped[2].renderBounds, equals: strokes[2].renderBounds.offsetBy(dx: 20, dy: 10))
        XCTAssertEqual(drag.resting.strokes.count, 3, "the resting drawing is never changed")
    }

    // MARK: - Helpers

    private func makeFixture(strokes: [PKStroke]? = nil) throws -> Fixture {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("LassoDragTests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }

        let store = try PaperStore(root: root)
        let controller = PaperController(store: store)
        // A fixed sheet: an infinite page would grow under ink near its edge
        // and move it, which is its own test.
        let notebook = try store.createNotebook(title: "Lasso", size: .a4)
        // The tool is remembered in defaults: a suite of its own, so picking
        // the Lasso here does not change the app's.
        let defaults = try scratchDefaults("LassoDragTests")
        let tools = ToolState(defaults: defaults)
        tools.select(.lasso)
        let session = NotebookSession(controller: controller, notebook: notebook, tools: tools)
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

        let page = try XCTUnwrap(notebookController.pageViews.first)
        let canvas = try XCTUnwrap(page.canvas, "the first page should be live after layout")
        let lasso = try XCTUnwrap(page.lasso)
        let stack = try XCTUnwrap(page.superview)
        let ref = try XCTUnwrap(session.ref(at: 0))
        let undo = try XCTUnwrap(canvas.undoManager, "the canvas has no undo manager")

        // Ink on the page and on disk, as a Pencil stroke and its autosave
        // would leave it.
        canvas.drawing = PKDrawing(strokes: strokes ?? [Self.stroke(x: 200, y: 240)])
        notebookController.pageViewDrawingChanged(page)
        controller.autosave.flushAll()
        XCTAssertFalse(controller.autosave.isDirty(ref))
        undo.removeAllActions()

        return Fixture(
            store: store, controller: controller, session: session,
            notebookController: notebookController, page: page, canvas: canvas, lasso: lasso,
            stack: stack, ref: ref, undo: undo)
    }

    /// One loop round `rect` (page points), the way `-debugLassoLoop` draws it.
    private func select(round rect: CGRect, _ f: Fixture) {
        let radius = max(rect.width, rect.height) / 2 + 30
        func point(_ angle: Double) -> CGPoint {
            f.onScreen(
                CGPoint(x: rect.midX + cos(angle) * radius, y: rect.midY + sin(angle) * radius))
        }
        f.notebookController.routeLassoTouch(.began(point(0)))
        for step in 1...48 {
            f.notebookController.routeLassoTouch(.moved(point(Double(step) / 48 * 2 * .pi)))
        }
        f.notebookController.routeLassoTouch(.ended)
    }

    /// Carries the touch `offset` page points from `start`, in twelve moves.
    private func carry(_ f: Fixture, from start: CGPoint, by offset: CGSize) {
        for step in 1...12 {
            let fraction = CGFloat(step) / 12
            f.notebookController.routeLassoTouch(
                .moved(
                    CGPoint(
                        x: start.x + offset.width * f.zoom * fraction,
                        y: start.y + offset.height * f.zoom * fraction)))
        }
    }

    /// The selection's outline in overlay points, the way the overlay
    /// measures it: the union of the strokes' render bounds.
    private func strokeBounds(_ drawing: PKDrawing, _ f: Fixture) -> CGRect {
        drawing.strokes.map(\.renderBounds).reduce(CGRect.null) { $0.union($1) }
            .applying(CGAffineTransform(scaleX: f.zoom, y: f.zoom))
    }

    private func assertRect(
        _ actual: CGRect, equals expected: CGRect, accuracy: CGFloat = 0.5,
        file: StaticString = #filePath, line: UInt = #line
    ) {
        XCTAssertEqual(actual.minX, expected.minX, accuracy: accuracy, "minX", file: file, line: line)
        XCTAssertEqual(actual.minY, expected.minY, accuracy: accuracy, "minY", file: file, line: line)
        XCTAssertEqual(actual.width, expected.width, accuracy: accuracy, "width", file: file, line: line)
        XCTAssertEqual(actual.height, expected.height, accuracy: accuracy, "height", file: file, line: line)
    }

    private static func stroke(x: CGFloat, y: CGFloat) -> PKStroke {
        let points = stride(from: 0.0, through: 200.0, by: 8.0).map { offset -> PKStrokePoint in
            PKStrokePoint(
                location: CGPoint(x: x + offset, y: y + sin(offset / 40) * 30),
                timeOffset: offset / 200, size: CGSize(width: 5, height: 5), opacity: 1,
                force: 1, azimuth: 0, altitude: .pi / 2)
        }
        return PKStroke(
            ink: PKInk(.pen, color: .black),
            path: PKStrokePath(controlPoints: points, creationDate: Date()))
    }
}
