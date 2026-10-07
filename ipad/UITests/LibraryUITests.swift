import XCTest

/// The library through real touches: a folder opens with the back chip and
/// its name centred; a folder is made, renamed in place, given a notebook
/// through the move picker, starred, trashed and brought back.
///
/// Every launch points at a server nothing listens on, which gives the tests
/// a store of their own (the store is keyed by the server) and keeps them
/// offline. `-debugFolders` seeds the folders of the owner's screenshot once.
final class LibraryUITests: XCTestCase {
    private let server = "http://127.0.0.1:8799"

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    private func launch(_ arguments: [String] = []) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments =
            ["-surface", "paper", "-debugFolders", "-server", server] + arguments
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

    /// Replaces what a field holds: the caret goes to the end, then every
    /// character is deleted.
    private func replace(_ field: XCUIElement, with text: String) {
        field.coordinate(withNormalizedOffset: CGVector(dx: 0.97, dy: 0.5)).tap()
        let current = (field.value as? String) ?? ""
        field.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: current.count + 2))
        field.typeText(text)
    }

    func testAFolderOpensWithTheBackChipAndItsNameCentred() {
        let app = launch()
        let accounting = row(app, "Accounting")
        XCTAssertTrue(accounting.waitForExistence(timeout: 30), "the seeded folder never appeared")
        XCTAssertTrue(app.staticTexts["library.title"].exists, "no Documents title at the top level")
        XCTAssertFalse(app.buttons["Back"].exists, "a back chip at the top level")
        XCTAssertTrue(app.buttons["Sidebar"].exists)
        XCTAssertTrue(app.buttons["New"].exists)

        accounting.tap()
        let back = app.buttons["Back"]
        XCTAssertTrue(back.waitForExistence(timeout: 8), "no back chip inside the folder")
        let title = app.staticTexts["library.folderTitle"]
        XCTAssertTrue(title.waitForExistence(timeout: 5))
        XCTAssertEqual(title.label, "Accounting")
        let window = app.windows.firstMatch.frame
        XCTAssertEqual(title.frame.midX, window.midX, accuracy: 2, "the folder's name is not centred")
        XCTAssertLessThan(title.frame.midY, row(app, "Practice Test").frame.minY)
        XCTAssertFalse(app.staticTexts["library.title"].exists, "the large title stayed")
        XCTAssertTrue(row(app, "Practice Test").exists)
        XCTAssertTrue(back.frame.minX > app.buttons["Sidebar"].frame.maxX, "the back chip is beside the sidebar chip")

        back.tap()
        XCTAssertTrue(app.staticTexts["library.title"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["Back"].exists)
    }

    /// Settings sits at the foot of the sidebar, under Trash, and opens the
    /// shell's settings: the one way to them in a Release build.
    func testSettingsOpensFromTheFootOfTheSidebar() {
        let app = launch(["-debugLibrary", "sidebar"])
        let settings = app.buttons["library.settings"]
        XCTAssertTrue(settings.waitForExistence(timeout: 30), "no Settings in the sidebar")
        XCTAssertEqual(settings.label, "Settings")
        let trash = app.buttons["Trash"]
        XCTAssertTrue(trash.exists)
        XCTAssertGreaterThan(settings.frame.minY, trash.frame.maxY, "Settings is not under Trash")
        settings.tap()
        XCTAssertTrue(
            app.navigationBars["Settings"].waitForExistence(timeout: 8), "the settings did not open")
        XCTAssertTrue(app.buttons["Reset local Paper cache"].exists)
    }

    func testAFolderIsMadeRenamedFilledStarredTrashedAndBroughtBack() {
        let app = launch()
        XCTAssertTrue(row(app, "Accounting").waitForExistence(timeout: 30))
        let stamp = String(UUID().uuidString.prefix(6))
        let name = "UI \(stamp)"
        let renamed = "Renamed \(stamp)"
        let notebook = "Move \(stamp)"

        // + New > Folder.
        app.buttons["New"].tap()
        let folderEntry = app.buttons["Folder"]
        XCTAssertTrue(folderEntry.waitForExistence(timeout: 5), "the New menu did not open")
        folderEntry.tap()
        let field = app.textFields["Name"]
        XCTAssertTrue(field.waitForExistence(timeout: 8), "the folder sheet did not open")
        field.typeText(name)
        app.navigationBars["New Folder"].buttons["Done"].tap()
        XCTAssertTrue(row(app, name).waitForExistence(timeout: 8), "the new folder is not listed")

        // Renamed in place, in its menu's name field.
        app.buttons["itemMenu:\(name)"].tap()
        let menuName = app.textFields["itemMenu.name"]
        XCTAssertTrue(menuName.waitForExistence(timeout: 5), "the item menu did not open")
        replace(menuName, with: renamed + "\n")
        XCTAssertTrue(row(app, renamed).waitForExistence(timeout: 8), "the rename did not land")
        XCTAssertFalse(row(app, name).exists)

        // A notebook to move: + New > Notebook opens it; Home comes back.
        app.buttons["New"].tap()
        let notebookEntry = app.buttons["Notebook"]
        XCTAssertTrue(notebookEntry.waitForExistence(timeout: 5))
        notebookEntry.tap()
        let title = app.textFields["Title"]
        XCTAssertTrue(title.waitForExistence(timeout: 8), "the notebook sheet did not open")
        title.typeText(notebook + "\n")
        let home = app.buttons["Home"]
        XCTAssertTrue(home.waitForExistence(timeout: 20), "the new notebook did not open")
        home.tap()
        XCTAssertTrue(row(app, notebook).waitForExistence(timeout: 8))

        // Into the folder through the move picker.
        app.buttons["itemMenu:\(notebook)"].tap()
        let move = app.buttons["Move"]
        XCTAssertTrue(move.waitForExistence(timeout: 5))
        move.tap()
        let target = app.buttons["moveTarget:\(renamed)"]
        XCTAssertTrue(target.waitForExistence(timeout: 8), "the picker does not list the folder")
        target.tap()
        let confirm = app.navigationBars.buttons["Move"]
        XCTAssertTrue(waitFor { confirm.isEnabled }, "Move stayed disabled")
        confirm.tap()
        XCTAssertTrue(waitFor { !self.row(app, notebook).exists }, "the notebook is still at the top")
        row(app, renamed).tap()
        XCTAssertTrue(row(app, notebook).waitForExistence(timeout: 8), "the notebook is not inside")
        app.buttons["Back"].tap()
        XCTAssertTrue(row(app, renamed).waitForExistence(timeout: 8))

        // Starred.
        let star = app.buttons["favorite:\(renamed)"]
        XCTAssertEqual(star.value as? String, "off")
        star.tap()
        XCTAssertTrue(waitFor { star.value as? String == "on" }, "the star did not take")
        XCTAssertTrue(row(app, renamed).label.hasSuffix(", Favorite"))

        // Trashed, and back with Undo.
        app.buttons["itemMenu:\(renamed)"].tap()
        let trash = app.buttons["Move to Trash"]
        XCTAssertTrue(trash.waitForExistence(timeout: 5))
        trash.tap()
        XCTAssertTrue(waitFor { !self.row(app, renamed).exists }, "the folder was not trashed")
        let undo = app.buttons["Undo"]
        XCTAssertTrue(undo.waitForExistence(timeout: 5), "no undo toast")
        undo.tap()
        XCTAssertTrue(row(app, renamed).waitForExistence(timeout: 8), "Undo did not bring it back")
        row(app, renamed).tap()
        XCTAssertTrue(row(app, notebook).waitForExistence(timeout: 8), "the notebook did not come back with it")
    }
}
