import CoreGraphics
import Foundation
import PencilKit
import XCTest

@testable import PaperCore

/// A board's plane: its fixed centre, zoom, content and render rectangles,
/// the levelled grid, recognition tiles, the minimap and the limits.
final class BoardGeometryTests: XCTestCase {
    private let extent = BoardGeometry.defaultExtent

    /// Zooms from 5 % to 400 %, each 0.2 % bigger than the last.
    private var sweep: [CGFloat] {
        var zooms: [CGFloat] = []
        var zoom = BoardGeometry.zoomRange.lowerBound
        while zoom <= BoardGeometry.zoomRange.upperBound {
            zooms.append(zoom)
            zoom *= 1.002
        }
        return zooms
    }

    // MARK: - Extent and zoom

    func testTheExtentAndItsFixedCentre() {
        XCTAssertEqual(extent, CGSize(width: 16_384, height: 16_384))
        XCTAssertEqual(extent, PaperSize.board.points)
        XCTAssertEqual(BoardGeometry.center(of: extent), CGPoint(x: 8192, y: 8192))
        let board = Page(size: .board, boardExtent: SizePoints(width: 20_480, height: 4096))
        XCTAssertEqual(BoardGeometry.extent(of: board), CGSize(width: 20_480, height: 4096))
        XCTAssertEqual(BoardGeometry.extent(of: Page(size: .board)), extent)
    }

    func testZoomRangeStepsAndLabel() {
        XCTAssertEqual(BoardGeometry.zoomRange, 0.05...4)
        XCTAssertEqual(BoardGeometry.clampZoom(0.01), 0.05)
        XCTAssertEqual(BoardGeometry.clampZoom(9), 4)
        XCTAssertEqual(BoardGeometry.clampZoom(.nan), 1)
        XCTAssertEqual(BoardGeometry.steppedZoom(1, by: 1), 1.25, accuracy: 1e-9)
        XCTAssertEqual(BoardGeometry.steppedZoom(1, by: -2), 0.64, accuracy: 1e-9)
        XCTAssertEqual(BoardGeometry.steppedZoom(3.9, by: 1), 4)
        XCTAssertEqual(BoardGeometry.doubleTapZoom(0.56), 1.12, accuracy: 1e-9)
        XCTAssertEqual(BoardGeometry.doubleTapZoom(3), 4)
        XCTAssertEqual(BoardGeometry.doubleTapZoom(4), 1)
        XCTAssertEqual(BoardGeometry.zoomLabel(0.56), "56%")
        XCTAssertEqual(BoardGeometry.zoomLabel(0.0549), "5%")
        XCTAssertEqual(BoardGeometry.zoomLabel(1), "100%")
        XCTAssertEqual(BoardGeometry.zoomPresets, [0.25, 0.5, 1, 2, 4])
        let viewport = CGSize(width: 1366, height: 1024)
        XCTAssertEqual(
            BoardGeometry.coveringZoom(extent: extent, viewport: viewport), 1366 / 16_384,
            accuracy: 1e-9)
    }

    func testTheViewStateCentreRoundTripsThroughTheScrollOffset() {
        let viewport = CGSize(width: 1366, height: 1024)
        let center = CGPoint(x: 9000.5, y: 7123.25)
        for zoom: CGFloat in [0.05, 0.56, 1, 4] {
            let offset = BoardGeometry.contentOffset(center: center, zoom: zoom, viewport: viewport)
            let back = BoardGeometry.viewportCenter(contentOffset: offset, zoom: zoom, viewport: viewport)
            XCTAssertEqual(back.x, center.x, accuracy: 1e-6)
            XCTAssertEqual(back.y, center.y, accuracy: 1e-6)
            let visible = BoardGeometry.visibleRect(center: center, zoom: zoom, viewport: viewport)
            XCTAssertEqual(visible.width, viewport.width / zoom, accuracy: 1e-6)
            XCTAssertEqual(visible.midX, center.x, accuracy: 1e-6)
        }
        // A new board opens centred at 100 %.
        XCTAssertEqual(
            BoardGeometry.contentOffset(
                center: BoardGeometry.center(of: extent), zoom: 1, viewport: viewport),
            CGPoint(x: 8192 - 683, y: 8192 - 512))
        // Held inside the board; the middle when the board is the smaller.
        let held = BoardGeometry.clampedCenter(
            CGPoint(x: 10, y: 16_380), zoom: 1, viewport: viewport, extent: extent)
        XCTAssertEqual(held, CGPoint(x: 683, y: 16_384 - 512))
        XCTAssertEqual(
            BoardGeometry.clampedCenter(
                CGPoint(x: 10, y: 10), zoom: 0.05, viewport: viewport, extent: extent),
            CGPoint(x: 8192, y: 8192))
    }

    // MARK: - Content, render rectangle and Fit Content

    func testContentBoundsTakeInkAndObjectsIncludingFlippedLines() {
        XCTAssertNil(BoardGeometry.contentBounds(ink: nil, objects: []))
        XCTAssertNil(BoardGeometry.contentBounds(ink: .null, objects: []))
        let line = PageObject(
            type: .shape, frame: PageObject.Frame(x: 500, y: 500, width: -200, height: -100))
        let bounds = BoardGeometry.contentBounds(
            ink: CGRect(x: 600, y: 600, width: 10, height: 10), objects: [line])
        XCTAssertEqual(bounds, CGRect(x: 300, y: 400, width: 310, height: 210))
    }

    func testTheRenderRectangleIsTheContentAndAMarginInWholePoints() {
        XCTAssertEqual(
            BoardGeometry.renderRect(content: nil, extent: extent),
            CGRect(x: 7509, y: 7680, width: 1366, height: 1024))
        let rect = BoardGeometry.renderRect(
            content: CGRect(x: 9000.4, y: 8000.6, width: 300.2, height: 99.9), extent: extent)
        XCTAssertEqual(rect, CGRect(x: 8952, y: 7952, width: 397, height: 197))
        // Held inside the extent at its edges.
        XCTAssertEqual(
            BoardGeometry.renderRect(
                content: CGRect(x: 10, y: 16_300, width: 50, height: 84), extent: extent),
            CGRect(x: 0, y: 16_252, width: 108, height: 132))
        // Content that spans the board renders within the pixel caps.
        let spanning = BoardGeometry.renderRect(
            content: CGRect(x: 0, y: 0, width: 16_384, height: 16_384), extent: extent)
        XCTAssertEqual(spanning, CGRect(origin: .zero, size: extent))
        let scale = PageRenderer.cappedScale(2, for: spanning.size)
        XCTAssertLessThanOrEqual(spanning.width * scale, PageRenderer.maximumPixelEdge)
        XCTAssertLessThanOrEqual(
            spanning.width * scale * spanning.height * scale, PageRenderer.maximumPixelArea + 1)
    }

    func testFitContentShowsTheContentWithAMargin() {
        let viewport = CGSize(width: 1000, height: 800)
        let fit = BoardGeometry.fitContent(
            CGRect(x: 6000, y: 7000, width: 4000, height: 1000), extent: extent, viewport: viewport,
            margin: 50)
        XCTAssertEqual(fit.zoom, 0.225, accuracy: 1e-6)
        XCTAssertEqual(fit.center, CGPoint(x: 8000, y: 7500))
        // A dot of ink does not zoom past 400 %; a span wider than 5 % shows
        // stops at 5 %.
        XCTAssertEqual(
            BoardGeometry.fitContent(
                CGRect(x: 10, y: 10, width: 0.5, height: 0.5), extent: extent, viewport: viewport
            ).zoom, 4)
        XCTAssertEqual(
            BoardGeometry.fitContent(
                CGRect(x: 0, y: 0, width: 99_000, height: 10), extent: extent, viewport: viewport
            ).zoom, 0.05)
        // An empty board fits as a new board opens.
        let empty = BoardGeometry.fitContent(nil, extent: extent, viewport: viewport)
        XCTAssertEqual(empty.zoom, 1)
        XCTAssertEqual(empty.center, CGPoint(x: 8192, y: 8192))
    }

    // MARK: - Grid levels

    func testAtFiftySixPercentLinesFallFourteenPointsApartAndEveryOtherIsStronger() throws {
        let levels = BoardGeometry.gridLevels(zoom: 0.56)
        let base = try XCTUnwrap(levels.first { $0.spacing == 25 })
        XCTAssertEqual(base.screenSpacing, 14, accuracy: 1e-9)
        XCTAssertEqual(base.opacity, 0.42, accuracy: 0.01)
        XCTAssertFalse(base.drawsAllLines)
        let top = try XCTUnwrap(levels.last)
        XCTAssertEqual(top.spacing, 50)
        XCTAssertEqual(top.opacity, 1)
        XCTAssertTrue(top.drawsAllLines)
        // The 12.5-point level would be 7 screen points apart, under 1 %.
        XCTAssertEqual(levels.count, 2)

        let anchor = BoardGeometry.center(of: extent).x
        let opacities = (0..<8).map {
            BoardGeometry.gridLineOpacity(at: anchor + CGFloat($0) * 25, anchor: anchor, zoom: 0.56)
        }
        XCTAssertEqual(opacities[0], 1)
        XCTAssertEqual(opacities[1], base.opacity)
        XCTAssertEqual(opacities[2], 1)
        XCTAssertEqual(opacities[3], base.opacity)
        XCTAssertEqual(
            BoardGeometry.gridLineOpacity(at: anchor + 12.5, anchor: anchor, zoom: 0.56), 0)
    }

    /// Zooming in never divides the squares: from 96 % up the only level is
    /// the base one, drawn in full, and its squares grow with the zoom.
    func testTheGridStopsDividingAtTwentyFivePoints() throws {
        for zoom: CGFloat in [0.96, 1, 1.7, 2, 4] {
            let levels = BoardGeometry.gridLevels(zoom: zoom)
            XCTAssertEqual(levels.count, 1, "zoom \(zoom)")
            let only = try XCTUnwrap(levels.first, "zoom \(zoom)")
            XCTAssertEqual(only.exponent, 0, "zoom \(zoom)")
            XCTAssertEqual(only.spacing, 25, "zoom \(zoom)")
            XCTAssertEqual(only.screenSpacing, 25 * zoom, accuracy: 1e-9, "zoom \(zoom)")
            XCTAssertEqual(only.opacity, 1, "zoom \(zoom)")
            XCTAssertTrue(only.drawsAllLines, "zoom \(zoom)")
        }
        for zoom: CGFloat in [0.05, 0.1, 0.33, 0.56, 4] {
            for level in BoardGeometry.gridLevels(zoom: zoom) {
                XCTAssertGreaterThanOrEqual(level.exponent, 0, "zoom \(zoom)")
            }
        }
    }

    func testLevelsAreTwentyFiveTimesPowersOfTwoAnchoredAtTheCentre() {
        let anchor = BoardGeometry.center(of: extent).x
        for zoom: CGFloat in [0.05, 0.1, 0.33, 0.56, 1, 1.7, 4] {
            for level in BoardGeometry.gridLevels(zoom: zoom) {
                XCTAssertEqual(level.spacing, 25 * pow(2, CGFloat(level.exponent)), accuracy: 1e-9)
                for x in level.lines(from: 7000, to: 9000, anchor: anchor) {
                    let steps = (x - anchor) / level.spacing
                    XCTAssertEqual(steps, steps.rounded(), accuracy: 1e-9)
                    if !level.drawsAllLines {
                        XCTAssertNotEqual(Int(steps.rounded()) % 2, 0)
                    }
                }
            }
        }
    }

    func testTheGridIsNeverAbsentAndNeverASmear() throws {
        for zoom in sweep {
            let levels = BoardGeometry.gridLevels(zoom: zoom)
            let context = "zoom \(zoom)"
            let finest = try XCTUnwrap(levels.first, context)
            let coarsest = try XCTUnwrap(levels.last, context)
            XCTAssertEqual(coarsest.opacity, 1, context)
            XCTAssertTrue(coarsest.drawsAllLines, context)
            XCTAssertGreaterThan(finest.screenSpacing, BoardGeometry.gridFadeStart, context)
            XCTAssertLessThanOrEqual(levels.count, 3, context)
            for level in levels.dropLast() {
                XCTAssertFalse(level.drawsAllLines, context)
                XCTAssertLessThan(level.opacity, 1, context)
                XCTAssertGreaterThanOrEqual(level.opacity, BoardGeometry.gridMinimumOpacity, context)
            }
            for (finer, coarser) in zip(levels, levels.dropFirst()) {
                XCTAssertEqual(coarser.exponent, finer.exponent + 1, context)
                XCTAssertEqual(coarser.spacing, finer.spacing * 2, accuracy: 1e-9, context)
            }
        }
    }

    func testOpacityIsASmoothstepFromSixToTwentyFourScreenPoints() {
        XCTAssertEqual(BoardGeometry.gridOpacity(screenSpacing: 6), 0)
        XCTAssertEqual(BoardGeometry.gridOpacity(screenSpacing: 15), 0.5, accuracy: 1e-9)
        XCTAssertEqual(BoardGeometry.gridOpacity(screenSpacing: 24), 1)
        var previous: CGFloat = -1
        for step in 0...200 {
            let value = BoardGeometry.gridOpacity(screenSpacing: CGFloat(step) * 0.2)
            XCTAssertGreaterThanOrEqual(value, previous)
            previous = value
        }
    }

    func testNonsenseZoomDrawsNothingRatherThanLooping() {
        XCTAssertTrue(BoardGeometry.gridLevels(zoom: 0).isEmpty)
        XCTAssertTrue(BoardGeometry.gridLevels(zoom: -1).isEmpty)
        XCTAssertTrue(BoardGeometry.gridLevels(zoom: .nan).isEmpty)
        XCTAssertTrue(BoardGeometry.gridLevels(zoom: .infinity).isEmpty)
    }

    /// Every line is drawn by exactly one level, the coarsest it belongs to.
    func testEachLineIsDrawnExactlyOnce() {
        let anchor = BoardGeometry.center(of: extent).y
        for zoom in stride(from: 0.05, through: 4.0, by: 0.01) {
            let levels = BoardGeometry.gridLevels(zoom: CGFloat(zoom))
            var drawn: [CGFloat: Int] = [:]
            for level in levels {
                for y in level.lines(from: anchor - 3000, to: anchor + 3000, anchor: anchor) {
                    drawn[y, default: 0] += 1
                }
            }
            XCTAssertTrue(drawn.values.allSatisfy { $0 == 1 }, "zoom \(zoom)")
            if let top = levels.last {
                for y in top.lines(from: anchor - 3000, to: anchor + 3000, anchor: anchor) {
                    XCTAssertEqual(drawn[y], 1, "zoom \(zoom)")
                }
            }
        }
    }

    /// The point of the levels: no line jumps in or out as the zoom changes.
    func testLineStrengthChangesContinuouslyWithTheZoom() {
        let anchor = BoardGeometry.center(of: extent).x
        let zooms = sweep
        for (before, after) in zip(zooms, zooms.dropFirst()) {
            for step in -64...64 {
                let x = anchor + CGFloat(step) * 12.5
                let a = BoardGeometry.gridLineOpacity(at: x, anchor: anchor, zoom: before)
                let b = BoardGeometry.gridLineOpacity(at: x, anchor: anchor, zoom: after)
                XCTAssertLessThan(abs(a - b), 0.03, "x \(x) between \(before) and \(after)")
            }
        }
    }

    func testGridColoursFollowThePaper() {
        XCTAssertEqual(BoardGeometry.gridLineComponents(on: .cream).alpha, 0.085)
        XCTAssertEqual(BoardGeometry.gridLineComponents(on: .cream).red, 0)
        XCTAssertEqual(BoardGeometry.gridLineComponents(on: .dark).alpha, 0.105)
        XCTAssertEqual(BoardGeometry.gridLineComponents(on: .dark).red, 1)
        XCTAssertEqual(BoardGeometry.gridDotComponents(on: .cream).alpha, 0.64)
        XCTAssertEqual(BoardGeometry.gridDotComponents(on: .dark).alpha, 0.72)
    }

    /// The calibration against the owner's screenshot moved every strength by
    /// one factor, so lines, dots and dark paper keep their proportions.
    func testGridStrengthsKeepTheirProportions() {
        // A line's darkness is its alpha over its width in pixels; the dots
        // and the dark-paper line keep their proportion to that.
        let line = BoardGeometry.gridLineAlphaLight * BoardGeometry.gridLinePixels
        XCTAssertEqual(BoardGeometry.gridLineAlphaDark / BoardGeometry.gridLineAlphaLight, 0.10 / 0.08, accuracy: 0.03)
        XCTAssertEqual(BoardGeometry.gridDotAlphaLight / line, 0.30 / 0.08, accuracy: 0.03)
        XCTAssertEqual(BoardGeometry.gridDotAlphaDark / line, 0.34 / 0.08, accuracy: 0.03)
    }


    // MARK: - Recognition tiles

    func testOneSmallPatchOfInkIsOneTile() throws {
        let tiles = BoardGeometry.recognitionTiles(
            content: [CGRect(x: 8000, y: 8000, width: 300, height: 40)])
        XCTAssertEqual(tiles.count, 1)
        let tile = try XCTUnwrap(tiles.first)
        XCTAssertEqual(tile.rect, CGRect(x: 8000, y: 8000, width: 1024, height: 1024))
        XCTAssertTrue(tile.keeps(CGRect(x: 8000, y: 8000, width: 300, height: 40)))
        XCTAssertTrue(BoardGeometry.recognitionTiles(content: []).isEmpty)
    }

    func testOnlyTilesHoldingInkCountRowByRowFromTheTopLeft() {
        // Ink at the left and right ends of a 3,000-point span, and one patch
        // a row down at the right: the middle column of the top row is empty.
        let tiles = BoardGeometry.recognitionTiles(content: [
            CGRect(x: 1000, y: 1000, width: 100, height: 50),
            CGRect(x: 3900, y: 1000, width: 100, height: 50),
            CGRect(x: 3900, y: 2100, width: 100, height: 50),
        ])
        let stride = BoardGeometry.recognitionTileSide - BoardGeometry.recognitionTileOverlap
        XCTAssertEqual(stride, 960)
        // Columns start at 1000, 1960, 2920 and 3880; the ink at 3900-4000
        // sits in the overlap of the last two, so both read it.
        XCTAssertEqual(
            tiles.map(\.rect.origin),
            [
                CGPoint(x: 1000, y: 1000), CGPoint(x: 2920, y: 1000), CGPoint(x: 3880, y: 1000),
                CGPoint(x: 2920, y: 1960), CGPoint(x: 3880, y: 1960),
            ])
        XCTAssertFalse(tiles.contains { $0.rect.minX == 1960 && $0.rect.minY == 1000 })
        // Neighbouring tiles overlap by 64 points.
        XCTAssertEqual(tiles[1].rect.minX - tiles[0].rect.minX, 2 * stride)
    }

    /// Every box centre in the inked area belongs to exactly one tile's core,
    /// so a line in an overlap is filed once.
    func testCoresPartitionTheOverlaps() {
        let content = [
            CGRect(x: 0, y: 0, width: 3000, height: 2500)
        ]
        let tiles = BoardGeometry.recognitionTiles(content: content)
        // 3,000 x 2,500 points: four columns and three rows.
        XCTAssertEqual(tiles.count, 12)
        for x in stride(from: 1.0, to: 3000, by: 37) {
            for y in stride(from: 1.0, to: 2500, by: 41) {
                let box = CGRect(x: x - 20, y: y - 5, width: 40, height: 10)
                XCTAssertEqual(tiles.filter { $0.keeps(box) }.count, 1, "\(x), \(y)")
            }
        }
    }

    func testAtMostSixtyFourTilesArePassedToOneRun() {
        var strokes: [CGRect] = []
        for row in 0..<10 {
            for column in 0..<10 {
                strokes.append(
                    CGRect(x: CGFloat(column) * 960 + 100, y: CGFloat(row) * 960 + 100, width: 50, height: 20))
            }
        }
        let tiles = BoardGeometry.recognitionTiles(content: strokes)
        XCTAssertEqual(tiles.count, BoardGeometry.recognitionTileLimit)
        // The ink spans 8,690 points: nine columns of tiles, every one inked.
        // The first 64 in reading order are seven whole rows and one more.
        XCTAssertEqual(tiles.first?.rect.origin, CGPoint(x: 100, y: 100))
        XCTAssertEqual(tiles[62].rect.origin, CGPoint(x: 100 + 8 * 960, y: 100 + 6 * 960))
        XCTAssertEqual(tiles.last?.rect.origin, CGPoint(x: 100, y: 100 + 7 * 960))
    }

    // MARK: - Minimap

    func testTheMinimapShowsContentAndViewportAtItsAspect() {
        let content = CGRect(x: 6000, y: 7000, width: 3000, height: 500)
        let viewport = CGRect(x: 8000, y: 7800, width: 1366, height: 1024)
        let map = BoardGeometry.Minimap(content: content, viewport: viewport)
        XCTAssertEqual(map.size, CGSize(width: 200, height: 140))
        XCTAssertTrue(map.world.contains(content))
        XCTAssertTrue(map.world.contains(viewport))
        XCTAssertEqual(map.world.width / map.world.height, 200 / 140, accuracy: 1e-9)
        let union = content.union(viewport)
        XCTAssertGreaterThanOrEqual(map.world.width, union.width * 1.1 - 1e-6)
        XCTAssertGreaterThanOrEqual(map.world.height, union.height * 1.1 - 1e-6)
        XCTAssertEqual(map.world.midX, union.midX, accuracy: 1e-6)
        // Its picture is 400 x 280 pixels.
        XCTAssertEqual(map.world.width * map.imageScale, 400, accuracy: 1e-6)
        XCTAssertEqual(map.world.height * map.imageScale, 280, accuracy: 1e-6)
    }

    func testAMinimapPointRoundTripsToTheCanvas() {
        let map = BoardGeometry.Minimap(
            content: CGRect(x: 100, y: 200, width: 900, height: 2000),
            viewport: CGRect(x: 8000, y: 8000, width: 1366, height: 1024))
        for point in [CGPoint(x: 0, y: 0), CGPoint(x: 37.5, y: 99), CGPoint(x: 200, y: 140)] {
            let canvas = map.canvasPoint(fromMinimap: point)
            let back = map.minimapPoint(fromCanvas: canvas)
            XCTAssertEqual(back.x, point.x, accuracy: 1e-9)
            XCTAssertEqual(back.y, point.y, accuracy: 1e-9)
        }
        let rect = map.minimapRect(fromCanvas: CGRect(x: 8000, y: 8000, width: 1366, height: 1024))
        XCTAssertEqual(rect.width, 1366 * map.scale, accuracy: 1e-9)
        // An empty board: the viewport alone.
        let empty = BoardGeometry.Minimap(
            content: nil, viewport: CGRect(x: 7509, y: 7680, width: 1366, height: 1024))
        XCTAssertEqual(empty.world.midX, 8192, accuracy: 1e-9)
        XCTAssertEqual(empty.world.midY, 8192, accuracy: 1e-9)
    }

    // MARK: - Limits

    func testLimitsWarnThenStopTheInk() {
        XCTAssertEqual(BoardLimits.state(drawingBytes: 0, strokes: 0, objects: 0), .normal)
        XCTAssertEqual(
            BoardLimits.state(drawingBytes: 16 * 1024 * 1024 - 1, strokes: 14_000, objects: 999),
            .normal)
        XCTAssertEqual(
            BoardLimits.state(drawingBytes: 16 * 1024 * 1024, strokes: 0, objects: 0), .nearlyFull)
        XCTAssertEqual(
            BoardLimits.state(drawingBytes: 0, strokes: 14_000, objects: 1_000), .nearlyFull)
        XCTAssertEqual(
            BoardLimits.state(drawingBytes: 24 * 1024 * 1024, strokes: 0, objects: 0), .full)
        XCTAssertEqual(BoardLimits.state(drawingBytes: 0, strokes: 24_990, objects: 10), .full)
        XCTAssertEqual(BoardLimits.State.nearlyFull.message, "Board nearly full")
        XCTAssertEqual(BoardLimits.State.full.message, "Board full")
        XCTAssertNil(BoardLimits.State.normal.message)
        XCTAssertFalse(BoardLimits.State.full.allowsInking)
        XCTAssertTrue(BoardLimits.State.nearlyFull.allowsInking)
        XCTAssertLessThan(BoardLimits.fullBytes, 32 * 1024 * 1024)
    }
}

// MARK: - Convert to Whiteboard

/// The conversion as pure data: where each page's content lands on its board,
/// and that every page keeps its id.
final class WhiteboardConversionTests: XCTestCase {
    private let ink = CGRect(x: 100, y: 200, width: 300, height: 80)

    private func stroke(_ rect: CGRect) -> Data {
        InkFixtures.data([
            InkFixtures.stroke(
                from: CGPoint(x: rect.minX + 5, y: rect.midY), to: CGPoint(x: rect.maxX - 5, y: rect.midY),
                width: 4)
        ])
    }

    func testAPagedNotebookPageLandsCentredOnADefaultBoard() throws {
        let note = PageObject(
            type: .text, frame: PageObject.Frame(x: 60, y: 600, width: 200, height: 40), text: "Note")
        let page = Page(
            template: .ruled, size: .a4, revision: 7, objects: [note], paperColor: .cream)
        let drawing = stroke(ink)
        let measured = try XCTUnwrap(WhiteboardConversion.inkBounds(of: drawing))
        let plans = try WhiteboardConversion.plan(pages: [page], inkBounds: [page.id: measured])
        let plan = try XCTUnwrap(plans.first)

        let bounds = measured.union(note.frame.rect)
        XCTAssertEqual(plan.pageID, page.id)
        XCTAssertEqual(plan.contentBounds, bounds)
        XCTAssertEqual(plan.extent, SizePoints(width: 16_384, height: 16_384))
        XCTAssertEqual(plan.translation.width, 8192 - bounds.midX, accuracy: 1e-9)
        XCTAssertEqual(plan.translation.height, 8192 - bounds.midY, accuracy: 1e-9)
        XCTAssertEqual(plan.template, .blank, "ruled becomes blank")
        XCTAssertEqual(plan.paperColor, .cream)
        XCTAssertEqual(plan.title, "Board 1")
        XCTAssertNil(plan.pdfImageFrame)

        let board = WhiteboardConversion.board(from: page, plan: plan)
        XCTAssertEqual(board.id, page.id)
        XCTAssertEqual(board.revision, 7)
        XCTAssertEqual(board.size, .board)
        XCTAssertEqual(board.boardExtent, plan.extent)
        XCTAssertNil(board.customSize)
        XCTAssertEqual(board.template, .blank)
        XCTAssertEqual(board.title, "Board 1")
        XCTAssertEqual(board.objects.first?.id, note.id)
        XCTAssertEqual(board.objects.first?.frame.x, 60 + Double(plan.translation.width))
        XCTAssertEqual(board.pointSize, BoardGeometry.defaultExtent)

        // The ink moves by the same translation and sits round the centre.
        let moved = try WhiteboardConversion.translatedDrawing(drawing, by: plan.translation)
        let movedBounds = try XCTUnwrap(WhiteboardConversion.inkBounds(of: moved))
        // PencilKit measures bounds to about half a point.
        XCTAssertEqual(movedBounds.minX, measured.minX + plan.translation.width, accuracy: 1)
        XCTAssertEqual(movedBounds.minY, measured.minY + plan.translation.height, accuracy: 1)
        let content = try XCTUnwrap(
            BoardGeometry.contentBounds(ink: movedBounds, objects: board.objects))
        XCTAssertEqual(content.midX, 8192, accuracy: 1)
        XCTAssertEqual(content.midY, 8192, accuracy: 1)
    }

    /// An infinite page has grown; its growth is already in the ink.
    func testAnInfinitePageKeepsItsTitleAndGridAndIsCentredByItsContent() throws {
        let grown = CGRect(x: 160, y: 300, width: 2700, height: 4400)
        let page = Page(
            title: "Ideas", template: .grid, size: .infinite,
            customSize: SizePoints(width: 3000, height: 5000))
        let empty = Page(template: .dotted, size: .infinite, customSize: SizePoints(width: 1024, height: 1366))
        let plans = try WhiteboardConversion.plan(
            pages: [page, empty], inkBounds: [page.id: grown])
        XCTAssertEqual(plans.map(\.pageID), [page.id, empty.id])
        XCTAssertEqual(plans[0].title, "Ideas")
        XCTAssertEqual(plans[0].template, .grid)
        XCTAssertEqual(plans[0].extent, SizePoints(width: 16_384, height: 16_384))
        XCTAssertEqual(plans[0].translation, CGSize(width: 8192 - grown.midX, height: 8192 - grown.midY))
        // An empty page is centred by its page rect.
        XCTAssertEqual(plans[1].title, "Board 2")
        XCTAssertEqual(plans[1].template, .dotted)
        XCTAssertEqual(plans[1].contentBounds, CGRect(x: 0, y: 0, width: 1024, height: 1366))
        XCTAssertEqual(plans[1].translation, CGSize(width: 8192 - 512, height: 8192 - 683))

        let board = WhiteboardConversion.board(from: page, plan: plans[0])
        XCTAssertEqual(board.size, .board)
        XCTAssertNil(board.customSize, "size_points is dropped")
        XCTAssertEqual(board.title, "Ideas")
    }

    func testAPDFPageBecomesTheBottomImageAtThePageRectMoved() throws {
        let note = PageObject(
            type: .text, frame: PageObject.Frame(x: 50, y: 50, width: 100, height: 30), text: "a")
        let page = Page(
            template: .blank, size: .letter, objects: [note],
            pdf: PDFBinding(asset: "paper/Imports.paper/assets/source-0123.pdf", pageIndex: 2),
            customSize: SizePoints(width: 612, height: 792))
        let plan = try XCTUnwrap(
            try WhiteboardConversion.plan(pages: [page], inkBounds: [:]).first)
        XCTAssertEqual(plan.contentBounds, CGRect(x: 0, y: 0, width: 612, height: 792))
        let frame = try XCTUnwrap(plan.pdfImageFrame)
        XCTAssertEqual(frame, PageObject.Frame(x: 8192 - 306, y: 8192 - 396, width: 612, height: 792))

        let asset = "paper/Imports.paper/assets/page-3-abcdef0123456789.png"
        let board = WhiteboardConversion.board(from: page, plan: plan, pdfImageAsset: asset)
        XCTAssertNil(board.pdf)
        XCTAssertEqual(board.objects.count, 2)
        XCTAssertEqual(board.objects[0].type, .image)
        XCTAssertEqual(board.objects[0].asset, asset)
        XCTAssertEqual(board.objects[0].frame, frame)
        XCTAssertEqual(board.objects[1].id, note.id)
        XCTAssertEqual(board.objects[1].frame.x, 50 + Double(plan.translation.width))

        // Ink beyond the PDF page widens the content.
        let wide = try XCTUnwrap(
            try WhiteboardConversion.plan(
                pages: [page], inkBounds: [page.id: CGRect(x: 600, y: 700, width: 400, height: 200)]
            ).first)
        XCTAssertEqual(wide.contentBounds, CGRect(x: 0, y: 0, width: 1000, height: 900))
    }

    func testContentTooBigForTheDefaultGrowsTheExtentAndBeyondTheCapIsRefused() throws {
        let page = Page(template: .blank, size: .infinite, customSize: SizePoints(width: 20_000, height: 3000))
        let plan = try XCTUnwrap(
            try WhiteboardConversion.plan(
                pages: [page], inkBounds: [page.id: CGRect(x: 0, y: 0, width: 20_000, height: 3000)]
            ).first)
        // 20,000 + 2 x 1,024 = 22,048, up to the next multiple of 1,024.
        XCTAssertEqual(plan.extent, SizePoints(width: 22_528, height: 16_384))
        XCTAssertEqual(plan.translation.width, 22_528 / 2 - 10_000)
        XCTAssertEqual(WhiteboardConversion.extent(for: CGSize(width: 14_336, height: 10)),
            SizePoints(width: 16_384, height: 16_384))
        XCTAssertEqual(WhiteboardConversion.extent(for: CGSize(width: 14_337, height: 10)),
            SizePoints(width: 17_408, height: 16_384))
        XCTAssertEqual(WhiteboardConversion.extent(for: CGSize(width: 30_720, height: 10))?.width, 32_768)
        XCTAssertNil(WhiteboardConversion.extent(for: CGSize(width: 30_721, height: 10)))

        let huge = Page(template: .blank, size: .infinite, customSize: SizePoints(width: 40_000, height: 2000))
        XCTAssertThrowsError(
            try WhiteboardConversion.plan(
                pages: [page, huge],
                inkBounds: [huge.id: CGRect(x: 0, y: 0, width: 31_000, height: 100)])
        ) { error in
            XCTAssertEqual(error as? WhiteboardConversionError, .tooLarge(pageID: huge.id))
        }
    }

    func testACoverBecomesAWhiteBlankBoardAndPaperColoursStay() throws {
        let cover = Page(template: .cover, size: .iPad, paperColor: .navy)
        let dark = Page(template: .grid, size: .iPad, paperColor: .dark)
        let plans = try WhiteboardConversion.plan(pages: [cover, dark], inkBounds: [:])
        XCTAssertEqual(plans[0].template, .blank)
        XCTAssertEqual(plans[0].paperColor, .white)
        XCTAssertEqual(plans[1].template, .grid)
        XCTAssertEqual(plans[1].paperColor, .dark)

        let notebook = Notebook(
            title: "Midterm", template: .ruled, paperSize: .a4, kind: .notebook,
            path: "paper/Midterm.paper")
        let whiteboard = WhiteboardConversion.whiteboard(from: notebook, plans: plans)
        XCTAssertEqual(whiteboard.id, notebook.id)
        XCTAssertEqual(whiteboard.kind, .whiteboard)
        XCTAssertEqual(whiteboard.paperSize, .board)
        XCTAssertEqual(whiteboard.template, .blank, "board 1's template")
    }

    /// Through a real store: the pages a conversion plans for are the
    /// notebook's own, ink measured from their drawings.
    func testAPlanFromTheStoreKeepsEveryPageID() throws {
        let root = FileManager.default.temporaryDirectory
            .appending(path: "Convert-\(UUID().uuidString)", directoryHint: .isDirectory)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = try PaperStore(root: root)
        let notebook = try store.createNotebook(title: "Midterm", pageCount: 3)
        let pages = try store.pages(ofNotebook: notebook.id)
        try store.writeDrawing(stroke(ink), notebookPath: notebook.path, pageID: pages[1].id)
        var bounds: [String: CGRect] = [:]
        for page in pages {
            bounds[page.id] = WhiteboardConversion.inkBounds(
                of: store.drawingData(notebookPath: notebook.path, pageID: page.id))
        }
        XCTAssertNil(bounds[pages[0].id] ?? nil)
        let plans = try WhiteboardConversion.plan(pages: pages, inkBounds: bounds.compactMapValues { $0 })
        XCTAssertEqual(plans.map(\.pageID), notebook.pages)
        XCTAssertEqual(plans.map(\.title), ["Board 1", "Board 2", "Board 3"])
        XCTAssertEqual(plans[1].contentBounds.midX, ink.midX, accuracy: 3)
    }
}

// MARK: - Recognition on boards

/// Boards are read from tiles cut from the drawing, never from `render.png`.
final class BoardRecognitionTests: XCTestCase {
    /// Reads nothing: answers one line in the middle of every tile it is
    /// shown, and keeps the pictures.
    final class TileBackend: HandwritingRecognitionBackend, @unchecked Sendable {
        private let lock = NSLock()
        private var seen: [(image: CGImage, size: CGSize)] = []

        var images: [(image: CGImage, size: CGSize)] {
            lock.lock()
            defer { lock.unlock() }
            return seen
        }

        func recognize(image: CGImage, pageSize: CGSize, languages: [String]) throws -> PageText {
            lock.lock()
            seen.append((image, pageSize))
            let index = seen.count
            lock.unlock()
            return PageText(lines: [
                RecognizedLine(
                    text: "tile \(index)",
                    box: PageObject.Frame(
                        x: Double(pageSize.width / 2 - 50), y: Double(pageSize.height / 2 - 10),
                        width: 100, height: 20),
                    confidence: 0.9)
            ])
        }
    }

    private var root: URL!
    private var store: PaperStore!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appending(path: "BoardRecognition-\(UUID().uuidString)", directoryHint: .isDirectory)
        store = try PaperStore(root: root)
    }

    override func tearDownWithError() throws {
        store = nil
        try? FileManager.default.removeItem(at: root)
    }

    private func whiteboard(color: PaperColor = .cream) throws -> (Notebook, Page) {
        var options = NewDocumentOptions.whiteboardFactory
        options.paperColor = color
        let notebook = try store.createDocument(
            title: "Board", folderPath: "paper", kind: .whiteboard, options: options)
        let page = try store.page(notebookPath: notebook.path, id: try XCTUnwrap(notebook.pages.first))
        return (notebook, page)
    }

    func testABoardIsReadInTilesAndItsLinesLandInCanvasSpace() async throws {
        let (notebook, page) = try whiteboard()
        let drawing = InkFixtures.data([
            InkFixtures.stroke(from: CGPoint(x: 2000, y: 3000), to: CGPoint(x: 2400, y: 3000)),
            InkFixtures.stroke(from: CGPoint(x: 12_000, y: 9000), to: CGPoint(x: 12_300, y: 9000)),
        ])
        try store.writeDrawing(drawing, notebookPath: notebook.path, pageID: page.id)
        // No render.png exists: a board is never read from it.
        XCTAssertFalse(
            FileManager.default.fileExists(
                atPath: store.paths.render(notebook.path, page.id).path(percentEncoded: false)))

        let backend = TileBackend()
        let recognizer = HandwritingRecognizer(store: store, idleDelay: 0, backend: backend)
        let recognised = await recognizer.recognizeNow(
            RecognitionJob(notebookPath: notebook.path, page: page))
        let text = try XCTUnwrap(recognised)

        // Two separate patches of ink: two tiles, not the 100 between them.
        let seen = backend.images
        XCTAssertEqual(seen.count, 2)
        for item in seen {
            XCTAssertEqual(item.size, CGSize(width: 1024, height: 1024))
            XCTAssertEqual(item.image.width, 2048, "2x")
        }
        XCTAssertEqual(text.lines.count, 2)
        // Each line's box is in canvas space, in the middle of its tile.
        let tiles = BoardGeometry.recognitionTiles(
            content: try PKDrawing(data: drawing).strokes.map(\.renderBounds))
        for (line, tile) in zip(text.lines, tiles) {
            XCTAssertEqual(line.box.rect.midX, tile.rect.midX, accuracy: 0.01)
            XCTAssertEqual(line.box.rect.midY, tile.rect.midY, accuracy: 0.01)
        }
        XCTAssertLessThan(text.lines[0].box.y, text.lines[1].box.y)
        // Filed and searchable like any page's text.
        XCTAssertEqual(store.pageText(notebookPath: notebook.path, pageID: page.id)?.lines.count, 2)
    }

    /// Ink only, on white: no paper, no grid. On dark paper the black ink
    /// shows light, so the picture is inverted to dark on white for reading.
    func testTilesAreInkOnWhiteAndDarkPaperIsInverted() async throws {
        for color in [PaperColor.cream, .dark] {
            let (notebook, page) = try whiteboard(color: color)
            let drawing = InkFixtures.data([
                InkFixtures.stroke(from: CGPoint(x: 8000, y: 8000), to: CGPoint(x: 8400, y: 8000), width: 10)
            ])
            try store.writeDrawing(drawing, notebookPath: notebook.path, pageID: page.id)
            let backend = TileBackend()
            let recognizer = HandwritingRecognizer(store: store, idleDelay: 0, backend: backend)
            _ = await recognizer.recognizeNow(RecognitionJob(notebookPath: notebook.path, page: page))
            let image = try RenderedPixels(image: try XCTUnwrap(backend.images.first?.image))
            // Away from the ink: pure white, whatever the paper.
            XCTAssertEqual(image[1500, 1500].r, 255, "\(color)")
            XCTAssertEqual(image[1500, 1500].b, 255, "\(color)")
            // The stroke: dark on white.
            let dark = image.count(in: CGRect(x: 0, y: 0, width: 900, height: 60)) {
                ($0 + $1 + $2) / 3 < 100
            }
            XCTAssertGreaterThan(dark, 1000, "\(color)")
        }
    }

    func testALineInAnOverlapIsFiledByOneTile() {
        let tiles = BoardGeometry.recognitionTiles(content: [
            CGRect(x: 0, y: 0, width: 50, height: 50), CGRect(x: 1500, y: 0, width: 50, height: 50),
        ])
        XCTAssertEqual(tiles.count, 2)
        // A line centred at x = 1000: inside both tiles (0-1024, 960-1984).
        let line = RecognizedLine(
            text: "edge", box: PageObject.Frame(x: 960, y: 10, width: 80, height: 20), confidence: 1)
        var leftCopy = line
        leftCopy.box.x -= Double(tiles[0].rect.minX)
        var rightCopy = line
        rightCopy.box.x -= Double(tiles[1].rect.minX)
        let kept =
            HandwritingRecognizer.boardLines([leftCopy], in: tiles[0])
            + HandwritingRecognizer.boardLines([rightCopy], in: tiles[1])
        XCTAssertEqual(kept.count, 1)
        XCTAssertEqual(kept.first?.box.x, 960)
    }

    func testAnEmptyBoardFilesNoText() async throws {
        let (notebook, page) = try whiteboard()
        let backend = TileBackend()
        let recognizer = HandwritingRecognizer(store: store, idleDelay: 0, backend: backend)
        let text = await recognizer.recognizeNow(RecognitionJob(notebookPath: notebook.path, page: page))
        XCTAssertEqual(text?.lines.count, 0)
        XCTAssertTrue(backend.images.isEmpty)
    }

    /// A board pass yields between tiles, so a second pass can start while
    /// the first is half way through. Whatever the order, what is filed in
    /// the end is never the older pass's reading.
    func testANewerPassWinsOverAnOlderOneStillReading() async throws {
        final class Backend: HandwritingRecognitionBackend, @unchecked Sendable {
            private let lock = NSLock()
            private var calls = 0
            private var spawned: Task<PageText?, Never>?
            var onFirstCall: (@Sendable () -> Task<PageText?, Never>)?

            var second: Task<PageText?, Never>? {
                lock.lock()
                defer { lock.unlock() }
                return spawned
            }

            func recognize(image: CGImage, pageSize: CGSize, languages: [String]) throws -> PageText {
                lock.lock()
                calls += 1
                let call = calls
                let first = call == 1 ? onFirstCall : nil
                lock.unlock()
                if let first {
                    let task = first()
                    lock.lock()
                    spawned = task
                    lock.unlock()
                }
                return PageText(lines: [
                    RecognizedLine(
                        text: "call \(call)",
                        box: PageObject.Frame(x: 462, y: 502, width: 100, height: 20), confidence: 1)
                ])
            }
        }
        let (notebook, page) = try whiteboard()
        // Three patches of ink, three tiles. While the first is read, the
        // board is written down to one patch and a second pass starts: it
        // has one tile to read, so without the guard the older pass, two
        // tiles behind, would file last.
        try store.writeDrawing(
            InkFixtures.data([
                InkFixtures.stroke(from: CGPoint(x: 2000, y: 3000), to: CGPoint(x: 2400, y: 3000)),
                InkFixtures.stroke(from: CGPoint(x: 7000, y: 6000), to: CGPoint(x: 7300, y: 6000)),
                InkFixtures.stroke(from: CGPoint(x: 12_000, y: 9000), to: CGPoint(x: 12_300, y: 9000)),
            ]), notebookPath: notebook.path, pageID: page.id)
        let backend = Backend()
        let recognizer = HandwritingRecognizer(store: store, idleDelay: 0, backend: backend)
        let job = RecognitionJob(notebookPath: notebook.path, page: page)
        let store = store!
        backend.onFirstCall = {
            try? store.writeDrawing(
                InkFixtures.data([
                    InkFixtures.stroke(from: CGPoint(x: 2000, y: 3000), to: CGPoint(x: 2400, y: 3000))
                ]), notebookPath: notebook.path, pageID: page.id)
            return Task { await recognizer.recognizeNow(job) }
        }
        _ = await recognizer.recognizeNow(job)
        let second = try XCTUnwrap(backend.second)
        _ = await second.value

        let filed = try XCTUnwrap(store.pageText(notebookPath: notebook.path, pageID: page.id))
        XCTAssertEqual(filed.lines.count, 1, "\(filed.lines.map(\.text))")
        XCTAssertFalse(filed.lines.contains { $0.text == "call 1" }, "\(filed.lines.map(\.text))")
    }
}
