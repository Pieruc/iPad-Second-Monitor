import CryptoKit
import Foundation
import Network

/// Collegamento con il PC, via cavo USB oppure via Wi-Fi.
///
/// L'app resta in ascolto sulla porta 7878:
///  - via cavo il PC si collega attraverso il servizio Apple per l'USB (usbmuxd);
///  - via Wi-Fi il PC si collega direttamente all'indirizzo IP dell'iPad.
/// Per il Wi-Fi il PC chiede all'app di dimostrare di conoscere il "codice di
/// abbinamento" (mostrato sullo schermo dell'iPad), così nessun altro dispositivo
/// in rete può fingersi l'iPad e muovere il mouse del PC.
///
/// Protocollo: ogni messaggio = 1 byte di tipo + 4 byte di lunghezza (big endian) + dati.
///   PC -> iPad: 1 = hello (JSON), 2 = immagine JPEG intera, 3 = sfida di autenticazione (JSON),
///               4 = zone cambiate dello schermo, 5 = controllo (JSON: puntatore, dimensioni)
///   iPad -> PC: 10 = messaggio JSON (tocchi, ack, info, risposta alla sfida...)
final class Link {
    static let port: UInt16 = 7878

    enum MsgType: UInt8 { case hello = 1, frame = 2, auth = 3, patches = 4, control = 5, json = 10 }

    var onState: ((String) -> Void)?
    var onHello: (([String: Any]) -> Void)?
    var onFrame: ((Data) -> Void)?
    var onPatches: ((Data) -> Void)?
    var onControl: (([String: Any]) -> Void)?
    var onConnected: ((_ viaUSB: Bool) -> Void)?
    var onDisconnected: (() -> Void)?

    /// Codice di abbinamento a 6 cifre, generato una volta e ricordato.
    let pairingCode: String = {
        if let c = UserDefaults.standard.string(forKey: "pairingCode") { return c }
        let c = String(format: "%06d", Int.random(in: 0..<1_000_000))
        UserDefaults.standard.set(c, forKey: "pairingCode")
        return c
    }()

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

    /// Testo mostrato mentre si aspetta il PC.
    func waitingText() -> String {
        var s = "In attesa del PC…\nCavo: collega l'iPad e avvia il programma sul PC"
        if let ip = Link.wifiAddress() {
            s += "\nWi-Fi: indirizzo \(ip) · codice \(pairingCode)"
        } else {
            s += "\nWi-Fi: non connesso"
        }
        return s
    }

    private func startListener() {
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true
        do {
            let l = try NWListener(using: params, on: NWEndpoint.Port(rawValue: Link.port)!)
            l.newConnectionHandler = { [weak self] c in self?.accept(c) }
            l.stateUpdateHandler = { [weak self] state in
                guard let self else { return }
                switch state {
                case .ready:
                    if self.connection == nil { self.report(self.waitingText()) }
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

    private static func isLoopback(_ endpoint: NWEndpoint) -> Bool {
        if case let .hostPort(host, _) = endpoint {
            let h = "\(host)"
            return h.hasPrefix("127.") || h == "::1" || h.hasPrefix("::ffff:127.")
        }
        return false
    }

    private func accept(_ c: NWConnection) {
        connection?.cancel()  // un solo PC alla volta: l'ultima connessione vince
        connection = c
        let viaUSB = Link.isLoopback(c.endpoint)  // usbmuxd arriva dall'interfaccia locale
        c.stateUpdateHandler = { [weak self, weak c] state in
            guard let self, let c else { return }
            switch state {
            case .ready:
                self.report(viaUSB ? "Connesso via cavo" : "Connesso via Wi-Fi")
                DispatchQueue.main.async { self.onConnected?(viaUSB) }
                self.readHeader(c)
            case .failed, .cancelled:
                if self.connection === c {
                    self.connection = nil
                    self.report("PC scollegato.\n" + self.waitingText())
                    DispatchQueue.main.async { self.onDisconnected?() }
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
        case .patches:
            onPatches?(payload)
        case .control:
            if let obj = try? JSONSerialization.jsonObject(with: payload) as? [String: Any] {
                DispatchQueue.main.async { self.onControl?(obj) }
            }
        case .auth:
            // Il PC manda un numero casuale: rispondiamo con HMAC-SHA256(codice, numero)
            if let obj = try? JSONSerialization.jsonObject(with: payload) as? [String: Any],
               let nonce = obj["nonce"] as? String {
                let key = SymmetricKey(data: Data(pairingCode.utf8))
                let mac = HMAC<SHA256>.authenticationCode(for: Data(nonce.utf8), using: key)
                let hex = mac.map { String(format: "%02x", $0) }.joined()
                send(["t": "auth", "mac": hex])
            }
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

    /// Indirizzo IPv4 dell'iPad sulla rete Wi-Fi (interfaccia en0).
    static func wifiAddress() -> String? {
        var ifaddr: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&ifaddr) == 0, let first = ifaddr else { return nil }
        defer { freeifaddrs(ifaddr) }
        var result: String?
        for ptr in sequence(first: first, next: { $0.pointee.ifa_next }) {
            let ifa = ptr.pointee
            guard let addr = ifa.ifa_addr, addr.pointee.sa_family == UInt8(AF_INET),
                  String(cString: ifa.ifa_name) == "en0" else { continue }
            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            if getnameinfo(addr, socklen_t(addr.pointee.sa_len), &host, socklen_t(host.count),
                           nil, 0, NI_NUMERICHOST) == 0 {
                result = String(cString: host)
            }
        }
        return result
    }
}
