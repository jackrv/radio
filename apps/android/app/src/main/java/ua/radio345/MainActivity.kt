package ua.radio345

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.ui.graphics.toArgb

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(RadioColors.bg.toArgb()),
            navigationBarStyle = SystemBarStyle.dark(RadioColors.bg.toArgb()),
        )
        val model = (application as RadioApp).model
        setContent { RadioRoot(model) }
    }
}
