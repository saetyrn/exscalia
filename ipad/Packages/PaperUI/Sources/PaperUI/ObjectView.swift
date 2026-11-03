import PaperCore
import UIKit

@MainActor
protocol ObjectViewDelegate: AnyObject {
    /// The object finished changing: persist it and put the inverse on the
    /// undo stack.
    func objectView(_ view: ObjectView, didCommit object: PageObject, actionName: String)
    func objectViewDidChangeCropMode(_ view: ObjectView)
    func objectViewNeedsLayout(_ view: ObjectView)
}

/// What text, image and shape objects have in common: a frame in page
/// points, a selection outline and handles.
///
/// The frame is always page points at zoom 1, so a page written at 2x and read
/// at 0.5x has objects in the same place as the ink around them; the view frame
/// is the page frame scaled, inflated by the handle margin so the handles sit
/// inside the view rather than escaping it.
///
/// An object view takes no touches of its own. A drag on a selected object or
/// one of its handles is the lasso's touch (`NotebookViewController
/// .routeLassoTouch`), which the scroll view waits for, and arrives here as
/// `beginInteraction`, `updateInteraction` and `endInteraction`.
class ObjectView: UIView {
    /// Room around the content for the handles to sit in.
    static let handleInset: CGFloat = 10
    /// How close to a handle's centre a touch takes it, in view points.
    static let handleReach: CGFloat = 22

    static func viewFrame(for rect: CGRect, zoom: CGFloat) -> CGRect {
        rect.standardized.applying(CGAffineTransform(scaleX: zoom, y: zoom))
            .insetBy(dx: -handleInset, dy: -handleInset)
    }

    enum Handle: CaseIterable, Sendable {
        case topLeft, top, topRight, right, bottomRight, bottom, bottomLeft, left

        var label: String {
            switch self {
            case .topLeft: return "Top left"
            case .top: return "Top"
            case .topRight: return "Top right"
            case .right: return "Right"
            case .bottomRight: return "Bottom right"
            case .bottom: return "Bottom"
            case .bottomLeft: return "Bottom left"
            case .left: return "Left"
            }
        }

        /// Unit position within the content rect.
        var unit: CGPoint {
            switch self {
            case .topLeft: return CGPoint(x: 0, y: 0)
            case .top: return CGPoint(x: 0.5, y: 0)
            case .topRight: return CGPoint(x: 1, y: 0)
            case .right: return CGPoint(x: 1, y: 0.5)
            case .bottomRight: return CGPoint(x: 1, y: 1)
            case .bottom: return CGPoint(x: 0.5, y: 1)
            case .bottomLeft: return CGPoint(x: 0, y: 1)
            case .left: return CGPoint(x: 0, y: 0.5)
            }
        }

        var isCorner: Bool { unit.x != 0.5 && unit.y != 0.5 }
        var movesLeft: Bool { unit.x == 0 }
        var movesRight: Bool { unit.x == 1 }
        var movesTop: Bool { unit.y == 0 }
        var movesBottom: Bool { unit.y == 1 }
    }

    /// What a touch on a selected object is doing.
    enum Interaction: Equatable, Sendable {
        case move
        case resize(Handle)
    }

    weak var delegate: ObjectViewDelegate?
    private(set) var object: PageObject

    var zoom: CGFloat = 1 {
        didSet {
            guard zoom != oldValue else { return }
            zoomChanged()
            setNeedsLayout()
        }
    }

    /// The page this object sits on is dark paper (`PaperAppearance`).
    var isDarkPaper = false {
        didSet {
            guard isDarkPaper != oldValue else { return }
            paperDidChange()
        }
    }

    var isSelected = false {
        didSet {
            guard isSelected != oldValue else { return }
            outline.isHidden = !isSelected
            refreshHandles()
            if !isSelected { endEditingContent() }
        }
    }

    /// Where subclasses put their content. Inset by the handle margin.
    let content = UIView()
    private let outline = CAShapeLayer()
    private var handleViews: [Handle: HandleView] = [:]
    /// The object as it was when the current interaction started, so one drag
    /// is one undo step.
    private var gestureStart: PageObject?
    private(set) var interaction: Interaction?

    init(object: PageObject) {
        self.object = object
        super.init(frame: .zero)
        backgroundColor = .clear

        outline.fillColor = UIColor.clear.cgColor
        outline.strokeColor = UIColor(PaperTokens.accent).cgColor
        outline.lineWidth = 1
        outline.lineDashPattern = [4, 3]
        outline.isHidden = true
        layer.addSublayer(outline)

        content.backgroundColor = .clear
        content.isUserInteractionEnabled = false
        addSubview(content)

        for handle in Handle.allCases {
            let view = HandleView(handle: handle)
            view.isHidden = true
            addSubview(view)
            handleViews[handle] = view
        }

        isAccessibilityElement = false
        accessibilityContainerType = .semanticGroup
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("ObjectView is created in code") }

    // MARK: - Contents

    func apply(_ object: PageObject) {
        self.object = object
        contentDidChange()
        refreshHandles()
        setNeedsLayout()
    }

    /// Subclass hook: the object's payload changed.
    func contentDidChange() {}
    /// Subclass hook: the zoom changed, so text has to be re-sized.
    func zoomChanged() {}
    /// Subclass hook: the page's paper turned dark or light.
    func paperDidChange() {}
    /// Subclass hook: a handle drag while the subclass is in a special mode
    /// (an image being cropped). Returns the object as it would be, or nil
    /// when the drag resizes as usual. `translation` is in page points.
    func specialResize(_ handle: Handle, from start: PageObject, translation: CGSize) -> PageObject? {
        nil
    }
    func beginEditingContent() {}
    func endEditingContent() {}
    /// Whether the object's own content is taking input -- text being typed.
    var isEditingContent: Bool { false }

    /// Which handles the object offers: corners scale it proportionally, and
    /// text and shapes also have edges that resize one side.
    var offersEdgeHandles: Bool { object.type == .text || object.type == .shape }

    /// The smallest an object may be resized to, in page points.
    var minimumSize: CGSize { CGSize(width: 32, height: 24) }

    /// Changes the model and tells the container, which persists and registers
    /// the undo.
    func update(_ mutate: (inout PageObject) -> Void, actionName: String) {
        var next = object
        mutate(&next)
        guard next != object else { return }
        object = next
        contentDidChange()
        setNeedsLayout()
        delegate?.objectView(self, didCommit: next, actionName: actionName)
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        let inset = Self.handleInset
        let contentRect = bounds.insetBy(dx: inset, dy: inset)
        content.frame = contentRect
        outline.path = UIBezierPath(rect: contentRect.insetBy(dx: -2, dy: -2)).cgPath
        for (handle, view) in handleViews {
            view.center = CGPoint(
                x: contentRect.minX + contentRect.width * handle.unit.x,
                y: contentRect.minY + contentRect.height * handle.unit.y)
            view.bounds = CGRect(x: 0, y: 0, width: 22, height: 22)
        }
        delegate?.objectViewNeedsLayout(self)
    }

    func refreshHandles() {
        for (handle, view) in handleViews {
            view.isHidden = !isSelected || (!handle.isCorner && !offersEdgeHandles)
        }
    }

    /// Handles sit outside the content rect, so hit testing has to look past
    /// the bounds a little.
    override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {
        if super.point(inside: point, with: event) { return true }
        guard isSelected else { return false }
        return bounds.insetBy(dx: -8, dy: -8).contains(point)
    }

    // MARK: - Interaction

    /// The handle under `point` (this view's coordinates), nearest first.
    func handle(at point: CGPoint) -> Handle? {
        guard isSelected else { return nil }
        var best: (Handle, CGFloat)?
        for (handle, view) in handleViews where !view.isHidden {
            let distance = hypot(point.x - view.center.x, point.y - view.center.y)
            if distance <= Self.handleReach, distance < (best?.1 ?? .infinity) {
                best = (handle, distance)
            }
        }
        return best?.0
    }

    /// Whether `point` (this view's coordinates) is on the object's body.
    func bodyContains(_ point: CGPoint) -> Bool {
        content.frame.insetBy(dx: -4, dy: -4).contains(point)
    }

    func beginInteraction(_ interaction: Interaction) {
        self.interaction = interaction
        gestureStart = object
    }

    /// Follows the touch. `translation` is how far it has travelled since the
    /// interaction began, in view points.
    func updateInteraction(translation: CGSize) {
        guard let start = gestureStart, let interaction else { return }
        let scale = max(zoom, 0.01)
        let page = CGSize(width: translation.width / scale, height: translation.height / scale)
        var next = start
        switch interaction {
        case .move:
            next.frame.x = start.frame.x + Double(page.width)
            next.frame.y = start.frame.y + Double(page.height)
        case .resize(let handle):
            if let special = specialResize(handle, from: start, translation: page) {
                next = special
            } else {
                next = Self.resized(start, handle: handle, by: page, minimum: minimumSize)
            }
        }
        object = next
        frame = Self.viewFrame(for: next.frame.rect, zoom: zoom)
        contentDidChange()
        setNeedsLayout()
    }

    /// Ends the interaction: one committed edit, or the object back where it
    /// began when `commit` is false (the system took the touch away).
    func endInteraction(commit: Bool) {
        guard let start = gestureStart, let interaction else { return }
        gestureStart = nil
        self.interaction = nil
        guard commit else {
            apply(start)
            frame = Self.viewFrame(for: start.frame.rect, zoom: zoom)
            return
        }
        guard object != start else { return }
        let name: String
        switch interaction {
        case .move: name = "Move Object"
        case .resize: name = object.crop != start.crop ? "Crop Image" : "Resize Object"
        }
        delegate?.objectView(self, didCommit: object, actionName: name)
    }

    /// An object resized by dragging `handle` by `delta` (page points).
    ///
    /// A corner scales the object proportionally about the opposite corner,
    /// and a text object's type with it. An edge moves that side only. A line
    /// or an arrow keeps the direction it runs in: its frame may have a
    /// negative width or height, and the resize works on the box it spans.
    static func resized(
        _ object: PageObject, handle: Handle, by delta: CGSize, minimum: CGSize
    ) -> PageObject {
        var next = object
        let rect = object.frame.rect.standardized
        // Never below the minimum, but never forced up to it either: a
        // horizontal line has no height and keeps none.
        let floor = CGSize(width: min(minimum.width, rect.width), height: min(minimum.height, rect.height))
        var result = rect
        if handle.isCorner {
            let corner = CGPoint(
                x: rect.minX + rect.width * handle.unit.x, y: rect.minY + rect.height * handle.unit.y)
            let anchor = CGPoint(
                x: rect.minX + rect.width * (1 - handle.unit.x),
                y: rect.minY + rect.height * (1 - handle.unit.y))
            let diagonal = CGSize(width: corner.x - anchor.x, height: corner.y - anchor.y)
            let length = diagonal.width * diagonal.width + diagonal.height * diagonal.height
            guard length > 0 else { return object }
            let moved = CGSize(width: diagonal.width + delta.width, height: diagonal.height + delta.height)
            var factor = (moved.width * diagonal.width + moved.height * diagonal.height) / length
            var smallest: CGFloat = 0
            if rect.width > 0 { smallest = max(smallest, floor.width / rect.width) }
            if rect.height > 0 { smallest = max(smallest, floor.height / rect.height) }
            factor = max(factor, smallest, 0.05)
            let width = rect.width * factor
            let height = rect.height * factor
            result = CGRect(
                x: handle.movesLeft ? anchor.x - width : anchor.x,
                y: handle.movesTop ? anchor.y - height : anchor.y, width: width, height: height)
            if object.type == .text {
                let size = object.fontSize ?? PageObject.defaultFontSize
                next.fontSize = min(max(size * Double(factor), 4), 400)
            }
        } else {
            if handle.movesLeft {
                let shift = min(delta.width, rect.width - floor.width)
                result.origin.x += shift
                result.size.width -= shift
            }
            if handle.movesRight {
                result.size.width = max(floor.width, rect.width + delta.width)
            }
            if handle.movesTop {
                let shift = min(delta.height, rect.height - floor.height)
                result.origin.y += shift
                result.size.height -= shift
            }
            if handle.movesBottom {
                result.size.height = max(floor.height, rect.height + delta.height)
            }
        }
        // Back into the direction the line ran.
        // `CGRect.width` is always positive; the stored frame says which way
        // the line runs.
        var frame = PageObject.Frame(result)
        if object.frame.width < 0 {
            frame.x = Double(result.maxX)
            frame.width = -Double(result.width)
        }
        if object.frame.height < 0 {
            frame.y = Double(result.maxY)
            frame.height = -Double(result.height)
        }
        next.frame = frame
        return next
    }

    /// An object scaled about its centre by `factor`, as the menu's Resize
    /// does.
    static func scaled(_ object: PageObject, by factor: CGFloat, minimum: CGSize) -> PageObject {
        var next = object
        let rect = object.frame.rect.standardized
        var factor = factor
        if rect.width > 0 { factor = max(factor, min(minimum.width, rect.width) / rect.width) }
        if rect.height > 0 { factor = max(factor, min(minimum.height, rect.height) / rect.height) }
        next.frame.width = object.frame.width * Double(factor)
        next.frame.height = object.frame.height * Double(factor)
        // A negative width keeps the line's start on the right, so this
        // lands on the far edge of the box for it.
        next.frame.x = Double(rect.midX) - next.frame.width / 2
        next.frame.y = Double(rect.midY) - next.frame.height / 2
        if object.type == .text {
            next.fontSize = min(max((object.fontSize ?? PageObject.defaultFontSize) * Double(factor), 4), 400)
        }
        return next
    }
}

/// One resize handle: a small square with a target big enough to catch.
/// It takes no touches; the lasso's recogniser carries a drag that starts on
/// it.
final class HandleView: UIView {
    let handle: ObjectView.Handle

    init(handle: ObjectView.Handle) {
        self.handle = handle
        super.init(frame: CGRect(x: 0, y: 0, width: 22, height: 22))
        backgroundColor = .clear
        isUserInteractionEnabled = false
        isAccessibilityElement = false
        accessibilityLabel = "\(handle.label) resize handle"
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("HandleView is created in code") }

    override func draw(_ rect: CGRect) {
        guard let context = UIGraphicsGetCurrentContext() else { return }
        let square = rect.insetBy(dx: 6, dy: 6)
        context.setFillColor(UIColor.white.cgColor)
        context.setStrokeColor(UIColor(PaperTokens.accent).cgColor)
        context.setLineWidth(1)
        context.fill(square)
        context.stroke(square)
    }
}
