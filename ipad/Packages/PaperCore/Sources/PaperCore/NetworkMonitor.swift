import Foundation
import Network

/// Tells the sync engine whether there is a route to anywhere.
///
/// `NWPathMonitor` rather than a failed request: "offline -- saved on iPad" has
/// to be true the moment the Wi-Fi drops, not one timeout later, and a person
/// writing on a train should never see a spinner-shaped lie.
public final class NetworkMonitor: Sendable {
    private let monitor: NWPathMonitor
    private let queue = DispatchQueue(label: "art.exscalia.paper.network")

    public init() {
        monitor = NWPathMonitor()
    }

    /// Starts watching and reports every change, including the first reading.
    public func start(onChange: @escaping @Sendable (Bool) -> Void) {
        monitor.pathUpdateHandler = { path in
            onChange(path.status == .satisfied)
        }
        monitor.start(queue: queue)
    }

    public func cancel() {
        monitor.cancel()
    }
}
