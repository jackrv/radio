@file:OptIn(ExperimentalMaterial3Api::class)

package ua.radio345

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.view.HapticFeedbackConstants
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.Crossfade
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.GraphicEq
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.PlaylistAddCheck
import androidx.compose.material.icons.filled.Sensors
import androidx.compose.material.icons.filled.VolumeUp
import androidx.compose.material.icons.outlined.Home
import androidx.compose.material.icons.outlined.Settings
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.draw.scale
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.sin

object RadioColors {
    val bg = Color(0xFF0B111A)
    val surface = Color(0xFF121C29)
    val surface2 = Color(0xFF192737)
    val border = Color(0xFF273544)
    val lime = Color(0xFFB8EF86)
    val muted = Color(0xFF91A0B1)
    val ok = Color(0xFF7FD8A4)
    val warn = Color(0xFFF0C46C)
    val bad = Color(0xFFFF8F8F)
    val text = Color(0xFFF4F7FB)
}

private val scheme = darkColorScheme(
    primary = RadioColors.lime, onPrimary = Color.Black,
    background = RadioColors.bg, surface = RadioColors.surface, surfaceVariant = RadioColors.surface2,
    onBackground = RadioColors.text, onSurface = RadioColors.text, onSurfaceVariant = RadioColors.muted,
    outline = RadioColors.border, secondaryContainer = RadioColors.surface2, onSecondaryContainer = RadioColors.lime,
)

@Composable
fun RadioRoot(model: RadioModel) {
    MaterialTheme(colorScheme = scheme) {
        var showSettings by remember { mutableStateOf(false) }
        val transmitting = model.transmitter.isTransmitting
        // Інтерфейс злегка світиться, коли в ефірі реально звучить мовлення (червоним — при передачі).
        val glow by animateFloatAsState(
            (if (transmitting) max(0.5, model.transmitter.level) else model.glow).toFloat(),
            tween(if (transmitting) 150 else 250), label = "glow")
        val glowColor = if (transmitting) RadioColors.bad else RadioColors.lime
        Box(
            Modifier
                .fillMaxSize()
                .background(RadioColors.bg)
                .drawBehind {
                    drawRect(Brush.radialGradient(listOf(glowColor.copy(alpha = 0.28f * glow), Color.Transparent),
                        center = center, radius = 520.dp.toPx()))
                }
        ) {
            if (model.settings.isConfigured) {
                RadioPager(model) { showSettings = true }
            } else {
                SettingsScreen(model, isSheet = false, onClose = {})
            }
            AnimatedVisibility(showSettings, enter = slideInVertically { it } + fadeIn(),
                exit = slideOutVertically { it } + fadeOut()) {
                BackHandler { showSettings = false }
                Box(Modifier.fillMaxSize().background(RadioColors.bg)) {
                    SettingsScreen(model, isSheet = true, onClose = { showSettings = false })
                }
            }
        }
    }
}

@Composable
private fun RadioPager(model: RadioModel, openSettings: () -> Unit) {
    val pager = rememberPagerState { 2 }
    Box(Modifier.fillMaxSize().safeDrawingPadding()) {
        HorizontalPager(pager, Modifier.fillMaxSize()) { page ->
            if (page == 0) RadioPage(model, openSettings) else HistoryPage(model)
        }
        Row(Modifier.align(Alignment.BottomCenter).padding(bottom = 14.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            repeat(2) {
                Box(Modifier.size(7.dp).clip(CircleShape)
                    .background(if (pager.currentPage == it) RadioColors.text else RadioColors.muted.copy(alpha = 0.4f)))
            }
        }
    }
}

// Сторінка рації

@Composable
private fun RadioPage(model: RadioModel, openSettings: () -> Unit) {
    Column(Modifier.fillMaxSize().padding(horizontal = 20.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        TopBar(model, openSettings)
        Spacer(Modifier.weight(1f))
        ListenVisualizer(model)
        Spacer(Modifier.weight(1f))
        PttButton(model)
        Spacer(Modifier.height(44.dp))  // над крапками сторінок
    }
}

@Composable
private fun TopBar(model: RadioModel, openSettings: () -> Unit) {
    var menu by remember { mutableStateOf(false) }
    Row(Modifier.fillMaxWidth().padding(top = 8.dp), verticalAlignment = Alignment.CenterVertically) {
        Box {
            Row(
                Modifier.height(40.dp).clip(CircleShape).background(RadioColors.surface)
                    .clickable { menu = true }.padding(horizontal = 14.dp),
                verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                Icon(channelIcon(model.settings.channel), null, tint = RadioColors.lime, modifier = Modifier.size(18.dp))
                Text(model.settings.channel.title, color = RadioColors.lime, fontWeight = FontWeight.SemiBold, fontSize = 15.sp)
                Icon(Icons.Filled.KeyboardArrowDown, null, tint = RadioColors.muted, modifier = Modifier.size(16.dp))
            }
            DropdownMenu(menu, { menu = false }, containerColor = RadioColors.surface2) {
                StreamChannel.entries.forEach { channel ->
                    DropdownMenuItem(
                        text = {
                            Column {
                                Text(channel.title, fontWeight = FontWeight.SemiBold)
                                Text(channel.hint, fontSize = 12.sp, color = RadioColors.muted)
                            }
                        },
                        leadingIcon = { Icon(channelIcon(channel), null, tint = RadioColors.lime) },
                        onClick = { menu = false; model.switchChannel(channel) },
                    )
                }
            }
        }
        Spacer(Modifier.weight(1f))
        RoundIcon(if (model.atHome) Icons.Filled.Home else Icons.Outlined.Home, model.atHome,
            if (model.atHome) "Вдома, звук вимкнено" else "Вдома") { model.atHome = !model.atHome }
        Spacer(Modifier.width(10.dp))
        RoundIcon(Icons.Outlined.Settings, false, "Налаштування", openSettings)
    }
}

private fun channelIcon(channel: StreamChannel): ImageVector =
    if (channel == StreamChannel.LIVE) Icons.Filled.Sensors else Icons.Filled.PlaylistAddCheck

@Composable
private fun RoundIcon(icon: ImageVector, active: Boolean, label: String, onClick: () -> Unit) {
    Box(
        Modifier.size(40.dp).clip(CircleShape).background(if (active) RadioColors.warn else RadioColors.surface)
            .clickable(onClick = onClick).semantics { contentDescription = label },
        contentAlignment = Alignment.Center,
    ) {
        Icon(icon, null, tint = if (active) Color.Black else RadioColors.muted, modifier = Modifier.size(20.dp))
    }
}

/** Центр сторінки: смуги рівня того, що реально грає. Торкання — слухати / стоп. */
@Composable
private fun ListenVisualizer(model: RadioModel) {
    val context = LocalContext.current
    val askNotifications = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {}
    Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(18.dp)) {
        Box(
            Modifier.fillMaxWidth().height(120.dp)
                .clickable(remember { MutableInteractionSource() }, null) {
                    // Без дозволу медіасповіщення на Android 13+ не видно (служба однаково працює).
                    if (!model.isListening && Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(
                            context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                        askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
                    }
                    model.toggle()
                }
                .semantics { contentDescription = if (model.isListening) "Зупинити прослуховування" else "Слухати ефір" },
            contentAlignment = Alignment.Center,
        ) {
            Crossfade(model.isListening, label = "listen") { listening ->
                if (listening) {
                    LevelBars(model.levels, if (model.atHome) RadioColors.warn else RadioColors.lime)
                } else {
                    Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(10.dp)) {
                        Icon(Icons.Filled.PlayArrow, null, tint = RadioColors.lime, modifier = Modifier.size(44.dp))
                        Text("Слухати", color = RadioColors.lime, fontWeight = FontWeight.SemiBold, fontSize = 17.sp)
                    }
                }
            }
        }
        Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                Box(Modifier.size(8.dp).clip(CircleShape).background(statusColor(model)))
                Text(statusText(model), fontSize = 15.sp, fontWeight = FontWeight.Medium, textAlign = TextAlign.Center,
                    color = if (model.isSoundActive && model.isListening) RadioColors.text else RadioColors.muted)
            }
            model.clips.firstOrNull()?.let { clip ->
                Text(clip.text, fontSize = 13.sp, color = RadioColors.muted, maxLines = 2, overflow = TextOverflow.Ellipsis,
                    textAlign = TextAlign.Center)
                Text(timeFormat.format(Date(clip.atMillis)), fontSize = 11.sp, fontFamily = FontFamily.Monospace,
                    color = RadioColors.muted.copy(alpha = 0.7f))
            }
        }
    }
}

private val timeFormat = SimpleDateFormat("HH:mm", Locale.getDefault())
private val secondsFormat = SimpleDateFormat("ss", Locale.getDefault())

private fun statusText(model: RadioModel): String {
    if (!model.isListening) {
        return if (model.airState == SourceState.UNKNOWN) "Не слухаю" else model.airState.text(model.settings.receiver.title)
    }
    val state = model.state
    if (state is PlayerState.Retrying && model.lastError.isNotEmpty()) return "${state.text} · ${model.lastError}"
    if (state != PlayerState.Playing) return state.text
    if (model.atHome) return if (model.isSoundActive) "Передача · вдома без звуку" else "Вдома · звук вимкнено"
    return if (model.isSoundActive) "Передача" else "Тиша в ефірі"
}

private fun statusColor(model: RadioModel): Color = when (model.state) {
    PlayerState.Playing -> when {
        model.atHome -> RadioColors.warn
        model.isSoundActive -> RadioColors.lime
        else -> RadioColors.ok
    }
    PlayerState.Connecting -> RadioColors.warn
    is PlayerState.Retrying, is PlayerState.Failed -> RadioColors.bad
    PlayerState.Idle -> if (model.airState == SourceState.OFFLINE) RadioColors.bad else RadioColors.muted
}

@Composable
private fun LevelBars(levels: List<Double>, color: Color) {
    Row(horizontalArrangement = Arrangement.spacedBy(4.dp), verticalAlignment = Alignment.CenterVertically) {
        levels.forEach { level ->
            val height by animateFloatAsState(max(4.0, 110 * level).toFloat(), tween(70), label = "bar")
            Box(Modifier.width(5.dp).height(height.dp).clip(CircleShape)
                .background(color.copy(alpha = (0.35 + 0.65 * level).toFloat())))
        }
    }
}

// PTT

/**
 * Велика кругла кнопка передачі. У спокої «дихає»; під час передачі від неї розходяться хвилі,
 * а кругові смуги й ядро рухаються в такт голосу. Дотик забирає всі події — сторінки не гортаються,
 * поки кнопку тримають.
 */
@Composable
private fun PttButton(model: RadioModel) {
    val context = LocalContext.current
    val view = LocalView.current
    val tx = model.transmitter
    val available = tx.isAvailable
    var pressed by remember { mutableStateOf(false) }
    var permissionNote by remember { mutableStateOf<String?>(null) }
    val askMic = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        // Перший раз: системний запит; передача почнеться з наступного натискання.
        permissionNote = if (granted) null else "Немає доступу до мікрофона: Налаштування → Застосунки → Рація 345"
    }
    val live = pressed && tx.isTransmitting
    var time by remember { mutableFloatStateOf(0f) }
    LaunchedEffect(available) {
        while (available) withFrameNanos { time = it / 1_000_000_000f }
    }
    val pressScale by animateFloatAsState(if (pressed && !live) 0.94f else 1f, tween(120), label = "press")

    Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Box(Modifier.size(290.dp), contentAlignment = Alignment.Center) {
            Canvas(Modifier.fillMaxSize()) {
                val level = tx.level.toFloat()
                val core = 88.dp.toPx()
                if (live) {
                    repeat(4) { i ->
                        val phase = ((time * 0.9f + i / 4f) % 1f)
                        drawCircle(RadioColors.bad.copy(alpha = (1 - phase) * (0.25f + 0.6f * level)),
                            radius = core * (1 + phase * (0.55f + 0.5f * level)),
                            style = Stroke((2 + 8 * (1 - phase)).dp.toPx()))
                    }
                    radialBars(tx.levels, time * 0.35f)
                } else if (available) {
                    val breath = (sin(time * 1.7f) + 1) / 2
                    drawCircle(RadioColors.lime.copy(alpha = 0.08f + 0.14f * breath),
                        radius = (100 + 5 * breath).dp.toPx(), style = Stroke(10.dp.toPx()))
                    drawCircle(RadioColors.lime.copy(alpha = 0.05f + 0.06f * (1 - breath)),
                        radius = (116 + 7 * breath).dp.toPx(), style = Stroke(1.dp.toPx()))
                }
            }
            val coreScale = if (live) 0.95f + 0.1f * tx.level.toFloat() else pressScale
            Box(
                Modifier.size(176.dp).scale(coreScale)
                    .shadow(if (live) 30.dp else 18.dp, CircleShape,
                        ambientColor = if (live) RadioColors.bad else RadioColors.lime,
                        spotColor = if (live) RadioColors.bad else RadioColors.lime)
                    .clip(CircleShape)
                    .background(Brush.radialGradient(
                        if (live) listOf(Color(0xFFFF736B), Color(0xFFBF2E33)) else listOf(RadioColors.surface2, RadioColors.surface),
                        center = Offset.Zero, radius = 600f))
                    .border(1.5.dp, if (live) Color.White.copy(alpha = 0.35f)
                        else if (available) RadioColors.lime.copy(alpha = 0.6f) else RadioColors.border, CircleShape)
                    .pointerInput(available) {
                        awaitEachGesture {
                            val down = awaitFirstDown(requireUnconsumed = false)
                            val r = size.width / 2f
                            if (!available || hypot(down.position.x - r, down.position.y - r) > r) return@awaitEachGesture
                            down.consume()
                            val granted = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) ==
                                PackageManager.PERMISSION_GRANTED
                            if (!granted) {
                                askMic.launch(Manifest.permission.RECORD_AUDIO)
                                return@awaitEachGesture
                            }
                            permissionNote = null
                            pressed = true
                            view.performHapticFeedback(HapticFeedbackConstants.LONG_PRESS)
                            model.beginTransmit()
                            try {
                                do {
                                    val event = awaitPointerEvent()
                                    event.changes.forEach { it.consume() }
                                } while (event.changes.any { it.pressed })
                            } finally {
                                pressed = false
                                view.performHapticFeedback(HapticFeedbackConstants.VIRTUAL_KEY)
                                model.endTransmit()
                            }
                        }
                    }
                    .semantics { contentDescription = "Передача" },
                contentAlignment = Alignment.Center,
            ) {
                Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    val tint = if (live) Color.White else if (available) RadioColors.lime else RadioColors.muted.copy(alpha = 0.5f)
                    Icon(if (live) Icons.Filled.GraphicEq else Icons.Filled.Mic, null, tint = tint, modifier = Modifier.size(44.dp))
                    Text(if (live) "Передаю" else "PTT", color = tint, fontSize = 20.sp, fontWeight = FontWeight.Bold)
                }
            }
        }
        Text(permissionNote ?: tx.lastError ?: if (available) "Утримуйте, щоб говорити" else tx.unavailableReason,
            fontSize = 12.sp, color = RadioColors.muted.copy(alpha = 0.8f), textAlign = TextAlign.Center)
    }
}

/** Смуги по колу, довжина — рівень голосу; повільно обертаються. Дзеркально: найсвіжіші рівні — вгорі й унизу. */
private fun androidx.compose.ui.graphics.drawscope.DrawScope.radialBars(levels: List<Double>, rotation: Float) {
    val inner = 100.dp.toPx()
    val count = 64
    for (i in 0 until count) {
        val mirrored = if (i < count / 2) i else count - 1 - i
        val index = (levels.size - 1 - mirrored * levels.size / (count / 2) % levels.size).coerceIn(0, levels.size - 1)
        val level = levels[index].toFloat()
        val angle = i.toFloat() / count * 2 * PI.toFloat() + rotation
        val length = (3 + 24 * level).dp.toPx()
        val dir = Offset(cos(angle), sin(angle))
        drawLine(Color.White.copy(alpha = 0.35f + 0.65f * level), center + dir * inner, center + dir * (inner + length),
            strokeWidth = 3.dp.toPx(), cap = StrokeCap.Round)
    }
}

// Сторінка історії

@Composable
private fun HistoryPage(model: RadioModel) {
    val scope = rememberCoroutineScope()
    var refreshing by remember { mutableStateOf(false) }
    Column(Modifier.fillMaxSize()) {
        Text("Історія", fontSize = 28.sp, fontWeight = FontWeight.Bold, color = RadioColors.text,
            modifier = Modifier.padding(start = 20.dp, end = 20.dp, top = 14.dp, bottom = 10.dp))
        if (model.textState != SourceState.OK && model.textState != SourceState.UNKNOWN) {
            Text(model.textState.text("Тексти, ${model.settings.textReceiver.title}") +
                if (model.clips.isEmpty()) "" else " · показано збережене",
                fontSize = 13.sp, color = RadioColors.warn, modifier = Modifier.padding(start = 20.dp, end = 20.dp, bottom = 8.dp))
        }
        PullToRefreshBox(refreshing, onRefresh = {
            scope.launch { refreshing = true; model.refreshNow(); refreshing = false }
        }, modifier = Modifier.fillMaxSize()) {
            if (model.clips.isEmpty()) {
                Box(Modifier.fillMaxSize().verticalScroll(rememberScrollState()), contentAlignment = Alignment.Center) {
                    Text("Фраз ще немає", color = RadioColors.muted, modifier = Modifier.padding(top = 200.dp))
                }
            } else {
                LazyColumn(Modifier.fillMaxSize(), contentPadding = androidx.compose.foundation.layout.PaddingValues(bottom = 60.dp)) {
                    items(model.clips, key = { it.id }) { clip ->
                        HistoryRow(model, clip)
                        HorizontalDivider(Modifier.padding(start = 20.dp), color = RadioColors.border)
                    }
                }
            }
        }
    }
}

@Composable
private fun HistoryRow(model: RadioModel, clip: Clip) {
    val date = Date(clip.atMillis)
    Row(Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 12.dp), horizontalArrangement = Arrangement.spacedBy(14.dp)) {
        Column(Modifier.width(78.dp)) {
            Text(timeFormat.format(date), fontSize = 26.sp, fontWeight = FontWeight.Bold, color = RadioColors.lime,
                fontFamily = FontFamily.Monospace, letterSpacing = (-1).sp)
            Text("${secondsFormat.format(date)} с · ${String.format(Locale.US, "%.1f", clip.durationS)} с",
                fontSize = 11.sp, color = RadioColors.muted, fontFamily = FontFamily.Monospace)
        }
        Column(Modifier.weight(1f).padding(top = 4.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
            Text(clip.text, fontSize = 15.sp, color = RadioColors.text)
            if (clip.header.isNotEmpty() && clip.header != clip.text) {
                Text(clip.header.replace("\n", " · "), fontSize = 11.sp, color = RadioColors.muted, maxLines = 1,
                    overflow = TextOverflow.Ellipsis)
            }
        }
        val enabled = model.canReplay(clip)
        Box(
            Modifier.padding(top = 2.dp).size(30.dp).clip(CircleShape).background(RadioColors.surface2)
                .clickable(enabled = enabled) { model.replay(clip) }
                .semantics { contentDescription = "Повторити фразу" },
            contentAlignment = Alignment.Center,
        ) {
            Icon(if (model.replayingId == clip.id) Icons.Filled.VolumeUp else Icons.Filled.PlayArrow, null,
                tint = RadioColors.lime.copy(alpha = if (enabled) 1f else 0.35f), modifier = Modifier.size(16.dp))
        }
    }
}

// Налаштування

@Composable
private fun SettingsScreen(model: RadioModel, isSheet: Boolean, onClose: () -> Unit) {
    val settings = model.settings
    val focus = LocalFocusManager.current
    var macHost by remember { mutableStateOf(settings.macHost) }
    var piHost by remember { mutableStateOf(settings.piHost) }
    val askNotifications = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        model.notifyInBackground = granted
    }

    /** Адреса застосовується, коли введення завершено, а не на кожну літеру. */
    fun commitHosts() {
        if (macHost.trim() != settings.macHost) { settings.macHost = macHost.trim(); model.hostChanged(Receiver.MAC) }
        if (piHost.trim() != settings.piHost) { settings.piHost = piHost.trim(); model.hostChanged(Receiver.PI) }
    }

    fun close() { focus.clearFocus(); commitHosts(); onClose() }
    if (isSheet) BackHandler { close() }

    Column(Modifier.fillMaxSize().safeDrawingPadding()) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(if (isSheet) "Налаштування" else "Підключення", fontSize = 20.sp, fontWeight = FontWeight.SemiBold,
                color = RadioColors.text, modifier = Modifier.padding(start = 12.dp).weight(1f))
            TextButton(onClick = ::close) { Text("Готово", fontWeight = FontWeight.SemiBold) }
        }
        Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(18.dp)) {
            if (isSheet) {
                Section(footer = "Звуки сповіщень телефонів не граються. Початок кожної передачі звучить на ~2–3 с пізніше: приймач чекає, щоб відрізнити сигнал від мовлення.") {
                    ToggleRow("Відсіювати сигнали в «Наживо»", settings.filterSignals) {
                        settings.filterSignals = it; model.settingsChanged()
                    }
                }
                Section(footer = "Коли телефон повертається в мережу приймача, звук вимикається, щоб його мікрофон не чув телефон. Режим знімається сам, щойно телефон виходить із цієї мережі.") {
                    ToggleRow("Вмикати «Вдома» автоматично", settings.autoHome) { settings.autoHome = it }
                }
                Section(footer = "Працює, поки застосунок слухає ефір у фоні.") {
                    ToggleRow("Сповіщення з текстом фрази", model.notifyInBackground) { on ->
                        if (on && !model.canNotify() && Build.VERSION.SDK_INT >= 33) {
                            askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
                        } else model.notifyInBackground = on
                    }
                }
            }
            Section(header = "Підключення",
                footer = "«Наживо» й передача йдуть з обраного вузла ефіру; тексти, «Відібрані» й повтор фраз — з вузла текстів. Збій одного не зупиняє інший. Ім'я пристрою в Tailscale або адреса 100.x.y.z; порт типово 8346. Токен не потрібен: приймач пускає лише пристрої вашого облікового запису Tailscale.") {
                Label("Ефір і передача")
                Segments(Receiver.entries, settings.receiver, { it.title }) {
                    if (it != settings.receiver) { commitHosts(); settings.receiver = it; model.receiverChanged() }
                }
                HostField(macHost, "Адреса Mac (ім'я або 100.x.y.z)", { macHost = it }) { focus.clearFocus(); commitHosts() }
                HostField(piHost, "Адреса Pi (напр. radio345)", { piHost = it }) { focus.clearFocus(); commitHosts() }
                Label("Тексти")
                Segments(TextSource.entries, settings.textSource, { it.title }) {
                    if (it != settings.textSource) { settings.textSource = it; model.textSourceChanged() }
                }
            }
        }
    }
}

@Composable
private fun Section(header: String? = null, footer: String? = null, content: @Composable () -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        header?.let { Text(it.uppercase(), fontSize = 12.sp, color = RadioColors.muted, modifier = Modifier.padding(start = 4.dp)) }
        Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp)).background(RadioColors.surface).padding(14.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)) { content() }
        footer?.let { Text(it, fontSize = 12.sp, color = RadioColors.muted, modifier = Modifier.padding(horizontal = 4.dp)) }
    }
}

@Composable
private fun Label(text: String) = Text(text, fontSize = 14.sp, color = RadioColors.muted)

@Composable
private fun ToggleRow(title: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth().heightIn(min = 40.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(title, color = RadioColors.text, fontSize = 15.sp, modifier = Modifier.weight(1f))
        Switch(checked, onChange, colors = SwitchDefaults.colors(checkedTrackColor = RadioColors.lime,
            checkedThumbColor = Color.Black))
    }
}

@Composable
private fun <T> Segments(options: List<T>, selected: T, title: (T) -> String, onSelect: (T) -> Unit) {
    SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
        options.forEachIndexed { i, option ->
            SegmentedButton(selected = option == selected, onClick = { onSelect(option) },
                shape = SegmentedButtonDefaults.itemShape(i, options.size),
                colors = SegmentedButtonDefaults.colors(activeContainerColor = RadioColors.surface2,
                    activeContentColor = RadioColors.lime, inactiveContainerColor = RadioColors.surface,
                    inactiveContentColor = RadioColors.muted)) {
                Text(title(option))
            }
        }
    }
}

@Composable
private fun HostField(value: String, label: String, onChange: (String) -> Unit, onDone: () -> Unit) {
    OutlinedTextField(value, onChange, Modifier.fillMaxWidth(), label = { Text(label) }, singleLine = true,
        keyboardOptions = KeyboardOptions(autoCorrectEnabled = false, keyboardType = KeyboardType.Uri, imeAction = ImeAction.Done),
        keyboardActions = KeyboardActions(onDone = { onDone() }),
        colors = OutlinedTextFieldDefaults.colors(focusedBorderColor = RadioColors.lime, unfocusedBorderColor = RadioColors.border))
}
