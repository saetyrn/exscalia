import CoreGraphics
import XCTest

@testable import PaperCore

/// The ruling as geometry: where the rules, grid lines and dots fall, which the
/// screen, the renderer and the template picker all draw from.
final class TemplateGeometryTests: XCTestCase {
    /// The fixed sheets plus an infinite page that has grown both ways.
    private let sizes: [CGSize] = [
        PaperSize.a4.points, PaperSize.letter.points, PaperSize.iPad.points,
        CGSize(width: 1024 + 683, height: 1366 + 683 + 512),
    ]

    private func split(_ segments: [(CGPoint, CGPoint)]) -> (
        rows: [(CGPoint, CGPoint)], columns: [(CGPoint, CGPoint)]
    ) {
        (
            segments.filter { $0.0.y == $0.1.y },
            segments.filter { $0.0.x == $0.1.x }
        )
    }

    func testGridOuterLinesMeet() throws {
        let template = Template.grid
        for size in sizes {
            let context = "\(size)"
            let segments = TemplateGeometry.lines(template: template, size: size)
            let (rows, columns) = split(segments)
            XCTAssertFalse(rows.isEmpty, context)
            XCTAssertFalse(columns.isEmpty, context)
            XCTAssertEqual(rows.count + columns.count, segments.count, context)

            let lastColumn = try XCTUnwrap(columns.last?.0.x, context)
            let lastRow = try XCTUnwrap(rows.last?.0.y, context)
            XCTAssertEqual(rows.map { max($0.0.x, $0.1.x) }.max(), lastColumn, context)
            XCTAssertEqual(columns.map { max($0.0.y, $0.1.y) }.max(), lastRow, context)
            for row in rows {
                XCTAssertEqual(row.0.x, template.inset, context)
                XCTAssertEqual(row.1.x, lastColumn, context)
            }
            for column in columns {
                XCTAssertEqual(column.0.y, template.inset, context)
                XCTAssertEqual(column.1.y, lastRow, context)
            }
            XCTAssertLessThanOrEqual(lastColumn, size.width, context)
            XCTAssertLessThanOrEqual(lastRow, size.height, context)
        }
    }

    /// The sizes the audit measured: Letter's last column is at 590.9 and its
    /// last row at 775.2, past where the lines used to stop (588 and 768).
    func testGridReachesTheLastColumnAndRowOnLetter() throws {
        let (rows, columns) = split(
            TemplateGeometry.lines(template: .grid, size: PaperSize.letter.points))
        let lastColumn = try XCTUnwrap(columns.last?.0.x)
        let lastRow = try XCTUnwrap(rows.last?.0.y)
        XCTAssertEqual(lastColumn, 590.9, accuracy: 0.1)
        XCTAssertEqual(lastRow, 775.2, accuracy: 0.1)
        XCTAssertEqual(try XCTUnwrap(rows.first?.1.x), lastColumn)
        XCTAssertEqual(try XCTUnwrap(columns.first?.1.y), lastRow)
    }

    func testRuledRulesFallOnWholeSpacingsFromTheOrigin() {
        let template = Template.ruled
        let size = CGSize(width: 1024, height: 2732)
        let rules = TemplateGeometry.lines(template: template, size: size)
        XCTAssertFalse(rules.isEmpty)
        for (index, rule) in rules.enumerated() {
            XCTAssertEqual(rule.0.y, rule.1.y)
            XCTAssertEqual(
                rule.0.y, template.spacing * 2 + CGFloat(index) * template.spacing, accuracy: 1e-6)
        }
        XCTAssertLessThan(rules.last?.0.y ?? 0, size.height - template.spacing)
    }

    func testGrowthQuantumIsTheSpacingOfWhatThePaperRules() {
        XCTAssertEqual(TemplateGeometry.growthQuantum(for: .blank), .zero)
        XCTAssertEqual(
            TemplateGeometry.growthQuantum(for: .ruled),
            CGSize(width: 0, height: Template.ruled.spacing))
        XCTAssertEqual(
            TemplateGeometry.growthQuantum(for: .grid),
            CGSize(width: Template.grid.spacing, height: Template.grid.spacing))
        XCTAssertEqual(
            TemplateGeometry.growthQuantum(for: .dotted),
            CGSize(width: Template.dotted.spacing, height: Template.dotted.spacing))
    }
}
