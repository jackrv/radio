# 🤖 Рація на Android

[← Вікі](README.md) · [Рація на iPhone](iphone-radio.md) — канали, «Вдома», PTT і налаштування вузла ті самі.

Порт застосунку для iPhone: «Відібрані» / «Наживо» (з відсіюванням сигналів), історія з повтором фраз,
звук у фоні й при заблокованому екрані, медіасповіщення з кнопкою «Стоп», сповіщення з текстом фрази,
PTT і «Вдома», окремі адреси Mac і Pi та окремий вузол текстів. Сервер той самий, змін на ньому немає.

Код: [apps/android/](../apps/android/) (Kotlin, Jetpack Compose, Android 8+).

| Файл | Що робить |
| --- | --- |
| `Feeds.kt` | список фраз, кеш, стан джерела, адреси (як `Feeds.swift`) |
| `Mp3Frames.kt`, `StreamPlayer.kt` | MP3-потік → MediaCodec → AudioTrack; черга понад 2,5 с («Наживо») / 6 с відкидається |
| `Transmitter.kt` | мікрофон 16 кГц PCM16 → WebSocket `/transmit` |
| `RadioModel.kt` | два опитування `/events`, повтори, «Вдома», сповіщення, MediaSession |
| `RadioService.kt` | служба переднього плану, поки слухаєте: wake/Wi-Fi lock, медіасповіщення |

## Збирання

Потрібні JDK 17 і Android SDK (без Android Studio):

```sh
brew install openjdk@17 && brew install --cask android-commandlinetools
export JAVA_HOME=/opt/homebrew/opt/openjdk@17 ANDROID_HOME=/opt/homebrew/share/android-commandlinetools
sdkmanager "platforms;android-35" "build-tools;35.0.0" "platform-tools"
echo "sdk.dir=$ANDROID_HOME" > apps/android/local.properties
cd apps/android && ./gradlew testReleaseUnitTest assembleRelease
```

APK: `apps/android/app/build/outputs/apk/release/app-release.apk` (≈1,4 МБ, R8). Підпис — локальний
debug-ключ `~/.android/debug.keystore`: оновлення ставиться поверх лише з тим самим ключем, тож збирайте на одному Mac.

## Встановлення

Перенесіть APK на телефон і відкрийте його (дозвольте встановлення з цього джерела) або через кабель:
`adb install -r app-release.apk` (на телефоні — «Для розробників» → «Налагодження USB»).
Далі Tailscale на телефоні, адреса вузла в «Підключенні» — як для iPhone. Строку підпису немає: переустановлювати не треба.

## Відмінності від iPhone

- Дозволи запитуються в мить потреби: сповіщення — при першому «Слухати» (Android 13+), мікрофон — при першому PTT;
  передача почнеться з наступного натискання.
- Аудіофокус не запитується: дзвінок чи інший плеєр не зупиняє ефір.
- Виробники з агресивною економією батареї (Xiaomi, Huawei тощо) можуть зупиняти фон: вимкніть оптимізацію батареї для «Рація 345».
