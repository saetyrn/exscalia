import CoreGraphics
import Foundation
import ImageIO
import PencilKit
import XCTest

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
                XCTAssertEqual(image.width, Int(size.points.width * 2))
                XCTAssertEqual(image.height, Int(size.points.height * 2))
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
