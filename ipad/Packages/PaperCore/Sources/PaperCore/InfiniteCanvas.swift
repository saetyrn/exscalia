import CoreGraphics
import Foundation

/// The geometry of a page that has no bottom and no right edge.
///
/// Pure arithmetic, deliberately: growing a page is the one part of the
/// infinite canvas that must behave identically on screen, in the renderer and
/// in the exporter, so it lives in `PaperCore` where `swift test` can reach it
/// and no view is involved.
///
/// A page starts at `baseExtent` and is extended when the ink or the objects
/// come within `margin` of an edge. Growing at the bottom or the right only
/// adds room. Growing at the top or the left also *translates* everything that
/// is already there, so that the writing does not appear to jump: the caller
/// applies `translation` to the drawing, to the object frames and to its own
/// scroll offset in the same turn it applies `extent`.
public enum InfiniteCanvas {
    /// Where an infinite page starts: the iPad page, so a fresh notebook looks
    /// like a notebook rather than like a wall.
    public static let baseExtent = CGSize(width: 1024, height: 1366)

    /// How close the work may come to an edge before the page is extended.
    public static let margin: CGFloat = 160

    /// The smallest useful extension, for the case where no viewport is known.
    public static let minimumStep: CGFloat = 256

    /// A page's new extent and the offset everything on it must move by.
    public struct Growth: Equatable, Sendable {
        public var extent: CGSize
        public var translation: CGSize

        public init(extent: CGSize, translation: CGSize) {
            self.extent = extent
            self.translation = translation
        }

        /// True when nothing on the page has to move -- the page only grew
        /// down or to the right.
        public var isPureExtension: Bool { translation == .zero }
    }

    /// How far the page grows in one step: half a screen, floored so that a
    /// tiny or unknown viewport still yields a useful amount of new paper.
    public static func step(forViewport viewport: CGSize) -> CGSize {
        CGSize(
            width: max(viewport.width / 2, minimumStep),
            height: max(viewport.height / 2, minimumStep))
    }

    /// The extent an infinite page needs, given what is drawn on it.
    ///
    /// - Parameters:
    ///   - extent: the page's current extent in page points.
    ///   - content: the union of the ink bounds and the object frames, in page
    ///     points. Pass `nil` (or a null rectangle) for an empty page.
    ///   - viewport: the visible area in page points, which sets the step.
    ///   - quantum: what a top or left growth is rounded up to a multiple of,
    ///     per axis -- the template's spacing (`TemplateGeometry
    ///     .growthQuantum(for:)`), so the moved ink stays on the rule, grid
    ///     line or dot row it was written on. Zero leaves that axis unsnapped.
    /// - Returns: `nil` when the page already has room, otherwise the new
    ///   extent and the offset the content must be moved by.
    public static func growth(
        extent: CGSize,
        content: CGRect?,
        viewport: CGSize,
        quantum: CGSize = .zero
    ) -> Growth? {
        guard let content, !content.isNull, !content.isInfinite,
            content.width.isFinite, content.height.isFinite,
            content.origin.x.isFinite, content.origin.y.isFinite
        else { return nil }

        let step = step(forViewport: viewport)
        var width = max(extent.width, baseExtent.width)
        var height = max(extent.height, baseExtent.height)
        var dx: CGFloat = 0
        var dy: CGFloat = 0

        if content.maxX > width - margin {
            // Enough steps in one go that a long paste does not need several
            // rounds of growth to be contained.
            width += stepsNeeded(by: content.maxX - (width - margin), step: step.width) * step.width
        }
        if content.maxY > height - margin {
            height += stepsNeeded(by: content.maxY - (height - margin), step: step.height)
                * step.height
        }
        // Growing at the top or the left moves the ink while the template stays
        // put, so the move is a whole number of template spacings.
        if content.minX < margin {
            let grow = snapped(
                stepsNeeded(by: margin - content.minX, step: step.width) * step.width,
                to: quantum.width)
            width += grow
            dx = grow
        }
        if content.minY < margin {
            let grow = snapped(
                stepsNeeded(by: margin - content.minY, step: step.height) * step.height,
                to: quantum.height)
            height += grow
            dy = grow
        }

        guard width != extent.width || height != extent.height || dx != 0 || dy != 0 else {
            return nil
        }
        return Growth(
            extent: CGSize(width: width, height: height),
            translation: CGSize(width: dx, height: dy))
    }

    private static func stepsNeeded(by deficit: CGFloat, step: CGFloat) -> CGFloat {
        guard step > 0 else { return 1 }
        return max(1, (deficit / step).rounded(.up))
    }

    private static func snapped(_ amount: CGFloat, to quantum: CGFloat) -> CGFloat {
        guard quantum > 0, quantum.isFinite else { return amount }
        return (amount / quantum).rounded(.up) * quantum
    }

    /// The union of an ink rectangle and a set of object frames -- the thing
    /// `growth(extent:content:viewport:)` measures. Returns `nil` when the page
    /// is empty.
    public static func contentBounds(ink: CGRect?, objects: [PageObject]) -> CGRect? {
        var union = CGRect.null
        if let ink, !ink.isNull, !ink.isEmpty { union = union.union(ink) }
        for object in objects {
            union = union.union(object.frame.rect)
        }
        return union.isNull ? nil : union
    }
}
