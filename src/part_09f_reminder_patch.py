# This module is intentionally loaded after part_09 handlers are defined.
# It wraps message handlers so reminder requests are intercepted before the LLM.

_original_text_handler = globals().get("text_handler") or globals().get("handle_text")
_original_voice_handler = globals().get("voice_handler") or globals().get("handle_voice")

if _original_text_handler is not None:
    async def _reminder_aware_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.message.text if update.message else "") or ""
        if await maybe_handle_reminder_text(update, context, text):
            return
        return await _original_text_handler(update, context)

    if "text_handler" in globals():
        text_handler = _reminder_aware_text_handler
    if "handle_text" in globals():
        handle_text = _reminder_aware_text_handler

# Voice reminders are handled after ASR only if the project's voice handler exposes
# the recognized text to maybe_handle_reminder_text. Text reminders work immediately.
