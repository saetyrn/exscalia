import XCTest

/// Selection first, with real finger taps: a tap selects ink, a text box or a
/// shape and the object menu appears at once, a finger drag on the selection
/// moves it, and a tap on bare paper puts it down. XCUITest has no Pencil, so
/// every touch here is a finger -- which is what selects.
final class SelectionUITests: XCTestCase {
    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    private func launch(_ extra: [String]) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-surface", "paper", "-debugNewNotebook", "a4"] + extra
        app.launch()
        return app
    }

    private func page(_ app: XCUIApplication) -> XCUIElement {
        let element = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", "drawing canvas")).firstMatch
        XCTAssertTrue(element.waitForExistence(timeout: 20), "the first page never appeared")
        return element
    }

    private func field(_ name: String, _ page: XCUIElement) -> String? {
        let value = (page.value as? String) ?? ""
        return value.split(separator: " ").first(where: { $0.hasPrefix("\(name):") })
            .map { String($0.dropFirst(name.count + 1)) }
    }

    private func rect(_ name: String, _ page: XCUIElement) -> CGRect? {
        let numbers = field(name, page)?.split(separator: ",").compactMap { Double($0) } ?? []
        guard numbers.count == 4 else { return nil }
        return CGRect(x: numbers[0], y: numbers[1], width: numbers[2], height: numbers[3])
    }

    private func extent(_ page: XCUIElement) -> CGSize {
        let numbers = field("extent", page)?.split(separator: "x").compactMap { Double($0) } ?? []
        guard numbers.count == 2 else { return .zero }
        return CGSize(width: numbers[0], height: numbers[1])
    }

    /// A point of the page, in page points, as a coordinate XCUITest can press.
    private func coordinate(_ page: XCUIElement, at point: CGPoint) -> XCUICoordinate {
        let size = extent(page)
        let zoom = page.frame.width / max(size.width, 1)
        return page.coordinate(withNormalizedOffset: .zero)
            .withOffset(CGVector(dx: point.x * zoom, dy: point.y * zoom))
    }

    private func waitFor(_ condition: @escaping () -> Bool, timeout: TimeInterval = 8) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            RunLoop.current.run(until: Date().addingTimeInterval(0.2))
        }
        return condition()
    }

    /// A point on the `-debugStroke` wave: (90 + t, 240 + 40 sin(t / 40)).
    private static let onTheInk = CGPoint(x: 170, y: 240 + 40 * sin(2.0))
    private static let barePaper = CGPoint(x: 420, y: 760)

    /// The pen is selected; a finger tap on the ink selects the word, the
    /// toolbar goes to the Lasso and the ink's menu is there. A finger drag
    /// on it moves the ink; a tap on bare paper puts it down.
    func testATapSelectsInkThatAFingerThenMovesAndATapPutsDown() {
        let app = launch(["-debugStroke", "-paper.tool", "pen"])
        let page = self.page(app)
        XCTAssertTrue(waitFor { self.rect("ink", page) != nil }, "the page reported no ink")
        guard let before = rect("ink", page) else { return }

        coordinate(page, at: Self.onTheInk).tap()
        XCTAssertTrue(app.buttons["Duplicate"].waitForExistence(timeout: 5), "no menu appeared")
        XCTAssertTrue(app.buttons["Convert to Text"].exists, "not the ink's menu")
        XCTAssertTrue(waitFor { app.buttons["toolbar.lasso"].value as? String == "selected" })
        XCTAssertEqual(field("selected", self.page(app)), "ink")

        let frame = page.frame
        coordinate(page, at: Self.onTheInk).press(
            forDuration: 0.2,
            thenDragTo: coordinate(
                page, at: CGPoint(x: Self.onTheInk.x + 60, y: Self.onTheInk.y + 120)))
        let after = self.page(app)
        XCTAssertEqual(frame.origin.y, after.frame.origin.y, accuracy: 2, "the drag scrolled")
        guard let moved = rect("ink", after) else { return XCTFail("the page reported no ink") }
        XCTAssertEqual(moved.minX - before.minX, 60, accuracy: 4, "the ink did not follow the finger")
        XCTAssertEqual(moved.minY - before.minY, 120, accuracy: 4, "the ink did not follow the finger")

        coordinate(page, at: Self.barePaper).tap()
        XCTAssertTrue(waitFor { !app.buttons["Duplicate"].exists }, "the tap did not put it down")
        XCTAssertEqual(field("selected", self.page(app)), "none")
    }

    /// A text box: selected by a tap, carried by a finger, put down by a tap.
    func testATapSelectsATextBoxThatAFingerThenMoves() {
        let app = launch(["-debugTextObject", "-paper.tool", "pen"])
        let page = self.page(app)
        XCTAssertTrue(waitFor { self.rect("object", page) != nil }, "the page reported no object")
        guard let box = rect("object", page) else { return }
        let middle = CGPoint(x: box.midX, y: box.midY)

        coordinate(page, at: middle).tap()
        XCTAssertTrue(app.buttons["Edit"].waitForExistence(timeout: 5), "no text menu appeared")
        XCTAssertEqual(field("selected", self.page(app)), "text")

        coordinate(page, at: middle).press(
            forDuration: 0.2,
            thenDragTo: coordinate(page, at: CGPoint(x: middle.x - 80, y: middle.y + 200)))
        guard let moved = rect("object", self.page(app)) else { return XCTFail("no object") }
        XCTAssertEqual(moved.minX - box.minX, -80, accuracy: 4, "the box did not follow the finger")
        XCTAssertEqual(moved.minY - box.minY, 200, accuracy: 4, "the box did not follow the finger")
        XCTAssertTrue(app.buttons["Edit"].exists, "the drag put the box down")

        coordinate(page, at: Self.barePaper).tap()
        XCTAssertTrue(waitFor { !app.buttons["Edit"].exists })
        XCTAssertEqual(field("selected", self.page(app)), "none")
    }

    /// The Shapes tool: a tap places a shape, which is selected with its menu,
    /// and the toolbar falls back to the Lasso.
    func testTheShapesToolPlacesAShapeWithATap() {
        let app = launch(["-paper.tool", "pen"])
        let page = self.page(app)
        let shapes = app.buttons["toolbar.shape"]
        XCTAssertTrue(shapes.waitForExistence(timeout: 10))
        shapes.tap()
        XCTAssertTrue(waitFor { shapes.value as? String == "selected" })

        coordinate(page, at: CGPoint(x: 300, y: 500)).tap()
        XCTAssertTrue(app.buttons["Stroke Color"].waitForExistence(timeout: 5), "no shape menu")
        XCTAssertTrue(waitFor { app.buttons["toolbar.lasso"].value as? String == "selected" })
        XCTAssertEqual(field("objects", self.page(app)), "1")
        guard let shape = rect("object", self.page(app)) else { return XCTFail("no object") }
        XCTAssertEqual(shape.width, 160, accuracy: 1)
        XCTAssertEqual(shape.height, 120, accuracy: 1)
        XCTAssertEqual(shape.midX, 300, accuracy: 1)
        XCTAssertEqual(shape.midY, 500, accuracy: 1)
    }

    /// A drag with the Shapes tool draws the shape's box. That is the
    /// Pencil's job; `-debugFingerInk` lets a finger stand in for it.
    func testADragWithTheShapesToolDrawsTheBox() {
        let app = launch(["-debugFingerInk", "-paper.tool", "shape"])
        let page = self.page(app)
        coordinate(page, at: CGPoint(x: 150, y: 400)).press(
            forDuration: 0.1, thenDragTo: coordinate(page, at: CGPoint(x: 350, y: 550)))
        XCTAssertTrue(app.buttons["Stroke Color"].waitForExistence(timeout: 5), "no shape menu")
        guard let shape = rect("object", self.page(app)) else { return XCTFail("no object") }
        XCTAssertEqual(shape.minX, 150, accuracy: 6)
        XCTAssertEqual(shape.minY, 400, accuracy: 6)
        XCTAssertEqual(shape.width, 200, accuracy: 8)
        XCTAssertEqual(shape.height, 150, accuracy: 8)
        XCTAssertTrue(waitFor { app.buttons["toolbar.lasso"].value as? String == "selected" })
    }
}
