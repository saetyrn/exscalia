import PaperCore
import PencilKit
import XCTest

@testable import PaperUI

/// The pen width is a number now rather than one of three buttons, so the thing
/// worth testing is that the number survives: the pen a person picked is the
/// pen they get back tomorrow.
@MainActor
final class ToolWidthTests: XCTestCase {
    private var suite: String = ""
    private var defaults = UserDefaults.standard

    override func setUp() {
        super.setUp()
        suite = "art.exscalia.tests.\(UUID().uuidString)"
        guard let defaults = UserDefaults(suiteName: suite) else {
            return XCTFail("could not make a defaults suite")
        }
        self.defaults = defaults
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suite)
        super.tearDown()
    }

    func testWidthsSurviveARestart() {
        let first = ToolState(defaults: defaults)
        first.penWidth = 7.25
        first.highlighterWidth = 31
        first.eraserWidth = 55

        let second = ToolState(defaults: defaults)
        XCTAssertEqual(second.penWidth, 7.25, accuracy: 0.0001)
        XCTAssertEqual(second.highlighterWidth, 31, accuracy: 0.0001)
        XCTAssertEqual(second.eraserWidth, 55, accuracy: 0.0001)
    }

    func testDefaultsAreInsideTheirRanges() {
        let tools = ToolState(defaults: defaults)
        XCTAssertTrue(ToolState.penWidthRange.contains(tools.penWidth))
        XCTAssertTrue(ToolState.highlighterWidthRange.contains(tools.highlighterWidth))
        XCTAssertTrue(ToolState.eraserWidthRange.contains(tools.eraserWidth))
    }

    func testRangesAreTheOnesThePlanAsksFor() {
        XCTAssertEqual(ToolState.penWidthRange, 0.5...12)
        XCTAssertEqual(ToolState.highlighterWidthRange, 6...40)
        XCTAssertEqual(ToolState.eraserWidthRange, 4...60)
        XCTAssertEqual(ToolState.widthRange(for: .pen), ToolState.penWidthRange)
        XCTAssertEqual(ToolState.widthRange(for: .highlighter), ToolState.highlighterWidthRange)
        XCTAssertEqual(ToolState.widthRange(for: .eraser), ToolState.eraserWidthRange)
    }

    func testAWidthOutsideItsRangeIsClamped() {
        let tools = ToolState(defaults: defaults)
        tools.penWidth = 900
        XCTAssertEqual(tools.penWidth, ToolState.penWidthRange.upperBound)
        tools.penWidth = -4
        XCTAssertEqual(tools.penWidth, ToolState.penWidthRange.lowerBound)
        tools.eraserWidth = 1
        XCTAssertEqual(tools.eraserWidth, ToolState.eraserWidthRange.lowerBound)
    }

    func testAStoredWidthFromAnOlderBuildIsClampedOnLoad() {
        // The old three-button eraser offered 12/24/48; the old highlighter
        // offered 12/20/30. Both are inside the new ranges. A value that is
        // not -- a corrupt or hand-edited default -- must not reach PencilKit.
        defaults.set(9_999.0, forKey: "paper.tool.penWidth")
        let tools = ToolState(defaults: defaults)
        XCTAssertEqual(tools.penWidth, ToolState.penWidthRange.upperBound)
    }

    func testTheWidthReachesTheTool() {
        let tools = ToolState(defaults: defaults)
        tools.setWidth(9, for: .pen)
        XCTAssertEqual(tools.width(for: .pen), 9)
        guard let ink = tools.pkTool as? PKInkingTool else { return XCTFail("expected an ink") }
        XCTAssertEqual(ink.width, 9, accuracy: 0.0001)

        tools.select(.eraser)
        tools.setWidth(40, for: .eraser)
        guard let eraser = tools.pkTool as? PKEraserTool else {
            return XCTFail("expected an eraser")
        }
        XCTAssertEqual(eraser.width, 40, accuracy: 0.0001)
    }
}
