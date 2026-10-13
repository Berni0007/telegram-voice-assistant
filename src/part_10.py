if __name__ == "__main__":
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(restore_reminders)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mode", mode_command))
    app.add_handler(CommandHandler("voice", voice_command))
    app.add_handler(CommandHandler("new", new_dialog))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("forget", forget_command))
    app.add_handler(CommandHandler("voices", voices))
    app.add_handler(CommandHandler("diag", diag_command))
    app.add_handler(CommandHandler("reminders", reminders_command))
    app.add_handler(CommandHandler("delreminder", delete_reminder_command))
    app.add_handler(CommandHandler("projects", projects_command))
    app.add_handler(CommandHandler("project", project_command))
    app.add_handler(CommandHandler("projectinfo", project_info_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(
        MessageHandler(filters.VOICE, voice_message)
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, echo_text)
    )

    logger.info(
        "Starting bot: llm=%s asr=%s/%s tts=%s/%s voice=%s reminders_tz=%s",
        LLM_MODEL,
        NVIDIA_ASR_SERVER,
        ASR_LANGUAGE,
        NVIDIA_TTS_SERVER,
        TTS_LANGUAGE,
        TTS_VOICE or "<auto>",
        REMINDER_TIMEZONE,
    )
    app.run_polling()
