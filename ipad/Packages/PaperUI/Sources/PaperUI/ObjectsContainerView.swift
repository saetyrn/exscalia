import PaperCore
import UIKit

@MainActor
protocol ObjectsContainerDelegate: AnyObject {
    /// The objects changed and should be written to `page.json`.
    func objectsContainer(_ container: ObjectsContainerView, didChange objects: [PageObject])
    /// The undo manager every object edit registers on -- the canvas's own, so
    /// Cmd-Z walks ink and objects in one history.
    func objectsContainerUndoManager(_ container: ObjectsContainerView) -> UndoManager?
    /// Editing objects takes the touches away from the canvas while it lasts.
    func objectsContainer(_ container: ObjectsContainerView, didChangeEditing isEditing: Bool)
    func objectsContainer(_ container: ObjectsContainerView, imageFor assetPath: String) -> UIImage?
}

/// The layer of text and image objects, between the paper and the ink.
///
/// It is inert until something in it is selected: ink has to reach the canvas
/// above without asking, so the container hands over touches only once the user
/// has tapped an object, and gives them back the moment the selection is
/// dropped.
final class ObjectsContainerView: UIView {
    weak var delegate: ObjectsContainerDelegate?
    /// How far growth has moved the page's content so far. Set by `PageView`.
    var growthOffset: (() -> CGSize)?

    private(set) var objects: [PageObject] = []
    private var views: [String: ObjectView] = [:]
    private let inspector = ObjectInspectorView()

    var zoom: CGFloat = 1 {
        didSet {
            guard zoom != oldValue else { return }
            for view in views.values { view.zoom = zoom }
            setNeedsLayout()
        }
    }

    private(set) var selectedID: String? {
        didSet {
            guard selectedID != oldValue else { return }
            for (id, view) in views { view.isSelected = id == selectedID }
            updateInspector()
            delegate?.objectsContainer(self, didChangeEditing: selectedID != nil)
        }
    }

    override init(frame: CGRect) {
        super.init(frame: frame)
        isUserInteractionEnabled = false
        inspector.isHidden = true
        inspector.delegate = self
        addSubview(inspector)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("ObjectsContainerView is created in code") }

    // MARK: - Contents

    func setObjects(_ next: [PageObject]) {
        objects = next
        let ids = Set(next.map(\.id))
        for (id, view) in views where !ids.contains(id) {
            view.removeFromSuperview()
            views.removeValue(forKey: id)
        }
        for object in next {
            if let existing = views[object.id] {
                // Only what changed: a text view mid-typing holds words its
                // object does not have yet, and re-applying the same object
                // would put the old text back.
                if existing.object != object { existing.apply(object) }
            } else {
                let view = make(object)
                views[object.id] = view
                insertSubview(view, belowSubview: inspector)
            }
        }
        if let selectedID, !ids.contains(selectedID) { self.selectedID = nil }
        for (id, view) in views { view.isSelected = id == selectedID }
        setNeedsLayout()
        updateInspector()
    }

    private func make(_ object: PageObject) -> ObjectView {
        let view: ObjectView
        switch object.type {
        case .text:
            view = TextObjectView(object: object)
        case .image:
            let image = object.asset.flatMap { delegate?.objectsContainer(self, imageFor: $0) }
            view = ImageObjectView(object: object, image: image)
        case .unknown:
            // A kind a newer build wrote: nothing to draw, but it can still be
            // selected, moved and deleted like any other object.
            view = ObjectView(object: object)
        }
        view.zoom = zoom
        view.delegate = self
        return view
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        for object in objects {
            views[object.id]?.frame = ObjectView.viewFrame(for: object.frame.rect, zoom: zoom)
        }
        layoutInspector()
    }

    /// The id of the object under a point in page-view coordinates, topmost
    /// first, or `nil` for bare paper.
    func objectID(at point: CGPoint) -> String? {
        for object in objects.reversed() {
            let frame = ObjectView.viewFrame(for: object.frame.rect, zoom: zoom)
            if frame.contains(point) { return object.id }
        }
        return nil
    }

    func select(_ id: String?) {
        selectedID = id
        if let id, let view = views[id] { bringSubviewToFront(view) }
        bringSubviewToFront(inspector)
    }

    func deselect() {
        for view in views.values { view.endEditingContent() }
        select(nil)
    }

    /// Adds an object, selects it, and registers the inverse on the canvas's
    /// undo manager so Cmd-Z removes it again.
    func insert(_ object: PageObject) {
        let previous = objects
        var next = objects
        next.append(object)
        apply(next, previous: previous, actionName: "Insert \(object.type == .text ? "Text" : "Image")")
        select(object.id)
        if let text = views[object.id] as? TextObjectView { text.beginEditingContent() }
    }

    func deleteSelected() {
        guard let selectedID else { return }
        let previous = objects
        apply(
            objects.filter { $0.id != selectedID }, previous: previous, actionName: "Delete Object")
        select(nil)
    }

    /// The single path every mutation takes: replace the array, tell the
    /// delegate to persist it, register the inverse.
    ///
    /// Persisting can grow an infinite page and move every object on it, so
    /// both arrays are held with the growth they were made at and moved by
    /// whatever growth came after when the inverse runs.
    private func apply(_ next: [PageObject], previous: [PageObject], actionName: String) {
        let base = growthOffset?() ?? .zero
        setObjects(next)
        delegate?.objectsContainer(self, didChange: next)
        guard let manager = delegate?.objectsContainerUndoManager(self) else { return }
        manager.registerUndo(withTarget: self) { target in
            MainActor.assumeIsolated {
                let now = target.growthOffset?() ?? base
                let delta = CGSize(width: now.width - base.width, height: now.height - base.height)
                target.apply(
                    Self.translated(previous, by: delta),
                    previous: Self.translated(next, by: delta), actionName: actionName)
            }
        }
        manager.setActionName(actionName)
    }

    private static func translated(_ objects: [PageObject], by delta: CGSize) -> [PageObject] {
        guard delta != .zero else { return objects }
        let transform = CGAffineTransform(translationX: delta.width, y: delta.height)
        return objects.map { object in
            var copy = object
            copy.frame = PageObject.Frame(object.frame.rect.applying(transform))
            return copy
        }
    }

    private func replace(_ object: PageObject, previous: [PageObject], actionName: String) {
        var next = objects
        guard let index = next.firstIndex(where: { $0.id == object.id }) else { return }
        next[index] = object
        apply(next, previous: previous, actionName: actionName)
    }

    // MARK: - Inspector

    private func updateInspector() {
        guard let selectedID, let object = objects.first(where: { $0.id == selectedID }) else {
            inspector.isHidden = true
            return
        }
        inspector.isHidden = false
        inspector.configure(for: object, isCropping: (views[selectedID] as? ImageObjectView)?.isCropping ?? false)
        bringSubviewToFront(inspector)
        layoutInspector()
    }

    private func layoutInspector() {
        guard !inspector.isHidden, let selectedID,
            let object = objects.first(where: { $0.id == selectedID })
        else { return }
        let frame = ObjectView.viewFrame(for: object.frame.rect, zoom: zoom)
        let size = inspector.systemLayoutSizeFitting(UIView.layoutFittingCompressedSize)
        inspector.bounds.size = size
        inspector.center = CGPoint(
            x: min(max(frame.midX, size.width / 2 + 8), max(bounds.width - size.width / 2 - 8, size.width / 2 + 8)),
            y: max(size.height / 2 + 8, frame.minY - 12 - size.height / 2))
    }
}

// MARK: - Object callbacks

extension ObjectsContainerView: ObjectViewDelegate {
    func objectViewDidSelect(_ view: ObjectView) {
        select(view.object.id)
    }

    func objectView(_ view: ObjectView, didCommit object: PageObject, actionName: String) {
        var previous = objects
        if let index = previous.firstIndex(where: { $0.id == object.id }),
            previous[index] == object
        {
            return
        }
        previous = objects
        replace(object, previous: previous, actionName: actionName)
    }

    func objectViewDidChangeCropMode(_ view: ObjectView) {
        updateInspector()
    }

    func objectViewNeedsLayout(_ view: ObjectView) {
        layoutInspector()
    }
}

extension ObjectsContainerView: ObjectInspectorDelegate {
    func objectInspector(_ inspector: ObjectInspectorView, nudgeBy offset: CGSize) {
        guard let selectedID, var object = objects.first(where: { $0.id == selectedID }) else {
            return
        }
        object.frame.x += Double(offset.width)
        object.frame.y += Double(offset.height)
        replace(object, previous: objects, actionName: "Move Object")
        setNeedsLayout()
    }

    func objectInspector(_ inspector: ObjectInspectorView, scaleBy factor: CGFloat) {
        guard let selectedID, var object = objects.first(where: { $0.id == selectedID }) else {
            return
        }
        object.frame.width = max(32, object.frame.width * Double(factor))
        object.frame.height = max(24, object.frame.height * Double(factor))
        replace(object, previous: objects, actionName: "Resize Object")
        setNeedsLayout()
    }

    func objectInspectorDidTapEdit(_ inspector: ObjectInspectorView) {
        guard let selectedID, let text = views[selectedID] as? TextObjectView else { return }
        text.beginEditingContent()
    }

    func objectInspectorDidTapCrop(_ inspector: ObjectInspectorView) {
        guard let selectedID, let image = views[selectedID] as? ImageObjectView else { return }
        image.isCropping.toggle()
        updateInspector()
    }

    func objectInspectorDidTapDelete(_ inspector: ObjectInspectorView) {
        deleteSelected()
    }
}

// MARK: - Inspector view

@MainActor
protocol ObjectInspectorDelegate: AnyObject {
    func objectInspector(_ inspector: ObjectInspectorView, nudgeBy offset: CGSize)
    func objectInspector(_ inspector: ObjectInspectorView, scaleBy factor: CGFloat)
    func objectInspectorDidTapEdit(_ inspector: ObjectInspectorView)
    func objectInspectorDidTapCrop(_ inspector: ObjectInspectorView)
    func objectInspectorDidTapDelete(_ inspector: ObjectInspectorView)
}

/// The contextual capsule for a selected object, built to the same shape as the
/// lasso's `SelectionInspector`: one blurred capsule of monochrome glyphs that
/// appears with the selection and leaves with it.
final class ObjectInspectorView: UIView {
    weak var delegate: ObjectInspectorDelegate?

    private let stack = UIStackView()
    private let editButton: UIButton
    private let cropButton: UIButton

    init() {
        editButton = Self.button(symbol: "textformat", label: "Edit text")
        cropButton = Self.button(symbol: "crop", label: "Crop")
        super.init(frame: .zero)

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
        layer.cornerRadius = 22
        layer.cornerCurve = .continuous
        layer.borderWidth = 1
        layer.borderColor = UIColor.separator.cgColor
        clipsToBounds = true

        stack.axis = .horizontal
        stack.alignment = .center
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: topAnchor, constant: 2),
            stack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -2),
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 4),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -4),
        ])

        let move = Self.button(
            symbol: "arrow.up.and.down.and.arrow.left.and.right", label: "Move")
        move.accessibilityHint = "Drag the object, or use this menu to nudge it"
        move.showsMenuAsPrimaryAction = true
        move.menu = UIMenu(children: [
            nudge("Move up", "arrow.up", CGSize(width: 0, height: -16)),
            nudge("Move down", "arrow.down", CGSize(width: 0, height: 16)),
            nudge("Move left", "arrow.left", CGSize(width: -16, height: 0)),
            nudge("Move right", "arrow.right", CGSize(width: 16, height: 0)),
        ])
        stack.addArrangedSubview(move)

        let resize = Self.button(symbol: "arrow.up.left.and.arrow.down.right", label: "Resize")
        resize.accessibilityHint = "Drag a handle, or use this menu to resize"
        resize.showsMenuAsPrimaryAction = true
        resize.menu = UIMenu(children: [
            UIAction(title: "Larger", image: UIImage(systemName: "plus.magnifyingglass")) {
                [weak self] _ in
                guard let self else { return }
                self.delegate?.objectInspector(self, scaleBy: 1.25)
            },
            UIAction(title: "Smaller", image: UIImage(systemName: "minus.magnifyingglass")) {
                [weak self] _ in
                guard let self else { return }
                self.delegate?.objectInspector(self, scaleBy: 0.8)
            },
        ])
        stack.addArrangedSubview(resize)

        editButton.accessibilityHint = "Puts the cursor in this text"
        editButton.addAction(
            UIAction { [weak self] _ in
                guard let self else { return }
                self.delegate?.objectInspectorDidTapEdit(self)
            }, for: .touchUpInside)
        stack.addArrangedSubview(editButton)

        cropButton.accessibilityHint = "Drag the handles to change what the image shows"
        cropButton.addAction(
            UIAction { [weak self] _ in
                guard let self else { return }
                self.delegate?.objectInspectorDidTapCrop(self)
            }, for: .touchUpInside)
        stack.addArrangedSubview(cropButton)

        let delete = Self.button(symbol: "trash", label: "Delete")
        delete.accessibilityHint = "Removes this object from the page"
        delete.addAction(
            UIAction { [weak self] _ in
                guard let self else { return }
                self.delegate?.objectInspectorDidTapDelete(self)
            }, for: .touchUpInside)
        stack.addArrangedSubview(delete)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("ObjectInspectorView is created in code") }

    func configure(for object: PageObject, isCropping: Bool) {
        editButton.isHidden = object.type != .text
        cropButton.isHidden = object.type != .image
        cropButton.tintColor = isCropping ? UIColor(PaperTokens.accent) : .label
        cropButton.accessibilityValue = isCropping ? "cropping" : nil
    }

    private func nudge(_ title: String, _ symbol: String, _ offset: CGSize) -> UIAction {
        UIAction(title: title, image: UIImage(systemName: symbol)) { [weak self] _ in
            guard let self else { return }
            self.delegate?.objectInspector(self, nudgeBy: offset)
        }
    }

    private static func button(symbol: String, label: String) -> UIButton {
        let button = UIButton(type: .system)
        button.setImage(
            UIImage(
                systemName: symbol,
                withConfiguration: UIImage.SymbolConfiguration(pointSize: 16, weight: .regular)),
            for: .normal)
        button.tintColor = .label
        button.translatesAutoresizingMaskIntoConstraints = false
        button.accessibilityLabel = label
        NSLayoutConstraint.activate([
            button.widthAnchor.constraint(equalToConstant: PaperTokens.minimumTarget),
            button.heightAnchor.constraint(equalToConstant: PaperTokens.minimumTarget),
        ])
        return button
    }
}
