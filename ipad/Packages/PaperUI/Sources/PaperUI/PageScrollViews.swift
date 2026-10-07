import UIKit

/// The vertical scroller the pages live in.
///
/// Its only special behaviour is the input policy: its pan takes fingers and
/// never the Pencil.
final class PageScrollView: UIScrollView {
    override init(frame: CGRect) {
        super.init(frame: frame)
        // Pencil is creation, fingers are navigation -- the research principle,
        // enforced here once for every tool rather than tool by tool. Without
        // this a Pencil drag drives the pan like any other touch, which is why
        // the lasso only ever scrolled the page on hardware.
        panGestureRecognizer.allowedTouchTypes = [NSNumber(value: UITouch.TouchType.direct.rawValue)]
        // No 150ms hesitation before a touch reaches the page. Cancellation is
        // deliberately left on: a scroll view that may not cancel its content's
        // touches is a scroll view that stops scrolling the moment a finger
        // lands on a page, and the lasso no longer needs it -- it is a gesture
        // recogniser now, and the pan waits for it.
        delaysContentTouches = false
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("PageScrollView is created in code") }
}

/// Lays the pages out in one column with 24pt gutters, at whatever the current
/// zoom makes each page.
final class PageStackView: UIView {
    static let gutter: CGFloat = 24

    private(set) var pageViews: [PageView] = []
    var zoom: CGFloat = 1

    /// Removes the pages that went and adds the ones that came. Every other
    /// view stays where it is: taking a live `PKCanvasView` out of the
    /// hierarchy, even to put it straight back, ends its stroke and its
    /// responder state.
    func setPageViews(_ views: [PageView]) {
        let kept = Set(views.map(ObjectIdentifier.init))
        for view in pageViews where !kept.contains(ObjectIdentifier(view)) && view.superview === self {
            view.removeFromSuperview()
        }
        pageViews = views
        for view in views where view.superview !== self { addSubview(view) }
        setNeedsLayout()
    }

    /// Page origins in stack coordinates, which is what the scroll offset and
    /// the live window are computed from.
    func frame(ofPageAt index: Int) -> CGRect {
        guard pageViews.indices.contains(index) else { return .zero }
        return pageViews[index].frame
    }

    var contentHeight: CGFloat {
        guard let last = pageViews.last else { return 0 }
        return last.frame.maxY + Self.gutter
    }

    /// How wide the column has to be. An infinite page grows sideways as well
    /// as downwards, so the stack is no longer always the width of the screen.
    var contentWidth: CGFloat {
        let widest = pageViews.map { $0.pageSize.width * zoom }.max() ?? 0
        return widest + Self.gutter * 2
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        var y = Self.gutter
        for view in pageViews {
            let size = CGSize(
                width: view.pageSize.width * zoom, height: view.pageSize.height * zoom)
            view.frame = CGRect(
                x: (bounds.width - size.width) / 2, y: y, width: size.width, height: size.height)
            y += size.height + Self.gutter
        }
    }
}
