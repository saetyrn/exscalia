import XCTest

/// What only real touches can prove: where a drag on the page goes.
///
/// `simctl` cannot synthesise a drag at all and `XCUITest` can draw only a
/// straight line, so what a *loop* catches is exercised separately, by the
/// `-debugLassoLoop` launch argument, which drives the overlay through the very
/// entry points the recogniser calls. A Pencil drag of the selection, and the
/// system cancelling one, are `-debugLassoDrag` for the same reason. A finger
/// XCUITest can do for real.
final class LassoInputUITests: XCTestCase {
    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    /// `fingerInk` passes `-debugFingerInk`: XCUITest has no Pencil, so a
    /// finger stands in for it.
    private func launch(
        tool: String, fingerInk: Bool, extra: [String] = []
    ) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments =
            [
                "-surface", "paper",
                // A fixed sheet, so the page geometry these drags rely on does
                // not move under them the way an infinite page's does.
                "-debugNewNotebook", "a4",
                "-debugStroke",
                "-debugPages", "4",
                // UserDefaults reads the argument domain, so the tool's own key
                // is how the test picks a tool.
                "-paper.tool", tool,
            ] + (fingerInk ? ["-debugFingerInk"] : []) + extra
        app.launch()
        return app
    }

    /// The canvas itself, not the header's page counter: only the canvas moves
    /// when the notebook scrolls.
    private func page(_ app: XCUIApplication) -> XCUIElement {
        let element = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", "drawing canvas")).firstMatch
        XCTAssertTrue(element.waitForExistence(timeout: 20), "the first page never appeared")
        return element
    }

    private func strokes(_ page: XCUIElement) -> String {
        (page.value as? String) ?? ""
    }

    private func drag(_ page: XCUIElement, from: CGVector, to: CGVector) {
        page.coordinate(withNormalizedOffset: from)
            .press(
                forDuration: 0.1,
                thenDragTo: page.coordinate(withNormalizedOffset: to))
    }

    /// "ink:x,y,w,h" from the canvas's DEBUG value, in page points.
    private func ink(_ page: XCUIElement) -> CGRect? {
        let value = (page.value as? String) ?? ""
        guard let field = value.split(separator: " ").first(where: { $0.hasPrefix("ink:") })
        else { return nil }
        let numbers = field.dropFirst("ink:".count).split(separator: ",").compactMap { Double($0) }
        guard numbers.count == 4 else { return nil }
        return CGRect(x: numbers[0], y: numbers[1], width: numbers[2], height: numbers[3])
    }

    private func extent(_ page: XCUIElement) -> CGSize {
        let value = (page.value as? String) ?? ""
        guard let field = value.split(separator: " ").first(where: { $0.hasPrefix("extent:") })
        else { return .zero }
        let numbers = field.dropFirst("extent:".count).split(separator: "x").compactMap {
            Double($0)
        }
        guard numbers.count == 2 else { return .zero }
        return CGSize(width: numbers[0], height: numbers[1])
    }

    /// A point of the page, in page points, as a coordinate XCUITest can press.
    private func coordinate(_ page: XCUIElement, at point: CGPoint) -> XCUICoordinate {
        let size = extent(page)
        return page.coordinate(
            withNormalizedOffset: CGVector(
                dx: point.x / max(size.width, 1), dy: point.y / max(size.height, 1)))
    }

    /// The bug as reported: a drag on the page in Lasso mode belongs to the
    /// lasso. It must neither scroll the notebook nor -- with a finger standing
    /// in for the Pencil -- leave a stroke behind.
    func testLassoDragIsCapturedByTheLassoRatherThanTheCanvasOrTheScrollView() {
        let app = launch(tool: "lasso", fingerInk: true)
        let page = self.page(app)
        let before = page.frame
        XCTAssertTrue(strokes(page).hasPrefix("strokes:1"))

        drag(page, from: CGVector(dx: 0.3, dy: 0.7), to: CGVector(dx: 0.7, dy: 0.2))

        let after = self.page(app)
        XCTAssertEqual(
            before.origin.y, after.frame.origin.y, accuracy: 2,
            "a drag in Lasso mode scrolled the notebook")
        XCTAssertTrue(
            strokes(after).hasPrefix("strokes:1"),
            "a drag in Lasso mode drew ink instead of a loop")
    }

    /// With a drawing tool selected and a finger standing in for the Pencil,
    /// the same drag does leave a stroke -- the control that shows the lasso
    /// assertion above is not simply measuring a page that never accepts
    /// anything.
    func testFingerDrawsWithAToolThatDraws() {
        let app = launch(tool: "pen", fingerInk: true)
        let page = self.page(app)
        XCTAssertTrue(strokes(page).hasPrefix("strokes:1"))

        drag(page, from: CGVector(dx: 0.3, dy: 0.7), to: CGVector(dx: 0.7, dy: 0.2))

        XCTAssertTrue(strokes(self.page(app)).hasPrefix("strokes:2"))
    }

    /// The other half of "Pencil is creation, fingers are navigation": a
    /// finger drag scrolls, so the fix has not nailed the scroll view shut.
    func testFingerDragStillScrollsWhenItIsNotAllowedToDraw() {
        let app = launch(tool: "pen", fingerInk: false)
        let page = self.page(app)
        let before = page.frame

        drag(page, from: CGVector(dx: 0.5, dy: 0.8), to: CGVector(dx: 0.5, dy: 0.1))

        XCTAssertLessThan(
            self.page(app).frame.origin.y, before.origin.y - 20,
            "a finger drag with the pen selected did not scroll the notebook")
    }

    /// And what the loop does once it has been captured: the selection
    /// inspector appears over the strokes it caught.
    func testCapturedLoopSelectsTheStrokeAndShowsTheInspector() {
        let app = launch(tool: "lasso", fingerInk: true, extra: ["-debugLassoLoop"])
        _ = page(app)
        XCTAssertTrue(
            app.buttons["Duplicate"].waitForExistence(timeout: 10),
            "the selection inspector never appeared")
        XCTAssertTrue(app.buttons["Delete"].exists)
    }

    // MARK: - Moving a selection

    /// The bug as the owner reported it: "lasso doesn't actually move
    /// anything". A finger is navigation, and a finger on the selection went
    /// to the scroll view, so the notebook scrolled and the ink stayed. A
    /// finger on the selection carries it; the notebook stays put.
    func testAFingerDragOnTheSelectionMovesTheInk() {
        let app = launch(tool: "lasso", fingerInk: false, extra: ["-debugLassoLoop"])
        let page = self.page(app)
        XCTAssertTrue(app.buttons["Duplicate"].waitForExistence(timeout: 10))
        guard let before = ink(page) else { return XCTFail("the page reported no ink") }
        let frame = page.frame

        let start = CGPoint(x: before.midX, y: before.midY)
        coordinate(page, at: start).press(
            forDuration: 0.2,
            thenDragTo: coordinate(page, at: CGPoint(x: start.x + 80, y: start.y + 160)))

        let after = self.page(app)
        XCTAssertEqual(frame.origin.y, after.frame.origin.y, accuracy: 2, "the drag scrolled the notebook")
        guard let moved = ink(after) else { return XCTFail("the page reported no ink") }
        XCTAssertEqual(moved.minX - before.minX, 80, accuracy: 4, "the ink did not follow the finger")
        XCTAssertEqual(moved.minY - before.minY, 160, accuracy: 4, "the ink did not follow the finger")
        XCTAssertTrue(app.buttons["Duplicate"].exists, "the selection was dropped with the ink")
    }

    /// Fingers scroll when they start anywhere but on the selection.
    func testAFingerDragAwayFromTheSelectionStillScrolls() {
        let app = launch(tool: "lasso", fingerInk: false, extra: ["-debugLassoLoop"])
        let page = self.page(app)
        XCTAssertTrue(app.buttons["Duplicate"].waitForExistence(timeout: 10))
        guard let before = ink(page) else { return XCTFail("the page reported no ink") }
        let frame = page.frame

        let size = extent(page)
        coordinate(page, at: CGPoint(x: size.width / 2, y: size.height * 0.75)).press(
            forDuration: 0.1,
            thenDragTo: coordinate(page, at: CGPoint(x: size.width / 2, y: size.height * 0.15)))

        let after = self.page(app)
        XCTAssertLessThan(
            after.frame.origin.y, frame.origin.y - 20, "a finger off the selection did not scroll")
        XCTAssertEqual(ink(after), before, "scrolling moved the ink")
    }

    /// Two fingers still zoom with a selection on the page.
    func testPinchStillZoomsWithASelection() {
        let app = launch(tool: "lasso", fingerInk: false, extra: ["-debugLassoLoop"])
        let page = self.page(app)
        XCTAssertTrue(app.buttons["Duplicate"].waitForExistence(timeout: 10))
        let width = page.frame.width

        // A gentler synthesised pinch does not zoom this notebook with the pen
        // selected either; this one does.
        page.pinch(withScale: 2.5, velocity: 4)

        XCTAssertGreaterThan(self.page(app).frame.width, width + 20, "the pinch did not zoom")
    }

    /// A Pencil drag, through the entry points the recogniser uses: the ink
    /// lands where it was dropped.
    func testADraggedSelectionLandsWhereItWasDropped() {
        let app = launch(tool: "lasso", fingerInk: false, extra: ["-debugLassoDrag"])
        let page = self.page(app)
        XCTAssertTrue(app.buttons["Duplicate"].waitForExistence(timeout: 10))
        guard let moved = ink(page) else { return XCTFail("the page reported no ink") }
        XCTAssertEqual(moved.minX, Self.dropPoint.x, accuracy: 2)
        XCTAssertEqual(moved.minY, Self.dropPoint.y, accuracy: 2)
    }

    /// The same, then picked up again and carried until the system cancels
    /// the touch: the ink is where it lay before that drag, and the
    /// inspector -- which follows the outline -- sits just above it rather
    /// than where the cancelled drag had carried it.
    func testACancelledDragLeavesTheInkWhereItLay() {
        let app = launch(tool: "lasso", fingerInk: false, extra: ["-debugLassoDrag", "cancel"])
        let page = self.page(app)
        let duplicate = app.buttons["Duplicate"]
        XCTAssertTrue(duplicate.waitForExistence(timeout: 10))
        guard let resting = ink(page) else { return XCTFail("the page reported no ink") }
        XCTAssertEqual(resting.minX, Self.dropPoint.x, accuracy: 2)
        XCTAssertEqual(resting.minY, Self.dropPoint.y, accuracy: 2)

        let zoom = page.frame.width / max(extent(page).width, 1)
        let inkTop = page.frame.minY + resting.minY * zoom
        XCTAssertLessThanOrEqual(duplicate.frame.maxY, inkTop, "the outline stayed where the drag left it")
        XCTAssertGreaterThan(duplicate.frame.minY, inkTop - 120, "the outline stayed where the drag left it")
    }

    /// `NotebookViewController.debugLassoDropPoint`.
    private static let dropPoint = CGPoint(x: 200, y: 450)
}
