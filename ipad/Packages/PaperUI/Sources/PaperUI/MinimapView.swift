import PaperCore
import UIKit

/// A whiteboard's minimap: the whole of what is on the board and where the
/// screen is on it, 200 x 140 points above the zoom pill.
///
/// Its world is the content and the viewport together, grown by a tenth and
/// widened to the map's shape (`BoardGeometry.Minimap`), so both always show.
/// The ink is a picture its board renders off the main thread
/// (`PKDrawing.image(from:scale:)`); images and shapes are grey boxes, and
/// the screen is an outline. Between pictures the last one is laid where its
/// world falls in the current one, so the map keeps up with a pan.
///
/// A finger dragged inside it moves the view there; a double-tap fits the
/// content.
final class MinimapView: UIView {
    /// Centre the board on this canvas point.
    var onMove: ((CGPoint) -> Void)?
    var onFit: (() -> Void)?

    private(set) var map = BoardGeometry.Minimap(
        content: nil, viewport: CGRect(x: 0, y: 0, width: 1, height: 1))
    /// The world the ink picture was drawn for, or null before the first.
    private(set) var imageWorld: CGRect = .null
    /// While a finger drags inside the map, its world holds still under it:
    /// the view it moves would otherwise move the map under the finger.
    private(set) var isDragging = false

    private let paper = UIView()
    private let imageView = UIImageView()
    private let boxes = CAShapeLayer()
    private let viewport = CAShapeLayer()

    override init(frame: CGRect) {
        super.init(frame: frame)
        PaperTokens.applyFloatingPillStyle(to: self, cornerRadius: 12)

        paper.layer.cornerRadius = 9
        paper.layer.cornerCurve = .continuous
        paper.clipsToBounds = true
        paper.isUserInteractionEnabled = false
        addSubview(paper)
        imageView.contentMode = .scaleToFill
        paper.addSubview(imageView)
        boxes.fillColor = UIColor(white: 0.55, alpha: 0.35).cgColor
        boxes.strokeColor = UIColor(white: 0.45, alpha: 0.6).cgColor
        boxes.lineWidth = 0.5
        paper.layer.addSublayer(boxes)
        viewport.fillColor = UIColor(PaperTokens.accent).withAlphaComponent(0.10).cgColor
        viewport.strokeColor = UIColor(PaperTokens.accent).cgColor
        viewport.lineWidth = 1.5
        paper.layer.addSublayer(viewport)

        let press = UILongPressGestureRecognizer(target: self, action: #selector(handlePress(_:)))
        // A touch that lands moves the view at once, before it travels.
        press.minimumPressDuration = 0
        press.allowableMovement = .greatestFiniteMagnitude
        let doubleTap = UITapGestureRecognizer(target: self, action: #selector(handleDoubleTap(_:)))
        doubleTap.numberOfTapsRequired = 2
        press.delegate = self
        for gesture in [press, doubleTap] as [UIGestureRecognizer] {
            gesture.allowedTouchTypes = [
                NSNumber(value: UITouch.TouchType.direct.rawValue),
                NSNumber(value: UITouch.TouchType.pencil.rawValue),
            ]
            addGestureRecognizer(gesture)
        }

        isAccessibilityElement = true
        accessibilityIdentifier = "board.minimapView"
        accessibilityLabel = "Minimap"
        accessibilityTraits = [.allowsDirectInteraction]
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("MinimapView is created in code") }

    /// The map inside the dark frame.
    static let inset: CGFloat = 3
    /// The whole view.
    static let size = CGSize(
        width: BoardGeometry.Minimap.defaultSize.width,
        height: BoardGeometry.Minimap.defaultSize.height)

    private var mapSize: CGSize {
        CGSize(width: bounds.width - Self.inset * 2, height: bounds.height - Self.inset * 2)
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        paper.frame = bounds.insetBy(dx: Self.inset, dy: Self.inset)
        layOutContents()
    }

    /// The paper the board is on.
    func setPaper(_ color: PaperColor) {
        paper.backgroundColor = color.uiColor
        viewport.strokeColor =
            color.isDark ? UIColor.white.cgColor : UIColor(PaperTokens.accent).cgColor
    }

    private var content: CGRect?
    private var visible: CGRect = .null
    private var objectFrames: [CGRect] = []

    /// The board moved or changed. Returns whether the ink picture is now
    /// for another world than the map's, so the board draws a new one.
    @discardableResult
    func update(content: CGRect?, viewport visible: CGRect, objects: [CGRect]) -> Bool {
        self.content = content
        self.visible = visible
        objectFrames = objects
        if !isDragging, mapSize.width > 0, mapSize.height > 0 {
            map = BoardGeometry.Minimap(content: content, viewport: visible, size: mapSize)
        }
        layOutContents()
        return needsImage
    }

    /// The map's world has no picture drawn for it.
    var needsImage: Bool { imageWorld != map.world }

    /// A picture of the ink over `world`, from the board: nil when there is
    /// no ink, and a null world to throw the picture away.
    func setImage(_ image: UIImage?, world: CGRect) {
        imageView.image = image
        imageWorld = world
        layOutContents()
    }

    private func layOutContents() {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        defer { CATransaction.commit() }
        imageView.isHidden = imageWorld.isNull || imageView.image == nil
        if !imageWorld.isNull { imageView.frame = map.minimapRect(fromCanvas: imageWorld) }
        let path = CGMutablePath()
        for frame in objectFrames {
            let rect = map.minimapRect(fromCanvas: frame)
            path.addRect(CGRect(
                x: rect.minX, y: rect.minY, width: max(rect.width, 1.5),
                height: max(rect.height, 1.5)))
        }
        boxes.path = path
        viewport.path =
            visible.isNull
            ? nil
            : UIBezierPath(roundedRect: map.minimapRect(fromCanvas: visible), cornerRadius: 2)
                .cgPath
    }

    // MARK: - Touch

    private func move(to location: CGPoint) {
        let point = CGPoint(x: location.x - Self.inset, y: location.y - Self.inset)
        onMove?(map.canvasPoint(fromMinimap: point))
    }

    @objc private func handlePress(_ gesture: UILongPressGestureRecognizer) {
        switch gesture.state {
        case .began:
            isDragging = true
            move(to: gesture.location(in: self))
        case .changed:
            move(to: gesture.location(in: self))
        case .ended, .cancelled, .failed:
            isDragging = false
            update(content: content, viewport: visible, objects: objectFrames)
        default:
            break
        }
    }

    @objc private func handleDoubleTap(_ gesture: UITapGestureRecognizer) {
        guard gesture.state == .ended else { return }
        onFit?()
    }

    override func accessibilityActivate() -> Bool {
        onFit?()
        return true
    }
}

extension MinimapView: UIGestureRecognizerDelegate {
    func gestureRecognizer(
        _ gestureRecognizer: UIGestureRecognizer,
        shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer
    ) -> Bool {
        other.view === self
    }
}
