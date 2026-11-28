package ua.radio345

import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.media.app.NotificationCompat.MediaStyle

/**
 * Служба переднього плану, поки слухаємо ефір: тримає процес, CPU й Wi-Fi у фоні та при
 * заблокованому екрані, показує медіасповіщення з кнопкою «Стоп». Сама нічого не грає —
 * усе робить [RadioModel].
 */
class RadioService : Service() {
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    @SuppressLint("WakelockTimeout")  // знімається в onDestroy, коли прослуховування зупинено
    override fun onCreate() {
        super.onCreate()
        val model = (application as RadioApp).model
        val type = if (Build.VERSION.SDK_INT >= 29) ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK else 0
        ServiceCompat.startForeground(this, NOTIFICATION_ID, build(this, model), type)
        wakeLock = getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "radio345:listen").apply { acquire() }
        @Suppress("DEPRECATION")
        val mode = if (Build.VERSION.SDK_INT >= 29) WifiManager.WIFI_MODE_FULL_LOW_LATENCY else WifiManager.WIFI_MODE_FULL_HIGH_PERF
        wifiLock = applicationContext.getSystemService(WifiManager::class.java)
            .createWifiLock(mode, "radio345:listen").apply { acquire() }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val model = (application as RadioApp).model
        if (intent?.action == ACTION_STOP) {
            model.stop()
            return START_NOT_STICKY
        }
        // Процес убили: без стану моделі відновлювати нічого.
        if (!model.isListening) stopSelf()
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        wakeLock?.takeIf { it.isHeld }?.release()
        wifiLock?.takeIf { it.isHeld }?.release()
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        super.onDestroy()
    }

    companion object {
        private const val NOTIFICATION_ID = 345
        private const val ACTION_STOP = "ua.radio345.STOP"

        fun build(context: Context, model: RadioModel): Notification {
            val open = PendingIntent.getActivity(context, 1, Intent(context, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP), PendingIntent.FLAG_IMMUTABLE)
            val stop = PendingIntent.getService(context, 2, Intent(context, RadioService::class.java)
                .setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE)
            return NotificationCompat.Builder(context, RadioModel.CHANNEL_LISTEN)
                .setSmallIcon(R.drawable.ic_radio)
                .setContentTitle(model.nowPlayingTitle)
                .setContentText(model.nowPlayingSubtitle)
                .setContentIntent(open)
                .addAction(android.R.drawable.ic_media_pause, "Стоп", stop)
                .setStyle(MediaStyle().setMediaSession(model.session.sessionToken).setShowActionsInCompactView(0))
                .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setSilent(true)
                .build()
        }

        /** Оновити текст сповіщення (нова фраза, канал, «Вдома»). */
        fun refresh(context: Context, model: RadioModel) {
            context.getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, build(context, model))
        }
    }
}
