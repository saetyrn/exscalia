import PaperCore
import XCTest

@testable import PaperUI

/// A page builds its object views in `init`, before it has a delegate to ask
/// for their pictures. An image object must still end up showing its picture:
/// when the delegate is set, and later when a file that was missing arrives.
@MainActor
final class ImageObjectLoadingTests: XCTestCase {
    /// Held here because the page keeps its delegate weakly.
    private var host: ImageStubDelegate?

    private func makeHost() throws -> ImageStubDelegate {
        let defaults = try scratchDefaults("ImageObjectLoadingTests")
        let host = ImageStubDelegate(tools: ToolState(defaults: defaults))
        self.host = host
        return host
    }

    private func pageWithImage() -> (Page, PageObject) {
        let object = PageObject(
            type: .image, frame: PageObject.Frame(x: 100, y: 400, width: 120, height: 80),
            asset: "paper/T.paper/assets/picture.png")
        var page = Page()
        page.objects = [object]
        return (page, object)
    }

    private func imageView(of object: PageObject, in view: PageView) throws -> ImageObjectView {
        try XCTUnwrap(view.objects.views[object.id] as? ImageObjectView)
    }

    func testAPageLoadsItsPicturesWhenItGetsItsDelegate() throws {
        let host = try makeHost()
        host.images["paper/T.paper/assets/picture.png"] = Self.picture()
        let (page, object) = pageWithImage()

        let view = PageView(page: page, notebookPath: "paper/T.paper")
        XCTAssertFalse(try imageView(of: object, in: view).hasImage)
        view.delegate = host

        XCTAssertTrue(try imageView(of: object, in: view).hasImage)
    }

    func testABoardLoadsItsPicturesWhenItGetsItsDelegate() throws {
        let host = try makeHost()
        host.images["paper/T.paper/assets/picture.png"] = Self.picture()
        let (page, object) = pageWithImage()

        let view = PageView(page: page, notebookPath: "paper/T.paper", isBoard: true)
        view.delegate = host

        XCTAssertTrue(try imageView(of: object, in: view).hasImage)
    }

    func testAPictureThatArrivesLaterShowsOnTheNextUpdate() throws {
        let host = try makeHost()
        let (page, object) = pageWithImage()
        let view = PageView(page: page, notebookPath: "paper/T.paper")
        view.delegate = host
        XCTAssertFalse(try imageView(of: object, in: view).hasImage)

        // The file comes down with a sync; the page is refreshed unchanged.
        host.images["paper/T.paper/assets/picture.png"] = Self.picture()
        view.update(page: page)

        XCTAssertTrue(try imageView(of: object, in: view).hasImage)
        XCTAssertEqual(host.asked, 2, "once at the delegate, once at the refresh")
        view.update(page: page)
        XCTAssertEqual(host.asked, 2, "a picture that is showing is not fetched again")
    }

    private static func picture() -> UIImage {
        UIGraphicsImageRenderer(size: CGSize(width: 4, height: 4)).image { context in
            UIColor.red.setFill()
            context.fill(CGRect(x: 0, y: 0, width: 4, height: 4))
        }
    }
}

@MainActor
private final class ImageStubDelegate: PageViewDelegate {
    let tools: ToolState
    var images: [String: UIImage] = [:]
    private(set) var asked = 0

    init(tools: ToolState) { self.tools = tools }

    func pageViewDrawingChanged(_ pageView: PageView) {}
    func pageViewDidBeginUsingTool(_ pageView: PageView) {}
    func pageViewDidEndUsingTool(_ pageView: PageView) {}
    func pageViewToolState(_ pageView: PageView) -> ToolState { tools }
    func pageView(_ pageView: PageView, didChangeSelection isEmpty: Bool) {}
    func pageView(_ pageView: PageView, didChangeObjects objects: [PageObject]) {}
    func pageView(_ pageView: PageView, imageForAsset assetPath: String) -> UIImage? {
        asked += 1
        return images[assetPath]
    }
    func pageView(_ pageView: PageView, didDropImage data: Data, name: String, at point: CGPoint) {}
    func pageView(_ pageView: PageView, didDropPDF data: Data, name: String) {}
}
