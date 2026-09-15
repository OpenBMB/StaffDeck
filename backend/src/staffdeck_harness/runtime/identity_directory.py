"""The selected identity adapter decides which directory entries are internal users."""
def is_internal_actor(user):
    if user is None:
        return False
    from app.config import get_settings
    from staffdeck_harness.security.profile import get_profile
    identity = get_profile(get_settings()).identity
    accept = getattr(identity, "is_internal_user", None)
    return bool(accept(user)) if callable(accept) else getattr(user, "source", None) == "web"
