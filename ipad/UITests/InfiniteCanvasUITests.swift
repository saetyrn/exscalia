import XCTest

/// An infinite page grows. The arithmetic is covered by `swift test` in
/// PaperCore; what is checked here is that the canvas, the layout and the
/// stored extent actually follow it.
final class InfiniteCanvasUITests: XCTestCase {
    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    private func page(_ app: XCUIApplication) -> XCUIElement {
        let element = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", "drawing canvas")).firstMatch
        XCTAssertTrue(element.waitForExistence(timeout: 20), "the first page never appeared")
        return element
    }

    /// `value` reports "strokes:N extent:WxH" in DEBUG builds.
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

    func testANewNotebookStartsAtTheInfiniteBaseExtent() {
        let app = XCUIApplication()
        app.launchArguments = ["-surface", "paper", "-debugNewNotebook"]
        app.launch()
        // A new notebook is Ruled + Infinite, and an infinite page starts at
        // 1024x1366.
        XCTAssertEqual(extent(page(app)), CGSize(width: 1024, height: 1366))
    }

    func testWritingNearTheBottomEdgeExtendsThePage() {
        let app = XCUIApplication()
        app.launchArguments = ["-surface", "paper", "-debugNewNotebook", "-debugGrow"]
        app.launch()
        let size = extent(page(app))
        // 1024x1366 is where an infinite page starts; the debug stroke is
        // written 90 points from the bottom, well inside the 160-point margin.
        XCTAssertGreaterThan(
            size.height, 1366,
            "writing at the bottom of an infinite page did not extend it")
    }
}
