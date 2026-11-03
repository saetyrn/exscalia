import PaperCore
import PencilKit
import UIKit

// MARK: - Debug

extension NotebookViewController {
    /// The simulator has no Pencil and `simctl` cannot synthesise a drag, so a
    /// DEBUG-only launch argument puts one real stroke on page 1. It is the
    /// only way to exercise autosave and the pill from a script.
    func runDebugStrokeIfRequested() {
        #if DEBUG
            guard CommandLine.arguments.contains("-debugStroke"),
                let first = pageViews.first, let canvas = first.canvas
            else { return }
            let points = stride(from: 0.0, through: 320.0, by: 8.0).map { offset -> PKStrokePoint in
                let wave = sin(offset / 40) * 40
                return PKStrokePoint(
                    location: CGPoint(x: 90 + offset, y: 240 + wave),
                    timeOffset: offset / 320, size: CGSize(width: 5, height: 5), opacity: 1,
                    force: 1, azimuth: 0, altitude: .pi / 2)
            }
            let stroke = PKStroke(
                ink: PKInk(.pen, color: UIColor(red: 0x1c / 255, green: 0x1c / 255, blue: 0x1e / 255, alpha: 1)),
                path: PKStrokePath(controlPoints: points, creationDate: Date()))
            canvas.drawing = PKDrawing(strokes: [stroke])
            if let ref = session.ref(at: 0) {
                markDirty(ref)
            }
        #endif
    }

    /// `-debugPages <n>` pads the notebook out so that the scroll view has
    /// somewhere to scroll to. The UI test needs a notebook that can scroll in
    /// order to prove that, in Lasso mode, it does not.
    func runDebugPagesIfRequested() {
        #if DEBUG
            let arguments = CommandLine.arguments
            guard let index = arguments.firstIndex(of: "-debugPages"),
                arguments.indices.contains(index + 1),
                let wanted = Int(arguments[index + 1])
            else { return }
            while session.pages.count < min(wanted, 12) {
                session.addPage(after: session.pages.count - 1)
            }
            scrollTo(index: 0, animated: false)
        #endif
    }

    /// `-debugGrow` writes near the bottom-right of page 1, which is the one
    /// thing an infinite page is supposed to answer by getting bigger.
    func runDebugGrowIfRequested() {
        #if DEBUG
            guard CommandLine.arguments.contains("-debugGrow"),
                let first = pageViews.first, let canvas = first.canvas
            else { return }
            let extent = first.pageSize
            let baseX: CGFloat = extent.width - 260
            let baseY: CGFloat = extent.height - 90
            let points = stride(from: 0.0, through: 200.0, by: 8.0).map { offset -> PKStrokePoint in
                let location = CGPoint(x: baseX + CGFloat(offset), y: baseY + CGFloat(offset) / 6)
                return PKStrokePoint(
                    location: location,
                    timeOffset: offset / 200, size: CGSize(width: 5, height: 5), opacity: 1,
                    force: 1, azimuth: 0, altitude: .pi / 2)
            }
            canvas.drawing = PKDrawing(
                strokes: canvas.drawing.strokes + [
                    PKStroke(
                        ink: PKInk(.pen, color: .black),
                        path: PKStrokePath(controlPoints: points, creationDate: Date()))
                ])
        #endif
    }

    /// Drives one loop through exactly the entry points
    /// `LassoGestureRecognizer` calls, because `simctl` cannot synthesise a
    /// curved drag and `XCUITest` only draws straight lines. The touch
    /// arbitration -- the actual bug -- is covered by the UI test; this covers
    /// what the loop then does with what it caught.
    ///
    /// `-debugLassoDrag [cancel|hold]` goes on to drag the selection, through
    /// `routeLassoTouch` like the recogniser, until the top-left of the ink
    /// is at `debugLassoDropPoint` on the page. With `cancel` it then picks
    /// the selection up again, carries it and has the system cancel the
    /// touch, which must leave the ink at that same point; with `hold` it
    /// picks it up again and never lets go, for a screenshot of the ink in
    /// the air.
    func runDebugLassoLoopIfRequested() {
        #if DEBUG
            let arguments = CommandLine.arguments
            let dragIndex = arguments.firstIndex(of: "-debugLassoDrag")
            guard arguments.contains("-debugLassoLoop") || dragIndex != nil else { return }
            let variant = dragIndex.flatMap { index in
                arguments.indices.contains(index + 1) ? arguments[index + 1] : nil
            }
            tools.select(.lasso)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.scrollTo(index: 0, animated: false)
                    self.view.layoutIfNeeded()
                    guard let page = self.pageViews.first, page.lasso != nil,
                        let ink = page.drawing?.bounds, ink.width > 1, ink.height > 1
                    else { return }
                    self.debugLasso(round: ink, on: page)
                    guard dragIndex != nil else { return }
                    let target = Self.debugLassoDropPoint
                    self.debugDrag(
                        on: page, by: CGSize(width: target.x - ink.minX, height: target.y - ink.minY),
                        then: .ended)
                    switch variant {
                    case "cancel":
                        self.debugDrag(on: page, by: CGSize(width: 150, height: 150), then: .cancelled)
                    case "hold":
                        self.debugDrag(on: page, by: CGSize(width: 150, height: 150), then: nil)
                    default:
                        break
                    }
                }
            }
        #endif
    }

    #if DEBUG
        /// Where `-debugLassoDrag` puts the top-left of the ink, in page
        /// points: clear of every edge an infinite page grows from.
        static let debugLassoDropPoint = CGPoint(x: 200, y: 450)

        /// One loop round `ink` (page points). Round whatever is actually on
        /// the page: an infinite page may have grown and moved the ink since
        /// it was put there.
        private func debugLasso(round ink: CGRect, on page: PageView) {
            let centre = CGPoint(x: ink.midX * zoom, y: ink.midY * zoom)
            let radius = (max(ink.width, ink.height) / 2 + 60) * zoom
            func point(_ angle: Double) -> CGPoint {
                stack.convert(
                    CGPoint(x: centre.x + cos(angle) * radius, y: centre.y + sin(angle) * radius),
                    from: page)
            }
            routeLassoTouch(.began(point(0)))
            for step in 1...48 {
                routeLassoTouch(.moved(point(Double(step) / 48 * 2 * .pi)))
            }
            routeLassoTouch(.ended)
        }

        /// Picks the selection up at its middle and carries it `offset` page
        /// points in 24 moves, then ends the touch with `end` -- or leaves it
        /// held when that is nil.
        private func debugDrag(on page: PageView, by offset: CGSize, then end: LassoTouch?) {
            guard let lasso = page.lasso else { return }
            let bounds = lasso.selectionBounds
            let start = stack.convert(CGPoint(x: bounds.midX, y: bounds.midY), from: page)
            routeLassoTouch(.began(start))
            for step in 1...24 {
                let fraction = CGFloat(step) / 24
                routeLassoTouch(
                    .moved(
                        CGPoint(
                            x: start.x + offset.width * zoom * fraction,
                            y: start.y + offset.height * zoom * fraction)))
            }
            if let end { routeLassoTouch(end) }
        }
    #endif

    /// M5's counterpart: the simulator has no Pencil, so the only scriptable
    /// way to give Vision something to read is to stamp the page with printed
    /// text and let the ordinary render-then-recognise pipeline run.
    func runDebugHandwritingIfRequested() {
        #if DEBUG
            let arguments = CommandLine.arguments
            guard arguments.contains("-debugHandwriting") else { return }
            guard let view = currentPageView else { return }
            let phrase =
                arguments.firstIndex(of: "-debugHandwriting").flatMap { index in
                    arguments.indices.contains(index + 1)
                        && !arguments[index + 1].hasPrefix("-")
                        ? arguments[index + 1] : nil
                } ?? "quarterly review"
            insertTextObject(
                text: phrase,
                at: CGPoint(x: view.pageSize.width / 2, y: view.pageSize.height * 0.25),
                fontSize: 40)
            view.deselectObjects()
        #endif
    }

    /// The M4 counterparts of `-debugStroke`: the simulator has no Pencil, no
    /// camera and no Files app worth scripting, so text, an image, a PDF import
    /// and an export each get a launch argument.
    func runDebugObjectsIfRequested() {
        #if DEBUG
            let arguments = CommandLine.arguments
            if arguments.contains("-debugTextObject") {
                guard let view = currentPageView else { return }
                insertTextObject(
                    text: "Quarterly review",
                    at: CGPoint(x: view.pageSize.width / 2, y: view.pageSize.height * 0.22))
                view.deselectObjects()
                if let data = Self.debugImagePNG() {
                    insertImage(
                        data: data, name: "debug-swatch.png",
                        at: CGPoint(x: view.pageSize.width / 2, y: view.pageSize.height * 0.62))
                    view.deselectObjects()
                }
            }
            let importing = runDebugImportIfRequested()
            if let index = arguments.firstIndex(of: "-debugExport") {
                let path = arguments.indices.contains(index + 1) ? arguments[index + 1] : ""
                Task { @MainActor [weak self] in
                    // After the import, so the export has its pages in it.
                    await importing?.value
                    try? await Task.sleep(nanoseconds: 1_500_000_000)
                    await self?.runDebugExport(to: path)
                }
            }
        #endif
    }

    #if DEBUG
        /// `-debugImportPDF [path]`: the file, or a built-in three-page
        /// fixture, imported after the current page and then annotated.
        private func runDebugImportIfRequested() -> Task<Void, Never>? {
            let arguments = CommandLine.arguments
            guard let index = arguments.firstIndex(of: "-debugImportPDF") else { return nil }
            let path = arguments.indices.contains(index + 1) ? arguments[index + 1] : ""
            let data =
                (path.isEmpty ? nil : try? Data(contentsOf: URL(fileURLWithPath: path)))
                ?? Self.debugFixturePDF()
            guard let data else { return nil }
            return Task { @MainActor [weak self] in
                await self?.insertPDF(data: data, name: "debug-fixture.pdf")
                if let self, let view = self.currentPageView {
                    self.annotate(view)
                }
            }
        }

        /// One stroke over whatever page is showing, so an imported PDF page
        /// can be screenshotted with an annotation on it.
        private func annotate(_ view: PageView) {
            guard let canvas = view.canvas else { return }
            let points = stride(from: 0.0, through: 300.0, by: 8.0).map { offset -> PKStrokePoint in
                PKStrokePoint(
                    location: CGPoint(x: 120 + offset, y: 420 + sin(offset / 40) * 30),
                    timeOffset: offset / 300, size: CGSize(width: 6, height: 6), opacity: 1,
                    force: 1, azimuth: 0, altitude: .pi / 2)
            }
            canvas.drawing = PKDrawing(strokes: [
                PKStroke(
                    ink: PKInk(.pen, color: UIColor(red: 0.75, green: 0.12, blue: 0.12, alpha: 1)),
                    path: PKStrokePath(controlPoints: points, creationDate: Date()))
            ])
            if let index = pageViews.firstIndex(where: { $0 === view }),
                let ref = session.ref(at: index)
            {
                markDirty(ref)
            }
        }

        private func runDebugExport(to path: String) async {
            guard let url = await controller.exportPDF(notebook: session.notebook) else {
                print("[paper] export failed")
                return
            }
            var destination = URL(fileURLWithPath: path.isEmpty ? "export.pdf" : path)
            if path.isEmpty || (try? Data(contentsOf: url).write(to: destination)) == nil {
                let documents = FileManager.default.urls(
                    for: .documentDirectory, in: .userDomainMask)
                destination =
                    (documents.first ?? FileManager.default.temporaryDirectory)
                    .appending(path: "export.pdf", directoryHint: .notDirectory)
                try? FileManager.default.removeItem(at: destination)
                try? FileManager.default.copyItem(at: url, to: destination)
            }
            print("[paper] exported to \(destination.path(percentEncoded: false))")
        }

        /// A red square, used as the image object in screenshots.
        private static func debugImagePNG() -> Data? {
            let renderer = UIGraphicsImageRenderer(size: CGSize(width: 240, height: 160))
            return renderer.image { context in
                UIColor(red: 0.85, green: 0.3, blue: 0.2, alpha: 1).setFill()
                context.fill(CGRect(x: 0, y: 0, width: 240, height: 160))
            }.pngData()
        }

        /// A three-page PDF built with the exporter, so the import path has
        /// something real to read without a file in the bundle.
        private static func debugFixturePDF() -> Data? {
            let sizes = [
                CGSize(width: 595, height: 842), CGSize(width: 612, height: 792),
                CGSize(width: 400, height: 400),
            ]
            let texts = ["Alpha page one", "Beta page two", "Gamma page three"]
            let jobs = zip(sizes, texts).map { size, text in
                PageRenderJob(
                    notebookPath: "paper/Fixture.paper", pageID: PaperID.make(), template: .blank,
                    size: .a4, customSize: SizePoints(size),
                    objects: [
                        PageObject(
                            type: .text,
                            frame: PageObject.Frame(
                                x: 40, y: 40, width: Double(size.width) - 80, height: 120),
                            text: text, fontSize: 24)
                    ])
            }
            return try? PDFExporter.pdf(jobs)
        }
    #endif
}
