import CoreGraphics
import Foundation

// MARK: - Paper colour

/// The colour of a sheet: paper for writing on, or a cover. Stored by name in
/// `page.json` (`paper_color`) and `notebook.json` (`paperColor`).
///
/// A name this build does not know decodes as `.white`, so a page a newer
/// build coloured is still a page here.
public enum PaperColor: String, Codable, CaseIterable, Sendable {
    case white, cream, dark, graphite, navy, forest, oxblood, ochre, sage, sand, slate

    /// What the Color row of the notebook and whiteboard sheets offers.
    public static let paperChoices: [PaperColor] = [.white, .cream, .dark]
    /// What the cover swatches offer, in this order.
    public static let coverChoices: [PaperColor] = [
        .navy, .graphite, .forest, .oxblood, .slate, .ochre, .sage, .sand,
    ]

    /// sRGB components, 0...1.
    public var rgb: (red: CGFloat, green: CGFloat, blue: CGFloat) {
        let value = hex
        return (
            CGFloat((value >> 16) & 0xff) / 255, CGFloat((value >> 8) & 0xff) / 255,
            CGFloat(value & 0xff) / 255
        )
    }

    /// `0xRRGGBB`. White is today's paper, cream is sampled from the owner's
    /// screenshot of the reference app's whiteboard.
    public var hex: UInt32 {
        switch self {
        case .white: return 0xFDFCFA
        case .cream: return 0xF7F7EA
        case .dark: return 0x2A2B2E
        case .graphite: return 0x34373C
        case .navy: return 0x23324A
        case .forest: return 0x2E4636
        case .oxblood: return 0x5B2A2E
        case .ochre: return 0xB5852F
        case .sage: return 0x8FA38A
        case .sand: return 0xD9CBAE
        case .slate: return 0x5C6B7A
        }
    }

    /// WCAG relative luminance of the sRGB colour, 0 (black) to 1 (white).
    public var relativeLuminance: CGFloat {
        func linear(_ channel: CGFloat) -> CGFloat {
            channel <= 0.04045 ? channel / 12.92 : pow((channel + 0.055) / 1.055, 2.4)
        }
        let (red, green, blue) = rgb
        return 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue)
    }

    /// Whether ink, rules and text on this colour use the dark appearance.
    /// Relative luminance below 0.4: of the cover colours only sand is light.
    public var isDark: Bool { relativeLuminance < 0.4 }

    /// Shown beside a swatch and read by VoiceOver.
    public var displayName: String {
        rawValue.prefix(1).uppercased() + rawValue.dropFirst()
    }

    public init(from decoder: Decoder) throws {
        let name = try decoder.singleValueContainer().decode(String.self)
        self = PaperColor.matching(rawValue: name) ?? .white
    }
}

// MARK: - The dark rule

/// The one place that decides how a page is drawn for its paper colour.
///
/// A page on dark paper is drawn with a dark appearance -- its canvas, its
/// render, its cover, its export and the minimap -- and every other page with
/// a light one. Stroke colours are always stored as their light-appearance
/// values; PencilKit maps them for display, so black ink shows light on dark
/// paper and dark again if the paper turns light.
public enum PaperAppearance {
    /// The only test for the dark appearance.
    public static func isDark(page: Page) -> Bool { page.paperColor.isDark }

    /// Rules, grid lines and dots on a page of this colour: black at 12 % on
    /// light paper, white at 14 % on dark.
    public static func ruleComponents(on color: PaperColor)
        -> (red: CGFloat, green: CGFloat, blue: CGFloat, alpha: CGFloat)
    {
        color.isDark ? (1, 1, 1, 0.14) : (0, 0, 0, 0.12)
    }

    /// A text object with no `color` of its own: `#1C1C1E` on light paper,
    /// `#F2F2F7` on dark.
    public static func defaultTextComponents(on color: PaperColor)
        -> (red: CGFloat, green: CGFloat, blue: CGFloat)
    {
        color.isDark
            ? (0xF2 / 255, 0xF2 / 255, 0xF7 / 255)
            : (0x1C / 255, 0x1C / 255, 0x1E / 255)
    }

    /// The cover template's single frame: white at 35 % on a dark cover,
    /// black at 20 % on a light one.
    public static func coverFrameComponents(on color: PaperColor)
        -> (red: CGFloat, green: CGFloat, blue: CGFloat, alpha: CGFloat)
    {
        color.isDark ? (1, 1, 1, 0.35) : (0, 0, 0, 0.20)
    }

    /// How far the cover frame sits inside the page edge, in points.
    public static let coverFrameInset: CGFloat = 24
    /// The cover frame's line width, in points.
    public static let coverFrameWidth: CGFloat = 1
}
