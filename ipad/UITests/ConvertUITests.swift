import XCTest

/// Convert to Whiteboard through the menus: asked once, the tab stays open
/// and comes back as a whiteboard, and the library row reads "Whiteboard".
///
/// Every launch points at a server nothing listens on, which gives the tests
/// a store of their own and keeps them offline; the sync that runs first
/// gives up at once.
final class ConvertUITests: XCTestCase {
    private let server = "http://127.0.0.1:8799"

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    private func launch(_ arguments: [String]) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-surface", "paper", "-server", server] + arguments
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

    /// A row reads "name, date, kind[, Favorite]".
    private func row(_ app: XCUIApplication, _ name: String) -> XCUIElement {
        app.buttons.matching(identifier: "libraryItem")
            .matching(NSPredicate(format: "label BEGINSWITH %@", "\(name),")).firstMatch
    }

    private func confirm(_ app: XCUIApplication) {
        let alert = app.alerts["Convert to Whiteboard?"]
        XCTAssertTrue(alert.waitForExistence(timeout: 8), "nothing asked before converting")
        XCTAssertTrue(alert.buttons["Cancel"].exists)
        alert.buttons["Convert"].tap()
    }

    func testConvertingFromTheTabMenuKeepsTheTabAndTheLibraryRowReadsWhiteboard() {
        let app = launch(["-debugNewNotebook", "-debugStroke"])
        let tab = app.buttons.matching(identifier: "tab").firstMatch
        XCTAssertTrue(tab.waitForExistence(timeout: 30))
        XCTAssertTrue(app.buttons["Add Page"].waitForExistence(timeout: 20), "not a notebook to start")
        let title = tab.label

        tab.tap()
        let entry = app.buttons["Convert to Whiteboard"]
        XCTAssertTrue(entry.waitForExistence(timeout: 5), "no entry in the tab menu")
        XCTAssertTrue(waitFor { entry.isEnabled }, "the entry stayed off")
        entry.tap()
        confirm(app)

        // The same tab, still open and showing, now on a whiteboard.
        XCTAssertTrue(app.buttons["Add Board"].waitForExistence(timeout: 30), "not a whiteboard")
        let zoom = app.buttons["board.zoom"]
        XCTAssertTrue(zoom.waitForExistence(timeout: 15), "no board editor")
        XCTAssertEqual(zoom.label, "100%", "the converted board opens at 100 %")
        let tabs = app.buttons.matching(identifier: "tab")
        XCTAssertEqual(tabs.count, 1)
        XCTAssertEqual(tabs.firstMatch.label, title)
        XCTAssertEqual(tabs.firstMatch.value as? String, "selected")

        app.buttons["Home"].tap()
        let converted = row(app, title)
        XCTAssertTrue(converted.waitForExistence(timeout: 10))
        XCTAssertTrue(
            waitFor { converted.label.contains("Whiteboard") }, "the row still reads \(converted.label)")
    }

    func testConvertingFromTheLibraryMenuChangesTheRowsKind() {
        let app = launch(["-debugNewNotebook"])
        let tab = app.buttons.matching(identifier: "tab").firstMatch
        XCTAssertTrue(tab.waitForExistence(timeout: 30))
        let title = tab.label
        app.buttons["Home"].tap()
        let notebook = row(app, title)
        XCTAssertTrue(notebook.waitForExistence(timeout: 10))
        XCTAssertTrue(notebook.label.contains("Notebook"))

        app.buttons["itemMenu:\(title)"].tap()
        let entry = app.buttons["Convert to Whiteboard"]
        XCTAssertTrue(entry.waitForExistence(timeout: 5), "no entry in the item menu")
        XCTAssertTrue(waitFor { entry.isEnabled }, "the entry stayed off")
        entry.tap()
        confirm(app)

        XCTAssertTrue(
            waitFor({ self.row(app, title).label.contains("Whiteboard") }, timeout: 30),
            "the row still reads \(row(app, title).label)")
        // A whiteboard's menu has no entry.
        app.buttons["itemMenu:\(title)"].tap()
        XCTAssertTrue(app.buttons["Move"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["Convert to Whiteboard"].exists)
    }

    func testReadOnlyTurnsTheTabMenuEntryOff() {
        let app = launch(["-debugNewNotebook", "-debugReadOnly"])
        let tab = app.buttons.matching(identifier: "tab").firstMatch
        XCTAssertTrue(tab.waitForExistence(timeout: 30))
        tab.tap()
        let entry = app.buttons["Convert to Whiteboard"]
        XCTAssertTrue(entry.waitForExistence(timeout: 5))
        RunLoop.current.run(until: Date().addingTimeInterval(1))
        XCTAssertFalse(entry.isEnabled, "Read Only still offers to rewrite the document")
    }
}
