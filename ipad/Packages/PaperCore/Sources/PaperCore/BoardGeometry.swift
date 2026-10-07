import CoreGraphics
import Foundation

/// A whiteboard board, as plain arithmetic.
///
/// A board is a page of a fixed extent (`board_extent`, 16,384 points square
/// unless it says otherwise). Canvas space is page space: the origin is the
/// extent's top-left corner, y runs down, units are points. The centre of the
/// extent never moves -- an extent is never changed and a board never grows --
/// so the grid is anchored there and nothing written on a board ever shifts.
///
/// Everything the screen, the renderer, the exporter, the minimap and the
/// recogniser have to agree on lives here, where `swift test` can reach it.
public enum BoardGeometry {
    // MARK: - Extent

    /// The extent of a new board.
    public static let defaultExtent = CGSize(width: 16_384, height: 16_384)

    /// A board's extent as it is drawn: `board_extent` held to its limits, or
    /// the default when the page has none.
    public static func extent(of page: Page) -> CGSize { page.resolvedBoardExtent.cgSize }

    /// The fixed point every board is anchored at: the middle of its extent.
    public static func center(of extent: CGSize) -> CGPoint {
        CGPoint(x: extent.width / 2, y: extent.height / 2)
    }

    // MARK: - Zoom

    /// 5 % to 400 %.
    public static let zoomRange: ClosedRange<CGFloat> = 0.05...4
    /// What one step of Cmd-+ or Cmd-minus multiplies or divides the zoom by.
    public static let zoomStepFactor: CGFloat = 1.25
    /// The fixed zooms the pill's menu offers below Fit Content.
    public static let zoomPresets: [CGFloat] = [0.25, 0.5, 1, 2, 4]

    /// `zoom` held to `zoomRange`; 100 % for a value that is not a number.
    public static func clampZoom(_ zoom: CGFloat) -> CGFloat {
        guard zoom.isFinite else { return 1 }
        return min(max(zoom, zoomRange.lowerBound), zoomRange.upperBound)
    }

    /// `steps` presses of Cmd-+ (positive) or Cmd-minus (negative).
    public static func steppedZoom(_ zoom: CGFloat, by steps: Int) -> CGFloat {
        clampZoom(clampZoom(zoom) * pow(zoomStepFactor, CGFloat(steps)))
    }

    /// What a one-finger double-tap zooms to: twice the zoom, and from 400 %
    /// back to 100 %.
    public static func doubleTapZoom(_ zoom: CGFloat) -> CGFloat {
        let current = clampZoom(zoom)
        return current >= zoomRange.upperBound - 0.001 ? 1 : clampZoom(current * 2)
    }

    /// The pill's label: "56%".
    public static func zoomLabel(_ zoom: CGFloat) -> String {
        "\(Int((clampZoom(zoom) * 100).rounded()))%"
    }

    /// The smallest zoom at which a board of `extent` still covers all of
    /// `viewport`, never below 5 %. Below it the edge of the board shows.
    public static func coveringZoom(extent: CGSize, viewport: CGSize) -> CGFloat {
        guard extent.width > 0, extent.height > 0 else { return zoomRange.lowerBound }
        return clampZoom(max(viewport.width / extent.width, viewport.height / extent.height))
    }

    // MARK: - Viewport

    /// The scroll offset that puts the canvas point `center` in the middle of
    /// a `viewport` showing the board at `zoom`. The view state stores the
    /// centre (`offset_x`, `offset_y`), not the offset.
    public static func contentOffset(center: CGPoint, zoom: CGFloat, viewport: CGSize) -> CGPoint {
        CGPoint(x: center.x * zoom - viewport.width / 2, y: center.y * zoom - viewport.height / 2)
    }

    /// The inverse of `contentOffset(center:zoom:viewport:)`.
    public static func viewportCenter(contentOffset: CGPoint, zoom: CGFloat, viewport: CGSize)
        -> CGPoint
    {
        guard zoom > 0 else { return .zero }
        return CGPoint(
            x: (contentOffset.x + viewport.width / 2) / zoom,
            y: (contentOffset.y + viewport.height / 2) / zoom)
    }

    /// The canvas rectangle a `viewport` centred on `center` shows at `zoom`.
    public static func visibleRect(center: CGPoint, zoom: CGFloat, viewport: CGSize) -> CGRect {
        guard zoom > 0 else { return .null }
        let size = CGSize(width: viewport.width / zoom, height: viewport.height / zoom)
        return CGRect(
            x: center.x - size.width / 2, y: center.y - size.height / 2, width: size.width,
            height: size.height)
    }

    /// `center` moved the least that keeps the viewport inside the board. On
    /// an axis where the viewport is wider than the board, the board's centre.
    public static func clampedCenter(
        _ center: CGPoint, zoom: CGFloat, viewport: CGSize, extent: CGSize
    ) -> CGPoint {
        guard zoom > 0, center.x.isFinite, center.y.isFinite else { return Self.center(of: extent) }
        func clamp(_ value: CGFloat, half: CGFloat, side: CGFloat) -> CGFloat {
            guard half * 2 < side else { return side / 2 }
            return min(max(value, half), side - half)
        }
        return CGPoint(
            x: clamp(center.x, half: viewport.width / zoom / 2, side: extent.width),
            y: clamp(center.y, half: viewport.height / zoom / 2, side: extent.height))
    }

    // MARK: - Content

    /// The union of the ink bounds and the object frames, or `nil` for an
    /// empty board. A line or arrow's frame may have a negative side; it is
    /// measured as the rectangle it covers.
    public static func contentBounds(ink: CGRect?, objects: [PageObject]) -> CGRect? {
        var union = CGRect.null
        if let ink, !ink.isNull, !ink.isInfinite, ink.width.isFinite, ink.height.isFinite {
            union = union.union(ink.standardized)
        }
        for object in objects {
            let rect = object.frame.rect.standardized
            guard rect.origin.x.isFinite, rect.origin.y.isFinite, rect.width.isFinite,
                rect.height.isFinite
            else { continue }
            union = union.union(rect)
        }
        return union.isNull ? nil : union
    }

    /// Air round the content in `render.png`, in points.
    public static let renderMargin: CGFloat = 48
    /// What an empty board's render shows, centred on the board.
    public static let emptyRenderSize = CGSize(width: 1366, height: 1024)

    /// The area of the board `render.png` shows, recorded as `render_rect`:
    /// the content and a 48-point margin, in whole points and inside the
    /// extent, or a 1,366 x 1,024 rectangle round the centre of an empty
    /// board.
    public static func renderRect(content: CGRect?, extent: CGSize) -> CGRect {
        let board = CGRect(origin: .zero, size: extent)
        let middle = center(of: extent)
        let empty = CGRect(
            x: (middle.x - emptyRenderSize.width / 2).rounded(.down),
            y: (middle.y - emptyRenderSize.height / 2).rounded(.down),
            width: emptyRenderSize.width, height: emptyRenderSize.height
        ).intersection(board)
        guard let content, !content.isNull, !content.isInfinite else { return empty }
        let grown = content.standardized.insetBy(dx: -renderMargin, dy: -renderMargin)
        let minX = grown.minX.rounded(.down)
        let minY = grown.minY.rounded(.down)
        let rect = CGRect(
            x: minX, y: minY, width: grown.maxX.rounded(.up) - minX,
            height: grown.maxY.rounded(.up) - minY
        ).intersection(board)
        return rect.isNull || rect.isEmpty ? empty : rect
    }

    /// Screen points left round the content by Fit Content.
    public static let fitMargin: CGFloat = 48

    /// Fit Content: the zoom that shows all of `content` inside `viewport`
    /// with `margin` screen points round it, within the zoom range, and the
    /// centre to show it at. An empty board fits as a new board opens:
    /// centred, at 100 %.
    public static func fitContent(
        _ content: CGRect?, extent: CGSize, viewport: CGSize, margin: CGFloat = fitMargin
    ) -> (zoom: CGFloat, center: CGPoint) {
        guard let content, !content.isNull, content.width.isFinite, content.height.isFinite else {
            return (1, center(of: extent))
        }
        let width = max(viewport.width - margin * 2, 1)
        let height = max(viewport.height - margin * 2, 1)
        let zoom = min(width / max(content.width, 1e-6), height / max(content.height, 1e-6))
        return (clampZoom(zoom), CGPoint(x: content.midX, y: content.midY))
    }

    // MARK: - Grid

    /// The finest square, in board points; every level is this times a power
    /// of two (`25 x 2^k` for k >= 0). The grid never divides below it: zooming
    /// in makes the squares larger on screen, as in the reference app, and only
    /// zooming out brings in coarser levels so the lines do not merge.
    public static let gridBaseSpacing: CGFloat = 25
    /// A level whose squares are this many screen points or fewer is not drawn.
    public static let gridFadeStart: CGFloat = 6
    /// A level whose squares are this many screen points or more is drawn at
    /// full strength.
    public static let gridFadeEnd: CGFloat = 24
    /// Levels fainter than this are left out: even at a line's full 21 %
    /// they would not change one 8-bit pixel.
    public static let gridMinimumOpacity: CGFloat = 0.01
    /// A Square Grid line at full strength: black at 8.5 % on light paper,
    /// two pixels wide. Measured against the owner's screenshots of the
    /// reference on cream at 56 % and at 100 %: a line there is one screen
    /// point wide (two pixels) and darkens the paper by about 30 levels
    /// summed across its width at the screenshots' scale (2,000 of the
    /// screen's 2,732 pixels); two pixels at 8.5 % give the same sum with the
    /// same softness, where one pixel at 17 % gave the sum but a harder line.
    public static let gridLineAlphaLight: CGFloat = 0.085
    /// White at 10.5 % on dark paper, in the same proportion to the light line
    /// as before the calibration (10 % to 8 %).
    public static let gridLineAlphaDark: CGFloat = 0.105
    /// A Dot Grid dot at full strength, light paper. Stronger than a line,
    /// because a dot covers far fewer pixels than a line does; in the same
    /// proportion to the line as before the calibration (30 % to 8 %).
    public static let gridDotAlphaLight: CGFloat = 0.64
    /// A Dot Grid dot at full strength, dark paper (34 % to 8 % before).
    public static let gridDotAlphaDark: CGFloat = 0.72
    /// Line width in screen pixels, whatever the zoom: one screen point on a
    /// 2x display, as in the reference.
    public static let gridLinePixels: CGFloat = 2
    /// Dot diameter in screen pixels, whatever the zoom.
    public static let gridDotPixels: CGFloat = 1.5

    /// One level of the grid at one zoom.
    public struct GridLevel: Equatable, Sendable {
        /// k in `25 x 2^k`.
        public var exponent: Int
        /// Side of one square, in board points.
        public var spacing: CGFloat
        /// Side of one square, in screen points at the zoom asked for.
        public var screenSpacing: CGFloat
        /// `smoothstep(6, 24, screenSpacing)`, 0...1.
        public var opacity: CGFloat
        /// True only for the coarsest level, which draws every one of its
        /// lines at full strength. A finer level leaves out every second line:
        /// that one belongs to a coarser level too, and a line takes the
        /// highest opacity of the levels it belongs to, so each line on screen
        /// is drawn exactly once.
        public var drawsAllLines: Bool

        public init(
            exponent: Int, spacing: CGFloat, screenSpacing: CGFloat, opacity: CGFloat,
            drawsAllLines: Bool
        ) {
            self.exponent = exponent
            self.spacing = spacing
            self.screenSpacing = screenSpacing
            self.opacity = opacity
            self.drawsAllLines = drawsAllLines
        }

        /// Where this level draws lines between `lower` and `upper` along one
        /// axis of a grid anchored at `anchor`, in board points.
        public func lines(from lower: CGFloat, to upper: CGFloat, anchor: CGFloat) -> [CGFloat] {
            indexedLines(from: lower, to: upper, anchor: anchor)
                .filter { drawsAllLines || $0.index % 2 != 0 }.map(\.position)
        }

        /// Every line of this level in the range, with its index from the anchor.
        func indexedLines(from lower: CGFloat, to upper: CGFloat, anchor: CGFloat)
            -> [(index: Int, position: CGFloat)]
        {
            guard spacing > 0, lower.isFinite, upper.isFinite, anchor.isFinite, lower <= upper
            else { return [] }
            let first = ((lower - anchor) / spacing).rounded(.up)
            let last = ((upper - anchor) / spacing).rounded(.down)
            // A range this long at this spacing is a caller's mistake, not a
            // grid: refuse it rather than allocate millions of lines.
            guard first <= last, last - first < 200_000 else { return [] }
            return (Int(first)...Int(last)).map { ($0, anchor + CGFloat($0) * spacing) }
        }
    }

    /// `smoothstep(6, 24, screenSpacing)`.
    public static func gridOpacity(screenSpacing: CGFloat) -> CGFloat {
        let t = min(max((screenSpacing - gridFadeStart) / (gridFadeEnd - gridFadeStart), 0), 1)
        return t * t * (3 - 2 * t)
    }

    /// The levels of the Square Grid and the Dot Grid at `zoom`, finest first.
    /// The last is always at full strength and draws all its lines. Empty for
    /// a zoom that is not a positive finite number.
    ///
    /// The screen calls this with its zoom, a render with its scale over 2x
    /// and an export with its ink scale over 3x, so all three draw the same
    /// grid for the same view.
    public static func gridLevels(zoom: CGFloat) -> [GridLevel] {
        guard zoom.isFinite, zoom > 0 else { return [] }
        func spacing(_ exponent: Int) -> CGFloat { scalbn(gridBaseSpacing, exponent) }
        var exponent = max(0, Int(log2(gridFadeStart / (gridBaseSpacing * zoom)).rounded(.down)))
        var guardCount = 0
        while gridOpacity(screenSpacing: spacing(exponent) * zoom) < gridMinimumOpacity,
            guardCount < 64
        {
            exponent += 1
            guardCount += 1
        }
        var levels: [GridLevel] = []
        while levels.count < 64 {
            let size = spacing(exponent)
            let screen = size * zoom
            let opacity = gridOpacity(screenSpacing: screen)
            let top = opacity >= 1
            levels.append(
                GridLevel(
                    exponent: exponent, spacing: size, screenSpacing: screen,
                    opacity: top ? 1 : opacity, drawsAllLines: top))
            if top { break }
            exponent += 1
        }
        return levels
    }

    /// The strength of the grid line at `position` (board points along one
    /// axis, from the board's left or top edge) at `zoom`: the highest opacity
    /// of the levels it belongs to, 0 when no level draws it.
    public static func gridLineOpacity(at position: CGFloat, anchor: CGFloat, zoom: CGFloat)
        -> CGFloat
    {
        var best: CGFloat = 0
        for level in gridLevels(zoom: zoom) {
            let steps = (position - anchor) / level.spacing
            if abs(steps - steps.rounded()) < 1e-6 { best = max(best, level.opacity) }
        }
        return best
    }

    /// RGBA of a Square Grid line at full strength on `paper`.
    public static func gridLineComponents(on paper: PaperColor)
        -> (red: CGFloat, green: CGFloat, blue: CGFloat, alpha: CGFloat)
    {
        paper.isDark ? (1, 1, 1, gridLineAlphaDark) : (0, 0, 0, gridLineAlphaLight)
    }

    /// RGBA of a Dot Grid dot at full strength on `paper`.
    public static func gridDotComponents(on paper: PaperColor)
        -> (red: CGFloat, green: CGFloat, blue: CGFloat, alpha: CGFloat)
    {
        paper.isDark ? (1, 1, 1, gridDotAlphaDark) : (0, 0, 0, gridDotAlphaLight)
    }

    /// Draws the part of a board's background inside `visibleRect`: the paper
    /// and its Square Grid or Dot Grid (any other template is blank).
    ///
    /// `context` is in canvas space, y down. `zoom` picks the grid levels;
    /// `pixelsPerPoint` is how many device pixels one canvas point covers, so
    /// that lines come out one pixel wide and dots one and a half: on screen
    /// the display scale times the zoom, in a render the render's scale.
    /// `snapToPixels` centres each line on a device pixel, for bitmaps; a PDF
    /// passes false.
    public static func drawBackground(
        template: Template, paperColor: PaperColor, extent: CGSize, visibleRect: CGRect,
        zoom: CGFloat, pixelsPerPoint: CGFloat, snapToPixels: Bool = false, in context: CGContext
    ) {
        let rect = visibleRect.intersection(CGRect(origin: .zero, size: extent))
        guard !rect.isNull, !rect.isEmpty else { return }
        let paper = paperColor.rgb
        context.saveGState()
        defer { context.restoreGState() }
        context.setFillColor(red: paper.red, green: paper.green, blue: paper.blue, alpha: 1)
        context.fill(rect)
        guard pixelsPerPoint > 0, pixelsPerPoint.isFinite, template == .grid || template == .dotted
        else { return }

        let anchor = center(of: extent)
        let levels = gridLevels(zoom: zoom)
        func snap(_ value: CGFloat, vertical: Bool) -> CGFloat {
            guard snapToPixels else { return value }
            let point = vertical ? CGPoint(x: value, y: rect.minY) : CGPoint(x: rect.minX, y: value)
            var device = context.convertToDeviceSpace(point)
            // A line on a pixel boundary goes to the pixel after it, even when
            // the arithmetic lands a hair short of the boundary. A line an odd
            // number of pixels wide is centred on that pixel; an even one on
            // the boundary after it, so it fills whole pixels either way.
            let offset: CGFloat = gridLinePixels.truncatingRemainder(dividingBy: 2) == 0 ? 1 : 0.5
            if vertical {
                device.x = (device.x + 1e-4).rounded(.down) + offset
            } else {
                device.y = (device.y + 1e-4).rounded(.down) + offset
            }
            let back = context.convertToUserSpace(device)
            return vertical ? back.x : back.y
        }

        if template == .grid {
            let colour = gridLineComponents(on: paperColor)
            context.setLineWidth(gridLinePixels / pixelsPerPoint)
            context.setLineCap(.butt)
            for level in levels {
                context.setStrokeColor(
                    red: colour.red, green: colour.green, blue: colour.blue,
                    alpha: colour.alpha * level.opacity)
                context.beginPath()
                for x in level.lines(from: rect.minX, to: rect.maxX, anchor: anchor.x) {
                    let snapped = snap(x, vertical: true)
                    context.move(to: CGPoint(x: snapped, y: rect.minY))
                    context.addLine(to: CGPoint(x: snapped, y: rect.maxY))
                }
                for y in level.lines(from: rect.minY, to: rect.maxY, anchor: anchor.y) {
                    let snapped = snap(y, vertical: false)
                    context.move(to: CGPoint(x: rect.minX, y: snapped))
                    context.addLine(to: CGPoint(x: rect.maxX, y: snapped))
                }
                context.strokePath()
            }
        } else {
            let colour = gridDotComponents(on: paperColor)
            let diameter = gridDotPixels / pixelsPerPoint
            for level in levels {
                context.setFillColor(
                    red: colour.red, green: colour.green, blue: colour.blue,
                    alpha: colour.alpha * level.opacity)
                let columns = level.indexedLines(from: rect.minX, to: rect.maxX, anchor: anchor.x)
                let rows = level.indexedLines(from: rect.minY, to: rect.maxY, anchor: anchor.y)
                guard columns.count * rows.count < 2_000_000 else { continue }
                context.beginPath()
                for row in rows {
                    for column in columns
                    where level.drawsAllLines || row.index % 2 != 0 || column.index % 2 != 0 {
                        context.addEllipse(
                            in: CGRect(
                                x: column.position - diameter / 2, y: row.position - diameter / 2,
                                width: diameter, height: diameter))
                    }
                }
                context.fillPath()
            }
        }
    }

    // MARK: - Recognition tiles

    /// Side of a recognition tile, in board points.
    public static let recognitionTileSide: CGFloat = 1024
    /// How far neighbouring tiles overlap, so a word on a tile edge is read
    /// whole by one of them.
    public static let recognitionTileOverlap: CGFloat = 64
    /// The most tiles one recognition pass reads. Ink beyond them is not
    /// indexed: a documented limit.
    public static let recognitionTileLimit = 64

    /// One square of the board read by itself.
    public struct RecognitionTile: Equatable, Sendable {
        /// The square read, in canvas space.
        public var rect: CGRect
        /// The part of `rect` this tile answers for: a line is kept by the one
        /// tile whose core holds the centre of its box. Cores meet in the
        /// middle of each overlap and do not overlap each other.
        public var core: CGRect

        public init(rect: CGRect, core: CGRect) {
            self.rect = rect
            self.core = core
        }

        /// Whether a line read with `box` (canvas space) belongs to this tile.
        public func keeps(_ box: CGRect) -> Bool {
            let middle = CGPoint(x: box.midX, y: box.midY)
            return middle.x >= core.minX && middle.x < core.maxX && middle.y >= core.minY
                && middle.y < core.maxY
        }
    }

    /// The tiles that cover `content` -- the rectangles of the ink, one per
    /// stroke -- with 1,024-point squares overlapping by 64 points, laid from
    /// the top-left corner of the ink's bounds. Only tiles holding ink count,
    /// taken row by row from the top left, at most `recognitionTileLimit`.
    public static func recognitionTiles(content: [CGRect]) -> [RecognitionTile] {
        let rects = content.map(\.standardized).filter {
            !$0.isNull && !$0.isInfinite && $0.minX.isFinite && $0.minY.isFinite
                && $0.width.isFinite && $0.height.isFinite
        }
        let bounds = rects.reduce(CGRect.null) { $0.union($1) }
        guard !bounds.isNull else { return [] }

        let side = recognitionTileSide
        let overlap = recognitionTileOverlap
        let stride = side - overlap
        func count(_ length: CGFloat) -> Int {
            max(1, Int(((length - overlap) / stride).rounded(.up)))
        }
        let columns = count(bounds.width)
        let rows = count(bounds.height)
        let origin = bounds.origin

        // Which tiles hold ink, found per stroke rather than per tile so a
        // board of ten thousand strokes stays cheap.
        var holding = Set<Int>()
        for rect in rects {
            func range(_ low: CGFloat, _ high: CGFloat, _ start: CGFloat, _ limit: Int)
                -> ClosedRange<Int>?
            {
                let first = max(0, Int(((low - start - side) / stride).rounded(.down)))
                let last = min(limit - 1, Int(((high - start) / stride).rounded(.up)))
                return first <= last ? first...last : nil
            }
            guard let columnRange = range(rect.minX, rect.maxX, origin.x, columns),
                let rowRange = range(rect.minY, rect.maxY, origin.y, rows)
            else { continue }
            for row in rowRange {
                for column in columnRange {
                    let tile = CGRect(
                        x: origin.x + CGFloat(column) * stride, y: origin.y + CGFloat(row) * stride,
                        width: side, height: side)
                    // A stroke one point wide still has area; a zero-size
                    // rectangle counts if it sits on the tile.
                    if tile.intersects(rect) || tile.contains(rect.origin) {
                        holding.insert(row * columns + column)
                    }
                }
            }
        }

        var tiles: [RecognitionTile] = []
        for key in holding.sorted() {
            let row = key / columns
            let column = key % columns
            let rect = CGRect(
                x: origin.x + CGFloat(column) * stride, y: origin.y + CGFloat(row) * stride,
                width: side, height: side)
            // Inner edges meet halfway through the overlap; outer edges reach
            // past the tile, where no box centre can be anyway.
            let minX = column == 0 ? rect.minX - overlap : rect.minX + overlap / 2
            let maxX = column == columns - 1 ? rect.maxX + overlap : rect.maxX - overlap / 2
            let minY = row == 0 ? rect.minY - overlap : rect.minY + overlap / 2
            let maxY = row == rows - 1 ? rect.maxY + overlap : rect.maxY - overlap / 2
            tiles.append(
                RecognitionTile(
                    rect: rect, core: CGRect(x: minX, y: minY, width: maxX - minX, height: maxY - minY)))
            if tiles.count == recognitionTileLimit { break }
        }
        return tiles
    }

    // MARK: - Minimap

    /// The minimap: a small picture of the board's content and the viewport,
    /// and the arithmetic between it and the canvas.
    public struct Minimap: Equatable, Sendable {
        /// The minimap's size on screen, in points.
        public static let defaultSize = CGSize(width: 200, height: 140)
        /// Its picture has two pixels per minimap point: 400 x 280.
        public static let pixelsPerPoint: CGFloat = 2
        /// How much the world grows past the content and the viewport, in all
        /// (5 % on each side).
        public static let margin: CGFloat = 0.10

        /// The canvas rectangle the minimap shows, with its aspect.
        public var world: CGRect
        /// The minimap's size in points.
        public var size: CGSize

        /// The world is (content union viewport), grown by 10 % and widened or
        /// heightened to the minimap's aspect, so all of both always shows.
        public init(content: CGRect?, viewport: CGRect, size: CGSize = Minimap.defaultSize) {
            self.size = size
            var union = viewport.standardized
            if let content, !content.isNull { union = union.isNull ? content : union.union(content) }
            if union.isNull || union.isEmpty {
                union = CGRect(
                    x: union.isNull ? 0 : union.midX - 0.5, y: union.isNull ? 0 : union.midY - 0.5,
                    width: 1, height: 1)
            }
            var grown = union.insetBy(
                dx: -union.width * Self.margin / 2, dy: -union.height * Self.margin / 2)
            let aspect = size.height > 0 ? size.width / size.height : 1
            if grown.width / grown.height > aspect {
                let height = grown.width / aspect
                grown = grown.insetBy(dx: 0, dy: (grown.height - height) / 2)
            } else {
                let width = grown.height * aspect
                grown = grown.insetBy(dx: (grown.width - width) / 2, dy: 0)
            }
            world = grown
        }

        /// Minimap points per canvas point.
        public var scale: CGFloat { world.width > 0 ? size.width / world.width : 0 }

        /// The scale to pass `PKDrawing.image(from: world, scale:)` for a
        /// picture of exactly `size` times `pixelsPerPoint` pixels.
        public var imageScale: CGFloat { scale * Self.pixelsPerPoint }

        /// Where a canvas point shows on the minimap.
        public func minimapPoint(fromCanvas point: CGPoint) -> CGPoint {
            CGPoint(x: (point.x - world.minX) * scale, y: (point.y - world.minY) * scale)
        }

        /// The canvas point under a minimap point: where a drag inside the
        /// minimap centres the viewport.
        public func canvasPoint(fromMinimap point: CGPoint) -> CGPoint {
            guard scale > 0 else { return CGPoint(x: world.midX, y: world.midY) }
            return CGPoint(x: world.minX + point.x / scale, y: world.minY + point.y / scale)
        }

        /// Where a canvas rectangle -- the viewport, an image's frame -- shows.
        public func minimapRect(fromCanvas rect: CGRect) -> CGRect {
            let origin = minimapPoint(fromCanvas: rect.standardized.origin)
            return CGRect(
                origin: origin,
                size: CGSize(
                    width: rect.standardized.width * scale, height: rect.standardized.height * scale))
        }
    }
}

/// How much a board holds before it says so, and before the Pencil stops.
///
/// Each flush measures the drawing's bytes and the count of strokes plus
/// objects. Both thresholds sit well under the server's 32 MB cap, because
/// every flush uploads, renders and recognises the whole board.
public enum BoardLimits {
    /// "Board nearly full" from here.
    public static let warningBytes = 16 * 1024 * 1024
    public static let warningItems = 15_000
    /// "Board full" from here: the Pencil stops inking on that board, while
    /// the eraser, the lasso and objects keep working.
    public static let fullBytes = 24 * 1024 * 1024
    public static let fullItems = 25_000

    public enum State: Equatable, Sendable {
        case normal, nearlyFull, full

        /// What the pill area says, or nothing.
        public var message: String? {
            switch self {
            case .normal: return nil
            case .nearlyFull: return "Board nearly full"
            case .full: return "Board full"
            }
        }

        /// Whether the Pencil may still ink.
        public var allowsInking: Bool { self != .full }
    }

    public static func state(drawingBytes: Int, strokes: Int, objects: Int) -> State {
        let items = strokes + objects
        if drawingBytes >= fullBytes || items >= fullItems { return .full }
        if drawingBytes >= warningBytes || items >= warningItems { return .nearlyFull }
        return .normal
    }
}
