import PaperCore
import PencilKit
import UIKit
import UniformTypeIdentifiers

/// The paper itself: `#fdfcfa` plus the ruling.
///
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

/// A plain `UIView` rather than a `CALayer` subclass so the drawing context is
/// UIKit's y-down one -- the same coordinate space `TemplateGeometry` uses for
/// the off-screen render, which is what keeps screen and render identical.
final class TemplateLayer: UIView {
    var template: Template = .ruled { didSet { setNeedsDisplay() } }
    var pageSize: CGSize = .zero { didSet { setNeedsDisplay() } }

    override init(frame: CGRect) {
        super.init(frame: frame)
        isOpaque = true
        isUserInteractionEnabled = false
        contentMode = .redraw
        backgroundColor = UIColor(PaperTokens.paper)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("TemplateLayer is created in code") }

    override func draw(_ rect: CGRect) {
        guard pageSize.width > 0, pageSize.height > 0,
            let context = UIGraphicsGetCurrentContext()
        else { return }
        context.scaleBy(x: bounds.width / pageSize.width, y: bounds.height / pageSize.height)
        TemplateGeometry.draw(template: template, size: pageSize, in: context)
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
    func pageViewDrawingPolicy(_ pageView: PageView) -> PKCanvasViewDrawingPolicy
    func pageView(_ pageView: PageView, didChangeSelection isEmpty: Bool)
    /// Objects changed: write them to `page.json` and mark the page dirty.
    func pageView(_ pageView: PageView, didChangeObjects objects: [PageObject])
    /// Image bytes for an object's asset, loaded through the store.
    func pageView(_ pageView: PageView, imageForAsset assetPath: String) -> UIImage?
    /// Something was dropped on the page: image bytes, or a PDF to import.
    func pageView(_ pageView: PageView, didDropImage data: Data, name: String, at point: CGPoint)
    func pageView(_ pageView: PageView, didDropPDF data: Data, name: String)
}

/// One page.
///
/// The composition is fixed: template, then (M4) PDF background and objects,
/// then ink, then the lasso overlay. Only the pages near the viewport get a
/// real `PKCanvasView`; the rest show their render, which is what lets a
/// 200-page notebook scroll.
final class PageView: UIView {
    private(set) var page: Page
    let notebookPath: String
    weak var delegate: PageViewDelegate?

    let template = TemplateLayer()
    private let pdfBackground = PDFBackgroundLayer()
    private let renderView = UIImageView()
    private(set) var canvas: PKCanvasView?
    let objects = ObjectsContainerView()
    private let hover = HoverPreviewLayer()
    private let highlight = HighlightLayer()
    private(set) var lasso: LassoOverlayView?
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

    /// Where the Pencil last hovered, in page points. The `+` menu inserts
    /// there rather than at a fixed spot, so "put a text box here" means here.
    private(set) var lastHoverPoint: CGPoint?

    init(page: Page, notebookPath: String) {
        self.page = page
        self.notebookPath = notebookPath
        super.init(frame: CGRect(origin: .zero, size: page.size.points))
        build()
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("PageView is created in code") }

    private func build() {
        backgroundColor = UIColor(PaperTokens.paper)
        layer.cornerRadius = 2
        layer.shadowColor = UIColor.black.cgColor
        layer.shadowOpacity = 0.08
        layer.shadowRadius = 6
        layer.shadowOffset = CGSize(width: 0, height: 2)

        template.template = page.template
        template.pageSize = pageSize
        addSubview(template)

        addSubview(pdfBackground)

        renderView.contentMode = .scaleAspectFill
        renderView.isUserInteractionEnabled = false
        addSubview(renderView)

        objects.delegate = self
        objects.growthOffset = { [weak self] in self?.growthTranslation ?? .zero }
        addSubview(objects)
        objects.setObjects(page.objects)

        addSubview(hover)
        addSubview(highlight)

        let hoverGesture = UIHoverGestureRecognizer(target: self, action: #selector(handleHover))
        addGestureRecognizer(hoverGesture)

        // A tap picks up an object, and a tap on bare paper puts it down. Ink
        // never travels through this: the canvas above keeps its own touches,
        // and the objects layer only takes them while something is selected.
        let tap = UITapGestureRecognizer(target: self, action: #selector(handleObjectTap(_:)))
        tap.cancelsTouchesInView = false
        addGestureRecognizer(tap)

        addInteraction(UIDropInteraction(delegate: self))

        // A container rather than an element: the page itself is one element,
        // and the selection inspector -- when there is one -- is another. A
        // plain element would hide the inspector's buttons from VoiceOver
        // entirely.
        isAccessibilityElement = false
        canvasElement.accessibilityTraits = [.allowsDirectInteraction]
    }

    private lazy var canvasElement = UIAccessibilityElement(accessibilityContainer: self)

    override var accessibilityElements: [Any]? {
        get {
            canvasElement.accessibilityFrameInContainerSpace = bounds
            var elements: [Any] = [canvasElement]
            if let inspector = lasso?.accessibleInspector { elements.append(inspector) }
            return elements
        }
        set { _ = newValue }
    }

    /// "Page 3 of 12, drawing canvas" -- set by the stack, which is the only
    /// thing that knows the page's number.
    func setAccessibility(index: Int, total: Int) {
        canvasElement.accessibilityLabel = "Page \(index) of \(total), drawing canvas"
        canvasElement.accessibilityHint =
            "Write with the Apple Pencil. Pinch with two fingers to zoom."
        refreshDebugValue()
    }

    /// How much ink is on the page, published as the element's value so a UI
    /// test can tell a captured lasso loop from a stroke that was drawn by
    /// mistake. DEBUG only: the shipping value belongs to the transcript.
    func refreshDebugValue() {
        #if DEBUG
            canvasElement.accessibilityValue =
                "strokes:\(canvas?.drawing.strokes.count ?? 0) "
                + "extent:\(Int(pageSize.width.rounded()))x\(Int(pageSize.height.rounded()))"
        #endif
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        template.frame = bounds
        pdfBackground.frame = bounds
        renderView.frame = bounds
        objects.frame = bounds
        hover.frame = bounds
        highlight.frame = bounds
        highlight.zoom = zoom
        lasso?.frame = bounds
        canvas?.frame = bounds
    }

    // MARK: - Live window

    var isLive: Bool { canvas != nil }

    /// Gives the page a real canvas. `drawingData` is the page's stored ink.
    func goLive(drawingData: Data?) {
        guard canvas == nil, let delegate else { return }
        let canvas = PKCanvasView(frame: bounds)
        // PencilKit derives ink colour from the trait collection, so a dark
        // system appearance would turn black ink white. The page is always
        // light paper; force it.
        canvas.overrideUserInterfaceStyle = .light
        canvas.backgroundColor = .clear
        canvas.isOpaque = false
        canvas.isScrollEnabled = false
        canvas.bouncesZoom = false
        canvas.showsVerticalScrollIndicator = false
        canvas.showsHorizontalScrollIndicator = false
        canvas.contentInsetAdjustmentBehavior = .never
        canvas.drawingPolicy = delegate.pageViewDrawingPolicy(self)
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

        let overlay = LassoOverlayView(canvas: canvas)
        overlay.frame = bounds
        overlay.growthOffset = { [weak self] in self?.growthTranslation ?? .zero }
        overlay.onSelectionChange = { [weak self] isEmpty in
            guard let self else { return }
            self.delegate?.pageView(self, didChangeSelection: isEmpty)
        }
        overlay.onConvertToText = { [weak self] text, rect in
            self?.onConvertToText?(text, rect)
        }
        overlay.onAIAction = { [weak self] action, data, rect in
            self?.onAIAction?(action, data, rect)
        }
        overlay.onCopyLink = { [weak self] rect in self?.onCopyLink?(rect) }
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
    /// object edits that canvas's history ordered them against.
    func goDormant(render: UIImage?) {
        lasso?.clearSelection()
        lasso?.removeFromSuperview()
        lasso = nil
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
        canvas?.drawing.dataRepresentation()
    }

    var drawing: PKDrawing? { canvas?.drawing }

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
        let current = canvas.drawing
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
    /// of the time the canvas must see every touch. The overlay sits above the
    /// objects layer, so a selected object's handles and inspector would be out
    /// of reach under it: the object is put down as the Lasso is picked up.
    func setLassoActive(_ active: Bool) {
        isLassoActive = active
        refreshCanvasInteraction()
        if active {
            objects.deselect()
        } else {
            lasso?.clearSelection()
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
    /// one is, the overlay only in Lasso mode -- and none of them while
    /// another window is writing the page.
    private func refreshCanvasInteraction() {
        let open = !isExternallyEditing
        canvas?.isUserInteractionEnabled = open && !isEditingObjects
        objects.isUserInteractionEnabled = open && isEditingObjects
        lasso?.isUserInteractionEnabled = open && isLassoActive
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
        self.page = page
        template.template = page.template
        template.pageSize = pageSize
        objects.setObjects(page.objects)
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
                let moved = page.objects.map { object -> PageObject in
                    var copy = object
                    copy.frame = PageObject.Frame(object.frame.rect.applying(transform))
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
    var insertionPoint: CGPoint {
        lastHoverPoint ?? CGPoint(x: pageSize.width / 2, y: pageSize.height / 2)
    }

    func insertObject(_ object: PageObject) {
        objects.select(nil)
        objects.insert(object)
    }

    func deselectObjects() {
        objects.deselect()
    }

    var hasSelectedObject: Bool { objects.selectedID != nil }

    @objc private func handleObjectTap(_ gesture: UITapGestureRecognizer) {
        handleObjectTap(at: gesture.location(in: self))
    }

    /// In Lasso mode the overlay owns the page: a tap there belongs to the
    /// loop, never to the objects underneath it. A page another window is
    /// writing takes no taps at all.
    func handleObjectTap(at point: CGPoint) {
        guard !isExternallyEditing, lasso?.isUserInteractionEnabled != true else { return }
        if let id = objects.objectID(at: point) {
            objects.select(id)
        } else if objects.selectedID != nil {
            objects.deselect()
        }
    }

    // MARK: - Zoom

    /// Frames resize and the canvas's own `zoomScale` follows. The parent
    /// scroll view never applies a transform: a transformed `PKCanvasView`
    /// draws blurry ink and puts strokes in the wrong place.
    private func applyZoom() {
        guard let canvas else { return }
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


// MARK: - Objects

extension PageView: ObjectsContainerDelegate {
    func objectsContainer(_ container: ObjectsContainerView, didChange objects: [PageObject]) {
        page.objects = objects
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
        delegate?.pageView(self, didChangeSelection: !isEditing)
    }

    func objectsContainer(_ container: ObjectsContainerView, imageFor assetPath: String) -> UIImage?
    {
        delegate?.pageView(self, imageForAsset: assetPath)
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
