import SwiftUI

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
}
