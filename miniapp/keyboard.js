"use strict";

// A visible way out of text entry on iOS/Android Telegram WebViews.
(() => {
  const done = document.getElementById("keyboard-done");
  const root = document.documentElement;
  const viewport = window.visualViewport;
  let focused = null;

  function isTextField(element) {
    return element?.matches?.('textarea, input:not([type="checkbox"]):not([type="radio"]):not([type="button"]):not([type="submit"])');
  }

  function position() {
    const editing = isTextField(document.activeElement);
    done.hidden = !editing;
    root.classList.toggle("text-entry-active", Boolean(editing));
    if (!editing) return;
    const height = viewport?.height || window.Telegram?.WebApp?.viewportHeight || window.innerHeight;
    const top = viewport?.offsetTop || 0;
    done.style.top = `${Math.max(top + 8, top + height - 56)}px`;
  }

  function dismiss() {
    if (focused) focused.blur();
    if (isTextField(document.activeElement)) document.activeElement.blur();
    try {
      // Supported by current Telegram clients; blur remains the fallback.
      window.Telegram?.WebApp?.hideKeyboard?.();
    } catch (_) { /* Older Telegram versions may reject this method. */ }
    focused = null;
    done.hidden = true;
    root.classList.remove("text-entry-active");
  }

  document.addEventListener("focusin", event => {
    if (isTextField(event.target)) focused = event.target;
    position();
  });
  document.addEventListener("focusout", () => {
    // Let focus move to another field without flickering the button.
    window.requestAnimationFrame(position);
  });
  done.addEventListener("pointerdown", event => event.preventDefault());
  done.addEventListener("click", dismiss);
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") dismiss();
  });
  document.addEventListener("pointerdown", event => {
    // Background taps close the keyboard; controls retain their normal actions.
    if (focused && !event.target.closest('input, textarea, select, button, a, label, summary, audio, [contenteditable]')) dismiss();
  });
  document.addEventListener("radio345:view", dismiss);
  viewport?.addEventListener("resize", position);
  viewport?.addEventListener("scroll", position);
  window.addEventListener("resize", position);
  window.Telegram?.WebApp?.onEvent?.("viewportChanged", position);
})();
