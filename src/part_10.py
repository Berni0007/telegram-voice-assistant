if __name__ == "__main__":
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(restore_runtime_jobs)
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
    app.add_handler(CommandHandler("watch", watch_command))
    app.add_handler(CommandHandler("watches", watches_command))
    app.add_handler(CommandHandler("delwatch", delete_watch_command))
    app.add_handler(CommandHandler("projects", projects_command))
    app.add_handler(CommandHandler("project", project_command))
    app.add_handler(CommandHandler("projectinfo", project_info_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(
        MessageHandler(filters.PHOTO, photo_message)
    )
    app.add_handler(
        MessageHandler(filters.VOICE, voice_message)
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, echo_text)
    )

    logger.info(
        "Starting bot: llm=%s vision=%s asr=%s/%s tts=%s/%s voice=%s reminders_tz=%s auto_project=%s watches=%s",
        LLM_MODEL,
        VISION_MODEL,
        NVIDIA_ASR_SERVER,
        ASR_LANGUAGE,
        NVIDIA_TTS_SERVER,
        TTS_LANGUAGE,
        TTS_VOICE or "<auto>",
        REMINDER_TIMEZONE,
        AUTO_PROJECT_ENABLED,
        WATCH_ENABLED,
    )
    app.run_polling()
