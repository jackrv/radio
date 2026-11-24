"use strict";

// Закриття клавіатури без окремої кнопки (вона перекривала інтерфейс):
// дотик до фону, Escape або перехід до іншого розділу.
(() => {
  const root = document.documentElement;
  let focused = null;

  function isTextField(element) {
    return element?.matches?.('textarea, input:not([type="checkbox"]):not([type="radio"]):not([type="button"]):not([type="submit"])');
  }

  function update() {
    // Під час введення ховаємо нижню навігацію, щоб вона не перекривала поле.
    root.classList.toggle("text-entry-active", Boolean(isTextField(document.activeElement)));
  }

  function dismiss() {
    if (focused) focused.blur();
    if (isTextField(document.activeElement)) document.activeElement.blur();
    try {
      // Supported by current Telegram clients; blur remains the fallback.
      window.Telegram?.WebApp?.hideKeyboard?.();
    } catch (_) { /* Older Telegram versions may reject this method. */ }
    focused = null;
    root.classList.remove("text-entry-active");
  }

  document.addEventListener("focusin", event => {
    if (isTextField(event.target)) focused = event.target;
    update();
  });
  document.addEventListener("focusout", () => {
    // Let focus move to another field without flickering the layout.
    window.requestAnimationFrame(update);
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") dismiss();
  });
  document.addEventListener("pointerdown", event => {
    // Background taps close the keyboard; controls retain their normal actions.
    if (focused && !event.target.closest('input, textarea, select, button, a, label, summary, audio, [contenteditable]')) dismiss();
  });
  document.addEventListener("radio345:view", dismiss);
})();
