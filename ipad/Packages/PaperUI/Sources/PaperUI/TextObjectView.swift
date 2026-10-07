import PaperCore
import UIKit

/// A typed text object.
///
/// A plain `UITextView` with scrolling off, which is what makes Scribble,
/// selection, dictation and the system keyboard all work without a line of code
/// -- and what makes the box grow as the text does instead of hiding it. The
/// font is the body size in page points scaled by the zoom, so text written at
/// 2x is the same size on the page as text written at 1x.
///
/// The text view takes touches only while it is being typed in: until then a
/// tap selects the box rather than putting a caret in it.
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
        textView.tintColor = UIColor(PaperTokens.accent)
        textView.delegate = self
        textView.isUserInteractionEnabled = false
        textView.accessibilityLabel = "Text object"
        content.isUserInteractionEnabled = true
        content.addSubview(textView)

        applyText()
        applyPaper()
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

    override func paperDidChange() {
        applyPaper()
    }

    override var isEditingContent: Bool { textView.isFirstResponder }

    /// The colour the text shows in: its own `color`, mapped for dark paper as
    /// ink is, or the paper's text colour (§3.4).
    static func displayColor(of object: PageObject, darkPaper: Bool) -> UIColor {
        if case .string(let hex)? = object.additionalFields["color"],
            let color = PageRenderer.displayColor(hex: hex, dark: darkPaper)
        {
            return UIColor(cgColor: color)
        }
        let rgb = PaperAppearance.defaultTextComponents(on: darkPaper ? .dark : .white)
        return UIColor(red: rgb.red, green: rgb.green, blue: rgb.blue, alpha: 1)
    }

    /// The colour shown now, for the tests.
    var shownColor: UIColor? { textView.textColor }

    private func applyPaper() {
        // The keyboard, the caret and the selection follow the paper, as the
        // canvas does.
        textView.overrideUserInterfaceStyle = isDarkPaper ? .dark : .light
        textView.keyboardAppearance = isDarkPaper ? .dark : .light
        textView.textColor = Self.displayColor(of: object, darkPaper: isDarkPaper)
    }

    private func applyText() {
        let size = object.resolvedFontSize * zoom
        textView.font = .systemFont(ofSize: max(size, 1))
        if textView.text != (object.text ?? "") {
            textView.text = object.text ?? ""
        }
        textView.textColor = Self.displayColor(of: object, darkPaper: isDarkPaper)
        accessibilityLabel = object.text.map { "Text, \($0)" } ?? "Text"
    }

    override func beginEditingContent() {
        textView.isUserInteractionEnabled = true
        textView.isEditable = true
        textView.becomeFirstResponder()
    }

    override func endEditingContent() {
        guard textView.isFirstResponder else {
            textView.isUserInteractionEnabled = false
            return
        }
        textView.resignFirstResponder()
        textView.isUserInteractionEnabled = false
        commitText()
    }

    /// Fits the box to its text after a change of size.
    func fitToText() {
        layoutIfNeeded()
        fitHeight()
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
        textView.isUserInteractionEnabled = false
        commitText()
    }
}
