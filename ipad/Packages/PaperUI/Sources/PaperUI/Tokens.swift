import SwiftUI
import UIKit

/// Chrome colours mirroring the web app's `app.css`. The paper page itself is
/// always light; only the surrounding chrome follows system appearance.
public enum PaperTokens {
    public static let accent = Color(red: 0x6c / 255, green: 0x5c / 255, blue: 0xe7 / 255)
    public static let paper = Color(red: 0xfd / 255, green: 0xfc / 255, blue: 0xfa / 255)

    public static var background: Color { Color(uiColor: .systemBackground) }
    public static var panel: Color { Color(uiColor: .secondarySystemBackground) }
    public static var text: Color { Color(uiColor: .label) }
    public static var textMuted: Color { Color(uiColor: .secondaryLabel) }
    public static var border: Color { Color(uiColor: .separator) }

    /// Minimum hit target, per the accessibility rule in the plan.
    public static let minimumTarget: CGFloat = 44

    // MARK: Editor chrome
    //
    // Sampled from the owner's screenshot of the reference app, and the same
    // under light and dark appearance.

    /// The document tab strip above the navigation bar.
    public static let tabStrip = hex(0x1F335E)
    /// The navigation bar, which the toolbar sits in, and the active tab.
    public static let navigationBar = hex(0x294C80)
    /// An inactive tab's title and close cross.
    public static let tabInactiveTitle = hex(0xA1AECE)
    /// The rounded square behind the selected toolbar tool.
    public static let toolSelected = hex(0xC8D2DF)
    /// The selected tool's glyph on that square.
    public static let toolSelectedGlyph = hex(0x1F335E)
    /// Undo and redo, the tool menu, the zoom pill, the minimap, the board's
    /// limit banner and the object menu. Drawn with `floatingPillStyle` (or
    /// `applyFloatingPillStyle` on a `UIView`), never on its own.
    public static let floatingPill = hex(0x292C2D)
    /// The hairline round every floating pill. Dark paper (`#2A2B2E`) is
    /// nearly the pill's own colour, and without it a pill vanishes there.
    static let floatingPillHairline = UIColor(white: 1, alpha: 0.14)
    static let floatingPillHairlineWidth: CGFloat = 1
    static let floatingPillShadowOpacity: Float = 0.18
    static let floatingPillShadowRadius: CGFloat = 6
    static let floatingPillShadowOffset: CGFloat = 2
    /// The selected writing tool in the tool menu.
    public static let writingToolSelected = hex(0x3A8092)
    /// The selected stroke slot, and the ring round the selected colour.
    public static let floatingSelected = hex(0x494B4F)
    /// The rules between the tool menu's groups.
    public static let floatingDivider = hex(0x45484B)

    // MARK: Library
    //
    // Sampled from the owner's screenshots of the reference library, which
    // were taken under a dark appearance. The library follows the system
    // appearance; the light values are this app's own.

    /// Behind the library: black under a dark appearance.
    public static let libraryBackground = dynamic(dark: 0x000000, light: 0xFFFFFF)
    /// The round chips and the search pill in the library's top bar.
    public static let libraryChip = dynamic(dark: 0x212121, light: 0xEFEFF0)
    /// The rules between library rows, and the divider before the save state.
    public static let libraryHairline = dynamic(dark: 0x373737, light: 0xD8D8DA)
    /// The library sidebar's panel and its selected entry.
    public static let librarySidebar = dynamic(dark: 0x121212, light: 0xF4F4F5)
    public static let librarySidebarSelected = dynamic(dark: 0x2A2A2C, light: 0xE2E2E5)
    /// The "+ New" button, with white text.
    public static let newButton = hex(0x127DC6)

    static func hex(_ value: UInt32) -> Color {
        Color(
            .sRGB, red: Double((value >> 16) & 0xff) / 255,
            green: Double((value >> 8) & 0xff) / 255, blue: Double(value & 0xff) / 255)
    }

    /// The floating pill on a layer: its colour, a continuous corner, the
    /// hairline and the shadow.
    static func applyFloatingPillStyle(to view: UIView, cornerRadius: CGFloat) {
        view.backgroundColor = UIColor(floatingPill)
        view.layer.cornerRadius = cornerRadius
        view.layer.cornerCurve = .continuous
        view.layer.borderWidth = floatingPillHairlineWidth
        view.layer.borderColor = floatingPillHairline.cgColor
        view.layer.shadowColor = UIColor.black.cgColor
        view.layer.shadowOpacity = floatingPillShadowOpacity
        view.layer.shadowRadius = floatingPillShadowRadius
        view.layer.shadowOffset = CGSize(width: 0, height: floatingPillShadowOffset)
    }

    /// One colour under a dark appearance and another under a light one.
    static func dynamic(dark: UInt32, light: UInt32) -> Color {
        func color(_ value: UInt32) -> UIColor {
            UIColor(
                red: CGFloat((value >> 16) & 0xff) / 255, green: CGFloat((value >> 8) & 0xff) / 255,
                blue: CGFloat(value & 0xff) / 255, alpha: 1)
        }
        let darkColor = color(dark)
        let lightColor = color(light)
        return Color(
            uiColor: UIColor { traits in
                traits.userInterfaceStyle == .dark ? darkColor : lightColor
            })
    }
}

extension View {
    /// The floating pill in SwiftUI: `PaperTokens.floatingPill` behind, the
    /// hairline round it and the shadow under it. A popover, which draws its
    /// own edge, takes the colour alone (`floating: false`).
    func floatingPillStyle(cornerRadius: CGFloat, floating: Bool = true) -> some View {
        let shape = RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
        return
            self
            .background(shape.fill(PaperTokens.floatingPill))
            .clipShape(shape)
            .overlay {
                shape
                    .strokeBorder(
                        Color(uiColor: PaperTokens.floatingPillHairline),
                        lineWidth: floating ? PaperTokens.floatingPillHairlineWidth : 0)
                    .allowsHitTesting(false)
            }
            .shadow(
                color: .black.opacity(floating ? Double(PaperTokens.floatingPillShadowOpacity) : 0),
                radius: PaperTokens.floatingPillShadowRadius, y: PaperTokens.floatingPillShadowOffset)
    }
}
