import XCTest

/// The whiteboard with real touches: the zoom pill and its menu, the minimap
/// button, a finger that pans and never draws, a pinch that starts on the
/// board, and a finger tap that selects. What the Pencil does needs an iPad.
final class BoardUITests: XCTestCase {
    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    private struct Reading {
        var zoom: Double
        var centre: CGPoint
        var visible: CGRect
        var strokes: Int
        var selected: String
    }

    private func launch(_ extra: [String]) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-surface", "paper", "-debugNewWhiteboard"] + extra
        app.launch()
        return app
    }

    /// The board's canvas element: the board on screen, inset clear of the
    /// floating pills, so a gesture XCUITest starts near its corners lands
    /// on the board.
    private func board(_ app: XCUIApplication) -> XCUIElement {
        let element = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", "drawing canvas")).firstMatch
        XCTAssertTrue(element.waitForExistence(timeout: 20), "the board never appeared")
        return element
    }

    /// The element's DEBUG value: "strokes:N ... selected:K ... zoom:Z
    /// centre:X,Y visible:x,y,w,h", in board points.
    private func read(_ board: XCUIElement) -> Reading? {
        let fields = ((board.value as? String) ?? "").split(separator: " ")
        func field(_ name: String) -> Substring? {
            fields.first { $0.hasPrefix(name + ":") }?.dropFirst(name.count + 1)
        }
        func numbers(_ name: String) -> [Double] {
            field(name)?.split(separator: ",").compactMap { Double($0) } ?? []
        }
        let centre = numbers("centre")
        let visible = numbers("visible")
        guard let zoom = field("zoom").flatMap({ Double($0) }), centre.count == 2,
            visible.count == 4, let strokes = field("strokes").flatMap({ Int($0) })
        else { return nil }
        return Reading(
            zoom: zoom, centre: CGPoint(x: centre[0], y: centre[1]),
            visible: CGRect(x: visible[0], y: visible[1], width: visible[2], height: visible[3]),
            strokes: strokes, selected: field("selected").map(String.init) ?? "none")
    }

    /// Waits for a reading that passes `condition` and holds still.
    private func reading(
        _ board: XCUIElement, timeout: TimeInterval = 10, where condition: (Reading) -> Bool = { _ in true }
    ) -> Reading? {
        let deadline = Date().addingTimeInterval(timeout)
        var last: Reading?
        while Date() < deadline {
            if let value = read(board), condition(value) {
                if let previous = last, previous.zoom == value.zoom, previous.centre == value.centre {
                    return value
                }
                last = value
            } else {
                last = nil
            }
            Thread.sleep(forTimeInterval: 0.3)
        }
        return last
    }

    /// A board point as a coordinate XCUITest can press.
    private func coordinate(_ board: XCUIElement, at point: CGPoint, in reading: Reading) -> XCUICoordinate {
        board.coordinate(withNormalizedOffset: .zero).withOffset(
            CGVector(
                dx: (point.x - reading.visible.minX) * reading.zoom,
                dy: (point.y - reading.visible.minY) * reading.zoom))
    }

    private func waitFor(_ condition: () -> Bool, timeout: TimeInterval = 8) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            RunLoop.current.run(until: Date().addingTimeInterval(0.2))
        }
        return condition()
    }

    func testThePillReadsTheZoomAndItsMenuSetsIt() throws {
        let app = launch([])
        let board = self.board(app)
        _ = try XCTUnwrap(reading(board) { abs($0.zoom - 1) < 0.001 })
        let pill = app.buttons["board.zoom"]
        XCTAssertTrue(pill.waitForExistence(timeout: 5))
        XCTAssertEqual(pill.label, "100%")
        pill.tap()
        let half = app.buttons["50%"]
        XCTAssertTrue(half.waitForExistence(timeout: 5), "the pill's menu did not open")
        XCTAssertTrue(app.buttons["Fit Content"].exists)
        half.tap()
        XCTAssertTrue(waitFor { pill.label == "50%" }, "the pill reads \(pill.label)")
        let after = try XCTUnwrap(reading(board) { abs($0.zoom - 0.5) < 0.001 })
        XCTAssertEqual(after.zoom, 0.5, accuracy: 0.001)
    }

    func testTheMinimapButtonShowsAndHidesTheMinimap() {
        let app = launch(["-paper.board.minimapVisible", "YES"])
        _ = board(app)
        let button = app.buttons["board.minimap"]
        XCTAssertTrue(button.waitForExistence(timeout: 5))
        XCTAssertEqual(button.label, "Minimap")
        XCTAssertEqual(button.value as? String, "shown")
        let minimap = app.descendants(matching: .any)["board.minimapView"]
        XCTAssertTrue(minimap.exists, "no minimap")
        button.tap()
        XCTAssertTrue(waitFor { button.value as? String == "hidden" })
        XCTAssertTrue(waitFor { !minimap.exists }, "the minimap stayed")
        button.tap()
        XCTAssertTrue(waitFor { button.value as? String == "shown" })
        XCTAssertTrue(waitFor { minimap.exists }, "the minimap did not come back")
    }

    func testAFingerDragPansTheBoardAndDrawsNothing() throws {
        let app = launch(["-paper.tool", "pen"])
        let board = self.board(app)
        let before = try XCTUnwrap(reading(board) { abs($0.zoom - 1) < 0.001 })
        XCTAssertEqual(before.strokes, 0)
        let start = board.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        // Slow and held at the end, so there is no fling.
        start.press(
            forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: -300, dy: -200)),
            withVelocity: .slow, thenHoldForDuration: 0.4)
        let after = try XCTUnwrap(reading(board) { $0.centre != before.centre })
        XCTAssertEqual(after.strokes, 0, "a finger drew on the board")
        XCTAssertEqual(after.zoom, 1, accuracy: 0.001)
        // Up and left moves the view down and right, a point a point at 100%.
        XCTAssertEqual(after.centre.x - before.centre.x, 300, accuracy: 30)
        XCTAssertEqual(after.centre.y - before.centre.y, 200, accuracy: 30)
    }

    func testAPinchThatStartsOnTheBoardZooms() throws {
        let app = launch(["-paper.tool", "pen"])
        let board = self.board(app)
        _ = try XCTUnwrap(reading(board) { abs($0.zoom - 1) < 0.001 })
        // The element stops short of the pills, so both fingers land on the
        // board.
        board.pinch(withScale: 0.4, velocity: -1)
        let after = try XCTUnwrap(
            reading(board) { $0.zoom < 0.9 }, "after the pinch: \(board.value ?? "none")")
        XCTAssertLessThan(after.zoom, 0.9)
        XCTAssertEqual(after.strokes, 0, "the pinch drew")
        XCTAssertEqual(app.buttons["board.zoom"].label, "\(Int((after.zoom * 100).rounded()))%")
    }

    func testAFingerTapOnInkSelectsIt() throws {
        let app = launch(["-debugBoardStrokes", "-paper.tool", "pen"])
        let board = self.board(app)
        let state = try XCTUnwrap(reading(board) { $0.strokes > 0 })
        // `-debugBoardStrokes` runs its first stroke level through the centre.
        let centre = CGPoint(x: 8_192 + 40, y: 8_192)
        coordinate(board, at: centre, in: state).tap()
        XCTAssertTrue(app.buttons["Duplicate"].waitForExistence(timeout: 5), "no menu appeared")
        XCTAssertTrue(waitFor { self.read(board)?.selected == "ink" })
        XCTAssertTrue(waitFor { app.buttons["toolbar.lasso"].value as? String == "selected" })

        coordinate(board, at: CGPoint(x: 8_192, y: 8_192 - 300), in: state).tap()
        XCTAssertTrue(waitFor { !app.buttons["Duplicate"].exists }, "the tap did not put it down")
        XCTAssertEqual(read(board)?.selected, "none")
    }
}
