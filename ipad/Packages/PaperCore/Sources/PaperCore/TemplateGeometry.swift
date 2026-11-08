import CoreGraphics
import Foundation

/// The ruling of a page, as plain geometry.
///
/// Shared on purpose: the on-screen `TemplateLayer` and the off-screen
/// `PageRenderer` must draw the same paper, or a page render would not match
/// what the user wrote on.
public enum TemplateGeometry {
    /// RGB of `#fdfcfa`, white paper. Other colours come from `PaperColor`.
    public static let paperComponents: (red: CGFloat, green: CGFloat, blue: CGFloat) =
        PaperColor.white.rgb
    /// Rules, grid lines and dots on white paper: black at 12 %. Light enough
    /// to disappear under ink. Any other paper asks
    /// `PaperAppearance.ruleComponents(on:)`.
    public static let ruleComponents: (red: CGFloat, green: CGFloat, blue: CGFloat, alpha: CGFloat) =
        PaperAppearance.ruleComponents(on: .white)

    public static let lineWidth: CGFloat = 0.75
    public static let dotRadius: CGFloat = 0.9

    /// Horizontal rules and vertical grid lines, in page points.
    public static func lines(template: Template, size: CGSize) -> [(CGPoint, CGPoint)] {
        let spacing = template.spacing
        guard spacing > 0 else { return [] }
        var result: [(CGPoint, CGPoint)] = []

        switch template {
        case .blank, .dotted, .cover:
            return []
        case .ruled:
            let inset = template.inset
            var y = spacing * 2
            while y < size.height - spacing {
                result.append((CGPoint(x: inset, y: y), CGPoint(x: size.width - 24, y: y)))
                y += spacing
            }
        case .grid:
            // Rows and columns first, so every line ends on the outermost line
            // across it and the grid's edges meet instead of overshooting.
            let inset = template.inset
            var rows: [CGFloat] = []
            var y = inset
            while y < size.height - inset / 2 {
                rows.append(y)
                y += spacing
            }
            var columns: [CGFloat] = []
            var x = inset
            while x < size.width - inset / 2 {
                columns.append(x)
                x += spacing
            }
            guard let lastRow = rows.last, let lastColumn = columns.last else { return [] }
            for y in rows {
                result.append((CGPoint(x: inset, y: y), CGPoint(x: lastColumn, y: y)))
            }
            for x in columns {
                result.append((CGPoint(x: x, y: inset), CGPoint(x: x, y: lastRow)))
            }
        }
        return result
    }

    /// What growing an infinite page at the top or the left must move the
    /// content by a multiple of, per axis, so the ink stays on the rule, grid
    /// line or dot it was written on: the template draws from the page origin,
    /// so any other shift slides the writing off the paper under it. Ruled
    /// paper only has rows; blank paper has nothing to line up with.
    public static func growthQuantum(for template: Template) -> CGSize {
        switch template {
        case .blank, .cover:
            return .zero
        case .ruled:
            return CGSize(width: 0, height: template.spacing)
        case .grid, .dotted:
            return CGSize(width: template.spacing, height: template.spacing)
        }
    }

    /// Dot centres, in page points.
    public static func dots(template: Template, size: CGSize) -> [CGPoint] {
        guard template == .dotted else { return [] }
        let spacing = template.spacing
        let inset = template.inset
        var result: [CGPoint] = []
        var y = inset
        while y < size.height - inset / 2 {
            var x = inset
            while x < size.width - inset / 2 {
                result.append(CGPoint(x: x, y: y))
                x += spacing
            }
            y += spacing
        }
        return result
    }

    /// Draws the page background into `context`, whose origin is the page's
    /// top-left with y increasing downwards: the paper in its colour, then its
    /// rules, grid or dots -- black at 12 % on light paper, white at 14 % on
    /// dark. A cover is its colour and one frame, 24 points in and 1 point
    /// wide, with no artwork.
    ///
    /// Boards draw their background with `BoardGeometry.drawBackground`.
    public static func draw(
        template: Template, size: CGSize, paperColor: PaperColor = .white, in context: CGContext
    ) {
        let paper = paperColor.rgb
        context.setFillColor(red: paper.red, green: paper.green, blue: paper.blue, alpha: 1)
        context.fill(CGRect(origin: .zero, size: size))

        if template == .cover {
            drawCoverFrame(size: size, paperColor: paperColor, in: context)
            return
        }

        let rule = PaperAppearance.ruleComponents(on: paperColor)
        context.setStrokeColor(red: rule.red, green: rule.green, blue: rule.blue, alpha: rule.alpha)
        context.setLineWidth(lineWidth)
        let segments = lines(template: template, size: size)
        if !segments.isEmpty {
            context.beginPath()
            for segment in segments {
                context.move(to: segment.0)
                context.addLine(to: segment.1)
            }
            context.strokePath()
        }

        let points = dots(template: template, size: size)
        if !points.isEmpty {
            context.setFillColor(red: rule.red, green: rule.green, blue: rule.blue, alpha: rule.alpha)
            for point in points {
                context.fillEllipse(
                    in: CGRect(
                        x: point.x - dotRadius, y: point.y - dotRadius, width: dotRadius * 2,
                        height: dotRadius * 2))
            }
        }
    }

    /// The cover's frame: its outer edge `coverFrameInset` inside the page
    /// edge, so the line covers exactly the 25th point in from each side.
    public static func coverFrame(size: CGSize) -> CGRect {
        let inset = PaperAppearance.coverFrameInset + PaperAppearance.coverFrameWidth / 2
        return CGRect(origin: .zero, size: size).insetBy(dx: inset, dy: inset)
    }

    private static func drawCoverFrame(
        size: CGSize, paperColor: PaperColor, in context: CGContext
    ) {
        let frame = coverFrame(size: size)
        guard !frame.isNull, frame.width > 0, frame.height > 0 else { return }
        let colour = PaperAppearance.coverFrameComponents(on: paperColor)
        context.setStrokeColor(
            red: colour.red, green: colour.green, blue: colour.blue, alpha: colour.alpha)
        context.setLineWidth(PaperAppearance.coverFrameWidth)
        context.stroke(frame)
    }
}
