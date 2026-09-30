import PaperCore
import UIKit

/// The presets popover: three widths and, for the drawing tools, the named
/// colour chips. Reached by tapping the already-active tool, by a Pencil
/// squeeze, or by the system's "show ink attributes" double-tap action.
final class ToolPresetsViewController: UIViewController, UIPopoverPresentationControllerDelegate {
    private let tools: ToolState
    private let tool: PaperTool
    private let onChange: () -> Void
    private let stack = UIStackView()
    private let preview = WidthPreviewView()
    private let readout = UILabel()
    private weak var slider: UISlider?

    init(tools: ToolState, tool: PaperTool, onChange: @escaping () -> Void) {
        self.tools = tools
        self.tool = tool
        self.onChange = onChange
        super.init(nibName: nil, bundle: nil)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("ToolPresetsViewController is created in code") }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        stack.axis = .vertical
        stack.spacing = 12
        stack.alignment = .leading
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: view.topAnchor, constant: 16),
            stack.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -16),
            stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 16),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -16),
        ])

        switch tool {
        case .pen:
            addWidthSlider()
            addColors(
                ToolState.penColors, currentIndex: tools.penColorIndex
            ) { [weak self] index in
                self?.tools.penColorIndex = index
                self?.refreshPreview()
            }
        case .highlighter:
            addWidthSlider()
            addColors(
                ToolState.highlighterColors, currentIndex: tools.highlighterColorIndex
            ) { [weak self] index in
                self?.tools.highlighterColorIndex = index
                self?.refreshPreview()
            }
        case .eraser:
            addEraserMode()
            addWidthSlider()
        case .lasso:
            let label = UILabel()
            label.text = "Draw a loop around what you want to select."
            label.numberOfLines = 0
            label.font = .preferredFont(forTextStyle: .footnote)
            label.adjustsFontForContentSizeCategory = true
            label.textColor = .secondaryLabel
            stack.addArrangedSubview(label)
        }

        preferredContentSize = CGSize(
            width: 300, height: stack.systemLayoutSizeFitting(UIView.layoutFittingCompressedSize).height + 32)
    }

    /// One continuous slider, a stroke drawn at the width and colour it will
    /// actually be, and the number itself. A pen has a size, not three sizes.
    private func addWidthSlider() {
        let range = ToolState.widthRange(for: tool)
        let row = UIStackView()
        row.axis = .horizontal
        row.alignment = .center
        row.spacing = 12

        let slider = UISlider()
        slider.minimumValue = Float(range.lowerBound)
        slider.maximumValue = Float(range.upperBound)
        slider.value = Float(tools.width(for: tool))
        slider.isContinuous = true
        slider.accessibilityLabel = "Width"
        slider.accessibilityValue = Self.accessibleWidth(tools.width(for: tool))
        slider.addAction(
            UIAction { [weak self, weak slider] _ in
                guard let self, let slider else { return }
                let width = CGFloat(slider.value)
                self.tools.setWidth(width, for: self.tool)
                slider.accessibilityValue = Self.accessibleWidth(self.tools.width(for: self.tool))
                self.refreshPreview()
                self.onChange()
            }, for: .valueChanged)
        self.slider = slider

        readout.font = .monospacedDigitSystemFont(ofSize: 13, weight: .regular)
        readout.textColor = .secondaryLabel
        readout.textAlignment = .right
        readout.isAccessibilityElement = false
        readout.setContentHuggingPriority(.required, for: .horizontal)
        readout.setContentCompressionResistancePriority(.required, for: .horizontal)
        readout.widthAnchor.constraint(equalToConstant: 52).isActive = true

        row.addArrangedSubview(slider)
        row.addArrangedSubview(readout)
        row.translatesAutoresizingMaskIntoConstraints = false

        preview.translatesAutoresizingMaskIntoConstraints = false
        preview.isAccessibilityElement = false
        preview.heightAnchor.constraint(equalToConstant: 44).isActive = true

        stack.addArrangedSubview(preview)
        stack.addArrangedSubview(row)
        preview.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        row.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        refreshPreview()
    }

    private func refreshPreview() {
        let width = tools.width(for: tool)
        readout.text = Self.formatted(width) + " pt"
        preview.configure(tool: tool, width: width, color: previewColor)
    }

    private var previewColor: UIColor {
        switch tool {
        case .pen, .lasso: return tools.penColor.uiColor
        case .highlighter: return tools.highlighterColor.uiColor.withAlphaComponent(0.45)
        case .eraser: return UIColor.label.withAlphaComponent(0.25)
        }
    }

    private static func formatted(_ width: CGFloat) -> String {
        width == width.rounded() ? "\(Int(width))" : String(format: "%.1f", Double(width))
    }

    /// VoiceOver reads the width in points, which is the unit the number on
    /// screen is in.
    private static func accessibleWidth(_ width: CGFloat) -> String {
        "\(formatted(width)) points"
    }

    private func addColors(
        _ colors: [NamedInkColor], currentIndex: Int, apply: @escaping (Int) -> Void
    ) {
        let row = UIStackView()
        row.axis = .horizontal
        row.spacing = 6
        row.distribution = .fillEqually
        for (index, color) in colors.enumerated() {
            let button = UIButton(type: .system)
            button.backgroundColor = color.uiColor
            button.layer.cornerRadius = 16
            button.layer.borderWidth = index == currentIndex ? 3 : 1
            button.layer.borderColor =
                (index == currentIndex ? UIColor(PaperTokens.accent) : UIColor.separator).cgColor
            button.accessibilityLabel = color.name
            button.accessibilityValue = index == currentIndex ? "selected" : nil
            button.translatesAutoresizingMaskIntoConstraints = false
            button.heightAnchor.constraint(equalToConstant: 32).isActive = true
            button.addAction(
                UIAction { [weak self] _ in
                    apply(index)
                    self?.onChange()
                    self?.dismiss(animated: !UIAccessibility.isReduceMotionEnabled)
                }, for: .touchUpInside)
            row.addArrangedSubview(button)
        }
        row.translatesAutoresizingMaskIntoConstraints = false
        stack.addArrangedSubview(row)
        row.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
    }

    private func addEraserMode() {
        let control = UISegmentedControl(items: ["Pixel", "Whole stroke"])
        control.selectedSegmentIndex = tools.eraserIsBitmap ? 0 : 1
        control.accessibilityLabel = "Eraser mode"
        control.addAction(
            UIAction { [weak self] _ in
                guard let self else { return }
                self.tools.eraserIsBitmap = control.selectedSegmentIndex == 0
                self.onChange()
            }, for: .valueChanged)
        control.translatesAutoresizingMaskIntoConstraints = false
        stack.addArrangedSubview(control)
        control.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
    }

    /// A popover on iPad stays a popover; the presets are anchored to the tool
    /// they belong to.
    func adaptivePresentationStyle(for controller: UIPresentationController)
        -> UIModalPresentationStyle
    {
        .none
    }
}


/// A stroke drawn at the width and colour that are about to be used, so the
/// number on the slider has something to mean.
final class WidthPreviewView: UIView {
    private var tool: PaperTool = .pen
    private var width: CGFloat = 3
    private var color: UIColor = .label

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = UIColor(PaperTokens.paper)
        layer.cornerRadius = 6
        layer.cornerCurve = .continuous
        layer.borderWidth = 1
        layer.borderColor = UIColor.separator.cgColor
        isOpaque = false
        contentMode = .redraw
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("WidthPreviewView is created in code") }

    func configure(tool: PaperTool, width: CGFloat, color: UIColor) {
        self.tool = tool
        self.width = width
        self.color = color
        setNeedsDisplay()
    }

    override func draw(_ rect: CGRect) {
        guard let context = UIGraphicsGetCurrentContext(), bounds.width > 24 else { return }
        context.setLineCap(.round)
        context.setLineJoin(.round)
        let inset = max(width / 2 + 4, 8)
        let midY = bounds.midY
        // The eraser has no stroke of its own, so it shows the size of the
        // hole it makes instead.
        if tool == .eraser {
            let radius = min(width / 2, (bounds.height - 8) / 2)
            color.setFill()
            context.fillEllipse(
                in: CGRect(
                    x: bounds.midX - radius, y: midY - radius, width: radius * 2,
                    height: radius * 2))
            return
        }
        let path = UIBezierPath()
        path.move(to: CGPoint(x: inset, y: midY))
        path.addCurve(
            to: CGPoint(x: bounds.maxX - inset, y: midY),
            controlPoint1: CGPoint(x: bounds.width * 0.35, y: midY - bounds.height * 0.3),
            controlPoint2: CGPoint(x: bounds.width * 0.65, y: midY + bounds.height * 0.3))
        path.lineWidth = min(width, bounds.height - 6)
        path.lineCapStyle = .round
        color.setStroke()
        path.stroke()
    }
}
