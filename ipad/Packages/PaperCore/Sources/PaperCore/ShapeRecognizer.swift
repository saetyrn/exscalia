import CoreGraphics
import Foundation

/// A clean shape found in a rough Pencil stroke.
public struct RecognizedShape: Equatable, Sendable {
    public enum Kind: String, CaseIterable, Sendable {
        case line, arrow, triangle, rectangle, ellipse, polygon
    }

    public var kind: Kind
    /// Line and arrow: the start and the end (an arrow ends at its tip).
    /// Triangle, rectangle and polygon: the corners in drawing order, the
    /// first not repeated. Empty for an ellipse.
    public var vertices: [CGPoint]
    /// Arrow only: the ends of the head's two barbs.
    public var arrowHead: [CGPoint]
    /// Ellipse only: the centre, the semi-axes and the rotation in radians of
    /// the first axis (`radii.width`), within ±45°. Zero for other kinds.
    public var center: CGPoint
    public var radii: CGSize
    public var rotation: CGFloat

    init(
        kind: Kind, vertices: [CGPoint] = [], arrowHead: [CGPoint] = [], center: CGPoint = .zero,
        radii: CGSize = .zero, rotation: CGFloat = 0
    ) {
        self.kind = kind
        self.vertices = vertices
        self.arrowHead = arrowHead
        self.center = center
        self.radii = radii
        self.rotation = rotation
    }

    /// An ellipse whose axes were close enough to be made equal.
    public var isCircle: Bool { kind == .ellipse && radii.width == radii.height }

    public var isClosed: Bool { kind != .line && kind != .arrow }

    /// The clean strokes as polylines: one for a line, one per closed outline
    /// (ending on its first point), and two for an arrow, the shaft and then
    /// the head (barb, tip, barb).
    public func outlines(ellipseSegments: Int = 72) -> [[CGPoint]] {
        switch kind {
        case .line:
            return [vertices]
        case .arrow:
            guard vertices.count == 2, arrowHead.count == 2 else { return [vertices] }
            return [vertices, [arrowHead[0], vertices[1], arrowHead[1]]]
        case .triangle, .rectangle, .polygon:
            guard let first = vertices.first else { return [] }
            return [vertices + [first]]
        case .ellipse:
            return [ellipsePoints(count: max(8, ellipseSegments))]
        }
    }

    /// The same strokes as control points for `PKStrokePath`, which draws a
    /// uniform cubic B-spline through them. Every corner inside a path appears
    /// three times, which makes the spline pass through it with straight edges
    /// on either side; an ellipse is sampled about every `ellipseSpacing`
    /// points, never fewer than 48 times.
    public func splinePaths(ellipseSpacing: CGFloat = 4) -> [[CGPoint]] {
        if kind == .ellipse {
            let a = Double(radii.width), b = Double(radii.height)
            // Ramanujan's approximation of the perimeter.
            let perimeter = Double.pi * (3 * (a + b) - ((3 * a + b) * (a + 3 * b)).squareRoot())
            let spacing = max(Double(ellipseSpacing), 0.5)
            let count = min(max(48, Int((perimeter / spacing).rounded(.up))), 4096)
            return [ellipsePoints(count: count)]
        }
        return outlines().map { path in
            guard path.count > 2 else { return path }
            var points = [path[0]]
            for corner in path.dropFirst().dropLast() {
                points.append(contentsOf: [corner, corner, corner])
            }
            points.append(path[path.count - 1])
            return points
        }
    }

    private func ellipsePoints(count: Int) -> [CGPoint] {
        let cosR = cos(Double(rotation)), sinR = sin(Double(rotation))
        let a = Double(radii.width), b = Double(radii.height)
        var points: [CGPoint] = []
        points.reserveCapacity(count + 1)
        for step in 0...count {
            let t = Double(step % count) / Double(count) * 2 * .pi
            let x = a * cos(t), y = b * sin(t)
            points.append(
                CGPoint(
                    x: Double(center.x) + x * cosR - y * sinR,
                    y: Double(center.y) + x * sinR + y * cosR))
        }
        return points
    }
}

/// Turns the last Draw Shape stroke into a clean line, arrow, triangle,
/// rectangle (at any rotation), ellipse (a circle when its axes nearly agree)
/// or polygon, or declines and leaves the stroke as drawn.
///
/// Every threshold is a named constant below. Lengths are relative to the
/// stroke's own size except `minimumSize`, which is in screen points.
public enum ShapeRecognizer {
    // MARK: Thresholds

    /// The smallest stroke considered: the longer side of its bounding box, in
    /// screen points (page points times zoom). Anything smaller is
    /// handwriting, a letter O included.
    public static let minimumSize: Double = 16

    /// A stroke is closed when its end comes back to within this fraction of
    /// its size (the bounding box's longer side) of its start.
    public static let closureGap: Double = 0.2
    /// Where the closing point is looked for: the last part of the stroke, as
    /// a fraction of its length (and, for a stroke whose end lands on its
    /// beginning, the first part). An overshoot past the start is cut off.
    public static let closureSearch: Double = 0.35

    /// A line, or an arrow's shaft, strays from straight by at most this
    /// fraction of its length...
    public static let lineDeviation: Double = 0.06
    /// ...and is at most this much longer than its chord, which rejects a
    /// wavy line and a line traced twice.
    public static let lineDetour: Double = 1.15
    /// The fraction of a line's length at each end that the straightness test
    /// ignores, so a hook where the Pencil lands or lifts does not spoil it.
    public static let lineEndTrim: Double = 0.05

    /// Each barb of an arrow is at least this fraction of the shaft...
    public static let arrowMinimumBarb: Double = 0.06
    /// ...the head stays within this fraction of the shaft from the tip...
    public static let arrowMaximumHead: Double = 0.6
    /// ...the shorter barb is at least this fraction of the longer...
    public static let arrowBarbBalance: Double = 0.35
    /// ...each barb leaves the tip at this many degrees from the shaft...
    public static let arrowBarbAngles: ClosedRange<Double> = 10...80
    /// ...and the head's path is at most this many times its longer barb,
    /// which rejects a scribble at the tip.
    public static let arrowHeadPath: Double = 5
    /// The tip is the first peak of distance from the start within this
    /// fraction of the farthest point, so a return to the tip between the
    /// barbs that overshoots it does not move the tip.
    public static let arrowTipTolerance: Double = 0.05

    /// An ellipse: the mean distance of the stroke from the fitted ellipse, as
    /// a fraction of its mean radius...
    public static let ellipseError: Double = 0.07
    /// ...the largest single distance...
    public static let ellipseMaximumError: Double = 0.3
    /// ...and the minor axis at least this fraction of the major.
    public static let ellipseMinimumAspect: Double = 0.12
    /// Axes within this fraction of each other make a circle.
    public static let circleTolerance: Double = 0.08

    /// Corner finding simplifies the outline to within this fraction of its
    /// mean radius.
    public static let polygonSimplify: Double = 0.1
    /// A corner turns the outline by at least this many degrees.
    public static let cornerMinimumTurn: Double = 25
    /// An edge shorter than this fraction of the median edge is a rounded
    /// corner, and its two ends become one corner.
    public static let polygonShortEdge: Double = 0.35
    /// A polygon: the mean distance of the stroke from the fitted outline, as
    /// a fraction of its mean radius.
    public static let polygonError: Double = 0.06
    /// Where an ellipse also fits, a polygon wins only when its error is below
    /// this fraction of the ellipse's. A circle is not an octagon.
    public static let polygonPreference: Double = 0.8
    /// The most corners a polygon has.
    public static let polygonMaximumCorners = 8
    /// A rectangle's corners are all within this many degrees of square.
    public static let rectangleAngleTolerance: Double = 20
    /// A triangle has no corner sharper than this many degrees.
    public static let triangleMinimumAngle: Double = 15

    /// Ellipses, triangles and rectangles are convex: the stroke encloses at
    /// least this fraction of its convex hull.
    public static let convexity: Double = 0.85
    /// A closed stroke turns round exactly once; its total absolute turning is
    /// at most this many full turns, which rejects a closed scribble.
    public static let maximumTurning: Double = 1.6
    /// A closed shape's convex hull covers at least this fraction of the
    /// square on its longer side, which rejects a line drawn there and back.
    public static let closedMinimumFill: Double = 0.05

    /// Lines, arrows, rectangles and ellipses within this many degrees of
    /// horizontal or vertical are straightened.
    public static let axisSnap: Double = 6

    /// Points a closed stroke is resampled to before fitting.
    static let closedSamples = 96
    /// Points it is resampled to before its turning is measured: coarse
    /// enough that a tremor along the line does not count as turning.
    static let turningSamples = 32

    // MARK: Recognition

    /// The shape `points` (one stroke, page space) was meant to be, or `nil`.
    /// `zoom` is the canvas zoom, which turns `minimumSize` into page points.
    public static func recognize(points: [CGPoint], zoom: CGFloat = 1) -> RecognizedShape? {
        let scale = zoom.isFinite && zoom > 0 ? Double(zoom) : 1
        let path = clean(points)
        guard path.count >= 2 else { return nil }
        let size = extent(path)
        guard size * scale >= minimumSize else { return nil }
        if let loop = closedLoop(path, size: size) {
            return closedShape(loop)
        }
        return arrow(path, scale: scale) ?? arrow(path.reversed(), scale: scale)
            ?? line(path, scale: scale)
    }

    /// Finite points with consecutive duplicates removed.
    static func clean(_ points: [CGPoint]) -> [Vec] {
        var result: [Vec] = []
        result.reserveCapacity(points.count)
        for point in points {
            let v = Vec(Double(point.x), Double(point.y))
            guard v.x.isFinite, v.y.isFinite else { continue }
            if let last = result.last, (v - last).length < 1e-6 { continue }
            result.append(v)
        }
        return result
    }

    /// The longer side of the bounding box.
    static func extent(_ path: [Vec]) -> Double {
        guard let first = path.first else { return 0 }
        var low = first, high = first
        for p in path {
            low = Vec(min(low.x, p.x), min(low.y, p.y))
            high = Vec(max(high.x, p.x), max(high.y, p.y))
        }
        return max(high.x - low.x, high.y - low.y)
    }

    /// The stroke as one closed loop, with any overshoot past its start cut
    /// off, or `nil` when it does not come back to where it began.
    static func closedLoop(_ path: [Vec], size: Double) -> [Vec]? {
        let lengths = cumulative(path)
        guard let total = lengths.last, total > 0, path.count >= 3 else { return nil }
        let gap = closureGap * size
        var best: (distance: Double, loop: ArraySlice<Vec>)?
        // The end comes back to the start, perhaps past it.
        for index in path.indices where lengths[index] >= (1 - closureSearch) * total {
            let distance = (path[index] - path[0]).length
            if distance <= gap, distance < (best?.distance ?? .infinity) {
                best = (distance, path[...index])
            }
        }
        // The stroke began on a part it later drew over: cut the lead-in.
        for index in path.indices where lengths[index] <= closureSearch * total {
            let distance = (path[index] - path[path.count - 1]).length
            if distance <= gap, distance < (best?.distance ?? .infinity) {
                best = (distance, path[index...])
            }
        }
        guard let loop = best?.loop, loop.count >= 3 else { return nil }
        return Array(loop)
    }

    // MARK: Closed shapes

    static func closedShape(_ loop: [Vec]) -> RecognizedShape? {
        let samples = resample(loop, count: closedSamples, closed: true)
        guard samples.count == closedSamples else { return nil }

        // Once round, with no loops or scribbles on the way.
        let coarse = resample(loop, count: turningSamples, closed: true)
        let m = coarse.count
        var net = 0.0, total = 0.0
        for index in 0..<m {
            let incoming = coarse[index] - coarse[(index + m - 1) % m]
            let outgoing = coarse[(index + 1) % m] - coarse[index]
            let turn = atan2(incoming.cross(outgoing), incoming.dot(outgoing))
            net += turn
            total += abs(turn)
        }
        guard abs(abs(net) - 2 * .pi) < .pi, total <= maximumTurning * 2 * .pi else { return nil }

        let area = abs(signedArea(samples))
        let hullArea = abs(signedArea(convexHull(samples)))
        let size = extent(samples)
        guard area > 0, hullArea >= closedMinimumFill * size * size else { return nil }
        let isConvex = area >= convexity * hullArea

        let ellipse = fitEllipse(samples)
        let scale = ellipse?.meanRadius ?? (hullArea / .pi).squareRoot()
        let polygon = fitPolygon(samples, scale: scale)

        let ellipseFits: Bool = {
            guard let ellipse, isConvex else { return false }
            return ellipse.meanError <= ellipseError && ellipse.maximumError <= ellipseMaximumError
                && ellipse.minor / ellipse.major >= ellipseMinimumAspect
        }()
        if let polygon, polygon.error <= polygonError,
            (3...polygonMaximumCorners).contains(polygon.corners.count),
            !ellipseFits || polygon.error < (ellipse?.meanError ?? .infinity) * polygonPreference,
            let shape = polygonShape(polygon.corners, isConvex: isConvex)
        {
            return shape
        }
        if ellipseFits, let ellipse {
            return ellipseShape(ellipse)
        }
        return nil
    }

    struct EllipseFit {
        var center: Vec
        var major: Double
        var minor: Double
        /// Direction of the major axis, radians.
        var angle: Double
        var meanRadius: Double
        var meanError: Double
        var maximumError: Double
    }

    /// Centre and axes from the points' covariance, then the two radii by
    /// least squares on `A u² + B v² = 1`. Errors are first-order (Sampson)
    /// distances to the ellipse, relative to the mean radius.
    static func fitEllipse(_ points: [Vec]) -> EllipseFit? {
        let count = Double(points.count)
        let center = points.reduce(Vec(0, 0), +) / count
        var sxx = 0.0, syy = 0.0, sxy = 0.0
        for p in points {
            let d = p - center
            sxx += d.x * d.x
            syy += d.y * d.y
            sxy += d.x * d.y
        }
        let angle = 0.5 * atan2(2 * sxy, sxx - syy)
        let axis = Vec(cos(angle), sin(angle))
        var s40 = 0.0, s22 = 0.0, s04 = 0.0, s20 = 0.0, s02 = 0.0
        var uv: [(Double, Double)] = []
        uv.reserveCapacity(points.count)
        for p in points {
            let d = p - center
            let u = d.dot(axis), v = axis.cross(d)
            let u2 = u * u, v2 = v * v
            s40 += u2 * u2
            s22 += u2 * v2
            s04 += v2 * v2
            s20 += u2
            s02 += v2
            uv.append((u, v))
        }
        let determinant = s40 * s04 - s22 * s22
        guard abs(determinant) > 1e-12 else { return nil }
        let a = (s20 * s04 - s02 * s22) / determinant
        let b = (s40 * s02 - s22 * s20) / determinant
        guard a > 0, b > 0 else { return nil }
        let radiusU = 1 / a.squareRoot(), radiusV = 1 / b.squareRoot()
        let meanRadius = (radiusU + radiusV) / 2
        var sum = 0.0, largest = 0.0
        for (u, v) in uv {
            let f = a * u * u + b * v * v - 1
            let gradient = 2 * (a * a * u * u + b * b * v * v).squareRoot()
            let distance = gradient > 1e-12 ? abs(f) / gradient : meanRadius
            sum += distance
            largest = max(largest, distance)
        }
        let isMajorU = radiusU >= radiusV
        return EllipseFit(
            center: center, major: max(radiusU, radiusV), minor: min(radiusU, radiusV),
            angle: isMajorU ? angle : angle + .pi / 2, meanRadius: meanRadius,
            meanError: sum / count / meanRadius, maximumError: largest / meanRadius)
    }

    struct PolygonFit {
        /// Corners in drawing order.
        var corners: [Vec]
        var error: Double
    }

    /// Corners by simplification of the outline, merged where an edge is
    /// nearly straight or a corner was drawn round, then sharpened by fitting
    /// a line to each edge's middle and intersecting neighbours.
    static func fitPolygon(_ samples: [Vec], scale: Double) -> PolygonFit? {
        let n = samples.count
        guard n >= 6, scale > 0 else { return nil }
        let centroid = samples.reduce(Vec(0, 0), +) / Double(n)
        let first = samples.indices.max { (samples[$0] - centroid).length < (samples[$1] - centroid).length } ?? 0
        let second = samples.indices.max {
            (samples[$0] - samples[first]).length < (samples[$1] - samples[first]).length
        } ?? 0
        guard first != second else { return nil }
        let tolerance = polygonSimplify * scale
        var corners = [first, second]
        corners += simplify(samples, from: first, to: second, tolerance: tolerance)
        corners += simplify(samples, from: second, to: first, tolerance: tolerance)
        corners.sort()

        let minimumTurn = cornerMinimumTurn * .pi / 180
        func turn(at position: Int) -> Double {
            let count = corners.count
            let previous = samples[corners[(position + count - 1) % count]]
            let here = samples[corners[position]]
            let next = samples[corners[(position + 1) % count]]
            let incoming = here - previous, outgoing = next - here
            return abs(atan2(incoming.cross(outgoing), incoming.dot(outgoing)))
        }
        while corners.count > 3 {
            let turns = corners.indices.map(turn(at:))
            if let flattest = turns.indices.min(by: { turns[$0] < turns[$1] }), turns[flattest] < minimumTurn {
                corners.remove(at: flattest)
                continue
            }
            let count = corners.count
            let edges = (0..<count).map { (samples[corners[($0 + 1) % count]] - samples[corners[$0]]).length }
            let median = edges.sorted()[count / 2]
            if let shortest = edges.indices.min(by: { edges[$0] < edges[$1] }),
                edges[shortest] < polygonShortEdge * median
            {
                let other = (shortest + 1) % count
                corners.remove(at: turns[shortest] < turns[other] ? shortest : other)
                continue
            }
            break
        }
        guard corners.count >= 3 else { return nil }

        // One line per edge, through the middle of the samples between corners.
        let count = corners.count
        var lines: [(point: Vec, direction: Vec)] = []
        for position in 0..<count {
            let start = corners[position], end = corners[(position + 1) % count]
            let span = (end - start + n) % n
            let trim = Int(Double(span) * 0.15)
            var points: [Vec] = []
            if span - 2 * trim >= 3 {
                for offset in (trim + 1)..<(span - trim) { points.append(samples[(start + offset) % n]) }
            }
            if points.count >= 2, let fitted = fitLine(points) {
                lines.append(fitted)
            } else {
                let a = samples[start], b = samples[end]
                lines.append((a, (b - a).normalized))
            }
        }
        var refined: [Vec] = []
        for position in 0..<count {
            let original = samples[corners[position]]
            let incoming = lines[(position + count - 1) % count], outgoing = lines[position]
            if let crossing = intersection(incoming, outgoing),
                (crossing - original).length <= 0.5 * scale
            {
                refined.append(crossing)
            } else {
                refined.append(original)
            }
        }
        // Start at the corner nearest where the stroke began.
        let lead = refined.indices.min { (refined[$0] - samples[0]).length < (refined[$1] - samples[0]).length } ?? 0
        refined = Array(refined[lead...] + refined[..<lead])
        var sum = 0.0
        for p in samples {
            var nearest = Double.infinity
            for position in 0..<count {
                nearest = min(nearest, distance(p, refined[position], refined[(position + 1) % count]))
            }
            sum += nearest
        }
        return PolygonFit(corners: refined, error: sum / Double(n) / scale)
    }

    /// Interior indices kept by Ramer-Douglas-Peucker between two samples of a
    /// closed outline, walking forward from `from` to `to`.
    static func simplify(_ samples: [Vec], from: Int, to: Int, tolerance: Double) -> [Int] {
        let n = samples.count
        let span = (to - from + n) % n
        guard span > 1 else { return [] }
        let a = samples[from], b = samples[to]
        var farthest = -1, largest = 0.0
        for offset in 1..<span {
            let index = (from + offset) % n
            let d = distance(samples[index], a, b)
            if d > largest {
                largest = d
                farthest = index
            }
        }
        guard farthest >= 0, largest > tolerance else { return [] }
        return simplify(samples, from: from, to: farthest, tolerance: tolerance) + [farthest]
            + simplify(samples, from: farthest, to: to, tolerance: tolerance)
    }

    static func polygonShape(_ corners: [Vec], isConvex: Bool) -> RecognizedShape? {
        let count = corners.count
        let angles = (0..<count).map { position -> Double in
            let here = corners[position]
            let toPrevious = corners[(position + count - 1) % count] - here
            let toNext = corners[(position + 1) % count] - here
            return abs(atan2(toPrevious.cross(toNext), toPrevious.dot(toNext))) * 180 / .pi
        }
        switch count {
        case 3:
            guard isConvex, angles.allSatisfy({ $0 >= triangleMinimumAngle }) else { return nil }
            return RecognizedShape(kind: .triangle, vertices: corners.map(\.point))
        case 4 where isConvex && angles.allSatisfy({ abs($0 - 90) <= rectangleAngleTolerance }):
            return RecognizedShape(kind: .rectangle, vertices: rectangle(corners).map(\.point))
        default:
            return RecognizedShape(kind: .polygon, vertices: corners.map(\.point))
        }
    }

    /// The rectangle that best fits four corners: its angle is the length-
    /// weighted mean of the edges' directions (modulo a right angle), and each
    /// side lies midway between the two corners on it.
    static func rectangle(_ corners: [Vec]) -> [Vec] {
        var sine = 0.0, cosine = 0.0
        for position in 0..<4 {
            let edge = corners[(position + 1) % 4] - corners[position]
            let angle = atan2(edge.y, edge.x)
            sine += edge.length * sin(4 * angle)
            cosine += edge.length * cos(4 * angle)
        }
        let direction = snapped(Vec(cos(atan2(sine, cosine) / 4), sin(atan2(sine, cosine) / 4)))
        let normal = Vec(-direction.y, direction.x)
        let center = corners.reduce(Vec(0, 0), +) / 4
        let local = corners.map { Vec(($0 - center).dot(direction), ($0 - center).dot(normal)) }
        let xs = local.map(\.x).sorted(), ys = local.map(\.y).sorted()
        let left = (xs[0] + xs[1]) / 2, right = (xs[2] + xs[3]) / 2
        let top = (ys[0] + ys[1]) / 2, bottom = (ys[2] + ys[3]) / 2
        var box = [Vec(left, top), Vec(right, top), Vec(right, bottom), Vec(left, bottom)]
        // Same turning direction as drawn, starting at the corner drawn first.
        if signedArea(local) < 0 { box.reverse() }
        let start = box.indices.min { (box[$0] - local[0]).length < (box[$1] - local[0]).length } ?? 0
        box = Array(box[start...] + box[..<start])
        return box.map { center + direction * $0.x + normal * $0.y }
    }

    static func ellipseShape(_ fit: EllipseFit) -> RecognizedShape {
        if fit.minor >= fit.major * (1 - circleTolerance) {
            let radius = (fit.major + fit.minor) / 2
            return RecognizedShape(
                kind: .ellipse, center: fit.center.point, radii: CGSize(width: radius, height: radius))
        }
        // Keep the rotation within ±45°, swapping the radii to match.
        var angle = remainder(fit.angle, .pi)
        var radii = CGSize(width: fit.major, height: fit.minor)
        if abs(angle) > .pi / 4 {
            angle -= angle > 0 ? .pi / 2 : -.pi / 2
            radii = CGSize(width: fit.minor, height: fit.major)
        }
        if abs(angle) <= axisSnap * .pi / 180 { angle = 0 }
        return RecognizedShape(kind: .ellipse, center: fit.center.point, radii: radii, rotation: angle)
    }

    // MARK: Open shapes

    static func line(_ path: [Vec], scale: Double) -> RecognizedShape? {
        guard let first = path.first, let last = path.last else { return nil }
        let chord = (last - first).length
        guard chord * scale >= minimumSize, length(path) <= lineDetour * chord,
            let fitted = straightFit(path, chord: chord)
        else { return nil }
        let projections = path.map { ($0 - fitted.point).dot(fitted.direction) }
        guard let low = projections.min(), let high = projections.max() else { return nil }
        let middle = fitted.point + fitted.direction * ((low + high) / 2)
        let direction = snapped(fitted.direction)
        let half = (high - low) / 2
        return RecognizedShape(
            kind: .line, vertices: [(middle - direction * half).point, (middle + direction * half).point])
    }

    /// A one-stroke arrow drawn tail first: a straight shaft to the tip (the
    /// first peak of distance from the start, within `arrowTipTolerance` of
    /// the farthest point), then a head that reaches back on both sides of
    /// it. `recognize` also tries the stroke reversed, for an arrow drawn head
    /// first.
    static func arrow<Points: Collection>(_ points: Points, scale: Double) -> RecognizedShape?
    where Points.Element == Vec {
        let path = Array(points)
        guard path.count >= 4 else { return nil }
        let start = path[0]
        let distances = path.map { ($0 - start).length }
        guard let farthest = distances.max(), farthest > 0,
            var tipIndex = distances.firstIndex(where: { $0 >= (1 - arrowTipTolerance) * farthest })
        else { return nil }
        while tipIndex + 1 < path.count, distances[tipIndex + 1] >= distances[tipIndex] { tipIndex += 1 }
        guard tipIndex < path.count - 1 else { return nil }
        let tip = path[tipIndex]
        let shaftLength = (tip - start).length
        guard shaftLength * scale >= minimumSize else { return nil }
        let shaft = Array(path[...tipIndex])
        guard length(shaft) <= lineDetour * shaftLength,
            let fitted = straightFit(shaft, chord: shaftLength)
        else { return nil }

        let head = Array(path[tipIndex...])
        let direction = fitted.direction
        var left: (length: Double, angle: Double)?
        var right: (length: Double, angle: Double)?
        for point in head {
            let offset = point - tip
            guard offset.length <= arrowMaximumHead * shaftLength else { return nil }
            let along = offset.dot(direction), across = direction.cross(offset)
            guard along < 0, across != 0 else { continue }
            let barb = (length: offset.length, angle: atan2(abs(across), -along) * 180 / .pi)
            if across > 0 {
                if barb.length > (left?.length ?? 0) { left = barb }
            } else if barb.length > (right?.length ?? 0) {
                right = barb
            }
        }
        guard let left, let right else { return nil }
        let shortest = min(left.length, right.length), longest = max(left.length, right.length)
        guard shortest >= max(arrowMinimumBarb * shaftLength, 3 / scale),
            shortest >= arrowBarbBalance * longest,
            arrowBarbAngles.contains(left.angle), arrowBarbAngles.contains(right.angle),
            length(head) <= arrowHeadPath * longest
        else { return nil }

        // Project the ends onto the shaft's line, straighten it if it is
        // nearly level or upright, and make the head symmetric.
        let tail = fitted.point + direction * (start - fitted.point).dot(direction)
        let end = fitted.point + direction * (tip - fitted.point).dot(direction)
        let middle = (tail + end) / 2
        let axis = snapped(direction)
        let half = (end - tail).length / 2
        let cleanTail = middle - axis * half, cleanTip = middle + axis * half
        let barb = (left.length + right.length) / 2
        let angle = (left.angle + right.angle) / 2 * .pi / 180
        let back = axis * -1
        return RecognizedShape(
            kind: .arrow, vertices: [cleanTail.point, cleanTip.point],
            arrowHead: [
                (cleanTip + back.rotated(by: angle) * barb).point,
                (cleanTip + back.rotated(by: -angle) * barb).point,
            ])
    }

    /// The best straight line through a stroke whose middle (`lineEndTrim`
    /// off each end) strays from it by at most `lineDeviation` of `chord`,
    /// pointing from the stroke's start to its end.
    static func straightFit(_ path: [Vec], chord: Double) -> (point: Vec, direction: Vec)? {
        guard let first = path.first, let last = path.last, chord > 0 else { return nil }
        let lengths = cumulative(path)
        let total = lengths.last ?? 0
        var middle = path.indices
            .filter { lengths[$0] >= lineEndTrim * total && lengths[$0] <= (1 - lineEndTrim) * total }
            .map { path[$0] }
        if middle.count < 2 { middle = path }
        guard var fitted = fitLine(middle) else { return nil }
        if fitted.direction.dot(last - first) < 0 { fitted.direction = fitted.direction * -1 }
        let deviation = middle.map { abs(fitted.direction.cross($0 - fitted.point)) }.max() ?? 0
        guard deviation <= lineDeviation * chord else { return nil }
        return fitted
    }

    // MARK: Geometry

    typealias Vec = SIMD2<Double>

    /// Total least squares: through the centroid, along the principal axis.
    static func fitLine(_ points: [Vec]) -> (point: Vec, direction: Vec)? {
        guard points.count >= 2 else { return nil }
        let center = points.reduce(Vec(0, 0), +) / Double(points.count)
        var sxx = 0.0, syy = 0.0, sxy = 0.0
        for p in points {
            let d = p - center
            sxx += d.x * d.x
            syy += d.y * d.y
            sxy += d.x * d.y
        }
        guard sxx + syy > 1e-12 else { return nil }
        let angle = 0.5 * atan2(2 * sxy, sxx - syy)
        return (center, Vec(cos(angle), sin(angle)))
    }

    static func intersection(_ a: (point: Vec, direction: Vec), _ b: (point: Vec, direction: Vec)) -> Vec? {
        let denominator = a.direction.cross(b.direction)
        // Edges within 10° of parallel have no useful crossing.
        guard abs(denominator) > sin(10 * Double.pi / 180) else { return nil }
        let t = (b.point - a.point).cross(b.direction) / denominator
        return a.point + a.direction * t
    }

    /// A unit direction turned onto the nearest axis when within `axisSnap`.
    static func snapped(_ direction: Vec) -> Vec {
        let angle = atan2(direction.y, direction.x)
        let quarter = (angle / (.pi / 2)).rounded()
        guard abs(angle - quarter * .pi / 2) <= axisSnap * .pi / 180 else { return direction.normalized }
        switch (Int(quarter) % 4 + 4) % 4 {
        case 0: return Vec(1, 0)
        case 1: return Vec(0, 1)
        case 2: return Vec(-1, 0)
        default: return Vec(0, -1)
        }
    }

    static func cumulative(_ path: [Vec]) -> [Double] {
        var lengths = [0.0]
        lengths.reserveCapacity(path.count)
        for index in path.indices.dropFirst() {
            lengths.append(lengths[index - 1] + (path[index] - path[index - 1]).length)
        }
        return lengths
    }

    static func length(_ path: [Vec]) -> Double { cumulative(path).last ?? 0 }

    /// `count` points evenly spaced along the path (round the loop when
    /// `closed`, without repeating the first point).
    static func resample(_ points: [Vec], count: Int, closed: Bool) -> [Vec] {
        guard let first = points.first, count >= 2 else { return [] }
        let path = closed ? points + [first] : points
        let lengths = cumulative(path)
        guard let total = lengths.last, total > 0 else { return [] }
        let step = total / Double(closed ? count : count - 1)
        var result: [Vec] = []
        result.reserveCapacity(count)
        var segment = 0
        for k in 0..<count {
            let target = Double(k) * step
            while segment < path.count - 2, lengths[segment + 1] < target { segment += 1 }
            let span = lengths[segment + 1] - lengths[segment]
            let t = span > 0 ? min(max((target - lengths[segment]) / span, 0), 1) : 0
            result.append(path[segment] + (path[segment + 1] - path[segment]) * t)
        }
        return result
    }

    /// Positive when the outline runs anticlockwise with y up.
    static func signedArea(_ polygon: [Vec]) -> Double {
        guard polygon.count >= 3 else { return 0 }
        var sum = 0.0
        for index in polygon.indices {
            sum += polygon[index].cross(polygon[(index + 1) % polygon.count])
        }
        return sum / 2
    }

    /// Andrew's monotone chain.
    static func convexHull(_ points: [Vec]) -> [Vec] {
        let sorted = points.sorted { $0.x == $1.x ? $0.y < $1.y : $0.x < $1.x }
        guard sorted.count >= 3 else { return sorted }
        var lower: [Vec] = [], upper: [Vec] = []
        for p in sorted {
            while lower.count >= 2, (lower[lower.count - 1] - lower[lower.count - 2]).cross(p - lower[lower.count - 1]) <= 0 {
                lower.removeLast()
            }
            lower.append(p)
        }
        for p in sorted.reversed() {
            while upper.count >= 2, (upper[upper.count - 1] - upper[upper.count - 2]).cross(p - upper[upper.count - 1]) <= 0 {
                upper.removeLast()
            }
            upper.append(p)
        }
        return Array(lower.dropLast() + upper.dropLast())
    }

    /// Distance from `p` to the segment `a`-`b`.
    static func distance(_ p: Vec, _ a: Vec, _ b: Vec) -> Double {
        let ab = b - a
        let squared = ab.dot(ab)
        guard squared > 0 else { return (p - a).length }
        let t = min(max((p - a).dot(ab) / squared, 0), 1)
        return (p - (a + ab * t)).length
    }
}

extension SIMD2 where Scalar == Double {
    fileprivate var length: Double { (x * x + y * y).squareRoot() }
    fileprivate var normalized: SIMD2<Double> {
        let l = length
        return l > 0 ? self / l : SIMD2(1, 0)
    }
    fileprivate var point: CGPoint { CGPoint(x: x, y: y) }
    fileprivate func dot(_ other: SIMD2<Double>) -> Double { x * other.x + y * other.y }
    fileprivate func cross(_ other: SIMD2<Double>) -> Double { x * other.y - y * other.x }
    fileprivate func rotated(by angle: Double) -> SIMD2<Double> {
        let c = cos(angle), s = sin(angle)
        return SIMD2(x * c - y * s, x * s + y * c)
    }
}
