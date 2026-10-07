import PaperCore
import PencilKit
import UIKit

/// Who may ink. The Pencil draws and fingers navigate; there is no setting.
@MainActor
enum EditorInput {
    /// DEBUG `-debugFingerInk`: a finger inks and lassos like the Pencil and
    /// panning takes two fingers, so XCUITest, which has no Pencil, can put
    /// ink down. It is a launch argument for UI tests, not a setting.
    static var fingerInk: Bool {
        #if DEBUG
            ProcessInfo.processInfo.arguments.contains("-debugFingerInk")
        #else
            false
        #endif
    }

    /// Every canvas's drawing policy.
    static var drawingPolicy: PKCanvasViewDrawingPolicy { fingerInk ? .anyInput : .pencilOnly }
}

/// The finger gestures the notebook adds to its scroll view: a double-tap
/// zooms, two fingers double-tapping undo, three redo. The taps that select
/// and place objects are `objectTap` and `PageView`'s insert taps.
///
/// Every one of them takes `.direct` touches and none cancels, delays or ends
/// late the touches pages and objects get. The controller is their delegate,
/// so each recognises alongside anything but the lasso.
@MainActor
final class EditorGestures: NSObject {
    private weak var controller: NotebookViewController?
    let undoTap = UITapGestureRecognizer()
    let redoTap = UITapGestureRecognizer()

    init(controller: NotebookViewController) {
        self.controller = controller
        super.init()
        let direct = NSNumber(value: UITouch.TouchType.direct.rawValue)

        controller.zoomDoubleTap.addTarget(self, action: #selector(zoomTapped(_:)))

        undoTap.numberOfTouchesRequired = 2
        redoTap.numberOfTouchesRequired = 3
        for tap in [undoTap, redoTap] {
            tap.numberOfTapsRequired = 2
            tap.allowedTouchTypes = [direct]
        }
        undoTap.addTarget(self, action: #selector(undoTapped(_:)))
        redoTap.addTarget(self, action: #selector(redoTapped(_:)))

        for tap in [undoTap, redoTap] {
            tap.cancelsTouchesInView = false
            tap.delaysTouchesEnded = false
            tap.delegate = controller
            controller.scrollView.addGestureRecognizer(tap)
        }
    }

    @objc private func zoomTapped(_ gesture: UITapGestureRecognizer) {
        guard gesture.state == .ended, let controller else { return }
        controller.toggleZoom(at: gesture.location(in: controller.stack))
    }

    @objc private func undoTapped(_ gesture: UITapGestureRecognizer) {
        guard gesture.state == .ended, let controller, !controller.isDrawing else { return }
        controller.performUndo()
    }

    @objc private func redoTapped(_ gesture: UITapGestureRecognizer) {
        guard gesture.state == .ended, let controller, !controller.isDrawing else { return }
        controller.performRedo()
    }
}

extension NotebookViewController {
    /// The one-finger double-tap: between fit-width and twice that, about the
    /// tap. A double-tap on a selected object belongs to the object.
    func toggleZoom(at anchor: CGPoint) {
        guard !isDrawing else { return }
        if let page = pageViews.first(where: { $0.frame.contains(anchor) }),
            page.hasSelectedObject
        {
            return
        }
        zoom(to: abs(zoom - 1) < 0.01 ? 2 : 1, about: anchor)
    }

    /// Zooms keeping the point `anchor` (stack coordinates) under the finger,
    /// as a pinch does.
    func zoom(to next: CGFloat, about anchor: CGPoint) {
        let previous = zoom
        let offset = scrollView.contentOffset
        let anchorInView = CGPoint(x: anchor.x - offset.x, y: anchor.y - offset.y)
        setZoom(next)
        guard previous > 0 else { return }
        let factor = zoom / previous
        let ceilingX = max(0, scrollView.contentSize.width - scrollView.bounds.width)
        let ceilingY = max(0, scrollView.contentSize.height - scrollView.bounds.height)
        scrollView.contentOffset = CGPoint(
            x: min(max(0, anchor.x * factor - anchorInView.x), ceilingX),
            y: min(max(0, anchor.y * factor - anchorInView.y), ceilingY))
        session.zoom = zoom
        schedulePersist()
    }
}
