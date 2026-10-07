import XCTest

/// The editor's frame through real touches: the document tabs, Home, Read
/// Only, the sidebar and Add Page. Controls are found by the accessibility
/// labels the contract gives them.
final class EditorChromeUITests: XCTestCase {
    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    private func launch(_ arguments: [String]) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-surface", "paper"] + arguments
        app.launch()
        return app
    }

    private func waitFor(_ condition: @escaping () -> Bool, timeout: TimeInterval = 8) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            RunLoop.current.run(until: Date().addingTimeInterval(0.2))
        }
        return condition()
    }

    private func tabs(_ app: XCUIApplication) -> XCUIElementQuery {
        app.buttons.matching(identifier: "tab")
    }

    private func page(_ app: XCUIApplication) -> XCUIElement {
        let element = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", "drawing canvas")).firstMatch
        XCTAssertTrue(element.waitForExistence(timeout: 20), "the first page never appeared")
        return element
    }

    private func strokes(_ page: XCUIElement) -> String {
        ((page.value as? String) ?? "").split(separator: " ").first.map(String.init) ?? ""
    }

    func testSwitchingAndClosingTabs() {
        let app = launch(["-debugTabs", "3"])
        _ = page(app)
        XCTAssertTrue(waitFor { self.tabs(app).count == 3 }, "three documents, three tabs")
        let first = tabs(app).element(boundBy: 0)
        let second = tabs(app).element(boundBy: 1)
        XCTAssertEqual(first.value as? String, "selected")
        let secondTitle = second.label

        second.tap()
        XCTAssertTrue(waitFor { second.value as? String == "selected" }, "the tab did not switch")
        XCTAssertNotEqual(first.value as? String, "selected")
        XCTAssertTrue(page(app).exists, "the switched-to document has no canvas")

        app.buttons["Close \(secondTitle)"].tap()
        XCTAssertTrue(waitFor { self.tabs(app).count == 2 }, "the tab did not close")
        XCTAssertFalse(app.buttons["Close \(secondTitle)"].exists)
        // The tab that took its place is showing.
        XCTAssertTrue(waitFor { self.tabs(app).element(boundBy: 1).value as? String == "selected" })
    }

    /// On a store of its own, at a server nothing listens on: the shared one
    /// holds every document every UI test ever made, so its library was long,
    /// slow to draw and still scrolling back when the card was tapped. Here
    /// the library holds the two tabs' documents and nothing else.
    func testHomeKeepsTheTabsAndATabComesBack() {
        let app = launch(["-debugTabs", "2", "-server", "http://127.0.0.1:8796"])
        _ = page(app)
        XCTAssertTrue(waitFor { self.tabs(app).count == 2 })
        let title = tabs(app).element(boundBy: 0).label

        app.buttons["Home"].tap()
        XCTAssertTrue(
            app.buttons["New"].waitForExistence(timeout: 8), "Home is not the library")
        XCTAssertEqual(tabs(app).count, 0, "the tab strip belongs to the editor")
        XCTAssertFalse(app.buttons["toolbar.lasso"].exists)

        // The library opens the document again, in the tab it already has.
        let card = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "\(title),"))
            .firstMatch
        XCTAssertTrue(card.waitForExistence(timeout: 8), "no card for \(title) in the library")
        XCTAssertTrue(waitFor { card.isHittable }, "the card for \(title) never settled")
        card.tap()
        XCTAssertTrue(waitFor { self.tabs(app).count == 2 }, "reopening added a tab")
        XCTAssertEqual(tabs(app).element(boundBy: 0).value as? String, "selected")
        XCTAssertTrue(page(app).exists)
    }

    func testReadOnlyHidesTheToolbarAndRefusesInk() {
        let app = launch(["-debugNewNotebook", "a4", "-debugFingerInk", "-paper.tool", "pen"])
        let page = self.page(app)
        XCTAssertEqual(strokes(page), "strokes:0")
        let readOnly = app.buttons["Read Only"]
        XCTAssertTrue(readOnly.waitForExistence(timeout: 8))
        XCTAssertEqual(readOnly.value as? String, "off")

        readOnly.tap()
        XCTAssertTrue(waitFor { readOnly.value as? String == "on" })
        XCTAssertTrue(waitFor { !app.buttons["toolbar.writing"].exists }, "the toolbar stayed")
        XCTAssertFalse(app.descendants(matching: .any)["UndoRedo"].exists, "the undo pill stayed")
        XCTAssertFalse(app.descendants(matching: .any)["ToolMenu"].exists, "the tool menu stayed")
        page.coordinate(withNormalizedOffset: CGVector(dx: 0.2, dy: 0.3))
            .press(forDuration: 0.1, thenDragTo: page.coordinate(withNormalizedOffset: CGVector(dx: 0.7, dy: 0.35)))
        RunLoop.current.run(until: Date().addingTimeInterval(1))
        XCTAssertEqual(strokes(page), "strokes:0", "a read-only page took ink")
        // Share and the sidebar still work.
        XCTAssertTrue(app.buttons["Share"].isEnabled)
        XCTAssertTrue(app.buttons["Sidebar"].isEnabled)

        readOnly.tap()
        XCTAssertTrue(app.buttons["toolbar.writing"].waitForExistence(timeout: 5))
        page.coordinate(withNormalizedOffset: CGVector(dx: 0.2, dy: 0.3))
            .press(forDuration: 0.1, thenDragTo: page.coordinate(withNormalizedOffset: CGVector(dx: 0.7, dy: 0.35)))
        XCTAssertTrue(waitFor { self.strokes(page) == "strokes:1" }, "ink did not come back")
    }

    func testTheSidebarShowsPagesAndOutline() {
        let app = launch(["-debugNewNotebook", "a4", "-debugPages", "3"])
        _ = page(app)
        let sidebar = app.buttons["Sidebar"]
        XCTAssertTrue(sidebar.waitForExistence(timeout: 8))
        sidebar.tap()
        XCTAssertTrue(waitFor { sidebar.value as? String == "shown" })
        let pages = app.buttons["Pages"]
        let outline = app.buttons["Outline"]
        XCTAssertTrue(pages.waitForExistence(timeout: 5))
        XCTAssertTrue(outline.exists)
        XCTAssertTrue(app.buttons["Page 1"].exists)
        XCTAssertTrue(app.buttons["Page 3"].exists)
        XCTAssertEqual(app.buttons["Page 1"].value as? String, "current")

        app.buttons["Page 3"].tap()
        XCTAssertTrue(
            waitFor { app.buttons["Page 3"].value as? String == "current" },
            "tapping a thumbnail did not go to its page")

        outline.tap()
        XCTAssertTrue(app.staticTexts["No titled pages"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["Page 1"].exists)

        sidebar.tap()
        XCTAssertTrue(waitFor { !app.buttons["Outline"].exists }, "the sidebar did not close")
    }

    func testAddPageAddsAPageAfterTheOneShowing() {
        let app = launch(["-debugNewNotebook", "a4"])
        let first = page(app)
        XCTAssertTrue(first.label.contains("of 1"), first.label)
        let add = app.buttons["Add Page"]
        XCTAssertTrue(add.waitForExistence(timeout: 8))
        add.tap()
        let squared = app.buttons["Squared"]
        XCTAssertTrue(squared.waitForExistence(timeout: 5), "the Add menu has no templates")
        XCTAssertTrue(app.buttons["Before"].exists)
        XCTAssertTrue(app.buttons["Import PDF"].exists)
        squared.tap()
        let added = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", "of 2, drawing canvas")).firstMatch
        XCTAssertTrue(added.waitForExistence(timeout: 8), "no second page")
    }
}
