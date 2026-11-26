import CoreGraphics
import XCTest

@testable import PaperCore

/// The growth rule for an infinite page. Pure geometry, so it is tested
/// directly rather than through a canvas.
final class InfiniteCanvasTests: XCTestCase {
    private let viewport = CGSize(width: 1024, height: 1366)

    private var step: CGSize { InfiniteCanvas.step(forViewport: viewport) }

    func testEmptyPageDoesNotGrow() {
        XCTAssertNil(
            InfiniteCanvas.growth(
                extent: InfiniteCanvas.baseExtent, content: nil, viewport: viewport))
        XCTAssertNil(
            InfiniteCanvas.growth(
                extent: InfiniteCanvas.baseExtent, content: .null, viewport: viewport))
    }

    func testWorkWellInsideTheMarginsDoesNotGrow() {
        let content = CGRect(x: 300, y: 300, width: 200, height: 200)
        XCTAssertNil(
            InfiniteCanvas.growth(
                extent: InfiniteCanvas.baseExtent, content: content, viewport: viewport))
    }

    func testInkNearTheBottomExtendsDownwardsWithoutMoving() {
        let base = InfiniteCanvas.baseExtent
        let content = CGRect(x: 300, y: base.height - 200, width: 50, height: 100)
        let growth = InfiniteCanvas.growth(extent: base, content: content, viewport: viewport)
        let unwrapped = try? XCTUnwrap(growth)
        guard let growth = unwrapped else { return XCTFail("expected growth") }
        XCTAssertEqual(growth.extent.width, base.width)
        XCTAssertEqual(growth.extent.height, base.height + step.height)
        XCTAssertEqual(growth.translation, .zero)
        XCTAssertTrue(growth.isPureExtension)
    }

    func testInkNearTheRightExtendsSidewaysWithoutMoving() {
        let base = InfiniteCanvas.baseExtent
        let content = CGRect(x: base.width - 190, y: 400, width: 100, height: 20)
        guard let growth = InfiniteCanvas.growth(extent: base, content: content, viewport: viewport)
        else { return XCTFail("expected growth") }
        XCTAssertEqual(growth.extent.width, base.width + step.width)
        XCTAssertEqual(growth.extent.height, base.height)
        XCTAssertEqual(growth.translation, .zero)
    }

    func testInkNearTheTopExtendsAndTranslates() {
        let base = InfiniteCanvas.baseExtent
        let content = CGRect(x: 400, y: 20, width: 50, height: 50)
        guard let growth = InfiniteCanvas.growth(extent: base, content: content, viewport: viewport)
        else { return XCTFail("expected growth") }
        XCTAssertEqual(growth.extent.height, base.height + step.height)
        XCTAssertEqual(growth.translation.height, step.height)
        XCTAssertEqual(growth.translation.width, 0)
        // After the translation the work is no longer inside the margin, which
        // is what stops the page growing again on the very next stroke.
        let moved = content.offsetBy(dx: growth.translation.width, dy: growth.translation.height)
        XCTAssertGreaterThanOrEqual(moved.minY, InfiniteCanvas.margin)
    }

    func testInkNearTheLeftExtendsAndTranslates() {
        let base = InfiniteCanvas.baseExtent
        let content = CGRect(x: 4, y: 500, width: 50, height: 50)
        guard let growth = InfiniteCanvas.growth(extent: base, content: content, viewport: viewport)
        else { return XCTFail("expected growth") }
        XCTAssertEqual(growth.extent.width, base.width + step.width)
        XCTAssertEqual(growth.translation.width, step.width)
        XCTAssertEqual(growth.translation.height, 0)
    }

    func testNegativeCoordinatesGrowByAsManyStepsAsAreNeeded() {
        let base = InfiniteCanvas.baseExtent
        // Far enough above the page that one step is not enough.
        let content = CGRect(x: 400, y: -1500, width: 50, height: 50)
        guard let growth = InfiniteCanvas.growth(extent: base, content: content, viewport: viewport)
        else { return XCTFail("expected growth") }
        let moved = content.offsetBy(dx: 0, dy: growth.translation.height)
        XCTAssertGreaterThanOrEqual(moved.minY, InfiniteCanvas.margin)
        XCTAssertEqual(
            growth.extent.height, base.height + growth.translation.height, accuracy: 0.001)
    }

    func testGrowthIsIdempotentOnceApplied() {
        let base = InfiniteCanvas.baseExtent
        let content = CGRect(x: 300, y: base.height - 100, width: 50, height: 50)
        guard let first = InfiniteCanvas.growth(extent: base, content: content, viewport: viewport)
        else { return XCTFail("expected growth") }
        let moved = content.offsetBy(dx: first.translation.width, dy: first.translation.height)
        XCTAssertNil(
            InfiniteCanvas.growth(extent: first.extent, content: moved, viewport: viewport))
    }

    func testExtentNeverShrinksBelowTheBase() {
        let tiny = CGSize(width: 100, height: 100)
        guard
            let growth = InfiniteCanvas.growth(
                extent: tiny, content: CGRect(x: 300, y: 300, width: 10, height: 10),
                viewport: viewport)
        else { return XCTFail("expected growth") }
        XCTAssertGreaterThanOrEqual(growth.extent.width, InfiniteCanvas.baseExtent.width)
        XCTAssertGreaterThanOrEqual(growth.extent.height, InfiniteCanvas.baseExtent.height)
    }

    func testStepIsHalfAScreenWithAFloor() {
        XCTAssertEqual(
            InfiniteCanvas.step(forViewport: CGSize(width: 2000, height: 1000)),
            CGSize(width: 1000, height: 500))
        XCTAssertEqual(
            InfiniteCanvas.step(forViewport: .zero),
            CGSize(width: InfiniteCanvas.minimumStep, height: InfiniteCanvas.minimumStep))
    }

    func testContentBoundsUnionsInkAndObjects() {
        let object = PageObject(
            type: .text, frame: PageObject.Frame(CGRect(x: 10, y: 10, width: 100, height: 40)),
            text: "hello")
        let bounds = InfiniteCanvas.contentBounds(
            ink: CGRect(x: 200, y: 300, width: 50, height: 50), objects: [object])
        XCTAssertEqual(bounds, CGRect(x: 10, y: 10, width: 240, height: 340))
        XCTAssertNil(InfiniteCanvas.contentBounds(ink: nil, objects: []))
    }

    func testInfinitePresetStartsAtTheBaseExtentAndIsNamed() {
        XCTAssertEqual(PaperSize.infinite.points, InfiniteCanvas.baseExtent)
        XCTAssertEqual(PaperSize.infinite.rawValue, "infinite")
        XCTAssertEqual(PaperSize.infinite.displayName, "Infinite")
        XCTAssertTrue(PaperSize.infinite.isInfinite)
        // A page that happens to measure 1024x1366 is still the iPad preset, so
        // PDF imports and older pages are unaffected.
        XCTAssertEqual(PaperSize.nearest(to: InfiniteCanvas.baseExtent), .iPad)
        XCTAssertEqual(
            RemoteSize(width: 1024, height: 1366).named, .iPad)
    }

    // MARK: - Growth snaps to the template

    /// Portrait and landscape iPads, a 12.9-inch at 2x zoom-out, and no
    /// viewport at all -- every step size the screen can produce.
    private let viewports = [
        CGSize(width: 1024, height: 1366), CGSize(width: 1366, height: 1024),
        CGSize(width: 2732, height: 2048), .zero,
    ]

    /// How far `value` is from the nearest whole multiple of `quantum`.
    private func offGrid(_ value: CGFloat, _ quantum: CGFloat) -> CGFloat {
        let remainder = abs(value.truncatingRemainder(dividingBy: quantum))
        return min(remainder, quantum - remainder)
    }

    func testTopGrowthMovesTheInkByWholeTemplateSpacings() {
        let base = InfiniteCanvas.baseExtent
        let content = CGRect(x: 400, y: 20, width: 50, height: 50)
        for template in Template.allCases {
            let quantum = TemplateGeometry.growthQuantum(for: template)
            for viewport in viewports {
                let context = "\(template) in \(viewport)"
                guard
                    let growth = InfiniteCanvas.growth(
                        extent: base, content: content, viewport: viewport, quantum: quantum)
                else {
                    XCTFail("expected growth: \(context)")
                    continue
                }
                let step = InfiniteCanvas.step(forViewport: viewport)
                if template.spacing > 0 {
                    XCTAssertEqual(
                        offGrid(growth.translation.height, template.spacing), 0, accuracy: 1e-6,
                        context)
                    XCTAssertLessThan(
                        growth.translation.height, step.height + template.spacing, context)
                } else {
                    XCTAssertEqual(growth.translation.height, step.height, context)
                }
                XCTAssertGreaterThanOrEqual(growth.translation.height, step.height, context)
                XCTAssertEqual(growth.translation.width, 0, context)
                XCTAssertEqual(
                    growth.extent.height, base.height + growth.translation.height, accuracy: 1e-9,
                    context)
                XCTAssertEqual(growth.extent.width, base.width, context)

                let moved = content.offsetBy(
                    dx: growth.translation.width, dy: growth.translation.height)
                XCTAssertGreaterThanOrEqual(moved.minY, InfiniteCanvas.margin, context)
                XCTAssertNil(
                    InfiniteCanvas.growth(
                        extent: growth.extent, content: moved, viewport: viewport,
                        quantum: quantum),
                    context)
            }
        }
    }

    func testLeftGrowthMovesTheInkByWholeColumnsOnGridAndDottedPaper() {
        let base = InfiniteCanvas.baseExtent
        let content = CGRect(x: 4, y: 500, width: 50, height: 50)
        for template in Template.allCases {
            let quantum = TemplateGeometry.growthQuantum(for: template)
            for viewport in viewports {
                let context = "\(template) in \(viewport)"
                guard
                    let growth = InfiniteCanvas.growth(
                        extent: base, content: content, viewport: viewport, quantum: quantum)
                else {
                    XCTFail("expected growth: \(context)")
                    continue
                }
                let step = InfiniteCanvas.step(forViewport: viewport)
                switch template {
                case .grid, .dotted:
                    XCTAssertEqual(
                        offGrid(growth.translation.width, template.spacing), 0, accuracy: 1e-6,
                        context)
                    XCTAssertGreaterThanOrEqual(growth.translation.width, step.width, context)
                    XCTAssertLessThan(
                        growth.translation.width, step.width + template.spacing, context)
                case .blank, .ruled, .cover:
                    // Nothing on the paper runs down the page, so nothing to
                    // line up with sideways.
                    XCTAssertEqual(growth.translation.width, step.width, context)
                }
                XCTAssertEqual(growth.translation.height, 0, context)
                XCTAssertEqual(
                    growth.extent.width, base.width + growth.translation.width, accuracy: 1e-9,
                    context)
                XCTAssertEqual(growth.extent.height, base.height, context)

                let moved = content.offsetBy(
                    dx: growth.translation.width, dy: growth.translation.height)
                XCTAssertGreaterThanOrEqual(moved.minX, InfiniteCanvas.margin, context)
                XCTAssertNil(
                    InfiniteCanvas.growth(
                        extent: growth.extent, content: moved, viewport: viewport,
                        quantum: quantum),
                    context)
            }
        }
    }

    /// The case from the audit: a 1300pt viewport gave a 650pt shift, 28.66
    /// rule gaps, and left a line written on the first rule about 15pt off it.
    func testWritingOnTheFirstRuleStaysOnARuleAfterTopGrowth() throws {
        let template = Template.ruled
        let base = InfiniteCanvas.baseExtent
        let firstRule = try XCTUnwrap(
            TemplateGeometry.lines(template: template, size: base).first?.0.y)
        let content = CGRect(x: 60, y: firstRule - 18, width: 300, height: 18)
        let growth = try XCTUnwrap(
            InfiniteCanvas.growth(
                extent: base, content: content, viewport: CGSize(width: 1000, height: 1300),
                quantum: TemplateGeometry.growthQuantum(for: template)))
        let rules = TemplateGeometry.lines(template: template, size: growth.extent).map(\.0.y)
        let baseline = firstRule + growth.translation.height
        let nearest = try XCTUnwrap(rules.min { abs($0 - baseline) < abs($1 - baseline) })
        XCTAssertEqual(nearest, baseline, accuracy: 0.01)
    }

    func testGridInkStaysOnItsCrossingAfterGrowingUpAndLeft() throws {
        let template = Template.grid
        let base = InfiniteCanvas.baseExtent
        let segments = TemplateGeometry.lines(template: template, size: base)
        let rows = segments.filter { $0.0.y == $0.1.y }.map(\.0.y)
        let columns = segments.filter { $0.0.x == $0.1.x }.map(\.0.x)
        // The second row and the second column cross near the top-left corner.
        let point = CGPoint(x: columns[1], y: rows[1])
        let content = CGRect(x: point.x, y: point.y, width: 40, height: 40)
        let growth = try XCTUnwrap(
            InfiniteCanvas.growth(
                extent: base, content: content, viewport: CGSize(width: 1100, height: 1300),
                quantum: TemplateGeometry.growthQuantum(for: template)))
        XCTAssertGreaterThan(growth.translation.width, 0)
        XCTAssertGreaterThan(growth.translation.height, 0)
        let grown = TemplateGeometry.lines(template: template, size: growth.extent)
        let grownRows = grown.filter { $0.0.y == $0.1.y }.map(\.0.y)
        let grownColumns = grown.filter { $0.0.x == $0.1.x }.map(\.0.x)
        let moved = CGPoint(
            x: point.x + growth.translation.width, y: point.y + growth.translation.height)
        XCTAssertTrue(grownRows.contains { abs($0 - moved.y) < 0.01 })
        XCTAssertTrue(grownColumns.contains { abs($0 - moved.x) < 0.01 })
    }

    func testRenderScaleIsCappedForVeryLongPages() {
        XCTAssertEqual(PageRenderer.cappedScale(2, for: CGSize(width: 1024, height: 1366)), 2)
        let long = CGSize(width: 1024, height: 20000)
        let scale = PageRenderer.cappedScale(2, for: long)
        XCTAssertLessThan(scale, 2)
        XCTAssertLessThanOrEqual(long.height * scale, PageRenderer.maximumPixelEdge + 0.001)
    }
}
