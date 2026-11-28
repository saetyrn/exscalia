import CoreGraphics
import Foundation
import PencilKit
import XCTest

@testable import PaperCore

private typealias V = SIMD2<Double>

private func cg(_ v: V) -> CGPoint { CGPoint(x: v.x, y: v.y) }
private func vec(_ p: CGPoint) -> V { V(Double(p.x), Double(p.y)) }
private func norm(_ v: V) -> Double { (v.x * v.x + v.y * v.y).squareRoot() }
private func rotate(_ v: V, degrees: Double) -> V {
    let a = degrees * .pi / 180
    return V(v.x * cos(a) - v.y * sin(a), v.x * sin(a) + v.y * cos(a))
}

/// Rough strokes as a hand draws them: a point every few points of travel,
/// a slow drift across the path and a little jitter, the same for the same
/// seed.
private struct Hand {
    private var state: UInt64
    private let phase: Double

    init(seed: UInt64) {
        state = seed &* 0x9E37_79B9_7F4A_7C15 | 1
        phase = Double(seed % 628) / 100
    }

    /// -1...1.
    mutating func jitter() -> Double {
        state ^= state << 13
        state ^= state >> 7
        state ^= state << 17
        return Double(state % 2_000_001) / 1_000_000 - 1
    }

    /// How far off the path the hand is after `travelled` points.
    mutating func drift(_ travelled: Double, _ amount: Double) -> Double {
        amount * (0.7 * sin(travelled / 37 + phase) + 0.3 * jitter())
    }

    /// Straight edges through `corners`, a point every `step`. A closed trace
    /// comes back to the first corner and runs `overshoot` points past it.
    mutating func trace(
        _ corners: [V], closed: Bool = false, overshoot: Double = 0, step: Double = 3, wobble: Double = 1.5
    ) -> [CGPoint] {
        var path = corners
        if closed, corners.count > 1 {
            path.append(corners[0])
            if overshoot > 0 {
                let direction = (corners[1] - corners[0]) / norm(corners[1] - corners[0])
                path.append(corners[0] + direction * overshoot)
            }
        }
        var points: [CGPoint] = []
        var travelled = 0.0
        for index in 0..<(path.count - 1) {
            let a = path[index], b = path[index + 1]
            let length = norm(b - a)
            guard length > 0 else { continue }
            let direction = (b - a) / length
            let normal = V(-direction.y, direction.x)
            let count = max(1, Int((length / step).rounded()))
            for k in 0..<count {
                let t = Double(k) / Double(count)
                points.append(cg(a + (b - a) * t + normal * drift(travelled + t * length, wobble)))
            }
            travelled += length
        }
        if let last = path.last { points.append(cg(last + V(jitter(), jitter()) * wobble * 0.3)) }
        return points
    }

    /// An ellipse from `start` degrees through `sweep` degrees, its radius
    /// wavering by `waver` of itself.
    mutating func ellipse(
        center: V, radii: V, rotation: Double = 0, start: Double = 0, sweep: Double = 375,
        waver: Double = 0.02, step: Double = 3
    ) -> [CGPoint] {
        let perimeter = 2 * Double.pi * ((radii.x * radii.x + radii.y * radii.y) / 2).squareRoot()
        let count = max(8, Int(perimeter * abs(sweep) / 360 / step))
        return (0...count).map { k in
            let theta = (start + sweep * Double(k) / Double(count)) * .pi / 180
            let factor = 1 + waver * (0.7 * sin(3 * theta + phase) + 0.3 * jitter())
            let local = V(radii.x * cos(theta), radii.y * sin(theta)) * factor
            return cg(center + rotate(local, degrees: rotation))
        }
    }
}

final class ShapeRecognizerTests: XCTestCase {
    private func recognize(_ points: [CGPoint], zoom: CGFloat = 1) -> RecognizedShape? {
        ShapeRecognizer.recognize(points: points, zoom: zoom)
    }

    private func assertNear(
        _ point: CGPoint?, _ expected: V, within tolerance: Double, _ message: String = "",
        file: StaticString = #filePath, line: UInt = #line
    ) {
        guard let point else { return XCTFail("no point \(message)", file: file, line: line) }
        let distance = norm(vec(point) - expected)
        XCTAssertLessThanOrEqual(
            distance, tolerance, "\(point) is \(distance) from \(expected) \(message)", file: file, line: line)
    }

    /// Each expected corner has a recognised vertex within `tolerance`.
    private func assertCorners(
        _ shape: RecognizedShape?, _ corners: [V], within tolerance: Double,
        file: StaticString = #filePath, line: UInt = #line
    ) {
        guard let shape else { return XCTFail("not recognised", file: file, line: line) }
        XCTAssertEqual(shape.vertices.count, corners.count, "\(shape.vertices)", file: file, line: line)
        for corner in corners {
            let nearest = shape.vertices.map { norm(vec($0) - corner) }.min() ?? .infinity
            XCTAssertLessThanOrEqual(nearest, tolerance, "corner \(corner) in \(shape.vertices)", file: file, line: line)
        }
    }

    private func degrees(_ v: V) -> Double { atan2(v.y, v.x) * 180 / .pi }

    // MARK: Recognised

    func testARoughRectangle() throws {
        var hand = Hand(seed: 1)
        let corners = [V(100, 100), V(300, 100), V(300, 220), V(100, 220)]
        let shape = try XCTUnwrap(recognize(hand.trace(corners, closed: true, overshoot: 12)))
        XCTAssertEqual(shape.kind, .rectangle)
        assertCorners(shape, corners, within: 5)
        assertNear(shape.vertices.first, corners[0], within: 5, "starts where the stroke did")
        // Straightened onto the axes.
        let v = shape.vertices
        XCTAssertEqual(v[0].y, v[1].y, accuracy: 1e-9)
        XCTAssertEqual(v[1].x, v[2].x, accuracy: 1e-9)
        XCTAssertEqual(v[2].y, v[3].y, accuracy: 1e-9)
        XCTAssertEqual(v[3].x, v[0].x, accuracy: 1e-9)
        XCTAssertTrue(shape.isClosed)
    }

    func testARotatedRectangle() throws {
        var hand = Hand(seed: 2)
        let center = V(300, 300)
        let corners = [V(-110, -65), V(110, -65), V(110, 65), V(-110, 65)].map { center + rotate($0, degrees: 30) }
        let shape = try XCTUnwrap(recognize(hand.trace(corners, closed: true, overshoot: 8)))
        XCTAssertEqual(shape.kind, .rectangle)
        assertCorners(shape, corners, within: 7)
        let v = shape.vertices.map(vec)
        for index in 0..<4 {
            let a = v[(index + 1) % 4] - v[index], b = v[(index + 2) % 4] - v[(index + 1) % 4]
            XCTAssertEqual((a.x * b.x + a.y * b.y) / (norm(a) * norm(b)), 0, accuracy: 1e-9, "square corners")
        }
        XCTAssertEqual(norm(v[1] - v[0]), 220, accuracy: 220 * 0.04)
        XCTAssertEqual(norm(v[2] - v[1]), 130, accuracy: 130 * 0.05)
        XCTAssertEqual(degrees(v[1] - v[0]), 30, accuracy: 2)
    }

    func testASquareDrawnAnticlockwiseFromAnotherCorner() throws {
        var hand = Hand(seed: 3)
        let corners = [V(300, 300), V(300, 150), V(150, 150), V(150, 300)]
        let shape = try XCTUnwrap(recognize(hand.trace(corners, closed: true, overshoot: 5)))
        XCTAssertEqual(shape.kind, .rectangle)
        assertCorners(shape, corners, within: 5)
        assertNear(shape.vertices.first, corners[0], within: 5)
        assertNear(shape.vertices.dropFirst().first, corners[1], within: 5, "in drawing order")
    }

    func testRoundedCornersStillMakeARectangle() throws {
        var hand = Hand(seed: 4)
        // Each corner cut by a short diagonal, as a quick hand rounds it.
        let r = 14.0
        let path = [
            V(100 + r, 100), V(300 - r, 100), V(300, 100 + r), V(300, 220 - r), V(300 - r, 220),
            V(100 + r, 220), V(100, 220 - r), V(100, 100 + r),
        ]
        let shape = try XCTUnwrap(recognize(hand.trace(path, closed: true, overshoot: 10)))
        XCTAssertEqual(shape.kind, .rectangle)
        assertCorners(shape, [V(100, 100), V(300, 100), V(300, 220), V(100, 220)], within: 8)
    }

    func testATriangle() throws {
        var hand = Hand(seed: 5)
        let corners = [V(100, 300), V(200, 120), V(320, 300)]
        let shape = try XCTUnwrap(recognize(hand.trace(corners, closed: true, overshoot: 10)))
        XCTAssertEqual(shape.kind, .triangle)
        assertCorners(shape, corners, within: 6)
    }

    func testAnEllipse() throws {
        var hand = Hand(seed: 6)
        let shape = try XCTUnwrap(
            recognize(hand.ellipse(center: V(300, 300), radii: V(150, 80), rotation: 20, start: 40)))
        XCTAssertEqual(shape.kind, .ellipse)
        XCTAssertFalse(shape.isCircle)
        assertNear(shape.center, V(300, 300), within: 5)
        XCTAssertEqual(Double(shape.radii.width), 150, accuracy: 150 * 0.06)
        XCTAssertEqual(Double(shape.radii.height), 80, accuracy: 80 * 0.06)
        XCTAssertEqual(Double(shape.rotation) * 180 / .pi, 20, accuracy: 3)
        XCTAssertTrue(shape.vertices.isEmpty)
    }

    func testAnUprightEllipseIsStraightened() throws {
        var hand = Hand(seed: 7)
        let shape = try XCTUnwrap(
            recognize(hand.ellipse(center: V(200, 300), radii: V(70, 140), rotation: 3, start: 200)))
        XCTAssertEqual(shape.kind, .ellipse)
        XCTAssertEqual(shape.rotation, 0, "within the snap, so level")
        XCTAssertEqual(Double(shape.radii.width), 70, accuracy: 70 * 0.06)
        XCTAssertEqual(Double(shape.radii.height), 140, accuracy: 140 * 0.06)
    }

    func testAnEllipseTiltedPast45DegreesSwapsItsRadii() throws {
        var hand = Hand(seed: 8)
        let shape = try XCTUnwrap(
            recognize(hand.ellipse(center: V(300, 300), radii: V(150, 80), rotation: 70)))
        XCTAssertEqual(shape.kind, .ellipse)
        XCTAssertEqual(Double(shape.rotation) * 180 / .pi, -20, accuracy: 3)
        XCTAssertEqual(Double(shape.radii.width), 80, accuracy: 80 * 0.06)
        XCTAssertEqual(Double(shape.radii.height), 150, accuracy: 150 * 0.06)
    }

    func testANearCircleBecomesACircle() throws {
        var hand = Hand(seed: 9)
        // Axes 4 % apart, inside the 8 % the contract allows.
        let shape = try XCTUnwrap(
            recognize(hand.ellipse(center: V(250, 250), radii: V(92, 88), rotation: 35, start: 90, waver: 0.03)))
        XCTAssertEqual(shape.kind, .ellipse)
        XCTAssertTrue(shape.isCircle)
        XCTAssertEqual(shape.rotation, 0)
        XCTAssertEqual(Double(shape.radii.width), 90, accuracy: 90 * 0.05)
        XCTAssertEqual(ShapeRecognizer.circleTolerance, 0.08)

        let oval = try XCTUnwrap(recognize(Hand(seed: 9).ellipseCopy(radii: V(100, 80))))
        XCTAssertFalse(oval.isCircle, "20 % apart stays an ellipse")
    }

    func testPolygons() throws {
        for (sides, seed) in [(5, UInt64(10)), (6, 11)] {
            var hand = Hand(seed: seed)
            let corners = (0..<sides).map { k in
                V(300, 300) + rotate(V(0, -130), degrees: 360 * Double(k) / Double(sides) + 7)
            }
            let shape = try XCTUnwrap(recognize(hand.trace(corners, closed: true, overshoot: 8)), "\(sides)")
            XCTAssertEqual(shape.kind, .polygon, "\(sides)")
            assertCorners(shape, corners, within: 8)
        }
    }

    func testAnIrregularQuadrilateralIsAPolygon() throws {
        var hand = Hand(seed: 12)
        let corners = [V(100, 120), V(320, 90), V(280, 300), V(140, 260)]
        let shape = try XCTUnwrap(recognize(hand.trace(corners, closed: true, overshoot: 6)))
        XCTAssertEqual(shape.kind, .polygon)
        assertCorners(shape, corners, within: 7)
    }

    func testALine() throws {
        var hand = Hand(seed: 13)
        let shape = try XCTUnwrap(recognize(hand.trace([V(50, 50), V(350, 260)])))
        XCTAssertEqual(shape.kind, .line)
        XCTAssertEqual(shape.vertices.count, 2)
        assertNear(shape.vertices.first, V(50, 50), within: 5)
        assertNear(shape.vertices.last, V(350, 260), within: 5)
        XCTAssertFalse(shape.isClosed)
        XCTAssertEqual(shape.outlines().count, 1)
    }

    func testANearlyLevelLineIsMadeLevel() throws {
        var hand = Hand(seed: 14)
        let shape = try XCTUnwrap(recognize(hand.trace([V(50, 100), V(350, 112)])))  // 2.3°
        XCTAssertEqual(shape.kind, .line)
        XCTAssertEqual(shape.vertices[0].y, shape.vertices[1].y, accuracy: 1e-9)
        XCTAssertLessThan(shape.vertices[0].x, shape.vertices[1].x, "keeps its direction")

        let steep = try XCTUnwrap(recognize(Hand(seed: 15).traceCopy([V(100, 400), V(108, 100)])))
        XCTAssertEqual(steep.vertices[0].x, steep.vertices[1].x, accuracy: 1e-9)
        XCTAssertGreaterThan(steep.vertices[0].y, steep.vertices[1].y)
    }

    func testALineWithAHookAtTheEndIsStillALine() throws {
        var hand = Hand(seed: 16)
        let shape = try XCTUnwrap(recognize(hand.trace([V(50, 200), V(340, 200), V(346, 192)], wobble: 1)))
        XCTAssertEqual(shape.kind, .line)
        XCTAssertEqual(shape.vertices[0].y, shape.vertices[1].y, accuracy: 1e-9)
    }

    private func assertArrow(
        _ shape: RecognizedShape?, tail: V, tip: V, within tolerance: Double = 6,
        file: StaticString = #filePath, line: UInt = #line
    ) {
        guard let shape else { return XCTFail("not recognised", file: file, line: line) }
        XCTAssertEqual(shape.kind, .arrow, file: file, line: line)
        assertNear(shape.vertices.first, tail, within: tolerance, "tail", file: file, line: line)
        assertNear(shape.vertices.last, tip, within: tolerance, "tip", file: file, line: line)
        XCTAssertEqual(shape.arrowHead.count, 2, file: file, line: line)
        guard shape.vertices.count == 2, shape.arrowHead.count == 2 else { return }
        let start = vec(shape.vertices[0]), end = vec(shape.vertices[1])
        let axis = (end - start) / norm(end - start)
        let barbs = shape.arrowHead.map { vec($0) - end }
        // Symmetric about the shaft and pointing back along it.
        XCTAssertEqual(norm(barbs[0]), norm(barbs[1]), accuracy: 1e-6, file: file, line: line)
        let across = barbs.map { axis.x * $0.y - axis.y * $0.x }
        XCTAssertEqual(across[0], -across[1], accuracy: 1e-6, file: file, line: line)
        for barb in barbs { XCTAssertLessThan(barb.x * axis.x + barb.y * axis.y, 0, file: file, line: line) }
        XCTAssertEqual(shape.outlines().count, 2, file: file, line: line)
        XCTAssertEqual(shape.outlines().last?.count, 3, file: file, line: line)
    }

    func testAnArrowDrawnTailFirst() {
        var hand = Hand(seed: 17)
        let stroke = hand.trace([V(60, 300), V(300, 300), V(268, 278), V(299, 299), V(268, 322)], wobble: 1.2)
        let shape = recognize(stroke)
        assertArrow(shape, tail: V(60, 300), tip: V(300, 300))
        XCTAssertEqual(shape?.vertices[0].y, shape?.vertices[1].y, "level shaft")
    }

    func testAnArrowDrawnHeadFirst() {
        var hand = Hand(seed: 18)
        let stroke = hand.trace([V(270, 120), V(300, 150), V(270, 180), V(299, 151), V(60, 150)], wobble: 1.2)
        assertArrow(recognize(stroke), tail: V(60, 150), tip: V(300, 150))
    }

    func testAnArrowWhoseReturnToTheTipOvershootsIt() {
        var hand = Hand(seed: 19)
        let stroke = hand.trace([V(60, 100), V(300, 280), V(260, 276), V(306, 285), V(280, 240)], wobble: 1)
        assertArrow(recognize(stroke), tail: V(60, 100), tip: V(300, 280))
    }

    func testAnArrowWithAClosedHead() {
        var hand = Hand(seed: 20)
        let stroke = hand.trace([V(80, 80), V(80, 330), V(62, 300), V(98, 300), V(80, 330)], wobble: 1)
        let shape = recognize(stroke)
        assertArrow(shape, tail: V(80, 80), tip: V(80, 330))
        XCTAssertEqual(shape?.vertices[0].x, shape?.vertices[1].x, "upright shaft")
    }

    func testZoomSetsTheSmallestShape() {
        // 12 pt across on the page: handwriting at 1x, a circle at 2x.
        let small = Hand(seed: 21).ellipseCopy(center: V(50, 50), radii: V(6, 6))
        XCTAssertNil(recognize(small, zoom: 1))
        XCTAssertEqual(recognize(small, zoom: 2)?.kind, .ellipse)
        let medium = Hand(seed: 22).ellipseCopy(center: V(50, 50), radii: V(14, 14))
        XCTAssertEqual(recognize(medium, zoom: 1)?.kind, .ellipse)
        XCTAssertNil(recognize(medium, zoom: 0.5))
        XCTAssertEqual(recognize(medium, zoom: .nan)?.kind, .ellipse, "a bad zoom reads as 1")
        XCTAssertEqual(recognize(medium, zoom: 0)?.kind, .ellipse)
        XCTAssertEqual(ShapeRecognizer.minimumSize, 16)
    }

    func testDrawingDirectionAndStartDoNotMatter() throws {
        let corners = [V(100, 100), V(300, 100), V(300, 220), V(100, 220)]
        for seed in UInt64(30)..<UInt64(42) {
            var hand = Hand(seed: seed)
            let first = Int(seed) % 4
            let rotated = Array(corners[first...] + corners[..<first])
            let ordered = seed % 2 == 0 ? rotated : rotated.reversed()
            let shape = recognize(hand.trace(ordered, closed: true, overshoot: Double(seed % 5) * 4))
            XCTAssertEqual(shape?.kind, .rectangle, "seed \(seed)")
            assertCorners(shape, corners, within: 6)
        }
        for seed in UInt64(50)..<UInt64(62) {
            var hand = Hand(seed: seed)
            let shape = recognize(
                hand.ellipse(
                    center: V(300, 300), radii: V(140, 90), rotation: Double(seed) * 3,
                    start: Double(seed) * 29, sweep: seed % 2 == 0 ? 370 : -370))
            XCTAssertEqual(shape?.kind, .ellipse, "seed \(seed)")
        }
        for seed in UInt64(70)..<UInt64(82) {
            var hand = Hand(seed: seed)
            let angle = Double(seed) * 31
            let corners = [V(0, -110), V(100, 70), V(-95, 60)].map { V(300, 300) + rotate($0, degrees: angle) }
            let shape = recognize(hand.trace(seed % 2 == 0 ? corners : corners.reversed(), closed: true, overshoot: 6))
            XCTAssertEqual(shape?.kind, .triangle, "seed \(seed)")
        }
        for seed in UInt64(90)..<UInt64(102) {
            var hand = Hand(seed: seed)
            let angle = Double(seed) * 37
            let local = [V(-130, 0), V(130, 0), V(100, -22), V(129, 1), V(100, 22)]
            let points = local.map { V(300, 300) + rotate($0, degrees: angle) }
            let stroke = hand.trace(seed % 2 == 0 ? points : Array(points.reversed()), wobble: 1.2)
            // A shaft within the axis snap is straightened about its middle,
            // which moves its ends up to 130 × sin 6° ≈ 14 pt.
            assertArrow(recognize(stroke), tail: points[0], tip: points[1], within: 14)
        }
    }

    func testCleanShapesAreRecognisedAsThemselves() throws {
        let shapes = [
            RecognizedShape(kind: .rectangle, vertices: [CGPoint(x: 0, y: 0), CGPoint(x: 200, y: 0), CGPoint(x: 200, y: 100), CGPoint(x: 0, y: 100)]),
            RecognizedShape(kind: .triangle, vertices: [CGPoint(x: 0, y: 200), CGPoint(x: 100, y: 0), CGPoint(x: 200, y: 200)]),
            RecognizedShape(kind: .ellipse, center: CGPoint(x: 200, y: 200), radii: CGSize(width: 120, height: 60)),
            RecognizedShape(kind: .line, vertices: [CGPoint(x: 10, y: 10), CGPoint(x: 210, y: 150)]),
        ]
        for shape in shapes {
            let again = try XCTUnwrap(recognize(try XCTUnwrap(shape.outlines().first)), "\(shape.kind)")
            XCTAssertEqual(again.kind, shape.kind)
        }
    }

    // MARK: Left alone

    func testHandwritingAndScribblesAreLeftAlone() {
        var hand = Hand(seed: 70)
        let loops = (0...400).map { k -> CGPoint in
            let t = Double(k) * 0.06
            return CGPoint(x: 100 + 6 * t - 10 * sin(t), y: 200 + 14 * cos(t))
        }
        let spiral = (0...300).map { k -> CGPoint in
            let t = Double(k) * 0.06
            return CGPoint(x: 200 + (5 + 5 * t) * cos(t), y: 200 + (5 + 5 * t) * sin(t))
        }
        let figureEight = (0...240).map { k -> CGPoint in
            let t = Double(k) / 240 * 2 * .pi
            return CGPoint(x: 200 + 120 * sin(t), y: 200 + 70 * sin(t) * cos(t))
        }
        var shading: [V] = []
        for pass in 0..<10 { shading += [V(100, 100 + Double(pass) * 6), V(200, 103 + Double(pass) * 6)] }
        let wave = (0...200).map { k -> CGPoint in
            CGPoint(x: 50 + Double(k) * 1.5, y: 200 + 22 * sin(Double(k) / 12))
        }
        let cases: [(String, [CGPoint])] = [
            ("cursive loops", loops),
            ("spiral", spiral),
            ("figure eight", figureEight),
            ("shading", hand.trace(shading, wobble: 1)),
            ("wave", wave),
            ("tick", hand.trace([V(100, 140), V(130, 170), V(200, 80)])),
            ("zigzag", hand.trace([V(0, 0), V(40, 60), V(80, 0), V(120, 60), V(160, 0)])),
            ("C", hand.ellipse(center: V(200, 200), radii: V(80, 80), start: 40, sweep: 280)),
            ("U", hand.ellipse(center: V(200, 200), radii: V(90, 60), start: 0, sweep: 180)),
            ("there and back", hand.trace([V(50, 50), V(300, 50), V(56, 53)], wobble: 1.5)),
            ("circle traced three times", hand.ellipse(center: V(200, 200), radii: V(70, 70), sweep: 1080)),
            ("one-sided hook", hand.trace([V(60, 300), V(300, 300), V(268, 278)])),
            ("star", hand.trace(
                (0..<5).map { V(200, 200) + rotate(V(0, -120), degrees: 144 * Double($0)) }, closed: true)),
            ("crescent", hand.ellipse(center: V(200, 200), radii: V(100, 100), start: 60, sweep: 240)
                + Hand(seed: 71).ellipseCopy(center: V(250, 200), radii: V(70, 90), start: 240, sweep: -120)),
        ]
        for (name, stroke) in cases {
            XCTAssertNil(recognize(stroke), name)
        }
    }

    func testDegenerateInput() {
        XCTAssertNil(recognize([]))
        XCTAssertNil(recognize([CGPoint(x: 5, y: 5)]))
        XCTAssertNil(recognize(Array(repeating: CGPoint(x: 5, y: 5), count: 50)))
        XCTAssertNil(recognize([CGPoint(x: CGFloat.nan, y: 0), CGPoint(x: 0, y: CGFloat.infinity)]))
        // A dot, then non-finite points that are skipped.
        XCTAssertNil(recognize([CGPoint(x: 1, y: 1), CGPoint(x: 2, y: 2), CGPoint(x: CGFloat.nan, y: 9)]))
        let line = recognize([CGPoint(x: 0, y: 0), CGPoint(x: CGFloat.nan, y: 3), CGPoint(x: 100, y: 0)])
        XCTAssertEqual(line?.kind, .line, "two good points far apart are a line")
    }

    // MARK: Output

    func testOutlines() throws {
        let rectangle = RecognizedShape(
            kind: .rectangle,
            vertices: [CGPoint(x: 0, y: 0), CGPoint(x: 200, y: 0), CGPoint(x: 200, y: 100), CGPoint(x: 0, y: 100)])
        let outline = try XCTUnwrap(rectangle.outlines().first)
        XCTAssertEqual(outline.count, 5)
        XCTAssertEqual(outline.first, outline.last)

        let ellipse = RecognizedShape(
            kind: .ellipse, center: CGPoint(x: 10, y: 20), radii: CGSize(width: 50, height: 30), rotation: 0.3)
        let ring = try XCTUnwrap(ellipse.outlines(ellipseSegments: 36).first)
        XCTAssertEqual(ring.count, 37)
        XCTAssertEqual(Double(ring[0].x), Double(ring[36].x), accuracy: 1e-9)
        for point in ring {
            let local = rotate(vec(point) - V(10, 20), degrees: -0.3 * 180 / .pi)
            XCTAssertEqual(pow(local.x / 50, 2) + pow(local.y / 30, 2), 1, accuracy: 1e-9)
        }

        let arrow = RecognizedShape(
            kind: .arrow, vertices: [CGPoint(x: 0, y: 0), CGPoint(x: 100, y: 0)],
            arrowHead: [CGPoint(x: 80, y: -10), CGPoint(x: 80, y: 10)])
        XCTAssertEqual(
            arrow.outlines(),
            [[CGPoint(x: 0, y: 0), CGPoint(x: 100, y: 0)],
             [CGPoint(x: 80, y: -10), CGPoint(x: 100, y: 0), CGPoint(x: 80, y: 10)]])
    }

    func testSplinePathsTripleEveryInnerCorner() throws {
        let a = CGPoint(x: 0, y: 0), b = CGPoint(x: 200, y: 0), c = CGPoint(x: 200, y: 100), d = CGPoint(x: 0, y: 100)
        let rectangle = RecognizedShape(kind: .rectangle, vertices: [a, b, c, d])
        XCTAssertEqual(rectangle.splinePaths(), [[a, b, b, b, c, c, c, d, d, d, a]])
        let line = RecognizedShape(kind: .line, vertices: [a, b])
        XCTAssertEqual(line.splinePaths(), [[a, b]])

        let circle = RecognizedShape(kind: .ellipse, center: .zero, radii: CGSize(width: 100, height: 100))
        let ring = try XCTUnwrap(circle.splinePaths(ellipseSpacing: 4).first)
        XCTAssertEqual(ring.count, 158 + 1, "about every 4 points round 628")
        let tiny = RecognizedShape(kind: .ellipse, center: .zero, radii: CGSize(width: 5, height: 5))
        XCTAssertEqual(tiny.splinePaths().first?.count, 49, "never fewer than 48")
    }

    /// The clean strokes are built from `splinePaths`: PencilKit's spline must
    /// start and end on the outline, pass through each corner and keep each
    /// edge straight.
    func testPencilKitDrawsTheSplinePathsWithSharpCorners() throws {
        let shapes = [
            RecognizedShape(
                kind: .rectangle,
                vertices: [CGPoint(x: 10, y: 10), CGPoint(x: 210, y: 30), CGPoint(x: 200, y: 130), CGPoint(x: 0, y: 110)]),
            RecognizedShape(
                kind: .arrow, vertices: [CGPoint(x: 0, y: 0), CGPoint(x: 150, y: 0)],
                arrowHead: [CGPoint(x: 130, y: -12), CGPoint(x: 130, y: 12)]),
        ]
        for shape in shapes {
            for (controls, outline) in zip(shape.splinePaths(), shape.outlines()) {
                let path = PKStrokePath(
                    controlPoints: controls.enumerated().map { index, point in
                        PKStrokePoint(
                            location: point, timeOffset: Double(index) / 100, size: CGSize(width: 2, height: 2),
                            opacity: 1, force: 1, azimuth: 0, altitude: .pi / 2)
                    }, creationDate: Date())
                // PencilKit keeps single-precision floats: a thousandth of a
                // point is far below a pixel. Parametric steps land on every
                // control point, so a corner is sampled where it is.
                let drawn = path.interpolatedPoints(by: .parametricStep(0.125)).map(\.location)
                let tolerance = 1e-3
                assertNear(drawn.first, vec(outline[0]), within: tolerance, "\(shape.kind) start")
                assertNear(drawn.last, vec(outline[outline.count - 1]), within: tolerance, "\(shape.kind) end")
                for corner in outline {
                    let nearest = drawn.map { norm(vec($0) - vec(corner)) }.min() ?? .infinity
                    XCTAssertLessThan(nearest, tolerance, "\(shape.kind) corner \(corner)")
                }
                for point in drawn {
                    let off = zip(outline, outline.dropFirst()).map { distance(vec(point), vec($0), vec($1)) }.min() ?? 0
                    XCTAssertLessThan(off, tolerance, "\(shape.kind) edge at \(point)")
                }
            }
        }
    }

    private func distance(_ p: V, _ a: V, _ b: V) -> Double {
        let ab = b - a
        let t = min(max(((p - a) * ab).sum() / (ab * ab).sum(), 0), 1)
        return norm(p - (a + ab * t))
    }
}

extension Hand {
    /// A fresh hand's ellipse, for one-line cases.
    fileprivate func ellipseCopy(
        center: V = V(250, 250), radii: V, start: Double = 0, sweep: Double = 375
    ) -> [CGPoint] {
        var hand = self
        return hand.ellipse(center: center, radii: radii, start: start, sweep: sweep)
    }

    fileprivate func traceCopy(_ corners: [V]) -> [CGPoint] {
        var hand = self
        return hand.trace(corners)
    }
}
