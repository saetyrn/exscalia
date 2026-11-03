import PaperCore
import SwiftUI
import UIKit

/// A whiteboard's zoom pill, at the bottom left of the board: the zoom as a
/// percentage, which opens Fit Content and the fixed zooms, and beside it in
/// the same dark pill the button that shows or hides the minimap.
final class ZoomPill: UIView {
    /// The pill asks for a zoom; the board does it.
    var onZoom: ((ZoomRequest) -> Void)?
    var onToggleMinimap: (() -> Void)?

    let zoomButton = UIButton(type: .system)
    let minimapButton = UIButton(type: .system)
    private let divider = UIView()
    /// The percentage showing, "56%".
    private(set) var label: String?
    private(set) var isMinimapVisible = true

    static let height: CGFloat = 44

    override init(frame: CGRect) {
        super.init(frame: frame)
        PaperTokens.applyFloatingPillStyle(to: self, cornerRadius: 12)

        var zoom = UIButton.Configuration.plain()
        zoom.baseForegroundColor = UIColor(white: 0.86, alpha: 1)
        zoom.contentInsets = NSDirectionalEdgeInsets(top: 0, leading: 14, bottom: 0, trailing: 14)
        zoomButton.configuration = zoom
        zoomButton.showsMenuAsPrimaryAction = true
        zoomButton.menu = makeMenu()
        zoomButton.accessibilityIdentifier = "board.zoom"
        zoomButton.toolTip = "Zoom"

        var map = UIButton.Configuration.plain()
        map.image = UIImage(
            systemName: "map", withConfiguration: UIImage.SymbolConfiguration(pointSize: 16))
        map.contentInsets = NSDirectionalEdgeInsets(top: 0, leading: 12, bottom: 0, trailing: 12)
        minimapButton.configuration = map
        minimapButton.configurationUpdateHandler = { [weak self] button in
            guard var configuration = button.configuration else { return }
            let on = self?.isMinimapVisible ?? false
            configuration.baseForegroundColor = UIColor(white: on ? 1 : 0.86, alpha: 1)
            configuration.background.backgroundColor =
                on ? UIColor(PaperTokens.floatingSelected) : .clear
            configuration.background.cornerRadius = 8
            configuration.background.backgroundInsets = NSDirectionalEdgeInsets(
                top: 5, leading: 4, bottom: 5, trailing: 4)
            button.configuration = configuration
        }
        minimapButton.addAction(
            UIAction { [weak self] _ in self?.onToggleMinimap?() }, for: .touchUpInside)
        minimapButton.accessibilityIdentifier = "board.minimap"
        minimapButton.accessibilityLabel = "Minimap"
        minimapButton.toolTip = "Minimap"

        divider.backgroundColor = UIColor(PaperTokens.floatingDivider)
        divider.isAccessibilityElement = false

        let stack = UIStackView(arrangedSubviews: [zoomButton, divider, minimapButton])
        stack.axis = .horizontal
        stack.alignment = .center
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: topAnchor),
            stack.bottomAnchor.constraint(equalTo: bottomAnchor),
            stack.leadingAnchor.constraint(equalTo: leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor),
            heightAnchor.constraint(equalToConstant: Self.height),
            zoomButton.heightAnchor.constraint(equalTo: heightAnchor),
            zoomButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 84),
            minimapButton.heightAnchor.constraint(equalTo: heightAnchor),
            minimapButton.widthAnchor.constraint(equalToConstant: 48),
            divider.widthAnchor.constraint(equalToConstant: 1),
            divider.heightAnchor.constraint(equalToConstant: 24),
        ])
        setZoom(1)
        setMinimapVisible(true)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("ZoomPill is created in code") }

    /// "56%", as `BoardGeometry.zoomLabel` writes it. Written only when the
    /// text changes: a pinch reports every frame.
    func setZoom(_ zoom: CGFloat) {
        let text = BoardGeometry.zoomLabel(zoom)
        guard text != label else { return }
        label = text
        var configuration = zoomButton.configuration
        configuration?.attributedTitle = AttributedString(
            text,
            attributes: AttributeContainer([
                .font: UIFont.monospacedDigitSystemFont(ofSize: 17, weight: .regular)
            ]))
        zoomButton.configuration = configuration
        zoomButton.accessibilityLabel = text
    }

    func setMinimapVisible(_ visible: Bool) {
        isMinimapVisible = visible
        minimapButton.accessibilityValue = visible ? "shown" : "hidden"
        minimapButton.setNeedsUpdateConfiguration()
    }

    /// Fit Content, then the fixed zooms. Cmd-9 and Cmd-0 are the board's
    /// keys for the first and for 100 %.
    private func makeMenu() -> UIMenu {
        let fit = UIAction(
            title: "Fit Content", image: UIImage(systemName: "arrow.up.left.and.arrow.down.right")
        ) { [weak self] _ in self?.onZoom?(.fitContent) }
        let presets = BoardGeometry.zoomPresets.map { zoom in
            UIAction(title: BoardGeometry.zoomLabel(zoom)) { [weak self] _ in
                self?.onZoom?(.percent(Double(zoom * 100)))
            }
        }
        return UIMenu(children: [UIMenu(options: .displayInline, children: [fit])] + presets)
    }
}

/// What the pill area says once a board nears what it can hold: "Board nearly
/// full", or "Board full" with Add Board beside it.
final class BoardLimitBanner: UIView {
    var onAddBoard: (() -> Void)?

    private let label = UILabel()
    let addButton = UIButton(type: .system)
    private(set) var state: BoardLimits.State = .normal

    override init(frame: CGRect) {
        super.init(frame: frame)
        PaperTokens.applyFloatingPillStyle(to: self, cornerRadius: 12)
        isHidden = true

        label.font = .systemFont(ofSize: 15, weight: .medium)
        label.textColor = UIColor(white: 0.92, alpha: 1)
        label.accessibilityIdentifier = "board.limit"

        var add = UIButton.Configuration.plain()
        add.title = "Add Board"
        add.image = UIImage(systemName: "plus.circle")
        add.imagePadding = 6
        add.baseForegroundColor = .white
        addButton.configuration = add
        addButton.addAction(UIAction { [weak self] _ in self?.onAddBoard?() }, for: .touchUpInside)
        addButton.accessibilityLabel = "Add Board"

        let stack = UIStackView(arrangedSubviews: [label, addButton])
        stack.axis = .horizontal
        stack.alignment = .center
        stack.spacing = 4
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: topAnchor),
            stack.bottomAnchor.constraint(equalTo: bottomAnchor),
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -6),
            heightAnchor.constraint(equalToConstant: ZoomPill.height),
        ])
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("BoardLimitBanner is created in code") }

    func show(_ state: BoardLimits.State, announce: Bool = true) {
        self.state = state
        label.text = state.message
        isHidden = state.message == nil
        addButton.isHidden = state != .full
        if announce, let message = state.message {
            UIAccessibility.post(notification: .announcement, argument: message)
        }
    }
}
