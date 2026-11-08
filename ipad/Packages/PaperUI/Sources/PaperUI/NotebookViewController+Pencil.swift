import UIKit

// MARK: - Apple Pencil

extension NotebookViewController: UIPencilInteractionDelegate {
    /// Double-tap follows the system preference rather than inventing one: a
    /// person who set it to "switch to eraser" everywhere else expects it
    /// here. The palette actions show the tool menu at the tip.
    public func pencilInteraction(
        _ interaction: UIPencilInteraction, didReceiveTap tap: UIPencilInteraction.Tap
    ) {
        switch UIPencilInteraction.preferredTapAction {
        case .switchEraser:
            tools.toggleEraser()
        case .switchPrevious:
            tools.switchToPrevious()
        case .showColorPalette, .showInkAttributes, .showContextualPalette:
            presentToolMenu(at: tap.hoverPose?.location)
        case .ignore, .runSystemShortcut:
            // The system is already doing something with this tap; a second
            // meaning on top of it would be a surprise.
            return
        @unknown default:
            return
        }
    }

    /// A Pencil Pro squeeze shows the tool menu at the tip once it lets go,
    /// unless the person turned squeeze off or gave it to a system shortcut.
    public func pencilInteraction(
        _ interaction: UIPencilInteraction, didReceiveSqueeze squeeze: UIPencilInteraction.Squeeze
    ) {
        guard squeeze.phase == .ended else { return }
        switch UIPencilInteraction.preferredSqueezeAction {
        case .ignore, .runSystemShortcut:
            return
        default:
            presentToolMenu(at: squeeze.hoverPose?.location)
        }
    }
}
