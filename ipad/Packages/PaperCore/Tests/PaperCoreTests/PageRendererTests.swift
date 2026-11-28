import CoreGraphics
import Foundation
import ImageIO
import PDFKit
import PencilKit
import XCTest

#if canImport(UIKit)
    import UIKit
    typealias PlatformColor = UIColor
#else
    import AppKit
    typealias PlatformColor = NSColor
#endif

@testable import PaperCore

final class PageRendererTests: XCTestCase {
    /// A short diagonal stroke in the middle of the page, dark enough that a
    /// pixel test is unambiguous.
    private func drawingData() -> Data {
        let points = stride(from: 0.0, through: 200.0, by: 10.0).map { offset in
            PKStrokePoint(
                location: CGPoint(x: 120 + offset, y: 200 + offset), timeOffset: offset / 200,
                size: CGSize(width: 6, height: 6), opacity: 1, force: 1, azimuth: 0,
                altitude: .pi / 2)
        }
        let path = PKStrokePath(controlPoints: points, creationDate: Date())
        let ink = PKInk(.pen, color: .black)
        return PKDrawing(strokes: [PKStroke(ink: ink, path: path)]).dataRepresentation()
    }

    private func pixels(_ png: Data) throws -> (width: Int, height: Int, data: [UInt8]) {
        let source = try XCTUnwrap(CGImageSourceCreateWithData(png as CFData, nil))
        let image = try XCTUnwrap(CGImageSourceCreateImageAtIndex(source, 0, nil))
        let width = image.width
        let height = image.height
        let count = width * height * 4
        let raw = UnsafeMutableRawPointer.allocate(byteCount: count, alignment: 8)
        defer { raw.deallocate() }
        raw.initializeMemory(as: UInt8.self, repeating: 0, count: count)
        let context = try XCTUnwrap(
            CGContext(
                data: raw, width: width, height: height, bitsPerComponent: 8,
                bytesPerRow: width * 4, space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        let buffer = Array(UnsafeBufferPointer(start: raw.assumingMemoryBound(to: UInt8.self), count: count))
        return (width, height, buffer)
    }

    func testRendersAtTwiceThePageSize() throws {
        let png = try PageRenderer.renderPNG(
            PageRenderJob(
                notebookPath: "paper/A.paper", pageID: "p1", template: .ruled, size: .a4))
        let image = try pixels(png)
        XCTAssertEqual(image.width, 1190)  // 595 * 2
        XCTAssertEqual(image.height, 1684)  // 842 * 2
    }

    func testABlankRuledPageIsPaperColouredWithRules() throws {
        let png = try PageRenderer.renderPNG(
            PageRenderJob(
                notebookPath: "paper/A.paper", pageID: "p1", template: .ruled, size: .a4))
        let image = try pixels(png)

        // Top-left corner sits above the first rule: bare paper #fdfcfa.
        let corner = 4 * (2 * image.width + 2)
        XCTAssertEqual(image.data[corner], 0xfd)
        XCTAssertEqual(image.data[corner + 1], 0xfc)
        XCTAssertEqual(image.data[corner + 2], 0xfa)

        // Rules exist: something on the page is darker than the paper.
        let darkest = stride(from: 0, to: image.data.count, by: 4).map { image.data[$0] }.min() ?? 255
        XCTAssertLessThan(darkest, 0xf0)
    }

    func testInkLandsOnThePageAndOnlyWhereItWasDrawn() throws {
        let blank = try PageRenderer.renderPNG(
            PageRenderJob(
                notebookPath: "paper/A.paper", pageID: "p1", template: .blank, size: .a4))
        let inked = try PageRenderer.renderPNG(
            PageRenderJob(
                notebookPath: "paper/A.paper", pageID: "p1", template: .blank, size: .a4,
                drawing: drawingData()))

        let blankPixels = try pixels(blank)
        let inkedPixels = try pixels(inked)
        XCTAssertEqual(blankPixels.width, inkedPixels.width)

        // A blank page has no dark pixels at all.
        let blankDark = stride(from: 0, to: blankPixels.data.count, by: 4)
            .filter { blankPixels.data[$0] < 100 }.count
        XCTAssertEqual(blankDark, 0)

        // The inked one does, and they are a stroke rather than a wash.
        let inkedDark = stride(from: 0, to: inkedPixels.data.count, by: 4)
            .filter { inkedPixels.data[$0] < 100 }.count
        XCTAssertGreaterThan(inkedDark, 500)
        XCTAssertLessThan(inkedDark, inkedPixels.width * inkedPixels.height / 10)

        // Ink is upright: the stroke runs from (120,200) downwards, so the top
        // strip of the page stays clean.
        let topStrip = (0..<(inkedPixels.width * 200 * 4)).filter {
            $0 % 4 == 0 && inkedPixels.data[$0] < 100
        }
        XCTAssertTrue(topStrip.isEmpty)
    }

    func testEveryTemplateAndSizeRenders() throws {
        for template in Template.allCases {
            for size in PaperSize.allCases {
                let png = try PageRenderer.renderPNG(
                    PageRenderJob(
                        notebookPath: "paper/A.paper", pageID: "p1", template: template, size: size))
                let image = try pixels(png)
                // A board renders its content, not its 16,384-point extent;
                // an empty one shows 1,366 x 1,024 points round its centre.
                let expected = size == .board ? BoardGeometry.emptyRenderSize : size.points
                XCTAssertEqual(image.width, Int(expected.width * 2), "\(template) \(size)")
                XCTAssertEqual(image.height, Int(expected.height * 2), "\(template) \(size)")
            }
        }
    }

    /// The long edge alone would let a page grown in both directions render at
    /// 8192x8192; the area cap holds it to a picture a background flush can
    /// afford.
    func testRenderScaleIsCappedByAreaAsWellAsByTheLongEdge() {
        let square = CGSize(width: 8000, height: 8000)
        let scale = PageRenderer.cappedScale(2, for: square)
        XCTAssertGreaterThan(scale, 0)
        XCTAssertLessThanOrEqual(
            square.width * scale * square.height * scale, PageRenderer.maximumPixelArea + 1)
        XCTAssertLessThanOrEqual(
            max(square.width, square.height) * scale, PageRenderer.maximumPixelEdge)
        // Ordinary pages are not touched.
        XCTAssertEqual(PageRenderer.cappedScale(2, for: PaperSize.a4.points), 2)
        XCTAssertEqual(PageRenderer.cappedScale(3, for: PaperSize.iPad.points), 3)
    }

    func testCoverThumbnailIsSmallAndKeepsTheAspectRatio() throws {
        let png = try PageRenderer.renderPNG(
            PageRenderJob(
                notebookPath: "paper/A.paper", pageID: "p1", template: .dotted, size: .a4))
        let cover = try XCTUnwrap(PageRenderer.coverJPEG(from: png))
        let image = try pixels(cover)
        XCTAssertEqual(image.height, Int(PageRenderer.coverLongEdge))
        XCTAssertEqual(Double(image.width) / Double(image.height), 595.0 / 842.0, accuracy: 0.01)
        XCTAssertLessThan(cover.count, png.count)
    }

    func testRendererSkipsPagesThatAreStillBeingWrittenOn() async throws {
        let root = FileManager.default.temporaryDirectory
            .appending(path: "RendererTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        defer { try? FileManager.default.removeItem(at: root) }
        let paths = PaperPaths(root: root)
        let renderer = PageRenderer(paths: paths, isDirty: { $0 == "dirty" })

        await renderer.render(
            PageRenderJob(
                notebookPath: "paper/A.paper", pageID: "dirty", template: .ruled, size: .a4))
        await renderer.render(
            PageRenderJob(
                notebookPath: "paper/A.paper", pageID: "clean", template: .ruled, size: .a4))

        XCTAssertFalse(
            FileManager.default.fileExists(
                atPath: paths.render("paper/A.paper", "dirty").path(percentEncoded: false)))
        XCTAssertTrue(
            FileManager.default.fileExists(
                atPath: paths.render("paper/A.paper", "clean").path(percentEncoded: false)))
    }
}

/// Objects and PDF backgrounds are composited by the same function the export
/// uses, so these tests are what keeps `render.png` matching the screen.
final class PageRendererObjectTests: XCTestCase {
    private var root: URL!
    private var store: PaperStore!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appending(path: "RendererObjects-\(UUID().uuidString)", directoryHint: .isDirectory)
        store = try PaperStore(root: root)
    }

    override func tearDownWithError() throws {
        store = nil
        try? FileManager.default.removeItem(at: root)
    }

    private func pixels(_ png: Data) throws -> (width: Int, height: Int, data: [UInt8]) {
        let source = try XCTUnwrap(CGImageSourceCreateWithData(png as CFData, nil))
        let image = try XCTUnwrap(CGImageSourceCreateImageAtIndex(source, 0, nil))
        let width = image.width
        let height = image.height
        let count = width * height * 4
        let raw = UnsafeMutableRawPointer.allocate(byteCount: count, alignment: 8)
        defer { raw.deallocate() }
        raw.initializeMemory(as: UInt8.self, repeating: 0, count: count)
        let context = try XCTUnwrap(
            CGContext(
                data: raw, width: width, height: height, bitsPerComponent: 8,
                bytesPerRow: width * 4, space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        let buffer = Array(
            UnsafeBufferPointer(start: raw.assumingMemoryBound(to: UInt8.self), count: count))
        return (width, height, buffer)
    }

    /// Opaque random pixels, which PNG cannot compress: the stand-in for a
    /// photograph or a scan. Seeded, so every run draws the same picture.
    private func noisePNG(width: Int, height: Int) throws -> Data {
        var bytes = [UInt8](repeating: 255, count: width * height * 4)
        var state: UInt64 = 0x9E37_79B9_7F4A_7C15
        for pixel in 0..<(width * height) {
            state ^= state << 13
            state ^= state >> 7
            state ^= state << 17
            bytes[4 * pixel] = UInt8(truncatingIfNeeded: state)
            bytes[4 * pixel + 1] = UInt8(truncatingIfNeeded: state >> 8)
            bytes[4 * pixel + 2] = UInt8(truncatingIfNeeded: state >> 16)
        }
        let provider = try XCTUnwrap(CGDataProvider(data: Data(bytes) as CFData))
        let image = try XCTUnwrap(
            CGImage(
                width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32,
                bytesPerRow: width * 4, space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipLast.rawValue),
                provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent))
        let data = NSMutableData()
        let destination = try XCTUnwrap(
            CGImageDestinationCreateWithData(data as CFMutableData, "public.png" as CFString, 1, nil))
        CGImageDestinationAddImage(destination, image, nil)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
        return data as Data
    }

    private func pixel(_ image: (width: Int, height: Int, data: [UInt8]), x: Int, y: Int)
        -> (UInt8, UInt8, UInt8)
    {
        let offset = 4 * (y * image.width + x)
        return (image.data[offset], image.data[offset + 1], image.data[offset + 2])
    }

    func testATextObjectLandsInsideItsFrameAndNowhereElse() throws {
        let job = PageRenderJob(
            notebookPath: "paper/A.paper", pageID: "p1", template: .blank, size: .a4,
            objects: [
                PageObject(
                    type: .text, frame: PageObject.Frame(x: 60, y: 80, width: 400, height: 100),
                    text: "Quarterly review", fontSize: 32)
            ])
        let image = try pixels(try PageRenderer.renderPNG(job))

        // Dark pixels exist, and all of them are inside the object's frame.
        var darkCount = 0
        var outside = 0
        let frame = CGRect(x: 60, y: 80, width: 400, height: 100)
            .applying(CGAffineTransform(scaleX: PageRenderer.scale, y: PageRenderer.scale))
        for y in 0..<image.height {
            for x in 0..<image.width where image.data[4 * (y * image.width + x)] < 120 {
                darkCount += 1
                if !frame.insetBy(dx: -2, dy: -2).contains(CGPoint(x: x, y: y)) { outside += 1 }
            }
        }
        XCTAssertGreaterThan(darkCount, 200)
        XCTAssertEqual(outside, 0)
    }

    func testAnImageAssetIsDrawnWhereTheObjectSaysItIs() throws {
        let notebook = try store.createNotebook(title: "Images")
        let assetPath = try store.storeAsset(
            try PageRendererObjectFixtures.redPNG(), preferredName: "swatch.png",
            notebookPath: notebook.path)
        let job = PageRenderJob(
            notebookPath: notebook.path, pageID: "p1", template: .blank, size: .a4,
            objects: [
                PageObject(
                    type: .image, frame: PageObject.Frame(x: 100, y: 200, width: 120, height: 90),
                    asset: assetPath)
            ], vaultURL: store.paths.vault)
        let image = try pixels(try PageRenderer.renderPNG(job))

        let scale = Int(PageRenderer.scale)
        let inside = pixel(image, x: 160 * scale, y: 245 * scale)
        XCTAssertGreaterThan(inside.0, 200)
        XCTAssertLessThan(inside.1, 60)
        XCTAssertLessThan(inside.2, 60)

        // Just outside the frame the page is still paper.
        let outside = pixel(image, x: 90 * scale, y: 245 * scale)
        XCTAssertEqual(outside.0, 0xfd)
        XCTAssertEqual(outside.1, 0xfc)
        XCTAssertEqual(outside.2, 0xfa)
    }

    func testACropShowsOnlyThePartOfTheImageItSelects() throws {
        let notebook = try store.createNotebook(title: "Images")
        let assetPath = try store.storeAsset(
            try PageRendererObjectFixtures.redPNG(), preferredName: "swatch.png",
            notebookPath: notebook.path)
        var object = PageObject(
            type: .image, frame: PageObject.Frame(x: 100, y: 200, width: 120, height: 90),
            asset: assetPath)
        object.crop = PageObject.Frame(x: 0.25, y: 0.25, width: 0.5, height: 0.5)
        let job = PageRenderJob(
            notebookPath: notebook.path, pageID: "p1", template: .blank, size: .a4,
            objects: [object], vaultURL: store.paths.vault)
        let image = try pixels(try PageRenderer.renderPNG(job))
        let scale = Int(PageRenderer.scale)
        // The swatch is one colour, so a crop still fills the frame with red:
        // what is being checked is that cropping does not lose the image.
        let inside = pixel(image, x: 160 * scale, y: 245 * scale)
        XCTAssertGreaterThan(inside.0, 200)
    }

    func testAnImportedPDFPageIsDrawnUnderTheInk() throws {
        let notebook = try store.createNotebook(title: "Imports")
        let result = try PDFImporter(store: store).importPDF(
            data: try PDFFixture.threePages(), name: "source.pdf", into: notebook.id)
        let page = result.pages[0]
        let job = PageRenderJob(
            notebookPath: notebook.path, page: page,
            drawing: PageRendererObjectFixtures.strokeData(), vaultURL: store.paths.vault)
        let image = try pixels(try PageRenderer.renderPNG(job))

        XCTAssertEqual(image.width, Int(595 * PageRenderer.scale))
        XCTAssertEqual(image.height, Int(842 * PageRenderer.scale))
        // The fixture's own heading is near the top; the stroke is lower down.
        let headingDark = (60..<260).flatMap { y in (60..<600).map { x in (x, y) } }
            .filter { pixel(image, x: $0.0, y: $0.1).0 < 120 }.count
        XCTAssertGreaterThan(headingDark, 100)
        let strokeDark = (620..<1020).flatMap { y in (200..<700).map { x in (x, y) } }
            .filter { pixel(image, x: $0.0, y: $0.1).0 < 120 }.count
        XCTAssertGreaterThan(strokeDark, 100)
    }

    /// Pixels bound memory, not bytes: a page of photograph compresses so
    /// badly that a render inside the pixel cap is still far over the 8 MB the
    /// server takes. Such a render is drawn again smaller until it fits.
    func testAPhotographicPageRendersWithinTheUploadBudget() throws {
        let notebook = try store.createNotebook(title: "Photos")
        let assetPath = try store.storeAsset(
            try noisePNG(width: 2000, height: 2000),
            preferredName: "noise.png", notebookPath: notebook.path)
        let job = PageRenderJob(
            notebookPath: notebook.path, pageID: "p1", template: .blank, size: .infinite,
            customSize: SizePoints(width: 6000, height: 6000),
            objects: [
                PageObject(
                    type: .image, frame: PageObject.Frame(x: 0, y: 0, width: 6000, height: 6000),
                    asset: assetPath)
            ], vaultURL: store.paths.vault)

        let png = try PageRenderer.renderPNG(job)
        XCTAssertLessThanOrEqual(png.count, PageRenderer.maximumRenderBytes)
        let source = try XCTUnwrap(CGImageSourceCreateWithData(png as CFData, nil))
        let image = try XCTUnwrap(CGImageSourceCreateImageAtIndex(source, 0, nil))
        XCTAssertLessThanOrEqual(image.width, 4000)
        XCTAssertLessThanOrEqual(image.height, 4000)
        // Still square: the page shrank as a whole rather than being cropped.
        XCTAssertEqual(image.width, image.height)

        // An export is never uploaded and keeps the full pixel budget.
        let export = try PageRenderer.renderPNG(job, maximumBytes: nil)
        let exported = try XCTUnwrap(
            CGImageSourceCreateImageAtIndex(
                try XCTUnwrap(CGImageSourceCreateWithData(export as CFData, nil)), 0, nil))
        XCTAssertEqual(exported.width, 4000)
        XCTAssertGreaterThan(export.count, PageRenderer.maximumRenderBytes)
    }

    func testRenderingAnImportedPageNeverRewritesTheAsset() async throws {
        let notebook = try store.createNotebook(title: "Imports")
        let result = try PDFImporter(store: store).importPDF(
            data: try PDFFixture.threePages(), name: "source.pdf", into: notebook.id)
        let before = try XCTUnwrap(store.assets.data(forAssetPath: result.assetPath))

        let renderer = PageRenderer(paths: store.paths)
        await renderer.render(
            PageRenderJob(notebookPath: notebook.path, page: result.pages[0]))

        XCTAssertTrue(
            FileManager.default.fileExists(
                atPath: store.paths.render(notebook.path, result.pages[0].id)
                    .path(percentEncoded: false)))
        XCTAssertEqual(
            AssetStore.hash(before),
            AssetStore.hash(try XCTUnwrap(store.assets.data(forAssetPath: result.assetPath))))
    }

    /// `page.json` keeps the asset path of the package it was written in, and
    /// a rename moves the package out from under it. The render finds the
    /// image and the PDF page by name in the notebook the page is in now.
    func testImagesAndPDFPagesStillRenderAfterTheNotebookIsRenamed() throws {
        let notebook = try store.createNotebook(title: "Images")
        let pageID = try XCTUnwrap(notebook.pages.first)
        let assetPath = try store.storeAsset(
            try PageRendererObjectFixtures.redPNG(), preferredName: "swatch.png",
            notebookPath: notebook.path)
        let page = try store.setObjects(
            [
                PageObject(
                    type: .image, frame: PageObject.Frame(x: 100, y: 200, width: 120, height: 90),
                    asset: assetPath)
            ], notebookPath: notebook.path, pageID: pageID)
        let imported = try PDFImporter(store: store).importPDF(
            data: try PDFFixture.threePages(), name: "source.pdf", into: notebook.id)

        func nonPaperPixels(_ notebookPath: String, _ page: Page) throws -> Int {
            let image = try pixels(
                try PageRenderer.renderPNG(
                    PageRenderJob(notebookPath: notebookPath, page: page, vaultURL: store.paths.vault)))
            let data = image.data
            var count = 0
            for offset in stride(from: 0, to: data.count, by: 4)
            where data[offset] != 0xfd || data[offset + 1] != 0xfc || data[offset + 2] != 0xfa {
                count += 1
            }
            return count
        }
        let imageBefore = try nonPaperPixels(notebook.path, page)
        let pdfBefore = try nonPaperPixels(notebook.path, imported.pages[0])
        // The image alone is 240x180 pixels at 2x.
        XCTAssertGreaterThan(imageBefore, 240 * 180)
        XCTAssertGreaterThan(pdfBefore, 100)

        let renamed = try store.rename(notebookID: notebook.id, to: "Pictures")
        let movedPage = try store.page(notebookPath: renamed.path, id: pageID)
        let movedPDFPage = try store.page(notebookPath: renamed.path, id: imported.pages[0].id)
        // Nothing rewrote the references: they still name the old package.
        XCTAssertEqual(movedPage.objects.first?.asset, assetPath)
        XCTAssertEqual(movedPDFPage.pdf?.asset, imported.assetPath)
        XCTAssertFalse(store.assets.exists(assetPath: assetPath))

        XCTAssertEqual(try nonPaperPixels(renamed.path, movedPage), imageBefore)
        XCTAssertEqual(try nonPaperPixels(renamed.path, movedPDFPage), pdfBefore)
    }
}

// MARK: - Paper colours, covers and the dark rule

/// Decoded RGBA pixels, row 0 at the top.
struct RenderedPixels {
    let width: Int
    let height: Int
    let data: [UInt8]

    init(png: Data) throws {
        let source = try XCTUnwrap(CGImageSourceCreateWithData(png as CFData, nil))
        try self.init(image: try XCTUnwrap(CGImageSourceCreateImageAtIndex(source, 0, nil)))
    }

    init(image: CGImage) throws {
        width = image.width
        height = image.height
        var buffer = [UInt8](repeating: 0, count: width * height * 4)
        let context = try XCTUnwrap(
            CGContext(
                data: &buffer, width: width, height: height, bitsPerComponent: 8,
                bytesPerRow: width * 4, space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        data = buffer
    }

    /// Red, green and blue at (x, y), y from the top.
    subscript(x: Int, y: Int) -> (r: Int, g: Int, b: Int) {
        let offset = 4 * (y * width + x)
        return (Int(data[offset]), Int(data[offset + 1]), Int(data[offset + 2]))
    }

    /// Mean of the three channels, 0 (black) to 255 (white).
    func brightness(x: Int, y: Int) -> Int {
        let pixel = self[x, y]
        return (pixel.r + pixel.g + pixel.b) / 3
    }

    /// How many pixels inside `rect` (pixels) pass `test`.
    func count(in rect: CGRect? = nil, where test: (Int, Int, Int) -> Bool) -> Int {
        let area = (rect ?? CGRect(x: 0, y: 0, width: width, height: height)).integral
            .intersection(CGRect(x: 0, y: 0, width: width, height: height))
        guard !area.isNull else { return 0 }
        var total = 0
        for y in Int(area.minY)..<Int(area.maxY) {
            for x in Int(area.minX)..<Int(area.maxX) {
                let pixel = self[x, y]
                if test(pixel.r, pixel.g, pixel.b) { total += 1 }
            }
        }
        return total
    }
}

enum InkFixtures {
    /// One straight stroke from `start` to `end`, `width` points wide.
    static func stroke(
        from start: CGPoint, to end: CGPoint, width: CGFloat = 6, color: PlatformColor = .black
    ) -> PKStroke {
        let steps = 20
        let points = (0...steps).map { step -> PKStrokePoint in
            let t = CGFloat(step) / CGFloat(steps)
            return PKStrokePoint(
                location: CGPoint(x: start.x + (end.x - start.x) * t, y: start.y + (end.y - start.y) * t),
                timeOffset: TimeInterval(t), size: CGSize(width: width, height: width), opacity: 1,
                force: 1, azimuth: 0, altitude: .pi / 2)
        }
        return PKStroke(
            ink: PKInk(.pen, color: color),
            path: PKStrokePath(controlPoints: points, creationDate: Date()))
    }

    static func data(_ strokes: [PKStroke]) -> Data {
        PKDrawing(strokes: strokes).dataRepresentation()
    }
}

final class PageRendererColourTests: XCTestCase {
    private func render(_ job: PageRenderJob) throws -> RenderedPixels {
        try RenderedPixels(png: try PageRenderer.renderPNG(job))
    }

    private func job(
        template: Template = .blank, size: PaperSize = .a4, color: PaperColor = .white,
        drawing: Data? = nil, objects: [PageObject] = []
    ) -> PageRenderJob {
        PageRenderJob(
            notebookPath: "paper/A.paper", pageID: "p1", template: template, size: size,
            drawing: drawing, objects: objects, paperColor: color)
    }

    func testEveryPaperColourFillsThePage() throws {
        for color in PaperColor.allCases {
            let image = try render(job(color: color))
            let expected = color.rgb
            let pixel = image[40, 40]
            XCTAssertEqual(pixel.r, Int((expected.red * 255).rounded()), accuracy: 1, "\(color)")
            XCTAssertEqual(pixel.g, Int((expected.green * 255).rounded()), accuracy: 1, "\(color)")
            XCTAssertEqual(pixel.b, Int((expected.blue * 255).rounded()), accuracy: 1, "\(color)")
        }
    }

    func testRulesAreDarkOnLightPaperAndLightOnDarkPaper() throws {
        // The first rule of a ruled page is two spacings down: 45.35 pt.
        let y = Int((Template.ruled.spacing * 2 * 2).rounded())
        let light = try render(job(template: .ruled, color: .cream))
        let dark = try render(job(template: .ruled, color: .dark))
        let creamPaper = light.brightness(x: 300, y: 20)
        let darkPaper = dark.brightness(x: 300, y: 20)
        let lightRule = (y - 2...y + 2).map { light.brightness(x: 300, y: $0) }.min() ?? 255
        let darkRule = (y - 2...y + 2).map { dark.brightness(x: 300, y: $0) }.max() ?? 0
        XCTAssertLessThan(lightRule, creamPaper - 15, "black at 12 % on cream")
        XCTAssertGreaterThan(darkRule, darkPaper + 15, "white at 14 % on dark")
    }

    func testACoverIsItsColourWithOneFrame() throws {
        for color in [PaperColor.navy, .sand] {
            let image = try render(job(template: .cover, color: color))
            let paper = image.brightness(x: 20, y: 20)
            // 24 points in, one point wide: pixels 48 and 49 at 2x.
            let frame = image.brightness(x: 48, y: 400)
            let inside = image.brightness(x: 120, y: 400)
            XCTAssertEqual(inside, paper, accuracy: 1, "\(color): no artwork")
            XCTAssertEqual(image.brightness(x: 46, y: 400), paper, accuracy: 1, "\(color)")
            XCTAssertEqual(image.brightness(x: 51, y: 400), paper, accuracy: 1, "\(color)")
            if color.isDark {
                XCTAssertGreaterThan(frame, paper + 40, "white at 35 % on a dark cover")
            } else {
                XCTAssertLessThan(frame, paper - 25, "black at 20 % on a light cover")
            }
            // The frame runs right round: the top edge too.
            XCTAssertNotEqual(image.brightness(x: 300, y: 48), paper)
        }
    }

    /// The dark rule: ink is stored as its light-appearance colour and shown
    /// through the page's appearance. The same black stroke is dark on white
    /// paper and light on dark paper.
    func testBlackInkRendersLightOnDarkPaperAndDarkOnWhitePaper() throws {
        let drawing = InkFixtures.data([
            InkFixtures.stroke(from: CGPoint(x: 100, y: 300), to: CGPoint(x: 400, y: 300), width: 8)
        ])
        let white = try render(job(color: .white, drawing: drawing))
        let dark = try render(job(color: .dark, drawing: drawing))
        let strokeRow = CGRect(x: 300, y: 594, width: 300, height: 12)

        XCTAssertGreaterThan(white.count(in: strokeRow) { ($0 + $1 + $2) / 3 < 80 }, 1500)
        XCTAssertEqual(dark.count(in: strokeRow) { ($0 + $1 + $2) / 3 < 80 }, 0)
        XCTAssertGreaterThan(dark.count(in: strokeRow) { ($0 + $1 + $2) / 3 > 180 }, 1500)
        // And the paper round it is still dark.
        XCTAssertLessThan(dark.brightness(x: 300, y: 400), 60)
    }

    func testATextBoxWithNoColourFollowsThePaper() throws {
        let text = PageObject(
            type: .text, frame: PageObject.Frame(x: 60, y: 80, width: 400, height: 100),
            text: "Quarterly review", fontSize: 40)
        let frame = CGRect(x: 120, y: 160, width: 800, height: 200)
        let light = try render(job(color: .white, objects: [text]))
        let dark = try render(job(color: .dark, objects: [text]))
        XCTAssertGreaterThan(light.count(in: frame) { ($0 + $1 + $2) / 3 < 80 }, 200)
        XCTAssertGreaterThan(dark.count(in: frame) { ($0 + $1 + $2) / 3 > 200 }, 200)

        var red = text
        red.additionalFields["color"] = .string("#c0392b")
        let coloured = try render(job(color: .white, objects: [red]))
        XCTAssertGreaterThan(coloured.count(in: frame) { $0 > 150 && $1 < 90 && $2 < 90 }, 200)
    }

    // MARK: Shapes

    private func shape(
        _ kind: ShapeKind, _ frame: PageObject.Frame, stroke: String = "#1c1c1e",
        width: Double = 4, fill: String? = nil
    ) -> PageObject {
        PageObject(
            type: .shape, frame: frame,
            additionalFields: [
                "shape": .string(kind.rawValue), "stroke": .string(stroke),
                "strokeWidth": .double(width), "fill": fill.map(JSONValue.string) ?? .null,
            ])
    }

    func testARectangleIsStrokedInsideItsFrameAndFilledAtAQuarter() throws {
        let rectangle = shape(
            .rectangle, PageObject.Frame(x: 100, y: 100, width: 200, height: 100),
            stroke: "#c0392b", width: 4, fill: "#1f6feb")
        let image = try render(job(objects: [rectangle]))
        // The stroke: red, inside the frame's left edge (points 100-104).
        let edge = image[203, 300]
        XCTAssertGreaterThan(edge.r, 150)
        XCTAssertLessThan(edge.g, 100)
        // Outside the frame, paper.
        XCTAssertEqual(image.brightness(x: 196, y: 300), 251, accuracy: 2)
        // Inside, blue at 25 % over the paper.
        let inside = image[400, 300]
        let paper = PaperColor.white.rgb
        let blue = (r: 0x1f, g: 0x6f, b: 0xeb)
        XCTAssertEqual(
            inside.r, Int((Double(paper.red) * 255 * 0.75 + Double(blue.r) * 0.25).rounded()),
            accuracy: 2)
        XCTAssertEqual(
            inside.b, Int((Double(paper.blue) * 255 * 0.75 + Double(blue.b) * 0.25).rounded()),
            accuracy: 2)
    }

    func testEveryShapeKindDrawsAndALineFlipsWithANegativeWidth() throws {
        for kind in ShapeKind.allCases {
            let image = try render(
                job(objects: [shape(kind, PageObject.Frame(x: 100, y: 100, width: 200, height: 150))]))
            let marked = image.count(in: CGRect(x: 190, y: 190, width: 420, height: 320)) {
                ($0 + $1 + $2) / 3 < 100
            }
            XCTAssertGreaterThan(marked, 300, "\(kind)")
        }
        // A line runs from the frame's origin by its width and height: a
        // negative width runs it from top-right to bottom-left.
        let flipped = try render(
            job(objects: [shape(.line, PageObject.Frame(x: 300, y: 100, width: -200, height: 200))]))
        XCTAssertLessThan(flipped.brightness(x: 590, y: 210), 120, "near (300, 100)")
        XCTAssertLessThan(flipped.brightness(x: 210, y: 590), 120, "near (100, 300)")
        XCTAssertGreaterThan(flipped.brightness(x: 210, y: 210), 240, "the other diagonal is empty")
    }

    func testShapesFollowObjectOrderAndSitUnderTheInk() throws {
        let under = shape(
            .rectangle, PageObject.Frame(x: 100, y: 100, width: 200, height: 200), stroke: "#1f6feb",
            width: 2, fill: "#1f6feb")
        let over = shape(
            .rectangle, PageObject.Frame(x: 150, y: 150, width: 100, height: 100), stroke: "#c0392b",
            width: 2, fill: "#c0392b")
        let ink = InkFixtures.data([
            InkFixtures.stroke(from: CGPoint(x: 120, y: 200), to: CGPoint(x: 280, y: 200), width: 8)
        ])
        let image = try render(job(drawing: ink, objects: [under, over]))
        // Where the two fills overlap the later object shows on top: the red
        // takes blue out of the blue-only tint.
        let both = image[400, 340]
        let blueOnly = image[240, 340]
        XCTAssertLessThan(both.b, blueOnly.b - 30)
        XCTAssertLessThan(both.g, blueOnly.g - 20)
        // The ink is on top of both.
        XCTAssertLessThan(image.brightness(x: 400, y: 400), 80)
    }

    func testABlackShapeShowsLightOnDarkPaper() throws {
        let rectangle = shape(
            .rectangle, PageObject.Frame(x: 100, y: 100, width: 200, height: 100), width: 6)
        let dark = try render(job(color: .dark, objects: [rectangle]))
        XCTAssertGreaterThan(dark.brightness(x: 205, y: 300), 170)
        let light = try render(job(color: .white, objects: [rectangle]))
        XCTAssertLessThan(light.brightness(x: 205, y: 300), 60)
    }
}

// MARK: - Boards

/// A board renders its content and a margin, never its extent, and records
/// the rectangle as `render_rect`.
final class BoardRenderTests: XCTestCase {
    private var root: URL!
    private var store: PaperStore!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appending(path: "BoardRender-\(UUID().uuidString)", directoryHint: .isDirectory)
        store = try PaperStore(root: root)
    }

    override func tearDownWithError() throws {
        store = nil
        try? FileManager.default.removeItem(at: root)
    }

    private func board(
        template: Template = .grid, color: PaperColor = .cream, drawing: Data? = nil,
        objects: [PageObject] = []
    ) -> PageRenderJob {
        PageRenderJob(
            notebookPath: "paper/B.paper", pageID: "b1", template: template, size: .board,
            drawing: drawing, objects: objects, paperColor: color,
            boardExtent: SizePoints(BoardGeometry.defaultExtent))
    }

    func testAnEmptyBoardRendersTheRectangleRoundItsCentre() throws {
        let job = board()
        XCTAssertEqual(
            PageRenderer.renderArea(job), CGRect(x: 7509, y: 7680, width: 1366, height: 1024))
        let image = try RenderedPixels(png: try PageRenderer.renderPNG(job))
        XCTAssertEqual(image.width, 2732)
        XCTAssertEqual(image.height, 2048)
        // Cream paper between the lines.
        let paper = image[1380, 1000]
        XCTAssertEqual(paper.r, 0xF7, accuracy: 1)
        XCTAssertEqual(paper.b, 0xEA, accuracy: 1)
        // The grid is anchored at the board's centre, 683 points into the
        // render: that column and the next are a line at full strength,
        // black at 8.5 %, two pixels wide.
        for column in [1366, 1367] {
            let line = image[column, 1000]
            XCTAssertEqual(line.r, Int((0xF7 * (1 - BoardGeometry.gridLineAlphaLight)).rounded()), accuracy: 2)
            XCTAssertEqual(line.b, Int((0xEA * (1 - BoardGeometry.gridLineAlphaLight)).rounded()), accuracy: 2)
        }
        // Two pixels wide, and no more.
        XCTAssertEqual(image[1365, 1000].r, 0xF7, accuracy: 1)
        XCTAssertEqual(image[1368, 1000].r, 0xF7, accuracy: 1)
    }

    /// Two far corners of the board: the render spans them, inside the 8,192
    /// pixel edge and the 16-megapixel area, and both corners carry ink.
    func testABoardWithInkInTwoFarCornersRendersBothWithinTheCaps() throws {
        let drawing = InkFixtures.data([
            InkFixtures.stroke(from: CGPoint(x: 300, y: 300), to: CGPoint(x: 700, y: 700), width: 40),
            InkFixtures.stroke(
                from: CGPoint(x: 15_600, y: 15_700), to: CGPoint(x: 16_000, y: 16_000), width: 40),
        ])
        let job = board(drawing: drawing)
        let area = PageRenderer.renderArea(job)
        XCTAssertLessThanOrEqual(area.minX, 300 - 20 - 48)
        XCTAssertGreaterThanOrEqual(area.minX, 300 - 20 - 48 - 30)
        XCTAssertGreaterThanOrEqual(area.maxX, 16_000 + 20 + 48)
        XCTAssertLessThanOrEqual(area.maxX, BoardGeometry.defaultExtent.width)

        let image = try RenderedPixels(png: try PageRenderer.renderPNG(job))
        XCTAssertLessThanOrEqual(CGFloat(max(image.width, image.height)), PageRenderer.maximumPixelEdge)
        XCTAssertLessThanOrEqual(CGFloat(image.width * image.height), PageRenderer.maximumPixelArea)
        XCTAssertEqual(
            Double(image.width) / Double(image.height), Double(area.width / area.height),
            accuracy: 0.01)

        let scale = CGFloat(image.width) / area.width
        func inkNear(_ point: CGPoint) -> Int {
            let centre = CGPoint(x: (point.x - area.minX) * scale, y: (point.y - area.minY) * scale)
            return image.count(in: CGRect(x: centre.x - 20, y: centre.y - 20, width: 40, height: 40)) {
                ($0 + $1 + $2) / 3 < 100
            }
        }
        XCTAssertGreaterThan(inkNear(CGPoint(x: 500, y: 500)), 50, "top-left ink")
        XCTAssertGreaterThan(inkNear(CGPoint(x: 15_800, y: 15_850)), 50, "bottom-right ink")
        XCTAssertEqual(inkNear(CGPoint(x: 8000, y: 8000)), 0, "nothing in the middle")
    }

    func testTheRendererRecordsTheRectangleItRendered() async throws {
        let whiteboard = try store.createDocument(
            title: "Accounting", folderPath: "paper", kind: .whiteboard,
            options: .whiteboardFactory)
        let pageID = try XCTUnwrap(whiteboard.pages.first)
        let drawing = InkFixtures.data([
            InkFixtures.stroke(from: CGPoint(x: 9000, y: 9000), to: CGPoint(x: 9400, y: 9100))
        ])
        try store.writeDrawing(drawing, notebookPath: whiteboard.path, pageID: pageID)
        let before = try store.page(notebookPath: whiteboard.path, id: pageID)
        XCTAssertNil(before.renderRect)

        let renderer = PageRenderer(store: store)
        // As the controller asks after a flush: ink, template and size only.
        await renderer.render(
            PageRenderJob(
                notebookPath: whiteboard.path, pageID: pageID, template: before.template,
                size: before.size, drawing: drawing))

        let after = try store.page(notebookPath: whiteboard.path, id: pageID)
        let rect = try XCTUnwrap(after.renderRect)
        let expected = PageRenderer.renderArea(
            PageRenderJob(notebookPath: whiteboard.path, page: after, drawing: drawing))
        XCTAssertEqual(rect, PageObject.Frame(expected))
        XCTAssertLessThan(rect.width, 600)
        // Derived: the page was not modified by it, and nothing was journalled.
        XCTAssertEqual(after.modified, before.modified)
        XCTAssertEqual(after.revision, before.revision)

        let png = try Data(contentsOf: store.paths.render(whiteboard.path, pageID))
        let image = try RenderedPixels(png: png)
        XCTAssertEqual(image.width, Int(rect.width * 2))
        // Cream, from page.json, although the job did not say so.
        XCTAssertEqual(image[4, 4].b, 0xEA, accuracy: 1)

        // A notebook page keeps its whole-page render and no rectangle.
        let notebook = try store.createNotebook(title: "Pages")
        let page = try XCTUnwrap(try store.pages(ofNotebook: notebook.id).first)
        await renderer.render(PageRenderJob(notebookPath: notebook.path, page: page))
        XCTAssertNil(try store.page(notebookPath: notebook.path, id: page.id).renderRect)
    }

    func testSetRenderRectStoresAndClearsWithoutTouchingTheRest() throws {
        let whiteboard = try store.createDocument(
            title: "Board", folderPath: "paper", kind: .whiteboard, options: .whiteboardFactory)
        let pageID = try XCTUnwrap(whiteboard.pages.first)
        let before = try store.page(notebookPath: whiteboard.path, id: pageID)
        let journalled = try store.journal.pending().count
        let rect = PageObject.Frame(x: 7000, y: 7100, width: 900, height: 400)
        try store.setRenderRect(rect, notebookPath: whiteboard.path, pageID: pageID)
        let stored = try store.page(notebookPath: whiteboard.path, id: pageID)
        XCTAssertEqual(stored.renderRect, rect)
        XCTAssertEqual(stored.modified, before.modified)
        XCTAssertEqual(try store.journal.pending().count, journalled)
        // On disk as `render_rect`, and stripped from the page part.
        let json = try JSONSerialization.jsonObject(
            with: Data(contentsOf: store.paths.pageJSON(whiteboard.path, pageID))) as? [String: Any]
        XCTAssertNotNil(json?["render_rect"])
        try store.setRenderRect(nil, notebookPath: whiteboard.path, pageID: pageID)
        XCTAssertNil(try store.page(notebookPath: whiteboard.path, id: pageID).renderRect)
    }

    func testABoardExportsAsOnePageCoveringItsRenderRectangle() throws {
        let drawing = InkFixtures.data([
            InkFixtures.stroke(from: CGPoint(x: 8000, y: 8000), to: CGPoint(x: 8600, y: 8300))
        ])
        let first = board(drawing: drawing)
        let second = board(template: .dotted, color: .dark)
        let pdf = try PDFExporter.pdf([first, second])
        let document = try XCTUnwrap(PDFDocument(data: pdf))
        XCTAssertEqual(document.pageCount, 2)
        let box = try XCTUnwrap(document.page(at: 0)).bounds(for: .mediaBox)
        let area = PageRenderer.renderArea(first)
        XCTAssertEqual(box.width, area.width, accuracy: 0.5)
        XCTAssertEqual(box.height, area.height, accuracy: 0.5)
        let empty = try XCTUnwrap(document.page(at: 1)).bounds(for: .mediaBox)
        XCTAssertEqual(empty.size, BoardGeometry.emptyRenderSize)

        // The ink is there, at the place it was written.
        let page = try XCTUnwrap(document.page(at: 0))
        let thumbnail = page.thumbnail(of: CGSize(width: box.width, height: box.height), for: .mediaBox)
        var proposed = CGRect(origin: .zero, size: thumbnail.size)
        let cg = try XCTUnwrap(thumbnail.cgImage(forProposedRect: &proposed, context: nil, hints: nil))
        let image = try RenderedPixels(image: cg)
        let sx = CGFloat(image.width) / area.width
        let sy = CGFloat(image.height) / area.height
        let middle = CGPoint(x: (8300 - area.minX) * sx, y: (8150 - area.minY) * sy)
        XCTAssertLessThan(image.brightness(x: Int(middle.x), y: Int(middle.y)), 100)

        // A board PNG export is the same rectangle at three times.
        let png = try RenderedPixels(png: try PDFExporter.png(first))
        XCTAssertEqual(png.width, Int(area.width * 3))
    }

    /// The owner's screenshot at 56 %: lines 14 points apart on screen, every
    /// other one stronger, on cream.
    func testTheSquareGridAtFiftySixPercentMatchesTheScreenshot() throws {
        let zoom: CGFloat = 0.56
        let pixelsPerPoint: CGFloat = 2 * zoom
        // A window 600 x 300 points of board centred on the board's centre.
        let extent = BoardGeometry.defaultExtent
        let visible = CGRect(x: extent.width / 2 - 300, y: extent.height / 2 - 150, width: 600, height: 300)
        let width = Int(visible.width * pixelsPerPoint)
        let height = Int(visible.height * pixelsPerPoint)
        let context = try XCTUnwrap(
            CGContext(
                data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.translateBy(x: 0, y: CGFloat(height))
        context.scaleBy(x: pixelsPerPoint, y: -pixelsPerPoint)
        context.translateBy(x: -visible.minX, y: -visible.minY)
        BoardGeometry.drawBackground(
            template: .grid, paperColor: .cream, extent: extent, visibleRect: visible, zoom: zoom,
            pixelsPerPoint: pixelsPerPoint, snapToPixels: true, in: context)
        let image = try RenderedPixels(image: try XCTUnwrap(context.makeImage()))

        // Walk one row and find the lines: darker than the paper.
        let row = 37
        let darkening = (0..<width).map { 0xF7 - image[$0, row].r }
        // A line is a run of darkened pixels, two wide; its strength is the
        // darkening summed across the run and its place the run's middle.
        var lines: [(centre: CGFloat, sum: Int)] = []
        var column = 0
        while column < width {
            if darkening[column] > 1 {
                var run = [column]
                while column + 1 < width, darkening[column + 1] > 1 {
                    column += 1
                    run.append(column)
                }
                lines.append((CGFloat(run.first! + run.last!) / 2, run.map { darkening[$0] }.reduce(0, +)))
            }
            column += 1
        }
        XCTAssertGreaterThan(lines.count, 20)
        let gaps = zip(lines, lines.dropFirst()).map { ($1.centre - $0.centre) / 2 }
        for gap in gaps {
            // 25 points at 56 % is 14 screen points, 28 pixels at 2x.
            XCTAssertEqual(gap, 14, accuracy: 0.6)
        }
        // Every other line is stronger: the 50-point level at full strength
        // (black at 8.5 % over two pixels, which at the screenshot's
        // 2,000-pixel width is the owner's 31 levels summed across the line)
        // between 25-point lines at 42 %.
        let strengths = lines.map(\.sum)
        let strong = strengths.max() ?? 0
        let weak = strengths.min() ?? 0
        XCTAssertEqual(
            strong,
            Int((0xF7 * BoardGeometry.gridLineAlphaLight * BoardGeometry.gridLinePixels).rounded()),
            accuracy: 3)
        XCTAssertEqual(Double(weak) / Double(strong), 0.42, accuracy: 0.1)
        for (index, value) in strengths.enumerated() {
            let neighbour = strengths[index % 2 == 0 ? min(index + 1, strengths.count - 1) : index - 1]
            XCTAssertNotEqual(value > weak + 3, neighbour > weak + 3, "lines alternate")
        }
    }
}
