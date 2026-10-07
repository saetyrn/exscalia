import CoreGraphics
import Foundation
import ImageIO
import PencilKit
import UniformTypeIdentifiers
import XCTest

@testable import PaperCore

/// A small multi-page PDF built with the exporter itself, so the import tests
/// have something real to read without a binary fixture in the repository.
enum PDFFixture {
    static let pageTexts = ["Alpha page one", "Beta page two", "Gamma page three"]

    /// Three pages, each a different size, each carrying a line of text.
    static func threePages() throws -> Data {
        let sizes = [
            CGSize(width: 595, height: 842), CGSize(width: 612, height: 792),
            CGSize(width: 400, height: 400),
        ]
        let jobs = zip(sizes, pageTexts).map { size, text in
            PageRenderJob(
                notebookPath: "paper/Fixture.paper", pageID: PaperID.make(), template: .blank,
                size: .a4,
                customSize: SizePoints(size),
                objects: [
                    PageObject(
                        type: .text,
                        frame: PageObject.Frame(x: 40, y: 40, width: Double(size.width) - 80, height: 120),
                        text: text, fontSize: 24)
                ])
        }
        return try PDFExporter.pdf(jobs)
    }
}

/// Ink and an image, shared by the tests that check what lands on a page.
enum PageRendererObjectFixtures {
    /// One dark diagonal stroke, long enough that a pixel count is unambiguous.
    static func strokeData() -> Data {
        let points = stride(from: 0.0, through: 200.0, by: 10.0).map { offset in
            PKStrokePoint(
                location: CGPoint(x: 120 + offset, y: 300 + offset), timeOffset: offset / 200,
                size: CGSize(width: 6, height: 6), opacity: 1, force: 1, azimuth: 0,
                altitude: .pi / 2)
        }
        return PKDrawing(strokes: [
            PKStroke(
                ink: PKInk(.pen, color: .black),
                path: PKStrokePath(controlPoints: points, creationDate: Date()))
        ]).dataRepresentation()
    }

    /// A solid red PNG, so "the image landed" is a colour test rather than a
    /// guess about anti-aliasing.
    static func redPNG(width: Int = 64, height: Int = 64) throws -> Data {
        let context = try XCTUnwrap(
            CGContext(
                data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.setFillColor(red: 1, green: 0, blue: 0, alpha: 1)
        context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        let image = try XCTUnwrap(context.makeImage())
        let data = NSMutableData()
        let destination = try XCTUnwrap(
            CGImageDestinationCreateWithData(data as CFMutableData, UTType.png.identifier as CFString, 1, nil))
        CGImageDestinationAddImage(destination, image, nil)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
        return data as Data
    }
}
