import UIKit

/// Mostra lo schermo del PC e trasforma i tocchi in eventi mouse.
/// Gesti (uguali alla versione web):
///   1 dito: tocco = click, trascina = trascinamento, pressione lunga = click destro
///   2 dita: scorrimento
///   3 dita (tap): attiva/disattiva il puntatore disegnato dal PC
final class ScreenViewController: UIViewController, UIPointerInteractionDelegate {
    private let link = Link()
    private let imageView = UIImageView()
    private let statusLabel = PaddedLabel()
    private let decodeQueue = DispatchQueue(label: "secondscreen.decode")
    private var hideStatusWork: DispatchWorkItem?

    private var frameSize = CGSize(width: 16, height: 9)
    private var drawCursor = UserDefaults.standard.object(forKey: "drawCursor") as? Bool ?? true
    private var fpsCount = 0
    private var fps = 0
    private var fpsStart = CACurrentMediaTime()

    // stato dei gesti
    private struct OneFinger {
        var start: CGPoint
        var last: CGPoint
        var down = false
        var cancelled = false
    }
    private var one: OneFinger?
    private var longPressTimer: Timer?
    private var scrollCenter: CGPoint?
    private let dragThreshold: CGFloat = 8
    private let longPressDelay: TimeInterval = 0.55

    override var prefersStatusBarHidden: Bool { true }
    override var prefersHomeIndicatorAutoHidden: Bool { true }
    override var preferredScreenEdgesDeferringSystemGestures: UIRectEdge { .all }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        view.isMultipleTouchEnabled = true

        imageView.contentMode = .scaleAspectFit
        imageView.frame = view.bounds
        imageView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(imageView)

        statusLabel.textColor = .white
        statusLabel.font = .systemFont(ofSize: 15, weight: .medium)
        statusLabel.backgroundColor = UIColor(white: 0.12, alpha: 0.88)
        statusLabel.layer.cornerRadius = 10
        statusLabel.layer.masksToBounds = true
        statusLabel.numberOfLines = 0
        statusLabel.textAlignment = .center
        statusLabel.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(statusLabel)
        NSLayoutConstraint.activate([
            statusLabel.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            statusLabel.topAnchor.constraint(equalTo: view.topAnchor, constant: 20),
            statusLabel.widthAnchor.constraint(lessThanOrEqualTo: view.widthAnchor, multiplier: 0.8),
        ])

        // Trackpad/mouse dell'iPad: nasconde il puntatore di iPadOS e segue il movimento
        view.addInteraction(UIPointerInteraction(delegate: self))
        view.addGestureRecognizer(UIHoverGestureRecognizer(target: self, action: #selector(hover(_:))))

        link.onState = { [weak self] s in self?.showStatus(s, autoHide: s.hasPrefix("Connesso")) }
        link.onConnected = { [weak self] _ in
            guard let self else { return }
            self.link.send(["t": "cursor", "on": self.drawCursor])
            self.sendInfo()
        }
        link.onHello = { _ in }
        link.onFrame = { [weak self] data in self?.decode(data) }
        showStatus("Avvio…", autoHide: false)
        link.start()
    }

    func appBecameActive() {
        link.ensureRunning()
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        sendInfo()
    }

    // MARK: immagini

    private func decode(_ data: Data) {
        decodeQueue.async { [weak self] in
            guard let self else { return }
            let image = UIImage(data: data)?.preparingForDisplay()
            DispatchQueue.main.async {
                if let image {
                    self.imageView.image = image
                    self.frameSize = image.size
                    self.countFrame()
                }
                self.link.send(["t": "ack"])  // pronto per l'immagine successiva
            }
        }
    }

    private func countFrame() {
        fpsCount += 1
        let now = CACurrentMediaTime()
        if now - fpsStart > 1 { fps = fpsCount; fpsCount = 0; fpsStart = now }
    }

    private func sendInfo() {
        let s = view.window?.screen.scale ?? UIScreen.main.scale
        link.send(["t": "info",
                   "w": Int(view.bounds.width * s), "h": Int(view.bounds.height * s), "dpr": s,
                   "fullscreen": true, "standalone": true, "fsApi": false,
                   "secure": true, "wakeLock": "app nativa", "video": "-",
                   "ua": "SecondScreen app (iPadOS \(UIDevice.current.systemVersion))"])
    }

    // MARK: coordinate

    /// Rettangolo in cui l'immagine viene effettivamente mostrata (aspect fit).
    private var imageRect: CGRect {
        let b = view.bounds
        guard frameSize.width > 0, frameSize.height > 0 else { return b }
        let scale = min(b.width / frameSize.width, b.height / frameSize.height)
        let w = frameSize.width * scale, h = frameSize.height * scale
        return CGRect(x: (b.width - w) / 2, y: (b.height - h) / 2, width: w, height: h)
    }

    private func norm(_ p: CGPoint) -> [String: Any] {
        let r = imageRect
        let x = min(max((p.x - r.minX) / r.width, 0), 1)
        let y = min(max((p.y - r.minY) / r.height, 0), 1)
        return ["x": Double(x), "y": Double(y)]
    }

    private func send(_ type: String, at p: CGPoint, button: String? = nil) {
        var m = norm(p)
        m["t"] = type
        if let button { m["b"] = button }
        link.send(m)
    }

    // MARK: tocchi

    private func activeTouches(_ event: UIEvent?) -> [UITouch] {
        (event?.allTouches ?? []).filter { $0.phase != .ended && $0.phase != .cancelled }
    }

    private func centroid(_ touches: [UITouch]) -> CGPoint {
        let pts = touches.map { $0.location(in: view) }
        let n = CGFloat(max(pts.count, 1))
        return CGPoint(x: pts.map(\.x).reduce(0, +) / n, y: pts.map(\.y).reduce(0, +) / n)
    }

    private func cancelOneFinger() {
        longPressTimer?.invalidate()
        longPressTimer = nil
        if var o = one {
            if o.down { send("up", at: o.last, button: "left") }
            o.cancelled = true
            one = o
        }
    }

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
        let active = activeTouches(event)
        switch active.count {
        case 1:
            let p = active[0].location(in: view)
            one = OneFinger(start: p, last: p)
            send("move", at: p)
            longPressTimer?.invalidate()
            longPressTimer = Timer.scheduledTimer(withTimeInterval: longPressDelay, repeats: false) { [weak self] _ in
                guard let self, var o = self.one, !o.down, !o.cancelled else { return }
                self.send("click", at: o.last, button: "right")
                o.cancelled = true
                self.one = o
                UIImpactFeedbackGenerator(style: .light).impactOccurred()
            }
        case 2:
            cancelOneFinger()
            scrollCenter = centroid(active)
        case 3:
            cancelOneFinger()
            scrollCenter = nil
            drawCursor.toggle()
            UserDefaults.standard.set(drawCursor, forKey: "drawCursor")
            link.send(["t": "cursor", "on": drawCursor])
            var text = "Puntatore disegnato: \(drawCursor ? "sì" : "no") · " +
                       "\(Int(frameSize.width))×\(Int(frameSize.height)) · \(fps) fps"
            if let ip = Link.wifiAddress() {
                text += "\nWi-Fi: \(ip) · codice \(link.pairingCode)"
            }
            showStatus(text, autoHide: true)
        default:
            break
        }
    }

    override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent?) {
        let active = activeTouches(event)
        if active.count == 1, var o = one, !o.cancelled {
            let p = active[0].location(in: view)
            o.last = p
            if !o.down && hypot(p.x - o.start.x, p.y - o.start.y) > dragThreshold {
                longPressTimer?.invalidate()
                send("down", at: o.start, button: "left")
                o.down = true
            }
            one = o
            send("move", at: p)
        } else if active.count == 2, let c0 = scrollCenter {
            let c = centroid(active)
            scrollCenter = c
            // scorrimento "naturale", come sull'iPad
            link.send(["t": "scroll", "dx": Int((c0.x - c.x) * 3), "dy": Int((c0.y - c.y) * 3)])
        }
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) {
        guard activeTouches(event).isEmpty else { return }
        longPressTimer?.invalidate()
        if let o = one, !o.cancelled {
            if o.down { send("up", at: o.last, button: "left") }
            else { send("click", at: o.last, button: "left") }
        }
        one = nil
        scrollCenter = nil
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) {
        cancelOneFinger()
        one = nil
        scrollCenter = nil
    }

    @objc private func hover(_ g: UIHoverGestureRecognizer) {
        if g.state == .began || g.state == .changed {
            send("move", at: g.location(in: view))
        }
    }

    func pointerInteraction(_ interaction: UIPointerInteraction,
                            styleFor region: UIPointerRegion) -> UIPointerStyle? {
        .hidden()  // resta visibile solo il puntatore del PC
    }

    // MARK: messaggi a schermo

    private func showStatus(_ text: String, autoHide: Bool) {
        statusLabel.text = text
        UIView.animate(withDuration: 0.2) { self.statusLabel.alpha = 1 }
        hideStatusWork?.cancel()
        guard autoHide else { return }
        let work = DispatchWorkItem { [weak self] in
            UIView.animate(withDuration: 0.6) { self?.statusLabel.alpha = 0 }
        }
        hideStatusWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 2, execute: work)
    }
}

/// UILabel con un po' di margine interno.
final class PaddedLabel: UILabel {
    private let inset = UIEdgeInsets(top: 8, left: 14, bottom: 8, right: 14)
    override func drawText(in rect: CGRect) { super.drawText(in: rect.inset(by: inset)) }
    override var intrinsicContentSize: CGSize {
        let s = super.intrinsicContentSize
        return CGSize(width: s.width + inset.left + inset.right, height: s.height + inset.top + inset.bottom)
    }
}
