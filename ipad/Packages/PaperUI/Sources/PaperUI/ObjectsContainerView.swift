import PaperCore
import UIKit

@MainActor
protocol ObjectsContainerDelegate: AnyObject {
    /// The objects changed and should be written to `page.json`.
    func objectsContainer(_ container: ObjectsContainerView, didChange objects: [PageObject])
    /// The undo manager every object edit registers on -- the canvas's own, so
    /// Cmd-Z walks ink and objects in one history.
    func objectsContainerUndoManager(_ container: ObjectsContainerView) -> UndoManager?
    /// An object was selected or put down. While one is selected the objects
    /// layer has the touches.
    func objectsContainer(_ container: ObjectsContainerView, didChangeEditing isEditing: Bool)
    func objectsContainer(_ container: ObjectsContainerView, imageFor assetPath: String) -> UIImage?
    /// Cut or Copy: the objects, with a picture or a string for other apps.
    func objectsContainer(
        _ container: ObjectsContainerView, copy objects: [PageObject], image: UIImage?,
        string: String?)
    /// Share an image object's picture, from `rect` in the container.
    func objectsContainer(_ container: ObjectsContainerView, share image: UIImage, from rect: CGRect)
    /// The sizes, widths and colours the menu offers for an object.
    func objectsContainer(_ container: ObjectsContainerView, menuContextFor object: PageObject)
        -> ObjectMenuContext
}

/// The layer of text, image and shape objects, between the paper and the ink.
///
/// It is inert until something in it is selected: ink has to reach the canvas
/// above without asking, so the container hands over touches only once an
/// object is selected, and gives them back the moment the selection is
/// dropped. A selected object shows its handles and the object menu; a drag on
/// it or on a handle comes in through `beginInteraction`, from the lasso's
/// recogniser, which the scroll view waits for.
final class ObjectsContainerView: UIView {
    weak var delegate: ObjectsContainerDelegate?
    /// How far growth has moved the page's content so far. Set by `PageView`.
    var growthOffset: (() -> CGSize)?

    private(set) var objects: [PageObject] = []
    private(set) var views: [String: ObjectView] = [:]
    let menu = ObjectMenu()
    /// Where the touch carrying the selected object began, in container points.
    private var interactionOrigin: CGPoint?

    var zoom: CGFloat = 1 {
        didSet {
            guard zoom != oldValue else { return }
            for view in views.values { view.zoom = zoom }
            setNeedsLayout()
        }
    }

    /// The page is dark paper: text and shapes show as ink does there.
    var isDarkPaper = false {
        didSet {
            guard isDarkPaper != oldValue else { return }
            for view in views.values { view.isDarkPaper = isDarkPaper }
        }
    }

    private(set) var selectedID: String? {
        didSet {
            guard selectedID != oldValue else { return }
            for (id, view) in views { view.isSelected = id == selectedID }
            updateMenu()
            delegate?.objectsContainer(self, didChangeEditing: selectedID != nil)
        }
    }

    override init(frame: CGRect) {
        super.init(frame: frame)
        isUserInteractionEnabled = false
        menu.isHidden = true
        menu.onAction = { [weak self] action in self?.perform(action) }
        addSubview(menu)
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
                loadImageIfMissing(for: object, into: existing)
            } else {
                views[object.id] = make(object)
            }
        }
        // Stacked in the array's order, which is the render's: Bring to Front
        // means last. Only when it differs: moving a text view in the
        // hierarchy while it is being typed in would put the keyboard away.
        let stacked = subviews.compactMap { ($0 as? ObjectView)?.object.id }
        if stacked != next.map(\.id) {
            for object in next {
                if let view = views[object.id] { insertSubview(view, belowSubview: menu) }
            }
        }
        if let selectedID, !ids.contains(selectedID) { self.selectedID = nil }
        for (id, view) in views { view.isSelected = id == selectedID }
        setNeedsLayout()
        updateMenu()
    }

    /// Gives a picture to each image object that shows the placeholder: one
    /// made before the page had a delegate to ask, or whose file had not
    /// arrived on this device yet.
    func loadMissingImages() {
        for object in objects {
            if let view = views[object.id] { loadImageIfMissing(for: object, into: view) }
        }
    }

    private func loadImageIfMissing(for object: PageObject, into view: ObjectView) {
        guard let view = view as? ImageObjectView, !view.hasImage, let asset = object.asset,
            let image = delegate?.objectsContainer(self, imageFor: asset)
        else { return }
        view.setImage(image)
    }

    private func make(_ object: PageObject) -> ObjectView {
        let view: ObjectView
        switch object.type {
        case .text:
            view = TextObjectView(object: object)
        case .image:
            let image = object.asset.flatMap { delegate?.objectsContainer(self, imageFor: $0) }
            view = ImageObjectView(object: object, image: image)
        case .shape:
            view = ShapeObjectView(object: object)
        case .unknown:
            // A kind a newer build wrote: nothing to draw, and nothing a tap
            // can select; it rides along in `page.json` untouched.
            view = ObjectView(object: object)
        }
        view.zoom = zoom
        view.isDarkPaper = isDarkPaper
        view.delegate = self
        return view
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        for object in objects {
            guard let view = views[object.id] else { continue }
            if view.transform != .identity { view.transform = .identity }
            view.frame = ObjectView.viewFrame(for: view.object.frame.rect, zoom: zoom)
        }
        layoutMenu()
    }

    /// The id of the object under a point in page-view coordinates, topmost
    /// first, or `nil` for bare paper.
    func objectID(at point: CGPoint) -> String? {
        for object in objects.reversed() {
            let frame = ObjectView.viewFrame(for: object.frame.rect, zoom: zoom)
                .insetBy(dx: ObjectView.handleInset, dy: ObjectView.handleInset)
            if frame.contains(point) { return object.id }
        }
        return nil
    }

    var selectedObject: PageObject? {
        guard let selectedID else { return nil }
        return objects.first { $0.id == selectedID }
    }

    var selectedView: ObjectView? { selectedID.flatMap { views[$0] } }

    func select(_ id: String?) {
        if let id, !objects.contains(where: { $0.id == id }) { return }
        selectedID = id
        bringSubviewToFront(menu)
        updateMenu()
    }

    func deselect() {
        for view in views.values { view.endEditingContent() }
        cancelInteraction()
        select(nil)
    }

    /// Puts the cursor in the selected text object.
    func beginEditingSelected() {
        guard let view = selectedView as? TextObjectView else { return }
        view.beginEditingContent()
    }

    /// Adds an object and selects it, and registers the inverse on the
    /// canvas's undo manager so Cmd-Z removes it again. A text object opens
    /// for typing unless `edit` is false.
    func insert(_ object: PageObject, edit: Bool = true) {
        let previous = objects
        var next = objects
        next.append(object)
        apply(next, previous: previous, actionName: Self.insertName(object.type))
        select(object.id)
        if edit, let text = views[object.id] as? TextObjectView { text.beginEditingContent() }
    }

    static func insertName(_ type: PageObject.Kind) -> String {
        switch type {
        case .text: return "Insert Text"
        case .image: return "Insert Image"
        case .shape: return "Insert Shape"
        case .unknown: return "Insert Object"
        }
    }

    func deleteSelected() {
        guard let selectedID else { return }
        commit(objects.filter { $0.id != selectedID }, actionName: "Delete Object")
        select(nil)
    }

    /// Replaces the objects as one undo step: what a mixed selection's move,
    /// delete or duplicate does to its share of the page.
    func commit(_ next: [PageObject], actionName: String) {
        guard next != objects else { return }
        apply(next, previous: objects, actionName: actionName)
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
            copy.frame.x += Double(transform.tx)
            copy.frame.y += Double(transform.ty)
            return copy
        }
    }

    private func replace(_ object: PageObject, actionName: String) {
        var next = objects
        guard let index = next.firstIndex(where: { $0.id == object.id }) else { return }
        guard next[index] != object else { return }
        next[index] = object
        apply(next, previous: objects, actionName: actionName)
    }

    // MARK: - Mixed selections

    /// Shows some objects moved by `transform` (page points) while a lasso
    /// drag carries them, without changing them; nil puts them back.
    func preview(_ ids: [String], transform: CGAffineTransform?) {
        let toView = CGAffineTransform(scaleX: zoom, y: zoom)
        for id in ids {
            guard let view = views[id] else { continue }
            view.transform = .identity
            view.frame = ObjectView.viewFrame(for: view.object.frame.rect, zoom: zoom)
            guard let transform else { continue }
            let viewTransform = toView.inverted().concatenating(transform).concatenating(toView)
            let scale = sqrt(abs(transform.a * transform.d - transform.b * transform.c))
            let centre = view.center.applying(viewTransform)
            view.transform = CGAffineTransform(scaleX: scale, y: scale)
            view.center = centre
        }
    }

    // MARK: - Touches on the selected object

    /// What a touch at `point` (container coordinates) on the selected object
    /// would do: a handle resizes it, its body moves it. Text being typed
    /// keeps its touches for the caret.
    func interaction(at point: CGPoint) -> ObjectView.Interaction? {
        guard let view = selectedView, !menuContains(point) else { return nil }
        // The handles are where the last layout put them: make sure there
        // was one since the object was selected or changed.
        layoutIfNeeded()
        view.layoutIfNeeded()
        let local = view.convert(point, from: self)
        // Well inside the object is its body, however near a handle: on a
        // short text box every point is within a finger of an edge.
        if !view.isEditingContent, view.content.frame.insetBy(dx: 10, dy: 10).contains(local) {
            return .move
        }
        if let handle = view.handle(at: local) { return .resize(handle) }
        guard !view.isEditingContent, view.bodyContains(local) else { return nil }
        return .move
    }

    /// Whether `point` (container coordinates) is on the menu's buttons.
    func menuContains(_ point: CGPoint) -> Bool {
        !menu.isHidden && menu.frame.insetBy(dx: -4, dy: -4).contains(point)
    }

    var isInteracting: Bool { interactionOrigin != nil }

    func beginInteraction(_ interaction: ObjectView.Interaction, at point: CGPoint) {
        guard let view = selectedView else { return }
        cancelInteraction()
        interactionOrigin = point
        view.beginInteraction(interaction)
    }

    func updateInteraction(to point: CGPoint) {
        guard let origin = interactionOrigin, let view = selectedView else { return }
        menu.isHidden = true
        view.updateInteraction(
            translation: CGSize(width: point.x - origin.x, height: point.y - origin.y))
    }

    /// One edit for the whole drag; a cancelled one puts the object back.
    func endInteraction(commit: Bool) {
        guard interactionOrigin != nil else { return }
        interactionOrigin = nil
        selectedView?.endInteraction(commit: commit)
        setNeedsLayout()
        updateMenu()
    }

    func cancelInteraction() {
        endInteraction(commit: false)
    }

    // MARK: - Menu

    private func updateMenu() {
        guard let object = selectedObject, let kind = ObjectMenuKind(object.type),
            interactionOrigin == nil
        else {
            menu.isHidden = true
            return
        }
        var context =
            delegate?.objectsContainer(self, menuContextFor: object) ?? ObjectMenuContext()
        context.isCropping = (views[object.id] as? ImageObjectView)?.isCropping ?? false
        menu.configure(kind: kind, context: context)
        menu.isHidden = false
        bringSubviewToFront(menu)
        layoutMenu()
    }

    private func layoutMenu() {
        guard !menu.isHidden, let object = selectedObject else { return }
        let frame = object.frame.rect.standardized
            .applying(CGAffineTransform(scaleX: zoom, y: zoom))
            .insetBy(dx: -ObjectView.handleInset, dy: -ObjectView.handleInset)
        menu.place(around: frame, visible: ObjectMenu.visibleBounds(of: self))
    }

    func perform(_ action: ObjectMenuAction) {
        guard var object = selectedObject else { return }
        let view = views[object.id]
        switch action {
        case .edit:
            beginEditingSelected()
        case .cut:
            copySelected()
            commit(objects.filter { $0.id != object.id }, actionName: "Cut")
            select(nil)
        case .copy:
            copySelected()
        case .duplicate:
            var copy = object
            copy.id = PaperID.make()
            copy.frame.x += 16
            copy.frame.y += 16
            commit(objects + [copy], actionName: "Duplicate Object")
            select(copy.id)
        case .delete:
            deleteSelected()
        case .color(let hex):
            object.additionalFields["color"] = .string(ToolSettings.normalizedColor(hex) ?? hex)
            replace(object, actionName: "Text Color")
        case .size(let size):
            object.fontSize = size
            replace(object, actionName: "Text Size")
            (views[object.id] as? TextObjectView)?.fitToText()
        case .bringToFront:
            commit(objects.filter { $0.id != object.id } + [object], actionName: "Bring to Front")
        case .sendToBack:
            commit([object] + objects.filter { $0.id != object.id }, actionName: "Send to Back")
        case .crop:
            guard let image = view as? ImageObjectView else { return }
            image.isCropping.toggle()
            updateMenu()
        case .share:
            guard let image = (view as? ImageObjectView)?.shownImage else { return }
            delegate?.objectsContainer(self, share: image, from: menu.frame)
        case .strokeColor(let hex):
            var style = ShapeStyle(object)
            style.stroke = ToolSettings.normalizedColor(hex) ?? style.stroke
            style.apply(to: &object)
            replace(object, actionName: "Stroke Color")
        case .fill(let hex):
            var style = ShapeStyle(object)
            style.fill = hex.flatMap(ToolSettings.normalizedColor)
            style.apply(to: &object)
            replace(object, actionName: "Fill")
        case .width(let width):
            var style = ShapeStyle(object)
            style.strokeWidth = min(max(width, 0.5), 12)
            style.apply(to: &object)
            replace(object, actionName: "Stroke Width")
        case .move(let offset):
            object.frame.x += Double(offset.width)
            object.frame.y += Double(offset.height)
            replace(object, actionName: "Move Object")
        case .scale(let factor):
            let minimum = view?.minimumSize ?? CGSize(width: 32, height: 24)
            replace(ObjectView.scaled(object, by: factor, minimum: minimum), actionName: "Resize Object")
        case .convertToText, .copyLink, .ai:
            break
        }
        setNeedsLayout()
        updateMenu()
    }

    /// The selected object on the pasteboard: Paper's copy, and its picture or
    /// words for other apps.
    private func copySelected() {
        guard let object = selectedObject else { return }
        let view = views[object.id]
        var image: UIImage?
        if let picture = (view as? ImageObjectView)?.shownImage {
            image = picture
        } else if let view, object.type == .shape {
            image = UIGraphicsImageRenderer(bounds: view.content.bounds).image { context in
                view.content.layer.render(in: context.cgContext)
            }
        }
        delegate?.objectsContainer(
            self, copy: [object], image: image, string: object.type == .text ? object.text : nil)
    }
}

// MARK: - Object callbacks

extension ObjectsContainerView: ObjectViewDelegate {
    func objectView(_ view: ObjectView, didCommit object: PageObject, actionName: String) {
        replace(object, actionName: actionName)
    }

    func objectViewDidChangeCropMode(_ view: ObjectView) {
        updateMenu()
    }

    func objectViewNeedsLayout(_ view: ObjectView) {
        layoutMenu()
    }
}
