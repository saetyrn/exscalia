import CoreGraphics
import Foundation

extension PaperStore {
    /// Records the area of a board that its `render.png` shows, as
    /// `render_rect` in the local `page.json`; `nil` clears it.
    ///
    /// Derived, like the render it describes: it does not touch `modified` and
    /// journals nothing. It rides the page PUT's `render_rect` field with the
    /// next push of the render, full or derived-only, and the server keeps its
    /// own copy beside the picture. A rectangle that has not changed writes
    /// nothing.
    @discardableResult
    public func setRenderRect(_ rect: PageObject.Frame?, notebookPath: String, pageID: String)
        throws -> Page
    {
        try updatePage(notebookPath: notebookPath, pageID: pageID) { page in
            page.renderRect = rect
        }
    }
}
