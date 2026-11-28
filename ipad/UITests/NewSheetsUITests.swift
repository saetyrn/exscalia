import XCTest

/// The New Notebook, New Whiteboard and New Folder sheets: focus starts in
/// the title, Return creates, the pickers hand back what was tapped, and the
/// emoji field refuses text. `-debugNewSheet` opens a sheet at launch and
/// shows what it handed back in a line at the bottom of the screen.
///
/// Escape is not tested here: on a simulator with no hardware keyboard
/// connected, `typeKey` reaches no SwiftUI keyboard shortcut at all (the
/// library's long-standing Cmd-N does not fire either), so Cancel on Escape
/// is checked on the iPad.
final class NewSheetsUITests: XCTestCase {
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

    private func result(_ app: XCUIApplication) -> String {
        let line = app.staticTexts["debugNewSheet.result"]
        XCTAssertTrue(line.waitForExistence(timeout: 10), "the sheet handed nothing back")
        return line.label
    }

    private func hasFocus(_ element: XCUIElement) -> Bool {
        (element.value(forKey: "hasKeyboardFocus") as? Bool) ?? false
    }

    func testNotebookTitleHasFocusAndReturnCreates() {
        let app = launch(["-debugNewSheet", "notebook", "-paper.newNotebook.options", "{}"])
        let title = app.textFields["Title"]
        XCTAssertTrue(title.waitForExistence(timeout: 20), "the notebook sheet never appeared")
        XCTAssertTrue(app.navigationBars["New Notebook"].exists)
        let focused = NSPredicate { _, _ in self.hasFocus(title) }
        wait(for: [expectation(for: focused, evaluatedWith: title)], timeout: 5)
        app.typeText("Plans\n")
        let line = result(app)
        XCTAssertTrue(line.hasPrefix("notebook created: title=Plans "), line)
        XCTAssertTrue(line.contains(#""template":"ruled""#), line)
        XCTAssertTrue(line.contains(#""size":"iPad""#), line)
        XCTAssertFalse(app.navigationBars["New Notebook"].exists)
    }

    func testWhiteboardReturnCreatesASquareGridBoard() {
        let app = launch([
            "-debugNewSheet", "whiteboard", "-paper.newWhiteboard.options", "{}",
        ])
        let title = app.textFields["Title"]
        XCTAssertTrue(title.waitForExistence(timeout: 20), "the whiteboard sheet never appeared")
        XCTAssertTrue(app.buttons["Square Grid"].exists)
        XCTAssertTrue(app.buttons["Dot Grid"].exists)
        XCTAssertTrue(app.buttons["Blank"].exists)
        let focused = NSPredicate { _, _ in self.hasFocus(title) }
        wait(for: [expectation(for: focused, evaluatedWith: title)], timeout: 5)
        app.typeText("Accounting\n")
        let line = result(app)
        XCTAssertTrue(line.hasPrefix("whiteboard created: title=Accounting "), line)
        XCTAssertTrue(line.contains(#""template":"grid""#), line)
        XCTAssertTrue(line.contains(#""paperColor":"cream""#), line)
        XCTAssertTrue(line.contains(#""size":"board""#), line)
    }

    func testFolderTakesAColourAndASymbol() {
        let app = launch(["-debugNewSheet", "folder"])
        let name = app.textFields["Name"]
        XCTAssertTrue(name.waitForExistence(timeout: 20), "the folder sheet never appeared")
        XCTAssertTrue(app.navigationBars["New Folder"].exists)
        let focused = NSPredicate { _, _ in self.hasFocus(name) }
        wait(for: [expectation(for: focused, evaluatedWith: name)], timeout: 5)
        app.typeText("Work")
        app.buttons["Red"].tap()
        app.buttons["Briefcase"].tap()
        app.navigationBars["New Folder"].buttons["Done"].tap()
        XCTAssertEqual(result(app), "folder done: name=Work color=red icon=symbol:briefcase")
    }

    func testTheEmojiFieldRefusesText() {
        let app = launch(["-debugNewSheet", "folder", "red", "emoji:📒"])
        let emoji = app.textFields["Emoji"]
        XCTAssertTrue(emoji.waitForExistence(timeout: 20), "the folder sheet never appeared")
        XCTAssertTrue(app.navigationBars["Edit Folder"].exists)
        XCTAssertEqual(emoji.value as? String, "📒")
        emoji.tap()
        emoji.typeText("ab")
        XCTAssertEqual(emoji.value as? String, "📒")
        app.typeText("\n")
        XCTAssertEqual(result(app), "folder done: name=Accounting color=red icon=emoji:📒")
    }
}
