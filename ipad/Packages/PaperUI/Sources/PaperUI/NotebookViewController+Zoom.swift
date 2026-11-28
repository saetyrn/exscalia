import PaperCore
import UIKit

// MARK: - Zoom

extension NotebookViewController {
    @objc func handlePinch(_ gesture: UIPinchGestureRecognizer) {
        switch gesture.state {
        case .began:
            // A pinch that arrives mid-stroke is a palm landing, not a zoom.
            pinchAccepted = !isDrawing
            guard pinchAccepted else { return }
            pinchStartZoom = zoom
        case .changed:
            // An accepted pinch still holds still under a live stroke.
            guard pinchAccepted, !isDrawing else { return }
            let anchor = gesture.location(in: stack)
            let previous = zoom
            setZoom(pinchStartZoom * gesture.scale)
            guard previous > 0 else { return }
            // Keep the point under the fingers under the fingers, on both
            // axes: an infinite page scrolls sideways as well.
            let factor = zoom / previous
            let offset = scrollView.contentOffset
            let anchorInView = CGPoint(
                x: anchor.x - offset.x, y: anchor.y - offset.y)
            let ceilingX = max(0, scrollView.contentSize.width - scrollView.bounds.width)
            let ceilingY = max(0, scrollView.contentSize.height - scrollView.bounds.height)
            scrollView.contentOffset = CGPoint(
                x: min(max(0, anchor.x * factor - anchorInView.x), ceilingX),
                y: min(max(0, anchor.y * factor - anchorInView.y), ceilingY))
        case .ended, .cancelled, .failed:
            defer { pinchAccepted = false }
            guard pinchAccepted else { return }
            session.zoom = zoom
            schedulePersist()
        default:
            break
        }
    }

    func setZoom(_ next: CGFloat) {
        let clamped = min(max(next, 0.5), 4)
        guard abs(clamped - zoom) > 0.001 else { return }
        zoom = clamped
        stack.zoom = clamped
        for view in pageViews { view.zoom = clamped }
        view.setNeedsLayout()
        view.layoutIfNeeded()
        scrollView.contentSize = CGSize(
            width: max(scrollView.bounds.width, stack.contentWidth), height: stack.contentHeight)
    }

    func schedulePersist() {
        persistWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.persistPosition()
            }
        }
        persistWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5, execute: work)
    }

    // MARK: - Infinite pages

    /// Extends an infinite page when the work approaches an edge.
    ///
    /// The rule itself is `InfiniteCanvas` in PaperCore, so the screen, the
    /// renderer and the exporter cannot disagree about it. What lives here is
    /// only the consequence: write the new extent, move the objects if the page
    /// grew backwards, and shift the scroll offset by exactly as much as the
    /// content moved, so the page under the pen does not jump.
    func growPageIfNeeded(_ pageView: PageView) {
        guard pageView.page.size.isInfinite,
            let ref = ref(for: pageView),
            let content = pageView.contentBounds
        else { return }
        let scale = max(zoom, 0.01)
        let viewport = CGSize(
            width: scrollView.bounds.width / scale, height: scrollView.bounds.height / scale)
        guard
            let growth = InfiniteCanvas.growth(
                extent: pageView.pageSize, content: content, viewport: viewport,
                quantum: TemplateGeometry.growthQuantum(for: pageView.page.template))
        else { return }

        // Where a fixed point of the page sits before the page changes shape.
        let before = stack.convert(CGPoint.zero, from: pageView)
        let moved = pageView.applyGrowth(growth)
        stack.setNeedsLayout()
        view.setNeedsLayout()
        view.layoutIfNeeded()
        // ...and where that same material point sits afterwards.
        let after = stack.convert(
            CGPoint(x: growth.translation.width * scale, y: growth.translation.height * scale),
            from: pageView)
        let ceilingY = max(0, scrollView.contentSize.height - scrollView.bounds.height)
        let ceilingX = max(0, scrollView.contentSize.width - scrollView.bounds.width)
        scrollView.contentOffset = CGPoint(
            x: min(max(0, scrollView.contentOffset.x + after.x - before.x), ceilingX),
            y: min(max(0, scrollView.contentOffset.y + after.y - before.y), ceilingY))

        try? controller.store.setPageExtent(
            growth.extent, notebookPath: session.notebook.path, pageID: pageView.page.id)
        if let moved {
            setObjects(moved, on: ref)
        }
        markDirty(ref)
        session.reload()
    }
}
