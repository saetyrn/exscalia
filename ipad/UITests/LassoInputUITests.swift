import XCTest

/// What only real touches can prove: where a drag on the page goes.
///
/// `simctl` cannot synthesise a drag at all and `XCUITest` can draw only a
/// straight line, so what a *loop* catches is exercised separately, by the
/// `-debugLassoLoop` launch argument, which drives the overlay through the very
/// entry points the recogniser calls.
final class LassoInputUITests: XCTestCase {
    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    private func launch(
        tool: String, drawWithFinger: Bool, extra: [String] = []
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
                // UserDefaults reads the argument domain, so the settings
                // screen's own keys are how the test picks a tool and an input
                // policy.
                "-paper.tool", tool,
                "-paper.drawWithFinger", drawWithFinger ? "YES" : "NO",
            ] + extra
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

    /// The bug as reported: a drag on the page in Lasso mode belongs to the
    /// lasso. It must neither scroll the notebook nor -- with a finger allowed
    /// to draw -- leave a stroke behind.
    func testLassoDragIsCapturedByTheLassoRatherThanTheCanvasOrTheScrollView() {
        let app = launch(tool: "lasso", drawWithFinger: true)
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

    /// With a drawing tool selected and the finger allowed to draw, the same
    /// drag does leave a stroke -- the control that shows the lasso assertion
    /// above is not simply measuring a page that never accepts anything.
    func testFingerDrawsWithAToolThatDraws() {
        let app = launch(tool: "pen", drawWithFinger: true)
        let page = self.page(app)
        XCTAssertTrue(strokes(page).hasPrefix("strokes:1"))

        drag(page, from: CGVector(dx: 0.3, dy: 0.7), to: CGVector(dx: 0.7, dy: 0.2))

        XCTAssertTrue(strokes(self.page(app)).hasPrefix("strokes:2"))
    }

    /// The other half of "Pencil is creation, fingers are navigation": with the
    /// finger reserved for navigation, a drag scrolls, so the fix has not
    /// nailed the scroll view shut.
    func testFingerDragStillScrollsWhenItIsNotAllowedToDraw() {
        let app = launch(tool: "pen", drawWithFinger: false)
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
        let app = launch(tool: "lasso", drawWithFinger: true, extra: ["-debugLassoLoop"])
        _ = page(app)
        XCTAssertTrue(
            app.buttons["Duplicate"].waitForExistence(timeout: 10),
            "the selection inspector never appeared")
        XCTAssertTrue(app.buttons["Delete"].exists)
    }
}
