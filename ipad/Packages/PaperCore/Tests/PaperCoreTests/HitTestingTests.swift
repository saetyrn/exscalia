import CoreGraphics
import XCTest

@testable import PaperCore

/// What a one-finger tap selects (§4.6): objects first, then the nearest
/// stroke and the rest of its word.
final class HitTestingTests: XCTestCase {
    private typealias Stroke = HitTesting.Stroke
    private typealias Object = HitTesting.Object

    private func line(_ x0: CGFloat, _ y0: CGFloat, _ x1: CGFloat, _ y1: CGFloat, width: CGFloat = 2) -> Stroke {
        let count = 20
        let points = (0...count).map { k -> CGPoint in
            let t = CGFloat(k) / CGFloat(count)
            return CGPoint(x: x0 + (x1 - x0) * t, y: y0 + (y1 - y0) * t)
        }
        return Stroke(points: points, width: width)
    }

    /// A small loop standing in for a letter, `size` across, its top-left at
    /// (x, y).
    private func letter(_ x: CGFloat, _ y: CGFloat, size: CGFloat = 12, height: CGFloat? = nil) -> Stroke {
        let h = height ?? size
        let points = (0...24).map { k -> CGPoint in
            let t = CGFloat(k) / 24 * 2 * .pi
            return CGPoint(x: x + size / 2 + size / 2 * cos(t), y: y + h / 2 + h / 2 * sin(t))
        }
        return Stroke(points: points, width: 1.5)
    }

    private func target(_ x: CGFloat, _ y: CGFloat, _ strokes: [Stroke] = [], _ objects: [Object] = [], zoom: CGFloat = 1)
        -> HitTarget?
    {
        HitTesting.target(at: CGPoint(x: x, y: y), strokes: strokes, objects: objects, zoom: zoom)
    }

    func testTheContractsNumbers() {
        XCTAssertEqual(HitTesting.touchRadius, 12)
        XCTAssertEqual(HitTesting.wordGap, 4)
    }

    func testEmptyPaperSelectsNothing() {
        XCTAssertNil(target(100, 100))
        XCTAssertNil(target(100, 100, [line(0, 300, 300, 300)]))
        XCTAssertNil(target(.nan, 100, [line(0, 100, 300, 100)]))
        XCTAssertNil(target(150, .infinity, [line(0, 100, 300, 100)]))
    }

    // MARK: Objects

    func testObjectsComeBeforeInk() {
        let strokes = [line(0, 120, 400, 120)]
        let objects = [
            Object(id: "t", kind: .text, frame: CGRect(x: 100, y: 100, width: 200, height: 50)),
        ]
        XCTAssertEqual(target(150, 120, strokes, objects), .text(id: "t"))
        XCTAssertEqual(target(350, 120, strokes, objects), .ink([0]))
    }

    func testTheTopMostObjectWins() {
        let objects = [
            Object(id: "photo", kind: .image, frame: CGRect(x: 0, y: 0, width: 300, height: 300)),
            Object(id: "box", kind: .shape, frame: CGRect(x: 100, y: 100, width: 100, height: 100)),
            Object(id: "note", kind: .text, frame: CGRect(x: 150, y: 150, width: 100, height: 40)),
        ]
        XCTAssertEqual(target(160, 160, [], objects), .text(id: "note"))
        XCTAssertEqual(target(120, 120, [], objects), .shape(id: "box"))
        XCTAssertEqual(target(20, 20, [], objects), .image(id: "photo"))
        XCTAssertNil(target(320, 320, [], objects))
    }

    func testAThinObjectIsWidenedToTheTouch() {
        let rule = [Object(id: "rule", kind: .shape, frame: CGRect(x: 100, y: 200, width: 200, height: 2))]
        XCTAssertEqual(target(150, 210, [], rule), .shape(id: "rule"))
        XCTAssertEqual(target(150, 190, [], rule), .shape(id: "rule"))
        XCTAssertNil(target(150, 210, [], rule, zoom: 4), "3 pt of reach at 4x")
        XCTAssertNil(target(90, 201, [], rule), "only the thin axis grows")
        // A shape stored with a negative size is still found.
        let flipped = [Object(id: "f", kind: .shape, frame: CGRect(x: 300, y: 300, width: -100, height: -60))]
        XCTAssertEqual(target(250, 270, [], flipped), .shape(id: "f"))
    }

    func testALineShapeIsHitAlongItsDiagonal() {
        let down = [Object(id: "l", kind: .shape, frame: CGRect(x: 0, y: 0, width: 200, height: 100), isLine: true)]
        XCTAssertEqual(target(100, 50, [], down), .shape(id: "l"))
        XCTAssertEqual(target(100, 58, [], down), .shape(id: "l"))
        XCTAssertNil(target(180, 20, [], down), "inside the frame, far from the line")
        XCTAssertNil(target(210, 105, [], down, zoom: 2), "past its end")

        // A negative width runs the line from the top-right to the bottom-left.
        let up = [Object(id: "u", kind: .shape, frame: CGRect(x: 200, y: 0, width: -200, height: 100), isLine: true)]
        XCTAssertEqual(target(100, 50, [], up), .shape(id: "u"))
        XCTAssertEqual(target(190, 5, [], up), .shape(id: "u"))
        XCTAssertNil(target(20, 20, [], up))

        let thick = [
            Object(id: "w", kind: .shape, frame: CGRect(x: 0, y: 0, width: 200, height: 0), isLine: true, strokeWidth: 40),
        ]
        XCTAssertEqual(target(100, 18, [], thick, zoom: 4), .shape(id: "w"), "half its width reaches further than the touch")
    }

    func testABrokenFrameIsNeverHit() {
        let objects = [Object(id: "x", kind: .image, frame: CGRect(x: CGFloat.nan, y: 0, width: 100, height: 100))]
        XCTAssertNil(target(50, 50, [], objects))
    }

    // MARK: Strokes

    func testTheTouchReachShrinksWithZoom() {
        let strokes = [line(0, 100, 300, 100)]
        XCTAssertEqual(target(150, 110, strokes), .ink([0]))
        XCTAssertNil(target(150, 110, strokes, zoom: 2))
        XCTAssertEqual(target(150, 105, strokes, zoom: 2), .ink([0]))
        XCTAssertEqual(target(150, 140, strokes, zoom: 0.25), .ink([0]), "48 pt at a quarter")
        XCTAssertEqual(target(150, 110, strokes, zoom: .nan), .ink([0]), "a bad zoom reads as 1")
        XCTAssertNil(target(150, 113, strokes))
        XCTAssertNil(target(-15, 100, strokes), "past the end")
    }

    func testAWideStrokeIsHitAcrossItsWidth() {
        let highlighter = [line(0, 100, 300, 100, width: 30)]
        XCTAssertEqual(target(150, 114, highlighter, zoom: 2), .ink([0]))
        XCTAssertNil(target(150, 116, highlighter, zoom: 2))
    }

    func testTheNearestStrokeWins() {
        let strokes = [line(0, 100, 300, 100), line(0, 118, 300, 118)]
        XCTAssertEqual(target(150, 107, strokes), .ink([0]))
        XCTAssertEqual(target(150, 111, strokes), .ink([1]))
        // On a tie the top-most, the later one, is hit.
        let same = [line(0, 100, 300, 100), line(0, 100, 300, 100)]
        XCTAssertEqual(HitTesting.stroke(at: CGPoint(x: 150, y: 104), in: same, reach: 12), 1)
    }

    func testAStrokeIsTestedByItsPathNotItsBounds() {
        // A diagonal: the middle of its bounding box is empty paper.
        let strokes = [line(0, 0, 300, 300)]
        XCTAssertNil(target(250, 50, strokes))
        XCTAssertEqual(target(150, 155, strokes), .ink([0]))
    }

    func testADotAndAnEmptyStroke() {
        let strokes = [
            Stroke(points: [CGPoint(x: 100, y: 100)], width: 4),
            Stroke(points: [], width: 4),
            Stroke(points: [CGPoint(x: CGFloat.nan, y: 1)], width: 4),
        ]
        XCTAssertEqual(target(108, 100, strokes), .ink([0]))
        XCTAssertNil(target(0, 0, strokes))
        XCTAssertTrue(strokes[1].bounds.isNull)
        XCTAssertTrue(strokes[2].bounds.isNull)
        XCTAssertEqual(Stroke(points: [], width: .nan).width, 0)
    }

    func testBoundsDefaultToThePointsGrownByHalfTheWidth() {
        let stroke = Stroke(points: [CGPoint(x: 10, y: 20), CGPoint(x: 110, y: 70)], width: 6)
        XCTAssertEqual(stroke.bounds, CGRect(x: 7, y: 17, width: 106, height: 56))
        let given = Stroke(points: [CGPoint(x: 10, y: 20)], width: 6, bounds: CGRect(x: 0, y: 0, width: 1, height: 1))
        XCTAssertEqual(given.bounds, CGRect(x: 0, y: 0, width: 1, height: 1))
    }

    // MARK: Words

    /// Two lines of handwriting: "hello" and "world" on the first, 20 pt
    /// apart, and "again" 30 pt below.
    private func page() -> [Stroke] {
        var strokes: [Stroke] = []
        for k in 0..<5 { strokes.append(letter(100 + CGFloat(k) * 15, 100)) }  // 0...4, gaps of 3
        for k in 0..<5 { strokes.append(letter(195 + CGFloat(k) * 15, 100)) }  // 5...9, 20 from "hello"
        for k in 0..<5 { strokes.append(letter(100 + CGFloat(k) * 15, 142)) }  // 10...14, 30 below
        return strokes
    }

    func testATapSelectsTheWholeWord() {
        let strokes = page()
        XCTAssertEqual(target(130, 100, strokes), .ink([0, 1, 2, 3, 4]))
        XCTAssertEqual(target(201, 100, strokes), .ink([5, 6, 7, 8, 9]))
        XCTAssertEqual(target(100, 148, strokes), .ink([10, 11, 12, 13, 14]))
        XCTAssertEqual(HitTesting.word(around: 9, in: strokes), [5, 6, 7, 8, 9])
    }

    func testStrokesOfOneLetterAreChained() {
        // A "t": the stem and a cross bar that overlaps it, then an "o" 2 pt
        // to the right of the bar's end.
        let strokes = [
            line(100, 90, 100, 120), line(92, 100, 108, 100), letter(110, 96),
            letter(200, 96),
        ]
        XCTAssertEqual(target(100, 115, strokes), .ink([0, 1, 2]))
        XCTAssertEqual(target(205, 96, strokes), .ink([3]))
    }

    func testDotsAndAccentsJoinTheirLetter() {
        // An "i": its stem, and a dot 3 pt above it that shares none of its
        // height; then an "n" beside it.
        let strokes = [
            line(100, 100, 100, 112), Stroke(points: [CGPoint(x: 100, y: 94)], width: 3), letter(104, 100),
        ]
        XCTAssertEqual(target(100, 108, strokes), .ink([0, 1, 2]))
        XCTAssertEqual(target(100, 94, strokes), .ink([0, 1, 2]), "tapping the dot")
        // A dot between two lines joins one and pulls in neither's neighbour.
        let lines = [letter(100, 100), Stroke(points: [CGPoint(x: 106, y: 116)], width: 3), letter(100, 118)]
        XCTAssertEqual(target(106, 100, lines), .ink([0, 1]))
    }

    func testADescenderDoesNotPullInTheLineBelow() {
        // A "g" on the first line whose tail reaches into the line below.
        var strokes = page()
        strokes.append(letter(175, 100, size: 12, height: 46))  // 15: y 100...146
        strokes.append(letter(175, 142))  // 16: ends "again" on the line below, under the tail
        let tappedLetter = target(130, 100, strokes)
        XCTAssertEqual(tappedLetter, .ink([0, 1, 2, 3, 4, 15]), "the g joins its word; the line below does not")
        let tappedTail = target(181, 146, strokes)
        XCTAssertEqual(tappedTail, .ink([0, 1, 2, 3, 4, 15]), "from the tail too")
        let below = target(181, 154, strokes)
        XCTAssertEqual(below, .ink([10, 11, 12, 13, 14, 16]), "and the line below stays on its own")
    }

    func testWordOutOfRange() {
        XCTAssertEqual(HitTesting.word(around: 3, in: []), [])
        XCTAssertEqual(HitTesting.word(around: 0, in: [Stroke(points: [], width: 1)]), [0])
    }
}
