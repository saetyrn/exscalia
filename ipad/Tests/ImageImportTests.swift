import CoreGraphics
import UIKit
import XCTest

@testable import PaperUI

/// A portrait camera photo is a landscape bitmap flagged `.right`. It used to
/// be stored with `pngData()`, which kept the bitmap and dropped the flag, so
/// the photo landed on the page sideways and weighed tens of megabytes.
final class ImageImportTests: XCTestCase {
    func testAPortraitPhotoIsTurnedUprightDownsampledAndStoredAsJPEG() throws {
        let sensor = try XCTUnwrap(Self.bitmap(width: 4000, height: 3000, alpha: false))

        let result = try XCTUnwrap(
            ImageImport.normalized(cgImage: sensor, orientation: .right, scale: 1))

        XCTAssertEqual(result.extension, "jpg")
        XCTAssertEqual(Array(result.data.prefix(2)), [0xFF, 0xD8])
        let stored = try XCTUnwrap(UIImage(data: result.data))
        XCTAssertEqual(stored.imageOrientation, .up)
        let pixels = try XCTUnwrap(stored.cgImage)
        // Upright means the axes are swapped, and the long edge is capped.
        XCTAssertEqual(pixels.width, 2250)
        XCTAssertEqual(pixels.height, 3000)
    }

    func testTheRotationIsInThePixels() throws {
        // Left half red, right half blue, as the sensor saw it. Turned `.right`
        // (90 degrees clockwise) the red half is on top.
        let sensor = try XCTUnwrap(Self.bitmap(width: 400, height: 200, alpha: false) { context in
            context.setFillColor(UIColor.red.cgColor)
            context.fill(CGRect(x: 0, y: 0, width: 200, height: 200))
            context.setFillColor(UIColor.blue.cgColor)
            context.fill(CGRect(x: 200, y: 0, width: 200, height: 200))
        })

        let result = try XCTUnwrap(
            ImageImport.normalized(cgImage: sensor, orientation: .right, scale: 1))
        let stored = try XCTUnwrap(UIImage(data: result.data)?.cgImage)

        XCTAssertEqual(stored.width, 200)
        XCTAssertEqual(stored.height, 400)
        let top = try XCTUnwrap(Self.pixel(of: stored, x: 100, y: 50))
        let bottom = try XCTUnwrap(Self.pixel(of: stored, x: 100, y: 350))
        XCTAssertGreaterThan(top.red, 200)
        XCTAssertLessThan(top.blue, 60)
        XCTAssertGreaterThan(bottom.blue, 200)
        XCTAssertLessThan(bottom.red, 60)
    }

    func testAnImageThatCanBeTransparentStaysPNG() throws {
        let clear = try XCTUnwrap(Self.bitmap(width: 300, height: 200, alpha: true))

        let result = try XCTUnwrap(
            ImageImport.normalized(cgImage: clear, orientation: .up, scale: 2))

        XCTAssertEqual(result.extension, "png")
        XCTAssertEqual(Array(result.data.prefix(4)), [0x89, 0x50, 0x4E, 0x47])
        // Small images keep their size; nothing is upscaled.
        let stored = try XCTUnwrap(UIImage(data: result.data)?.cgImage)
        XCTAssertEqual(stored.width, 300)
        XCTAssertEqual(stored.height, 200)
    }

    // MARK: - Helpers

    private static func bitmap(
        width: Int, height: Int, alpha: Bool, draw: ((CGContext) -> Void)? = nil
    ) -> CGImage? {
        guard
            let context = CGContext(
                data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                space: CGColorSpace(name: CGColorSpace.sRGB)!,
                bitmapInfo: alpha
                    ? CGImageAlphaInfo.premultipliedLast.rawValue
                    : CGImageAlphaInfo.noneSkipLast.rawValue)
        else { return nil }
        if let draw {
            draw(context)
        } else if !alpha {
            context.setFillColor(UIColor.gray.cgColor)
            context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        }
        return context.makeImage()
    }

    /// One pixel, read through an sRGB RGBA bitmap so JPEG's own layout does
    /// not matter. `y` counts from the top.
    private static func pixel(of image: CGImage, x: Int, y: Int) -> (red: UInt8, blue: UInt8)? {
        var bytes = [UInt8](repeating: 0, count: 4)
        guard
            let context = CGContext(
                data: &bytes, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 4,
                space: CGColorSpace(name: CGColorSpace.sRGB)!,
                bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
        else { return nil }
        // Core Graphics is y-up: shift the image so the wanted pixel sits on
        // the context's only one.
        context.draw(
            image,
            in: CGRect(
                x: -x, y: -(image.height - 1 - y), width: image.width, height: image.height))
        return (bytes[0], bytes[2])
    }
}
