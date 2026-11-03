import PaperCore
import UIKit

/// A board's paper and its Square Grid or Dot Grid, drawn in screen space
/// under the canvas. It takes the place a page's `TemplateLayer` has.
///
/// It has no backing store. The paper is the view's background colour, and
/// every grid line or dot is one small `CALayer` repeated by
/// `CAReplicatorLayer`s, so the grid costs a few hundred quads whatever the
/// zoom or the size of the board, and moving it is setting a handful of
/// numbers. The board's canvas moves it from its own `layoutSubviews`, in the
/// same transaction that moves the ink, so the two never drift apart by a
/// frame. A drawn bitmap would have to be redrawn on every frame of a pan.
///
/// What it draws is `BoardGeometry`, which is also what renders and exports
/// draw with (`drawBackground`): the levels (`gridLevels(zoom:)`), the colours
/// (`gridLineComponents(on:)`, `gridDotComponents(on:)`) and the sizes
/// (`gridLinePixels`, `gridDotPixels`). Changing one of those changes the
/// screen, `render.png` and the PDF together.
final class BoardBackgroundView: UIView {
    /// `grid` is the Square Grid, `dotted` the Dot Grid; anything else is
    /// plain paper.
    var template: Template = .grid {
        didSet { if template != oldValue { layOut() } }
    }

    var paperColor: PaperColor = .cream {
        didSet {
            guard paperColor != oldValue else { return }
            backgroundColor = paperColor.uiColor
            layOut()
        }
    }

    /// The levels on screen now, finest first. For the tests.
    private(set) var levels: [BoardGeometry.GridLevel] = []

    private var zoom: CGFloat = 1
    /// Where the board's centre -- the grid's anchor -- is, in this view.
    private var anchor: CGPoint = .zero
    /// The board's extent on screen, in this view: the grid stops at its edge.
    private var boardRect: CGRect = .null
    private var lineSlots: [LineSlot] = []
    private var dotSlots: [DotSlot] = []

    override init(frame: CGRect) {
        super.init(frame: frame)
        isOpaque = true
        isUserInteractionEnabled = false
        isAccessibilityElement = false
        backgroundColor = paperColor.uiColor
        // Lines are a pixel wide, so a new display scale moves them all.
        registerForTraitChanges([UITraitDisplayScale.self]) { (view: BoardBackgroundView, _) in
            view.layOut()
        }
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("BoardBackgroundView is created in code") }

    override func layoutSubviews() {
        super.layoutSubviews()
        layOut()
    }

    /// Follows the canvas.
    /// - Parameters:
    ///   - zoom: screen points per board point.
    ///   - contentOffset: the canvas's scroll offset.
    ///   - extent: the board's extent, in board points.
    func update(zoom: CGFloat, contentOffset: CGPoint, extent: CGSize) {
        let centre = BoardGeometry.center(of: extent)
        self.zoom = zoom
        anchor = CGPoint(x: centre.x * zoom - contentOffset.x, y: centre.y * zoom - contentOffset.y)
        boardRect = CGRect(
            x: -contentOffset.x, y: -contentOffset.y, width: extent.width * zoom,
            height: extent.height * zoom)
        layOut()
    }

    private var pixel: CGFloat {
        let scale = traitCollection.displayScale
        return 1 / (scale > 0 ? scale : 2)
    }

    /// One flat colour that looks exactly like `components` laid over the
    /// paper at `strength`: opaque, so two lines of one level crossing do not
    /// darken where they cross, as one stroke of `drawBackground` does not.
    private func mixed(
        _ components: (red: CGFloat, green: CGFloat, blue: CGFloat, alpha: CGFloat),
        strength: CGFloat
    ) -> CGColor {
        let paper = paperColor.rgb
        let a = components.alpha * min(max(strength, 0), 1)
        return CGColor(
            srgbRed: paper.red + (components.red - paper.red) * a,
            green: paper.green + (components.green - paper.green) * a,
            blue: paper.blue + (components.blue - paper.blue) * a, alpha: 1)
    }

    private func layOut() {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        defer { CATransaction.commit() }
        let visible = bounds.intersection(boardRect)
        let drawsLines = template == .grid
        let drawsDots = template == .dotted
        guard !visible.isNull, !visible.isEmpty, drawsLines || drawsDots else {
            levels = []
            for slot in lineSlots { slot.hide() }
            for slot in dotSlots { slot.hide() }
            return
        }
        levels = BoardGeometry.gridLevels(zoom: zoom)
        if drawsLines {
            for slot in dotSlots { slot.hide() }
            while lineSlots.count < levels.count {
                let slot = LineSlot()
                layer.addSublayer(slot.vertical.replicator)
                layer.addSublayer(slot.horizontal.replicator)
                lineSlots.append(slot)
            }
            let width = BoardGeometry.gridLinePixels * pixel
            let components = BoardGeometry.gridLineComponents(on: paperColor)
            // Finest first, so a coarser line lies over a finer one where
            // they meet.
            for (index, slot) in lineSlots.enumerated() {
                guard index < levels.count else {
                    slot.hide()
                    continue
                }
                let level = levels[index]
                let colour = mixed(components, strength: level.opacity)
                let screen = level.spacing * zoom
                slot.vertical.lay(
                    run: Self.run(
                        anchor: anchor.x, spacing: screen, all: level.drawsAllLines,
                        from: visible.minX, to: visible.maxX),
                    across: visible, width: width, colour: colour, vertical: true)
                slot.horizontal.lay(
                    run: Self.run(
                        anchor: anchor.y, spacing: screen, all: level.drawsAllLines,
                        from: visible.minY, to: visible.maxY),
                    across: visible, width: width, colour: colour, vertical: false)
            }
        } else {
            for slot in lineSlots { slot.hide() }
            while dotSlots.count < levels.count {
                let slot = DotSlot()
                for group in slot.groups { layer.addSublayer(group.rows) }
                dotSlots.append(slot)
            }
            let diameter = BoardGeometry.gridDotPixels * pixel
            let components = BoardGeometry.gridDotComponents(on: paperColor)
            for (index, slot) in dotSlots.enumerated() {
                guard index < levels.count else {
                    slot.hide()
                    continue
                }
                let level = levels[index]
                let colour = mixed(components, strength: level.opacity)
                let screen = level.spacing * zoom
                if level.drawsAllLines {
                    // Every dot of the coarsest level.
                    slot.groups[0].lay(
                        rows: Self.run(
                            anchor: anchor.y, spacing: screen, all: true, from: visible.minY,
                            to: visible.maxY),
                        columns: Self.run(
                            anchor: anchor.x, spacing: screen, all: true, from: visible.minX,
                            to: visible.maxX),
                        in: visible, diameter: diameter, colour: colour)
                    slot.groups[1].hide()
                } else {
                    // A finer level leaves out the dots a coarser one draws:
                    // those on an even row and an even column. That is the odd
                    // rows whole, and the odd columns of the even rows.
                    slot.groups[0].lay(
                        rows: Self.run(
                            anchor: anchor.y, spacing: screen, all: false, from: visible.minY,
                            to: visible.maxY),
                        columns: Self.run(
                            anchor: anchor.x, spacing: screen, all: true, from: visible.minX,
                            to: visible.maxX),
                        in: visible, diameter: diameter, colour: colour)
                    slot.groups[1].lay(
                        rows: Self.run(
                            anchor: anchor.y, spacing: screen * 2, all: true, from: visible.minY,
                            to: visible.maxY),
                        columns: Self.run(
                            anchor: anchor.x, spacing: screen, all: false, from: visible.minX,
                            to: visible.maxX),
                        in: visible, diameter: diameter, colour: colour)
                }
            }
        }
    }

    // MARK: - Runs

    /// Where one level's lines fall along one axis between `lower` and
    /// `upper`: `first + i * stride` for `i` in `0..<count`. With `all` every
    /// line `anchor + k * spacing`; without, only those with an odd `k`, which
    /// are the ones no coarser level draws.
    struct Run: Equatable {
        var first: CGFloat
        var stride: CGFloat
        var count: Int
    }

    static func run(
        anchor: CGFloat, spacing: CGFloat, all: Bool, from lower: CGFloat, to upper: CGFloat
    ) -> Run {
        let stride = all ? spacing : spacing * 2
        let start = all ? anchor : anchor + spacing
        guard stride > 0, stride.isFinite, start.isFinite, lower.isFinite, upper > lower else {
            return Run(first: 0, stride: 1, count: 0)
        }
        let first = start + ((lower - start) / stride).rounded(.up) * stride
        guard first <= upper else { return Run(first: first, stride: stride, count: 0) }
        let count = Int(((upper - first) / stride).rounded(.down)) + 1
        // A run this long is a zoom the levels would never pick; refuse it
        // rather than ask the render server for a million layers.
        return Run(first: first, stride: stride, count: min(count, 20_000))
    }

    // MARK: - Layers

    /// One level of the Square Grid: its vertical lines and its horizontal ones.
    private final class LineSlot {
        let vertical = LineAxis()
        let horizontal = LineAxis()

        func hide() {
            vertical.replicator.isHidden = true
            horizontal.replicator.isHidden = true
        }
    }

    /// One level's lines along one axis: a replicator stepping by the run's
    /// stride over one line.
    private final class LineAxis {
        let replicator = CAReplicatorLayer()
        let line = CALayer()

        init() {
            replicator.addSublayer(line)
            // Edges on whole pixels, so a one-pixel line stays one pixel
            // wherever the zoom puts it.
            line.allowsEdgeAntialiasing = false
        }

        func lay(run: Run, across visible: CGRect, width: CGFloat, colour: CGColor, vertical: Bool) {
            replicator.isHidden = run.count == 0
            guard run.count > 0 else { return }
            replicator.frame = visible
            replicator.instanceCount = run.count
            line.backgroundColor = colour
            if vertical {
                replicator.instanceTransform = CATransform3DMakeTranslation(run.stride, 0, 0)
                line.frame = CGRect(
                    x: run.first - visible.minX - width / 2, y: 0, width: width,
                    height: visible.height)
            } else {
                replicator.instanceTransform = CATransform3DMakeTranslation(0, run.stride, 0)
                line.frame = CGRect(
                    x: 0, y: run.first - visible.minY - width / 2, width: visible.width,
                    height: width)
            }
        }
    }

    /// One level of the Dot Grid: two groups of rows of dots, because a finer
    /// level leaves gaps where the coarser one's dots are.
    private final class DotSlot {
        let groups = [DotGroup(), DotGroup()]

        func hide() {
            for group in groups { group.hide() }
        }
    }

    /// Rows of dots: a replicator of rows over a replicator of columns over
    /// one dot.
    private final class DotGroup {
        let rows = CAReplicatorLayer()
        let columns = CAReplicatorLayer()
        let dot = CALayer()

        init() {
            rows.addSublayer(columns)
            columns.addSublayer(dot)
        }

        func hide() { rows.isHidden = true }

        func lay(rows rowRun: Run, columns columnRun: Run, in visible: CGRect, diameter: CGFloat, colour: CGColor) {
            rows.isHidden = rowRun.count == 0 || columnRun.count == 0
            guard !rows.isHidden else { return }
            rows.frame = visible
            columns.frame = rows.bounds
            rows.instanceCount = rowRun.count
            rows.instanceTransform = CATransform3DMakeTranslation(0, rowRun.stride, 0)
            columns.instanceCount = columnRun.count
            columns.instanceTransform = CATransform3DMakeTranslation(columnRun.stride, 0, 0)
            dot.backgroundColor = colour
            dot.cornerRadius = diameter / 2
            dot.frame = CGRect(
                x: columnRun.first - visible.minX - diameter / 2,
                y: rowRun.first - visible.minY - diameter / 2, width: diameter, height: diameter)
        }
    }
}
