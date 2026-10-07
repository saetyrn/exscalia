import PaperCore
import SwiftUI

/// The paper ruling drawn directly, used for the template picker and for pages
/// with no render yet.
///
/// Geometry and colours are the page's own (`TemplateGeometry`,
/// `PaperAppearance`), so a preview is the real page at small size. Only the
/// line weight is not scaled: a 0.75 pt rule shrunk to a thumbnail is a few
/// hundredths of a point and vanishes, so rules stay half a point and dots
/// 1.4 pt across at any scale.
struct TemplatePreview: View {
    let template: Template
    var paperColor: PaperColor = .white
    /// The page stood in for, in points and turned the way the page is. The
    /// view is expected to have its aspect ratio.
    var pageSize: CGSize = PaperSize.a4.points

    var body: some View {
        Canvas { context, size in
            context.fill(Path(CGRect(origin: .zero, size: size)), with: .color(paper))
            // The ruling is drawn at the same relative spacing the real page uses.
            let reference = pageSize
            guard reference.width > 0 else { return }
            let scale = size.width / reference.width
            if template == .cover {
                let frame = TemplateGeometry.coverFrame(size: reference)
                guard !frame.isNull, frame.width > 0, frame.height > 0 else { return }
                let line = PaperAppearance.coverFrameComponents(on: paperColor)
                context.stroke(
                    Path(
                        CGRect(
                            x: frame.minX * scale, y: frame.minY * scale,
                            width: frame.width * scale, height: frame.height * scale)),
                    with: .color(Color(red: line.red, green: line.green, blue: line.blue).opacity(line.alpha)),
                    lineWidth: max(PaperAppearance.coverFrameWidth * scale, 0.5))
                return
            }
            var path = Path()
            for line in TemplateGeometry.lines(template: template, size: reference) {
                path.move(to: CGPoint(x: line.0.x * scale, y: line.0.y * scale))
                path.addLine(to: CGPoint(x: line.1.x * scale, y: line.1.y * scale))
            }
            context.stroke(path, with: .color(ruleColor), lineWidth: 0.5)
            for dot in TemplateGeometry.dots(template: template, size: reference) {
                let point = CGPoint(x: dot.x * scale, y: dot.y * scale)
                context.fill(
                    Path(ellipseIn: CGRect(x: point.x - 0.7, y: point.y - 0.7, width: 1.4, height: 1.4)),
                    with: .color(ruleColor))
            }
        }
        .background(paper)
    }

    private var paper: Color {
        let rgb = paperColor.rgb
        return Color(red: rgb.red, green: rgb.green, blue: rgb.blue)
    }

    private var ruleColor: Color {
        let rule = PaperAppearance.ruleComponents(on: paperColor)
        return Color(red: rule.red, green: rule.green, blue: rule.blue).opacity(rule.alpha)
    }
}
