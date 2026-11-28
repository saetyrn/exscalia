import PaperCore
import SwiftUI
import UIKit

// MARK: - Colour

extension FolderColor {
    /// The system colour of the same name, which follows the appearance.
    var uiColor: UIColor {
        switch self {
        case .red: return .systemRed
        case .orange: return .systemOrange
        case .yellow: return .systemYellow
        case .green: return .systemGreen
        case .mint: return .systemMint
        case .teal: return .systemTeal
        case .blue: return .systemBlue
        case .indigo: return .systemIndigo
        case .purple: return .systemPurple
        case .pink: return .systemPink
        case .brown: return .systemBrown
        case .gray: return .systemGray
        }
    }

    var swatch: Color { Color(uiColor: uiColor) }

    /// The back of the folder, behind the tab: the colour deeper and a
    /// little darker. Measured on the reference's folders, the back is about
    /// 0.16 more saturated and 6 % darker than the front.
    var backSwatch: Color {
        let base = uiColor
        return Color(
            uiColor: UIColor { traits in
                FolderColor.shade(base.resolvedColor(with: traits), saturation: 0.16, brightness: 0.935)
            })
    }

    /// The light edge along the top of the front: the colour 12 % of the
    /// way to white.
    var highlightSwatch: Color {
        let base = uiColor
        return Color(
            uiColor: UIColor { traits in
                FolderColor.tint(base.resolvedColor(with: traits), toward: 1, by: 0.12)
            })
    }

    static func shade(_ color: UIColor, saturation: CGFloat, brightness: CGFloat) -> UIColor {
        var hue: CGFloat = 0, sat: CGFloat = 0, value: CGFloat = 0, alpha: CGFloat = 0
        guard color.getHue(&hue, saturation: &sat, brightness: &value, alpha: &alpha) else {
            return color
        }
        return UIColor(
            hue: hue, saturation: min(1, sat + saturation), brightness: value * brightness,
            alpha: alpha)
    }

    static func tint(_ color: UIColor, toward white: CGFloat, by amount: CGFloat) -> UIColor {
        var red: CGFloat = 0, green: CGFloat = 0, blue: CGFloat = 0, alpha: CGFloat = 0
        guard color.getRed(&red, green: &green, blue: &blue, alpha: &alpha) else { return color }
        func mix(_ value: CGFloat) -> CGFloat { value + (white - value) * amount }
        return UIColor(red: mix(red), green: mix(green), blue: mix(blue), alpha: alpha)
    }
}

// MARK: - Icon rules

/// The emoji a folder may show: exactly one grapheme, and an emoji rather
/// than text. `FolderIcon.isValidEmoji` is the wire rule (1-16 code points,
/// no ASCII, whitespace or controls); this is the stricter rule the picker
/// holds the owner to.
enum FolderEmoji {
    /// Whether `character` is one emoji. A symbol that defaults to text
    /// presentation, such as U+2764 or U+00A9, counts only with the emoji
    /// variation selector after it.
    static func isEmoji(_ character: Character) -> Bool {
        guard FolderIcon.isValidEmoji(String(character)),
            let first = character.unicodeScalars.first
        else { return false }
        if first.properties.isEmojiPresentation { return true }
        return first.properties.isEmoji && character.unicodeScalars.contains("\u{FE0F}")
    }

    /// The one emoji `text` holds, ignoring whitespace round it; `nil` for
    /// anything else, text or two emoji alike.
    static func single(_ text: String) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count == 1, let character = trimmed.first, isEmoji(character) else {
            return nil
        }
        return trimmed
    }

    /// What the emoji field holds after an edit that would leave it reading
    /// `proposed`, when it held `current`. Text is refused (the field keeps
    /// `current`), a second emoji replaces the first instead of joining it,
    /// and clearing the field clears it.
    static func filtered(_ proposed: String, current: String) -> String {
        let text = proposed.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return "" }
        guard text.allSatisfy(isEmoji) else { return current }
        let added = text.filter { String($0) != current }
        return added.last.map(String.init) ?? current
    }
}

/// Names for the 24 symbols of `FolderIcon.symbolChoices`, for VoiceOver and
/// tooltips.
enum FolderSymbolNames {
    static func name(for symbol: String) -> String {
        switch symbol {
        case "folder": return "Folder"
        case "book.closed": return "Book"
        case "books.vertical": return "Books"
        case "graduationcap": return "Graduation Cap"
        case "briefcase": return "Briefcase"
        case "doc.text": return "Document"
        case "pencil": return "Pencil"
        case "paintbrush": return "Paintbrush"
        case "lightbulb": return "Light Bulb"
        case "star": return "Star"
        case "heart": return "Heart"
        case "flag": return "Flag"
        case "bookmark": return "Bookmark"
        case "calendar": return "Calendar"
        case "clock": return "Clock"
        case "chart.bar": return "Chart"
        case "function": return "Function"
        case "atom": return "Atom"
        case "leaf": return "Leaf"
        case "globe": return "Globe"
        case "house": return "House"
        case "person": return "Person"
        case "music.note": return "Music Note"
        case "camera": return "Camera"
        default:
            return symbol.split(separator: ".").map { $0.prefix(1).uppercased() + $0.dropFirst() }
                .joined(separator: " ")
        }
    }
}

// MARK: - Folder thumbnail

/// The folder Paper draws: a back panel with its tab, and the front over it.
/// Our own drawing, proportioned on the owner's screenshot of the reference
/// library: the tab is a little over a third of the width, the front covers
/// all but the top sixth.
struct FolderShape: Shape {
    enum Part { case back, front }
    let part: Part

    /// Height over width.
    static let aspect: CGFloat = 0.85
    /// Where the front starts, as a fraction of the height.
    static let frontTop: CGFloat = 0.16
    /// The tab's height, as a fraction of the height.
    static let tabHeight: CGFloat = 0.08
    /// Where the tab's top ends and where its slope lands, as fractions of
    /// the width.
    static let tabEnd: CGFloat = 0.36
    static let slopeEnd: CGFloat = 0.45

    func path(in rect: CGRect) -> Path {
        let width = rect.width, height = rect.height
        let radius = width * 0.07
        let front = CGRect(
            x: rect.minX, y: rect.minY + height * Self.frontTop,
            width: width, height: height * (1 - Self.frontTop))
        switch part {
        case .front:
            return RoundedRectangle(cornerRadius: radius, style: .continuous).path(in: front)
        case .back:
            // Down only as far as the front's top corners, so the two never
            // share an edge for the antialiasing to fringe.
            let bottom = front.minY + radius
            let band = rect.minY + height * Self.tabHeight
            let tabEnd = rect.minX + width * Self.tabEnd
            let slopeEnd = rect.minX + width * Self.slopeEnd
            let ease = width * 0.04
            var path = Path()
            path.move(to: CGPoint(x: rect.minX, y: bottom))
            path.addLine(to: CGPoint(x: rect.minX, y: rect.minY + radius))
            path.addQuadCurve(
                to: CGPoint(x: rect.minX + radius, y: rect.minY),
                control: CGPoint(x: rect.minX, y: rect.minY))
            path.addLine(to: CGPoint(x: tabEnd, y: rect.minY))
            path.addCurve(
                to: CGPoint(x: slopeEnd, y: band),
                control1: CGPoint(x: tabEnd + ease, y: rect.minY),
                control2: CGPoint(x: slopeEnd - ease, y: band))
            path.addLine(to: CGPoint(x: rect.maxX - radius, y: band))
            path.addQuadCurve(
                to: CGPoint(x: rect.maxX, y: band + radius), control: CGPoint(x: rect.maxX, y: band))
            path.addLine(to: CGPoint(x: rect.maxX, y: bottom))
            path.closeSubpath()
            return path
        }
    }
}

/// A folder as the library draws it: `FolderShape` in two tones of its
/// colour, the back deeper than the front, with its symbol or emoji on the
/// front. A symbol this device lacks draws the plain folder.
struct FolderThumbnail: View {
    let color: FolderColor
    let icon: FolderIcon?
    /// The folder's width in points.
    var size: CGFloat = 44

    var body: some View {
        ZStack {
            FolderShape(part: .back).fill(color.backSwatch)
            // The front's top edge catches the light: a short fade from the
            // tint to the colour.
            FolderShape(part: .front)
                .fill(
                    LinearGradient(
                        stops: [
                            .init(color: color.highlightSwatch, location: FolderShape.frontTop),
                            .init(color: color.swatch, location: FolderShape.frontTop + 0.08),
                        ], startPoint: .top, endPoint: .bottom))
            mark
                // The middle of the front, below the tab.
                .offset(y: size * FolderShape.aspect * FolderShape.frontTop / 2)
        }
        .frame(width: size, height: size * FolderShape.aspect)
        .accessibilityHidden(true)
    }

    @ViewBuilder
    private var mark: some View {
        switch icon {
        case .symbol(let name) where UIImage(systemName: name) != nil:
            Image(systemName: name)
                .font(.system(size: size * 0.3, weight: .semibold))
                .symbolRenderingMode(.monochrome)
                // A darker shade of the folder itself, which reads on all
                // twelve colours in both appearances.
                .foregroundStyle(.black.opacity(0.32))
        case .emoji(let text):
            Text(text).font(.system(size: size * 0.34))
        default:
            EmptyView()
        }
    }
}

// MARK: - Pickers

/// The folder's colour and icon, as two form sections: Color, the twelve
/// swatches; Icon, None, the 24 symbols and an emoji field. The New Folder
/// sheet uses both; a menu that edits only one can use `FolderColorPicker` or
/// `FolderIconPicker` alone.
struct FolderAppearancePicker: View {
    @Binding var color: FolderColor
    @Binding var icon: FolderIcon?
    /// Return in the emoji field.
    var onSubmit: () -> Void = {}

    var body: some View {
        Section("Color") {
            FolderColorPicker(selection: $color)
        }
        Section("Icon") {
            FolderIconPicker(selection: $icon, onSubmit: onSubmit)
        }
    }
}

/// The twelve folder colours as swatches.
struct FolderColorPicker: View {
    @Binding var selection: FolderColor

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 44, maximum: 56), spacing: 6)], spacing: 10) {
            ForEach(FolderColor.allCases, id: \.self) { color in
                Button {
                    selection = color
                } label: {
                    Circle()
                        .fill(color.swatch)
                        .frame(width: 30, height: 30)
                        .padding(4)
                        .overlay(
                            Circle().strokeBorder(
                                selection == color ? Color.primary : .clear, lineWidth: 2)
                        )
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help(color.displayName)
                .accessibilityLabel(color.displayName)
                .accessibilityAddTraits(selection == color ? .isSelected : [])
            }
        }
        .padding(.vertical, 4)
    }
}

/// None, the 24 symbols, and a field for one emoji.
struct FolderIconPicker: View {
    @Binding var selection: FolderIcon?
    var onSubmit: () -> Void = {}
    @State private var emoji: String

    init(selection: Binding<FolderIcon?>, onSubmit: @escaping () -> Void = {}) {
        _selection = selection
        self.onSubmit = onSubmit
        if case .emoji(let text) = selection.wrappedValue {
            _emoji = State(initialValue: text)
        } else {
            _emoji = State(initialValue: "")
        }
    }

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 44, maximum: 52), spacing: 6)], spacing: 6) {
            cell(symbol: "nosign", label: "None", selected: selection == nil) {
                selection = nil
            }
            ForEach(FolderIcon.symbolChoices, id: \.self) { symbol in
                cell(
                    symbol: symbol, label: FolderSymbolNames.name(for: symbol),
                    selected: selection == .symbol(symbol)
                ) {
                    selection = .symbol(symbol)
                }
            }
        }
        .padding(.vertical, 4)
        // A plain row rather than `LabeledContent`, which would stretch the
        // field's accessibility frame across the row and repeat its label.
        HStack {
            Text("Emoji")
                .accessibilityHidden(true)
            Spacer(minLength: 12)
            EmojiField(text: $emoji, onSubmit: onSubmit)
                .frame(width: 64, height: 36)
                .background(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .strokeBorder(isEmojiSelected ? Color.primary : Color(uiColor: .separator),
                            lineWidth: isEmojiSelected ? 2 : 1))
        }
        .onChange(of: emoji) { _, text in
            if let single = FolderEmoji.single(text) {
                if selection != .emoji(single) { selection = .emoji(single) }
            } else if case .emoji = selection {
                selection = nil
            }
        }
        .onChange(of: selection) { _, icon in
            if case .emoji(let text) = icon {
                if emoji != text { emoji = text }
            } else if !emoji.isEmpty {
                emoji = ""
            }
        }
    }

    private var isEmojiSelected: Bool {
        if case .emoji = selection { return true }
        return false
    }

    private func cell(symbol: String, label: String, selected: Bool, action: @escaping () -> Void)
        -> some View
    {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 19, weight: .regular))
                .symbolRenderingMode(.monochrome)
                .foregroundStyle(Color.primary)
                .frame(width: 44, height: 44)
                .background(
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .fill(selected ? Color.primary.opacity(0.12) : .clear))
                .overlay(
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .strokeBorder(selected ? Color.primary : .clear, lineWidth: 1.5))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(label)
        .accessibilityLabel(label)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

// MARK: - Emoji field

/// A one-emoji text field. It opens the emoji keyboard, and every edit goes
/// through `FolderEmoji.filtered` before it lands, so text never shows.
struct EmojiField: UIViewRepresentable {
    @Binding var text: String
    var onSubmit: () -> Void = {}

    func makeUIView(context: Context) -> EmojiTextField {
        let field = EmojiTextField()
        field.delegate = context.coordinator
        field.text = text
        field.textAlignment = .center
        field.font = .systemFont(ofSize: 24)
        field.returnKeyType = .done
        field.autocorrectionType = .no
        field.autocapitalizationType = .none
        field.spellCheckingType = .no
        field.smartInsertDeleteType = .no
        field.smartQuotesType = .no
        field.smartDashesType = .no
        field.inlinePredictionType = .no
        field.accessibilityLabel = "Emoji"
        field.addTarget(
            context.coordinator, action: #selector(Coordinator.editingChanged(_:)),
            for: .editingChanged)
        return field
    }

    func updateUIView(_ field: EmojiTextField, context: Context) {
        context.coordinator.parent = self
        if field.text != text, field.markedTextRange == nil { field.text = text }
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    final class Coordinator: NSObject, UITextFieldDelegate {
        var parent: EmojiField

        init(_ parent: EmojiField) { self.parent = parent }

        func textField(
            _ field: UITextField, shouldChangeCharactersIn range: NSRange,
            replacementString string: String
        ) -> Bool {
            let current = field.text ?? ""
            guard let swiftRange = Range(range, in: current) else { return false }
            let proposed = current.replacingCharacters(in: swiftRange, with: string)
            let result = FolderEmoji.filtered(proposed, current: current)
            if result == proposed { return true }
            field.text = result
            publish(result)
            return false
        }

        /// Catches what bypasses the delegate, such as committed marked text.
        @objc func editingChanged(_ field: UITextField) {
            guard field.markedTextRange == nil else { return }
            let value = field.text ?? ""
            let result = FolderEmoji.filtered(value, current: parent.text)
            if result != value { field.text = result }
            publish(result)
        }

        func textFieldShouldReturn(_ field: UITextField) -> Bool {
            parent.onSubmit()
            return false
        }

        private func publish(_ value: String) {
            if parent.text != value { parent.text = value }
        }
    }
}

/// A text field that asks for the emoji keyboard.
final class EmojiTextField: UITextField {
    override var textInputMode: UITextInputMode? {
        UITextInputMode.activeInputModes.first { $0.primaryLanguage == "emoji" }
            ?? super.textInputMode
    }
}
