# Runtime activation shim.
# If the Application instance already exists at this point, register immediately.
for _app_name in ("application", "app"):
    _app_obj = globals().get(_app_name)
    if _app_obj is not None:
        try:
            register_reminder_features(_app_obj)
        except Exception:
            logger.exception("Reminder feature registration failed")
        break
