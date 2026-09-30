import CoreGraphics
import CoreText
import Foundation
import ImageIO
import PDFKit
import PencilKit
import UniformTypeIdentifiers

#if canImport(UIKit)
    import UIKit
#elseif canImport(AppKit)
    import AppKit
#endif

/// Everything needed to draw a page without touching the store.
public struct PageRenderJob: Sendable {
    public var notebookPath: String
    public var pageID: String
    public var template: Template
    public var size: PaperSize
    public var drawing: Data?
    /// The media box of an imported PDF page, when the page is not a preset.
    public var customSize: SizePoints?
    /// Text and image objects, drawn under the ink.
    public var objects: [PageObject]
    /// The PDF page this page annotates, if any.
    public var pdf: PDFBinding?
    /// Root of the local vault, used to resolve the vault-relative asset paths
    /// the objects and the PDF binding carry. Filled in by the renderer when a
    /// caller leaves it nil.
    public var vaultURL: URL?

    public init(
        notebookPath: String, pageID: String, template: Template, size: PaperSize,
        drawing: Data? = nil, customSize: SizePoints? = nil, objects: [PageObject] = [],
        pdf: PDFBinding? = nil, vaultURL: URL? = nil
    ) {
        self.notebookPath = notebookPath
        self.pageID = pageID
        self.template = template
        self.size = size
        self.drawing = drawing
        self.customSize = customSize
        self.objects = objects
        self.pdf = pdf
        self.vaultURL = vaultURL
    }

    /// Everything a page needs to be drawn, taken straight off a `Page`.
    public init(notebookPath: String, page: Page, drawing: Data? = nil, vaultURL: URL? = nil) {
        self.init(
            notebookPath: notebookPath, pageID: page.id, template: page.template, size: page.size,
            drawing: drawing, customSize: page.customSize, objects: page.objects, pdf: page.pdf,
            vaultURL: vaultURL)
    }

    /// The page's real size in points.
    public var pointSize: CGSize { customSize?.cgSize ?? size.points }
}

public enum PageRenderError: Error, Equatable, Sendable {
    case contextUnavailable
    case encodingFailed
}

/// Turns a page into `render.png` (2x) and a small cover JPEG.
///
/// Renders are derived data: losing one costs a redraw, never ink. They are
/// therefore coalesced per page and skipped entirely while the page is dirty,
/// so a writing session does no rendering work at all until the pen stops.
public actor PageRenderer {
    /// Renders are 2x so they stay legible when the web viewer shows them at
    /// page width on a Retina display.
    public static let scale: CGFloat = 2
    /// Long edge of the library cover thumbnail.
    public static let coverLongEdge: CGFloat = 400

    private let paths: PaperPaths
    private let isDirty: @Sendable (String) -> Bool
    private var running: Set<String> = []
    private var queued: [String: PageRenderJob] = [:]

    public init(paths: PaperPaths, isDirty: @escaping @Sendable (String) -> Bool = { _ in false }) {
        self.paths = paths
        self.isDirty = isDirty
    }

    /// Renders `job` unless the page is dirty, coalescing repeat requests for
    /// the same page into one run.
    public func render(_ input: PageRenderJob) async {
        var job = input
        if job.vaultURL == nil { job.vaultURL = paths.vault }
        // Objects and PDF bindings live in `page.json`, which is the truth, so
        // a caller that only knows about ink still gets a render that matches
        // the screen.
        if job.objects.isEmpty, job.pdf == nil,
            let page = try? AtomicFile.readJSON(
                Page.self, at: paths.pageJSON(job.notebookPath, job.pageID))
        {
            job.objects = page.objects
            job.pdf = page.pdf
            if job.customSize == nil { job.customSize = page.customSize }
        }
        guard !isDirty(job.pageID) else { return }
        if running.contains(job.pageID) {
            queued[job.pageID] = job
            return
        }
        running.insert(job.pageID)
        defer { running.remove(job.pageID) }

        do {
            let png = try Self.renderPNG(job)
            try AtomicFile.write(png, to: paths.render(job.notebookPath, job.pageID))
            if let cover = Self.coverJPEG(from: png) {
                try? AtomicFile.write(cover, to: Self.coverURL(pageID: job.pageID), protection: false)
            }
        } catch {
            // Derived data: a failed render is retried on the next flush.
        }

        if let next = queued.removeValue(forKey: job.pageID) {
            await render(next)
        }
    }

    /// Where the library looks for a cover. `Caches/` because it is
    /// regenerable and the system may reclaim it.
    public nonisolated static func coverURL(pageID: String) -> URL {
        let base =
            (try? FileManager.default.url(
                for: .cachesDirectory, in: .userDomainMask, appropriateFor: nil, create: true))
            ?? URL(fileURLWithPath: NSTemporaryDirectory())
        return base.appending(path: "PaperCovers", directoryHint: .isDirectory)
            .appending(path: "\(pageID).jpg", directoryHint: .notDirectory)
    }

    // MARK: - Drawing

    /// Template, then ink, at `scale`. Synchronous and free of the store so the
    /// tests can call it directly.
    ///
    /// `maximumBytes` is the upload budget: the server refuses a larger render
    /// with 413, and the ink sent beside it with it. A page of photograph or
    /// scan compresses badly, so a PNG over the budget is drawn again at a
    /// smaller scale until it fits. An export, which is never uploaded, passes
    /// nil and keeps every pixel.
    public nonisolated static func renderPNG(
        _ job: PageRenderJob, scale: CGFloat = PageRenderer.scale,
        maximumBytes: Int? = PageRenderer.maximumRenderBytes
    ) throws -> Data {
        // An infinite page has no upper bound on its extent, so the render
        // scale does. Beyond `maximumPixelEdge` or `maximumPixelArea` the
        // picture is scaled down rather than refused: a big page still gets a
        // thumbnail, a cover and an export, just at fewer pixels per point.
        var scale = cappedScale(scale, for: job.pointSize)
        var png = try autoreleasepool { try drawPNG(job, scale: scale) }
        guard let maximumBytes else { return png }
        var attempts = 0
        while png.count > maximumBytes, attempts < 3, scale > minimumScale {
            let shrink = (CGFloat(maximumBytes) / CGFloat(png.count)).squareRoot() * 0.9
            scale = max(scale * shrink, minimumScale)
            png = try autoreleasepool { try drawPNG(job, scale: scale) }
            attempts += 1
        }
        return png
    }

    private nonisolated static func drawPNG(_ job: PageRenderJob, scale: CGFloat) throws -> Data {
        let size = job.pointSize
        let pixelWidth = Int((size.width * scale).rounded())
        let pixelHeight = Int((size.height * scale).rounded())
        guard pixelWidth > 0, pixelHeight > 0 else { throw PageRenderError.contextUnavailable }

        guard
            let context = CGContext(
                data: nil, width: pixelWidth, height: pixelHeight, bitsPerComponent: 8,
                bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
        else { throw PageRenderError.contextUnavailable }

        // CoreGraphics is y-up; pages are y-down. Flip once, here, so every
        // caller below can think in page points.
        context.translateBy(x: 0, y: CGFloat(pixelHeight))
        context.scaleBy(x: scale, y: -scale)
        compose(job, in: context, inkScale: scale)

        guard let image = context.makeImage() else { throw PageRenderError.contextUnavailable }
        return try encode(image, as: UTType.png.identifier, quality: 1)
    }

    /// The longest edge any render may have, in pixels.
    public static let maximumPixelEdge: CGFloat = 8192
    /// The most pixels any render may have. The long edge alone still lets a
    /// page grown in both directions reach 8192x8192 -- 67 MP, a quarter of a
    /// gigabyte for the context and as much again for the ink image beside it.
    public static let maximumPixelArea: CGFloat = 16_000_000
    /// Mirrors the server's `MAX_RENDER_BYTES`, the way `AssetStore.maxBytes`
    /// mirrors `MAX_ASSET_BYTES`.
    public static let maximumRenderBytes = 8 * 1024 * 1024
    private static let minimumScale: CGFloat = 0.05

    /// The scale a page of this size may actually be drawn at.
    public nonisolated static func cappedScale(_ scale: CGFloat, for size: CGSize) -> CGFloat {
        let longEdge = max(size.width, size.height)
        guard longEdge > 0, scale > 0 else { return scale }
        var cap = maximumPixelEdge / longEdge
        let area = size.width * size.height
        if area > 0 { cap = min(cap, (maximumPixelArea / area).squareRoot()) }
        guard scale > cap else { return scale }
        return max(cap, minimumScale)
    }

    /// Draws a whole page into a context already in page points with y running
    /// downwards from the top-left corner.
    ///
    /// One function, used by the on-device render, by the PDF export and by the
    /// tests, because "the export looks different from the screen" is exactly
    /// the class of bug that costs a person their trust in the app. Order is
    /// fixed: template, PDF background, images, text, ink on top.
    public nonisolated static func compose(
        _ job: PageRenderJob, in context: CGContext, inkScale: CGFloat
    ) {
        let size = job.pointSize
        TemplateGeometry.draw(template: job.template, size: size, in: context)
        drawPDFBackground(job, size: size, in: context)

        for object in job.objects where object.type == .image {
            drawImageObject(object, job: job, in: context)
        }
        for object in job.objects where object.type == .text {
            drawTextObject(object, in: context)
        }

        if let data = job.drawing, !data.isEmpty,
            let drawing = try? PKDrawing(data: data),
            let ink = inkImage(drawing, size: size, scale: inkScale)
        {
            context.saveGState()
            context.translateBy(x: 0, y: size.height)
            context.scaleBy(x: 1, y: -1)
            context.draw(ink, in: CGRect(origin: .zero, size: size))
            context.restoreGState()
        }
    }

    /// The imported page, drawn as vectors so exported text stays selectable.
    /// The original PDF bytes are only ever read.
    private nonisolated static func drawPDFBackground(
        _ job: PageRenderJob, size: CGSize, in context: CGContext
    ) {
        guard let binding = job.pdf, let vaultURL = job.vaultURL else { return }
        let url = assetURL(binding.asset, notebookPath: job.notebookPath, vaultURL: vaultURL)
        guard let document = PDFDocument(url: url),
            binding.pageIndex >= 0, binding.pageIndex < document.pageCount,
            let page = document.page(at: binding.pageIndex)
        else { return }

        let bounds = page.bounds(for: .mediaBox)
        guard bounds.width > 0, bounds.height > 0 else { return }
        context.saveGState()
        // PDF space is y-up; step back into it for the duration of the draw.
        context.translateBy(x: 0, y: size.height)
        context.scaleBy(x: 1, y: -1)
        // A page whose media box is not the page size is scaled to fit rather
        // than cropped: losing part of an imported page is worse than a scale.
        let rotated = page.rotation % 180 == 0
            ? bounds.size
            : CGSize(width: bounds.height, height: bounds.width)
        context.scaleBy(x: size.width / rotated.width, y: size.height / rotated.height)
        page.draw(with: .mediaBox, to: context)
        context.restoreGState()
    }

    private nonisolated static func drawImageObject(
        _ object: PageObject, job: PageRenderJob, in context: CGContext
    ) {
        guard let assetPath = object.asset, let vaultURL = job.vaultURL else { return }
        let url = assetURL(assetPath, notebookPath: job.notebookPath, vaultURL: vaultURL)
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
            var image = CGImageSourceCreateImageAtIndex(source, 0, nil)
        else { return }
        if object.crop != nil {
            let rect = object.cropRect(
                in: CGSize(width: image.width, height: image.height))
            if let cropped = image.cropping(to: rect.integral) { image = cropped }
        }
        let rect = object.frame.rect
        guard rect.width > 0, rect.height > 0 else { return }
        context.saveGState()
        context.translateBy(x: rect.minX, y: rect.maxY)
        context.scaleBy(x: 1, y: -1)
        context.interpolationQuality = .high
        context.draw(image, in: CGRect(origin: .zero, size: rect.size))
        context.restoreGState()
    }

    private nonisolated static func drawTextObject(_ object: PageObject, in context: CGContext) {
        guard let text = object.text, !text.isEmpty else { return }
        let rect = object.frame.rect
        guard rect.width > 0, rect.height > 0 else { return }
        let attributed = attributedText(text, size: object.resolvedFontSize)
        let framesetter = CTFramesetterCreateWithAttributedString(attributed)
        let path = CGPath(rect: CGRect(origin: .zero, size: rect.size), transform: nil)
        let frame = CTFramesetterCreateFrame(
            framesetter, CFRangeMake(0, attributed.length), path, nil)

        context.saveGState()
        context.translateBy(x: rect.minX, y: rect.maxY)
        context.scaleBy(x: 1, y: -1)
        context.textMatrix = .identity
        CTFrameDraw(frame, context)
        context.restoreGState()
    }

    /// The one place text style is decided, so the screen and the render agree.
    public nonisolated static func attributedText(_ text: String, size: CGFloat)
        -> NSAttributedString
    {
        let font =
            CTFontCreateUIFontForLanguage(.system, size, nil)
            ?? CTFontCreateWithName("Helvetica" as CFString, size, nil)
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineBreakMode = .byWordWrapping
        let color = CGColor(
            colorSpace: CGColorSpaceCreateDeviceRGB(),
            components: [0x1c / 255.0, 0x1c / 255.0, 0x1e / 255.0, 1.0])
        var attributes: [NSAttributedString.Key: Any] = [
            .init(kCTFontAttributeName as String): font,
            .init(kCTParagraphStyleAttributeName as String): paragraph,
        ]
        if let color { attributes[.init(kCTForegroundColorAttributeName as String)] = color }
        return NSAttributedString(string: text, attributes: attributes)
    }

    /// The file an asset reference names or, when the notebook has been
    /// renamed, moved or duplicated since the reference was written, the file
    /// of the same content-hashed name in the notebook the page is in now --
    /// the rule `AssetStore.url(forAssetPath:inNotebook:)` follows.
    private nonisolated static func assetURL(
        _ assetPath: String, notebookPath: String, vaultURL: URL
    ) -> URL {
        let literal = fileURL(assetPath.split(separator: "/"), under: vaultURL)
        guard !FileManager.default.fileExists(atPath: literal.path(percentEncoded: false)),
            let filename = assetPath.split(separator: "/").last
        else { return literal }
        return fileURL(notebookPath.split(separator: "/") + ["assets", filename], under: vaultURL)
    }

    private nonisolated static func fileURL(_ components: [Substring], under vaultURL: URL) -> URL {
        // The last component is a file: a directory hint would leave a trailing
        // slash that `fileExists` refuses to match.
        let parts = components.filter { $0 != ".." }
        var url = vaultURL
        for component in parts.dropLast() {
            url = url.appending(path: String(component), directoryHint: .isDirectory)
        }
        guard let last = parts.last else { return url }
        return url.appending(path: String(last), directoryHint: .notDirectory)
    }

    /// PencilKit picks ink colours from the current trait collection, so a dark
    /// appearance would render black ink as white. Every render is forced light.
    static func inkImage(_ drawing: PKDrawing, size: CGSize, scale: CGFloat = PageRenderer.scale)
        -> CGImage?
    {
        let bounds = CGRect(origin: .zero, size: size)
        #if canImport(UIKit)
            var image: UIImage?
            UITraitCollection(userInterfaceStyle: .light).performAsCurrent {
                image = drawing.image(from: bounds, scale: scale)
            }
            return image?.cgImage
        #elseif canImport(AppKit)
            // Same trap on the Mac, where PaperCore's tests run: under a dark
            // appearance PencilKit renders black ink white.
            var image: NSImage?
            if let aqua = NSAppearance(named: .aqua) {
                aqua.performAsCurrentDrawingAppearance {
                    image = drawing.image(from: bounds, scale: scale)
                }
            } else {
                image = drawing.image(from: bounds, scale: scale)
            }
            var rect = bounds
            return image?.cgImage(forProposedRect: &rect, context: nil, hints: nil)
        #else
            return nil
        #endif
    }

    /// Small JPEG for the library grid, made from the render rather than a
    /// second pass over the ink.
    public nonisolated static func coverJPEG(from png: Data) -> Data? {
        guard let source = CGImageSourceCreateWithData(png as CFData, nil),
            let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
        else { return nil }
        let longEdge = CGFloat(max(image.width, image.height))
        let factor = min(1, coverLongEdge / max(longEdge, 1))
        let width = max(1, Int((CGFloat(image.width) * factor).rounded()))
        let height = max(1, Int((CGFloat(image.height) * factor).rounded()))
        guard
            let context = CGContext(
                data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
        else { return nil }
        context.interpolationQuality = .high
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        guard let scaled = context.makeImage() else { return nil }
        return try? encode(scaled, as: UTType.jpeg.identifier, quality: 0.8)
    }

    private nonisolated static func encode(_ image: CGImage, as type: String, quality: CGFloat)
        throws -> Data
    {
        let data = NSMutableData()
        guard
            let destination = CGImageDestinationCreateWithData(
                data as CFMutableData, type as CFString, 1, nil)
        else { throw PageRenderError.encodingFailed }
        CGImageDestinationAddImage(
            destination, image, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
        guard CGImageDestinationFinalize(destination) else { throw PageRenderError.encodingFailed }
        return data as Data
    }
}
