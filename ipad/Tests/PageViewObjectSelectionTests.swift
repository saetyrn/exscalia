import PaperCore
import PencilKit
import XCTest

@testable import PaperUI

/// The lasso overlay sits above the objects layer, so an object selected
/// underneath it would keep its handles and inspector where no touch can reach
/// them. Picking the Lasso puts the object down, and while the Lasso is active
/// a tap never picks one up.
@MainActor
final class PageViewObjectSelectionTests: XCTestCase {
    /// Held here because the page keeps its delegate weakly.
    private var host: StubPageViewDelegate?

    private func makeLivePage() -> PageView {
        let host = StubPageViewDelegate()
        self.host = host
        let view = PageView(page: Page(), notebookPath: "paper/T.paper")
        view.delegate = host
        view.goLive(drawingData: nil)
        XCTAssertTrue(view.isLive)
        return view
    }

    private func textObject() -> PageObject {
        PageObject(
            type: .text, frame: PageObject.Frame(x: 100, y: 100, width: 200, height: 60),
            text: "x")
    }

    func testPickingTheLassoDropsTheObjectSelection() {
        let view = makeLivePage()
        view.insertObject(textObject())
        XCTAssertTrue(view.hasSelectedObject)
        XCTAssertEqual(view.canvas?.isUserInteractionEnabled, false)

        view.setLassoActive(true)
        XCTAssertFalse(view.hasSelectedObject)
        XCTAssertEqual(view.canvas?.isUserInteractionEnabled, true)
        XCTAssertFalse(view.objects.isUserInteractionEnabled)

        view.setLassoActive(false)
        view.insertObject(textObject())
        XCTAssertTrue(view.hasSelectedObject)
    }

    func testATapInLassoModeDoesNotSelectAnObject() {
        let view = makeLivePage()
        let object = textObject()
        view.insertObject(object)
        view.deselectObjects()
        XCTAssertFalse(view.hasSelectedObject)
        let inside = CGPoint(x: object.frame.rect.midX, y: object.frame.rect.midY)

        view.setLassoActive(true)
        view.handleObjectTap(at: inside)
        XCTAssertFalse(view.hasSelectedObject)

        view.setLassoActive(false)
        view.handleObjectTap(at: inside)
        XCTAssertTrue(view.hasSelectedObject)
    }
}

@MainActor
private final class StubPageViewDelegate: PageViewDelegate {
    let tools = ToolState()

    func pageViewDrawingChanged(_ pageView: PageView) {}
    func pageViewDidBeginUsingTool(_ pageView: PageView) {}
    func pageViewDidEndUsingTool(_ pageView: PageView) {}
    func pageViewToolState(_ pageView: PageView) -> ToolState { tools }
    func pageViewDrawingPolicy(_ pageView: PageView) -> PKCanvasViewDrawingPolicy { .pencilOnly }
    func pageView(_ pageView: PageView, didChangeSelection isEmpty: Bool) {}
    func pageView(_ pageView: PageView, didChangeObjects objects: [PageObject]) {}
    func pageView(_ pageView: PageView, imageForAsset assetPath: String) -> UIImage? { nil }
    func pageView(_ pageView: PageView, didDropImage data: Data, name: String, at point: CGPoint) {}
    func pageView(_ pageView: PageView, didDropPDF data: Data, name: String) {}
}
