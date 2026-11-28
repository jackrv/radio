package ua.radio345

import android.app.Application

/** Одна модель на процес: її бачать і екран, і служба прослуховування. */
class RadioApp : Application() {
    lateinit var model: RadioModel
        private set

    override fun onCreate() {
        super.onCreate()
        model = RadioModel(this)
    }
}
