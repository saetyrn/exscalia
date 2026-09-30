import PaperCore
import UIKit

@MainActor
protocol ToolCapsuleDelegate: AnyObject {
    func capsule(_ capsule: ToolCapsuleView, didSelect tool: PaperTool)
    /// Tapping the already-active tool opens its presets.
    func capsule(_ capsule: ToolCapsuleView, showPresetsFor tool: PaperTool, from source: UIView)
    func capsuleDidTapUndo(_ capsule: ToolCapsuleView)
    func capsuleDidTapRedo(_ capsule: ToolCapsuleView)
    func capsuleMenu(_ capsule: ToolCapsuleView) -> UIMenu
}

/// The constitutional toolbar: `Pen | Highlighter | Eraser | Lasso | Undo |
/// Redo | +`, in that order, always.
///
/// It floats over the page and can be dragged to whichever edge suits the hand
/// holding the iPad, but its contents never reorder and undo/redo are never
/// hidden -- the research is blunt that a toolbar which moves things around is
/// a toolbar people stop trusting.
final class ToolCapsuleView: UIView {
    weak var delegate: ToolCapsuleDelegate?

    /// Collapses to `Undo | Redo | +` when PencilKit's own picker is driving
    /// the tools.
    var isCollapsed = false {
        didSet {
            guard isCollapsed != oldValue else { return }
            for tool in PaperTool.allCases { buttons[tool]?.isHidden = isCollapsed }
            underline.isHidden = isCollapsed
            layoutUnderline()
        }
    }

    private let state: ToolState
    private let stack = UIStackView()
    private var buttons: [PaperTool: UIButton] = [:]
    private let undoButton = UIButton(type: .system)
    private let redoButton = UIButton(type: .system)
    private let plusButton = UIButton(type: .system)
    private let underline = UIView()

    /// 0...1 along the current edge, mirrored into `ToolState` when dragging
    /// ends so the placement survives a relaunch.
    private(set) var edge: ToolState.CapsuleEdge
    private(set) var offset: CGFloat

    private var isVertical: Bool { edge != .bottom }

    init(state: ToolState) {
        self.state = state
        self.edge = state.capsuleEdge
        self.offset = state.capsuleOffset
        super.init(frame: .zero)
        build()
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("ToolCapsuleView is created in code") }

    // MARK: - Building

    private func build() {
        let background = UIVisualEffectView(effect: UIBlurEffect(style: .systemThickMaterial))
        background.translatesAutoresizingMaskIntoConstraints = false
        background.isUserInteractionEnabled = false
        addSubview(background)
        NSLayoutConstraint.activate([
            background.topAnchor.constraint(equalTo: topAnchor),
            background.bottomAnchor.constraint(equalTo: bottomAnchor),
            background.leadingAnchor.constraint(equalTo: leadingAnchor),
            background.trailingAnchor.constraint(equalTo: trailingAnchor),
        ])
        layer.cornerRadius = 26
        layer.cornerCurve = .continuous
        layer.borderWidth = 1
        layer.borderColor = UIColor.separator.cgColor
        clipsToBounds = true

        stack.axis = isVertical ? .vertical : .horizontal
        stack.alignment = .center
        stack.spacing = 2
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: topAnchor, constant: 6),
            stack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -6),
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 6),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -6),
        ])

        for tool in PaperTool.allCases {
            let button = makeButton(
                symbol: tool.symbolName, label: tool.label,
                hint: "Selects the \(tool.label.lowercased()). Tap again for its settings.")
            button.addAction(
                UIAction { [weak self] _ in self?.toolTapped(tool, button) }, for: .touchUpInside)
            buttons[tool] = button
            stack.addArrangedSubview(button)
        }

        let separator = UIView()
        separator.backgroundColor = .separator
        separator.isUserInteractionEnabled = false
        separator.translatesAutoresizingMaskIntoConstraints = false
        stack.addArrangedSubview(separator)

        configure(
            undoButton, symbol: "arrow.uturn.backward", label: "Undo",
            hint: "Undoes the last change on this page")
        undoButton.addAction(
            UIAction { [weak self] _ in
                guard let self else { return }
                self.delegate?.capsuleDidTapUndo(self)
            }, for: .touchUpInside)
        stack.addArrangedSubview(undoButton)

        configure(
            redoButton, symbol: "arrow.uturn.forward", label: "Redo",
            hint: "Redoes the change that was undone")
        redoButton.addAction(
            UIAction { [weak self] _ in
                guard let self else { return }
                self.delegate?.capsuleDidTapRedo(self)
            }, for: .touchUpInside)
        stack.addArrangedSubview(redoButton)

        configure(plusButton, symbol: "plus", label: "Insert", hint: "Adds content and page actions")
        plusButton.showsMenuAsPrimaryAction = true
        plusButton.menu = UIMenu(children: [UIDeferredMenuElement.uncached { [weak self] completion in
            guard let self, let menu = self.delegate?.capsuleMenu(self) else {
                completion([])
                return
            }
            completion(menu.children)
        }])
        stack.addArrangedSubview(plusButton)

        NSLayoutConstraint.activate([
            separator.widthAnchor.constraint(
                equalToConstant: isVertical ? PaperTokens.minimumTarget - 16 : 1),
            separator.heightAnchor.constraint(
                equalToConstant: isVertical ? 1 : PaperTokens.minimumTarget - 16),
        ])

        underline.backgroundColor = UIColor(PaperTokens.accent)
        underline.layer.cornerRadius = 1
        underline.isUserInteractionEnabled = false
        addSubview(underline)

        let drag = UIPanGestureRecognizer(target: self, action: #selector(handleDrag))
        addGestureRecognizer(drag)

        accessibilityContainerType = .semanticGroup
        isAccessibilityElement = false
        refresh()
    }

    private func makeButton(symbol: String, label: String, hint: String) -> UIButton {
        let button = UIButton(type: .system)
        configure(button, symbol: symbol, label: label, hint: hint)
        return button
    }

    private func configure(_ button: UIButton, symbol: String, label: String, hint: String) {
        button.setImage(
            UIImage(
                systemName: symbol,
                withConfiguration: UIImage.SymbolConfiguration(pointSize: 18, weight: .regular)),
            for: .normal)
        button.tintColor = UIColor.label
        button.translatesAutoresizingMaskIntoConstraints = false
        button.accessibilityLabel = label
        button.accessibilityHint = hint
        NSLayoutConstraint.activate([
            button.widthAnchor.constraint(equalToConstant: PaperTokens.minimumTarget),
            button.heightAnchor.constraint(equalToConstant: PaperTokens.minimumTarget),
        ])
    }

    // MARK: - State

    /// Repaints selection, and enables/disables undo and redo.
    func refresh(canUndo: Bool = false, canRedo: Bool = false) {
        for tool in PaperTool.allCases {
            guard let button = buttons[tool] else { continue }
            let isActive = tool == state.tool && !isCollapsed
            button.setImage(
                UIImage(
                    systemName: isActive ? tool.filledSymbolName : tool.symbolName,
                    withConfiguration: UIImage.SymbolConfiguration(pointSize: 18, weight: .regular)),
                for: .normal)
            button.tintColor = isActive ? UIColor(PaperTokens.accent) : UIColor.label
            button.accessibilityValue = isActive ? "selected" : nil
            button.accessibilityTraits = isActive ? [.button, .selected] : [.button]
        }
        // Never hidden, only dimmed: the research is explicit that undo must
        // always be where the user last saw it.
        undoButton.isEnabled = canUndo
        undoButton.alpha = canUndo ? 1 : 0.4
        undoButton.accessibilityTraits = canUndo ? [.button] : [.button, .notEnabled]
        redoButton.isEnabled = canRedo
        redoButton.alpha = canRedo ? 1 : 0.4
        redoButton.accessibilityTraits = canRedo ? [.button] : [.button, .notEnabled]
        layoutUnderline()
    }

    private func toolTapped(_ tool: PaperTool, _ button: UIButton) {
        if state.tool == tool {
            delegate?.capsule(self, showPresetsFor: tool, from: button)
        } else {
            delegate?.capsule(self, didSelect: tool)
        }
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        layoutUnderline()
    }

    /// A 2pt accent bar under the active glyph: state carried by a shape, not
    /// only by a colour.
    private func layoutUnderline() {
        guard !isCollapsed, let button = buttons[state.tool], button.superview != nil else {
            underline.isHidden = true
            return
        }
        underline.isHidden = false
        let frame = button.convert(button.bounds, to: self)
        underline.frame = CGRect(
            x: frame.midX - 11, y: frame.maxY - 7, width: 22, height: 2)
    }

    // MARK: - Dragging

    private var dragStart: CGPoint = .zero

    @objc private func handleDrag(_ gesture: UIPanGestureRecognizer) {
        guard let container = superview else { return }
        switch gesture.state {
        case .began:
            dragStart = center
        case .changed:
            let translation = gesture.translation(in: container)
            center = CGPoint(x: dragStart.x + translation.x, y: dragStart.y + translation.y)
        case .ended, .cancelled, .failed:
            snapToNearestEdge(in: container)
        default:
            break
        }
    }

    private func snapToNearestEdge(in container: UIView) {
        let bounds = container.bounds
        let distances: [(ToolState.CapsuleEdge, CGFloat)] = [
            (.leading, center.x),
            (.trailing, bounds.width - center.x),
            (.bottom, bounds.height - center.y),
        ]
        let nearest = distances.min(by: { $0.1 < $1.1 })?.0 ?? .trailing
        let along: CGFloat =
            nearest == .bottom
            ? (bounds.width > 0 ? center.x / bounds.width : 0.5)
            : (bounds.height > 0 ? center.y / bounds.height : 0.5)

        edge = nearest
        offset = min(max(along, 0.08), 0.92)
        state.capsuleEdge = nearest
        state.capsuleOffset = offset

        let apply = { [weak self] in self?.reposition(in: container) }
        if UIAccessibility.isReduceMotionEnabled {
            apply()
        } else {
            UIView.animate(withDuration: 0.22, delay: 0, options: [.curveEaseOut]) { apply() }
        }
    }

    /// Places the capsule on its remembered edge. Called on layout and after a
    /// drag.
    func reposition(in container: UIView) {
        let wantsVertical = edge != .bottom
        if (stack.axis == .vertical) != wantsVertical {
            stack.axis = wantsVertical ? .vertical : .horizontal
        }
        let insets = container.safeAreaInsets
        let size = systemLayoutSizeFitting(UIView.layoutFittingCompressedSize)
        bounds.size = size
        let margin: CGFloat = 12
        switch edge {
        case .leading:
            center = CGPoint(
                x: insets.left + margin + size.width / 2,
                y: min(
                    max(container.bounds.height * offset, insets.top + size.height / 2 + margin),
                    container.bounds.height - insets.bottom - size.height / 2 - margin))
        case .trailing:
            center = CGPoint(
                x: container.bounds.width - insets.right - margin - size.width / 2,
                y: min(
                    max(container.bounds.height * offset, insets.top + size.height / 2 + margin),
                    container.bounds.height - insets.bottom - size.height / 2 - margin))
        case .bottom:
            center = CGPoint(
                x: min(
                    max(container.bounds.width * offset, insets.left + size.width / 2 + margin),
                    container.bounds.width - insets.right - size.width / 2 - margin),
                y: container.bounds.height - insets.bottom - margin - size.height / 2)
        }
        layoutUnderline()
    }
}
