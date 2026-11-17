import Foundation
import Network

/// Collegamento con il PC tramite cavo USB.
///
/// L'app resta in ascolto sulla porta 7878 dell'interfaccia locale (127.0.0.1).
/// Il PC si collega attraverso il servizio Apple per l'USB (usbmuxd), che
/// inoltra la connessione all'iPad lungo il cavo: niente Wi-Fi.
///
/// Protocollo: ogni messaggio = 1 byte di tipo + 4 byte di lunghezza (big endian) + dati.
///   PC -> iPad: 1 = hello (JSON), 2 = immagine JPEG
///   iPad -> PC: 10 = messaggio JSON (tocchi, ack, info...)
final class Link {
    static let port: UInt16 = 7878

    enum MsgType: UInt8 { case hello = 1, frame = 2, json = 10 }

    var onState: ((String) -> Void)?
    var onHello: (([String: Any]) -> Void)?
    var onFrame: ((Data) -> Void)?
    var onConnected: (() -> Void)?

    private let queue = DispatchQueue(label: "secondscreen.link")
    private var listener: NWListener?
    private var connection: NWConnection?

    func start() {
        queue.async { self.startListener() }
    }

    /// Da chiamare quando l'app torna in primo piano: iOS può aver chiuso il listener.
    func ensureRunning() {
        queue.async {
            if self.listener == nil { self.startListener() }
        }
    }

    private func startListener() {
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true
        params.requiredLocalEndpoint = .hostPort(host: "127.0.0.1",
                                                 port: NWEndpoint.Port(rawValue: Link.port)!)
        do {
            let l = try NWListener(using: params)
            l.newConnectionHandler = { [weak self] c in self?.accept(c) }
            l.stateUpdateHandler = { [weak self] state in
                guard let self else { return }
                switch state {
                case .ready:
                    self.report("In attesa del PC… collega il cavo e avvia il programma sul PC")
                case .failed(let err):
                    self.report("Errore: \(err.localizedDescription). Riprovo…")
                    self.listener?.cancel()
                    self.listener = nil
                    self.queue.asyncAfter(deadline: .now() + 1) { self.startListener() }
                default:
                    break
                }
            }
            listener = l
            l.start(queue: queue)
        } catch {
            report("Impossibile aprire la porta \(Link.port): \(error.localizedDescription)")
            queue.asyncAfter(deadline: .now() + 2) { self.startListener() }
        }
    }

    private func accept(_ c: NWConnection) {
        connection?.cancel()  // un solo PC alla volta: l'ultima connessione vince
        connection = c
        c.stateUpdateHandler = { [weak self, weak c] state in
            guard let self, let c else { return }
            switch state {
            case .ready:
                self.report("Connesso via cavo")
                DispatchQueue.main.async { self.onConnected?() }
                self.readHeader(c)
            case .failed, .cancelled:
                if self.connection === c {
                    self.connection = nil
                    self.report("PC scollegato. In attesa…")
                }
            default:
                break
            }
        }
        c.start(queue: queue)
    }

    private func readHeader(_ c: NWConnection) {
        c.receive(minimumIncompleteLength: 5, maximumLength: 5) { [weak self] data, _, isComplete, error in
            guard let self else { return }
            guard error == nil, let data, data.count == 5 else {
                if isComplete || error != nil { c.cancel() }
                return
            }
            let bytes = [UInt8](data)
            let type = bytes[0]
            let length = Int(bytes[1]) << 24 | Int(bytes[2]) << 16 | Int(bytes[3]) << 8 | Int(bytes[4])
            if length == 0 {
                self.handle(type: type, payload: Data())
                self.readHeader(c)
            } else {
                self.readPayload(c, type: type, length: length)
            }
        }
    }

    private func readPayload(_ c: NWConnection, type: UInt8, length: Int) {
        c.receive(minimumIncompleteLength: length, maximumLength: length) { [weak self] data, _, _, error in
            guard let self else { return }
            guard error == nil, let data, data.count == length else { c.cancel(); return }
            self.handle(type: type, payload: data)
            self.readHeader(c)
        }
    }

    private func handle(type: UInt8, payload: Data) {
        switch MsgType(rawValue: type) {
        case .hello:
            if let obj = try? JSONSerialization.jsonObject(with: payload) as? [String: Any] {
                DispatchQueue.main.async { self.onHello?(obj) }
            }
        case .frame:
            onFrame?(payload)  // decodificato fuori dal thread principale da chi lo riceve
        default:
            break
        }
    }

    /// Invia un messaggio JSON al PC (tocchi, ack, ...). Thread-safe.
    func send(_ obj: [String: Any]) {
        guard let body = try? JSONSerialization.data(withJSONObject: obj) else { return }
        var msg = Data([MsgType.json.rawValue])
        let n = UInt32(body.count)
        msg.append(contentsOf: [UInt8(n >> 24 & 0xff), UInt8(n >> 16 & 0xff),
                                UInt8(n >> 8 & 0xff), UInt8(n & 0xff)])
        msg.append(body)
        queue.async {
            self.connection?.send(content: msg, completion: .contentProcessed { _ in })
        }
    }

    private func report(_ s: String) {
        DispatchQueue.main.async { self.onState?(s) }
    }
}
