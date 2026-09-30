import PaperCore
import UIKit

/// A typed text object.
///
/// A plain `UITextView` with scrolling off, which is what makes Scribble,
/// selection, dictation and the system keyboard all work without a line of code
/// -- and what makes the box grow as the text does instead of hiding it. The
/// font is the body size in page points scaled by the zoom, so text written at
/// 2x is the same size on the page as text written at 1x.
final class TextObjectView: ObjectView {
    /// How long typing pauses before the text is persisted. Longer than the
    /// autosave debounce on purpose: a page.json write per keystroke is waste,
    /// and the text view still holds everything typed since.
    private static let typingDebounce: TimeInterval = 1.5

    private let textView = UITextView()
    private var typingWork: DispatchWorkItem?

    override init(object: PageObject) {
        super.init(object: object)
        textView.isScrollEnabled = false
        textView.backgroundColor = .clear
        textView.textContainerInset = .zero
        textView.textContainer.lineFragmentPadding = 0
        textView.textColor = UIColor(red: 0x1c / 255, green: 0x1c / 255, blue: 0x1e / 255, alpha: 1)
        textView.tintColor = UIColor(PaperTokens.accent)
        textView.delegate = self
        // The page is always light paper, so the text on it is too.
        textView.overrideUserInterfaceStyle = .light
        textView.accessibilityLabel = "Text object"
        textView.accessibilityHint = "Write with the Apple Pencil or type"
        content.addSubview(textView)

        applyText()
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        textView.frame = content.bounds
    }

    override func contentDidChange() {
        applyText()
    }

    override func zoomChanged() {
        applyText()
    }

    private func applyText() {
        let size = object.resolvedFontSize * zoom
        textView.font = .systemFont(ofSize: max(size, 1))
        if textView.text != (object.text ?? "") {
            textView.text = object.text ?? ""
        }
    }

    override func beginEditingContent() {
        textView.isEditable = true
        textView.becomeFirstResponder()
    }

    override func endEditingContent() {
        guard textView.isFirstResponder else { return }
        textView.resignFirstResponder()
        commitText()
    }

    /// Grows the frame downwards to whatever the text now needs. Text that is
    /// taller than its box would otherwise be invisible in the render, which
    /// the on-screen view would not show.
    private func fitHeight() {
        // Before the first layout the content is zero-width, and asking a text
        // view to fit itself into nothing returns a column one character wide.
        guard content.bounds.width > 1 else { return }
        let width = content.bounds.width
        let needed = textView.sizeThatFits(CGSize(width: width, height: .greatestFiniteMagnitude))
        let pageHeight = Double(max(needed.height / zoom, 24))
        guard abs(pageHeight - object.frame.height) > 0.5 else { return }
        update({ $0.frame.height = pageHeight }, actionName: "Edit Text")
        frame = Self.viewFrame(for: object.frame.rect, zoom: zoom)
    }

    private func commitText() {
        typingWork?.cancel()
        typingWork = nil
        let text = textView.text ?? ""
        guard text != (object.text ?? "") else {
            fitHeight()
            return
        }
        update({ $0.text = text }, actionName: "Edit Text")
        fitHeight()
    }
}

extension TextObjectView: UITextViewDelegate {
    func textViewDidChange(_ textView: UITextView) {
        typingWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            MainActor.assumeIsolated { self?.commitText() }
        }
        typingWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.typingDebounce, execute: work)
        fitHeight()
    }

    func textViewDidEndEditing(_ textView: UITextView) {
        commitText()
    }
}
