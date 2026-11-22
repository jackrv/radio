import SwiftUI

@main
struct Radio345App: App {
    @State private var model = RadioModel()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environment(model)
                .preferredColorScheme(.dark)
        }
    }
}
