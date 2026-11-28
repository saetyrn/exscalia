import Foundation

/// What the New Notebook and New Whiteboard sheets choose, and what they open
/// with next time: "Create" saves the choices, and the next sheet of the same
/// kind loads them. A Quick Note takes the notebook's.
///
/// Stored as JSON under `defaultsKey(_:)`. Loading never fails: a missing or
/// unreadable value is the factory one, field by field, and a choice the
/// sheet does not offer for that kind is replaced by the factory value.
public struct NewDocumentOptions: Codable, Equatable, Sendable {
    /// Notebook: one of `Template.paperTemplates`. Whiteboard: one of
    /// `Template.boardTemplates`.
    public var template: Template
    /// The paper: one of `PaperColor.paperChoices`.
    public var paperColor: PaperColor
    /// Notebook: A4, Letter or iPad (`PaperSize.newNotebookChoices`).
    /// Whiteboard: always `.board`.
    public var size: PaperSize
    /// Notebooks only; a whiteboard is always `.portrait`.
    public var orientation: PageOrientation
    /// Notebooks only: page 1 is a cover. A whiteboard never has one.
    public var cover: Bool
    /// The cover's colour: one of `PaperColor.coverChoices`.
    public var coverColor: PaperColor

    public init(
        template: Template, paperColor: PaperColor, size: PaperSize,
        orientation: PageOrientation = .portrait, cover: Bool = false,
        coverColor: PaperColor = .navy
    ) {
        self.template = template
        self.paperColor = paperColor
        self.size = size
        self.orientation = orientation
        self.cover = cover
        self.coverColor = coverColor
    }

    /// Ruled, white, iPad, portrait, no cover (navy if turned on).
    public static let notebookFactory = NewDocumentOptions(
        template: .ruled, paperColor: .white, size: .iPad)
    /// Square Grid on cream.
    public static let whiteboardFactory = NewDocumentOptions(
        template: .grid, paperColor: .cream, size: .board)

    public static func factory(_ kind: DocumentKind) -> NewDocumentOptions {
        kind == .notebook ? notebookFactory : whiteboardFactory
    }

    /// `paper.newNotebook.options` or `paper.newWhiteboard.options`.
    public static func defaultsKey(_ kind: DocumentKind) -> String {
        kind == .notebook ? "paper.newNotebook.options" : "paper.newWhiteboard.options"
    }

    /// The choices the last "Create" of this kind saved, or the factory ones.
    public static func load(_ kind: DocumentKind, defaults: UserDefaults) -> NewDocumentOptions {
        let factory = factory(kind)
        guard let data = defaults.data(forKey: defaultsKey(kind))
            ?? defaults.string(forKey: defaultsKey(kind)).map({ Data($0.utf8) }),
            let stored = try? JSONDecoder().decode(Stored.self, from: data)
        else { return factory }
        let options = NewDocumentOptions(
            template: stored.template.flatMap(Template.matching(rawValue:)) ?? factory.template,
            paperColor: stored.paperColor.flatMap(PaperColor.matching(rawValue:))
                ?? factory.paperColor,
            size: stored.size.flatMap(PaperSize.matching(rawValue:)) ?? factory.size,
            orientation: stored.orientation.flatMap(PageOrientation.matching(rawValue:))
                ?? factory.orientation,
            cover: stored.cover ?? factory.cover,
            coverColor: stored.coverColor.flatMap(PaperColor.matching(rawValue:))
                ?? factory.coverColor)
        return options.sanitized(for: kind)
    }

    /// Remembers these choices for the next sheet of `kind`, as the sheet
    /// offers them.
    public func save(_ kind: DocumentKind, defaults: UserDefaults) {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        guard let data = try? encoder.encode(sanitized(for: kind)),
            let text = String(data: data, encoding: .utf8)
        else { return }
        // A string, so a launch argument can set it for one run as well.
        defaults.set(text, forKey: Self.defaultsKey(kind))
    }

    /// These options held to what a sheet of `kind` offers. Each choice it
    /// does not offer becomes the factory one; a whiteboard's size is the
    /// board and it has no cover or orientation.
    public func sanitized(for kind: DocumentKind) -> NewDocumentOptions {
        let factory = Self.factory(kind)
        var options = self
        if !PaperColor.paperChoices.contains(options.paperColor) {
            options.paperColor = factory.paperColor
        }
        if !PaperColor.coverChoices.contains(options.coverColor) {
            options.coverColor = factory.coverColor
        }
        switch kind {
        case .notebook:
            if !Template.paperTemplates.contains(options.template) {
                options.template = factory.template
            }
            if !PaperSize.newNotebookChoices.contains(options.size) { options.size = factory.size }
        case .whiteboard:
            if !Template.boardTemplates.contains(options.template) {
                options.template = factory.template
            }
            options.size = .board
            options.orientation = .portrait
            options.cover = false
        }
        return options
    }

    /// Every field optional and spelled as a name, so one bad value costs
    /// that field and not the rest.
    private struct Stored: Decodable {
        var template: String?
        var paperColor: String?
        var size: String?
        var orientation: String?
        var cover: Bool?
        var coverColor: String?

        enum CodingKeys: String, CodingKey {
            case template, paperColor, size, orientation, cover, coverColor
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            template = try? container.decode(String.self, forKey: .template)
            paperColor = try? container.decode(String.self, forKey: .paperColor)
            size = try? container.decode(String.self, forKey: .size)
            orientation = try? container.decode(String.self, forKey: .orientation)
            cover = try? container.decode(Bool.self, forKey: .cover)
            coverColor = try? container.decode(String.self, forKey: .coverColor)
        }
    }
}
