import PaperCore
import PencilKit
import PhotosUI
import UIKit
import UniformTypeIdentifiers

// MARK: - Objects and import

extension NotebookViewController {
    /// The page an insertion lands on: whatever is under the viewport.
    var currentPageView: PageView? {
        pageViews.indices.contains(session.currentIndex) ? pageViews[session.currentIndex] : nil
    }

    private var currentRef: PageRef? { session.ref(at: session.currentIndex) }

    /// Puts a text object centred on a point -- the last hover point, or the
    /// middle of the page -- in the Text tool's colour, and opens the keyboard
    /// on it. Scribble works because it is a plain `UITextView`.
    func insertTextObject(
        text: String = "", at insertion: CGPoint? = nil,
        fontSize: Double = PageObject.defaultFontSize, on page: PageView? = nil
    ) {
        guard let view = page ?? currentPageView else { return }
        let point = insertion ?? view.insertionPoint
        let width = min(420, max(160, view.pageSize.width - point.x - 24))
        var object = view.textObject(at: point, text: text, fontSize: fontSize)
        object.frame = PageObject.Frame(
            x: Double(max(12, point.x - width / 2)), y: Double(max(12, point.y - 20)),
            width: Double(width), height: max(44, fontSize * 1.6))
        view.insertObject(object)
        UIAccessibility.post(notification: .layoutChanged, argument: nil)
    }

    /// A tap with the Text tool at a point of the page stack: a text box there,
    /// at the text slot's size and colour, typing at once. The tool falls back
    /// to the lasso when the box is put down. A tap on an object leaves it to
    /// the object.
    func placeText(at point: CGPoint) {
        guard tools.active == .text, !session.chrome.isReadOnly,
            let index = pageViews.firstIndex(where: { $0.frame.contains(point) }),
            pageViews[index].isLive, !pageViews[index].isExternallyEditing
        else { return }
        let page = pageViews[index]
        let local = page.convert(point, from: stack)
        guard page.objects.objectID(at: local) == nil, !page.hasSelectedObject else { return }
        session.currentIndex = index
        let scale = max(zoom, 0.01)
        page.placeText(at: CGPoint(x: local.x / scale, y: local.y / scale))
    }

    /// Stores the bytes as an asset and puts an image object on the page.
    func insertImage(data: Data, name: String, at point: CGPoint? = nil) {
        guard let view = currentPageView, let image = UIImage(data: data) else { return }
        guard
            let assetPath = controller.storeAsset(
                data, name: name, notebookPath: session.notebook.path)
        else {
            presentImageNotAdded(tooLarge: data.count > AssetStore.maxBytes)
            return
        }
        let centre = point ?? view.insertionPoint
        let pixels = CGSize(
            width: image.size.width * image.scale, height: image.size.height * image.scale)
        let frame = ImageObjectView.fittedFrame(
            pixels: pixels, pageSize: view.pageSize, centre: centre)
        view.insertObject(PageObject(type: .image, frame: frame, asset: assetPath))
    }

    /// A photo, paste or drop, as the picture the person saw. Turning it
    /// upright, capping its size and encoding it run off the main actor; only
    /// the insert comes back.
    func importImage(_ image: UIImage, name: String, at point: CGPoint? = nil) {
        guard let cgImage = image.cgImage else {
            presentImageNotAdded(tooLarge: false)
            return
        }
        let orientation = image.imageOrientation
        let scale = image.scale
        Task.detached(priority: .userInitiated) { [weak self] in
            let normalized = ImageImport.normalized(
                cgImage: cgImage, orientation: orientation, scale: scale)
            await self?.insertNormalizedImage(normalized, name: name, at: point)
        }
    }

    private func insertNormalizedImage(
        _ normalized: (data: Data, extension: String)?, name: String, at point: CGPoint?
    ) {
        guard let normalized else {
            presentImageNotAdded(tooLarge: false)
            return
        }
        insertImage(data: normalized.data, name: "\(name).\(normalized.extension)", at: point)
    }

    private func presentImageNotAdded(tooLarge: Bool) {
        let cap = AssetStore.maxBytes / (1024 * 1024)
        let alert = UIAlertController(
            title: "Image not added",
            message: tooLarge
                ? "Images larger than \(cap) MB cannot be added."
                : "The image could not be saved.",
            preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "OK", style: .default))
        present(alert, animated: !UIAccessibility.isReduceMotionEnabled)
    }

    func insertPDF(data: Data, name: String) async {
        guard
            let result = await controller.importPDF(
                data: data, name: name, into: session.notebook,
                after: session.pages.indices.contains(session.currentIndex)
                    ? session.pages[session.currentIndex].id : nil)
        else { return }
        session.reload()
        rebuildPages()
        if let first = result.pages.first,
            let index = session.pages.firstIndex(where: { $0.id == first.id })
        {
            session.currentIndex = index
            scrollTo(index: index, animated: false)
        }
        UIAccessibility.post(
            notification: .announcement,
            argument: "Imported \(result.pages.count) pages")
    }

    func setTemplate(_ template: Template) {
        guard let ref = currentRef,
            var page = try? controller.store.page(notebookPath: ref.notebookPath, id: ref.pageID)
        else { return }
        page.template = template
        page.modified = Date()
        try? controller.store.save(page: page, notebookPath: ref.notebookPath)
        markDirty(ref)
        session.reload()
        currentPageView?.update(page: page)
    }

    // MARK: - Pickers

    func presentPhotoPicker() {
        var configuration = PHPickerConfiguration()
        configuration.filter = .images
        configuration.selectionLimit = 1
        let picker = PHPickerViewController(configuration: configuration)
        picker.delegate = self
        present(picker, animated: !UIAccessibility.isReduceMotionEnabled)
    }

    func presentCamera() {
        guard UIImagePickerController.isSourceTypeAvailable(.camera) else { return }
        let picker = UIImagePickerController()
        picker.sourceType = .camera
        picker.delegate = self
        present(picker, animated: !UIAccessibility.isReduceMotionEnabled)
    }

    func presentPDFPicker() {
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: [.pdf])
        picker.allowsMultipleSelection = false
        picker.delegate = self
        present(picker, animated: !UIAccessibility.isReduceMotionEnabled)
    }

    func presentDocumentScanner() {
        guard let scanDocument else { return }
        Task { @MainActor [weak self] in
            guard let self else { return }
            do {
                let data = try await scanDocument(self)
                await self.insertPDF(data: data, name: "Scanned document.pdf")
            } catch {
                // Cancellation needs no error UI; every other scanner failure
                // is useful and stays local to this sheet.
                let nsError = error as NSError
                if nsError.localizedDescription.localizedCaseInsensitiveContains("cancel") {
                    return
                }
                let alert = UIAlertController(
                    title: "Scan unavailable", message: nsError.localizedDescription,
                    preferredStyle: .alert)
                alert.addAction(UIAlertAction(title: "OK", style: .default))
                self.present(alert, animated: !UIAccessibility.isReduceMotionEnabled)
            }
        }
    }

    /// A link is a text object holding a URL: one kind of object to move,
    /// resize and search rather than a second thing that looks like text.
    func presentLinkPrompt() {
        let alert = UIAlertController(title: "Link", message: nil, preferredStyle: .alert)
        alert.addTextField { field in
            field.placeholder = "https://"
            field.keyboardType = .URL
            field.autocapitalizationType = .none
            field.accessibilityLabel = "Link address"
        }
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel))
        alert.addAction(
            UIAlertAction(title: "Insert", style: .default) { [weak self, weak alert] _ in
                let text = alert?.textFields?.first?.text ?? ""
                guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
                self?.insertTextObject(text: text)
            })
        present(alert, animated: !UIAccessibility.isReduceMotionEnabled)
    }

    /// Cmd-V, onto the page under the viewport.
    func pasteFromClipboard() {
        guard let view = currentPageView else { return }
        paste(onto: view, at: nil)
    }

    /// Paper's own copy comes back as ink and objects, selected; other
    /// images come off the pasteboard as pictures; anything else is pasted
    /// as text. `point` is in page points: the long-press menu's Paste puts
    /// the paste there, Cmd-V a little down and right of where it was copied.
    func paste(onto view: PageView, at point: CGPoint?) {
        guard !session.chrome.isReadOnly, !view.isExternallyEditing else { return }
        if let index = pageViews.firstIndex(where: { $0 === view }) { session.currentIndex = index }
        if let clip = PaperClip.read() {
            paste(clip, onto: view, at: point)
            return
        }
        let board = UIPasteboard.general
        if board.hasImages, let image = board.image {
            importImage(image, name: "pasted", at: point)
        } else if board.hasStrings, let text = board.string, !text.isEmpty {
            insertTextObject(text: text, at: point, on: view)
        }
    }

    /// Ink and objects from Paper's copy, as one undo step: new ids, images
    /// from another notebook stored again in this one, and the paste is the
    /// selection afterwards.
    func paste(_ clip: PaperClip, onto view: PageView, at point: CGPoint?) {
        guard let canvas = view.canvas else { return }
        let bounds = clip.bounds
        guard !bounds.isNull else { return }
        let offset =
            point.map { CGSize(width: $0.x - bounds.midX, height: $0.y - bounds.midY) }
            ?? CGSize(width: 16, height: 16)
        view.putDownSelection()

        var objects = clip.objects.map { object -> PageObject in
            var copy = object
            copy.id = PaperID.make()
            copy.frame.x += Double(offset.width)
            copy.frame.y += Double(offset.height)
            return copy
        }
        if clip.notebookPath != session.notebook.path {
            for index in objects.indices where objects[index].type == .image {
                guard let asset = objects[index].asset,
                    let data = controller.store.assets.data(
                        forAssetPath: asset, inNotebook: clip.notebookPath),
                    let stored = controller.storeAsset(
                        data, name: (asset as NSString).lastPathComponent,
                        notebookPath: session.notebook.path)
                else { continue }
                objects[index].asset = stored
            }
        }
        if !objects.isEmpty {
            view.objects.commit(view.objects.objects + objects, actionName: "Paste")
        }
        var strokes: [Int] = []
        if let data = clip.drawing, let ink = try? PKDrawing(data: data), !ink.strokes.isEmpty {
            let move = CGAffineTransform(translationX: offset.width, y: offset.height)
            let pasted = ink.strokes.map { stroke -> PKStroke in
                var copy = stroke
                copy.transform = copy.transform.concatenating(move)
                return copy
            }
            let first = canvas.drawing.strokes.count
            replaceInk(
                on: view, with: PKDrawing(strokes: canvas.drawing.strokes + pasted),
                actionName: "Paste")
            strokes = Array(first..<(first + pasted.count))
        }
        putDownSelections(except: view)
        if tools.active != .lasso { tools.select(.lasso) }
        if strokes.isEmpty, objects.count == 1, let only = objects.first {
            view.selectObject(only.id)
        } else {
            view.lasso?.select(strokes: strokes, objects: objects.map(\.id))
        }
        UIAccessibility.post(notification: .announcement, argument: "Pasted")
    }
}

extension NotebookViewController: PHPickerViewControllerDelegate {
    public func picker(_ picker: PHPickerViewController, didFinishPicking results: [PHPickerResult])
    {
        picker.dismiss(animated: !UIAccessibility.isReduceMotionEnabled)
        guard let provider = results.first?.itemProvider,
            provider.canLoadObject(ofClass: UIImage.self)
        else { return }
        let name = provider.suggestedName ?? "photo"
        // Already off the main actor here, so the image is made upright and
        // encoded before the hop back.
        provider.loadObject(ofClass: UIImage.self) { [weak self] object, _ in
            guard let image = object as? UIImage, let cgImage = image.cgImage else { return }
            let normalized = ImageImport.normalized(
                cgImage: cgImage, orientation: image.imageOrientation, scale: image.scale)
            Task { @MainActor in
                self?.insertNormalizedImage(normalized, name: name, at: nil)
            }
        }
    }
}

extension NotebookViewController: UIImagePickerControllerDelegate, UINavigationControllerDelegate {
    public func imagePickerController(
        _ picker: UIImagePickerController,
        didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]
    ) {
        picker.dismiss(animated: !UIAccessibility.isReduceMotionEnabled)
        guard let image = info[.originalImage] as? UIImage else { return }
        importImage(image, name: "photo")
    }

    public func imagePickerControllerDidCancel(_ picker: UIImagePickerController) {
        picker.dismiss(animated: !UIAccessibility.isReduceMotionEnabled)
    }
}

extension NotebookViewController: UIDocumentPickerDelegate {
    public func documentPicker(
        _ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]
    ) {
        guard let url = urls.first else { return }
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        guard let data = try? Data(contentsOf: url) else { return }
        let name = url.lastPathComponent
        Task { await insertPDF(data: data, name: name) }
    }
}

// MARK: - Debug

#if DEBUG
    extension NotebookViewController {
        /// `-debugShape <kind>`: the Shapes tool places one shape of that kind
        /// low on page 1, through the tap a finger makes with the tool, and
        /// falls back to the lasso with the shape selected.
        func runDebugShape() {
            guard let page = pageViews.first, page.isLive else { return }
            tools.select(.shape)
            let point = CGPoint(
                x: page.pageSize.width / 2, y: min(page.pageSize.height - 90, 720))
            page.handleObjectTap(at: CGPoint(x: point.x * zoom, y: point.y * zoom))
            print(
                "[paper] shape \(tools.settings.shapeKind.rawValue): "
                    + "\(page.selectionKind?.rawValue ?? "nothing") selected, tool \(tools.active.rawValue)")
        }
    }
#endif
