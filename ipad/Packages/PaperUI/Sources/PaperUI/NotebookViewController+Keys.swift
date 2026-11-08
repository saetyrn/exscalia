import UIKit

// MARK: - Keyboard

extension NotebookViewController {
    public override var canBecomeFirstResponder: Bool { true }

    public override var keyCommands: [UIKeyCommand]? {
        // Read Only keeps the keys that only move about, copy and share.
        if session.chrome.isReadOnly {
            return [
                command("]", [.command], #selector(commandNextPage), "Next page"),
                command("[", [.command], #selector(commandPreviousPage), "Previous page"),
                command("p", [.command], #selector(commandExport), "Export"),
                command("c", [.command], #selector(commandCopy), "Copy"),
            ]
        }
        return [
            command("z", [.command], #selector(commandUndo), "Undo"),
            command("z", [.command, .shift], #selector(commandRedo), "Redo"),
            command("n", [.command, .shift], #selector(commandNewPage), "New page"),
            command("]", [.command], #selector(commandNextPage), "Next page"),
            command("[", [.command], #selector(commandPreviousPage), "Previous page"),
            command("p", [.command], #selector(commandExport), "Export"),
            command("c", [.command], #selector(commandCopy), "Copy"),
            command("x", [.command], #selector(commandCut), "Cut"),
            command("v", [.command], #selector(commandPaste), "Paste"),
            command(UIKeyCommand.inputEscape, [], #selector(commandEscape), "Clear selection"),
            command("\u{8}", [], #selector(commandDeleteSelection), "Delete selection"),
        ]
    }

    private func command(
        _ input: String, _ modifiers: UIKeyModifierFlags, _ action: Selector, _ title: String
    ) -> UIKeyCommand {
        let command = UIKeyCommand(input: input, modifierFlags: modifiers, action: action)
        command.discoverabilityTitle = title
        command.wantsPriorityOverSystemBehavior = true
        return command
    }

    @objc private func commandUndo() { performUndo() }
    @objc private func commandRedo() { performRedo() }
    @objc private func commandNewPage() { session.addPage(after: session.currentIndex) }
    @objc private func commandNextPage() { scrollTo(index: session.currentIndex + 1, animated: true) }
    @objc private func commandPreviousPage() {
        scrollTo(index: session.currentIndex - 1, animated: true)
    }
    @objc private func commandEscape() { putDownSelectionFromKeyboard() }
    @objc private func commandExport() { presentExport(from: view) }
    @objc private func commandCopy() { copySelectionFromKeyboard() }
    @objc private func commandCut() { cutSelectionFromKeyboard() }
    @objc private func commandPaste() {
        if forwardToTextBeingEdited(#selector(UIResponderStandardEditActions.paste(_:))) { return }
        pasteFromClipboard()
    }
    @objc private func commandDeleteSelection() {
        guard pageViews.indices.contains(session.currentIndex) else { return }
        let view = pageViews[session.currentIndex]
        if view.hasSelectedObject {
            view.objects.deleteSelected()
        } else {
            view.lasso?.deleteSelection()
        }
    }

    /// Nothing is marked dirty here: an undone stroke or lasso edit reports
    /// itself through the canvas and an undone object edit through the
    /// objects layer, each for the page it actually changed -- which, with a
    /// shared undo manager, need not be the page under the viewport.
    func performUndo() {
        liveCanvas?.undoManager?.undo()
        historyDidMove()
    }

    func performRedo() {
        liveCanvas?.undoManager?.redo()
        historyDidMove()
    }

    /// A lasso selection is a list of stroke positions and a box drawn round
    /// them; after an undo or a redo neither need describe the page any more.
    private func historyDidMove() {
        for view in pageViews where view.isLive { view.lasso?.clearSelection() }
        refreshUndoState()
    }
}
