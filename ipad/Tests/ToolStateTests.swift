import PaperCore
import PencilKit
import XCTest

@testable import EXSCALIA
@testable import PaperUI

/// The window's tools: PaperCore's `ToolSettings`, saved on every change and
/// translated into PencilKit. The rules themselves are tested in PaperCore;
/// what matters here is that what the menu shows is what the canvas gets, and
/// that the pen a person picked is the pen they get back tomorrow.
@MainActor
final class ToolStateTests: XCTestCase {
    private var scratch: ScratchDefaults?
    private var defaults = UserDefaults.standard

    override func setUp() {
        super.setUp()
        do {
            let scratch = try ScratchDefaults("ToolStateTests")
            self.scratch = scratch
            defaults = scratch.defaults
        } catch {
            XCTFail("could not make a defaults suite")
        }
    }

    override func tearDown() {
        scratch?.remove()
        scratch = nil
        super.tearDown()
    }

    func testAFreshInstallStartsWithTheFountainPen() throws {
        let tools = ToolState(defaults: defaults)
        XCTAssertEqual(tools.settings, ToolSettings.factory)
        let ink = try XCTUnwrap(tools.pkTool as? PKInkingTool)
        XCTAssertEqual(ink.inkType, .fountainPen)
        XCTAssertEqual(ink.width, 5, accuracy: 0.0001)
    }

    func testChangesSurviveARestart() {
        let first = ToolState(defaults: defaults)
        first.select(.highlighter)
        first.setWidth(31, for: .highlighter)
        first.selectColor(2, for: .highlighter)
        first.addColor("#336699", for: .pen)
        first.setDock(.leading)

        let second = ToolState(defaults: defaults)
        XCTAssertEqual(second.settings, first.settings)
        XCTAssertEqual(second.active, .highlighter)
        XCTAssertEqual(second.settings.width(for: .highlighter), 31)
        XCTAssertEqual(second.settings.color(for: .pen), "#336699")
        XCTAssertEqual(second.settings.dock, .leading)
    }

    /// Was the raw-key clamp test: a width an older build stored, out of
    /// range, is migrated into three clamped slots and the old key goes.
    func testAnOlderBuildsWidthIsMigratedAndClamped() throws {
        defaults.set(9_999.0, forKey: "paper.tool.penWidth")
        let tools = ToolState(defaults: defaults)
        XCTAssertEqual(tools.settings.slots[.pen]?.widths, [6, 12, 12])
        XCTAssertEqual(tools.settings.penStyle, .brush)
        XCTAssertNil(defaults.object(forKey: "paper.tool.penWidth"))
        let ink = try XCTUnwrap(tools.pkTool as? PKInkingTool)
        XCTAssertEqual(ink.inkType, .pen)
        XCTAssertEqual(ink.width, 12, accuracy: 0.0001)
    }

    func testTheCanvasGetsTheInkOfEachTool() throws {
        let tools = ToolState(defaults: defaults)
        func ink() throws -> PKInkingTool { try XCTUnwrap(tools.pkTool as? PKInkingTool) }

        tools.selectPenStyle(.ball)
        XCTAssertEqual(try ink().inkType, .monoline)
        tools.selectPenStyle(.brush)
        XCTAssertEqual(try ink().inkType, .pen)
        tools.select(.pencil)
        XCTAssertEqual(try ink().inkType, .pencil)
        tools.select(.highlighter)
        XCTAssertEqual(try ink().inkType, .marker)
        tools.select(.drawShape)
        XCTAssertEqual(try ink().inkType, .monoline)

        tools.setColor("#c0392b", for: .drawShape)
        tools.setWidth(5, for: .drawShape)
        XCTAssertEqual(try ink().width, 5, accuracy: 0.0001)
        XCTAssertEqual(ToolState.hex(try ink().color), "#c0392b")

        // The lasso and the insert tools leave the last writing tool's ink.
        tools.select(.lasso)
        XCTAssertEqual(try ink().inkType, .monoline)
    }

    func testTheEraserFollowsItsModeAndSize() throws {
        let tools = ToolState(defaults: defaults)
        tools.select(.eraser)
        tools.setWidth(40, for: .eraser)
        var eraser = try XCTUnwrap(tools.pkTool as? PKEraserTool)
        XCTAssertEqual(eraser.eraserType, .fixedWidthBitmap)
        XCTAssertEqual(eraser.width, 40, accuracy: 0.0001)

        tools.setEraserMode(.stroke)
        eraser = try XCTUnwrap(tools.pkTool as? PKEraserTool)
        XCTAssertEqual(eraser.eraserType, .vector)
    }

    func testTappingTheSelectedToolShowsAndHidesTheMenu() {
        let tools = ToolState(defaults: defaults)
        XCTAssertTrue(tools.settings.showsToolMenu)
        tools.tap(.writing)
        XCTAssertFalse(tools.settings.showsToolMenu)
        tools.tap(.writing)
        XCTAssertTrue(tools.settings.showsToolMenu)
        tools.tap(.eraser)
        XCTAssertEqual(tools.active, .eraser)
        tools.tap(.lasso)
        XCTAssertEqual(tools.active, .lasso)
        XCTAssertFalse(tools.settings.showsToolMenu, "the lasso has no menu")
    }

    func testObserversHearEachChangeOnceAndNothingThatChangesNothing() {
        let tools = ToolState(defaults: defaults)
        var heard: [(PaperTool, PaperTool)] = []
        let id = tools.addObserver { old, new in heard.append((old.active, new.active)) }
        tools.select(.eraser)
        tools.select(.eraser)
        tools.selectWidth(1, for: .eraser)
        XCTAssertEqual(heard.count, 1)
        XCTAssertEqual(heard.first?.0, .pen)
        XCTAssertEqual(heard.first?.1, .eraser)

        tools.removeObserver(id)
        tools.select(.pen)
        XCTAssertEqual(heard.count, 1)
    }

    func testAPickedColourIsStoredAsSRGBHex() {
        XCTAssertEqual(ToolState.hex(UIColor(red: 1, green: 0, blue: 0, alpha: 1)), "#ff0000")
        XCTAssertEqual(ToolState.hex(.black), "#000000")
        XCTAssertEqual(ToolState.hex(UIColor(white: 1, alpha: 1)), "#ffffff")
        // Display P3 red is outside sRGB; it comes back as the nearest.
        XCTAssertEqual(ToolState.hex(UIColor(displayP3Red: 1, green: 0, blue: 0, alpha: 1)), "#ff0000")
    }

    func testBlackInkPreviewsLightOnDarkPaper() {
        var white: CGFloat = 0
        ToolState.displayColor("#1c1c1e", onDarkPaper: true).getWhite(&white, alpha: nil)
        XCTAssertGreaterThan(white, 0.5)
        ToolState.displayColor("#1c1c1e", onDarkPaper: false).getWhite(&white, alpha: nil)
        XCTAssertLessThan(white, 0.2)
    }

    func testTheLassoColourMenuStillOffersTheEightSwatches() {
        XCTAssertEqual(
            ToolState.penColors.map(\.name),
            ["Black", "Graphite", "Blue", "Indigo", "Teal", "Green", "Amber", "Red"])
    }

    /// The settings screen used to write a comma-joined string that
    /// recognition, reading an array, never saw.
    func testTheLanguagesTheSettingsScreenWritesAreTheOnesRecognitionReads() {
        ServerSettingsView.storeRecognitionLanguages(["de-DE", "en-US"], defaults: defaults)
        XCTAssertEqual(PaperController.recognitionLanguages(defaults: defaults), ["de-DE", "en-US"])
    }
}

/// The tool runtime on a live notebook: Draw Shape, Clear Page, the zoom
/// double-tap, the Text tool and a tool switch under a lasso drag, driven
/// through the controller's own entry points against a real `PKCanvasView` and
/// the window's undo manager.
@MainActor
final class ToolRuntimeTests: XCTestCase {
    @MainActor
    private struct Fixture {
        let tools: ToolState
        let controller: PaperController
        let notebookController: NotebookViewController
        let page: PageView
        let canvas: PKCanvasView
        let undo: UndoManager
        let ref: PageRef
    }

    func testARoughRectangleBecomesOneCleanStrokeAndOneUndoBringsItBack() throws {
        let f = try makeFixture()
        f.tools.select(.drawShape)
        let rough = roughStroke("rectangle", f)
        f.canvas.drawing = PKDrawing(strokes: [rough])
        f.undo.removeAllActions()

        let shape = f.notebookController.cleanUpShape(on: f.page, strokesBefore: 0)
        XCTAssertEqual(shape?.kind, .rectangle)
        XCTAssertEqual(f.canvas.drawing.strokes.count, 1)
        let clean = try XCTUnwrap(f.canvas.drawing.strokes.first)
        // The start and end on one corner, the other three corners tripled.
        XCTAssertEqual(clean.path.count, 11)
        XCTAssertEqual(clean.ink.inkType, rough.ink.inkType)
        XCTAssertEqual(ToolState.hex(clean.ink.color), ToolState.hex(rough.ink.color))
        XCTAssertTrue(f.controller.autosave.isDirty(f.ref), "the clean shape was not saved")

        flushUndoGroup()
        XCTAssertEqual(f.undo.undoActionName, "Draw Shape")
        f.undo.undo()
        XCTAssertEqual(f.canvas.drawing.strokes.count, 1)
        XCTAssertEqual(f.canvas.drawing.strokes.first?.path.count, rough.path.count)
        f.undo.redo()
        XCTAssertEqual(f.canvas.drawing.strokes.first?.path.count, 11)
    }

    func testAnArrowBecomesTwoStrokes() throws {
        let f = try makeFixture()
        f.tools.select(.drawShape)
        f.canvas.drawing = PKDrawing(strokes: [roughStroke("arrow", f)])
        XCTAssertEqual(f.notebookController.cleanUpShape(on: f.page, strokesBefore: 0)?.kind, .arrow)
        XCTAssertEqual(f.canvas.drawing.strokes.count, 2)
    }

    func testAScribbleStaysAsDrawn() throws {
        let f = try makeFixture()
        let points = (0..<80).map { step in
            CGPoint(x: 200 + Double(step) * 4, y: 300 + sin(Double(step) * 0.9) * 40)
        }
        let scribble = stroke(points)
        f.canvas.drawing = PKDrawing(strokes: [scribble])
        XCTAssertNil(f.notebookController.cleanUpShape(on: f.page, strokesBefore: 0))
        XCTAssertEqual(f.canvas.drawing.strokes.first?.path.count, scribble.path.count)
    }

    func testOnlyTheNewStrokeIsCleanedUp() throws {
        let f = try makeFixture()
        f.canvas.drawing = PKDrawing(strokes: [roughStroke("rectangle", f)])
        // Nothing was added since the pen went down at one stroke.
        XCTAssertNil(f.notebookController.cleanUpShape(on: f.page, strokesBefore: 1))
    }

    func testClearPageRemovesTheInkInOneUndoStep() throws {
        let f = try makeFixture()
        f.canvas.drawing = PKDrawing(strokes: [roughStroke("triangle", f), roughStroke("line", f)])
        f.undo.removeAllActions()
        f.notebookController.clearPage(at: 0)
        XCTAssertTrue(f.canvas.drawing.strokes.isEmpty)
        flushUndoGroup()
        XCTAssertEqual(f.undo.undoActionName, "Clear Page")
        f.undo.undo()
        XCTAssertEqual(f.canvas.drawing.strokes.count, 2)
    }

    func testDoubleTapZoomsToTwiceAndBack() throws {
        let f = try makeFixture()
        let anchor = f.page.frame.origin.applying(CGAffineTransform(translationX: 100, y: 100))
        f.notebookController.toggleZoom(at: anchor)
        XCTAssertEqual(f.notebookController.zoom, 2, accuracy: 0.001)
        f.notebookController.toggleZoom(at: anchor)
        XCTAssertEqual(f.notebookController.zoom, 1, accuracy: 0.001)
    }

    func testInsertToolsKeepThePencilOffTheInk() throws {
        let f = try makeFixture()
        XCTAssertTrue(f.canvas.drawingGestureRecognizer.isEnabled)
        f.tools.select(.shape)
        XCTAssertFalse(f.canvas.drawingGestureRecognizer.isEnabled)
        f.tools.select(.pen)
        XCTAssertTrue(f.canvas.drawingGestureRecognizer.isEnabled)
    }

    func testTheTextToolPlacesABoxAndFallsBackToTheLassoWhenItIsPutDown() throws {
        let f = try makeFixture()
        f.tools.select(.text)
        f.tools.setWidth(24, for: .text)
        let stack = try XCTUnwrap(f.page.superview)
        let point = stack.convert(CGPoint(x: 120, y: 160), from: f.page)
        f.notebookController.placeText(at: point)
        let object = try XCTUnwrap(f.page.objects.objects.first)
        XCTAssertEqual(object.type, .text)
        XCTAssertEqual(object.fontSize, 24)
        XCTAssertEqual(f.tools.active, .text, "still typing")

        f.page.deselectObjects()
        XCTAssertEqual(f.tools.active, .lasso)
    }

    /// A tool switch goes through `setLassoActive(false)`, which puts a drag
    /// in progress back where it was picked up.
    func testSwitchingToolsMidDragPutsTheSelectionBack() throws {
        let f = try makeFixture()
        f.canvas.drawing = PKDrawing(strokes: [roughStroke("rectangle", f)])
        let before = f.canvas.drawing
        f.tools.select(.lasso)
        let stack = try XCTUnwrap(f.page.superview)
        let ink = before.bounds
        func onScreen(_ point: CGPoint) -> CGPoint {
            stack.convert(CGPoint(x: point.x * f.page.zoom, y: point.y * f.page.zoom), from: f.page)
        }
        let radius = max(ink.width, ink.height) / 2 + 30
        f.notebookController.routeLassoTouch(.began(onScreen(CGPoint(x: ink.midX + radius, y: ink.midY))))
        for step in 1...48 {
            let angle = Double(step) / 48 * 2 * .pi
            f.notebookController.routeLassoTouch(
                .moved(onScreen(CGPoint(x: ink.midX + cos(angle) * radius, y: ink.midY + sin(angle) * radius))))
        }
        f.notebookController.routeLassoTouch(.ended)
        let lasso = try XCTUnwrap(f.page.lasso)
        XCTAssertEqual(lasso.selection, [0])

        let start = onScreen(CGPoint(x: ink.midX, y: ink.midY))
        f.notebookController.routeLassoTouch(.began(start))
        f.notebookController.routeLassoTouch(.moved(CGPoint(x: start.x + 80, y: start.y + 80)))
        XCTAssertTrue(lasso.isLifted)

        f.tools.select(.pen)
        XCTAssertNil(lasso.drag)
        XCTAssertEqual(f.canvas.drawing.dataRepresentation(), before.dataRepresentation())
    }

    // MARK: - Helpers

    /// Lets the run loop close the undo group the last edit opened, as the
    /// end of a touch does on the iPad.
    private func flushUndoGroup() {
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
    }

    private func roughStroke(_ name: String, _ f: Fixture) -> PKStroke {
        stroke(NotebookViewController.debugRoughShape(name))
    }

    private func stroke(_ points: [CGPoint]) -> PKStroke {
        PKStroke(
            ink: PKInk(.monoline, color: UIColor(red: 0.1, green: 0.4, blue: 0.8, alpha: 1)),
            path: PKStrokePath(
                controlPoints: points.enumerated().map { index, point in
                    PKStrokePoint(
                        location: point, timeOffset: Double(index) * 0.01,
                        size: CGSize(width: 3, height: 3), opacity: 1, force: 1, azimuth: 0,
                        altitude: .pi / 2)
                },
                creationDate: Date()))
    }

    private func makeFixture() throws -> Fixture {
        let root = FileManager.default.temporaryDirectory
            .appending(path: "ToolRuntimeTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }

        let store = try PaperStore(root: root)
        let defaults = try scratchDefaults("ToolRuntimeTests")
        let tools = ToolState(defaults: defaults)
        let controller = PaperController(store: store, tools: tools)
        let notebook = try store.createNotebook(title: "Tools", size: .a4)
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
        let undo = try XCTUnwrap(canvas.undoManager, "the canvas has no undo manager")
        let ref = try XCTUnwrap(session.ref(at: 0))
        undo.removeAllActions()
        return Fixture(
            tools: tools, controller: controller, notebookController: notebookController,
            page: page, canvas: canvas, undo: undo, ref: ref)
    }
}
