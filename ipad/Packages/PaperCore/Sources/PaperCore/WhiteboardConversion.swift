import CoreGraphics
import Foundation
import PencilKit

/// What converting one page into a board does to it.
public struct BoardPlan: Equatable, Sendable {
    /// The page, which keeps its id: history, links and recognition rows
    /// follow it onto the board.
    public var pageID: String
    /// The board's name: the page's title, or "Board N".
    public var title: String
    /// B: the ink, the object frames and any PDF page, in the page's own
    /// space; the page rect when the page is empty.
    public var contentBounds: CGRect
    /// E: the default extent, or as much more as B needs.
    public var extent: SizePoints
    /// t = centre(E) - centre(B). The drawing, the objects and the PDF's
    /// picture all move by it.
    public var translation: CGSize
    /// Square Grid and Dot Grid stay; every other ruling becomes Blank.
    public var template: Template
    /// The page's paper colour; a cover colour becomes white.
    public var paperColor: PaperColor
    /// For a PDF page: the frame of the bottom-most image object its picture
    /// becomes, the page rect moved by t.
    public var pdfImageFrame: PageObject.Frame?

    public init(
        pageID: String, title: String, contentBounds: CGRect, extent: SizePoints,
        translation: CGSize, template: Template, paperColor: PaperColor,
        pdfImageFrame: PageObject.Frame?
    ) {
        self.pageID = pageID
        self.title = title
        self.contentBounds = contentBounds
        self.extent = extent
        self.translation = translation
        self.template = template
        self.paperColor = paperColor
        self.pdfImageFrame = pdfImageFrame
    }
}

public enum WhiteboardConversionError: Error, Equatable, Sendable {
    /// The page's content needs a board wider or taller than 32,768 points.
    /// The menu entry is disabled and labelled "Too large to convert".
    case tooLarge(pageID: String)
}

/// Convert to Whiteboard, as data: the plan for each page, and the pure
/// rewrites of a page, its drawing and its notebook that follow from it.
///
/// The conversion happens in place, so page ids, history and links survive.
/// Paged notebooks and infinite notebooks go through the same steps: an
/// infinite page's growth translations are already baked into its ink. The
/// store's `convertToWhiteboard` does the files, the snapshots, the PDF
/// pictures and the journal; nothing here touches the disk.
public enum WhiteboardConversion {
    /// The room kept on each side of the content when it outgrows the
    /// default extent.
    public static let margin: CGFloat = 1024
    /// A grown extent is a whole number of these.
    public static let extentStep: CGFloat = 1024
    /// The widest and tallest a board may be (`board_extent`'s cap).
    public static let maximumSide = CGFloat(Page.boardSideRange.upperBound)

    /// The plan for every page of a notebook, in page order.
    ///
    /// - Parameters:
    ///   - pages: the notebook's pages, in order.
    ///   - inkBounds: each page's `PKDrawing.bounds`, by page id; a page with
    ///     no entry has no ink.
    /// - Throws: `WhiteboardConversionError.tooLarge` when any page would need
    ///   a side longer than 32,768 points. Nothing is converted then.
    public static func plan(pages: [Page], inkBounds: [String: CGRect]) throws -> [BoardPlan] {
        try pages.enumerated().map { index, page in
            try plan(page: page, number: index + 1, inkBounds: inkBounds[page.id])
        }
    }

    /// The plan for one page, the `number`th of its notebook.
    public static func plan(page: Page, number: Int, inkBounds: CGRect?) throws -> BoardPlan {
        let pageRect = CGRect(origin: .zero, size: page.pointSize)
        var content = BoardGeometry.contentBounds(ink: inkBounds, objects: page.objects)
        if page.pdf != nil { content = content.map { $0.union(pageRect) } ?? pageRect }
        let bounds = content ?? pageRect

        guard let extent = extent(for: bounds.size) else {
            throw WhiteboardConversionError.tooLarge(pageID: page.id)
        }
        let center = BoardGeometry.center(of: extent.cgSize)
        let translation = CGSize(width: center.x - bounds.midX, height: center.y - bounds.midY)

        let template: Template
        switch page.template {
        case .grid, .dotted: template = page.template
        default: template = .blank
        }
        let title = page.title?.trimmingCharacters(in: .whitespacesAndNewlines)
        return BoardPlan(
            pageID: page.id,
            title: title?.isEmpty == false ? title ?? "" : "Board \(number)",
            contentBounds: bounds, extent: extent, translation: translation, template: template,
            paperColor: PaperColor.paperChoices.contains(page.paperColor) ? page.paperColor : .white,
            pdfImageFrame: page.pdf == nil
                ? nil : PageObject.Frame(pageRect.offsetBy(dx: translation.width, dy: translation.height)))
    }

    /// The extent a board needs for content of `size`: the default, or -- when
    /// the content and 1,024 points on each side do not fit in it -- the next
    /// multiple of 1,024 on that side. `nil` beyond 32,768.
    public static func extent(for size: CGSize) -> SizePoints? {
        func side(_ length: CGFloat, _ standard: CGFloat) -> CGFloat? {
            guard length.isFinite else { return nil }
            let needed = max(length, 0) + margin * 2
            guard needed > standard else { return standard }
            let grown = (needed / extentStep).rounded(.up) * extentStep
            return grown <= maximumSide ? grown : nil
        }
        let standard = BoardGeometry.defaultExtent
        guard let width = side(size.width, standard.width),
            let height = side(size.height, standard.height)
        else { return nil }
        return SizePoints(width: Double(width), height: Double(height))
    }

    /// `page` as the board `plan` describes: the same id, revision and
    /// history; size `board` with the planned extent, ruling, colour and
    /// title; no `size_points`; every object moved by t. A PDF page's binding
    /// becomes the bottom-most image object showing `pdfImageAsset` -- the
    /// page's picture, which the caller has rendered and stored -- framed at
    /// the page rect moved by t. Pass the asset for every PDF page: without
    /// one the binding is dropped and the board shows only the ink.
    public static func board(
        from page: Page, plan: BoardPlan, pdfImageAsset: String? = nil, modified: Date = Date()
    ) -> Page {
        var board = page
        board.size = .board
        board.boardExtent = plan.extent
        board.customSize = nil
        board.renderRect = nil
        board.template = plan.template
        board.paperColor = plan.paperColor
        board.title = plan.title
        board.objects = page.objects.map { translated($0, by: plan.translation) }
        if page.pdf != nil {
            board.pdf = nil
            if let pdfImageAsset, let frame = plan.pdfImageFrame {
                board.objects.insert(
                    PageObject(type: .image, frame: frame, asset: pdfImageAsset), at: 0)
            }
        }
        board.modified = modified
        return board
    }

    /// The whiteboard a converted notebook becomes: kind `whiteboard`, paper
    /// size `board`, and board 1's ruling as its template. These three fields
    /// are what the notebook patch carries.
    public static func whiteboard(from notebook: Notebook, plans: [BoardPlan]) -> Notebook {
        var whiteboard = notebook
        whiteboard.kind = .whiteboard
        whiteboard.paperSize = .board
        whiteboard.template = plans.first?.template ?? .grid
        return whiteboard
    }

    /// An object moved by `translation`. A crop is in the image's own unit
    /// space and does not move.
    public static func translated(_ object: PageObject, by translation: CGSize) -> PageObject {
        var moved = object
        moved.frame.x += Double(translation.width)
        moved.frame.y += Double(translation.height)
        return moved
    }

    /// A `.pkdrawing` with every stroke moved by `translation`.
    public static func translatedDrawing(_ data: Data, by translation: CGSize) throws -> Data {
        guard !data.isEmpty else { return data }
        let drawing = try PKDrawing(data: data)
        return drawing.transformed(
            using: CGAffineTransform(translationX: translation.width, y: translation.height)
        ).dataRepresentation()
    }

    /// The bounds of the ink in a `.pkdrawing`, or `nil` when it has none:
    /// what `plan(pages:inkBounds:)` takes for each page.
    public static func inkBounds(of data: Data?) -> CGRect? {
        guard let data, !data.isEmpty, let drawing = try? PKDrawing(data: data),
            !drawing.strokes.isEmpty
        else { return nil }
        let bounds = drawing.bounds
        return bounds.isNull || bounds.isEmpty ? nil : bounds
    }
}
