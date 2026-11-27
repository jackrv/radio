"""Профіль каналу запису: який мікрофон/вхід чув ефір.

Зміна мікрофона змінює звук, тож пороги, статистику й дані для навчання порівнюють
у межах одного профілю. Нові записи несуть ``input`` = {"device", "profile"} з вузла
ефіру; старі визначаються за джерелом: до Pi слухав мікрофон Mac, на Pi — вебкамера.
"""

MAC_MIC = "mac-mic"
PI_WEBCAM = "pi-webcam"
PI_USB_AUDIO = "pi-usb-audio"   # USB-аудіо AB17X з мікрофоном, з 27.11.2026

TITLES = {MAC_MIC: "мікрофон Mac", PI_WEBCAM: "вебкамера на Pi", PI_USB_AUDIO: "USB-аудіо на Pi"}


def profile_of(data):
    """Профіль запису з його JSON ``data``; ніколи не порожній."""
    data = data if isinstance(data, dict) else {}
    source = data.get("input")
    if isinstance(source, dict):
        for key in ("profile", "device"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    # Сегменти з Pi приходять без мітки часу PortAudio («legacy»); на Mac вона є,
    # а ще старіші записи Mac поля не мають зовсім.
    return PI_WEBCAM if data.get("capture_source") == "legacy" else MAC_MIC


def title(profile):
    return TITLES.get(profile, profile)
