import SwiftUI

extension Color {
    static let radioBg = Color(red: 0.043, green: 0.067, blue: 0.102)
    static let radioSurface = Color(red: 0.071, green: 0.110, blue: 0.161)
    static let radioSurface2 = Color(red: 0.098, green: 0.153, blue: 0.216)
    static let radioBorder = Color(red: 0.153, green: 0.208, blue: 0.267)
    static let radioLime = Color(red: 0.722, green: 0.937, blue: 0.525)
    static let radioMuted = Color(red: 0.569, green: 0.627, blue: 0.694)
    static let radioOk = Color(red: 0.498, green: 0.847, blue: 0.643)
    static let radioWarn = Color(red: 0.941, green: 0.769, blue: 0.424)
    static let radioBad = Color(red: 1.0, green: 0.561, blue: 0.561)
    static let radioText = Color(red: 0.957, green: 0.969, blue: 0.984)
}

struct ContentView: View {
    @Environment(RadioModel.self) private var model
    @State private var showSettings = false
    @State private var page = 0

    var body: some View {
        ZStack {
            Color.radioBg.ignoresSafeArea()
            // Інтерфейс злегка світиться, коли в ефірі реально звучить мовлення (червоним — при передачі).
            RadialGradient(colors: [(model.transmitter.isTransmitting ? Color.radioBad : Color.radioLime)
                                        .opacity(0.28 * (model.transmitter.isTransmitting ? max(0.5, model.transmitter.level) : model.glow)),
                                    .clear],
                           center: .center, startRadius: 0, endRadius: 520)
                .ignoresSafeArea()
                .allowsHitTesting(false)
                .animation(.easeOut(duration: 0.25), value: model.glow)
                .animation(.easeOut(duration: 0.15), value: model.transmitter.level)
            if model.settings.isConfigured {
                TabView(selection: $page) {
                    RadioPage(showSettings: $showSettings).tag(0)
                    HistoryPage().tag(1)
                }
                .tabViewStyle(.page(indexDisplayMode: .always))
                .indexViewStyle(.page(backgroundDisplayMode: .never))
            } else {
                NavigationStack { SettingsView() }
            }
        }
        .sheet(isPresented: $showSettings) { NavigationStack { SettingsView(isSheet: true) } }
        .tint(.radioLime)
    }
}

// MARK: Сторінка рації

struct RadioPage: View {
    @Environment(RadioModel.self) private var model
    @Binding var showSettings: Bool

    var body: some View {
        VStack(spacing: 0) {
            TopBar(showSettings: $showSettings)
                .padding(.top, 8)
            Spacer(minLength: 24)
            ListenVisualizer()
            Spacer(minLength: 24)
            PTTButton()
                .padding(.bottom, 44)  // над крапками сторінок
        }
        .padding(.horizontal, 20)
    }
}

struct TopBar: View {
    @Environment(RadioModel.self) private var model
    @Binding var showSettings: Bool

    var body: some View {
        HStack(spacing: 10) {
            Menu {
                Picker("Канал", selection: Binding(get: { model.settings.channel }, set: model.switchChannel)) {
                    ForEach(StreamChannel.allCases) { channel in
                        Label(channel.title, systemImage: channel == .live ? "dot.radiowaves.left.and.right" : "text.badge.checkmark")
                            .tag(channel)
                    }
                }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: model.settings.channel == .live ? "dot.radiowaves.left.and.right" : "text.badge.checkmark")
                    Text(model.settings.channel.title).fontWeight(.semibold)
                    Image(systemName: "chevron.down").font(.caption2.weight(.bold)).foregroundStyle(Color.radioMuted)
                }
                .font(.subheadline)
                .foregroundStyle(Color.radioLime)
                .padding(.horizontal, 14)
                .frame(height: 40)
                .background(Color.radioSurface, in: Capsule())
            }
            Spacer()
            RoundIcon(systemName: model.atHome ? "house.fill" : "house",
                      active: model.atHome, label: model.atHome ? "Вдома, звук вимкнено" : "Вдома") {
                withAnimation(.snappy) { model.atHome.toggle() }
            }
            RoundIcon(systemName: "gearshape", active: false, label: "Налаштування") { showSettings = true }
        }
    }
}

struct RoundIcon: View {
    let systemName: String
    let active: Bool
    let label: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: systemName)
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(active ? Color.black : Color.radioMuted)
                .frame(width: 40, height: 40)
                .background(active ? Color.radioWarn : Color.radioSurface, in: Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }
}

/// Центр сторінки: смуги рівня того, що реально грає. Торкання — слухати / стоп.
struct ListenVisualizer: View {
    @Environment(RadioModel.self) private var model

    var body: some View {
        VStack(spacing: 18) {
            Button(action: model.toggle) {
                ZStack {
                    if model.isListening {
                        LevelBars(levels: model.levels, color: model.atHome ? .radioWarn : .radioLime)
                            .transition(.opacity)
                    } else {
                        VStack(spacing: 10) {
                            Image(systemName: "play.fill").font(.system(size: 34, weight: .bold))
                            Text("Слухати").font(.headline)
                        }
                        .foregroundStyle(Color.radioLime)
                        .transition(.opacity)
                    }
                }
                .frame(maxWidth: .infinity)
                .frame(height: 120)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(model.isListening ? "Зупинити прослуховування" : "Слухати ефір")

            VStack(spacing: 6) {
                HStack(spacing: 6) {
                    Circle().fill(statusColor).frame(width: 8, height: 8)
                    Text(statusText).font(.subheadline.weight(.medium))
                }
                .foregroundStyle(model.isSoundActive && model.isListening ? Color.radioText : Color.radioMuted)
                if let clip = model.clips.first {
                    Text(clip.text)
                        .font(.footnote)
                        .foregroundStyle(Color.radioMuted)
                        .lineLimit(2)
                        .multilineTextAlignment(.center)
                    Text(clip.date.formatted(date: .omitted, time: .shortened))
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(Color.radioMuted.opacity(0.7))
                }
            }
            .animation(.easeInOut(duration: 0.2), value: model.isSoundActive)
        }
        .animation(.easeInOut(duration: 0.25), value: model.isListening)
    }

    private var statusText: String {
        if !model.isListening {
            switch model.macReachable {
            case .some(false): return "\(model.settings.receiver.title) недоступний · перевірте Tailscale"
            case .some(true): return "\(model.settings.receiver.title) на зв'язку"
            case .none: return "Не слухаю"
            }
        }
        if case .retrying = model.state, !model.lastError.isEmpty {
            return "\(model.state.text) · \(model.lastError)"
        }
        guard model.state == .playing else { return model.state.text }
        if model.atHome { return model.isSoundActive ? "Передача · вдома без звуку" : "Вдома · звук вимкнено" }
        return model.isSoundActive ? "Передача" : "Тиша в ефірі"
    }

    private var statusColor: Color {
        switch model.state {
        case .playing:
            if model.atHome { return .radioWarn }
            return model.isSoundActive ? .radioLime : .radioOk
        case .connecting, .buffering: return .radioWarn
        case .retrying, .failed: return .radioBad
        case .idle: return model.macReachable == false ? .radioBad : .radioMuted
        }
    }
}

struct LevelBars: View {
    let levels: [Double]
    let color: Color

    var body: some View {
        HStack(alignment: .center, spacing: 4) {
            ForEach(levels.indices, id: \.self) { i in
                let level = levels[i]
                Capsule()
                    .fill(color.opacity(0.35 + 0.65 * level))
                    .frame(width: 5, height: max(4, 110 * level))
            }
        }
        .animation(.linear(duration: 0.07), value: levels)
    }
}

// MARK: PTT

/// Велика кругла кнопка передачі. У спокої «дихає»; під час передачі від неї розходяться хвилі,
/// а кругові смуги й ядро рухаються в такт голосу.
struct PTTButton: View {
    @Environment(RadioModel.self) private var model
    @State private var pressed = false

    private var available: Bool { model.transmitter.isAvailable }
    private var live: Bool { pressed && model.transmitter.isTransmitting }

    var body: some View {
        VStack(spacing: 10) {
            TimelineView(.animation(minimumInterval: 1 / 60, paused: !available)) { context in
                let t = context.date.timeIntervalSinceReferenceDate
                let level = model.transmitter.level
                ZStack {
                    if live {
                        ForEach(0..<4, id: \.self) { i in
                            let phase = (t * 0.9 + Double(i) / 4).truncatingRemainder(dividingBy: 1)
                            Circle()
                                .stroke(Color.radioBad.opacity((1 - phase) * (0.25 + 0.6 * level)),
                                        lineWidth: 2 + 8 * (1 - phase))
                                .frame(width: 176, height: 176)
                                .scaleEffect(1 + phase * (0.55 + 0.5 * level))
                        }
                        RadialBars(levels: model.transmitter.levels, rotation: t * 0.35)
                            .frame(width: 250, height: 250)
                    } else if available {
                        let breath = (sin(t * 1.7) + 1) / 2
                        Circle()
                            .stroke(Color.radioLime.opacity(0.08 + 0.14 * breath), lineWidth: 10)
                            .frame(width: 200 + 10 * breath, height: 200 + 10 * breath)
                        Circle()
                            .stroke(Color.radioLime.opacity(0.05 + 0.06 * (1 - breath)), lineWidth: 1)
                            .frame(width: 232 + 14 * breath, height: 232 + 14 * breath)
                    }

                    Circle()
                        .fill(RadialGradient(colors: live ? [Color(red: 1, green: 0.45, blue: 0.42), Color(red: 0.75, green: 0.18, blue: 0.2)]
                                                          : [Color.radioSurface2, Color.radioSurface],
                                             center: .topLeading, startRadius: 10, endRadius: 220))
                        .overlay(Circle().stroke(live ? Color.white.opacity(0.35)
                                                      : (available ? Color.radioLime.opacity(0.6) : Color.radioBorder),
                                                 lineWidth: 1.5))
                        .shadow(color: (live ? Color.radioBad : Color.radioLime).opacity(live ? 0.55 * (0.4 + level) : 0.12),
                                radius: live ? 30 : 18)
                        .frame(width: 176, height: 176)
                        .scaleEffect(live ? 0.95 + 0.1 * level : (pressed ? 0.94 : 1))

                    VStack(spacing: 6) {
                        Image(systemName: live ? "waveform" : "mic.fill")
                            .font(.system(size: 40, weight: .semibold))
                            .symbolEffect(.variableColor.iterative, isActive: live)
                        Text(live ? "Передаю" : "PTT")
                            .font(.system(size: 20, weight: .bold, design: .rounded))
                    }
                    .foregroundStyle(live ? Color.white : (available ? Color.radioLime : Color.radioMuted.opacity(0.5)))
                }
                .frame(width: 290, height: 290)
            }
            .overlay {
                // Дотик через UIKit: миттєво (без затримки сторінок) і без свайпу, поки кнопку тримають.
                PressArea {
                    guard available, !pressed else { return }
                    pressed = true
                    Haptics.press()
                    Task { await model.beginTransmit() }
                } onRelease: {
                    guard pressed else { return }
                    pressed = false
                    Haptics.release()
                    Task { await model.endTransmit() }
                }
                .frame(width: 196, height: 196)
            }
            .accessibilityLabel("Передача")
            .accessibilityHint(available ? "Утримуйте, щоб говорити" : model.transmitter.unavailableReason)
            .accessibilityAddTraits(.isButton)

            Text(model.transmitter.lastError ?? (available ? "Утримуйте, щоб говорити" : model.transmitter.unavailableReason))
                .font(.caption)
                .foregroundStyle(Color.radioMuted.opacity(0.8))
        }
        .frame(maxWidth: .infinity)
    }
}

enum Haptics {
    private static let heavy = UIImpactFeedbackGenerator(style: .heavy)
    private static let light = UIImpactFeedbackGenerator(style: .light)
    static func press() { heavy.impactOccurred(); light.prepare() }
    static func release() { light.impactOccurred(); heavy.prepare() }
}

/// Кругла зона натискання: торкання доходить одразу (у контейнера сторінок вимикається
/// delaysContentTouches), а свайп сторінок заблоковано, доки палець на кнопці.
struct PressArea: UIViewRepresentable {
    var onPress: () -> Void
    var onRelease: () -> Void

    func makeUIView(context: Context) -> PressView {
        let view = PressView()
        view.backgroundColor = .clear
        view.isMultipleTouchEnabled = false
        return view
    }

    func updateUIView(_ view: PressView, context: Context) {
        view.onPress = onPress
        view.onRelease = onRelease
    }
}

final class PressView: UIView {
    var onPress: (() -> Void)?
    var onRelease: (() -> Void)?
    private weak var pager: UIScrollView?
    private var pressing = false

    override func didMoveToWindow() {
        super.didMoveToWindow()
        var view = superview
        while let current = view {
            if let scroll = current as? UIScrollView {
                scroll.delaysContentTouches = false
                pager = scroll
                break
            }
            view = current.superview
        }
    }

    override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {
        let radius = min(bounds.width, bounds.height) / 2
        return hypot(point.x - bounds.midX, point.y - bounds.midY) <= radius
    }

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
        guard !pressing else { return }
        pressing = true
        pager?.isScrollEnabled = false
        onPress?()
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) { finish() }
    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) { finish() }

    private func finish() {
        guard pressing else { return }
        pressing = false
        pager?.isScrollEnabled = true
        onRelease?()
    }
}

/// Смуги по колу, довжина — рівень голосу; повільно обертаються.
struct RadialBars: View {
    let levels: [Double]
    let rotation: Double

    var body: some View {
        Canvas { context, size in
            let center = CGPoint(x: size.width / 2, y: size.height / 2)
            let inner = 100.0
            let count = 64
            for i in 0..<count {
                // Дзеркально: найсвіжіші рівні — вгорі й унизу одночасно.
                let mirrored = i < count / 2 ? i : count - 1 - i
                let index = levels.count - 1 - mirrored * levels.count / (count / 2) % levels.count
                let level = levels[max(0, min(levels.count - 1, index))]
                let angle = Double(i) / Double(count) * 2 * .pi + rotation
                let length = 3 + 24 * level
                var path = Path()
                path.move(to: CGPoint(x: center.x + cos(angle) * inner, y: center.y + sin(angle) * inner))
                path.addLine(to: CGPoint(x: center.x + cos(angle) * (inner + length),
                                         y: center.y + sin(angle) * (inner + length)))
                context.stroke(path, with: .color(Color.white.opacity(0.35 + 0.65 * level)),
                               style: StrokeStyle(lineWidth: 3, lineCap: .round))
            }
        }
    }
}

// MARK: Сторінка історії

struct HistoryPage: View {
    @Environment(RadioModel.self) private var model

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Історія").font(.system(size: 28, weight: .bold, design: .rounded))
                .padding(.horizontal, 20).padding(.top, 14).padding(.bottom, 10)
            if model.clips.isEmpty {
                Spacer()
                Text("Фраз ще немає").foregroundStyle(Color.radioMuted).frame(maxWidth: .infinity)
                Spacer()
            } else {
                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(model.clips) { clip in
                            HistoryRow(clip: clip)
                            Divider().overlay(Color.radioBorder).padding(.leading, 20)
                        }
                    }
                    .padding(.bottom, 60)
                }
                .scrollIndicators(.hidden)
                .refreshable { await model.refreshNow() }
            }
        }
    }
}

struct HistoryRow: View {
    let clip: Clip

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            VStack(alignment: .leading, spacing: 0) {
                Text(clip.date.formatted(.dateTime.hour(.twoDigits(amPM: .omitted)).minute(.twoDigits)))
                    .font(.system(size: 26, weight: .bold, design: .rounded).monospacedDigit())
                    .foregroundStyle(Color.radioLime)
                Text(clip.date.formatted(.dateTime.second(.twoDigits)) + " с · " + String(format: "%.1f", clip.duration_s) + " с")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(Color.radioMuted)
            }
            .frame(width: 78, alignment: .leading)
            VStack(alignment: .leading, spacing: 3) {
                Text(clip.text).font(.subheadline).frame(maxWidth: .infinity, alignment: .leading)
                if !clip.header.isEmpty, clip.header != clip.text {
                    Text(clip.header.replacingOccurrences(of: "\n", with: " · "))
                        .font(.caption2).foregroundStyle(Color.radioMuted).lineLimit(1)
                }
            }
            .padding(.top, 4)
            ReplayButton(clip: clip, compact: true).padding(.top, 2)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
    }
}

struct ReplayButton: View {
    @Environment(RadioModel.self) private var model
    let clip: Clip
    var compact = false

    var body: some View {
        Button { model.replay(clip) } label: {
            Image(systemName: model.replayingId == clip.id ? "speaker.wave.2.fill" : "play.fill")
                .font(compact ? .caption : .subheadline)
                .foregroundStyle(Color.radioLime)
                .frame(width: compact ? 30 : 38, height: compact ? 30 : 38)
                .background(Color.radioSurface2, in: Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Повторити фразу")
    }
}

// MARK: Налаштування

struct SettingsView: View {
    @Environment(RadioModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    var isSheet = false

    var body: some View {
        @Bindable var settings = model.settings
        @Bindable var bindable = model
        Form {
            if isSheet {
                Section {
                    Toggle("Відсіювати сигнали в «Наживо»", isOn: $settings.filterSignals)
                        .onChange(of: settings.filterSignals) { model.settingsChanged() }
                } footer: {
                    Text("Звуки сповіщень телефонів не граються. Початок кожної передачі звучить на ~2–3 с пізніше: приймач чекає, щоб відрізнити сигнал від мовлення.")
                }
                Section {
                    Toggle("Вмикати «Вдома» автоматично", isOn: $settings.autoHome)
                } footer: {
                    Text("Коли iPhone повертається в мережу приймача, звук вимикається, щоб його мікрофон не чув телефон. Режим знімається сам, щойно iPhone виходить із цієї мережі.")
                }
                Section {
                    Toggle("Сповіщення з текстом фрази", isOn: $bindable.notifyInBackground)
                } footer: {
                    Text("Працює, поки застосунок слухає ефір у фоні.")
                }
            }

            Section {
                Picker("Приймач", selection: $settings.receiver) {
                    ForEach(Receiver.allCases) { Text($0.title).tag($0) }
                }
                .pickerStyle(.segmented)
                .onChange(of: settings.receiver) { model.receiverChanged() }
                TextField("Адреса Mac (ім'я або 100.x.y.z)", text: $settings.macHost)
                    .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                    .onSubmit { if settings.receiver == .mac { model.receiverChanged() } }
                TextField("Адреса Pi (напр. radio345)", text: $settings.piHost)
                    .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                    .onSubmit { if settings.receiver == .pi { model.receiverChanged() } }
            } header: {
                Text("Підключення")
            } footer: {
                Text("Ім'я пристрою в Tailscale або адреса 100.x.y.z; порт типово 8346. Токен не потрібен: приймач пускає лише пристрої вашого облікового запису Tailscale.")
            }
        }
        .scrollContentBackground(.hidden)
        .background(Color.radioBg.ignoresSafeArea())
        .navigationTitle(isSheet ? "Налаштування" : "Підключення")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if isSheet { Button("Готово") { dismiss() } }
        }
    }
}
