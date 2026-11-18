import AVFoundation
import ImageIO
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
    private var canvas: CGContext?          // schermo del PC ricostruito (solo su decodeQueue)
    private let h264 = H264Decoder()         // video dal PC (solo su decodeQueue)
    private let pointerLayer = CAShapeLayer()
    private let videoLayer = AVSampleBufferDisplayLayer()   // video H.264 (chip grafico)
    private var pointer: (x: CGFloat, y: CGFloat, visible: Bool, size: CGFloat)?
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

        // video: decodificato e mostrato direttamente dal chip grafico
        videoLayer.videoGravity = .resizeAspect
        videoLayer.backgroundColor = UIColor.black.cgColor
        videoLayer.isHidden = true
        view.layer.addSublayer(videoLayer)

        // puntatore disegnato qui: il PC manda solo la sua posizione
        pointerLayer.fillColor = UIColor.white.cgColor
        pointerLayer.strokeColor = UIColor.black.cgColor
        pointerLayer.lineJoin = .round
        view.layer.addSublayer(pointerLayer)

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
            // questa app sa ricomporre le zone cambiate e disegnare il puntatore
            self.link.send(["t": "caps", "patches": true, "cursor": true, "video": ["h264"]])
            self.link.send(["t": "cursor", "on": self.drawCursor])
            self.sendInfo()
        }
        link.onHello = { _ in }
        link.onFrame = { [weak self] data in self?.decode(data) }
        link.onPatches = { [weak self] data in self?.applyPatches(data) }
        link.onVideo = { [weak self] data in self?.applyVideo(data) }
        link.onControl = { [weak self] msg in self?.handleControl(msg) }
        showStatus("Avvio…", autoHide: false)
        link.start()
    }

    func appBecameActive() {
        link.ensureRunning()
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        sendInfo()
        updatePointer()
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        videoLayer.frame = view.bounds
        CATransaction.commit()
    }

    // MARK: zone cambiate e puntatore

    private func handleControl(_ m: [String: Any]) {
        switch m["t"] as? String {
        case "size":
            guard let w = m["w"] as? Int, let h = m["h"] as? Int, w > 0, h > 0 else { return }
            decodeQueue.async { self.makeCanvas(width: w, height: h) }
        case "cur":
            let x = (m["x"] as? Double) ?? 0, y = (m["y"] as? Double) ?? 0
            let s = (m["s"] as? Double) ?? 0.025
            pointer = (CGFloat(x), CGFloat(y), (m["v"] as? Bool) ?? false, CGFloat(s))
            updatePointer()
        default:
            break
        }
    }

    /// Solo su decodeQueue.
    private func makeCanvas(width: Int, height: Int) {
        if let c = canvas, c.width == width, c.height == height { return }
        canvas = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
                           bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                           bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
        canvas?.setFillColor(UIColor.black.cgColor)
        canvas?.fill(CGRect(x: 0, y: 0, width: width, height: height))
    }

    private func applyPatches(_ data: Data) {
        decodeQueue.async { [weak self] in
            guard let self else { return }
            let bytes = [UInt8](data)
            func u16(_ i: Int) -> Int { Int(bytes[i]) | Int(bytes[i + 1]) << 8 }
            func u32(_ i: Int) -> Int { u16(i) | u16(i + 2) << 16 }
            var image: CGImage?
            if bytes.count >= 3, bytes[0] == 0x50 {
                let n = u16(1)
                var off = 3
                for _ in 0..<n {
                    guard off + 8 <= bytes.count else { break }
                    let x = u16(off), y = u16(off + 2), len = u32(off + 4)
                    off += 8
                    guard off + len <= bytes.count else { break }
                    let jpeg = data.subdata(in: data.startIndex + off ..< data.startIndex + off + len)
                    off += len
                    guard let src = CGImageSourceCreateWithData(jpeg as CFData, nil),
                          let patch = CGImageSourceCreateImageAtIndex(src, 0, nil) else { continue }
                    if self.canvas == nil && x == 0 && y == 0 {
                        self.makeCanvas(width: patch.width, height: patch.height)
                    }
                    guard let c = self.canvas else { continue }
                    // CGContext ha l'origine in basso a sinistra
                    c.draw(patch, in: CGRect(x: x, y: c.height - y - patch.height,
                                             width: patch.width, height: patch.height))
                }
                image = self.canvas?.makeImage()
            }
            DispatchQueue.main.async {
                if let image {
                    self.imageView.image = UIImage(cgImage: image)
                    self.frameSize = CGSize(width: image.width, height: image.height)
                    self.updatePointer()
                    self.countFrame()
                    if !self.videoLayer.isHidden {
                        CATransaction.begin()
                        CATransaction.setDisableActions(true)
                        self.videoLayer.isHidden = true
                        self.videoLayer.flushAndRemoveImage()
                        CATransaction.commit()
                    }
                }
                self.link.send(["t": "ack"])  // pronto per l'aggiornamento successivo
            }
        }
    }

    /// Video H.264 dal PC: b"V" + 1 byte (fotogramma chiave) + flusso Annex B.
    private func applyVideo(_ data: Data) {
        decodeQueue.async { [weak self] in
            guard let self else { return }
            var sample: CMSampleBuffer?
            if data.count > 2, data.first == 0x56 {
                sample = self.h264.sampleBuffer(data.subdata(in: data.startIndex + 2 ..< data.endIndex))
            }
            DispatchQueue.main.async {
                if let sample {
                    if self.videoLayer.status == .failed { self.videoLayer.flush() }
                    self.videoLayer.enqueue(sample)
                    if self.videoLayer.isHidden {
                        CATransaction.begin()
                        CATransaction.setDisableActions(true)
                        self.videoLayer.isHidden = false
                        CATransaction.commit()
                    }
                    self.countFrame()
                }
                self.link.send(["t": "ack"])
            }
        }
    }

    private func updatePointer() {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        defer { CATransaction.commit() }
        guard let p = pointer, p.visible else { pointerLayer.path = nil; return }
        let r = imageRect
        let k = p.size * r.height / 20
        let origin = CGPoint(x: r.minX + p.x * r.width, y: r.minY + p.y * r.height)
        let pts: [(CGFloat, CGFloat)] = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]
        let path = UIBezierPath()
        for (i, pt) in pts.enumerated() {
            let q = CGPoint(x: origin.x + pt.0 * k, y: origin.y + pt.1 * k)
            if i == 0 { path.move(to: q) } else { path.addLine(to: q) }
        }
        path.close()
        pointerLayer.lineWidth = max(1, k * 0.9)
        pointerLayer.path = path.cgPath
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
