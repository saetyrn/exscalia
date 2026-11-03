import PaperCore
import PencilKit
import UIKit

/// What draws a board for `BoardViewController`: the one place the answer to
/// how a board is put on screen lives (contract §6.7). Canvas space is page
/// space -- points from the top-left of the board's extent.
@MainActor protocol BoardCanvasHost: AnyObject {
    var view: UIView { get }
    /// 0.05...4.
    var zoom: CGFloat { get }
    /// What is on screen, in canvas space.
    var visibleRect: CGRect { get }
    func load(drawing: PKDrawing, objects: [PageObject])
    /// The whole board's ink, in canvas space.
    func currentDrawing() -> PKDrawing
    func setTool(_ tool: PKTool)
    /// Zooms keeping the canvas point `anchor` where it is on screen.
    func setZoom(_ zoom: CGFloat, anchor: CGPoint, animated: Bool)
    /// Puts the canvas point `point` in the middle of the screen.
    func scroll(toCenter point: CGPoint, animated: Bool)
    var onDrawingChanged: (() -> Void)? { get set }
    var onViewportChanged: (() -> Void)? { get set }
}

/// A board's `PKCanvasView`, which is also the scroll view that pans and
/// zooms the board.
///
/// It says when it lays out: a scroll view lays out on every change of its
/// offset and zoom, inside the same transaction, so whatever is moved from
/// here -- the grid, the board's page and its overlays -- moves in the same
/// frame as the ink.
final class BoardCanvasView: PKCanvasView {
    var onLayout: (() -> Void)?
    /// The board's page, over the ink: the lasso overlay and the menus.
    weak var overlay: UIView?
    /// The board's objects layer, under the ink, which takes touches only
    /// while an object is selected.
    weak var underlay: UIView?

    override func layoutSubviews() {
        super.layoutSubviews()
        onLayout?()
    }

    /// The board's page, which is the canvas's element and holds the menus,
    /// as a page's is; PencilKit's own views would otherwise stand in for it.
    override var accessibilityElements: [Any]? {
        get { overlay.map { [$0] } }
        set { _ = newValue }
    }

    /// What is over the ink first, then a selected object under it, then
    /// PencilKit. A page gets the same order by switching its canvas off
    /// while an object is selected; this canvas is the board's scroll view
    /// and stays on, so the fingers can pan and pinch whatever is selected.
    override func hitTest(_ point: CGPoint, with event: UIEvent?) -> UIView? {
        guard isUserInteractionEnabled, !isHidden, alpha > 0.01,
            self.point(inside: point, with: event)
        else { return nil }
        if let overlay, !overlay.isHidden,
            let hit = overlay.hitTest(convert(point, to: overlay), with: event)
        {
            return hit
        }
        if let underlay, underlay.isUserInteractionEnabled, !underlay.isHidden,
            let hit = underlay.hitTest(convert(point, to: underlay), with: event)
        {
            return hit
        }
        return super.hitTest(point, with: event)
    }
}

/// The board as one `PKCanvasView` acting as its own scroll view, measured on
/// the prototype (contract §0, "amendments after the whiteboard prototype").
///
/// `contentSize` is the board's extent at 100 %, and the zoom is the canvas's
/// own `zoomScale`, between `minimumZoomScale` and `maximumZoomScale`, with
/// its own pinch: PencilKit owns that zoom and draws its ink sharp at it.
/// Never a transform, and never a page in an outer scroll view zoomed by
/// resizing frames, which drew soft ink at 400 % and stalled on the first
/// pan. The canvas's pan and pinch take fingers only; its owner switches them
/// off while the Pencil is down.
///
/// Under the canvas sits the board's paper and grid (`BoardBackgroundView`),
/// in screen space; inside it, the board's `PageView` and its objects
/// (`PageView.goLive(onBoard:gestureHost:drawingData:)`). Both follow the
/// canvas from its `layoutSubviews`.
@MainActor
final class SingleCanvasBoardHost: BoardCanvasHost {
    /// The background and the canvas, filling whatever holds them.
    let view = UIView()
    let canvas = BoardCanvasView()
    /// The board showing, if any.
    private(set) var page: PageView?
    private(set) var extent: CGSize = BoardGeometry.defaultExtent
    var onDrawingChanged: (() -> Void)?
    var onViewportChanged: (() -> Void)?
    /// Set while `place(center:zoom:)` moves the canvas, so the move is not
    /// mistaken for the person's.
    private(set) var isPlacing = false

    init() {
        view.clipsToBounds = true
        view.backgroundColor = PaperColor.cream.uiColor
        canvas.frame = view.bounds
        canvas.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        configureCanvas()
        view.addSubview(canvas)
    }

    private func configureCanvas() {
        canvas.backgroundColor = .clear
        canvas.isOpaque = false
        canvas.drawingPolicy = EditorInput.drawingPolicy
        canvas.contentInsetAdjustmentBehavior = .never
        canvas.showsHorizontalScrollIndicator = false
        canvas.showsVerticalScrollIndicator = false
        canvas.scrollsToTop = false
        // iOS 26 blurs a scroll view's content under the bars; on a board
        // that blurs the ink and leaves the grid sharp.
        if #available(iOS 26.0, *) {
            canvas.topEdgeEffect.isHidden = true
            canvas.bottomEdgeEffect.isHidden = true
            canvas.leftEdgeEffect.isHidden = true
            canvas.rightEdgeEffect.isHidden = true
        }
        canvas.bouncesZoom = true
        canvas.minimumZoomScale = BoardGeometry.zoomRange.lowerBound
        canvas.maximumZoomScale = BoardGeometry.zoomRange.upperBound
        canvas.zoomScale = 1
        canvas.contentSize = extent
        // Fingers navigate and the Pencil writes: the Pencil never drives the
        // pan or the pinch. Under the DEBUG `-debugFingerInk` one finger inks,
        // so panning takes two.
        let direct = [NSNumber(value: UITouch.TouchType.direct.rawValue)]
        canvas.panGestureRecognizer.allowedTouchTypes = direct
        canvas.pinchGestureRecognizer?.allowedTouchTypes = direct
        canvas.panGestureRecognizer.minimumNumberOfTouches = EditorInput.fingerInk ? 2 : 1
        canvas.isAccessibilityElement = false
        canvas.onLayout = { [weak self] in self?.canvasDidLayout() }
    }

    // MARK: - Geometry

    var zoom: CGFloat { canvas.zoomScale }

    var viewport: CGSize { canvas.bounds.size }

    /// The canvas point in the middle of the screen.
    var viewportCenter: CGPoint {
        BoardGeometry.viewportCenter(
            contentOffset: canvas.contentOffset, zoom: zoom, viewport: viewport)
    }

    var visibleRect: CGRect {
        BoardGeometry.visibleRect(center: viewportCenter, zoom: zoom, viewport: viewport)
    }

    /// The zoom floor: 5 %, or less far out than that if the board would no
    /// longer cover the screen.
    var minimumZoom: CGFloat {
        guard viewport.width > 0, viewport.height > 0 else { return BoardGeometry.zoomRange.lowerBound }
        return BoardGeometry.coveringZoom(extent: extent, viewport: viewport)
    }

    /// `zoom` held between the floor and 400 %.
    func clamp(_ zoom: CGFloat) -> CGFloat {
        min(max(BoardGeometry.clampZoom(zoom), minimumZoom), BoardGeometry.zoomRange.upperBound)
    }

    /// The canvas point under a point of `view`.
    func canvasPoint(atScreen point: CGPoint) -> CGPoint {
        let scale = max(zoom, 0.0001)
        return CGPoint(
            x: (canvas.contentOffset.x + point.x) / scale,
            y: (canvas.contentOffset.y + point.y) / scale)
    }

    /// Where a canvas point is in `view`.
    func screenPoint(ofCanvas point: CGPoint) -> CGPoint {
        CGPoint(
            x: point.x * zoom - canvas.contentOffset.x, y: point.y * zoom - canvas.contentOffset.y)
    }

    /// The viewport changed size -- a rotation, the sidebar, split view.
    func viewportDidResize() {
        canvas.minimumZoomScale = minimumZoom
        if canvas.zoomScale < canvas.minimumZoomScale {
            place(center: viewportCenter, zoom: canvas.minimumZoomScale)
        }
    }

    // MARK: - The board

    /// Shows `page`, a board, with its ink. The board before it goes; the
    /// caller has flushed it.
    func attach(_ page: PageView, drawingData: Data?, gestureHost: UIView) {
        detach()
        extent = page.pageSize
        // The extent is the content at 100 %: set while the zoom is 1.
        canvas.minimumZoomScale = min(BoardGeometry.zoomRange.lowerBound, 1)
        canvas.setZoomScale(1, animated: false)
        canvas.contentSize = extent
        canvas.contentOffset = .zero
        canvas.minimumZoomScale = minimumZoom
        if let background = page.boardBackground {
            background.frame = view.bounds
            background.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            view.insertSubview(background, belowSubview: canvas)
        }
        view.backgroundColor = page.page.paperColor.uiColor
        page.goLive(onBoard: canvas, gestureHost: gestureHost, drawingData: drawingData)
        canvas.overlay = page
        canvas.underlay = page.objects
        self.page = page
        follow()
    }

    /// Takes the board off the canvas.
    func detach() {
        guard let page else { return }
        page.leaveBoard()
        page.boardBackground?.removeFromSuperview()
        canvas.overlay = nil
        canvas.underlay = nil
        self.page = nil
    }

    func load(drawing: PKDrawing, objects: [PageObject]) {
        guard let page else { return }
        page.applyDrawing(drawing)
        page.setObjects(objects)
        canvas.undoManager?.removeAllActions(withTarget: canvas)
        onDrawingChanged?()
    }

    func currentDrawing() -> PKDrawing {
        page?.drawing ?? canvas.drawing
    }

    func setTool(_ tool: PKTool) {
        canvas.tool = tool
    }

    // MARK: - Moving

    /// Sets the zoom and the offset at once, `center` (canvas space) in the
    /// middle of the screen, held inside the board.
    func place(center: CGPoint, zoom: CGFloat) {
        let zoom = clamp(zoom)
        isPlacing = true
        if abs(canvas.zoomScale - zoom) > 0.00001 {
            canvas.setZoomScale(zoom, animated: false)
        }
        canvas.contentOffset = offset(placing: center, zoom: zoom)
        isPlacing = false
        // Now rather than at the next layout pass, so whatever reads the
        // view straight after -- the pill, a test -- sees where it is.
        follow()
        onViewportChanged?()
    }

    /// The offset that puts `center` in the middle of the screen at `zoom`,
    /// inside the content.
    private func offset(placing center: CGPoint, zoom: CGFloat) -> CGPoint {
        let held = BoardGeometry.clampedCenter(
            center, zoom: zoom, viewport: viewport, extent: extent)
        let raw = BoardGeometry.contentOffset(center: held, zoom: zoom, viewport: viewport)
        let maxX = max(extent.width * zoom - viewport.width, 0)
        let maxY = max(extent.height * zoom - viewport.height, 0)
        return CGPoint(x: min(max(raw.x, 0), maxX), y: min(max(raw.y, 0), maxY))
    }

    func setZoom(_ zoom: CGFloat, anchor: CGPoint, animated: Bool) {
        self.zoom(to: zoom, keeping: anchor, at: screenPoint(ofCanvas: anchor), animated: animated)
    }

    func scroll(toCenter point: CGPoint, animated: Bool) {
        guard animated, !UIAccessibility.isReduceMotionEnabled else {
            place(center: point, zoom: zoom)
            return
        }
        canvas.setContentOffset(offset(placing: point, zoom: zoom), animated: true)
    }

    /// Zooms to `zoom` with `canvasPoint` ending up under `screenPoint` of
    /// `view`, held inside the board.
    func zoom(to zoom: CGFloat, keeping canvasPoint: CGPoint, at screenPoint: CGPoint, animated: Bool) {
        let zoom = clamp(zoom)
        let center = CGPoint(
            x: canvasPoint.x - (screenPoint.x - viewport.width / 2) / zoom,
            y: canvasPoint.y - (screenPoint.y - viewport.height / 2) / zoom)
        guard animated, !UIAccessibility.isReduceMotionEnabled else {
            place(center: center, zoom: zoom)
            return
        }
        let held = BoardGeometry.clampedCenter(
            center, zoom: zoom, viewport: viewport, extent: extent)
        canvas.zoom(
            to: BoardGeometry.visibleRect(center: held, zoom: zoom, viewport: viewport),
            animated: true)
    }

    // MARK: - Following the canvas

    private func canvasDidLayout() {
        follow()
        onViewportChanged?()
    }

    /// The grid, the page and its overlays, to where the ink is now.
    private func follow() {
        guard let page else { return }
        let zoom = self.zoom
        page.boardBackground?.update(
            zoom: zoom, contentOffset: canvas.contentOffset, extent: extent)
        let size = CGSize(width: extent.width * zoom, height: extent.height * zoom)
        if page.frame.size != size || page.frame.origin != .zero {
            page.frame = CGRect(origin: .zero, size: size)
        }
        if page.zoom != zoom { page.zoom = zoom }
    }
}
