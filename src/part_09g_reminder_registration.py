# Reminder registration helpers. part_10 calls register_reminder_features(application).


def register_reminder_features(application) -> None:
    application.add_handler(CommandHandler("reminders", reminders_command))
    application.add_handler(CommandHandler("delreminder", delete_reminder_command))

    previous_post_init = application.post_init

    async def _combined_post_init(app):
        if previous_post_init is not None:
            await previous_post_init(app)
        await restore_reminders(app)

    application.post_init = _combined_post_init
