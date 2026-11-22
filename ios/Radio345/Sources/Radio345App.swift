import SwiftUI

@main
struct Radio345App: App {
    @State private var model = RadioModel()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environment(model)
                .preferredColorScheme(.dark)
                .onOpenURL { model.settings.apply(link: $0.absoluteString) }
        }
    }
}
