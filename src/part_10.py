if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mode", mode_command))
    app.add_handler(CommandHandler("voice", voice_command))
    app.add_handler(CommandHandler("new", new_dialog))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("forget", forget_command))
    app.add_handler(CommandHandler("voices", voices))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(
        MessageHandler(filters.VOICE, voice_message)
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, echo_text)
    )

    logger.info(
        "Starting bot: llm=%s asr=%s/%s tts=%s/%s voice=%s",
        LLM_MODEL,
        NVIDIA_ASR_SERVER,
        ASR_LANGUAGE,
        NVIDIA_TTS_SERVER,
        TTS_LANGUAGE,
        TTS_VOICE or "<auto>",
    )
    app.run_polling()
