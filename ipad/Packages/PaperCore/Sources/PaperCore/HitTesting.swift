import CoreGraphics
import Foundation

/// What a one-finger tap selects.
public enum HitTarget: Equatable, Sendable {
    /// Strokes, as ascending indices into the `strokes` given: the stroke
    /// under the tap and the rest of its word.
    case ink([Int])
    case text(id: String)
    case image(id: String)
    case shape(id: String)
}

/// Decides from geometry alone what a finger tap on a page selects.
///
/// 1. Objects come first, top-most (last in the array) first, tested by
///    frame. A frame thinner than the touch on one axis is widened to it, and
///    a line or arrow shape is tested along its diagonal instead.
/// 2. Otherwise a stroke is hit when its path passes within
///    `max(touchRadius / zoom, width / 2)` of the tap; of several, the
///    nearest path wins, and the top-most on a tie.
/// 3. The hit stroke brings in every stroke whose bounds touch its bounds
///    grown by `wordGap`, and so on from those, so a whole word is selected.
///    Two strokes chain only when they share at least `wordOverlap` of the
///    shorter one's height, so a descender that touches the line below does
///    not pull it in. A mark no larger than `wordMark` (a dot, an accent)
///    joins whatever it touches but chains no further.
public enum HitTesting {
    /// How far from a path a finger still hits it, in screen points.
    public static let touchRadius: CGFloat = 12
    /// How far apart the bounds of two strokes of one word may be, in page
    /// points.
    public static let wordGap: CGFloat = 4
    /// The share of the shorter stroke's height two strokes of one line
    /// have in common.
    public static let wordOverlap: CGFloat = 0.5
    /// The largest a dot or an accent is on either side, in page points.
    public static let wordMark: CGFloat = 8

    /// A stroke's geometry in page space.
    public struct Stroke: Equatable, Sendable {
        /// Points along the stroke's centre line, transform applied.
        public var points: [CGPoint]
        /// The widest the stroke is drawn.
        public var width: CGFloat
        /// The rectangle the stroke covers; by default its points' bounds
        /// grown by half the width. Pass `PKStroke.renderBounds` when known.
        public var bounds: CGRect

        public init(points: [CGPoint], width: CGFloat, bounds: CGRect? = nil) {
            self.points = points
            self.width = width.isFinite ? max(width, 0) : 0
            self.bounds = bounds ?? Self.bounds(of: points, width: self.width)
        }

        static func bounds(of points: [CGPoint], width: CGFloat) -> CGRect {
            let finite = points.filter { $0.x.isFinite && $0.y.isFinite }
            guard let first = finite.first else { return .null }
            var minX = first.x, minY = first.y, maxX = first.x, maxY = first.y
            for p in finite {
                minX = min(minX, p.x)
                minY = min(minY, p.y)
                maxX = max(maxX, p.x)
                maxY = max(maxY, p.y)
            }
            return CGRect(x: minX, y: minY, width: maxX - minX, height: maxY - minY)
                .insetBy(dx: -width / 2, dy: -width / 2)
        }
    }

    /// An object's geometry in page space.
    public struct Object: Equatable, Sendable {
        public enum Kind: String, Sendable { case text, image, shape }

        public var id: String
        public var kind: Kind
        /// The object's frame. A shape's width or height may be negative.
        public var frame: CGRect
        /// A line or arrow shape: hit along the frame's diagonal, from its
        /// origin to the opposite corner, rather than inside it.
        public var isLine: Bool
        /// A line's drawn width.
        public var strokeWidth: CGFloat

        public init(id: String, kind: Kind, frame: CGRect, isLine: Bool = false, strokeWidth: CGFloat = 0) {
            self.id = id
            self.kind = kind
            self.frame = frame
            self.isLine = isLine
            self.strokeWidth = strokeWidth
        }
    }

    /// What a tap at `point` (page space) selects, or `nil` for empty paper.
    /// `objects` and `strokes` are in drawing order, bottom first.
    public static func target(
        at point: CGPoint, strokes: [Stroke], objects: [Object], zoom: CGFloat
    ) -> HitTarget? {
        guard point.x.isFinite, point.y.isFinite else { return nil }
        let reach = touchRadius / (zoom.isFinite && zoom > 0 ? zoom : 1)
        for object in objects.reversed() where hits(object, point, reach: reach) {
            switch object.kind {
            case .text: return .text(id: object.id)
            case .image: return .image(id: object.id)
            case .shape: return .shape(id: object.id)
            }
        }
        guard let hit = stroke(at: point, in: strokes, reach: reach) else { return nil }
        return .ink(word(around: hit, in: strokes))
    }

    static func hits(_ object: Object, _ point: CGPoint, reach: CGFloat) -> Bool {
        let frame = object.frame
        guard frame.origin.x.isFinite, frame.origin.y.isFinite, frame.width.isFinite,
            frame.height.isFinite
        else { return false }
        if object.isLine {
            // `origin` and `size` as stored: a negative size flips the line.
            let end = CGPoint(x: frame.origin.x + frame.size.width, y: frame.origin.y + frame.size.height)
            return distance(point, frame.origin, end) <= max(reach, object.strokeWidth / 2)
        }
        var box = frame.standardized
        if box.width < 2 * reach { box = box.insetBy(dx: (box.width - 2 * reach) / 2, dy: 0) }
        if box.height < 2 * reach { box = box.insetBy(dx: 0, dy: (box.height - 2 * reach) / 2) }
        return box.contains(point)
    }

    /// The index of the stroke whose path passes nearest the tap, within its
    /// reach; the top-most on a tie.
    static func stroke(at point: CGPoint, in strokes: [Stroke], reach: CGFloat) -> Int? {
        var best: (index: Int, distance: CGFloat)?
        for index in strokes.indices.reversed() {
            let stroke = strokes[index]
            let tolerance = max(reach, stroke.width / 2)
            guard !stroke.bounds.isNull,
                stroke.bounds.insetBy(dx: -tolerance, dy: -tolerance).contains(point)
            else { continue }
            let d = pathDistance(point, stroke.points)
            if d <= tolerance, d < (best?.distance ?? .infinity) { best = (index, d) }
        }
        return best?.index
    }

    /// The stroke at `index` and every stroke chained to it by bounds within
    /// `wordGap`, kept to one line.
    public static func word(around index: Int, in strokes: [Stroke]) -> [Int] {
        guard strokes.indices.contains(index) else { return [] }
        guard !strokes[index].bounds.isNull else { return [index] }
        var chosen: Set<Int> = [index]
        var frontier = [index]
        while let current = frontier.popLast() {
            let bounds = strokes[current].bounds
            let reach = bounds.insetBy(dx: -wordGap, dy: -wordGap)
            // A tapped mark reaches the letter it belongs to by touch alone.
            let fromMark = current == index && isMark(bounds)
            for candidate in strokes.indices where !chosen.contains(candidate) {
                let other = strokes[candidate].bounds
                guard !other.isNull, other.intersects(reach) else { continue }
                let mark = isMark(other)
                guard mark || fromMark || shareLine(bounds, other) else { continue }
                chosen.insert(candidate)
                if !mark { frontier.append(candidate) }
            }
        }
        return chosen.sorted()
    }

    static func isMark(_ bounds: CGRect) -> Bool { max(bounds.width, bounds.height) <= wordMark }

    static func shareLine(_ a: CGRect, _ b: CGRect) -> Bool {
        let overlap = min(a.maxY, b.maxY) - max(a.minY, b.minY)
        return overlap >= 0 && overlap >= wordOverlap * min(a.height, b.height)
    }

    static func pathDistance(_ point: CGPoint, _ path: [CGPoint]) -> CGFloat {
        guard let first = path.first else { return .infinity }
        guard path.count > 1 else { return hypot(point.x - first.x, point.y - first.y) }
        var nearest = CGFloat.infinity
        for index in path.indices.dropFirst() {
            nearest = min(nearest, distance(point, path[index - 1], path[index]))
        }
        return nearest
    }

    static func distance(_ p: CGPoint, _ a: CGPoint, _ b: CGPoint) -> CGFloat {
        let dx = b.x - a.x, dy = b.y - a.y
        let squared = dx * dx + dy * dy
        guard squared > 0 else { return hypot(p.x - a.x, p.y - a.y) }
        let t = min(max(((p.x - a.x) * dx + (p.y - a.y) * dy) / squared, 0), 1)
        return hypot(p.x - (a.x + dx * t), p.y - (a.y + dy * t))
    }
}
