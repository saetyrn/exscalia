import PaperCore
import PencilKit
import XCTest

@testable import PaperUI

/// The whiteboard editor against a real whiteboard on disk, in a window:
/// zoom, Fit Content, the double-tap, the view state, the tools, selection,
/// the limits, Read Only, Add Board and the save path. The touches come in
/// through the entry points the recognisers call; `BoardUITests` has the
/// real ones.
@MainActor
final class BoardViewControllerTests: XCTestCase {
    @MainActor
    private struct Fixture {
        let store: PaperStore
        let controller: PaperController
        let notebook: Notebook
        let session: NotebookSession
        let board: BoardViewController
        let window: UIWindow

        var host: SingleCanvasBoardHost { board.host }
        var page: PageView? { board.page }
        var centre: CGPoint { BoardGeometry.center(of: host.extent) }
        var ref: PageRef? { session.ref(at: session.currentIndex) }
    }

    private var minimapSetting: Any?

    override func setUp() {
        super.setUp()
        minimapSetting = UserDefaults.standard.object(forKey: BoardViewController.minimapVisibleKey)
    }

    override func tearDown() {
        UserDefaults.standard.set(minimapSetting, forKey: BoardViewController.minimapVisibleKey)
        super.tearDown()
    }

    private func makeFixture(
        root: URL? = nil, notebook existing: Notebook? = nil, options: NewDocumentOptions = .whiteboardFactory
    ) throws -> Fixture {
        let directory: URL
        if let root {
            directory = root
        } else {
            directory = FileManager.default.temporaryDirectory
                .appendingPathComponent("BoardViewControllerTests-\(UUID().uuidString)")
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            addTeardownBlock { try? FileManager.default.removeItem(at: directory) }
        }
        let defaults = try scratchDefaults("BoardViewControllerTests")
        let store = try PaperStore(root: directory)
        let tools = ToolState(defaults: defaults)
        let controller = PaperController(store: store, tools: tools)
        let notebook =
            try existing
            ?? store.createDocument(
                title: "Board", folderPath: "paper", kind: .whiteboard, options: options)
        let session = NotebookSession(controller: controller, notebook: notebook, tools: tools)
        let board = BoardViewController(session: session)
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 1024, height: 1366))
        window.rootViewController = board
        window.makeKeyAndVisible()
        addTeardownBlock { @MainActor in
            window.isHidden = true
            window.rootViewController = nil
        }
        board.loadViewIfNeeded()
        board.view.frame = window.bounds
        board.view.layoutIfNeeded()
        board.beginAppearanceTransition(true, animated: false)
        board.endAppearanceTransition()
        XCTAssertTrue(board.hasPositioned, "the board was never placed")
        return Fixture(
            store: store, controller: controller, notebook: notebook, session: session,
            board: board, window: window)
    }

    private func write(_ strokes: [PKStroke], on f: Fixture) throws {
        let canvas = try XCTUnwrap(f.page?.canvas)
        // As a Pencil edit reports itself.
        canvas.drawing = PKDrawing(strokes: canvas.drawing.strokes + strokes)
        f.board.refreshContentBounds()
    }

    private func waitFor(_ condition: () -> Bool, timeout: TimeInterval = 10) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            RunLoop.current.run(until: Date().addingTimeInterval(0.02))
        }
        return condition()
    }

    // MARK: - Opening

    func testANewBoardOpensCentredAtOneHundredPercentWithTheCommandsSet() throws {
        let f = try makeFixture()
        XCTAssertTrue(f.session.commands === f.board)
        XCTAssertEqual(f.host.zoom, 1, accuracy: 0.0001)
        XCTAssertEqual(f.host.viewportCenter.x, f.centre.x, accuracy: 0.5)
        XCTAssertEqual(f.host.viewportCenter.y, f.centre.y, accuracy: 0.5)
        XCTAssertEqual(f.board.zoomPill.label, "100%")
        XCTAssertEqual(f.board.zoomPill.zoomButton.accessibilityLabel, "100%")
        let page = try XCTUnwrap(f.page)
        XCTAssertTrue(page.isBoard)
        XCTAssertTrue(page.superview === f.host.canvas, "the board's page lives in its canvas")
        XCTAssertEqual(page.frame.size.width, f.host.extent.width, accuracy: 0.5)
        XCTAssertEqual(f.page?.boardBackground?.template, .grid)
        XCTAssertEqual(f.page?.boardBackground?.paperColor, .cream)
    }

    // MARK: - Zoom

    func testTheZoomStopsAtTheFloorAndAtFourHundredPercent() throws {
        let f = try makeFixture()
        f.board.zoom(.percent(1), animated: false)
        let floor = BoardGeometry.coveringZoom(extent: f.host.extent, viewport: f.host.viewport)
        XCTAssertGreaterThanOrEqual(floor, BoardGeometry.zoomRange.lowerBound)
        XCTAssertEqual(f.host.zoom, floor, accuracy: 0.0005)
        XCTAssertEqual(f.host.canvas.minimumZoomScale, floor, accuracy: 0.0005)
        // The board still covers the screen.
        XCTAssertGreaterThanOrEqual(f.host.extent.height * f.host.zoom, f.host.viewport.height - 0.5)

        f.board.zoom(.percent(1000), animated: false)
        XCTAssertEqual(f.host.zoom, 4, accuracy: 0.0001)
        XCTAssertEqual(f.board.zoomPill.label, "400%")

        f.board.zoom(.percent(56), animated: false)
        XCTAssertEqual(f.board.zoomPill.label, "56%")
        f.board.zoom(.stepIn, animated: false)
        XCTAssertEqual(f.host.zoom, 0.7, accuracy: 0.001)
        f.board.zoom(.stepOut, animated: false)
        XCTAssertEqual(f.host.zoom, 0.56, accuracy: 0.001)
        // About the middle of the screen.
        XCTAssertEqual(f.host.viewportCenter.x, f.centre.x, accuracy: 1)
    }

    func testFitContentShowsInkFarFromTheCentre() throws {
        let f = try makeFixture()
        try write(f.board.debugStrokes(extra: 0), on: f)
        let content = try XCTUnwrap(f.board.contentBounds)
        XCTAssertGreaterThan(content.width, 6000, "the clusters are thousands of points apart")
        f.board.zoom(.fitContent, animated: false)
        XCTAssertLessThan(f.host.zoom, 0.25)
        let visible = f.host.visibleRect
        XCTAssertTrue(visible.contains(content), "\(visible) does not show \(content)")
        XCTAssertEqual(visible.midX, content.midX, accuracy: 2)
    }

    func testADoubleTapDoublesTheZoomUnderTheFingerAndComesBackFromTheCeiling() throws {
        let f = try makeFixture()
        let tap = CGPoint(x: 700, y: 500)
        let under = f.host.canvasPoint(atScreen: tap)
        f.board.doubleTapZoom(at: tap, animated: false)
        XCTAssertEqual(f.host.zoom, 2, accuracy: 0.0001)
        let after = f.host.canvasPoint(atScreen: tap)
        XCTAssertEqual(after.x, under.x, accuracy: 0.5, "the point under the finger moved")
        XCTAssertEqual(after.y, under.y, accuracy: 0.5)
        f.board.doubleTapZoom(at: tap, animated: false)
        XCTAssertEqual(f.host.zoom, 4, accuracy: 0.0001)
        f.board.doubleTapZoom(at: tap, animated: false)
        XCTAssertEqual(f.host.zoom, 1, accuracy: 0.0001)
    }

    // MARK: - View state

    func testTheViewStateIsTheBoardTheZoomAndTheMiddleInCanvasSpace() throws {
        let f = try makeFixture()
        let spot = CGPoint(x: 9_000, y: 8_500)
        f.host.place(center: spot, zoom: 0.56)
        f.session.onFlush?()
        let state = f.store.viewState(notebookID: f.notebook.id)
        XCTAssertEqual(state.lastPageID, f.session.pages.first?.id)
        XCTAssertEqual(state.zoom, 0.56, accuracy: 0.0001)
        XCTAssertEqual(state.offset.x, spot.x, accuracy: 0.5)
        XCTAssertEqual(state.offset.y, spot.y, accuracy: 0.5)

        // Opened again, it is where it was left.
        let again = try makeFixture(root: f.store.paths.root, notebook: f.notebook)
        XCTAssertEqual(again.host.zoom, 0.56, accuracy: 0.0001)
        XCTAssertEqual(again.host.viewportCenter.x, spot.x, accuracy: 0.5)
        XCTAssertEqual(again.host.viewportCenter.y, spot.y, accuracy: 0.5)
    }

    // MARK: - Tools

    func testTheCanvasFollowsTheTools() throws {
        let f = try makeFixture()
        let canvas = f.host.canvas
        f.session.tools.select(.pen)
        XCTAssertTrue(canvas.tool is PKInkingTool)
        XCTAssertTrue(canvas.drawingGestureRecognizer.isEnabled)
        XCTAssertEqual(f.board.lassoGesture?.isEnabled, false)

        f.session.tools.select(.eraser)
        XCTAssertTrue(canvas.tool is PKEraserTool)
        XCTAssertTrue(canvas.drawingGestureRecognizer.isEnabled)

        // The lasso and the insert tools take the Pencil away from the ink.
        f.session.tools.select(.lasso)
        XCTAssertFalse(canvas.drawingGestureRecognizer.isEnabled)
        XCTAssertEqual(f.board.lassoGesture?.isEnabled, true)
        f.session.tools.select(.shape)
        XCTAssertFalse(canvas.drawingGestureRecognizer.isEnabled)
        XCTAssertEqual(f.board.lassoGesture?.isEnabled, false)

        // Fingers navigate: the pan and the pinch never take the Pencil.
        let direct = NSNumber(value: UITouch.TouchType.direct.rawValue)
        XCTAssertEqual(canvas.panGestureRecognizer.allowedTouchTypes, [direct])
        XCTAssertEqual(canvas.pinchGestureRecognizer?.allowedTouchTypes, [direct])
    }

    // MARK: - Selection

    func testAFingerTapOnInkSelectsItAndATapOnBarePaperPutsItDown() throws {
        let f = try makeFixture()
        f.session.tools.select(.pen)
        try write(f.board.debugStrokes(extra: 0), on: f)
        // The first stroke runs level through the centre.
        let onInk = f.host.screenPoint(ofCanvas: CGPoint(x: f.centre.x + 40, y: f.centre.y))
        f.board.selectionTap(at: onInk)
        XCTAssertEqual(f.page?.selectionKind, .ink)
        XCTAssertEqual(f.session.tools.active, .lasso, "a selection picks the lasso")
        XCTAssertFalse(f.host.canvas.drawingGestureRecognizer.isEnabled)

        let bare = f.host.screenPoint(ofCanvas: CGPoint(x: f.centre.x, y: f.centre.y - 300))
        f.board.selectionTap(at: bare)
        XCTAssertNil(f.page?.selectionKind)
    }

    func testAFingerOnTheSelectionMovesIt() throws {
        let f = try makeFixture()
        try write([BoardViewController.debugWord(at: f.centre, width: 200, amplitude: 2)], on: f)
        let before = try XCTUnwrap(f.page?.drawing?.bounds)
        let start = f.host.screenPoint(ofCanvas: CGPoint(x: f.centre.x + 100, y: f.centre.y))
        f.board.selectionTap(at: start)
        XCTAssertEqual(f.page?.selectionKind, .ink)
        XCTAssertTrue(f.board.lassoShouldBegin(at: start, touchType: .direct))
        f.board.routeLassoTouch(.began(start))
        f.board.routeLassoTouch(.moved(CGPoint(x: start.x + 60, y: start.y + 90)))
        f.board.routeLassoTouch(.ended)
        let after = try XCTUnwrap(f.page?.drawing?.bounds)
        // At 100 %, one screen point is one board point.
        XCTAssertEqual(after.minX - before.minX, 60, accuracy: 1)
        XCTAssertEqual(after.minY - before.minY, 90, accuracy: 1)
    }

    /// Runs the key command for `input` as the keyboard would; false when the
    /// board offers none.
    @discardableResult
    private func press(
        _ input: String, _ modifiers: UIKeyModifierFlags = .command, on f: Fixture
    ) -> Bool {
        guard
            let action = f.board.keyCommands?
                .first(where: { $0.input == input && $0.modifierFlags == modifiers })?.action
        else { return false }
        _ = f.board.perform(action)
        return true
    }

    /// The selection keys are the notebook's: Escape puts ink down with the
    /// Lasso active, Cmd-C copies, Cmd-X cuts as one undo step, and Read Only
    /// keeps Cmd-C alone.
    func testTheSelectionKeys() throws {
        let f = try makeFixture()
        f.session.tools.select(.pen)
        try write([BoardViewController.debugWord(at: f.centre, width: 200, amplitude: 2)], on: f)
        let onInk = f.host.screenPoint(ofCanvas: CGPoint(x: f.centre.x + 100, y: f.centre.y))
        f.board.selectionTap(at: onInk)
        XCTAssertEqual(f.page?.selectionKind, .ink)
        XCTAssertTrue(press(UIKeyCommand.inputEscape, [], on: f))
        XCTAssertNil(f.page?.selectionKind, "Escape left the ink selected")

        UIPasteboard.general.items = []
        f.board.selectionTap(at: onInk)
        XCTAssertTrue(press("c", on: f))
        XCTAssertNotNil(PaperClip.read(), "Cmd-C put nothing on the pasteboard")
        XCTAssertEqual(f.page?.drawing?.strokes.count, 1)

        UIPasteboard.general.items = []
        XCTAssertTrue(press("x", on: f))
        XCTAssertNotNil(PaperClip.read(), "Cmd-X put nothing on the pasteboard")
        XCTAssertEqual(f.host.canvas.drawing.strokes.count, 0, "Cmd-X left the ink")
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        f.host.canvas.undoManager?.undo()
        XCTAssertEqual(f.host.canvas.drawing.strokes.count, 1, "one undo did not bring it back")

        XCTAssertTrue(press("v", on: f))
        XCTAssertEqual(f.host.canvas.drawing.strokes.count, 2, "Cmd-V did not paste")

        f.board.setReadOnly(true)
        XCTAssertTrue(press("c", on: f))
        XCTAssertFalse(press("x", on: f))
        XCTAssertFalse(press("v", on: f))
        XCTAssertFalse(press(UIKeyCommand.inputEscape, [], on: f))
    }

    // MARK: - Limits

    func testANearlyFullBoardWarnsAndAFullOneStopsTheInkButNotTheEraser() throws {
        let f = try makeFixture()
        let ref = try XCTUnwrap(f.ref)
        f.session.tools.select(.pen)
        try write(f.board.debugStrokes(extra: BoardLimits.warningItems), on: f)
        _ = f.board.drawingData(for: ref)
        XCTAssertEqual(f.board.limitState, .nearlyFull)
        XCTAssertFalse(f.board.limitBanner.isHidden)
        XCTAssertTrue(f.host.canvas.drawingGestureRecognizer.isEnabled)

        try write(
            f.board.debugStrokes(extra: BoardLimits.fullItems - BoardLimits.warningItems), on: f)
        _ = f.board.drawingData(for: ref)
        XCTAssertEqual(f.board.limitState, .full)
        XCTAssertFalse(f.board.limitBanner.addButton.isHidden, "a full board offers Add Board")
        XCTAssertFalse(f.host.canvas.drawingGestureRecognizer.isEnabled, "the pen still inks")
        f.session.tools.select(.eraser)
        XCTAssertTrue(f.host.canvas.drawingGestureRecognizer.isEnabled, "the eraser stopped")
    }

    // MARK: - Read Only

    func testReadOnlyLocksInkAndSelectionAndLeavesPanAndZoom() throws {
        let f = try makeFixture()
        f.session.tools.select(.lasso)
        f.board.setReadOnly(true)
        XCTAssertTrue(f.session.chrome.isReadOnly)
        XCTAssertFalse(f.host.canvas.drawingGestureRecognizer.isEnabled)
        XCTAssertFalse(f.board.objectTap.isEnabled)
        XCTAssertEqual(f.board.lassoGesture?.isEnabled, false)
        XCTAssertFalse(f.board.undoTap.isEnabled)
        XCTAssertFalse(f.page?.longPress.isEnabled ?? true)
        XCTAssertTrue(f.host.canvas.panGestureRecognizer.isEnabled)
        f.board.zoom(.percent(50), animated: false)
        XCTAssertEqual(f.host.zoom, 0.5, accuracy: 0.0001)

        f.board.setReadOnly(false)
        XCTAssertTrue(f.board.objectTap.isEnabled)
        XCTAssertEqual(f.board.lassoGesture?.isEnabled, true)
    }

    // MARK: - Boards

    func testAddBoardShowsTheNewBoardCentredAndTheOldOneKeepsItsPlace() throws {
        let f = try makeFixture()
        let first = try XCTUnwrap(f.session.pages.first)
        f.host.place(center: CGPoint(x: 7_000, y: 7_500), zoom: 2)
        f.board.addPage(.last, template: nil)
        XCTAssertEqual(f.session.pages.count, 2)
        XCTAssertEqual(f.session.currentIndex, 1)
        XCTAssertEqual(f.page?.page.id, f.session.pages.last?.id)
        XCTAssertEqual(f.page?.page.title, "Board 2")
        XCTAssertEqual(f.host.zoom, 1, accuracy: 0.0001)
        XCTAssertEqual(f.host.viewportCenter.x, f.centre.x, accuracy: 0.5)

        f.session.show(pageAt: 0)
        XCTAssertEqual(f.page?.page.id, first.id)
        XCTAssertEqual(f.host.zoom, 2, accuracy: 0.0001)
        XCTAssertEqual(f.host.viewportCenter.x, 7_000, accuracy: 0.5)
    }

    func testTheBackgroundAndThePaperChangePerBoard() throws {
        let f = try makeFixture()
        f.board.setPageStyle(template: .dotted, paperColor: .dark, at: 0)
        XCTAssertEqual(f.page?.boardBackground?.template, .dotted)
        XCTAssertEqual(f.page?.boardBackground?.paperColor, .dark)
        XCTAssertEqual(f.host.canvas.overrideUserInterfaceStyle, .dark, "the dark rule")
        XCTAssertEqual(f.session.pages.first?.paperColor, .dark)
    }

    // MARK: - Saving

    func testInkReachesTheDiskThroughTheCopyEncodedAhead() throws {
        let f = try makeFixture()
        let ref = try XCTUnwrap(f.ref)
        try write(f.board.debugStrokes(extra: 0), on: f)
        f.board.encodeAhead()
        XCTAssertTrue(
            waitFor { f.board.encoded?.generation == f.board.drawingGeneration },
            "the ink was never encoded off the main thread")
        let encoded = try XCTUnwrap(f.board.encoded)
        XCTAssertEqual(f.board.drawingData(for: ref), encoded.data, "the flush encoded it again")
        f.controller.autosave.flushAll()
        let data = try XCTUnwrap(
            f.store.drawingData(notebookPath: ref.notebookPath, pageID: ref.pageID))
        XCTAssertEqual(try PKDrawing(data: data).strokes.count, f.page?.drawing?.strokes.count)

        // A stroke after the encode is not lost to a stale copy.
        try write([BoardViewController.debugWord(at: f.centre, width: 50)], on: f)
        let fresh = try XCTUnwrap(f.board.drawingData(for: ref))
        XCTAssertEqual(try PKDrawing(data: fresh).strokes.count, f.page?.drawing?.strokes.count)
    }

    func testClearBoardIsOneUndoStep() throws {
        let f = try makeFixture()
        try write(f.board.debugStrokes(extra: 0), on: f)
        let count = f.page?.drawing?.strokes.count ?? 0
        f.board.clearPage(at: 0)
        XCTAssertEqual(f.page?.drawing?.strokes.count, 0)
        XCTAssertTrue(f.session.chrome.canUndo)
        f.board.undo()
        XCTAssertEqual(f.page?.drawing?.strokes.count, count)
    }

    func testTheMinimapToggleIsRemembered() throws {
        let f = try makeFixture()
        let shown = f.board.isMinimapVisible
        f.board.toggleMinimap()
        XCTAssertEqual(f.board.isMinimapVisible, !shown)
        XCTAssertEqual(f.board.minimap.isHidden, shown)
        XCTAssertEqual(
            UserDefaults.standard.object(forKey: BoardViewController.minimapVisibleKey) as? Bool,
            !shown)
        XCTAssertEqual(f.board.zoomPill.minimapButton.accessibilityValue, shown ? "hidden" : "shown")
    }
}
