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
    /// The sheet's colour. A dark one is drawn with the dark appearance
    /// (`PaperAppearance`). The renderer takes it from `page.json` when it can.
    public var paperColor: PaperColor
    /// A board's extent; `nil` for the default, and ignored on a page.
    public var boardExtent: SizePoints?

    public init(
        notebookPath: String, pageID: String, template: Template, size: PaperSize,
        drawing: Data? = nil, customSize: SizePoints? = nil, objects: [PageObject] = [],
        pdf: PDFBinding? = nil, vaultURL: URL? = nil, paperColor: PaperColor = .white,
        boardExtent: SizePoints? = nil
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
        self.paperColor = paperColor
        self.boardExtent = boardExtent
    }

    /// Everything a page needs to be drawn, taken straight off a `Page`.
    public init(notebookPath: String, page: Page, drawing: Data? = nil, vaultURL: URL? = nil) {
        self.init(
            notebookPath: notebookPath, pageID: page.id, template: page.template, size: page.size,
            drawing: drawing, customSize: page.customSize, objects: page.objects, pdf: page.pdf,
            vaultURL: vaultURL, paperColor: page.paperColor, boardExtent: page.boardExtent)
    }

    /// Whether this is a whiteboard's board, which renders its content rather
    /// than its whole extent.
    public var isBoard: Bool { size == .board }

    /// Whether the page is drawn with the dark appearance (§3.4).
    public var isDark: Bool { paperColor.isDark }

    /// The page's real size in points; a board's extent.
    public var pointSize: CGSize {
        if isBoard {
            return Page(id: "", size: .board, boardExtent: boardExtent).resolvedBoardExtent.cgSize
        }
        return customSize?.cgSize ?? size.points
    }
}

public enum PageRenderError: Error, Equatable, Sendable {
    case contextUnavailable
    case encodingFailed
    /// The page is not where the job said and nowhere else, or is no longer
    /// what was drawn: nothing was written.
    case pageGone
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
    /// Records a board's `render_rect` beside its render. Without a store (the
    /// `paths:` initialiser) boards still render, and nothing is recorded.
    private let store: PaperStore?
    private let isDirty: @Sendable (String) -> Bool
    private var running: Set<String> = []
    private var queued: [String: PageRenderJob] = [:]

    public init(paths: PaperPaths, isDirty: @escaping @Sendable (String) -> Bool = { _ in false }) {
        self.paths = paths
        self.store = nil
        self.isDirty = isDirty
    }

    /// A renderer that also records each board's `render_rect` in its
    /// `page.json`, which is what the page PUT sends beside the render.
    public init(store: PaperStore, isDirty: @escaping @Sendable (String) -> Bool = { _ in false }) {
        self.paths = store.paths
        self.store = store
        self.isDirty = isDirty
    }

    /// Renders `job` unless the page is dirty, coalescing repeat requests for
    /// the same page into one run.
    public func render(_ input: PageRenderJob) async {
        var job = input
        if job.vaultURL == nil { job.vaultURL = paths.vault }
        // Objects, PDF bindings, the paper colour and a board's extent live in
        // `page.json`, which is the truth, so a caller that only knows about
        // ink still gets a render that matches the screen.
        if let page = try? AtomicFile.readJSON(
            Page.self, at: paths.pageJSON(job.notebookPath, job.pageID))
        {
            if job.objects.isEmpty, job.pdf == nil {
                job.objects = page.objects
                job.pdf = page.pdf
                if job.customSize == nil { job.customSize = page.customSize }
            }
            job.paperColor = page.paperColor
            if job.boardExtent == nil { job.boardExtent = page.boardExtent }
        }
        guard !isDirty(job.pageID) else { return }
        if running.contains(job.pageID) {
            queued[job.pageID] = job
            return
        }
        running.insert(job.pageID)
        defer { running.remove(job.pageID) }

        do {
            let drawing = Self.decodedDrawing(job)
            let area = Self.renderArea(job, drawing: drawing)
            let png = try Self.renderPNG(job, drawing: drawing, area: area)
            if let store {
                // Where the page is now, and still what was drawn; with the
                // rectangle before the caller journals the render, since the
                // push reads it out of `page.json` beside the picture.
                guard
                    try store.storeRender(
                        png, notebookPath: job.notebookPath, pageID: job.pageID,
                        isBoard: job.isBoard, area: PageObject.Frame(area)) != nil
                else { throw PageRenderError.pageGone }
            } else {
                try AtomicFile.write(png, to: paths.render(job.notebookPath, job.pageID))
            }
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
        let drawing = decodedDrawing(job)
        return try renderPNG(
            job, drawing: drawing, area: renderArea(job, drawing: drawing), scale: scale,
            maximumBytes: maximumBytes)
    }

    /// The part of the page a render or an export shows, in page points: the
    /// whole page, or for a board its content and a 48-point margin
    /// (`BoardGeometry.renderRect`), which is what `render_rect` records.
    public nonisolated static func renderArea(_ job: PageRenderJob) -> CGRect {
        renderArea(job, drawing: decodedDrawing(job))
    }

    static func renderArea(_ job: PageRenderJob, drawing: PKDrawing?) -> CGRect {
        guard job.isBoard else { return CGRect(origin: .zero, size: job.pointSize) }
        let ink = drawing.flatMap { $0.strokes.isEmpty ? nil : $0.bounds }
        return BoardGeometry.renderRect(
            content: BoardGeometry.contentBounds(ink: ink, objects: job.objects),
            extent: job.pointSize)
    }

    /// The job's ink, decoded once for everything that needs it.
    static func decodedDrawing(_ job: PageRenderJob) -> PKDrawing? {
        guard let data = job.drawing, !data.isEmpty else { return nil }
        return try? PKDrawing(data: data)
    }

    static func renderPNG(
        _ job: PageRenderJob, drawing: PKDrawing?, area: CGRect,
        scale requested: CGFloat = PageRenderer.scale,
        maximumBytes: Int? = PageRenderer.maximumRenderBytes
    ) throws -> Data {
        // An infinite page has no upper bound on its extent, and a board's
        // content can span it, so the render scale does. Beyond
        // `maximumPixelEdge` or `maximumPixelArea` the picture is scaled down
        // rather than refused: a big page still gets a thumbnail, a cover and
        // an export, just at fewer pixels per point.
        var scale = cappedScale(requested, for: area.size)
        func draw() throws -> Data {
            try autoreleasepool {
                try drawPNG(job, drawing: drawing, area: area, scale: scale, requested: requested)
            }
        }
        var png = try draw()
        guard let maximumBytes else { return png }
        var attempts = 0
        while png.count > maximumBytes, attempts < 3, scale > minimumScale {
            let shrink = (CGFloat(maximumBytes) / CGFloat(png.count)).squareRoot() * 0.9
            scale = max(scale * shrink, minimumScale)
            png = try draw()
            attempts += 1
        }
        return png
    }

    private nonisolated static func drawPNG(
        _ job: PageRenderJob, drawing: PKDrawing?, area: CGRect, scale: CGFloat,
        requested: CGFloat
    ) throws -> Data {
        let pixelWidth = Int((area.width * scale).rounded())
        let pixelHeight = Int((area.height * scale).rounded())
        guard pixelWidth > 0, pixelHeight > 0 else { throw PageRenderError.contextUnavailable }

        guard
            let context = CGContext(
                data: nil, width: pixelWidth, height: pixelHeight, bitsPerComponent: 8,
                bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
        else { throw PageRenderError.contextUnavailable }

        // CoreGraphics is y-up; pages are y-down. Flip once, here, and move the
        // area to the corner, so every caller below can think in page points.
        context.translateBy(x: 0, y: CGFloat(pixelHeight))
        context.scaleBy(x: scale, y: -scale)
        context.translateBy(x: -area.minX, y: -area.minY)
        // The grid is drawn for the view this picture is of: at the scale
        // asked for it is the board at 100 %, and a capped or shrunk render is
        // the board zoomed out by as much, so its grid thins out the way the
        // screen's does.
        compose(
            job, drawing: drawing, area: area, in: context, inkScale: scale,
            gridZoom: requested > 0 ? scale / requested : 1, pixelsPerPoint: scale,
            snapToPixels: true)

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

    /// Draws a page -- or, for a board, its `renderArea` -- into a context
    /// already in page points with y running downwards from the page's
    /// top-left corner.
    ///
    /// One function, used by the on-device render, by the PDF export and by the
    /// tests, because "the export looks different from the screen" is exactly
    /// the class of bug that costs a person their trust in the app. Order is
    /// fixed: paper and its pattern, PDF background, the objects in their own
    /// order, ink on top.
    public nonisolated static func compose(
        _ job: PageRenderJob, in context: CGContext, inkScale: CGFloat
    ) {
        let drawing = decodedDrawing(job)
        compose(
            job, drawing: drawing, area: renderArea(job, drawing: drawing), in: context,
            inkScale: inkScale, gridZoom: 1, pixelsPerPoint: scale, snapToPixels: false)
    }

    /// `compose` with the work already done: the ink decoded, the area
    /// measured, and the view the board's grid is drawn for.
    static func compose(
        _ job: PageRenderJob, drawing: PKDrawing?, area: CGRect, in context: CGContext,
        inkScale: CGFloat, gridZoom: CGFloat, pixelsPerPoint: CGFloat, snapToPixels: Bool
    ) {
        let size = job.pointSize
        if job.isBoard {
            BoardGeometry.drawBackground(
                template: job.template, paperColor: job.paperColor, extent: size,
                visibleRect: area, zoom: gridZoom, pixelsPerPoint: pixelsPerPoint,
                snapToPixels: snapToPixels, in: context)
        } else {
            TemplateGeometry.draw(
                template: job.template, size: size, paperColor: job.paperColor, in: context)
            drawPDFBackground(job, size: size, in: context)
        }

        // Objects keep the order they have in `page.json`, which is the order
        // the screen stacks them in: Bring to Front means last.
        for object in job.objects {
            switch object.type {
            case .image: drawImageObject(object, job: job, in: context)
            case .shape: drawShapeObject(object, dark: job.isDark, in: context)
            case .text: drawTextObject(object, paper: job.paperColor, in: context)
            case .unknown: continue
            }
        }

        if let drawing, !drawing.strokes.isEmpty,
            let ink = inkImage(drawing, rect: area, scale: inkScale, dark: job.isDark)
        {
            context.saveGState()
            context.translateBy(x: area.minX, y: area.maxY)
            context.scaleBy(x: 1, y: -1)
            context.draw(ink, in: CGRect(origin: .zero, size: area.size))
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

    private nonisolated static func drawTextObject(
        _ object: PageObject, paper: PaperColor, in context: CGContext
    ) {
        guard let text = object.text, !text.isEmpty else { return }
        let rect = object.frame.rect
        guard rect.width > 0, rect.height > 0 else { return }
        let attributed = attributedText(
            text, size: object.resolvedFontSize, color: textColor(of: object, on: paper))
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
    /// `color` nil is the text colour of white paper, `#1C1C1E`.
    public nonisolated static func attributedText(
        _ text: String, size: CGFloat, color: CGColor? = nil
    ) -> NSAttributedString {
        let font =
            CTFontCreateUIFontForLanguage(.system, size, nil)
            ?? CTFontCreateWithName("Helvetica" as CFString, size, nil)
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineBreakMode = .byWordWrapping
        let color =
            color
            ?? CGColor(
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

    // MARK: - Shapes

    /// The outline of a shape object in page points: what the renderer
    /// strokes and fills, and what the screen's shape view should draw.
    ///
    /// A rectangle, ellipse or triangle sits inside its frame, its stroke
    /// included; the triangle's apex is the middle of the top edge. A line or
    /// an arrow runs from the frame's top-left corner to its bottom-right, so
    /// a negative width or height flips it; an arrow's head is an open V at
    /// the end. `nil` for anything that is not a shape object.
    public nonisolated static func shapePath(for object: PageObject) -> CGPath? {
        guard object.type == .shape else { return nil }
        let frame = object.frame
        guard frame.x.isFinite, frame.y.isFinite, frame.width.isFinite, frame.height.isFinite
        else { return nil }
        let width = shapeStrokeWidth(of: object)
        let path = CGMutablePath()
        switch shapeKind(of: object) {
        case .line, .arrow:
            let start = CGPoint(x: frame.x, y: frame.y)
            let end = CGPoint(x: frame.x + frame.width, y: frame.y + frame.height)
            let length = hypot(end.x - start.x, end.y - start.y)
            guard length > 0 else { return nil }
            path.move(to: start)
            path.addLine(to: end)
            if shapeKind(of: object) == .arrow {
                let head = min(max(12, width * 4), length * 0.4)
                let angle = atan2(end.y - start.y, end.x - start.x)
                let spread = CGFloat.pi * 28 / 180
                func barb(_ turn: CGFloat) -> CGPoint {
                    CGPoint(
                        x: end.x - head * cos(angle + turn), y: end.y - head * sin(angle + turn))
                }
                path.move(to: barb(spread))
                path.addLine(to: end)
                path.addLine(to: barb(-spread))
            }
        case .rectangle, .ellipse, .triangle:
            let outer = frame.rect.standardized
            guard outer.width > 0, outer.height > 0 else { return nil }
            let inset = min(width / 2, outer.width / 2, outer.height / 2)
            let rect = outer.insetBy(dx: inset, dy: inset)
            switch shapeKind(of: object) {
            case .ellipse:
                path.addEllipse(in: rect)
            case .triangle:
                path.move(to: CGPoint(x: rect.midX, y: rect.minY))
                path.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY))
                path.addLine(to: CGPoint(x: rect.minX, y: rect.maxY))
                path.closeSubpath()
            default:
                path.addRect(rect)
            }
        }
        return path
    }

    /// The object's `shape`, or a rectangle when it names none this build
    /// knows.
    nonisolated static func shapeKind(of object: PageObject) -> ShapeKind {
        if case .string(let name)? = object.additionalFields["shape"],
            let kind = ShapeKind(rawValue: name.lowercased())
        {
            return kind
        }
        return .rectangle
    }

    /// The object's `strokeWidth` in points: 2 when absent, held to 0.5-12.
    nonisolated static func shapeStrokeWidth(of object: PageObject) -> CGFloat {
        let value: Double
        switch object.additionalFields["strokeWidth"] {
        case .double(let number)?: value = number
        case .int(let number)?: value = Double(number)
        default: value = 2
        }
        guard value.isFinite else { return 2 }
        return CGFloat(min(max(value, 0.5), 12))
    }

    /// A fill is drawn at a quarter of its colour's strength.
    public static let shapeFillOpacity: CGFloat = 0.25
    /// A shape with no `stroke` of its own.
    public static let defaultShapeStroke = "#1c1c1e"

    private nonisolated static func drawShapeObject(
        _ object: PageObject, dark: Bool, in context: CGContext
    ) {
        guard let path = shapePath(for: object) else { return }
        let kind = shapeKind(of: object)
        var strokeHex = defaultShapeStroke
        if case .string(let hex)? = object.additionalFields["stroke"],
            ToolSettings.normalizedColor(hex) != nil
        {
            strokeHex = hex
        }
        context.saveGState()
        defer { context.restoreGState() }
        if kind != .line, kind != .arrow, case .string(let hex)? = object.additionalFields["fill"],
            let fill = displayColor(hex: hex, dark: dark)
        {
            context.addPath(path)
            context.setFillColor(fill.copy(alpha: shapeFillOpacity) ?? fill)
            context.fillPath()
        }
        guard let stroke = displayColor(hex: strokeHex, dark: dark) else { return }
        context.addPath(path)
        context.setStrokeColor(stroke)
        context.setLineWidth(shapeStrokeWidth(of: object))
        context.setLineCap(.round)
        context.setLineJoin(.round)
        context.strokePath()
    }

    // MARK: - Colour and the dark rule

    /// A text object's colour on `paper`: its own `color`, shown as the dark
    /// rule shows ink, or `#1C1C1E` on light paper and `#F2F2F7` on dark.
    nonisolated static func textColor(of object: PageObject, on paper: PaperColor) -> CGColor? {
        if case .string(let hex)? = object.additionalFields["color"],
            let color = displayColor(hex: hex, dark: paper.isDark)
        {
            return color
        }
        let rgb = PaperAppearance.defaultTextComponents(on: paper)
        return CGColor(
            colorSpace: CGColorSpaceCreateDeviceRGB(),
            components: [rgb.red, rgb.green, rgb.blue, 1])
    }

    /// A `#rrggbb` colour as it shows on paper: unchanged on light paper, and
    /// on dark paper mapped the way PencilKit maps ink between appearances.
    /// Colours are stored as their light-appearance values, so a shape or a
    /// text box in the pen's black shows light on dark paper exactly as the
    /// ink beside it does. `nil` for a string that is not a colour.
    public nonisolated static func displayColor(hex: String, dark: Bool) -> CGColor? {
        guard let rgb = ToolSettings.rgb(hex) else { return nil }
        let light = CGColor(
            colorSpace: CGColorSpaceCreateDeviceRGB(),
            components: [CGFloat(rgb.red), CGFloat(rgb.green), CGFloat(rgb.blue), 1])
        guard dark, let light else { return light }
        #if canImport(UIKit)
            return PKInkingTool.convertColor(UIColor(cgColor: light), from: .light, to: .dark).cgColor
        #else
            return darkInkColor(light) ?? light
        #endif
    }

    #if !canImport(UIKit)
        /// The Mac has no public form of PencilKit's colour mapping, so ask
        /// PencilKit itself: draw a dot of the colour under the dark appearance
        /// and read it back. Only the tests run here.
        private nonisolated static func darkInkColor(_ color: CGColor) -> CGColor? {
            guard let ns = NSColor(cgColor: color) else { return nil }
            let points = [CGPoint(x: 6, y: 12), CGPoint(x: 18, y: 12)].enumerated().map {
                PKStrokePoint(
                    location: $0.element, timeOffset: Double($0.offset) * 0.1,
                    size: CGSize(width: 16, height: 16), opacity: 1, force: 1, azimuth: 0,
                    altitude: .pi / 2)
            }
            let drawing = PKDrawing(strokes: [
                PKStroke(
                    ink: PKInk(.monoline, color: ns),
                    path: PKStrokePath(controlPoints: points, creationDate: Date()))
            ])
            guard
                let image = inkImage(
                    drawing, rect: CGRect(x: 0, y: 0, width: 24, height: 24), scale: 1, dark: true)
            else { return nil }
            var pixel = [UInt8](repeating: 0, count: 4)
            guard
                let context = CGContext(
                    data: &pixel, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 4,
                    space: CGColorSpaceCreateDeviceRGB(),
                    bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
            else { return nil }
            context.draw(
                image,
                in: CGRect(
                    x: -CGFloat(image.width) / 2, y: -CGFloat(image.height) / 2,
                    width: CGFloat(image.width), height: CGFloat(image.height)))
            guard pixel[3] > 0 else { return nil }
            let alpha = CGFloat(pixel[3]) / 255
            return CGColor(
                colorSpace: CGColorSpaceCreateDeviceRGB(),
                components: [
                    CGFloat(pixel[0]) / 255 / alpha, CGFloat(pixel[1]) / 255 / alpha,
                    CGFloat(pixel[2]) / 255 / alpha, 1,
                ])
        }
    #endif

    /// The ink of `rect` as a picture, under the appearance the dark rule
    /// gives the page: PencilKit picks ink colours from the current trait
    /// collection, so black ink renders light under a dark appearance. That is
    /// right on dark paper and wrong everywhere else, so the appearance is
    /// always set here, never inherited from whatever the app is in.
    static func inkImage(_ drawing: PKDrawing, rect: CGRect, scale: CGFloat, dark: Bool)
        -> CGImage?
    {
        #if canImport(UIKit)
            var image: UIImage?
            UITraitCollection(userInterfaceStyle: dark ? .dark : .light).performAsCurrent {
                image = drawing.image(from: rect, scale: scale)
            }
            return image?.cgImage
        #elseif canImport(AppKit)
            // Same on the Mac, where PaperCore's tests run: `.aqua` is light,
            // `.darkAqua` dark.
            var image: NSImage?
            if let appearance = NSAppearance(named: dark ? .darkAqua : .aqua) {
                appearance.performAsCurrentDrawingAppearance {
                    image = drawing.image(from: rect, scale: scale)
                }
            } else {
                image = drawing.image(from: rect, scale: scale)
            }
            var proposed = CGRect(origin: .zero, size: rect.size)
            return image?.cgImage(forProposedRect: &proposed, context: nil, hints: nil)
        #else
            return nil
        #endif
    }

    /// A square of a board's ink for Vision to read: ink only, on white, at
    /// `scale`. Ink on dark paper is drawn as it shows there, light on black,
    /// and the picture inverted, so a pale colour picked on dark paper reads
    /// as a dark one on white.
    static func recognitionImage(
        _ drawing: PKDrawing, rect: CGRect, dark: Bool, scale: CGFloat = 2
    ) -> CGImage? {
        let width = Int((rect.width * scale).rounded())
        let height = Int((rect.height * scale).rounded())
        guard width > 0, height > 0,
            let context = CGContext(
                data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
        else { return nil }
        let full = CGRect(x: 0, y: 0, width: width, height: height)
        context.setFillColor(gray: dark ? 0 : 1, alpha: 1)
        context.fill(full)
        if let ink = inkImage(drawing, rect: rect, scale: scale, dark: dark) {
            context.draw(ink, in: full)
        }
        if dark {
            context.setBlendMode(.difference)
            context.setFillColor(gray: 1, alpha: 1)
            context.fill(full)
        }
        return context.makeImage()
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
