import CoreGraphics
import CoreText
import Foundation
import ImageIO
import UniformTypeIdentifiers
import XCTest

@testable import PaperCore

/// Vision, on a page we drew ourselves.
///
/// The simulator has no Apple Pencil and `swift test` has no simulator at all,
/// so the fixture is printed text rendered with CoreText: the same recogniser,
/// the same coordinate conversion, the same `text.json`, with an input a test
/// can assert against exactly.
final class RecognitionTests: XCTestCase {
    private let pageSize = CGSize(width: 595, height: 842)

    func testRecognisesPrintedTextOnAFixturePage() throws {
        let image = try Self.fixture(
            "hello world", size: pageSize, scale: 2, origin: CGPoint(x: 60, y: 200))
        let text = try HandwritingRecognizer.recognize(image: image, pageSize: pageSize)

        XCTAssertFalse(text.lines.isEmpty, "Vision read nothing off the fixture")
        XCTAssertTrue(
            text.transcript.lowercased().contains("hello"),
            "transcript was \(text.transcript)")
    }

    func testBoxesAreInPagePointsAndFlippedTheRightWayUp() throws {
        // Drawn near the top of the page. A box that came back unflipped would
        // land near the bottom, which is exactly the bug this guards.
        let image = try Self.fixture(
            "hello world", size: pageSize, scale: 2, origin: CGPoint(x: 60, y: 120))
        let text = try HandwritingRecognizer.recognize(image: image, pageSize: pageSize)
        let box = try XCTUnwrap(text.lines.first?.box)

        XCTAssertGreaterThanOrEqual(box.x, 0)
        XCTAssertGreaterThanOrEqual(box.y, 0)
        XCTAssertLessThanOrEqual(box.x + box.width, Double(pageSize.width) + 1)
        XCTAssertLessThanOrEqual(box.y + box.height, Double(pageSize.height) + 1)
        XCTAssertLessThan(
            box.y, Double(pageSize.height) / 2,
            "text drawn at the top of the page came back in the bottom half")
        XCTAssertGreaterThan(box.width, 20)
    }

    func testFlipConvertsVisionsBottomLeftBoxToPagePoints() {
        // Vision: a box in the top-left eighth of the page, origin bottom-left.
        let normalized = CGRect(x: 0.1, y: 0.8, width: 0.3, height: 0.05)
        let box = HandwritingRecognizer.pageBox(normalized, pageSize: CGSize(width: 100, height: 200))
        XCTAssertEqual(box.x, 10, accuracy: 0.001)
        // 1 - (0.8 + 0.05) = 0.15 of the height down from the top.
        XCTAssertEqual(box.y, 30, accuracy: 0.001)
        XCTAssertEqual(box.width, 30, accuracy: 0.001)
        XCTAssertEqual(box.height, 10, accuracy: 0.001)
    }

    /// The whole loop: a render on disk, five seconds shortened to nothing, a
    /// `text.json` beside it and a searchable index row.
    func testRecogniserWritesTextJSONAndIndexesThePage() async throws {
        let root = try Self.temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = try PaperStore(root: root)
        let notebook = try store.createNotebook(title: "Fixtures", template: .blank, size: .a4)
        let page = try XCTUnwrap(try store.pages(ofNotebook: notebook.id).first)

        let image = try Self.fixture(
            "quarterly review", size: pageSize, scale: 2, origin: CGPoint(x: 60, y: 200))
        try Self.writePNG(image, to: store.paths.render(notebook.path, page.id))

        let recognizer = HandwritingRecognizer(store: store, idleDelay: 0)
        let recognised = await recognizer.recognizeNow(
            RecognitionJob(notebookPath: notebook.path, page: page))
        let text = try XCTUnwrap(recognised)
        XCTAssertTrue(text.transcript.lowercased().contains("quarterly"))

        let onDisk = try XCTUnwrap(store.pageText(notebookPath: notebook.path, pageID: page.id))
        XCTAssertEqual(onDisk.transcript, text.transcript)
        XCTAssertFalse(onDisk.lines.isEmpty)

        let groups = store.textIndex.search("quarterly")
        XCTAssertEqual(groups.first?.pages.first?.pageID, page.id)
    }

    /// A page somebody is still writing on is never read: the transcript would
    /// describe ink that no longer exists.
    func testDirtyPageIsNotRecognised() async throws {
        let root = try Self.temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = try PaperStore(root: root)
        let notebook = try store.createNotebook(title: "Fixtures", template: .blank, size: .a4)
        let page = try XCTUnwrap(try store.pages(ofNotebook: notebook.id).first)
        let image = try Self.fixture(
            "hello world", size: pageSize, scale: 2, origin: CGPoint(x: 60, y: 200))
        try Self.writePNG(image, to: store.paths.render(notebook.path, page.id))

        let recognizer = HandwritingRecognizer(store: store, isDirty: { _ in true }, idleDelay: 0)
        let text = await recognizer.recognizeNow(
            RecognitionJob(notebookPath: notebook.path, page: page))
        XCTAssertNil(text)
        XCTAssertNil(store.pageText(notebookPath: notebook.path, pageID: page.id))
    }

    // MARK: - Fixtures

    static func temporaryRoot() throws -> URL {
        let url = URL(fileURLWithPath: NSTemporaryDirectory())
            .appending(path: "paper-recognition-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    /// A white page with one line of large printed text on it, drawn with
    /// CoreText so the bitmap is deterministic and needs no asset.
    ///
    /// `origin` is in page points from the **top** left, the same way every box
    /// in this codebase is expressed.
    static func fixture(_ string: String, size: CGSize, scale: CGFloat, origin: CGPoint) throws
        -> CGImage
    {
        let width = Int(size.width * scale)
        let height = Int(size.height * scale)
        guard
            let context = CGContext(
                data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
        else { throw RecognitionError.imageUnavailable }
        context.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
        context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        context.scaleBy(x: scale, y: scale)

        let font = CTFontCreateWithName("Helvetica" as CFString, 56, nil)
        let attributed = NSAttributedString(
            string: string,
            attributes: [
                .init(kCTFontAttributeName as String): font,
                .init(kCTForegroundColorAttributeName as String): CGColor(
                    red: 0, green: 0, blue: 0, alpha: 1),
            ])
        let line = CTLineCreateWithAttributedString(attributed)
        context.textMatrix = .identity
        // CoreText draws from the baseline upwards in a y-up context; the page
        // point given is measured from the top, so flip it once here.
        context.textPosition = CGPoint(x: origin.x, y: size.height - origin.y)
        CTLineDraw(line, context)

        guard let image = context.makeImage() else { throw RecognitionError.imageUnavailable }
        return image
    }

    static func writePNG(_ image: CGImage, to url: URL) throws {
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let data = NSMutableData()
        guard
            let destination = CGImageDestinationCreateWithData(
                data as CFMutableData, UTType.png.identifier as CFString, 1, nil)
        else { throw RecognitionError.imageUnavailable }
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else {
            throw RecognitionError.imageUnavailable
        }
        try (data as Data).write(to: url)
    }
}
