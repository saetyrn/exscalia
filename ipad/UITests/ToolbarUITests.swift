import XCTest

/// The toolbar, the tool menu, Draw Shape and the finger gestures, through
/// real touches. Tools persist between launches on one simulator, so nothing
/// here assumes which writing tool or colour an earlier test left selected.
final class ToolbarUITests: XCTestCase {
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

    private func strokes(_ page: XCUIElement) -> String {
        let value = (page.value as? String) ?? ""
        return value.split(separator: " ").first.map(String.init) ?? ""
    }

    private func waitFor(_ condition: @escaping () -> Bool, timeout: TimeInterval = 5) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            RunLoop.current.run(until: Date().addingTimeInterval(0.2))
        }
        return condition()
    }

    /// The selected tool's menu, shown if an earlier test hid it.
    private func toolMenu(_ app: XCUIApplication, selected slot: String) -> XCUIElement {
        let menu = app.descendants(matching: .any)["ToolMenu"]
        if !menu.waitForExistence(timeout: 3) { app.buttons["toolbar.\(slot)"].tap() }
        XCTAssertTrue(menu.waitForExistence(timeout: 5), "the tool menu never appeared")
        return menu
    }

    private func middle(of element: XCUIElement) -> XCUICoordinate {
        element.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
    }

    func testTheToolbarNamesItsToolsAndMarksTheSelectedOne() {
        let app = launch(["-paper.tool", "lasso"])
        _ = page(app)
        let lasso = app.buttons["toolbar.lasso"]
        XCTAssertTrue(lasso.waitForExistence(timeout: 10))
        XCTAssertEqual(lasso.label, "Lasso")
        XCTAssertEqual(lasso.value as? String, "selected")
        XCTAssertTrue(
            ["Pen", "Pencil", "Highlighter", "Draw Shape"].contains(app.buttons["toolbar.writing"].label))
        for (slot, label) in [
            ("eraser", "Eraser"), ("text", "Text"), ("image", "Image"), ("shape", "Shapes"),
        ] {
            XCTAssertEqual(app.buttons["toolbar.\(slot)"].label, label)
        }
        XCTAssertFalse(app.descendants(matching: .any)["ToolMenu"].exists, "the lasso has no menu")

        app.buttons["toolbar.eraser"].tap()
        XCTAssertTrue(waitFor { app.buttons["toolbar.eraser"].value as? String == "selected" })
        XCTAssertNotEqual(lasso.value as? String, "selected")
        let menu = toolMenu(app, selected: "eraser")
        XCTAssertTrue(menu.buttons["Clear Page"].exists)
        XCTAssertTrue(menu.buttons["Partial"].exists)
        XCTAssertTrue(menu.buttons["Whole Stroke"].exists)

        // The selected tool again hides its menu, and once more brings it back.
        app.buttons["toolbar.eraser"].tap()
        XCTAssertTrue(waitFor { !menu.exists }, "tapping the selected tool did not hide its menu")
        app.buttons["toolbar.eraser"].tap()
        XCTAssertTrue(menu.waitForExistence(timeout: 5))
    }

    func testTheWritingMenuSwitchesTypesAndTheToolbarFollows() {
        let app = launch(["-paper.tool", "pen"])
        _ = page(app)
        let menu = toolMenu(app, selected: "writing")
        for id in ["pen", "pencil", "highlighter", "drawShape", "width.0", "width.1", "width.2", "color.0"] {
            XCTAssertTrue(menu.buttons["toolMenu.\(id)"].exists, "no \(id) in the tool menu")
        }
        menu.buttons["toolMenu.highlighter"].tap()
        let writing = app.buttons["toolbar.writing"]
        XCTAssertTrue(waitFor { writing.label == "Highlighter" })
        XCTAssertEqual(writing.value as? String, "selected")
        menu.buttons["toolMenu.pen"].tap()
        XCTAssertTrue(waitFor { writing.label == "Pen" })
    }

    /// A second tap on the selected colour opens its editor; a swatch there
    /// rewrites the slot.
    func testTheSelectedColourOpensItsEditor() {
        let app = launch(["-paper.tool", "pen"])
        _ = page(app)
        let menu = toolMenu(app, selected: "writing")
        let selected = menu.buttons.matching(
            NSPredicate(format: "identifier BEGINSWITH 'toolMenu.color.' AND value == 'selected'")
        ).firstMatch
        XCTAssertTrue(selected.waitForExistence(timeout: 5))
        let identifier = selected.identifier
        selected.tap()
        let editor = app.descendants(matching: .any)["ColorSlotEditor"]
        XCTAssertTrue(editor.waitForExistence(timeout: 5), "the colour editor never opened")
        XCTAssertTrue(app.descendants(matching: .any)["colorSlot.custom"].exists)
        app.buttons["colorSlot.Teal"].tap()
        XCTAssertTrue(waitFor { app.buttons[identifier].label == "Teal" })
    }

    /// `-debugDrawShape arrow` sends a rough arrow through the recogniser: two
    /// clean strokes. Two fingers double-tapping undo that back to the one
    /// rough stroke, three redo it, and the pill's Undo does the same.
    func testDrawShapeAndTheUndoGestures() {
        let app = launch(["-debugDrawShape", "arrow"])
        let page = self.page(app)
        XCTAssertTrue(waitFor({ self.strokes(page) == "strokes:2" }, timeout: 10), strokes(page))
        XCTAssertEqual(app.buttons["toolbar.writing"].label, "Draw Shape")

        page.tap(withNumberOfTaps: 2, numberOfTouches: 2)
        XCTAssertTrue(waitFor { self.strokes(page) == "strokes:1" }, "two fingers did not undo")
        page.tap(withNumberOfTaps: 2, numberOfTouches: 3)
        XCTAssertTrue(waitFor { self.strokes(page) == "strokes:2" }, "three fingers did not redo")

        let undo = app.buttons["Undo"]
        XCTAssertTrue(undo.isEnabled)
        undo.tap()
        XCTAssertTrue(waitFor { self.strokes(page) == "strokes:1" }, "the pill did not undo")
    }

    func testADoubleTapZoomsInAndBackOut() {
        let app = launch(["-paper.tool", "pen"])
        let page = self.page(app)
        let width = page.frame.width
        let spot = app.windows.firstMatch.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.55))
        spot.doubleTap()
        XCTAssertTrue(
            waitFor { self.page(app).frame.width > width * 1.8 }, "a double-tap did not zoom in")
        spot.doubleTap()
        XCTAssertTrue(
            waitFor { abs(self.page(app).frame.width - width) < 2 }, "a double-tap did not zoom back")
    }

    /// Text: tap the tool, tap the page, type; putting the box down goes back
    /// to the lasso. Text boxes are inside the page's accessibility container
    /// and not listed, so the tool is the evidence: had the first tap placed
    /// nothing, the second would place a box and the tool would stay on Text.
    func testTheTextToolPlacesABoxAndFallsBackToTheLasso() {
        let app = launch(["-paper.tool", "pen"])
        let page = self.page(app)
        let text = app.buttons["toolbar.text"]
        text.tap()
        XCTAssertTrue(waitFor { text.value as? String == "selected" })
        page.coordinate(withNormalizedOffset: CGVector(dx: 0.3, dy: 0.3)).tap()
        RunLoop.current.run(until: Date().addingTimeInterval(1))
        XCTAssertEqual(text.value as? String, "selected", "the tool let go while the box was open")
        page.coordinate(withNormalizedOffset: CGVector(dx: 0.6, dy: 0.8)).tap()
        XCTAssertTrue(
            waitFor { app.buttons["toolbar.lasso"].value as? String == "selected" },
            "no box was placed, or putting it down did not return to the lasso")
    }

    /// Dragging the grip snaps the menu to the nearest side, where it stays.
    func testTheToolMenuSnapsToTheSideItIsDraggedTo() {
        let app = launch(["-paper.tool", "pen", "-debugToolDock", "top"])
        _ = page(app)
        let menu = toolMenu(app, selected: "writing")
        let window = app.windows.firstMatch.frame
        XCTAssertGreaterThan(menu.frame.width, menu.frame.height, "a top menu is a row")

        let grip = app.descendants(matching: .any)["Move tool menu"]
        XCTAssertTrue(grip.exists)
        let right = app.windows.firstMatch.coordinate(withNormalizedOffset: CGVector(dx: 0.97, dy: 0.5))
        middle(of: grip).press(forDuration: 0.3, thenDragTo: right)
        XCTAssertTrue(
            waitFor { menu.frame.height > menu.frame.width && menu.frame.maxX > window.maxX - 40 },
            "the menu did not dock on the right")

        let top = app.windows.firstMatch.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.12))
        middle(of: app.descendants(matching: .any)["Move tool menu"])
            .press(forDuration: 0.3, thenDragTo: top)
        XCTAssertTrue(
            waitFor { menu.frame.width > menu.frame.height }, "the menu did not go back to the top")
    }
}
