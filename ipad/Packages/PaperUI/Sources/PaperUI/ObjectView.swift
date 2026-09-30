import PaperCore
import UIKit

@MainActor
protocol ObjectViewDelegate: AnyObject {
    func objectViewDidSelect(_ view: ObjectView)
    /// The object finished changing: persist it and put the inverse on the
    /// undo stack.
    func objectView(_ view: ObjectView, didCommit object: PageObject, actionName: String)
    func objectViewDidChangeCropMode(_ view: ObjectView)
    func objectViewNeedsLayout(_ view: ObjectView)
}

/// What text and image objects have in common: a frame in page points, a
/// selection outline, eight resize handles and a drag.
///
/// The frame is always page points at zoom 1, so a page written at 2x and read
/// at 0.5x has objects in the same place as the ink around them; the view frame
/// is the page frame scaled, inflated by the handle margin so the handles sit
/// inside the view rather than escaping it.
class ObjectView: UIView {
    /// Room around the content for the handles to sit in.
    static let handleInset: CGFloat = 10

    static func viewFrame(for rect: CGRect, zoom: CGFloat) -> CGRect {
        rect.applying(CGAffineTransform(scaleX: zoom, y: zoom))
            .insetBy(dx: -handleInset, dy: -handleInset)
    }

    enum Handle: CaseIterable {
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

        var movesLeft: Bool { unit.x == 0 }
        var movesRight: Bool { unit.x == 1 }
        var movesTop: Bool { unit.y == 0 }
        var movesBottom: Bool { unit.y == 1 }
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

    var isSelected = false {
        didSet {
            guard isSelected != oldValue else { return }
            outline.isHidden = !isSelected
            for handle in handleViews.values { handle.isHidden = !isSelected }
            if !isSelected { endEditingContent() }
        }
    }

    /// Where subclasses put their content. Inset by the handle margin.
    let content = UIView()
    private let outline = CAShapeLayer()
    private var handleViews: [Handle: HandleView] = [:]
    /// The object as it was when the current gesture started, so one drag is
    /// one undo step.
    private var gestureStart: PageObject?
    private var minimumSize = CGSize(width: 32, height: 24)

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
        addSubview(content)

        for handle in Handle.allCases {
            let view = HandleView(handle: handle)
            view.isHidden = true
            view.addGestureRecognizer(
                UIPanGestureRecognizer(target: self, action: #selector(handleResize)))
            addSubview(view)
            handleViews[handle] = view
        }

        let tap = UITapGestureRecognizer(target: self, action: #selector(handleTap))
        addGestureRecognizer(tap)
        let pan = UIPanGestureRecognizer(target: self, action: #selector(handleMove))
        addGestureRecognizer(pan)

        isAccessibilityElement = false
        accessibilityContainerType = .semanticGroup
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("ObjectView is created in code") }

    // MARK: - Contents

    func apply(_ object: PageObject) {
        self.object = object
        contentDidChange()
        setNeedsLayout()
    }

    /// Subclass hook: the object's payload changed.
    func contentDidChange() {}
    /// Subclass hook: the zoom changed, so text has to be re-sized.
    func zoomChanged() {}
    /// Subclass hook: a drag on a handle while the subclass is in a special
    /// mode. Return `true` to say the resize was handled.
    func handleSpecialResize(_ handle: Handle, translation: CGSize) -> Bool { false }
    func beginEditingContent() {}
    func endEditingContent() {}

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

    /// Handles sit outside the content rect, so hit testing has to look past
    /// the bounds a little.
    override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {
        if super.point(inside: point, with: event) { return true }
        guard isSelected else { return false }
        return bounds.insetBy(dx: -8, dy: -8).contains(point)
    }

    // MARK: - Gestures

    @objc private func handleTap(_ gesture: UITapGestureRecognizer) {
        if isSelected {
            beginEditingContent()
        } else {
            delegate?.objectViewDidSelect(self)
        }
    }

    @objc private func handleMove(_ gesture: UIPanGestureRecognizer) {
        guard let container = superview else { return }
        switch gesture.state {
        case .began:
            delegate?.objectViewDidSelect(self)
            gestureStart = object
        case .changed, .ended, .cancelled:
            guard let start = gestureStart else { return }
            let translation = gesture.translation(in: container)
            var next = start
            next.frame.x = start.frame.x + Double(translation.x / zoom)
            next.frame.y = start.frame.y + Double(translation.y / zoom)
            object = next
            frame = Self.viewFrame(for: next.frame.rect, zoom: zoom)
            if gesture.state != .changed {
                gestureStart = nil
                delegate?.objectView(self, didCommit: next, actionName: "Move Object")
            }
        default:
            break
        }
    }

    @objc private func handleResize(_ gesture: UIPanGestureRecognizer) {
        guard let handleView = gesture.view as? HandleView, let container = superview else { return }
        let handle = handleView.handle
        switch gesture.state {
        case .began:
            gestureStart = object
        case .changed, .ended, .cancelled:
            guard let start = gestureStart else { return }
            let translation = gesture.translation(in: container)
            let size = CGSize(width: translation.x / zoom, height: translation.y / zoom)
            if handleSpecialResize(handle, translation: size) {
                if gesture.state != .changed {
                    gestureStart = nil
                    delegate?.objectView(self, didCommit: object, actionName: "Crop Image")
                }
                return
            }
            var next = start
            var rect = start.frame.rect
            if handle.movesLeft {
                let delta = min(size.width, rect.width - minimumSize.width)
                rect.origin.x += delta
                rect.size.width -= delta
            }
            if handle.movesRight {
                rect.size.width = max(minimumSize.width, rect.width + size.width)
            }
            if handle.movesTop {
                let delta = min(size.height, rect.height - minimumSize.height)
                rect.origin.y += delta
                rect.size.height -= delta
            }
            if handle.movesBottom {
                rect.size.height = max(minimumSize.height, rect.height + size.height)
            }
            next.frame = PageObject.Frame(rect)
            object = next
            frame = Self.viewFrame(for: rect, zoom: zoom)
            contentDidChange()
            setNeedsLayout()
            if gesture.state != .changed {
                gestureStart = nil
                delegate?.objectView(self, didCommit: next, actionName: "Resize Object")
            }
        default:
            break
        }
    }
}

/// One resize handle: a small accent square with a target big enough to catch.
final class HandleView: UIView {
    let handle: ObjectView.Handle

    init(handle: ObjectView.Handle) {
        self.handle = handle
        super.init(frame: CGRect(x: 0, y: 0, width: 22, height: 22))
        backgroundColor = .clear
        isAccessibilityElement = true
        accessibilityLabel = "\(handle.label) resize handle"
        accessibilityHint = "Drag to resize. The inspector has a resize menu too."
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("HandleView is created in code") }

    override func draw(_ rect: CGRect) {
        guard let context = UIGraphicsGetCurrentContext() else { return }
        let square = rect.insetBy(dx: 6, dy: 6)
        context.setFillColor(UIColor(PaperTokens.paper).cgColor)
        context.setStrokeColor(UIColor(PaperTokens.accent).cgColor)
        context.setLineWidth(1)
        context.fill(square)
        context.stroke(square)
    }
}
