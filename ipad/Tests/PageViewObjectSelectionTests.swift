import PaperCore
import PencilKit
import XCTest

@testable import PaperUI

/// Selection first: a finger tap selects whatever it lands on -- a word of
/// ink, a text box, an image, a shape -- through `HitTesting`, and the object
/// menu appears at once; a tap on bare paper puts it down. The Shapes and
/// Text tools place their objects with a tap, the page follows the dark rule,
/// and every edit from the menu is one undo step.
@MainActor
final class PageViewObjectSelectionTests: XCTestCase {
    /// Held here because the page keeps its delegate weakly.
    private var host: StubPageViewDelegate?

    // MARK: - Fixtures

    private func makeLivePage(page: Page = Page(), tool: PaperTool = .lasso) throws -> PageView {
        let defaults = try scratchDefaults("PageViewObjectSelectionTests")
        let host = StubPageViewDelegate(tools: ToolState(defaults: defaults))
        host.tools.select(tool)
        self.host = host
        let view = PageView(page: page, notebookPath: "paper/T.paper")
        view.delegate = host
        // In a window, so the canvas has the window's undo manager.
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 1024, height: 1366))
        window.addSubview(view)
        window.makeKeyAndVisible()
        addTeardownBlock { @MainActor in
            window.isHidden = true
            view.removeFromSuperview()
        }
        view.goLive(drawingData: nil)
        XCTAssertTrue(view.isLive)
        view.setLassoActive(tool == .lasso)
        view.layoutIfNeeded()
        return view
    }

    private func textObject(x: Double = 100, y: Double = 100) -> PageObject {
        PageObject(
            type: .text, frame: PageObject.Frame(x: x, y: y, width: 200, height: 60), text: "x")
    }

    private func imageObject() -> PageObject {
        PageObject(
            type: .image, frame: PageObject.Frame(x: 100, y: 400, width: 120, height: 80),
            asset: "paper/T.paper/assets/none.png")
    }

    private func shapeObject(_ kind: ShapeKind, frame: PageObject.Frame) -> PageObject {
        ShapeStyle(kind: kind).object(frame: frame)
    }

    /// A word's stroke: a wave from (x, y), 60 points long.
    private static func stroke(x: CGFloat, y: CGFloat, length: CGFloat = 60) -> PKStroke {
        let points = stride(from: 0.0, through: Double(length), by: 4.0).map { offset in
            PKStrokePoint(
                location: CGPoint(x: x + CGFloat(offset), y: y + CGFloat(sin(offset / 10) * 8)),
                timeOffset: offset / 100, size: CGSize(width: 3, height: 3), opacity: 1,
                force: 1, azimuth: 0, altitude: .pi / 2)
        }
        return PKStroke(
            ink: PKInk(.pen, color: .black),
            path: PKStrokePath(controlPoints: points, creationDate: Date()))
    }

    private func put(_ strokes: [PKStroke], on view: PageView) {
        view.applyDrawing(PKDrawing(strokes: strokes))
    }

    /// A point of a page, in page points, as a tap on the view reports it.
    private func tap(_ view: PageView, _ point: CGPoint) {
        view.handleObjectTap(at: CGPoint(x: point.x * view.zoom, y: point.y * view.zoom))
    }

    // MARK: - A tap selects each kind

    func testATapOnInkSelectsTheWordAndShowsTheInkMenu() throws {
        let view = try makeLivePage()
        // Two strokes of one word, and a line further down.
        put([Self.stroke(x: 100, y: 200), Self.stroke(x: 162, y: 200), Self.stroke(x: 100, y: 400)], on: view)

        tap(view, CGPoint(x: 100, y: 200))

        XCTAssertEqual(view.selectionKind, .ink)
        XCTAssertEqual(view.lasso?.selection, [0, 1], "the word was not selected whole")
        let menu = try XCTUnwrap(view.lasso?.accessibleInspector as? ObjectMenu, "no menu")
        XCTAssertEqual(menu.kind, .ink)
        XCTAssertTrue(menu.glyphLabels.contains("Duplicate"))
        XCTAssertTrue(menu.glyphLabels.contains("Convert to Text"))
        XCTAssertEqual(host?.selected, [.ink])
    }

    func testATapOnEachObjectKindSelectsIt() throws {
        let text = textObject()
        let image = imageObject()
        let shape = shapeObject(.ellipse, frame: PageObject.Frame(x: 350, y: 100, width: 160, height: 120))
        let view = try makeLivePage(page: Page(objects: [text, image, shape]))

        for (object, kind) in [(text, ObjectMenuKind.text), (image, .image), (shape, .shape)] {
            let frame = object.frame.rect
            tap(view, CGPoint(x: frame.midX, y: frame.midY))
            XCTAssertEqual(view.objects.selectedID, object.id, "\(kind) was not selected")
            XCTAssertEqual(view.selectionKind, kind)
            XCTAssertFalse(view.objects.menu.isHidden, "no menu for \(kind)")
            XCTAssertEqual(view.objects.menu.kind, kind)
            XCTAssertEqual(view.canvas?.isUserInteractionEnabled, false)
            XCTAssertTrue(view.objects.isUserInteractionEnabled)
            XCTAssertEqual(view.lasso?.isUserInteractionEnabled, false, "the overlay hides the handles")
        }
        XCTAssertEqual(host?.selected, [.text, .image, .shape])
    }

    func testALineIsHitOnItsLineNotAcrossItsBox() throws {
        let line = shapeObject(.arrow, frame: PageObject.Frame(x: 300, y: 300, width: -200, height: 100))
        let view = try makeLivePage(page: Page(objects: [line]))

        // Inside the box, far from the line.
        tap(view, CGPoint(x: 120, y: 310))
        XCTAssertNil(view.selectionKind)
        // On the line, which runs from (300, 300) to (100, 400).
        tap(view, CGPoint(x: 200, y: 350))
        XCTAssertEqual(view.selectionKind, .shape)
    }

    func testATapOnBarePaperPutsTheSelectionDown() throws {
        let view = try makeLivePage(page: Page(objects: [textObject()]))
        put([Self.stroke(x: 100, y: 500)], on: view)

        tap(view, CGPoint(x: 200, y: 130))
        XCTAssertEqual(view.selectionKind, .text)
        tap(view, CGPoint(x: 600, y: 800))
        XCTAssertNil(view.selectionKind)
        XCTAssertEqual(view.canvas?.isUserInteractionEnabled, true)
        XCTAssertFalse(view.objects.isUserInteractionEnabled)

        tap(view, CGPoint(x: 100, y: 500))
        XCTAssertEqual(view.selectionKind, .ink)
        // Ink and an object are never selected together by taps.
        tap(view, CGPoint(x: 200, y: 130))
        XCTAssertEqual(view.selectionKind, .text)
        XCTAssertEqual(view.lasso?.selection, [])
        tap(view, CGPoint(x: 600, y: 800))
        XCTAssertNil(view.selectionKind)
    }

    /// The Pencil with a writing tool writes over an object: the canvas is
    /// above the objects and keeps its touches until a finger selects one.
    func testWithAWritingToolTheCanvasKeepsThePencil() throws {
        let view = try makeLivePage(page: Page(objects: [textObject()]), tool: .pen)
        XCTAssertEqual(view.canvas?.isUserInteractionEnabled, true)
        XCTAssertFalse(view.objects.isUserInteractionEnabled)
        tap(view, CGPoint(x: 200, y: 130))
        XCTAssertEqual(view.selectionKind, .text)
        // Picking a tool other than the lasso puts the selection down.
        view.setLassoActive(false)
        XCTAssertNil(view.selectionKind)
        XCTAssertEqual(view.canvas?.isUserInteractionEnabled, true)
    }

    func testAPageAnotherWindowIsWritingIgnoresTaps() throws {
        let view = try makeLivePage(page: Page(objects: [textObject()]))
        view.setExternallyEditing(true)
        tap(view, CGPoint(x: 200, y: 130))
        XCTAssertNil(view.selectionKind)
        view.setExternallyEditing(false)
        tap(view, CGPoint(x: 200, y: 130))
        XCTAssertEqual(view.selectionKind, .text)
    }

    // MARK: - Insert taps

    func testTheShapesToolPlacesEachKindWhereItIsTapped() throws {
        for kind in ShapeKind.allCases {
            let view = try makeLivePage(tool: .shape)
            host?.tools.setShapeKind(kind)
            tap(view, CGPoint(x: 300, y: 400))

            let shape = try XCTUnwrap(view.objects.objects.first, "no \(kind) was placed")
            XCTAssertEqual(shape.type, .shape)
            XCTAssertEqual(shape.frame, PageObject.Frame(x: 220, y: 340, width: 160, height: 120))
            let style = ShapeStyle(shape)
            XCTAssertEqual(style.kind, kind)
            XCTAssertEqual(style.strokeWidth, host?.tools.settings.width(for: .shape))
            XCTAssertEqual(style.stroke, host?.tools.settings.color(for: .shape))
            XCTAssertNil(style.fill)
            XCTAssertEqual(view.selectionKind, .shape, "the placed \(kind) is not selected")
            XCTAssertEqual(host?.placed.last, .shape)
            let drawn = try XCTUnwrap(view.objects.views[shape.id] as? ShapeObjectView)
            XCTAssertNotNil(drawn.displayedPath, "the \(kind) draws nothing")
        }
    }

    func testFillOnFillsANewShapeInItsStrokeColour() throws {
        let view = try makeLivePage(tool: .shape)
        host?.tools.setShapeFill(true)
        tap(view, CGPoint(x: 300, y: 400))
        let style = ShapeStyle(try XCTUnwrap(view.objects.objects.first))
        XCTAssertEqual(style.fill, style.stroke)
    }

    func testADragKeepsALinesDirection() {
        let frame = ShapeStyle.dragFrame(
            from: CGPoint(x: 300, y: 100), to: CGPoint(x: 100, y: 250), kind: .line)
        XCTAssertEqual(frame, PageObject.Frame(x: 300, y: 100, width: -200, height: 150))
        let box = ShapeStyle.dragFrame(
            from: CGPoint(x: 300, y: 100), to: CGPoint(x: 100, y: 250), kind: .rectangle)
        XCTAssertEqual(box, PageObject.Frame(x: 100, y: 100, width: 200, height: 150))
    }

    func testTheTextToolPlacesABoxAtTheTapAndPutsItDownOnTheNextTap() throws {
        let view = try makeLivePage(tool: .text)
        host?.tools.setWidth(24, for: .text)
        host?.tools.setColor("#c0392b", for: .text)
        tap(view, CGPoint(x: 120, y: 300))

        let box = try XCTUnwrap(view.objects.objects.first)
        XCTAssertEqual(box.type, .text)
        XCTAssertEqual(box.fontSize, 24)
        XCTAssertEqual(box.frame.x, 120, accuracy: 0.5, "the box starts where it was tapped")
        XCTAssertEqual(box.additionalFields["color"], .string("#c0392b"))
        XCTAssertEqual(view.selectionKind, .text)

        // Bare paper again: the box is put down, not a second one placed.
        tap(view, CGPoint(x: 400, y: 700))
        XCTAssertEqual(view.objects.objects.count, 1)
        XCTAssertNil(view.selectionKind)
    }

    func testTheLongPressMenuOffersInsertTextAndPaste() throws {
        let view = try makeLivePage()
        UIPasteboard.general.items = []
        XCTAssertEqual(view.longPressActions(at: CGPoint(x: 100, y: 100)).map(\.title), ["Insert Text"])
        UIPasteboard.general.string = "pasted words"
        XCTAssertEqual(
            view.longPressActions(at: CGPoint(x: 100, y: 100)).map(\.title), ["Paste", "Insert Text"])
    }

    // MARK: - The menu's actions

    func testTextActionsAreEachOneUndoStep() throws {
        let view = try makeLivePage(page: Page(objects: [textObject(), textObject(x: 100, y: 300)]))
        let undo = try XCTUnwrap(view.canvas?.undoManager)
        undo.removeAllActions()
        let first = view.objects.objects[0]
        tap(view, CGPoint(x: 200, y: 130))

        // Each in a turn of its own, as taps on the menu are.
        view.objects.perform(.color("#1f6feb"))
        flushUndoGroup()
        XCTAssertEqual(view.objects.selectedObject?.additionalFields["color"], .string("#1f6feb"))
        XCTAssertEqual(undo.undoActionName, "Text Color")
        view.objects.perform(.size(32))
        flushUndoGroup()
        XCTAssertEqual(view.objects.selectedObject?.fontSize, 32)
        view.objects.perform(.bringToFront)
        flushUndoGroup()
        XCTAssertEqual(view.objects.objects.last?.id, first.id)
        view.objects.perform(.sendToBack)
        flushUndoGroup()
        XCTAssertEqual(view.objects.objects.first?.id, first.id)
        view.objects.perform(.duplicate)
        flushUndoGroup()
        XCTAssertEqual(view.objects.objects.count, 3)
        XCTAssertNotEqual(view.objects.selectedID, first.id, "the copy is the selection")

        undo.undo()
        XCTAssertEqual(view.objects.objects.count, 2, "the duplicate was not one undo step")
        XCTAssertEqual(view.objects.objects.first?.id, first.id, "undo went past the duplicate")
        undo.undo()
        XCTAssertEqual(view.objects.objects.last?.id, first.id, "Send to Back was not one step")
        view.objects.select(first.id)
        view.objects.perform(.delete)
        flushUndoGroup()
        XCTAssertEqual(view.objects.objects.count, 1)
        XCTAssertNil(view.selectionKind)
        undo.undo()
        XCTAssertEqual(view.objects.objects.count, 2)
        XCTAssertEqual(
            view.objects.objects.first { $0.id == first.id }?.additionalFields["color"],
            .string("#1f6feb"), "undo reached back past the delete")
    }

    func testShapeActionsChangeItsStyle() throws {
        let shape = shapeObject(.rectangle, frame: PageObject.Frame(x: 100, y: 100, width: 160, height: 120))
        let view = try makeLivePage(page: Page(objects: [shape]))
        tap(view, CGPoint(x: 180, y: 160))
        XCTAssertEqual(view.objects.menu.kind, .shape)

        view.objects.perform(.strokeColor("#c0392b"))
        view.objects.perform(.fill("#1f6feb"))
        view.objects.perform(.width(6))
        var style = ShapeStyle(try XCTUnwrap(view.objects.selectedObject))
        XCTAssertEqual(style.stroke, "#c0392b")
        XCTAssertEqual(style.fill, "#1f6feb")
        XCTAssertEqual(style.strokeWidth, 6)
        view.objects.perform(.fill(nil))
        style = ShapeStyle(try XCTUnwrap(view.objects.selectedObject))
        XCTAssertNil(style.fill)
        XCTAssertEqual(view.objects.selectedObject?.additionalFields["fill"], .null)
    }

    func testCutPutsTheObjectOnThePasteboardAndRemovesIt() throws {
        let view = try makeLivePage(page: Page(objects: [textObject()]))
        tap(view, CGPoint(x: 200, y: 130))
        view.objects.perform(.cut)
        XCTAssertTrue(view.objects.objects.isEmpty)
        let clip = try XCTUnwrap(PaperClip.read(), "Paper's copy is not on the pasteboard")
        XCTAssertEqual(clip.objects.map(\.text), ["x"])
        XCTAssertEqual(UIPasteboard.general.string, "x")
    }

    func testTheImageMenuTogglesCropAndScalesFromItsCorners() throws {
        let view = try makeLivePage(page: Page(objects: [imageObject()]))
        tap(view, CGPoint(x: 160, y: 440))
        let image = try XCTUnwrap(view.objects.selectedView as? ImageObjectView)
        XCTAssertFalse(image.offersEdgeHandles, "an image scales from its corners only")
        view.objects.perform(.crop)
        XCTAssertTrue(image.isCropping)
        XCTAssertTrue(view.objects.menu.context.isCropping)
        view.objects.perform(.crop)
        XCTAssertFalse(image.isCropping)
    }

    func testInkMenuActionsAreOneUndoStepEach() throws {
        let view = try makeLivePage()
        put([Self.stroke(x: 100, y: 200), Self.stroke(x: 100, y: 500)], on: view)
        let undo = try XCTUnwrap(view.canvas?.undoManager)
        undo.removeAllActions()
        tap(view, CGPoint(x: 100, y: 200))
        let lasso = try XCTUnwrap(view.lasso)
        XCTAssertEqual(lasso.selection, [0])

        lasso.menu.onAction?(.color("#c0392b"))
        flushUndoGroup()
        XCTAssertEqual(undo.undoActionName, "Recolour Selection")
        lasso.menu.onAction?(.duplicate)
        flushUndoGroup()
        XCTAssertEqual(view.canvas?.drawing.strokes.count, 3)
        XCTAssertEqual(lasso.selection, [2])
        lasso.menu.onAction?(.delete)
        flushUndoGroup()
        XCTAssertEqual(view.canvas?.drawing.strokes.count, 2)
        XCTAssertNil(view.selectionKind)
        undo.undo()
        XCTAssertEqual(view.canvas?.drawing.strokes.count, 3, "the delete was not one step")
        undo.undo()
        XCTAssertEqual(view.canvas?.drawing.strokes.count, 2, "the duplicate was not one step")
    }

    // MARK: - Dragging and resizing an object

    func testAnObjectDragIsOneUndoStep() throws {
        let view = try makeLivePage(page: Page(objects: [textObject()]))
        let undo = try XCTUnwrap(view.canvas?.undoManager)
        undo.removeAllActions()
        tap(view, CGPoint(x: 200, y: 130))
        let start = CGPoint(x: 200, y: 130)
        XCTAssertEqual(view.objects.interaction(at: start), .move)

        view.objects.beginInteraction(.move, at: start)
        for step in 1...10 {
            view.objects.updateInteraction(to: CGPoint(x: 200 + CGFloat(step) * 8, y: 130 + CGFloat(step) * 5))
        }
        XCTAssertTrue(view.objects.menu.isHidden, "the menu stays out of the way of a drag")
        view.objects.endInteraction(commit: true)

        let moved = try XCTUnwrap(view.objects.selectedObject)
        XCTAssertEqual(moved.frame.x, 180, accuracy: 0.01)
        XCTAssertEqual(moved.frame.y, 150, accuracy: 0.01)
        XCTAssertFalse(view.objects.menu.isHidden)
        XCTAssertEqual(undo.undoActionName, "Move Object")
        flushUndoGroup()
        undo.undo()
        XCTAssertEqual(view.objects.objects.first?.frame.x, 100)
        XCTAssertFalse(undo.canUndo, "the drag was more than one step")
    }

    func testACancelledObjectDragPutsItBack() throws {
        let view = try makeLivePage(page: Page(objects: [textObject()]))
        tap(view, CGPoint(x: 200, y: 130))
        view.objects.beginInteraction(.move, at: CGPoint(x: 200, y: 130))
        view.objects.updateInteraction(to: CGPoint(x: 300, y: 330))
        view.objects.endInteraction(commit: false)
        XCTAssertEqual(view.objects.objects.first?.frame.x, 100)
        XCTAssertEqual(view.objects.selectedView?.object.frame.x, 100)
    }

    func testCornersScaleProportionallyAndEdgesResizeOneSide() {
        let shape = shapeObject(.rectangle, frame: PageObject.Frame(x: 100, y: 100, width: 200, height: 100))
        let scaled = ObjectView.resized(
            shape, handle: .bottomRight, by: CGSize(width: 200, height: 0),
            minimum: CGSize(width: 8, height: 8))
        XCTAssertEqual(scaled.frame.x, 100)
        XCTAssertEqual(scaled.frame.width / scaled.frame.height, 2, accuracy: 0.001)
        XCTAssertEqual(scaled.frame.width, 360, accuracy: 0.001)

        let widened = ObjectView.resized(
            shape, handle: .right, by: CGSize(width: 50, height: 40), minimum: CGSize(width: 8, height: 8))
        XCTAssertEqual(widened.frame, PageObject.Frame(x: 100, y: 100, width: 250, height: 100))

        // A text box's type grows with it.
        let text = PageObject(
            type: .text, frame: PageObject.Frame(x: 0, y: 0, width: 100, height: 40), text: "x",
            fontSize: 20)
        let bigger = ObjectView.resized(
            text, handle: .bottomRight, by: CGSize(width: 100, height: 40), minimum: CGSize(width: 32, height: 24))
        XCTAssertEqual(bigger.fontSize ?? 0, 40, accuracy: 0.01)

        // A line keeps its direction.
        let line = shapeObject(.line, frame: PageObject.Frame(x: 300, y: 100, width: -200, height: 100))
        let longer = ObjectView.resized(
            line, handle: .bottomLeft, by: CGSize(width: -100, height: 50), minimum: CGSize(width: 8, height: 8))
        XCTAssertEqual(longer.frame.x, 300, accuracy: 0.01)
        XCTAssertEqual(longer.frame.width, -300, accuracy: 0.01)
        XCTAssertEqual(longer.frame.height, 150, accuracy: 0.01)
    }

    // MARK: - The dark rule

    func testAPageOnDarkPaperIsDrawnDark() throws {
        var page = Page(objects: [textObject()])
        page.paperColor = .dark
        let view = try makeLivePage(page: page)
        XCTAssertEqual(view.canvas?.overrideUserInterfaceStyle, .dark)
        XCTAssertEqual(view.template.paperColor, .dark)
        XCTAssertTrue(view.objects.isDarkPaper)
        XCTAssertEqual(view.lasso?.isDarkPaper, true)
        let text = try XCTUnwrap(view.objects.views[page.objects[0].id] as? TextObjectView)
        assertColor(text.shownColor, 0xF2 / 255, 0xF2 / 255, 0xF7 / 255)

        // Turned light, everything follows.
        page.paperColor = .cream
        view.update(page: page)
        XCTAssertEqual(view.canvas?.overrideUserInterfaceStyle, .light)
        XCTAssertEqual(view.template.paperColor, .cream)
        XCTAssertFalse(view.objects.isDarkPaper)
        assertColor(text.shownColor, 0x1C / 255, 0x1C / 255, 0x1E / 255)
    }

    func testAColouredTextOrShapeShowsAsInkDoesOnDarkPaper() throws {
        var text = textObject()
        text.additionalFields["color"] = .string("#1c1c1e")
        let mapped = try XCTUnwrap(PageRenderer.displayColor(hex: "#1c1c1e", dark: true))
        XCTAssertEqual(
            TextObjectView.displayColor(of: text, darkPaper: true).cgColor.components ?? [],
            UIColor(cgColor: mapped).cgColor.components ?? [])
        XCTAssertEqual(
            TextObjectView.displayColor(of: text, darkPaper: false).cgColor.components?.prefix(3).map {
                ($0 * 255).rounded()
            }, [0x1c, 0x1c, 0x1e])
    }

    func testAShapeViewDrawsTheRenderersPath() throws {
        let shape = shapeObject(.triangle, frame: PageObject.Frame(x: 50, y: 60, width: 160, height: 120))
        let view = try makeLivePage(page: Page(objects: [shape]))
        view.zoom = 2
        view.objects.layoutIfNeeded()
        let drawn = try XCTUnwrap(view.objects.views[shape.id] as? ShapeObjectView)
        drawn.layoutIfNeeded()
        let expected = try XCTUnwrap(PageRenderer.shapePath(for: shape))
        var transform = CGAffineTransform(scaleX: 2, y: 2).translatedBy(x: -50, y: -60)
        XCTAssertEqual(drawn.displayedPath?.boundingBox, expected.copy(using: &transform)?.boundingBox)
    }

    // MARK: - The menu

    func testTheMenuOffersTheContractsActionsForEachKind() {
        XCTAssertEqual(
            ObjectMenuKind.ink.items.map(\.label),
            ["Cut", "Copy", "Duplicate", "Delete", "Color", "Convert to Text", "Copy Link", "Ask OMNIA"])
        XCTAssertEqual(
            ObjectMenuKind.text.items.map(\.label),
            ["Edit", "Cut", "Copy", "Duplicate", "Delete", "Color", "Size", "Bring to Front", "Send to Back"])
        XCTAssertEqual(
            ObjectMenuKind.image.items.map(\.label),
            ["Cut", "Copy", "Duplicate", "Delete", "Crop", "Share", "Bring to Front", "Send to Back"])
        XCTAssertEqual(
            ObjectMenuKind.shape.items.map(\.label),
            [
                "Cut", "Copy", "Duplicate", "Delete", "Stroke Color", "Fill", "Width", "Bring to Front",
                "Send to Back",
            ])
        XCTAssertEqual(
            ObjectMenuKind.mixed.items.map(\.label), ["Cut", "Copy", "Duplicate", "Delete", "Ask OMNIA"])
        for kind in [ObjectMenuKind.ink, .text, .image, .shape, .mixed] {
            let menu = ObjectMenu()
            menu.configure(kind: kind)
            XCTAssertEqual(menu.glyphLabels.last, "More", "\(kind) has no overflow")
            XCTAssertTrue(Set(menu.glyphLabels.dropLast()).isSubset(of: kind.items.map(\.label)))
        }
    }

    func testTheMenuSitsAboveTheSelectionOrBelowWhenThereIsNoRoom() {
        let size = CGSize(width: 300, height: 48)
        let visible = CGRect(x: 0, y: 0, width: 800, height: 1000)
        let above = ObjectMenu.center(
            size: size, around: CGRect(x: 100, y: 400, width: 200, height: 100), visible: visible)
        XCTAssertLessThan(above.y + size.height / 2, 400)
        let below = ObjectMenu.center(
            size: size, around: CGRect(x: 100, y: 20, width: 200, height: 100), visible: visible)
        XCTAssertGreaterThan(below.y - size.height / 2, 120)
        let clamped = ObjectMenu.center(
            size: size, around: CGRect(x: 0, y: 400, width: 20, height: 20), visible: visible)
        XCTAssertGreaterThanOrEqual(clamped.x - size.width / 2, 0)
    }

    // MARK: - Helpers

    /// Lets the run loop close the undo group the last edit opened, as the
    /// end of a touch does on the iPad.
    private func flushUndoGroup() {
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
    }

    private func assertColor(
        _ color: UIColor?, _ red: CGFloat, _ green: CGFloat, _ blue: CGFloat,
        file: StaticString = #filePath, line: UInt = #line
    ) {
        var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
        guard let color, color.getRed(&r, green: &g, blue: &b, alpha: &a) else {
            return XCTFail("no colour", file: file, line: line)
        }
        XCTAssertEqual(r, red, accuracy: 0.01, file: file, line: line)
        XCTAssertEqual(g, green, accuracy: 0.01, file: file, line: line)
        XCTAssertEqual(b, blue, accuracy: 0.01, file: file, line: line)
    }
}

@MainActor
private final class StubPageViewDelegate: PageViewDelegate {
    let tools: ToolState
    var selected: [ObjectMenuKind] = []
    var placed: [PageObject.Kind] = []

    init(tools: ToolState) { self.tools = tools }

    func pageViewDrawingChanged(_ pageView: PageView) {}
    func pageViewDidBeginUsingTool(_ pageView: PageView) {}
    func pageViewDidEndUsingTool(_ pageView: PageView) {}
    func pageViewToolState(_ pageView: PageView) -> ToolState { tools }
    func pageView(_ pageView: PageView, didChangeSelection isEmpty: Bool) {}
    func pageView(_ pageView: PageView, didChangeObjects objects: [PageObject]) {}
    func pageView(_ pageView: PageView, imageForAsset assetPath: String) -> UIImage? { nil }
    func pageView(_ pageView: PageView, didDropImage data: Data, name: String, at point: CGPoint) {}
    func pageView(_ pageView: PageView, didDropPDF data: Data, name: String) {}
    func pageView(_ pageView: PageView, didSelect kind: ObjectMenuKind) { selected.append(kind) }
    func pageView(_ pageView: PageView, didPlace type: PageObject.Kind) { placed.append(type) }
}

/// The same, through the notebook: `selectionTap` (the finger tap's path) and
/// `routeLassoTouch` (the lasso recogniser's) against a real notebook on disk,
/// a live canvas and the window's undo manager.
@MainActor
final class SelectionFirstControllerTests: XCTestCase {
    @MainActor
    private struct Fixture {
        let store: PaperStore
        let controller: PaperController
        let tools: ToolState
        let session: NotebookSession
        let notebookController: NotebookViewController
        let page: PageView
        let canvas: PKCanvasView
        let stack: UIView
        let ref: PageRef
        let undo: UndoManager

        /// A page point as a recogniser reports it: stack coordinates.
        func onScreen(_ point: CGPoint) -> CGPoint {
            stack.convert(CGPoint(x: point.x * page.zoom, y: point.y * page.zoom), from: page)
        }

        func storedPage() throws -> Page {
            try store.page(notebookPath: ref.notebookPath, id: ref.pageID)
        }
    }

    /// A stroke along y = 240 from x = 200 to 400: easy to tap.
    private static func stroke(y: CGFloat = 240) -> PKStroke {
        let points = stride(from: 200.0, through: 400.0, by: 8.0).map { x in
            PKStrokePoint(
                location: CGPoint(x: x, y: Double(y)), timeOffset: x / 400,
                size: CGSize(width: 4, height: 4), opacity: 1, force: 1, azimuth: 0,
                altitude: .pi / 2)
        }
        return PKStroke(
            ink: PKInk(.pen, color: .black),
            path: PKStrokePath(controlPoints: points, creationDate: Date()))
    }

    private func makeFixture(tool: PaperTool = .pen) throws -> Fixture {
        let root = FileManager.default.temporaryDirectory
            .appending(path: "SelectionFirst-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        let store = try PaperStore(root: root)
        let defaults = try scratchDefaults("SelectionFirst")
        let tools = ToolState(defaults: defaults)
        tools.select(tool)
        let controller = PaperController(store: store, tools: tools)
        let notebook = try store.createNotebook(title: "Selection", size: .a4)
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
        let stack = try XCTUnwrap(page.superview)
        let ref = try XCTUnwrap(session.ref(at: 0))
        let undo = try XCTUnwrap(canvas.undoManager)
        canvas.drawing = PKDrawing(strokes: [Self.stroke()])
        notebookController.pageViewDrawingChanged(page)
        controller.autosave.flushAll()
        undo.removeAllActions()
        return Fixture(
            store: store, controller: controller, tools: tools, session: session,
            notebookController: notebookController, page: page, canvas: canvas, stack: stack,
            ref: ref, undo: undo)
    }

    private func addText(_ f: Fixture, at frame: PageObject.Frame) -> PageObject {
        let object = PageObject(type: .text, frame: frame, text: "words")
        f.page.insertObject(object, edit: false)
        f.page.deselectObjects()
        f.undo.removeAllActions()
        return object
    }

    private func flushUndoGroup() {
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
    }

    // MARK: - Taps

    func testAFingerTapOnInkSelectsItAndPicksTheLasso() throws {
        let f = try makeFixture(tool: .pen)
        f.notebookController.selectionTap(at: f.onScreen(CGPoint(x: 300, y: 242)))
        XCTAssertEqual(f.page.selectionKind, .ink)
        XCTAssertEqual(f.page.lasso?.selection, [0])
        XCTAssertEqual(f.tools.active, .lasso, "the toolbar did not go to the Lasso")
        XCTAssertNotNil(f.page.lasso?.accessibleInspector, "the menu did not appear at once")

        f.notebookController.selectionTap(at: f.onScreen(CGPoint(x: 300, y: 700)))
        XCTAssertNil(f.page.selectionKind)
        XCTAssertEqual(f.tools.active, .lasso)
    }

    func testAFingerDragOnATappedObjectMovesItAsOneUndoStep() throws {
        let f = try makeFixture()
        let text = addText(f, at: PageObject.Frame(x: 100, y: 500, width: 200, height: 60))
        let middle = CGPoint(x: 200, y: 530)
        f.notebookController.selectionTap(at: f.onScreen(middle))
        XCTAssertEqual(f.page.objects.selectedID, text.id)
        XCTAssertEqual(f.tools.active, .lasso)

        // A finger on the selected object is the lasso's, so the scroll waits.
        XCTAssertTrue(f.notebookController.lassoShouldBegin(at: f.onScreen(middle), touchType: .direct))
        XCTAssertFalse(
            f.notebookController.lassoShouldBegin(
                at: f.onScreen(CGPoint(x: 500, y: 800)), touchType: .direct))

        f.notebookController.routeLassoTouch(.began(f.onScreen(middle)))
        for step in 1...12 {
            f.notebookController.routeLassoTouch(
                .moved(f.onScreen(CGPoint(x: middle.x + CGFloat(step) * 5, y: middle.y + CGFloat(step) * 10))))
        }
        f.notebookController.routeLassoTouch(.ended)

        XCTAssertEqual(f.page.objects.objects.first?.frame.x ?? 0, 160, accuracy: 0.5)
        XCTAssertEqual(f.page.objects.objects.first?.frame.y ?? 0, 620, accuracy: 0.5)
        XCTAssertEqual(f.page.objects.selectedID, text.id, "the drag put the object down")
        f.controller.autosave.flushAll()
        XCTAssertEqual(try f.storedPage().objects.first?.frame.x ?? 0, 160, accuracy: 0.5)
        XCTAssertEqual(f.undo.undoActionName, "Move Object")
        flushUndoGroup()
        f.undo.undo()
        XCTAssertEqual(f.page.objects.objects.first?.frame.x, 100)
        XCTAssertFalse(f.undo.canUndo)
    }

    func testACornerHandleScalesAnObjectThroughTheLassoRoute() throws {
        let f = try makeFixture()
        _ = addText(f, at: PageObject.Frame(x: 100, y: 500, width: 200, height: 60))
        f.notebookController.selectionTap(at: f.onScreen(CGPoint(x: 200, y: 530)))
        let corner = CGPoint(x: 300, y: 560)
        f.notebookController.routeLassoTouch(.began(f.onScreen(corner)))
        f.notebookController.routeLassoTouch(.moved(f.onScreen(CGPoint(x: 500, y: 620))))
        f.notebookController.routeLassoTouch(.ended)
        let scaled = try XCTUnwrap(f.page.objects.objects.first)
        XCTAssertEqual(scaled.frame.x, 100, accuracy: 0.5)
        XCTAssertEqual(scaled.frame.width / scaled.frame.height, 200.0 / 60.0, accuracy: 0.01)
        XCTAssertGreaterThan(scaled.frame.width, 300)
        XCTAssertGreaterThan(scaled.fontSize ?? 0, PageObject.defaultFontSize)
        XCTAssertEqual(f.undo.undoActionName, "Resize Object")
    }

    /// A loop round ink and a text box makes one selection of both; a drag
    /// moves both, and one undo puts both back.
    func testALoopRoundInkAndAnObjectMakesAMixedSelectionThatMovesTogether() throws {
        let f = try makeFixture()
        f.tools.select(.lasso)
        _ = addText(f, at: PageObject.Frame(x: 220, y: 300, width: 160, height: 50))
        let centre = CGPoint(x: 300, y: 280)
        func loop(_ angle: Double) -> CGPoint {
            f.onScreen(CGPoint(x: centre.x + cos(angle) * 180, y: centre.y + sin(angle) * 140))
        }
        f.notebookController.routeLassoTouch(.began(loop(0)))
        for step in 1...48 { f.notebookController.routeLassoTouch(.moved(loop(Double(step) / 48 * 2 * .pi))) }
        f.notebookController.routeLassoTouch(.ended)
        let lasso = try XCTUnwrap(f.page.lasso)
        XCTAssertEqual(lasso.menuKind, .mixed)
        XCTAssertEqual(lasso.selection, [0])
        XCTAssertEqual(lasso.selectedObjects.count, 1)
        XCTAssertEqual(lasso.menu.kind, .mixed)

        let ink = f.canvas.drawing.bounds
        let start = f.onScreen(CGPoint(x: 300, y: 242))
        f.notebookController.routeLassoTouch(.began(start))
        for step in 1...10 {
            f.notebookController.routeLassoTouch(
                .moved(CGPoint(x: start.x + CGFloat(step) * 4 * f.page.zoom, y: start.y + CGFloat(step) * 10 * f.page.zoom)))
        }
        f.notebookController.routeLassoTouch(.ended)
        XCTAssertEqual(f.canvas.drawing.bounds.minY, ink.minY + 100, accuracy: 1)
        XCTAssertEqual(f.page.objects.objects.first?.frame.y ?? 0, 400, accuracy: 0.5)
        XCTAssertEqual(f.page.objects.objects.first?.frame.x ?? 0, 260, accuracy: 0.5)

        flushUndoGroup()
        f.undo.undo()
        XCTAssertEqual(f.canvas.drawing.bounds.minY, ink.minY, accuracy: 1)
        XCTAssertEqual(f.page.objects.objects.first?.frame.y ?? 0, 300, accuracy: 0.5)
        XCTAssertFalse(f.undo.canUndo, "ink and objects were two undo steps")
    }

    /// A loop round one object alone selects that object, with its handles.
    func testALoopRoundOneObjectSelectsItAsAnObject() throws {
        let f = try makeFixture()
        f.tools.select(.lasso)
        let text = addText(f, at: PageObject.Frame(x: 220, y: 600, width: 160, height: 50))
        let centre = CGPoint(x: 300, y: 625)
        func loop(_ angle: Double) -> CGPoint {
            f.onScreen(CGPoint(x: centre.x + cos(angle) * 120, y: centre.y + sin(angle) * 60))
        }
        f.notebookController.routeLassoTouch(.began(loop(0)))
        for step in 1...48 { f.notebookController.routeLassoTouch(.moved(loop(Double(step) / 48 * 2 * .pi))) }
        f.notebookController.routeLassoTouch(.ended)
        XCTAssertEqual(f.page.objects.selectedID, text.id)
        XCTAssertEqual(f.page.selectionKind, .text)
    }

    func testTheShapesToolPlacesAShapeAndFallsBackToTheLasso() throws {
        let f = try makeFixture()
        // Five places that do not overlap: a tap on a shape selects it.
        let places = [
            CGPoint(x: 150, y: 450), CGPoint(x: 450, y: 450), CGPoint(x: 150, y: 650),
            CGPoint(x: 450, y: 650), CGPoint(x: 300, y: 780),
        ]
        for (kind, place) in zip(ShapeKind.allCases, places) {
            f.tools.setShapeKind(kind)
            f.tools.select(.shape)
            let before = f.page.objects.objects.count
            f.notebookController.selectionTap(at: f.onScreen(place))
            XCTAssertEqual(f.page.objects.objects.count, before + 1, "no \(kind) was placed")
            let shape = try XCTUnwrap(f.page.objects.objects.last)
            XCTAssertEqual(ShapeStyle(shape).kind, kind)
            XCTAssertEqual(f.tools.active, .lasso, "the Shapes tool did not fall back after a \(kind)")
            XCTAssertEqual(f.page.objects.selectedID, shape.id)
            XCTAssertEqual(f.undo.undoActionName, "Insert Shape")
        }
        f.controller.autosave.flushAll()
        XCTAssertEqual(try f.storedPage().objects.count, ShapeKind.allCases.count)
    }

    func testReadOnlyPutsTheSelectionDownAndTakesNoTaps() throws {
        let f = try makeFixture()
        f.notebookController.selectionTap(at: f.onScreen(CGPoint(x: 300, y: 242)))
        XCTAssertEqual(f.page.selectionKind, .ink)

        f.notebookController.setReadOnly(true)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        XCTAssertNil(f.page.selectionKind, "Read Only left the selection up")
        f.notebookController.selectionTap(at: f.onScreen(CGPoint(x: 300, y: 242)))
        XCTAssertNil(f.page.selectionKind)
        XCTAssertFalse(
            f.notebookController.lassoShouldBegin(
                at: f.onScreen(CGPoint(x: 300, y: 242)), touchType: .pencil))
        XCTAssertFalse(f.notebookController.objectTap.isEnabled)

        f.notebookController.setReadOnly(false)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        XCTAssertTrue(f.notebookController.objectTap.isEnabled)
        f.notebookController.selectionTap(at: f.onScreen(CGPoint(x: 300, y: 242)))
        XCTAssertEqual(f.page.selectionKind, .ink)
    }

    func testCopyThenPasteBringsTheInkBackSelectedInOneUndoStep() throws {
        let f = try makeFixture()
        f.notebookController.selectionTap(at: f.onScreen(CGPoint(x: 300, y: 242)))
        let lasso = try XCTUnwrap(f.page.lasso)
        lasso.copySelection()
        XCTAssertNotNil(PaperClip.read())

        f.notebookController.paste(onto: f.page, at: CGPoint(x: 300, y: 600))
        XCTAssertEqual(f.canvas.drawing.strokes.count, 2)
        XCTAssertEqual(lasso.selection, [1], "the paste is not the selection")
        XCTAssertEqual(f.canvas.drawing.strokes[1].renderBounds.midY, 600, accuracy: 1)
        flushUndoGroup()
        f.undo.undo()
        XCTAssertEqual(f.canvas.drawing.strokes.count, 1)
    }

    /// A menu edit is written, and stays written through a tool switch.
    func testAnObjectEditIsSavedAndSurvivesAToolSwitch() throws {
        let f = try makeFixture()
        let text = addText(f, at: PageObject.Frame(x: 100, y: 500, width: 200, height: 60))
        f.notebookController.selectionTap(at: f.onScreen(CGPoint(x: 200, y: 530)))
        f.page.objects.perform(.color("#1f6feb"))
        f.tools.select(.pen)
        XCTAssertNil(f.page.selectionKind, "a tool switch kept the selection")
        f.controller.autosave.flushAll()
        let stored = try XCTUnwrap(try f.storedPage().objects.first { $0.id == text.id })
        XCTAssertEqual(stored.additionalFields["color"], .string("#1f6feb"))
    }

    // MARK: - Keys

    /// Runs the key command for `input` as the keyboard would; false when the
    /// editor offers none.
    @discardableResult
    private func press(
        _ input: String, _ modifiers: UIKeyModifierFlags = .command, on f: Fixture
    ) -> Bool {
        guard
            let action = f.notebookController.keyCommands?
                .first(where: { $0.input == input && $0.modifierFlags == modifiers })?.action
        else { return false }
        _ = f.notebookController.perform(action)
        return true
    }

    /// Escape puts ink down even with the Lasso active, which is where a tap
    /// that selects leaves the toolbar; an object too.
    func testEscapePutsDownInkAndObjects() throws {
        let f = try makeFixture()
        f.notebookController.selectionTap(at: f.onScreen(CGPoint(x: 300, y: 242)))
        XCTAssertEqual(f.page.selectionKind, .ink)
        XCTAssertEqual(f.tools.active, .lasso)
        XCTAssertTrue(press(UIKeyCommand.inputEscape, [], on: f))
        XCTAssertNil(f.page.selectionKind, "Escape left the ink selected")
        XCTAssertEqual(f.page.lasso?.selection, [])

        _ = addText(f, at: PageObject.Frame(x: 100, y: 500, width: 200, height: 60))
        f.notebookController.selectionTap(at: f.onScreen(CGPoint(x: 200, y: 530)))
        XCTAssertEqual(f.page.selectionKind, .text)
        XCTAssertTrue(press(UIKeyCommand.inputEscape, [], on: f))
        XCTAssertNil(f.page.selectionKind, "Escape left the text box selected")
    }

    /// Cmd-C copies ink without touching the page; Cmd-X copies it and takes
    /// it off as one undo step; Cmd-V puts it back.
    func testCommandCCopiesCommandXCutsAndCommandVPastes() throws {
        let f = try makeFixture()
        UIPasteboard.general.items = []
        f.notebookController.selectionTap(at: f.onScreen(CGPoint(x: 300, y: 242)))
        XCTAssertTrue(press("c", on: f))
        let copied = try XCTUnwrap(PaperClip.read(), "Cmd-C put nothing on the pasteboard")
        XCTAssertEqual(try PKDrawing(data: try XCTUnwrap(copied.drawing)).strokes.count, 1)
        XCTAssertEqual(f.canvas.drawing.strokes.count, 1, "Cmd-C changed the page")
        XCTAssertEqual(f.page.selectionKind, .ink, "Cmd-C put the selection down")

        UIPasteboard.general.items = []
        XCTAssertTrue(press("x", on: f))
        XCTAssertNotNil(PaperClip.read(), "Cmd-X put nothing on the pasteboard")
        XCTAssertEqual(f.canvas.drawing.strokes.count, 0, "Cmd-X left the ink")
        XCTAssertNil(f.page.selectionKind)
        flushUndoGroup()
        f.undo.undo()
        XCTAssertEqual(f.canvas.drawing.strokes.count, 1, "one undo did not bring the ink back")
        f.undo.redo()
        XCTAssertEqual(f.canvas.drawing.strokes.count, 0)

        XCTAssertTrue(press("v", on: f))
        XCTAssertEqual(f.canvas.drawing.strokes.count, 1, "Cmd-V did not paste")
        XCTAssertEqual(f.page.selectionKind, .ink, "the paste is not the selection")
    }

    /// An object goes the same way: Cmd-X takes the text box off the page and
    /// puts Paper's copy of it on the pasteboard.
    func testCommandXCutsATextBox() throws {
        let f = try makeFixture()
        UIPasteboard.general.items = []
        let text = addText(f, at: PageObject.Frame(x: 100, y: 500, width: 200, height: 60))
        f.notebookController.selectionTap(at: f.onScreen(CGPoint(x: 200, y: 530)))
        XCTAssertEqual(f.page.selectionKind, .text)
        XCTAssertTrue(press("x", on: f))
        XCTAssertEqual(PaperClip.read()?.objects.map(\.id), [text.id])
        XCTAssertFalse(f.page.objects.objects.contains { $0.id == text.id }, "Cmd-X left the text box")
        XCTAssertNil(f.page.selectionKind)
    }

    /// Read Only keeps Cmd-C and no other selection key.
    func testReadOnlyKeepsCopyAndNoOtherSelectionKey() throws {
        let f = try makeFixture()
        f.notebookController.setReadOnly(true)
        XCTAssertTrue(press("c", on: f))
        XCTAssertFalse(press("x", on: f))
        XCTAssertFalse(press("v", on: f))
        XCTAssertFalse(press(UIKeyCommand.inputEscape, [], on: f))
        f.notebookController.setReadOnly(false)
        XCTAssertNotNil(f.notebookController.keyCommands?.first { $0.input == "x" })
    }
}
