import PaperCore
import PencilKit
import UIKit
import UniformTypeIdentifiers

/// The accent outline a search result leaves round the words it found.
///
/// A view rather than a border on the page itself, because it has to sit above
/// the ink and disappear again without touching anything underneath it. Under
/// Reduce Motion it appears and vanishes; otherwise it fades.
final class HighlightLayer: UIView {
    /// How long the outline stays, per the plan. A variable only so a DEBUG
    /// launch argument can hold it open long enough to be screenshotted; the
    /// shipping value is never changed.
    static var duration: TimeInterval = 1.5

    private let outline = CAShapeLayer()
    /// The region, in page points. Scaled to the view by `zoom`.
    private(set) var box: CGRect?
    var zoom: CGFloat = 1 { didSet { layoutOutline() } }

    private var hideWork: DispatchWorkItem?

    override init(frame: CGRect) {
        super.init(frame: frame)
        isUserInteractionEnabled = false
        isHidden = true
        outline.fillColor = UIColor(PaperTokens.accent).withAlphaComponent(0.08).cgColor
        outline.strokeColor = UIColor(PaperTokens.accent).cgColor
        outline.lineWidth = 2
        layer.addSublayer(outline)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("HighlightLayer is created in code") }

    func flash(_ box: CGRect) {
        self.box = box
        hideWork?.cancel()
        layoutOutline()
        isHidden = false
        alpha = 1
        let work = DispatchWorkItem { [weak self] in
            MainActor.assumeIsolated { self?.fadeOut() }
        }
        hideWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.duration, execute: work)
    }

    private func fadeOut() {
        guard !UIAccessibility.isReduceMotionEnabled else {
            clear()
            return
        }
        UIView.animate(withDuration: 0.25) { self.alpha = 0 } completion: { _ in
            self.clear()
        }
    }

    func clear() {
        hideWork?.cancel()
        hideWork = nil
        box = nil
        isHidden = true
        alpha = 1
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        layoutOutline()
    }

    private func layoutOutline() {
        guard let box else {
            outline.path = nil
            return
        }
        let scaled = CGRect(
            x: box.minX * zoom, y: box.minY * zoom, width: box.width * zoom,
            height: box.height * zoom)
        outline.path = UIBezierPath(roundedRect: scaled.insetBy(dx: -4, dy: -4), cornerRadius: 4)
            .cgPath
    }
}

/// The paper itself: the page's colour plus the ruling.
///
/// A plain `UIView` rather than a `CALayer` subclass so the drawing context is
/// UIKit's y-down one -- the same coordinate space `TemplateGeometry` uses for
/// the off-screen render, which is what keeps screen and render identical.
final class TemplateLayer: UIView {
    var template: Template = .ruled { didSet { setNeedsDisplay() } }
    var pageSize: CGSize = .zero { didSet { setNeedsDisplay() } }
    /// The sheet's colour; the rules follow it (§3.4).
    var paperColor: PaperColor = .white {
        didSet {
            guard paperColor != oldValue else { return }
            backgroundColor = paperColor.uiColor
            setNeedsDisplay()
        }
    }

    override init(frame: CGRect) {
        super.init(frame: frame)
        isOpaque = true
        isUserInteractionEnabled = false
        contentMode = .redraw
        backgroundColor = paperColor.uiColor
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("TemplateLayer is created in code") }

    override func draw(_ rect: CGRect) {
        guard pageSize.width > 0, pageSize.height > 0,
            let context = UIGraphicsGetCurrentContext()
        else { return }
        context.scaleBy(x: bounds.width / pageSize.width, y: bounds.height / pageSize.height)
        TemplateGeometry.draw(
            template: template, size: pageSize, paperColor: paperColor, in: context)
    }
}

extension PaperColor {
    /// The sheet's colour on screen.
    var uiColor: UIColor {
        let (red, green, blue) = rgb
        return UIColor(red: red, green: green, blue: blue, alpha: 1)
    }
}

/// The circle that follows a hovering Pencil, sized to the current tool.
/// An enhancement, never a requirement: it appears and disappears, and nothing
/// depends on having seen it.
final class HoverPreviewLayer: UIView {
    private let circle = CAShapeLayer()

    override init(frame: CGRect) {
        super.init(frame: frame)
        isUserInteractionEnabled = false
        circle.fillColor = UIColor.clear.cgColor
        circle.strokeColor = UIColor.label.withAlphaComponent(0.5).cgColor
        circle.lineWidth = 1
        layer.addSublayer(circle)
        isHidden = true
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("HoverPreviewLayer is created in code") }

    func show(at point: CGPoint, radius: CGFloat) {
        isHidden = false
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        circle.path =
            UIBezierPath(
                arcCenter: point, radius: max(radius, 2), startAngle: 0, endAngle: .pi * 2,
                clockwise: true
            ).cgPath
        CATransaction.commit()
    }

    func hide() {
        isHidden = true
    }
}

@MainActor
protocol PageViewDelegate: AnyObject {
    func pageViewDrawingChanged(_ pageView: PageView)
    func pageViewDidBeginUsingTool(_ pageView: PageView)
    func pageViewDidEndUsingTool(_ pageView: PageView)
    func pageViewToolState(_ pageView: PageView) -> ToolState
    func pageView(_ pageView: PageView, didChangeSelection isEmpty: Bool)
    /// Objects changed: write them to `page.json` and mark the page dirty.
    func pageView(_ pageView: PageView, didChangeObjects objects: [PageObject])
    /// Image bytes for an object's asset, loaded through the store.
    func pageView(_ pageView: PageView, imageForAsset assetPath: String) -> UIImage?
    /// Something was dropped on the page: image bytes, or a PDF to import.
    func pageView(_ pageView: PageView, didDropImage data: Data, name: String, at point: CGPoint)
    func pageView(_ pageView: PageView, didDropPDF data: Data, name: String)
    /// A finger tap selected something on this page: the toolbar goes to the
    /// Lasso and every other page puts its selection down.
    func pageView(_ pageView: PageView, didSelect kind: ObjectMenuKind)
    /// An insert tool placed its object.
    func pageView(_ pageView: PageView, didPlace type: PageObject.Kind)
    /// The long-press menu's Paste, at a point in page points.
    func pageView(_ pageView: PageView, pasteAt point: CGPoint)
    /// The object menu's Share, from a rectangle of the page view.
    func pageView(_ pageView: PageView, share image: UIImage, from rect: CGRect)
    /// A picture of some ink and objects over `rect` (page points) on white:
    /// what Copy puts beside Paper's own copy, and what OMNIA is sent.
    func pageView(
        _ pageView: PageView, render drawing: PKDrawing, objects: [PageObject], in rect: CGRect
    ) -> UIImage?
    /// Read Only is on: no selection, no insert, no editing.
    func pageViewIsReadOnly(_ pageView: PageView) -> Bool
}

/// What a page asks of its controller beyond the basics. Defaults, so a test's
/// stand-in delegate need not answer them.
extension PageViewDelegate {
    func pageView(_ pageView: PageView, didSelect kind: ObjectMenuKind) {}
    func pageView(_ pageView: PageView, didPlace type: PageObject.Kind) {}
    func pageView(_ pageView: PageView, pasteAt point: CGPoint) {}
    func pageView(_ pageView: PageView, share image: UIImage, from rect: CGRect) {}
    func pageView(
        _ pageView: PageView, render drawing: PKDrawing, objects: [PageObject], in rect: CGRect
    ) -> UIImage? { nil }
    func pageViewIsReadOnly(_ pageView: PageView) -> Bool { false }
}

/// One page.
///
/// The composition is fixed: template, then (M4) PDF background and objects,
/// then ink, then the lasso overlay. Only the pages near the viewport get a
/// real `PKCanvasView`; the rest show their render, which is what lets a
/// 200-page notebook scroll.
///
/// The page is drawn in its paper colour, and a page on dark paper is drawn
/// with a dark appearance -- its canvas, its carried ink, its text and shapes
/// -- while every other page is drawn light (`PaperAppearance`, §3.4).
///
/// **A board** (`isBoard`) turns the page inside out. Its canvas is a scroll
/// view of its own that pans and zooms the board (`BoardCanvasHost`), so the
/// page lives *inside* the canvas, as its content, rather than the canvas
/// inside the page: `goLive(onBoard:gestureHost:drawingData:)` puts the
/// objects layer under the ink and this view, with the lasso overlay, over
/// it. This view's coordinates stay what they are on a page -- page points
/// times `zoom`, from the board's top-left -- so hit testing, the lasso and
/// the objects work unchanged. The paper and its grid are drawn in screen
/// space under the canvas by `boardBackground`, in place of the template.
final class PageView: UIView {
    private(set) var page: Page
    let notebookPath: String
    /// The objects are built in `init`, before there is a delegate to ask for
    /// their pictures, so the images load when one is set.
    weak var delegate: PageViewDelegate? {
        didSet { if delegate != nil { objects.loadMissingImages() } }
    }
    /// A whiteboard's board rather than a page of a notebook.
    let isBoard: Bool

    let template = TemplateLayer()
    /// A board's paper and grid, which its controller puts under the canvas.
    /// Nil on a page.
    let boardBackground: BoardBackgroundView?
    /// While a board's controller allows it, the Pencil inks: not with the
    /// lasso or an insert tool, not in Read Only, not on a full board. A
    /// page's canvas is switched by `isUserInteractionEnabled` instead.
    var allowsInk = true {
        didSet { if allowsInk != oldValue { refreshCanvasInteraction() } }
    }
    private let pdfBackground = PDFBackgroundLayer()
    private let renderView = UIImageView()
    private(set) var canvas: PKCanvasView?
    let objects = ObjectsContainerView()
    private let hover = HoverPreviewLayer()
    private let highlight = HighlightLayer()
    private(set) var lasso: LassoOverlayView?
    /// The box a Pencil drag with the Shapes tool is drawing.
    private let shapePreview = CAShapeLayer()
    /// Set by the notebook controller: a lasso selection was read as text and
    /// wants to become a text object at `rect`.
    var onConvertToText: ((String, CGRect) -> Void)?
    var onAIAction: ((SelectionAIAction, Data, CGRect) -> Void)?
    var onCopyLink: ((CGRect) -> Void)?

    /// Guards `canvas.drawing = …`, which fires the delegate and would
    /// otherwise make every programmatic write look like a user edit.
    private var isApplyingDrawing = false

    /// Another window on this device is writing on this page. Until its ink
    /// is on disk and this view has been reloaded from it, nothing here may
    /// touch the page: this canvas holds a copy that is about to be stale.
    private(set) var isExternallyEditing = false
    /// Kept here rather than read back off the overlay, which is also
    /// switched off while another window writes.
    private var isLassoActive = false
    /// An object is selected, so the objects layer has the touches.
    private var isEditingObjects = false

    /// The Pencil's tap with an insert tool: places text or a shape. A finger
    /// tap comes through the notebook's `objectTap` to the same place.
    let insertTap = UITapGestureRecognizer()
    /// A Pencil drag with the Shapes tool draws the shape's box.
    let shapeDrag = UIPanGestureRecognizer()
    /// A finger held on the page offers Paste and Insert Text.
    let longPress = UILongPressGestureRecognizer()
    /// The Pencil hovering, for the preview circle and the insertion point.
    private lazy var hoverGesture = UIHoverGestureRecognizer(
        target: self, action: #selector(handleHover))
    private lazy var dropInteraction = UIDropInteraction(delegate: self)
    /// When the last long press began. The finger that held is still a tap
    /// to the notebook's tap recogniser when it lifts, and must not select.
    private var longPressBegan: Date = .distantPast
    private var shapeDragStart: CGPoint?
    /// Where the touch a shape drag follows came down, in view points.
    private var shapeTouchDown: CGPoint?

    /// How far top and left growth has moved everything on the page, in page
    /// points, since this view was made. An undo registered before a growth
    /// holds coordinates from before it, and moves them by the difference.
    private(set) var growthTranslation: CGSize = .zero

    var zoom: CGFloat = 1 {
        didSet {
            guard zoom != oldValue else { return }
            objects.zoom = zoom
            applyZoom()
        }
    }

    var pageSize: CGSize { page.pointSize }

    /// Whether the page is drawn with the dark appearance.
    var isDarkPaper: Bool { PaperAppearance.isDark(page: page) }

    /// Where the Pencil last hovered, in page points. The `+` menu inserts
    /// there rather than at a fixed spot, so "put a text box here" means here.
    private(set) var lastHoverPoint: CGPoint?

    init(page: Page, notebookPath: String, isBoard: Bool = false) {
        self.page = page
        self.notebookPath = notebookPath
        self.isBoard = isBoard
        boardBackground = isBoard ? BoardBackgroundView() : nil
        super.init(frame: CGRect(origin: .zero, size: page.size.points))
        build()
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("PageView is created in code") }

    private func build() {
        if !isBoard {
            layer.cornerRadius = 2
            layer.shadowColor = UIColor.black.cgColor
            layer.shadowOpacity = 0.08
            layer.shadowRadius = 6
            layer.shadowOffset = CGSize(width: 0, height: 2)
        }

        template.template = page.template
        template.pageSize = pageSize
        // A board is many times the size a bitmap may be: its paper and grid
        // are `boardBackground`, and it has no PDF and no stand-in render.
        template.isHidden = isBoard
        addSubview(template)

        pdfBackground.isHidden = isBoard
        addSubview(pdfBackground)

        renderView.contentMode = .scaleAspectFill
        renderView.isUserInteractionEnabled = false
        renderView.isHidden = isBoard
        addSubview(renderView)

        objects.delegate = self
        objects.growthOffset = { [weak self] in self?.growthTranslation ?? .zero }
        addSubview(objects)
        objects.setObjects(page.objects)

        addSubview(hover)
        addSubview(highlight)

        shapePreview.lineCap = .round
        shapePreview.lineJoin = .round
        shapePreview.fillColor = nil
        layer.addSublayer(shapePreview)

        addGestureRecognizer(hoverGesture)

        // The insert tools' Pencil tap and drag. Each asks first whether its
        // tool is the one selected, so the rest of the time they never begin.
        let pencil = NSNumber(value: UITouch.TouchType.pencil.rawValue)
        let direct = NSNumber(value: UITouch.TouchType.direct.rawValue)
        insertTap.allowedTouchTypes = [pencil]
        insertTap.addTarget(self, action: #selector(handleInsertTap(_:)))
        // XCUITest has no Pencil: under `-debugFingerInk` a finger draws the
        // box as the Pencil would.
        shapeDrag.allowedTouchTypes = EditorInput.fingerInk ? [direct, pencil] : [pencil]
        shapeDrag.maximumNumberOfTouches = 1
        shapeDrag.addTarget(self, action: #selector(handleShapeDrag(_:)))
        longPress.allowedTouchTypes = [direct]
        longPress.addTarget(self, action: #selector(handleLongPress(_:)))
        for gesture in [insertTap, shapeDrag, longPress] as [UIGestureRecognizer] {
            gesture.cancelsTouchesInView = false
            gesture.delaysTouchesEnded = false
            gesture.delegate = self
            addGestureRecognizer(gesture)
        }
        addInteraction(UIEditMenuInteraction(delegate: self))

        addInteraction(dropInteraction)

        // A container rather than an element: the page itself is one element,
        // and the object menu -- when there is one -- is another. A plain
        // element would hide the menu's buttons from VoiceOver entirely.
        isAccessibilityElement = false
        canvasElement.accessibilityTraits = [.allowsDirectInteraction]
        applyPaper()
    }

    private lazy var canvasElement = CanvasElement(accessibilityContainer: self)

    /// How far a board's element stays inside the screen's edges, where the
    /// undo pill, the tool menu and the zoom pill float: a gesture XCUITest
    /// starts near the element's corners then lands on the board, as a
    /// person's fingers would.
    static let boardElementInsets = UIEdgeInsets(top: 72, left: 24, bottom: 72, right: 24)

    override var accessibilityElements: [Any]? {
        get {
            canvasElement.accessibilityFrameInContainerSpace = elementFrame
            var elements: [Any] = [canvasElement]
            if let menu = lasso?.accessibleInspector { elements.append(menu) }
            if !objects.menu.isHidden { elements.append(objects.menu) }
            return elements
        }
        set { _ = newValue }
    }

    /// The part of a board on screen, in this view's points; a page's bounds.
    var visibleBounds: CGRect {
        guard isBoard, let superview else { return bounds }
        let visible = convert(superview.bounds, from: superview).intersection(bounds)
        return visible.isNull ? bounds : visible
    }

    private var elementFrame: CGRect {
        guard isBoard else { return bounds }
        let inset = visibleBounds.inset(by: Self.boardElementInsets)
        return inset.isEmpty ? visibleBounds : inset
    }

    /// "Page 3 of 12, drawing canvas" -- set by the stack, which is the only
    /// thing that knows the page's number. "Board 2 of 3" on a whiteboard.
    func setAccessibility(index: Int, total: Int) {
        canvasElement.accessibilityLabel =
            "\(isBoard ? "Board" : "Page") \(index) of \(total), drawing canvas"
        canvasElement.accessibilityHint =
            "Write with the Apple Pencil. Pinch with two fingers to zoom."
        refreshDebugValue()
    }

    /// How much ink is on the page and where, published as the element's
    /// value -- "strokes:N extent:WxH ink:x,y,w,h", in page points -- so a UI
    /// test can tell a captured lasso loop from a stroke that was drawn by
    /// mistake, and see that a lasso drag moved the ink. Then the objects as
    /// "objects:N", the selection as "selected:<kind>" and the first object's
    /// frame as "object:x,y,w,h". DEBUG only: the shipping value belongs to
    /// the transcript.
    ///
    /// A board's value moves with every pan and pinch, so it is worked out
    /// when it is read, and adds "zoom:Z centre:X,Y" (the viewport's middle)
    /// and "visible:x,y,w,h", the board points the element's frame covers.
    func refreshDebugValue() {
        #if DEBUG
            if isBoard {
                canvasElement.liveValue = { [weak self] in self?.debugValue() }
            } else {
                canvasElement.accessibilityValue = debugValue()
            }
        #endif
    }

    #if DEBUG
        private func debugValue() -> String {
            var value =
                "strokes:\(drawing?.strokes.count ?? 0) "
                + "extent:\(Int(pageSize.width.rounded()))x\(Int(pageSize.height.rounded()))"
            if let ink = drawing?.bounds, !ink.isNull, !ink.isEmpty {
                value +=
                    " ink:\(Int(ink.minX.rounded())),\(Int(ink.minY.rounded())),"
                    + "\(Int(ink.width.rounded())),\(Int(ink.height.rounded()))"
            }
            value += " objects:\(objects.objects.count)"
            value += " selected:\(selectionKind?.rawValue ?? "none")"
            if let first = objects.objects.first {
                let frame = first.frame
                value +=
                    " object:\(Int(frame.x.rounded())),\(Int(frame.y.rounded())),"
                    + "\(Int(frame.width.rounded())),\(Int(frame.height.rounded()))"
            }
            if isBoard {
                let scale = max(zoom, 0.0001)
                let visible = visibleBounds
                let element = elementFrame
                value += String(
                    format: " zoom:%.4f centre:%.1f,%.1f visible:%.1f,%.1f,%.1f,%.1f", zoom,
                    visible.midX / scale, visible.midY / scale, element.minX / scale,
                    element.minY / scale, element.width / scale, element.height / scale)
            }
            return value
        }
    #endif

    override func layoutSubviews() {
        super.layoutSubviews()
        // On a board the canvas holds this view and the objects layer, both
        // at the board's origin, so `bounds` is the objects layer's frame
        // too; the template, the PDF and the render stay empty.
        let sheet = isBoard ? .zero : bounds
        template.frame = sheet
        pdfBackground.frame = sheet
        renderView.frame = sheet
        objects.frame = isBoard ? frame : bounds
        hover.frame = bounds
        highlight.frame = bounds
        highlight.zoom = zoom
        lasso?.frame = bounds
        if !isBoard { canvas?.frame = bounds }
        shapePreview.frame = bounds
    }

    /// A board's touches on bare paper go to the canvas under this view, as
    /// a page's go to its canvas; only what is on it -- the lasso overlay
    /// while the Lasso is out, the object menu -- keeps its own.
    override func hitTest(_ point: CGPoint, with event: UIEvent?) -> UIView? {
        let hit = super.hitTest(point, with: event)
        return isBoard && hit === self ? nil : hit
    }

    // MARK: - Paper

    /// The paper's colour and the appearance it is drawn with, on every part
    /// of the page that has one.
    private func applyPaper() {
        let dark = isDarkPaper
        // A board's paper is drawn under its canvas, in screen space; this
        // view lies over the ink and must let it show.
        backgroundColor = isBoard ? .clear : page.paperColor.uiColor
        boardBackground?.paperColor = page.paperColor
        boardBackground?.template = page.template
        template.paperColor = page.paperColor
        // PencilKit derives ink colour from the trait collection: black ink
        // shows light on a dark page and dark on every other, whatever the
        // system appearance is.
        canvas?.overrideUserInterfaceStyle = dark ? .dark : .light
        lasso?.isDarkPaper = dark
        objects.isDarkPaper = dark
    }

    // MARK: - Live window

    var isLive: Bool { canvas != nil }

    /// Gives the page a real canvas. `drawingData` is the page's stored ink.
    func goLive(drawingData: Data?) {
        guard canvas == nil, let delegate else { return }
        let canvas = PKCanvasView(frame: bounds)
        canvas.overrideUserInterfaceStyle = isDarkPaper ? .dark : .light
        canvas.backgroundColor = .clear
        canvas.isOpaque = false
        canvas.isScrollEnabled = false
        canvas.bouncesZoom = false
        canvas.showsVerticalScrollIndicator = false
        canvas.showsHorizontalScrollIndicator = false
        canvas.contentInsetAdjustmentBehavior = .never
        canvas.drawingPolicy = EditorInput.drawingPolicy
        canvas.tool = delegate.pageViewToolState(self).pkTool
        canvas.delegate = self
        canvas.isAccessibilityElement = false

        if let drawingData, !drawingData.isEmpty, let drawing = try? PKDrawing(data: drawingData) {
            isApplyingDrawing = true
            canvas.drawing = drawing
            isApplyingDrawing = false
        }

        insertSubview(canvas, aboveSubview: objects)
        self.canvas = canvas
        attachOverlay(to: canvas)
    }

    /// Makes this board live on `canvas`, the scroll view that pans and zooms
    /// it: the objects layer goes in under PencilKit's ink and this view, with
    /// the lasso overlay, over it. Both sit at the board's origin, so this
    /// view's points are the canvas's. The gestures that need a bare touch --
    /// the insert tap, the shape drag, the long press, hover and drop -- move
    /// to `gestureHost`, an ancestor of the canvas, since bare touches pass
    /// through this view to the ink.
    func goLive(onBoard canvas: PKCanvasView, gestureHost: UIView, drawingData: Data?) {
        guard isBoard, self.canvas == nil, let delegate else { return }
        canvas.overrideUserInterfaceStyle = isDarkPaper ? .dark : .light
        canvas.drawingPolicy = EditorInput.drawingPolicy
        canvas.tool = delegate.pageViewToolState(self).pkTool
        canvas.delegate = self
        var drawing = PKDrawing()
        if let drawingData, !drawingData.isEmpty, let decoded = try? PKDrawing(data: drawingData) {
            drawing = decoded
        }
        isApplyingDrawing = true
        canvas.drawing = drawing
        isApplyingDrawing = false
        canvas.undoManager?.removeAllActions(withTarget: canvas)

        canvas.insertSubview(objects, at: 0)
        canvas.addSubview(self)
        for gesture in [insertTap, shapeDrag, longPress, hoverGesture] as [UIGestureRecognizer] {
            removeGestureRecognizer(gesture)
            gestureHost.addGestureRecognizer(gesture)
        }
        removeInteraction(dropInteraction)
        gestureHost.addInteraction(dropInteraction)
        self.canvas = canvas
        attachOverlay(to: canvas)
    }

    /// Takes a board off its canvas again, when another board takes its
    /// place: the inverse of `goLive(onBoard:gestureHost:drawingData:)`. The
    /// caller flushes first. The history goes, as a page's does.
    func leaveBoard() {
        guard isBoard, let canvas else { return }
        objects.deselect()
        lasso?.clearSelection()
        lasso?.removeFromSuperview()
        lasso = nil
        endShapeDrag(place: false)
        canvas.undoManager?.removeAllActions(withTarget: canvas)
        canvas.undoManager?.removeAllActions(withTarget: objects)
        undoManager?.removeAllActions(withTarget: objects)
        for gesture in [insertTap, shapeDrag, longPress, hoverGesture] as [UIGestureRecognizer] {
            gesture.view?.removeGestureRecognizer(gesture)
        }
        dropInteraction.view?.removeInteraction(dropInteraction)
        canvas.delegate = nil
        objects.removeFromSuperview()
        removeFromSuperview()
        self.canvas = nil
    }

    /// The lasso overlay over a canvas that has just been given to the page.
    private func attachOverlay(to canvas: PKCanvasView) {
        let overlay = LassoOverlayView(canvas: canvas)
        overlay.frame = bounds
        overlay.isDarkPaper = isDarkPaper
        overlay.growthOffset = { [weak self] in self?.growthTranslation ?? .zero }
        overlay.setDrawingQuietly = { [weak self] drawing in self?.applyDrawing(drawing) }
        overlay.onSelectionChange = { [weak self] isEmpty in
            guard let self else { return }
            self.refreshDebugValue()
            self.delegate?.pageView(self, didChangeSelection: isEmpty)
        }
        overlay.onConvertToText = { [weak self] text, rect in
            self?.onConvertToText?(text, rect)
        }
        overlay.onAIAction = { [weak self] action, data, rect in
            self?.onAIAction?(action, data, rect)
        }
        overlay.onCopyLink = { [weak self] rect in self?.onCopyLink?(rect) }
        overlay.objectsProvider = { [weak self] in self?.objects.objects ?? [] }
        overlay.previewObjects = { [weak self] ids, transform in
            self?.objects.preview(ids, transform: transform)
        }
        overlay.commitObjects = { [weak self] next, name in self?.objects.commit(next, actionName: name) }
        overlay.onSelectObject = { [weak self] id in
            guard let self else { return }
            self.selectObject(id)
            self.delegate?.pageView(self, didSelect: self.selectionKind ?? .mixed)
        }
        overlay.onCopy = { [weak self] drawing, objects, image in
            guard let self else { return }
            PaperClip(
                drawing: drawing.strokes.isEmpty ? nil : drawing.dataRepresentation(),
                objects: objects, notebookPath: self.notebookPath
            ).write(image: image, string: nil)
        }
        overlay.renderSelection = { [weak self] drawing, objects, rect in
            guard let self else { return nil }
            return self.delegate?.pageView(self, render: drawing, objects: objects, in: rect)
        }
        addSubview(overlay)
        bringSubviewToFront(hover)
        bringSubviewToFront(highlight)
        lasso = overlay

        renderView.isHidden = true
        refreshCanvasInteraction()
        applyZoom()
        refreshDebugValue()
        setNeedsLayout()
    }

    /// Takes the canvas away once the page leaves the live window. The caller
    /// flushes first; this only drops the view.
    ///
    /// The history goes with it: whichever manager the edits registered on,
    /// Undo must not offer to change a canvas that is no longer there, nor
    /// object edits that canvas's history ordered them against. A selection
    /// is put down first, so text still being typed is written.
    func goDormant(render: UIImage?) {
        objects.deselect()
        lasso?.clearSelection()
        lasso?.removeFromSuperview()
        lasso = nil
        endShapeDrag(place: false)
        if let canvas {
            canvas.undoManager?.removeAllActions(withTarget: canvas)
            canvas.undoManager?.removeAllActions(withTarget: objects)
        }
        undoManager?.removeAllActions(withTarget: objects)
        canvas?.delegate = nil
        canvas?.removeFromSuperview()
        canvas = nil
        renderView.image = render
        renderView.isHidden = render == nil
    }

    var drawingData: Data? {
        drawing?.dataRepresentation()
    }

    /// The page's ink. While a lasso drag carries the selection the canvas
    /// shows the page without it; this is still the page with it, where it
    /// was picked up, so autosave never writes a page with a hole in it.
    var drawing: PKDrawing? { lasso?.drag?.resting ?? lasso?.scale?.resting ?? canvas?.drawing }

    /// Brings a live canvas up to the ink on disk after something other than
    /// this canvas wrote it: another device, a restored version, a conflict.
    /// A canvas already showing that ink is left alone, history and all.
    func reloadDrawing(_ data: Data?) {
        guard let canvas else { return }
        var stored = PKDrawing()
        if let data, !data.isEmpty {
            guard let decoded = try? PKDrawing(data: data) else { return }
            stored = decoded
        }
        let current = drawing ?? canvas.drawing
        guard stored != current, stored.dataRepresentation() != current.dataRepresentation()
        else { return }
        // Both describe ink that is no longer on the page.
        lasso?.clearSelection()
        canvas.undoManager?.removeAllActions(withTarget: canvas)
        applyDrawing(stored)
    }

    /// Replaces the ink without the change being reported as a user edit.
    func applyDrawing(_ drawing: PKDrawing) {
        guard let canvas else { return }
        isApplyingDrawing = true
        canvas.drawing = drawing
        isApplyingDrawing = false
    }

    func setTool(_ tool: PKTool) {
        canvas?.tool = tool
    }

    func setDrawingPolicy(_ policy: PKCanvasViewDrawingPolicy) {
        canvas?.drawingPolicy = policy
    }

    /// Lasso captures touches only while the lasso tool is selected; the rest
    /// of the time the canvas must see every touch. Picking any other tool
    /// puts the page's selection down, ink or object; picking the Lasso keeps
    /// it, because a tap that selects something picks the Lasso itself.
    func setLassoActive(_ active: Bool) {
        isLassoActive = active
        refreshCanvasInteraction()
        if !active {
            lasso?.clearSelection()
            objects.deselect()
        }
    }

    /// Another window started or stopped writing on this page. There is no
    /// banner: the pill, and that window's ink appearing here within a
    /// second, are the feedback.
    func setExternallyEditing(_ editing: Bool) {
        guard editing != isExternallyEditing else { return }
        isExternallyEditing = editing
        // A selection is a list of this canvas's strokes, which are about to
        // be replaced by that window's.
        if editing { lasso?.clearSelection() }
        refreshCanvasInteraction()
    }

    /// Who takes the page's touches, decided in one place so the Lasso, an
    /// object selection and another window's edit cannot undo each other:
    /// the canvas unless an object is selected, the objects layer only while
    /// one is, the overlay only in Lasso mode with no object selected -- and
    /// none of them while another window is writing the page.
    ///
    /// A board's canvas is its scroll view and always takes touches, so the
    /// fingers can pan and pinch whatever is selected; its ink is switched
    /// with `drawingGestureRecognizer` instead, and the overlays inside it
    /// take theirs first.
    private func refreshCanvasInteraction() {
        let open = !isExternallyEditing
        if isBoard {
            canvas?.drawingGestureRecognizer.isEnabled =
                open && !isEditingObjects && !isLassoActive && allowsInk
            objects.isUserInteractionEnabled = open && isEditingObjects
            lasso?.isUserInteractionEnabled = open && isLassoActive && !isEditingObjects
            return
        }
        canvas?.isUserInteractionEnabled = open && !isEditingObjects
        objects.isUserInteractionEnabled = open && isEditingObjects
        lasso?.isUserInteractionEnabled = open && isLassoActive && !isEditingObjects
    }

    /// Puts the accent outline round a region of the page for a second and a
    /// half -- what a search result leaves behind so the eye can find the
    /// words it was promised.
    func flashHighlight(_ box: CGRect) {
        bringSubviewToFront(highlight)
        highlight.zoom = zoom
        highlight.flash(box)
    }

    func clearHighlight() {
        highlight.clear()
    }

    func setRender(_ image: UIImage?) {
        guard !isLive else { return }
        renderView.image = image
        renderView.isHidden = image == nil
    }

    // MARK: - Objects

    /// Replaces the page this view is showing, keeping the same canvas. Used
    /// after an object edit or an import re-reads `page.json`, and for every
    /// page a rebuild keeps.
    func update(page: Page) {
        let resized = page.pointSize != pageSize
        if page.objects != self.page.objects {
            // Written by something other than this page, so the history
            // describes objects that are no longer there.
            forgetObjectHistory()
        }
        // A board's pattern is drawn with its paper, by `boardBackground`.
        let repainted =
            page.paperColor != self.page.paperColor
            || (isBoard && page.template != self.page.template)
        self.page = page
        template.template = page.template
        template.pageSize = pageSize
        objects.setObjects(page.objects)
        if repainted { applyPaper() }
        if resized {
            applyZoom()
            refreshDebugValue()
        }
        setNeedsLayout()
    }

    /// Object edits register on the canvas's manager when there is a canvas
    /// and on the page's own otherwise; forget them on both.
    private func forgetObjectHistory() {
        canvas?.undoManager?.removeAllActions(withTarget: objects)
        undoManager?.removeAllActions(withTarget: objects)
    }

    func setObjects(_ objects: [PageObject]) {
        self.page.objects = objects
        self.objects.setObjects(objects)
    }

    // MARK: - Infinite pages

    /// Everything that is on the page, in page points: the union the growth
    /// rule measures.
    var contentBounds: CGRect? {
        InfiniteCanvas.contentBounds(ink: canvas?.drawing.bounds, objects: page.objects)
    }

    /// Extends the page and, when the growth was at the top or the left, moves
    /// the ink and the objects with it so that nothing appears to jump.
    ///
    /// Returns the moved objects when there are any, because they live in
    /// `page.json` and only the controller may write them.
    @discardableResult
    func applyGrowth(_ growth: InfiniteCanvas.Growth) -> [PageObject]? {
        page.customSize = SizePoints(growth.extent)
        var movedObjects: [PageObject]?
        if growth.translation != .zero {
            growthTranslation.width += growth.translation.width
            growthTranslation.height += growth.translation.height
            let transform = CGAffineTransform(
                translationX: growth.translation.width, y: growth.translation.height)
            if let canvas {
                isApplyingDrawing = true
                canvas.drawing = canvas.drawing.transformed(using: transform)
                isApplyingDrawing = false
            }
            if !page.objects.isEmpty {
                // Moved, not re-measured: a line's frame keeps the direction
                // its negative width or height gives it.
                let moved = page.objects.map { object -> PageObject in
                    var copy = object
                    copy.frame.x += Double(growth.translation.width)
                    copy.frame.y += Double(growth.translation.height)
                    return copy
                }
                setObjects(moved)
                movedObjects = moved
            }
        }
        template.pageSize = pageSize
        template.setNeedsDisplay()
        applyZoom()
        refreshDebugValue()
        setNeedsLayout()
        return movedObjects
    }

    /// Loads the PDF page this page annotates, if it has one.
    func loadPDFBackground(pageDirectory: URL, assetURL: URL?) {
        pdfBackground.load(
            binding: page.pdf, pageDirectory: pageDirectory, assetURL: assetURL,
            pageSize: pageSize)
    }

    /// A point to insert at: where the Pencil last hovered, else the middle of
    /// the page.
    /// On a board, the middle of what is on screen unless the Pencil hovered
    /// somewhere on screen.
    var insertionPoint: CGPoint {
        guard isBoard else {
            return lastHoverPoint ?? CGPoint(x: pageSize.width / 2, y: pageSize.height / 2)
        }
        let scale = max(zoom, 0.01)
        let visible = visibleBounds
        if let hover = lastHoverPoint,
            visible.contains(CGPoint(x: hover.x * scale, y: hover.y * scale))
        {
            return hover
        }
        return CGPoint(x: visible.midX / scale, y: visible.midY / scale)
    }

    /// Adds an object and selects it; a text object opens for typing.
    func insertObject(_ object: PageObject, edit: Bool = true) {
        lasso?.clearSelection()
        objects.select(nil)
        objects.insert(object, edit: edit)
        refreshDebugValue()
    }

    func deselectObjects() {
        objects.deselect()
    }

    var hasSelectedObject: Bool { objects.selectedID != nil }

    // MARK: - Selection

    /// What is selected on this page, if anything.
    var selectionKind: ObjectMenuKind? {
        if let object = objects.selectedObject { return ObjectMenuKind(object.type) }
        return lasso?.menuKind
    }

    var hasSelection: Bool { selectionKind != nil }

    /// Puts down whatever is selected: ink, an object, text being typed.
    func putDownSelection() {
        lasso?.clearSelection()
        objects.deselect()
        refreshDebugValue()
    }

    /// Selects strokes, as a tap on a word does.
    func selectInk(_ strokes: [Int]) {
        objects.deselect()
        lasso?.select(strokes: strokes)
        refreshDebugValue()
    }

    /// Selects one object, with its handles and its menu.
    func selectObject(_ id: String) {
        lasso?.clearSelection()
        if objects.selectedID != id { objects.deselect() }
        objects.select(id)
        refreshDebugValue()
    }

    /// What a finger tap at `point` (this view's coordinates) lands on: an
    /// object, a word of ink, or nothing (`HitTesting`).
    func hitTarget(at point: CGPoint) -> HitTarget? {
        let scale = max(zoom, 0.01)
        return Self.hitTarget(
            at: CGPoint(x: point.x / scale, y: point.y / scale), drawing: drawing ?? PKDrawing(),
            objects: page.objects, zoom: zoom)
    }

    /// `HitTesting.target` over a drawing and objects, page points. Only the
    /// strokes near the tap are walked point by point; the rest bring their
    /// bounds, which is all a word needs from them.
    static func hitTarget(
        at point: CGPoint, drawing: PKDrawing, objects: [PageObject], zoom: CGFloat
    ) -> HitTarget? {
        let reach = HitTesting.touchRadius / max(zoom, 0.01)
        let strokes = drawing.strokes.map { stroke -> HitTesting.Stroke in
            let bounds = stroke.renderBounds
            let transform = stroke.transform
            let scale = sqrt(abs(transform.a * transform.d - transform.b * transform.c))
            let widest = stroke.path.reduce(CGFloat(0)) { max($0, $1.size.width, $1.size.height) }
            let width = widest * (scale > 0 ? scale : 1)
            let tolerance = max(reach, width / 2)
            guard bounds.insetBy(dx: -tolerance, dy: -tolerance).contains(point) else {
                return HitTesting.Stroke(points: [], width: width, bounds: bounds)
            }
            let points = stroke.path.interpolatedPoints(by: .distance(2)).map {
                $0.location.applying(transform)
            }
            return HitTesting.Stroke(points: points, width: width, bounds: bounds)
        }
        let candidates = objects.compactMap { object -> HitTesting.Object? in
            let kind: HitTesting.Object.Kind
            switch object.type {
            case .text: kind = .text
            case .image: kind = .image
            case .shape: kind = .shape
            case .unknown: return nil
            }
            let style = object.type == .shape ? ShapeStyle(object) : nil
            return HitTesting.Object(
                id: object.id, kind: kind, frame: object.frame.rect, isLine: style?.isLine ?? false,
                strokeWidth: CGFloat(style?.strokeWidth ?? 0))
        }
        return HitTesting.target(at: point, strokes: strokes, objects: candidates, zoom: zoom)
    }

    /// A finger tap on the page (this view's coordinates), from the
    /// notebook's `objectTap`; a Pencil tap with an insert tool comes here
    /// too.
    ///
    /// A tap on a stroke, a text box, an image or a shape selects it and the
    /// object menu appears; a tap on bare paper puts the selection down. With
    /// the Text or Shapes tool, a tap on bare paper places a box or a shape
    /// there instead, and a tap on a text box with the Text tool types in it.
    func handleObjectTap(at point: CGPoint) {
        guard !isExternallyEditing, !isReadOnly,
            Date().timeIntervalSince(longPressBegan) > 1
        else { return }
        if lasso?.menuContains(point) == true || objects.menuContains(point) { return }
        let tool = delegate?.pageViewToolState(self).active ?? .lasso
        // A tap on the ink selection keeps it.
        if let lasso, lasso.hasSelection, lasso.isOverSelection(point) { return }
        let target = hitTarget(at: point)
        switch target {
        case .text(let id)?, .image(let id)?, .shape(let id)?:
            let isText: Bool
            if case .text? = target { isText = true } else { isText = false }
            if objects.selectedID == id {
                if isText { objects.beginEditingSelected() }
                return
            }
            selectObject(id)
            if isText, tool == .text {
                objects.beginEditingSelected()
            } else {
                delegate?.pageView(self, didSelect: selectionKind ?? .mixed)
            }
        case .ink(let strokes)? where !tool.isInsert:
            selectInk(strokes)
            delegate?.pageView(self, didSelect: .ink)
        case .ink?, nil:
            if hasSelection {
                putDownSelection()
            } else if tool == .text || tool == .shape {
                place(tool, at: point)
            }
        }
    }

    private var isReadOnly: Bool { delegate?.pageViewIsReadOnly(self) ?? false }

    // MARK: - Insert

    /// The text box the Text tool places at `point` (page points): its left
    /// edge at the tap and its first line on it, at the text slot's size and
    /// colour.
    func textObject(at point: CGPoint, text: String = "", fontSize: Double? = nil) -> PageObject {
        let settings = delegate?.pageViewToolState(self).settings
        let size = fontSize ?? settings?.width(for: .text) ?? PageObject.defaultFontSize
        let width = min(420, max(160, pageSize.width - point.x - 24))
        let x = max(12, min(point.x, pageSize.width - width - 12))
        var object = PageObject(
            type: .text,
            frame: PageObject.Frame(
                x: Double(x), y: Double(max(12, point.y - CGFloat(size) * 0.8)),
                width: Double(width), height: max(44, size * 1.6)),
            text: text, fontSize: size)
        if let color = settings?.color(for: .text).flatMap(ToolSettings.normalizedColor) {
            object.additionalFields["color"] = .string(color)
        }
        return object
    }

    /// Places a text box and types in it at `point` (page points).
    func placeText(at point: CGPoint, text: String = "", fontSize: Double? = nil) {
        insertObject(textObject(at: point, text: text, fontSize: fontSize))
        delegate?.pageView(self, didPlace: .text)
        UIAccessibility.post(notification: .layoutChanged, argument: nil)
    }

    /// Places a shape of the Shapes tool's kind, width and colour.
    func placeShape(frame: PageObject.Frame) {
        guard let settings = delegate?.pageViewToolState(self).settings else { return }
        insertObject(ShapeStyle(tools: settings).object(frame: frame), edit: false)
        delegate?.pageView(self, didPlace: .shape)
        UIAccessibility.post(notification: .announcement, argument: "\(settings.shapeKind.label) placed")
    }

    /// A tap with an insert tool at `point` (this view's coordinates).
    private func place(_ tool: PaperTool, at point: CGPoint) {
        guard isLive else { return }
        let scale = max(zoom, 0.01)
        let pagePoint = CGPoint(x: point.x / scale, y: point.y / scale)
        switch tool {
        case .text: placeText(at: pagePoint)
        case .shape: placeShape(frame: ShapeStyle.tapFrame(at: pagePoint))
        default: break
        }
    }

    @objc private func handleInsertTap(_ gesture: UITapGestureRecognizer) {
        guard gesture.state == .ended else { return }
        handleObjectTap(at: gesture.location(in: self))
    }

    @objc private func handleShapeDrag(_ gesture: UIPanGestureRecognizer) {
        let scale = max(zoom, 0.01)
        let location = gesture.location(in: self)
        switch gesture.state {
        case .began:
            let travel = gesture.translation(in: self)
            let start =
                shapeTouchDown ?? CGPoint(x: location.x - travel.x, y: location.y - travel.y)
            putDownSelection()
            shapeDragStart = CGPoint(x: start.x / scale, y: start.y / scale)
            showShapePreview(to: CGPoint(x: location.x / scale, y: location.y / scale))
        case .changed:
            showShapePreview(to: CGPoint(x: location.x / scale, y: location.y / scale))
        case .ended:
            showShapePreview(to: CGPoint(x: location.x / scale, y: location.y / scale))
            endShapeDrag(place: true, at: CGPoint(x: location.x / scale, y: location.y / scale))
        default:
            endShapeDrag(place: false)
        }
    }

    /// Draws the box a shape drag spans so far, as the shape will look.
    private func showShapePreview(to end: CGPoint) {
        guard let start = shapeDragStart, let settings = delegate?.pageViewToolState(self).settings
        else { return }
        let style = ShapeStyle(tools: settings)
        let object = style.object(frame: ShapeStyle.dragFrame(from: start, to: end, kind: style.kind))
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        var transform = CGAffineTransform(scaleX: zoom, y: zoom)
        shapePreview.path = PageRenderer.shapePath(for: object).flatMap { $0.copy(using: &transform) }
        shapePreview.lineWidth = CGFloat(style.strokeWidth) * zoom
        shapePreview.strokeColor = PageRenderer.displayColor(hex: style.stroke, dark: isDarkPaper)
        CATransaction.commit()
    }

    /// A drag that travelled too little to make a box places the tap's shape.
    private func endShapeDrag(place: Bool, at end: CGPoint? = nil) {
        defer {
            shapeDragStart = nil
            shapePreview.path = nil
        }
        guard place, let start = shapeDragStart, let end, isLive,
            let settings = delegate?.pageViewToolState(self).settings
        else { return }
        let kind = settings.shapeKind
        let frame = ShapeStyle.dragFrame(from: start, to: end, kind: kind)
        if hypot(frame.width, frame.height) < 8 {
            placeShape(frame: ShapeStyle.tapFrame(at: start))
        } else {
            placeShape(frame: frame)
        }
    }

    // MARK: - Long press

    @objc private func handleLongPress(_ gesture: UILongPressGestureRecognizer) {
        guard gesture.state == .began else { return }
        longPressBegan = Date()
        let point = gesture.location(in: self)
        guard let interaction = interactions.compactMap({ $0 as? UIEditMenuInteraction }).first
        else { return }
        interaction.presentEditMenu(
            with: UIEditMenuConfiguration(identifier: nil, sourcePoint: point))
    }

    /// What the long-press menu offers at `point` (this view's coordinates).
    func longPressActions(at point: CGPoint) -> [UIAction] {
        let scale = max(zoom, 0.01)
        let pagePoint = CGPoint(x: point.x / scale, y: point.y / scale)
        var actions: [UIAction] = []
        if PaperClip.hasPasteable {
            actions.append(
                UIAction(title: "Paste", image: UIImage(systemName: "doc.on.clipboard")) {
                    [weak self] _ in
                    guard let self else { return }
                    self.delegate?.pageView(self, pasteAt: pagePoint)
                })
        }
        actions.append(
            UIAction(title: "Insert Text", image: UIImage(systemName: "character.textbox")) {
                [weak self] _ in
                guard let self else { return }
                self.putDownSelection()
                self.placeText(at: pagePoint)
            })
        return actions
    }

    // MARK: - Zoom

    /// Frames resize and the canvas's own `zoomScale` follows. The parent
    /// scroll view never applies a transform: a transformed `PKCanvasView`
    /// draws blurry ink and puts strokes in the wrong place.
    ///
    /// A board's canvas zooms itself (`BoardCanvasHost`) and tells this view.
    private func applyZoom() {
        guard let canvas else { return }
        guard !isBoard else {
            lasso?.zoom = zoom
            objects.zoom = zoom
            highlight.zoom = zoom
            return
        }
        canvas.minimumZoomScale = zoom
        canvas.maximumZoomScale = zoom
        canvas.zoomScale = zoom
        canvas.contentSize = CGSize(width: pageSize.width * zoom, height: pageSize.height * zoom)
        lasso?.zoom = zoom
        objects.zoom = zoom
    }

    // MARK: - Hover

    @objc private func handleHover(_ gesture: UIHoverGestureRecognizer) {
        guard let delegate else { return }
        switch gesture.state {
        case .began, .changed:
            lastHoverPoint = CGPoint(
                x: gesture.location(in: self).x / max(zoom, 0.01),
                y: gesture.location(in: self).y / max(zoom, 0.01))
            hover.show(
                at: gesture.location(in: self),
                radius: delegate.pageViewToolState(self).hoverRadius * zoom)
        default:
            hover.hide()
        }
    }
}

extension PageView: PKCanvasViewDelegate {
    func canvasViewDrawingDidChange(_ canvasView: PKCanvasView) {
        refreshDebugValue()
        guard !isApplyingDrawing else { return }
        delegate?.pageViewDrawingChanged(self)
    }

    func canvasViewDidBeginUsingTool(_ canvasView: PKCanvasView) {
        delegate?.pageViewDidBeginUsingTool(self)
    }

    func canvasViewDidEndUsingTool(_ canvasView: PKCanvasView) {
        delegate?.pageViewDidEndUsingTool(self)
    }
}

// MARK: - The page's own gestures

extension PageView: UIGestureRecognizerDelegate {
    /// The insert tap and the shape drag listen only while their tool is
    /// selected, and none of the three on a page another window is writing,
    /// a page without a canvas, or in Read Only.
    override func gestureRecognizerShouldBegin(_ gestureRecognizer: UIGestureRecognizer) -> Bool {
        guard gestureRecognizer === insertTap || gestureRecognizer === shapeDrag
            || gestureRecognizer === longPress
        else { return super.gestureRecognizerShouldBegin(gestureRecognizer) }
        guard isLive, !isExternallyEditing, !isReadOnly else { return false }
        let tool = delegate?.pageViewToolState(self).active
        if gestureRecognizer === insertTap { return tool == .text || tool == .shape }
        if gestureRecognizer === shapeDrag { return tool == .shape }
        let point = gestureRecognizer.location(in: self)
        return lasso?.menuContains(point) != true && !objects.menuContains(point)
    }

    func gestureRecognizer(
        _ gestureRecognizer: UIGestureRecognizer,
        shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer
    ) -> Bool {
        true
    }

    /// Where a shape drag's touch came down. A pan begins only once it has
    /// travelled, and by then a quick drag is well past where it started.
    func gestureRecognizer(
        _ gestureRecognizer: UIGestureRecognizer, shouldReceive touch: UITouch
    ) -> Bool {
        if gestureRecognizer === shapeDrag { shapeTouchDown = touch.location(in: self) }
        return true
    }
}

// MARK: - The long-press menu

extension PageView: @preconcurrency UIEditMenuInteractionDelegate {
    func editMenuInteraction(
        _ interaction: UIEditMenuInteraction, menuFor configuration: UIEditMenuConfiguration,
        suggestedActions: [UIMenuElement]
    ) -> UIMenu? {
        UIMenu(children: longPressActions(at: configuration.sourcePoint))
    }
}

// MARK: - Objects

extension PageView: ObjectsContainerDelegate {
    func objectsContainer(_ container: ObjectsContainerView, didChange objects: [PageObject]) {
        page.objects = objects
        refreshDebugValue()
        delegate?.pageView(self, didChangeObjects: objects)
    }

    /// Object edits register on the canvas's own undo manager, so Cmd-Z is one
    /// history whether the last thing you did was write a word or move a photo.
    func objectsContainerUndoManager(_ container: ObjectsContainerView) -> UndoManager? {
        canvas?.undoManager ?? undoManager
    }

    /// While an object is selected the objects layer takes the touches; the
    /// canvas gets them back the moment the selection is dropped, because
    /// nothing may come between the pen and the page.
    func objectsContainer(_ container: ObjectsContainerView, didChangeEditing isEditing: Bool) {
        isEditingObjects = isEditing
        refreshCanvasInteraction()
        refreshDebugValue()
        delegate?.pageView(self, didChangeSelection: !isEditing)
    }

    func objectsContainer(_ container: ObjectsContainerView, imageFor assetPath: String) -> UIImage?
    {
        delegate?.pageView(self, imageForAsset: assetPath)
    }

    func objectsContainer(
        _ container: ObjectsContainerView, copy objects: [PageObject], image: UIImage?,
        string: String?
    ) {
        PaperClip(drawing: nil, objects: objects, notebookPath: notebookPath)
            .write(image: image, string: string)
        UIAccessibility.post(notification: .announcement, argument: "Copied")
    }

    func objectsContainer(_ container: ObjectsContainerView, share image: UIImage, from rect: CGRect) {
        delegate?.pageView(self, share: image, from: convert(rect, from: container))
    }

    /// The text tool's sizes and the Shapes tool's widths, beside the
    /// object's own values.
    func objectsContainer(_ container: ObjectsContainerView, menuContextFor object: PageObject)
        -> ObjectMenuContext
    {
        let settings = delegate?.pageViewToolState(self).settings
        var context = ObjectMenuContext()
        switch object.type {
        case .text:
            let size = object.fontSize ?? PageObject.defaultFontSize
            context.sizes = Array(Set((settings?.slots[.text]?.widths ?? []) + [size])).sorted()
            context.size = size
            if case .string(let hex)? = object.additionalFields["color"] { context.color = hex }
        case .shape:
            let style = ShapeStyle(object)
            context.widths = Array(Set((settings?.slots[.shape]?.widths ?? []) + [style.strokeWidth]))
                .sorted()
            context.width = style.strokeWidth
            context.color = style.stroke
            context.fill = style.fill
        case .image, .unknown:
            break
        }
        return context
    }
}

// MARK: - Drop

extension PageView: UIDropInteractionDelegate {
    func dropInteraction(_ interaction: UIDropInteraction, canHandle session: UIDropSession) -> Bool
    {
        session.canLoadObjects(ofClass: UIImage.self)
            || session.hasItemsConforming(toTypeIdentifiers: [UTType.pdf.identifier])
    }

    func dropInteraction(
        _ interaction: UIDropInteraction, sessionDidUpdate session: UIDropSession
    ) -> UIDropProposal {
        UIDropProposal(operation: .copy)
    }

    func dropInteraction(_ interaction: UIDropInteraction, performDrop session: UIDropSession) {
        let point = session.location(in: self)
        let pagePoint = CGPoint(x: point.x / max(zoom, 0.01), y: point.y / max(zoom, 0.01))
        for item in session.items {
            let provider = item.itemProvider
            let name = provider.suggestedName ?? "dropped"
            if provider.hasItemConformingToTypeIdentifier(UTType.pdf.identifier) {
                provider.loadDataRepresentation(forTypeIdentifier: UTType.pdf.identifier) {
                    [weak self] data, _ in
                    guard let data else { return }
                    Task { @MainActor in
                        guard let self else { return }
                        self.delegate?.pageView(
                            self, didDropPDF: data, name: name.hasSuffix(".pdf") ? name : name + ".pdf")
                    }
                }
            } else if provider.canLoadObject(ofClass: UIImage.self) {
                provider.loadObject(ofClass: UIImage.self) { [weak self] object, _ in
                    // Off the main actor already: made upright and encoded
                    // here, so the page only stores bytes.
                    guard let image = object as? UIImage, let cgImage = image.cgImage,
                        let normalized = ImageImport.normalized(
                            cgImage: cgImage, orientation: image.imageOrientation,
                            scale: image.scale)
                    else { return }
                    Task { @MainActor in
                        guard let self else { return }
                        self.delegate?.pageView(
                            self, didDropImage: normalized.data,
                            name: name + "." + normalized.extension, at: pagePoint)
                    }
                }
            }
        }
    }
}

/// The page as one accessibility element. A board's value is read live,
/// because it changes with every pan and pinch.
final class CanvasElement: UIAccessibilityElement {
    var liveValue: (() -> String?)?

    override var accessibilityValue: String? {
        get { liveValue?() ?? super.accessibilityValue }
        set { super.accessibilityValue = newValue }
    }
}
